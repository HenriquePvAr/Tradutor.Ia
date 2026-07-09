import argparse
import json
import os
import shutil
import time

from PIL import Image, ImageStat

import config
from down import download_images, force_remove
from json_utils import dump_json, dumps_json
from ocr_balloon import process_image_file
from pdf import generate_pdf
from translator_nllb import get_translator


def main():
    parser = argparse.ArgumentParser(description="Teste controlado do pipeline Webtoon.")
    parser.add_argument("--url", default=config.TEST_URL)
    parser.add_argument("--max-images", type=int, default=config.TEST_MAX_IMAGES)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Processa todas as imagens validas do capitulo.",
    )
    parser.add_argument("--debug-folder", default=config.DEBUG_FOLDER)
    parser.add_argument("--keep-debug", action="store_true")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Evita imagens de debug por pagina, mantendo OCR, traducao e PDF.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Usa cache, paralelismo, resume e relatorio detalhado de performance.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Coleta, valida e audita as imagens sem executar OCR, traducao ou PDF.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignora caches e reprocessa todas as etapas.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Tambem ignora o cache de download. Por padrao, --force reprocessa OCR/traducao/render sem baixar tudo de novo.",
    )
    parser.add_argument(
        "--page-indices",
        default="",
        help="Processa apenas paginas validas especificas do capitulo, ex: 20,28,32,48.",
    )
    parser.add_argument(
        "--output-folder",
        default=os.path.join("output", "full_chapter"),
        help="Pasta dos artefatos de benchmark.",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=("paddle", "rapidocr"),
        help="Sobrescreve o motor OCR nesta execucao.",
    )
    args = parser.parse_args()
    _apply_ocr_cli_config(args.ocr_engine)

    if not args.full and args.max_images <= 0:
        parser.error("--max-images deve ser maior que zero.")

    if args.download_only:
        _run_download_only(args)
        return

    if args.benchmark:
        from benchmark_pipeline import run_benchmark

        run_benchmark(args)
        return

    started_at = time.perf_counter()
    debug_folder = os.path.abspath(args.debug_folder)
    if os.path.exists(debug_folder) and not args.keep_debug:
        force_remove(debug_folder)
    os.makedirs(debug_folder, exist_ok=True)

    print(f"Teste Webtoon: {args.url}")
    print(f"Limite de imagens: {'capitulo completo' if args.full else args.max_images}")
    print(f"Modo rapido: {'sim' if args.fast else 'nao'}")
    print(f"Debug: {debug_folder}")

    max_images = None if args.full else args.max_images
    image_paths = download_images(
        args.url,
        max_images=max_images,
        debug_folder=debug_folder,
        progress_callback=lambda current, total, msg: print(f"{msg}: {current}/{total}"),
    )

    translator, ocr_lang = get_translator("3")
    processed_paths = []
    page_summaries = []

    for idx, image_path in enumerate(image_paths, start=1):
        out_path, debug_data = process_image_file(
            image_path,
            ocr_lang,
            translator,
            font_path=config.FONT_PATH,
            debug_folder=None if args.fast else debug_folder,
            page_index=idx,
            return_debug=True,
        )

        if out_path and _valid_pdf_image(out_path):
            processed_paths.append(out_path)
        else:
            print(f"Imagem final invalida, ignorada no PDF: {out_path}")

        page_summaries.append(debug_data)

    if args.full:
        pdf_name = f"webtoon_full_{len(processed_paths):03}.pdf"
    else:
        pdf_name = f"webtoon_test_{args.max_images:03}.pdf"
    pdf_path = os.path.join(debug_folder, pdf_name)
    if processed_paths:
        generate_pdf(processed_paths, pdf_path)
    else:
        pdf_path = None
        print("Nenhuma imagem processada valida para gerar PDF.")

    compare_paths = [] if args.fast else _write_samples(debug_folder, processed_paths)
    elapsed_seconds = time.perf_counter() - started_at
    summary = _summary_payload(
        args,
        debug_folder,
        image_paths,
        processed_paths,
        page_summaries,
        pdf_path,
        compare_paths,
        elapsed_seconds,
    )
    summary_path = os.path.join(debug_folder, "test_summary.json")
    with open(summary_path, "w", encoding="utf-8") as file:
        dump_json(summary, file, ensure_ascii=False, indent=2)

    print(dumps_json(summary, ensure_ascii=False, indent=2))
    print(f"Resumo salvo em: {summary_path}")
    _print_final_report(summary)


def _run_download_only(args):
    output_folder = os.path.abspath(args.output_folder)
    input_folder = os.path.join(output_folder, "input")
    os.makedirs(output_folder, exist_ok=True)
    max_images = None if args.full else args.max_images
    print(f"Download-only: {args.url}", flush=True)
    print(
        f"Escopo: {'capitulo completo' if args.full else f'{max_images} imagens'}",
        flush=True,
    )
    image_paths = download_images(
        args.url,
        max_images=max_images,
        debug_folder=output_folder,
        target_folder=input_folder,
        force=bool(args.force or args.force_download),
        progress_callback=lambda current, total, message: print(
            f"{message}: {current}/{total}", flush=True
        ),
    )
    report_path = os.path.join(output_folder, "downloaded_images.json")
    with open(report_path, "r", encoding="utf-8") as file:
        report = json.load(file)
    gate = report.get("download_gate") or {}
    print(f"Imagens validas: {len(image_paths)}", flush=True)
    print(
        f"Download gate: {'aprovado' if gate.get('passed') else 'reprovado'}",
        flush=True,
    )
    print(f"Relatorio: {os.path.join(output_folder, 'download_report.txt')}", flush=True)
    print(
        f"Contact sheet: {os.path.join(output_folder, 'download_contact_sheet.jpg')}",
        flush=True,
    )
    if not gate.get("passed"):
        raise RuntimeError(
            "Download gate reprovado: " + ", ".join(gate.get("reasons") or [])
        )


def _apply_ocr_cli_config(engine):
    if not engine:
        return
    os.environ["OCR_ENGINE"] = engine
    config.OCR_ENGINE = engine
    if engine == "rapidocr":
        os.environ["RAPIDOCR_ENABLED"] = "True"
        os.environ["OCR_FALLBACK_ENGINE"] = "paddle"
        os.environ["OCR_HYBRID_FALLBACK"] = "True"
        config.RAPIDOCR_ENABLED = True
        config.OCR_FALLBACK_ENGINE = "paddle"
        config.OCR_HYBRID_FALLBACK = True
        config.POST_RENDER_OCR_VALIDATION = True


def _valid_pdf_image(path):
    if not path or not os.path.exists(path):
        return False

    try:
        with Image.open(path) as img:
            img.load()
            if img.width < 100 or img.height < 100:
                return False
            small = img.convert("L").resize((32, 32))
            stat = ImageStat.Stat(small)
            return stat.var[0] > 5
    except Exception:
        return False


def _write_samples(debug_folder, processed_paths):
    for idx, path in enumerate(processed_paths[:3], start=1):
        target = os.path.join(debug_folder, f"final_sample_{idx:03}.png")
        shutil.copyfile(path, target)

    compare_paths = []
    for idx in range(1, 4):
        compare = os.path.join(debug_folder, f"page_{idx:03}_compare.png")
        target = os.path.join(debug_folder, f"compare_sample_{idx:03}.png")
        if os.path.exists(compare):
            shutil.copyfile(compare, target)
            compare_paths.append(target)

    return compare_paths


def _summary_payload(
    args,
    debug_folder,
    image_paths,
    processed_paths,
    page_summaries,
    pdf_path,
    compare_paths,
    elapsed_seconds,
):
    translated_items = []
    ignored = 0
    ignored_groups = 0
    ignored_lines = 0
    ocr_lines = 0
    translated_count = 0
    classification_counts = {
        "speech": 0,
        "narration": 0,
        "sfx": 0,
        "decorative": 0,
        "unknown": 0,
    }

    for page in page_summaries:
        ocr_lines += page.get("ocr_line_count", 0)
        page_ignored_lines = page.get("ignored_line_count", 0)
        page_ignored_groups = page.get("ignored_group_count", 0)
        ignored_lines += page_ignored_lines
        ignored_groups += page_ignored_groups
        ignored += page_ignored_lines + page_ignored_groups
        translated_count += page.get("translated_group_count", 0)
        for name in classification_counts:
            classification_counts[name] += page.get("classification_counts", {}).get(name, 0)

        for item in page.get("items", []):
            if item.get("sent_to_nvidia"):
                translated_items.append(
                    {
                        "id": item.get("id"),
                        "original": item.get("clean_text"),
                        "translation": item.get("translation"),
                    }
                )

    download_report = _load_json(os.path.join(debug_folder, "downloaded_images.json"))

    return {
        "url": args.url,
        "mode": "full" if args.full else "controlled",
        "fast": args.fast,
        "requested_max_images": None if args.full else args.max_images,
        "debug_folder": debug_folder,
        "total_dom_images": download_report.get("total_dom_images", 0),
        "total_unique_urls": download_report.get("total_unique_urls", 0),
        "downloaded_images": len(image_paths),
        "processed_images": len(processed_paths),
        "ocr_detected_lines": ocr_lines,
        "ignored_ocr_items": ignored,
        "ignored_ocr_lines": ignored_lines,
        "ignored_groups": ignored_groups,
        "sent_to_translation": translated_count,
        "classification_counts": classification_counts,
        "translated_examples": translated_items[:20],
        "elapsed_seconds": round(elapsed_seconds, 2),
        "pdf_path": pdf_path,
        "compare_paths": compare_paths,
    }


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def _print_final_report(summary):
    print("\nRelatorio final")
    print(f"Imagens encontradas no DOM: {summary['total_dom_images']}")
    print(f"URLs unicas: {summary['total_unique_urls']}")
    print(f"Imagens baixadas: {summary['downloaded_images']}")
    print(f"Imagens processadas: {summary['processed_images']}")
    print(f"Textos OCR detectados: {summary['ocr_detected_lines']}")
    print(f"Textos ignorados: {summary['ignored_ocr_items']}")
    print(f"Grupos ignorados: {summary['ignored_groups']}")
    print(f"Grupos enviados para traducao: {summary['sent_to_translation']}")
    counts = summary["classification_counts"]
    print(f"Classificados como speech: {counts['speech']}")
    print(f"Classificados como narration: {counts['narration']}")
    print(f"Classificados como sfx: {counts['sfx']}")
    print(f"Classificados como decorative: {counts['decorative']}")
    print(f"Classificados como unknown: {counts['unknown']}")
    print(f"Tempo total: {summary['elapsed_seconds']:.2f}s")
    print(f"PDF: {summary['pdf_path']}")
    for compare_path in summary["compare_paths"]:
        print(f"Compare: {compare_path}")


if __name__ == "__main__":
    main()
