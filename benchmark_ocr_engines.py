import argparse
import hashlib
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


ENGINE_LABELS = {
    "paddle_current": "PaddleOCR atual",
    "paddle_no_upscale": "PaddleOCR atual sem upscale",
    "paddle_mobile": "PaddleOCR mobile",
    "rapidocr": "RapidOCR / ONNX Runtime",
    "easyocr": "EasyOCR",
    "tesseract": "Tesseract",
}

EXPECTED_TEXTS = {
    "002.png": ["TAP TAP", "JAMIE"],
    "003.png": ["PREGNANT"],
    "005.png": ["REALLY"],
    "006.png": ["REALLY"],
    "007.png": [
        "GONNA BE PARENTS",
        "THATS AMAZING",
        "FINISH POOPING QUICK",
    ],
    "008.png": ["PLUS ONE CREATED", "GLORIA KIM"],
    "009.png": ["GLORIA", "JAMIE", "BEEN MARRIED FOR TWO YEARS"],
}

PREVIEW_IMAGES = ("005.png", "007.png", "009.png")


@dataclass
class DetectedLine:
    text: str
    confidence: float
    polygon: np.ndarray

    @property
    def box(self):
        xs = self.polygon[:, 0]
        ys = self.polygon[:, 1]
        x1 = int(xs.min())
        y1 = int(ys.min())
        x2 = int(xs.max())
        y2 = int(ys.max())
        return x1, y1, max(1, x2 - x1), max(1, y2 - y1)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Benchmark isolado de motores OCR para o Tradutor.Ia.",
    )
    parser.add_argument(
        "--input-folder",
        default=str(Path("output/ocr_benchmark/input")),
    )
    parser.add_argument(
        "--output-root",
        default=str(Path("output/ocr_benchmark")),
    )
    parser.add_argument(
        "--engine",
        choices=sorted(ENGINE_LABELS),
        help="Executa apenas um motor e salva o resultado bruto.",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Agrega resultados existentes e gera relatórios/previews.",
    )
    parser.add_argument(
        "--record-skip",
        choices=sorted(ENGINE_LABELS),
        help="Registra um motor como indisponível.",
    )
    parser.add_argument(
        "--integrated-baseline",
        help="timing_report.json do pipeline estavel para comparacao.",
    )
    parser.add_argument(
        "--integrated-candidate",
        help="timing_report.json do pipeline com o candidato.",
    )
    parser.add_argument(
        "--integrated-engine",
        choices=sorted(ENGINE_LABELS),
        help="Motor usado no timing_report integrado do candidato.",
    )
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    output_root = Path(args.output_root).resolve()
    input_folder = Path(args.input_folder).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "raw").mkdir(parents=True, exist_ok=True)

    if args.record_skip:
        payload = {
            "engine": args.record_skip,
            "label": ENGINE_LABELS[args.record_skip],
            "status": "skipped",
            "reason": args.reason or "unavailable",
        }
        _write_json(_raw_path(output_root, args.record_skip), payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.aggregate:
        report = aggregate_results(
            input_folder,
            output_root,
            integrated_baseline=args.integrated_baseline,
            integrated_candidate=args.integrated_candidate,
            integrated_engine=args.integrated_engine,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if not args.engine:
        parser.error("use --engine, --aggregate ou --record-skip")

    result = benchmark_engine(args.engine, input_folder)
    _write_json(_raw_path(output_root, args.engine), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def benchmark_engine(engine_name, input_folder):
    images = sorted(input_folder.glob("*.png"))
    if not images:
        raise RuntimeError(f"Nenhuma imagem encontrada em {input_folder}")

    started = time.perf_counter()
    try:
        detector = _create_detector(engine_name)
    except Exception as exc:
        return {
            "engine": engine_name,
            "label": ENGINE_LABELS[engine_name],
            "status": "skipped",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    initialization_seconds = time.perf_counter() - started

    pages = []
    all_lines = []
    errors = []
    inference_started = time.perf_counter()
    for image_path in images:
        image = cv2.imread(str(image_path))
        page_started = time.perf_counter()
        if image is None:
            errors.append(
                {"image": image_path.name, "error": "image_load_failed"}
            )
            pages.append(
                {
                    "image": image_path.name,
                    "elapsed_seconds": time.perf_counter() - page_started,
                    "error": "image_load_failed",
                    "lines": [],
                }
            )
            continue

        try:
            lines = detector(image)
            lines = [
                line
                for line in lines
                if isinstance(line, DetectedLine) and line.text.strip()
            ]
            error = None
        except Exception as exc:
            lines = []
            error = f"{type(exc).__name__}: {exc}"
            errors.append({"image": image_path.name, "error": error})

        elapsed = time.perf_counter() - page_started
        records = [_serialize_line(line) for line in lines]
        pages.append(
            {
                "image": image_path.name,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "elapsed_seconds": round(elapsed, 6),
                "error": error,
                "lines": records,
            }
        )
        all_lines.extend(records)

    inference_seconds = time.perf_counter() - inference_started
    total_seconds = time.perf_counter() - started
    metrics = _quality_metrics(pages)
    return {
        "engine": engine_name,
        "label": ENGINE_LABELS[engine_name],
        "status": "success",
        "python": sys.version.split()[0],
        "dataset_hash": dataset_hash(images),
        "image_count": len(images),
        "initialization_seconds": round(initialization_seconds, 6),
        "inference_seconds": round(inference_seconds, 6),
        "total_seconds": round(total_seconds, 6),
        "average_seconds_per_image": round(
            inference_seconds / max(1, len(images)),
            6,
        ),
        "line_count": len(all_lines),
        "average_confidence": round(
            sum(line["confidence"] for line in all_lines)
            / max(1, len(all_lines)),
            6,
        ),
        "empty_or_junk_count": sum(
            _is_junk(line["text"]) for line in all_lines
        ),
        "error_count": len(errors),
        "errors": errors,
        **metrics,
        "pages": pages,
    }


def _create_detector(engine_name):
    if engine_name in {"paddle_current", "paddle_no_upscale"}:
        from ocr_engine import OCREngine

        engine = OCREngine("en")
        upscale = engine_name == "paddle_current"

        def detect(image):
            return [
                DetectedLine(
                    text=line.text,
                    confidence=float(line.confidence),
                    polygon=np.asarray(line.polygon, dtype=np.int32),
                )
                for line in engine.detect_lines(image, upscale=upscale)
            ]

        return detect

    if engine_name == "paddle_mobile":
        from paddleocr import PaddleOCR
        from ocr_engine import _extract_lines

        engine = PaddleOCR(
            lang="en",
            text_detection_model_name="PP-OCRv4_mobile_det",
            text_recognition_model_name="en_PP-OCRv4_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

        def detect(image):
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            result = engine.predict(input=rgb)
            return [
                DetectedLine(
                    text=line.text,
                    confidence=float(line.confidence),
                    polygon=np.asarray(line.polygon, dtype=np.int32),
                )
                for line in _extract_lines(result, 1.0)
            ]

        return detect

    if engine_name == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()

        def detect(image):
            output = engine(image)
            result = output[0] if isinstance(output, tuple) else output
            lines = []
            for item in result or []:
                if not isinstance(item, (list, tuple)) or len(item) < 3:
                    continue
                polygon, text, confidence = item[:3]
                polygon = np.asarray(polygon, dtype=np.int32).reshape(-1, 2)
                lines.append(
                    DetectedLine(
                        text=str(text),
                        confidence=float(confidence),
                        polygon=polygon,
                    )
                )
            return lines

        return detect

    if engine_name == "easyocr":
        import easyocr

        reader = easyocr.Reader(["en"], gpu=False, verbose=False)

        def detect(image):
            result = reader.readtext(
                image,
                detail=1,
                paragraph=False,
                batch_size=1,
                workers=0,
            )
            lines = []
            for polygon, text, confidence in result:
                lines.append(
                    DetectedLine(
                        text=str(text),
                        confidence=float(confidence),
                        polygon=np.asarray(polygon, dtype=np.int32).reshape(
                            -1,
                            2,
                        ),
                    )
                )
            return lines

        return detect

    if engine_name == "tesseract":
        import pytesseract

        tesseract_executable = Path(
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
        if tesseract_executable.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(
                tesseract_executable
            )

        def detect(image):
            data = pytesseract.image_to_data(
                image,
                lang="eng",
                config="--psm 11",
                output_type=pytesseract.Output.DICT,
            )
            groups = {}
            count = len(data.get("text", []))
            for index in range(count):
                text = str(data["text"][index]).strip()
                try:
                    confidence = float(data["conf"][index])
                except (TypeError, ValueError):
                    confidence = -1.0
                if not text or confidence < 0:
                    continue
                key = (
                    data["block_num"][index],
                    data["par_num"][index],
                    data["line_num"][index],
                )
                x = int(data["left"][index])
                y = int(data["top"][index])
                width = int(data["width"][index])
                height = int(data["height"][index])
                group = groups.setdefault(
                    key,
                    {
                        "texts": [],
                        "confidences": [],
                        "x1": x,
                        "y1": y,
                        "x2": x + width,
                        "y2": y + height,
                    },
                )
                group["texts"].append(text)
                group["confidences"].append(confidence / 100.0)
                group["x1"] = min(group["x1"], x)
                group["y1"] = min(group["y1"], y)
                group["x2"] = max(group["x2"], x + width)
                group["y2"] = max(group["y2"], y + height)

            lines = []
            for group in groups.values():
                x1, y1, x2, y2 = (
                    group["x1"],
                    group["y1"],
                    group["x2"],
                    group["y2"],
                )
                polygon = np.asarray(
                    [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                    dtype=np.int32,
                )
                lines.append(
                    DetectedLine(
                        text=" ".join(group["texts"]),
                        confidence=sum(group["confidences"])
                        / len(group["confidences"]),
                        polygon=polygon,
                    )
                )
            return sorted(lines, key=lambda line: (line.box[1], line.box[0]))

        return detect

    raise ValueError(f"Motor desconhecido: {engine_name}")


def aggregate_results(
    input_folder,
    output_root,
    integrated_baseline=None,
    integrated_candidate=None,
    integrated_engine=None,
):
    raw_results = []
    for engine_name in ENGINE_LABELS:
        path = _raw_path(output_root, engine_name)
        if path.is_file():
            raw_results.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            raw_results.append(
                {
                    "engine": engine_name,
                    "label": ENGINE_LABELS[engine_name],
                    "status": "not_run",
                    "reason": "resultado ausente",
                }
            )

    successes = [
        result
        for result in raw_results
        if result.get("status") == "success"
    ]
    baseline = next(
        (
            result
            for result in successes
            if result["engine"] == "paddle_current"
        ),
        None,
    )
    if baseline is None:
        raise RuntimeError("Resultado paddle_current ausente.")

    for result in successes:
        result["speedup_vs_paddle_percent"] = round(
            (
                (baseline["inference_seconds"] - result["inference_seconds"])
                / baseline["inference_seconds"]
            )
            * 100,
            3,
        )
        result["line_count_ratio_vs_paddle"] = round(
            result["line_count"] / max(1, baseline["line_count"]),
            4,
        )
        result["quality_similar_to_paddle"] = _quality_is_similar(
            result,
            baseline,
        )

    candidates = [
        result
        for result in successes
        if result["engine"] != "paddle_current"
        and result.get("quality_similar_to_paddle")
    ]
    best_candidate = (
        min(candidates, key=lambda item: item["inference_seconds"])
        if candidates
        else baseline
    )
    integration_candidate = (
        best_candidate
        if best_candidate["engine"] != "paddle_current"
        and best_candidate["speedup_vs_paddle_percent"] >= 30.0
        else None
    )
    if integrated_engine:
        measured_candidate = next(
            (
                result
                for result in candidates
                if result["engine"] == integrated_engine
                and result["speedup_vs_paddle_percent"] >= 30.0
            ),
            None,
        )
        if measured_candidate:
            integration_candidate = measured_candidate

    report = {
        "dataset": {
            "input_folder": str(input_folder),
            "image_count": len(list(input_folder.glob("*.png"))),
            "dataset_hash": dataset_hash(sorted(input_folder.glob("*.png"))),
        },
        "baseline_engine": "paddle_current",
        "best_candidate": best_candidate["engine"],
        "integration_candidate": (
            integration_candidate["engine"]
            if integration_candidate
            else None
        ),
        "engines": raw_results,
    }
    if integrated_baseline and integrated_candidate:
        report["integrated_pipeline"] = _integrated_comparison(
            Path(integrated_baseline),
            Path(integrated_candidate),
            integrated_engine,
        )
    _write_json(output_root / "report.json", report)
    (output_root / "report.txt").write_text(
        _report_text(report),
        encoding="utf-8",
    )
    _create_boxes_preview(
        input_folder,
        baseline,
        output_root / "preview_ocr_boxes_paddle.jpg",
    )
    _create_boxes_preview(
        input_folder,
        best_candidate,
        output_root / "preview_ocr_boxes_best_candidate.jpg",
    )
    return report


def _integrated_comparison(baseline_path, candidate_path, engine=None):
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    def compact(payload, path):
        return {
            "report_path": str(path.resolve()),
            "total_seconds": float(payload["total_seconds"]),
            "ocr_seconds": float(payload["stage_seconds"]["ocr"]),
            "images": int(payload["processed_images"]),
            "pdf_pages": int(payload["quality_validation"]["pdf_pages"]),
            "ocr_lines": int(payload["ocr_detected_lines"]),
            "groups_formed": int(payload["groups_formed"]),
            "groups_translated": int(payload["groups_translated"]),
            "sfx_decorative_ignored": int(
                payload["groups_ignored_sfx_decorative"]
            ),
            "pages_with_error": int(payload["pages_with_error"]),
            "quality_validation_passed": bool(
                payload["quality_validation"]["passed"]
            ),
            "invalid_or_blank_pages": list(
                payload["quality_validation"]["invalid_or_blank_pages"]
            ),
            "pdf_path": str(payload["pdf_path"]),
            "preview_contact_sheet": str(payload["preview_contact_sheet"]),
            "preview_compare_sheet": str(payload["preview_compare_sheet"]),
        }

    baseline_summary = compact(baseline, baseline_path)
    candidate_summary = compact(candidate, candidate_path)
    return {
        "engine": engine,
        "baseline": baseline_summary,
        "candidate": candidate_summary,
        "total_reduction_percent": round(
            (
                (
                    baseline_summary["total_seconds"]
                    - candidate_summary["total_seconds"]
                )
                / baseline_summary["total_seconds"]
            )
            * 100,
            3,
        ),
        "ocr_reduction_percent": round(
            (
                (
                    baseline_summary["ocr_seconds"]
                    - candidate_summary["ocr_seconds"]
                )
                / baseline_summary["ocr_seconds"]
            )
            * 100,
            3,
        ),
        "quality_equivalent_by_automatic_checks": (
            candidate_summary["quality_validation_passed"]
            and not candidate_summary["invalid_or_blank_pages"]
            and candidate_summary["pages_with_error"] == 0
            and candidate_summary["pdf_pages"] == baseline_summary["pdf_pages"]
        ),
    }


def _quality_metrics(pages):
    key_checks = []
    for page in pages:
        expected = EXPECTED_TEXTS.get(page["image"], [])
        page_text = " ".join(line["text"] for line in page["lines"])
        for phrase in expected:
            matched, score = _phrase_match(phrase, page_text)
            key_checks.append(
                {
                    "image": page["image"],
                    "expected": phrase,
                    "matched": matched,
                    "score": round(score, 4),
                }
            )

    all_records = [
        (page, line)
        for page in pages
        for line in page.get("lines", [])
    ]
    valid_boxes = sum(
        _valid_polygon(
            line.get("polygon", []),
            page.get("width", 0),
            page.get("height", 0),
        )
        for page, line in all_records
    )
    return {
        "key_texts_detected": sum(
            check["matched"] for check in key_checks
        ),
        "key_texts_total": len(key_checks),
        "key_text_recall": round(
            sum(check["matched"] for check in key_checks)
            / max(1, len(key_checks)),
            4,
        ),
        "key_text_checks": key_checks,
        "compatible_box_count": valid_boxes,
        "incompatible_box_count": len(all_records) - valid_boxes,
        "boxes_compatible": valid_boxes == len(all_records),
    }


def _quality_is_similar(result, baseline):
    line_ratio = result["line_count"] / max(1, baseline["line_count"])
    recall_floor = max(0.75, baseline.get("key_text_recall", 0.0) - 0.12)
    junk_ratio = result["empty_or_junk_count"] / max(1, result["line_count"])
    baseline_junk_ratio = baseline["empty_or_junk_count"] / max(
        1,
        baseline["line_count"],
    )
    return bool(
        0.75 <= line_ratio <= 1.55
        and result.get("key_text_recall", 0.0) >= recall_floor
        and junk_ratio <= baseline_junk_ratio + 0.18
        and result.get("boxes_compatible")
        and result.get("error_count", 0) == 0
    )


def _phrase_match(expected, detected):
    expected_normalized = _normalize_text(expected)
    detected_normalized = _normalize_text(detected)
    if expected_normalized in detected_normalized:
        return True, 1.0

    expected_tokens = expected_normalized.split()
    detected_tokens = detected_normalized.split()
    if expected_tokens and all(token in detected_tokens for token in expected_tokens):
        return True, 0.95

    window_size = max(1, len(expected_tokens) + 2)
    best = 0.0
    for start in range(len(detected_tokens)):
        window = " ".join(detected_tokens[start : start + window_size])
        best = max(
            best,
            SequenceMatcher(None, expected_normalized, window).ratio(),
        )
    return best >= 0.72, best


def _normalize_text(text):
    text = str(text).upper().replace("’", "'")
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_junk(text):
    normalized = _normalize_text(text)
    if not normalized:
        return True
    alphanumeric = re.sub(r"[^A-Z0-9]", "", normalized)
    if len(alphanumeric) <= 1:
        return True
    return False


def _valid_polygon(polygon, width, height):
    try:
        points = np.asarray(polygon, dtype=float).reshape(-1, 2)
    except (TypeError, ValueError):
        return False
    if len(points) < 4 or width <= 0 or height <= 0:
        return False
    xs = points[:, 0]
    ys = points[:, 1]
    return bool(
        np.all(np.isfinite(points))
        and xs.min() >= 0
        and ys.min() >= 0
        and xs.max() <= width
        and ys.max() <= height
        and xs.max() > xs.min()
        and ys.max() > ys.min()
    )


def _create_boxes_preview(input_folder, result, target):
    pages_by_name = {
        page["image"]: page
        for page in result.get("pages", [])
    }
    canvas_width = 960
    header_height = 130
    row_height = 570
    background = (20, 22, 27)
    paper = (244, 239, 231)
    accent = (225, 102, 62)
    canvas = Image.new(
        "RGB",
        (canvas_width, header_height + row_height * len(PREVIEW_IMAGES) + 24),
        background,
    )
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, 16, header_height), fill=accent)
    draw.text(
        (42, 24),
        result.get("label", result.get("engine", "OCR")),
        font=_font(32, bold=True),
        fill=paper,
    )
    draw.text(
        (44, 77),
        (
            f"{result.get('line_count', 0)} linhas · "
            f"{result.get('inference_seconds', 0.0):.2f}s · "
            f"recall {result.get('key_text_recall', 0.0) * 100:.0f}%"
        ),
        font=_font(18),
        fill=(184, 187, 193),
    )

    colors = (
        (225, 102, 62),
        (35, 118, 93),
        (198, 153, 48),
        (72, 100, 170),
    )
    for row, image_name in enumerate(PREVIEW_IMAGES):
        y = header_height + row * row_height
        draw.rounded_rectangle(
            (24, y + 16, canvas_width - 24, y + row_height - 16),
            radius=10,
            fill=paper,
        )
        image_path = input_folder / image_name
        image = Image.open(image_path).convert("RGB")
        page = pages_by_name.get(image_name, {"lines": []})
        overlay = ImageDraw.Draw(image)
        for index, line in enumerate(page.get("lines", [])):
            polygon = [tuple(point) for point in line["polygon"]]
            color = colors[index % len(colors)]
            overlay.line(polygon + [polygon[0]], fill=color, width=4)
            x = min(point[0] for point in polygon)
            y_text = max(0, min(point[1] for point in polygon) - 22)
            label = str(index + 1)
            overlay.rectangle((x, y_text, x + 24, y_text + 22), fill=color)
            overlay.text(
                (x + 6, y_text + 2),
                label,
                font=_font(14, bold=True),
                fill=(255, 255, 255),
            )

        thumb = ImageOps.contain(image, (500, row_height - 72))
        image.close()
        canvas.paste(thumb, (44, y + 40))
        text_x = 576
        draw.text(
            (text_x, y + 42),
            f"PÁGINA {image_name[:-4]}",
            font=_font(18, bold=True),
            fill=(31, 34, 40),
        )
        text_y = y + 82
        for index, line in enumerate(page.get("lines", [])[:12]):
            color = colors[index % len(colors)]
            wrapped = _wrap_text(f"{index + 1}. {line['text']}", 34)
            draw.text(
                (text_x, text_y),
                wrapped,
                font=_font(15),
                fill=color,
                spacing=4,
            )
            text_y += 25 * max(1, wrapped.count("\n") + 1)
            if text_y > y + row_height - 50:
                break

    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=84, optimize=True, progressive=True)


def _report_text(report):
    lines = [
        "Tradutor.Ia - Benchmark de motores OCR",
        f"Dataset: {report['dataset']['image_count']} imagens",
        f"Hash: {report['dataset']['dataset_hash']}",
        "",
    ]
    for result in report["engines"]:
        lines.append(f"[{result['label']}]")
        lines.append(f"Status: {result.get('status')}")
        if result.get("status") != "success":
            lines.append(f"Motivo: {result.get('reason', 'n/a')}")
            lines.append("")
            continue
        lines.extend(
            [
                f"Tempo total: {result['total_seconds']:.2f}s",
                f"Inferencia: {result['inference_seconds']:.2f}s",
                f"Media/imagem: {result['average_seconds_per_image']:.2f}s",
                f"Linhas: {result['line_count']}",
                f"Confianca media: {result['average_confidence']:.3f}",
                f"Vazios/lixo: {result['empty_or_junk_count']}",
                (
                    "Textos principais: "
                    f"{result['key_texts_detected']}/{result['key_texts_total']}"
                ),
                f"Boxes compativeis: {result['boxes_compatible']}",
                f"Erros: {result['error_count']}",
                (
                    "Ganho vs Paddle atual: "
                    f"{result.get('speedup_vs_paddle_percent', 0.0):.2f}%"
                ),
                (
                    "Qualidade semelhante: "
                    f"{result.get('quality_similar_to_paddle', False)}"
                ),
                "",
            ]
        )
    lines.extend(
        [
            f"Melhor candidato: {report['best_candidate']}",
            (
                "Candidato para integracao: "
                f"{report['integration_candidate'] or 'nenhum'}"
            ),
        ]
    )
    integrated = report.get("integrated_pipeline")
    if integrated:
        baseline = integrated["baseline"]
        candidate = integrated["candidate"]
        lines.extend(
            [
                "",
                (
                    "[Pipeline integrado: Paddle atual vs "
                    f"{integrated.get('engine') or 'candidato'}]"
                ),
                (
                    f"Baseline: {baseline['total_seconds']:.2f}s total; "
                    f"{baseline['ocr_seconds']:.2f}s OCR; "
                    f"{baseline['ocr_lines']} linhas; "
                    f"{baseline['groups_translated']} grupos traduzidos"
                ),
                (
                    f"Candidato: {candidate['total_seconds']:.2f}s total; "
                    f"{candidate['ocr_seconds']:.2f}s OCR; "
                    f"{candidate['ocr_lines']} linhas; "
                    f"{candidate['groups_translated']} grupos traduzidos"
                ),
                (
                    "Reducao total: "
                    f"{integrated['total_reduction_percent']:.2f}%"
                ),
                (
                    "Reducao OCR: "
                    f"{integrated['ocr_reduction_percent']:.2f}%"
                ),
                (
                    "Qualidade equivalente nos checks automaticos: "
                    f"{integrated['quality_equivalent_by_automatic_checks']}"
                ),
            ]
        )
    return "\n".join(lines) + "\n"


def _serialize_line(line):
    return {
        "text": str(line.text).strip(),
        "confidence": float(line.confidence),
        "polygon": np.asarray(line.polygon, dtype=np.int32).tolist(),
        "box": [int(value) for value in line.box],
    }


def dataset_hash(paths):
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _font(size, bold=False):
    candidates = (
        [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"]
        if bold
        else [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _wrap_text(text, width):
    words = str(text).split()
    lines = []
    current = []
    for word in words:
        candidate = " ".join(current + [word])
        if len(candidate) <= width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def _raw_path(output_root, engine_name):
    return output_root / "raw" / f"{engine_name}.json"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _json_safe(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


if __name__ == "__main__":
    main()
