from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from json_utils import dump_json


def to_rgb(img):
    if img.mode == "RGB":
        return img.copy()

    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg

    if img.mode == "P" and "transparency" in img.info:
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg

    return img.convert("RGB")


def generate_pdf(image_paths, pdf_path):
    if not image_paths:
        raise ValueError("Nenhuma imagem fornecida para gerar PDF.")

    pil_imgs = []

    for path in image_paths:
        try:
            with Image.open(path) as img:
                pil_imgs.append(to_rgb(img))
        except Exception as exc:
            print(f"Erro ao abrir imagem para PDF, pulando {path}: {exc}")

    if not pil_imgs:
        raise ValueError("Nenhuma imagem valida foi carregada para gerar o PDF.")

    first = pil_imgs[0]
    rest = pil_imgs[1:]

    try:
        first.save(pdf_path, "PDF", save_all=True, append_images=rest)
        print(f"PDF gerado com sucesso: {pdf_path}")
    except Exception as exc:
        raise RuntimeError(f"Erro ao gerar PDF: {exc}") from exc
    finally:
        for img in pil_imgs:
            img.close()


def generate_smart_webtoon_pdf(
    image_paths,
    pdf_path,
    split_folder,
    target_height=1800,
    min_height=1050,
    max_height=2400,
):
    """Rebuild a vertical chapter and split it only at visually safe bands."""

    page_paths, report = prepare_smart_webtoon_pages(
        image_paths,
        split_folder,
        target_height=target_height,
        min_height=min_height,
        max_height=max_height,
    )
    generate_pdf(page_paths, pdf_path)
    return page_paths, report


def prepare_smart_webtoon_pages(
    image_paths,
    split_folder,
    target_height=1800,
    min_height=1050,
    max_height=2400,
):
    """Join source slices and rebuild logical pages around low-risk horizontal bands.

    Webtoon source assets are transport slices, not real pages. Rebuilding them before
    OCR prevents a speech balloon or narration box split between adjacent assets from
    being recognized and rendered as two unrelated fragments.
    """

    paths = [str(path) for path in image_paths if path]
    if not paths:
        raise ValueError("Nenhuma imagem fornecida para reconstruir o Webtoon.")
    folder = Path(split_folder)
    folder.mkdir(parents=True, exist_ok=True)
    for previous in folder.glob("page_*.png"):
        previous.unlink(missing_ok=True)

    page_paths = []
    split_records = []
    buffer = None
    source_images = 0
    source_height = 0
    for path in paths:
        with Image.open(path) as opened:
            image = to_rgb(opened)
        source_images += 1
        source_height += image.height
        buffer = _append_vertical(buffer, image)
        image.close()
        while buffer.height >= max_height:
            # ``max_height`` is a soft target. If no safe gutter exists there,
            # keep a little more of the continuous stream instead of cutting a
            # balloon/panel merely to preserve a fixed page height.
            hard_max_height = max_height + target_height
            search_max_height = min(buffer.height - 1, hard_max_height)
            split_y, metrics = _find_safe_horizontal_split(
                buffer,
                target_height=target_height,
                min_height=min_height,
                max_height=search_max_height,
            )
            if not metrics.get("safe_band") and buffer.height < hard_max_height:
                break
            page = buffer.crop((0, 0, buffer.width, split_y))
            remainder = buffer.crop((0, split_y, buffer.width, buffer.height))
            buffer.close()
            page_path = folder / f"page_{len(page_paths) + 1:03}.png"
            page.save(page_path, "PNG", optimize=True)
            page.close()
            page_paths.append(str(page_path))
            split_records.append(
                {
                    "page": len(page_paths),
                    "height": split_y,
                    **metrics,
                }
            )
            buffer = remainder

    if buffer is not None and buffer.height:
        page_path = folder / f"page_{len(page_paths) + 1:03}.png"
        buffer.save(page_path, "PNG", optimize=True)
        split_records.append(
            {
                "page": len(page_paths) + 1,
                "height": buffer.height,
                "safe_band": True,
                "reason": "chapter_end",
            }
        )
        page_paths.append(str(page_path))
        buffer.close()

    report = {
        "source_images": source_images,
        "source_total_height": source_height,
        "pdf_pages": len(page_paths),
        "target_height": target_height,
        "minimum_height": min_height,
        "maximum_height": max_height,
        "hard_maximum_height": max_height + target_height,
        "splits": split_records,
        "unsafe_split_count": sum(
            not bool(record.get("safe_band")) for record in split_records
        ),
    }
    with (folder / "smart_split_report.json").open("w", encoding="utf-8") as file:
        dump_json(report, file, ensure_ascii=False, indent=2)
    return page_paths, report


def smart_split_audit(report):
    """Expand every unsafe cut into a record that can actually be audited.

    A count of unsafe cuts with no list behind it says a page may have been cut
    through artwork while naming no page, no coordinate and no metric, so nothing can
    be checked afterwards. The cut is still applied - the alternative is an unbounded
    page - but it is now fully described, and the counter can never exist without the
    matching detail.
    """
    details = []
    for record in report.get("splits") or []:
        if record.get("safe_band"):
            continue
        page = int(record.get("page") or 0)
        details.append(
            {
                "page": page,
                "logical_pages": [page, page + 1],
                "source_images": int(report.get("source_images") or 0),
                "split_y": int(record.get("height") or 0),
                "orientation": str(record.get("orientation") or "horizontal"),
                "band_score": record.get("band_score"),
                "white_ratio": record.get("white_ratio"),
                "dark_ratio": record.get("dark_ratio"),
                "texture": record.get("texture"),
                "horizontal_edges": record.get("horizontal_edges"),
                "reason": str(record.get("reason") or ""),
                "safe_band": False,
                # The lowest-risk band is taken rather than letting the page grow
                # without bound; nothing is discarded, so the cut is applied.
                "fallback_decision": "kept_lowest_risk_band",
                "accepted": True,
                "requires_review": True,
            }
        )
    return {
        "safe": not details,
        "unsafe_count": len(details),
        "details_count": len(details),
        "details": details,
    }


def create_split_boundary_contact_sheet(page_paths, report, target, max_items=24):
    """Render page seams, prioritizing low-confidence cuts, for visual auditing."""

    pages = [Path(path) for path in page_paths]
    boundaries = [
        record
        for record in report.get("splits", [])[:-1]
        if int(record.get("page", 0)) < len(pages)
    ]
    unsafe = [record for record in boundaries if not record.get("safe_band")]
    safe = [record for record in boundaries if record.get("safe_band")]
    selected = unsafe[:max_items]
    remaining = max(0, max_items - len(selected))
    if remaining and safe:
        step = max(1, len(safe) // remaining)
        selected.extend(safe[::step][:remaining])

    columns = 2
    card_width = 520
    card_height = 300
    header_height = 100
    rows = max(1, (len(selected) + columns - 1) // columns)
    canvas = Image.new(
        "RGB",
        (columns * card_width, header_height + rows * card_height),
        (18, 20, 25),
    )
    draw = ImageDraw.Draw(canvas)
    font = _diagnostic_font(18)
    small = _diagnostic_font(14)
    draw.text((24, 18), "SMART SPLIT - AUDITORIA DE EMENDAS", font=font, fill="white")
    draw.text(
        (24, 52),
        f"{len(pages)} paginas logicas / {len(unsafe)} cortes de baixo risco",
        font=small,
        fill=(180, 187, 199),
    )

    for position, record in enumerate(selected):
        page_number = int(record["page"])
        first = pages[page_number - 1]
        second = pages[page_number]
        with Image.open(first) as opened:
            top = opened.convert("RGB").crop(
                (0, max(0, opened.height - 130), opened.width, opened.height)
            )
        with Image.open(second) as opened:
            bottom = opened.convert("RGB").crop(
                (0, 0, opened.width, min(130, opened.height))
            )
        seam = Image.new("RGB", (max(top.width, bottom.width), 264), "white")
        seam.paste(top, ((seam.width - top.width) // 2, 0))
        seam.paste(bottom, ((seam.width - bottom.width) // 2, 134))
        top.close()
        bottom.close()
        preview = ImageOps.contain(seam, (480, 220))
        seam.close()

        column = position % columns
        row = position // columns
        x = column * card_width + 20
        y = header_height + row * card_height + 12
        fill = (230, 103, 74) if not record.get("safe_band") else (71, 181, 138)
        draw.text(
            (x, y),
            f"EMENDA {page_number:03}/{page_number + 1:03} - {record.get('reason')}",
            font=small,
            fill=fill,
        )
        canvas.paste(preview, (x, y + 32))
        draw.line((x, y + 32 + preview.height // 2, x + preview.width, y + 32 + preview.height // 2), fill=fill, width=3)

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=86, optimize=True, progressive=True)
    canvas.close()
    return str(target)


def _append_vertical(current, image):
    if current is None:
        return image.copy()
    width = max(current.width, image.width)
    canvas = Image.new("RGB", (width, current.height + image.height), "white")
    canvas.paste(current, ((width - current.width) // 2, 0))
    canvas.paste(image, ((width - image.width) // 2, current.height))
    current.close()
    return canvas


def _find_safe_horizontal_split(image, target_height, min_height, max_height):
    upper = min(int(max_height), image.height - 1)
    lower = min(int(min_height), upper)
    target = max(lower, min(int(target_height), upper))
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    best = None
    # A wider band prevents a one-pixel quiet row inside letters or balloon
    # borders from being mistaken for a genuine panel gutter.
    band_radius = 18
    for y in range(lower, upper + 1, 3):
        band = gray[max(0, y - band_radius) : min(gray.shape[0], y + band_radius)]
        if band.size == 0:
            continue
        white_ratio = float(np.mean(band >= 246))
        dark_ratio = float(np.mean(band <= 12))
        texture = float(np.std(band))
        horizontal_edges = float(np.mean(np.abs(np.diff(band, axis=1))))
        distance = abs(y - target) / max(1, upper - lower)
        white_gutter = (
            white_ratio >= 0.92
            and texture <= 8.0
            and horizontal_edges <= 1.0
        )
        dark_gutter = (
            dark_ratio >= 0.92
            and texture <= 8.0
            and horizontal_edges <= 1.0
        )
        uniform_gutter = texture <= 4.5 and horizontal_edges <= 2.5
        safe_band = white_gutter or dark_gutter or uniform_gutter
        dominant_uniform_ratio = max(white_ratio, dark_ratio)
        score = (
            dominant_uniform_ratio * 5.0
            + (2.0 if uniform_gutter else 0.0)
            - min(texture / 45.0, 2.0)
            - min(horizontal_edges / 18.0, 2.0)
            - distance * 0.65
        )
        band_type = (
            "white_gutter"
            if white_gutter
            else "dark_gutter"
            if dark_gutter
            else "uniform_gutter"
            if uniform_gutter
            else "low_risk"
        )
        candidate = (
            safe_band,
            score,
            -distance,
            y,
            white_ratio,
            dark_ratio,
            texture,
            horizontal_edges,
            band_type,
        )
        if best is None or candidate[:3] > best[:3]:
            best = candidate
    if best is None:
        return target, {
            "safe_band": False,
            "reason": "no_candidate_band",
            "orientation": "horizontal",
        }
    (
        safe_band,
        score,
        _,
        y,
        white_ratio,
        dark_ratio,
        texture,
        horizontal_edges,
        band_type,
    ) = best
    return int(y), {
        "safe_band": bool(safe_band),
        "reason": band_type if safe_band else "lowest_risk_band",
        "orientation": "horizontal",
        "band_score": round(float(score), 6),
        "white_ratio": round(float(white_ratio), 6),
        "dark_ratio": round(float(dark_ratio), 6),
        "texture": round(float(texture), 6),
        "horizontal_edges": round(float(horizontal_edges), 6),
    }


def _diagnostic_font(size):
    candidates = (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()
