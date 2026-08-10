from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from json_utils import dump_json

# Pillow writes a PDF page at 72 dpi, so one pixel is one PDF unit and the format's
# 14400-unit page limit is the real ceiling for a logical page. Nothing else in the
# pipeline resizes a page, so this is the only physical height constraint.
MAX_LOGICAL_PAGE_HEIGHT = 14400

# A balloon border sitting exactly on the seam still reads as cut, so the protected
# span is widened by this much on each side. A cut is rejected when
# ``top - padding < cut_y < bottom + padding``; the two edges themselves are allowed.
PROTECTED_REGION_PADDING = 12

# A balloon is a flat fill enclosed by a high contrast border. Pre-OCR that is the
# only thing the pixels can tell us, so flat blobs are what gets protected.
_UNIFORM_WINDOW = 15
_UNIFORM_STD_LIMIT = 6.0
_MIN_PROTECTED_AREA = 3000
_MIN_PROTECTED_HEIGHT = 40
# A flat band reaching across the page is the gutter we want to cut in, never a balloon.
_PAGE_SPANNING_WIDTH_RATIO = 0.97


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
    logical_pages=False,
    protected_regions=None,
):
    """Join source slices and rebuild logical pages around low-risk horizontal bands.

    Webtoon source assets are transport slices, not real pages. Rebuilding them before
    OCR prevents a speech balloon or narration box split between adjacent assets from
    being recognized and rendered as two unrelated fragments.

    ``logical_pages=True`` means the caller already supplies complete pages — the usual case
    for a local folder. Joining and re-cutting those would destroy the author's page
    boundaries, so the inputs pass through untouched, in order.

    ``protected_regions`` are ``(top, bottom)`` spans in joined-stream coordinates that a
    cut may never cross — OCR lines, text blocks or translation groups a caller already
    knows about. Balloons found in the pixels are protected on top of these.
    """

    paths = [str(path) for path in image_paths if path]
    if not paths:
        raise ValueError("Nenhuma imagem fornecida para reconstruir o Webtoon.")
    if logical_pages:
        return _passthrough_logical_pages(paths, split_folder)
    folder = Path(split_folder)
    folder.mkdir(parents=True, exist_ok=True)
    for previous in folder.glob("page_*.png"):
        previous.unlink(missing_ok=True)

    page_paths = []
    split_records = []
    buffer = None
    consumed = 0
    source_images = 0
    source_height = 0
    for source_position, path in enumerate(paths):
        is_last_source = source_position == len(paths) - 1
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
            protected = protected_vertical_intervals(
                buffer,
                _local_protected_regions(protected_regions, consumed, buffer.height),
            )
            split_y, metrics = _find_safe_horizontal_split(
                buffer,
                target_height=target_height,
                min_height=min_height,
                max_height=search_max_height,
                protected=protected,
            )
            if not metrics.get("gutter") and buffer.height < hard_max_height:
                break
            extended_hard_height = hard_max_height + min(target_height // 2, 900)
            if (
                not metrics.get("gutter")
                and not is_last_source
                and buffer.height < extended_hard_height
            ):
                break
            if not metrics.get("gutter") and buffer.height >= hard_max_height:
                # The hard limit is the point where we must stop waiting indefinitely,
                # not permission to cut through artwork when a real gutter is only a
                # short distance later in the already-buffered stream. Prefer a nearby
                # safe band over a known-dangerous lowest-risk cut; if no such band is
                # present, preserve the fail-closed unsafe record below.
                overshoot_max_height = min(
                    buffer.height - 1,
                    extended_hard_height,
                )
                if overshoot_max_height > hard_max_height:
                    overshoot_y, overshoot_metrics = _find_safe_horizontal_split(
                        buffer,
                        target_height=hard_max_height,
                        min_height=hard_max_height + 1,
                        max_height=overshoot_max_height,
                        protected=protected,
                    )
                    if overshoot_metrics.get("gutter"):
                        split_y, metrics = overshoot_y, overshoot_metrics
            if not metrics.get("safe_band"):
                # Every position in the normal window would divide a balloon, a text
                # box or a translation group. Search the rest of the buffered stream
                # before even considering a cut through protected content.
                split_y, metrics = _expanded_semantic_split(
                    buffer,
                    target_height=target_height,
                    min_height=min_height,
                    hard_max_height=hard_max_height,
                    protected=protected,
                    fallback=(split_y, metrics),
                )
            if not metrics.get("safe_band") and buffer.height < MAX_LOGICAL_PAGE_HEIGHT:
                # Keep the segments together: a taller logical page is always better
                # than a cut balloon, and the page may still find a safe seam once
                # more of the chapter is buffered.
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
            consumed += split_y
            buffer = remainder

    if buffer is not None and buffer.height:
        page_path = folder / f"page_{len(page_paths) + 1:03}.png"
        buffer.save(page_path, "PNG", optimize=True)
        split_records.append(
            {
                "page": len(page_paths) + 1,
                "height": buffer.height,
                "safe_band": True,
                "gutter": True,
                "reason": "chapter_end",
                "decision": "chapter_end",
                "target_y": buffer.height,
                "selected_y": buffer.height,
                "delta": 0,
                "hard_collision_count": 0,
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
        "physical_maximum_height": MAX_LOGICAL_PAGE_HEIGHT,
        "protected_region_padding": PROTECTED_REGION_PADDING,
        "splits": split_records,
        "unsafe_split_count": sum(
            not bool(record.get("safe_band")) for record in split_records
        ),
    }
    with (folder / "smart_split_report.json").open("w", encoding="utf-8") as file:
        dump_json(report, file, ensure_ascii=False, indent=2)
    return page_paths, report



def _local_protected_regions(regions, consumed, height):
    """Rebase caller-supplied stream spans onto the live buffer window."""
    for top, bottom in regions or ():
        local_top = int(top) - consumed
        local_bottom = int(bottom) - consumed
        if local_bottom > 0 and local_top < height:
            yield local_top, local_bottom


def _expanded_semantic_split(
    image,
    target_height,
    min_height,
    hard_max_height,
    protected,
    fallback,
):
    """Look past the normal window for the first cut that respects every balloon."""
    split_y, metrics = _find_safe_horizontal_split(
        image,
        target_height=target_height,
        min_height=min_height,
        max_height=min(image.height - 1, MAX_LOGICAL_PAGE_HEIGHT),
        protected=protected,
    )
    if not metrics.get("safe_band"):
        return fallback
    if split_y > hard_max_height:
        metrics = {
            **metrics,
            "reason": f"expanded_{metrics['reason']}",
            "decision": f"expanded_{metrics['decision']}",
        }
    return split_y, metrics


def _passthrough_logical_pages(paths, split_folder):
    """Copy already-complete pages into the split folder unchanged, preserving order."""
    folder = Path(split_folder)
    folder.mkdir(parents=True, exist_ok=True)
    for previous in folder.glob("page_*.png"):
        previous.unlink(missing_ok=True)

    page_paths = []
    source_height = 0
    for index, path in enumerate(paths, start=1):
        with Image.open(path) as opened:
            image = to_rgb(opened)
            source_height += image.height
            target = folder / f"page_{index:03d}.png"
            image.save(target)
        page_paths.append(str(target))

    report = {
        "strategy": "logical_pages_passthrough",
        "smart_split_skipped": True,
        "skip_reason": "inputs_are_complete_pages",
        "source_images": len(paths),
        "source_height": source_height,
        "pages": len(page_paths),
        "splits": [],
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
                "target_y": record.get("target_y"),
                "selected_y": record.get("selected_y"),
                "delta": record.get("delta"),
                "hard_collision_count": record.get("hard_collision_count"),
                "decision": str(record.get("decision") or record.get("reason") or ""),
                "reason": str(record.get("reason") or ""),
                "safe_band": False,
                # Reaching here means no position in the normal or expanded window
                # cleared the protected regions and the page already hit the physical
                # PDF limit, so the cut is applied and flagged rather than silently
                # producing an unopenable page.
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


def _merge_intervals(intervals):
    merged = []
    for top, bottom in sorted((int(top), int(bottom)) for top, bottom in intervals):
        if merged and top <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], bottom)
        else:
            merged.append([top, bottom])
    return [(top, bottom) for top, bottom in merged]


def _uniform_blob_intervals(image):
    """Vertical spans of flat, page-internal blobs - the pre-OCR balloon signal."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = gray.shape
    window = (_UNIFORM_WINDOW, _UNIFORM_WINDOW)
    mean = cv2.blur(gray, window)
    variance = cv2.blur(gray * gray, window) - mean * mean
    flat = (variance <= _UNIFORM_STD_LIMIT**2).astype(np.uint8)
    # Open drops flat speckle inside artwork, close reunites a balloon interior
    # that its own lettering broke into pieces.
    flat = cv2.morphologyEx(flat, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    flat = cv2.morphologyEx(flat, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(flat, 8)
    intervals = []
    for index in range(1, count):
        left, top, blob_width, blob_height, area = stats[index]
        if area < _MIN_PROTECTED_AREA or blob_height < _MIN_PROTECTED_HEIGHT:
            continue
        if blob_width >= _PAGE_SPANNING_WIDTH_RATIO * width:
            continue
        intervals.append((int(top), int(top + blob_height)))
    return intervals


def protected_vertical_intervals(
    image,
    extra_regions=(),
    padding=PROTECTED_REGION_PADDING,
):
    """Vertical spans a page cut must never pass through.

    Smart split runs before OCR, so no balloon, text or translation-group box exists
    yet and the containers have to be recovered from the pixels. Callers that already
    hold real boxes - OCR lines, text blocks, translation groups - pass them as
    ``(top, bottom)`` pairs in ``extra_regions`` and they are protected verbatim,
    which keeps the guarantee alive even when the pixel detector finds nothing.
    """
    regions = list(_uniform_blob_intervals(image))
    regions.extend((int(top), int(bottom)) for top, bottom in extra_regions or ())
    return _merge_intervals(
        (top - padding, bottom + padding) for top, bottom in regions if bottom > top
    )


def _protected_collision(y, intervals):
    for top, bottom in intervals:
        if top < y < bottom:
            return (top, bottom)
    return None


def _find_safe_horizontal_split(
    image,
    target_height,
    min_height,
    max_height,
    protected=(),
):
    upper = min(int(max_height), image.height - 1)
    lower = min(int(min_height), upper)
    target = max(lower, min(int(target_height), upper))
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    protected = _merge_intervals(protected)
    collisions = 0
    best = None
    # A wider band prevents a one-pixel quiet row inside letters or balloon
    # borders from being mistaken for a genuine panel gutter.
    band_radius = 18
    for y in range(lower, upper + 1, 3):
        if _protected_collision(y, protected) is not None:
            # Hard constraint: cutting here would divide a balloon, a text box or a
            # translation group. No visual score can buy this position back.
            collisions += 1
            continue
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
        gutter = white_gutter or dark_gutter or uniform_gutter
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
            else "semantic_safe"
        )
        candidate = (
            gutter,
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
            "gutter": False,
            "reason": "no_semantic_safe_band" if protected else "no_candidate_band",
            "decision": "no_semantic_safe_band" if protected else "no_candidate_band",
            "orientation": "horizontal",
            "target_y": int(target),
            "selected_y": int(target),
            "delta": 0,
            "hard_collision_count": collisions,
            "protected_intervals": len(protected),
        }
    (
        gutter,
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
        # Every surviving candidate clears the hard constraints, so it is safe to cut
        # here even when the artwork behind the seam is busy.
        "safe_band": True,
        "gutter": bool(gutter),
        "reason": band_type,
        "decision": band_type,
        "orientation": "horizontal",
        "band_score": round(float(score), 6),
        "white_ratio": round(float(white_ratio), 6),
        "dark_ratio": round(float(dark_ratio), 6),
        "texture": round(float(texture), 6),
        "horizontal_edges": round(float(horizontal_edges), 6),
        "target_y": int(target),
        "selected_y": int(y),
        "delta": int(y) - int(target),
        "hard_collision_count": collisions,
        "protected_intervals": len(protected),
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
