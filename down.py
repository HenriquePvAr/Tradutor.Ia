import base64
import html
import io
import json
import os
import re
import shutil
import stat
import time
from collections import Counter
from urllib.parse import urljoin

import requests
from PIL import Image, ImageStat
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import CHROMEDRIVER_PATH, MAX_RETRIES_DOWNLOAD, TEMP_FOLDER
from json_utils import dump_json


MIN_IMAGE_WIDTH = 480
MIN_IMAGE_HEIGHT = 220
MIN_IMAGE_AREA = 180_000
MIN_CHAPTER_IMAGE_HEIGHT = 1
MIN_CHAPTER_IMAGE_AREA = MIN_IMAGE_WIDTH


def force_remove(path):
    if not os.path.exists(path):
        return

    def remove_readonly(func, readonly_path, _):
        os.chmod(readonly_path, stat.S_IWRITE)
        func(readonly_path)

    shutil.rmtree(path, onerror=remove_readonly)


def download_images(
    url,
    progress_callback=None,
    max_retries=None,
    max_images=None,
    debug_folder=None,
    target_folder=None,
    force=True,
):
    max_retries = max_retries or MAX_RETRIES_DOWNLOAD
    target_folder = target_folder or TEMP_FOLDER
    total_started = time.perf_counter()

    if force and os.path.exists(target_folder):
        force_remove(target_folder)

    os.makedirs(target_folder, exist_ok=True)
    if debug_folder:
        os.makedirs(debug_folder, exist_ok=True)

    report = {
        "url": url,
        "total_dom_images": 0,
        "total_candidates": 0,
        "total_unique_urls": 0,
        "total_downloaded": 0,
        "total_ignored": 0,
        "requested_max_images": max_images,
        "collection_strategy": "incremental_scroll",
        "viewer_image_count": 0,
        "viewer_unique_urls": 0,
        "ignored": [],
        "downloaded": [],
        "target_folder": os.path.abspath(target_folder),
        "timings": {
            "collection_seconds": 0.0,
            "download_seconds": 0.0,
            "validation_seconds": 0.0,
            "image_save_seconds": 0.0,
            "total_seconds": 0.0,
        },
    }

    driver = _create_driver()
    try:
        collection_started = time.perf_counter()
        driver.get(url)
        time.sleep(4)
        viewer_snapshot = _viewer_image_snapshot(driver)
        report["viewer_image_count"] = viewer_snapshot["image_count"]
        report["viewer_unique_urls"] = len(viewer_snapshot["urls"])
        report["viewer_urls"] = viewer_snapshot["urls"]
        report["viewer_manifest_complete"] = bool(viewer_snapshot["complete_manifest"])
        scroll_diagnostics = _scroll_incrementally(driver)
        report["scroll_diagnostics"] = scroll_diagnostics
        viewer_snapshot_after_scroll = _viewer_image_snapshot(driver)
        report["viewer_image_count_after_scroll"] = viewer_snapshot_after_scroll["image_count"]
        report["viewer_unique_urls_after_scroll"] = len(viewer_snapshot_after_scroll["urls"])
        report["lazy_loading_fully_loaded"] = bool(
            scroll_diagnostics.get("reached_document_end")
            and viewer_snapshot_after_scroll["complete_manifest"]
            and len(viewer_snapshot_after_scroll["urls"]) >= len(viewer_snapshot["urls"])
        )
        if len(viewer_snapshot_after_scroll["urls"]) > len(viewer_snapshot["urls"]):
            viewer_snapshot = viewer_snapshot_after_scroll
            report["viewer_image_count"] = viewer_snapshot["image_count"]
            report["viewer_unique_urls"] = len(viewer_snapshot["urls"])
            report["viewer_urls"] = viewer_snapshot["urls"]
        if viewer_snapshot["complete_manifest"]:
            report["collection_strategy"] = "direct_viewer_manifest"
        candidates = _collect_image_candidates(driver, url)
        report["timings"]["collection_seconds"] = (
            time.perf_counter() - collection_started
        )
        report["total_dom_images"] = sum(1 for item in candidates if item.get("tag") == "img")
        report["total_candidates"] = len(candidates)
        unique_candidates = _dedupe_candidates(candidates)
        report["total_unique_urls"] = len(unique_candidates)
        return _download_candidates(
            driver,
            unique_candidates,
            progress_callback,
            max_retries,
            max_images,
            debug_folder,
            report,
            referer=url,
            target_folder=target_folder,
        )
    finally:
        try:
            driver.quit()
        finally:
            report["timings"]["total_seconds"] = time.perf_counter() - total_started
            if debug_folder:
                _write_download_report(debug_folder, report)


def _create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--window-size=1400,2200")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    if CHROMEDRIVER_PATH and os.path.isfile(CHROMEDRIVER_PATH):
        return webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=chrome_options)

    manager_error = None
    try:
        from webdriver_manager.chrome import ChromeDriverManager

        driver_path = ChromeDriverManager().install()
        return webdriver.Chrome(service=Service(driver_path), options=chrome_options)
    except Exception as exc:
        manager_error = exc

    try:
        return webdriver.Chrome(options=chrome_options)
    except Exception as selenium_error:
        raise RuntimeError(
            "Nao foi possivel iniciar o ChromeDriver automaticamente. "
            "Defina CHROMEDRIVER_PATH no .env se necessario."
        ) from (manager_error or selenium_error)


def _scroll_incrementally(driver, max_rounds=90, stable_rounds=5):
    stable = 0
    last_height = 0
    last_image_count = 0
    rounds = 0
    initial_height = int(driver.execute_script("return document.body.scrollHeight || 0") or 0)
    initial_image_count = int(
        driver.execute_script("return document.images ? document.images.length : 0") or 0
    )

    for round_index in range(max_rounds):
        rounds = round_index + 1
        height = int(driver.execute_script("return document.body.scrollHeight || 0") or 0)
        image_count = int(
            driver.execute_script("return document.images ? document.images.length : 0") or 0
        )
        viewport = int(driver.execute_script("return window.innerHeight || 900") or 900)
        current_y = int(driver.execute_script("return window.scrollY || 0") or 0)

        if height <= last_height and image_count <= last_image_count:
            stable += 1
        else:
            stable = 0

        if stable >= stable_rounds and current_y + viewport >= height - 8:
            break

        last_height = max(last_height, height)
        last_image_count = max(last_image_count, image_count)
        next_y = min(current_y + int(viewport * 0.82), max(height - viewport, 0))
        driver.execute_script("window.scrollTo(0, arguments[0]);", next_y)
        time.sleep(1.0)

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2.0)
    final_height = int(driver.execute_script("return document.body.scrollHeight || 0") or 0)
    final_image_count = int(
        driver.execute_script("return document.images ? document.images.length : 0") or 0
    )
    final_viewport = int(driver.execute_script("return window.innerHeight || 900") or 900)
    final_y = int(driver.execute_script("return window.scrollY || 0") or 0)
    return {
        "rounds": int(rounds),
        "max_rounds": int(max_rounds),
        "stable_rounds_required": int(stable_rounds),
        "stable_rounds_observed": int(stable),
        "initial_scroll_height": int(initial_height),
        "final_scroll_height": int(final_height),
        "initial_dom_image_count": int(initial_image_count),
        "final_dom_image_count": int(final_image_count),
        "final_scroll_y": int(final_y),
        "viewport_height": int(final_viewport),
        "reached_document_end": bool(final_y + final_viewport >= final_height - 8),
        "stabilized": bool(stable >= stable_rounds),
    }


def _viewer_image_snapshot(driver):
    """Return the chapter image manifest exposed by supported viewer pages."""

    payload = driver.execute_script(
        r"""
        const images = [...document.querySelectorAll('#_imageList img, .viewer_img img._images')];
        const urls = images
            .map((el) => el.getAttribute('data-url') || el.getAttribute('data-src') || '')
            .filter(Boolean);
        return {
            imageCount: images.length,
            urls: [...new Set(urls)],
        };
        """
    ) or {}
    image_count = int(payload.get("imageCount") or 0)
    urls = [str(value) for value in payload.get("urls") or [] if value]
    return {
        "image_count": image_count,
        "urls": urls,
        "complete_manifest": bool(image_count and len(urls) == image_count),
    }


def _collect_image_candidates(driver, page_url):
    script = r"""
        const out = [];
        const add = (el, tag, url, source, order) => {
            if (!url) return;
            const rect = el.getBoundingClientRect();
            out.push({
                tag,
                url,
                source,
                order,
                width: Math.round(rect.width || el.naturalWidth || 0),
                height: Math.round(rect.height || el.naturalHeight || 0),
                naturalWidth: el.naturalWidth || 0,
                naturalHeight: el.naturalHeight || 0,
                declaredWidth: Math.round(parseFloat(el.getAttribute('width') || '0')),
                declaredHeight: Math.round(parseFloat(el.getAttribute('height') || '0')),
                isChapterCandidate: Boolean(
                    el.matches('#_imageList img, .viewer_img img._images') ||
                    el.closest('#_imageList, .viewer_img, .viewer_lst') &&
                    (el.getAttribute('data-url') || el.classList.contains('_images'))
                ),
                y: Math.round((window.scrollY || 0) + rect.top),
                className: el.className || "",
                id: el.id || "",
                alt: el.alt || ""
            });
        };

        let order = 0;
        document.querySelectorAll("img").forEach((el) => {
            [
                "src",
                "data-src",
                "data-original",
                "data-url",
                "data-lazy-src",
                "data-image-url"
            ].forEach((name) => add(el, "img", el.getAttribute(name), name, order));

            const srcset = el.getAttribute("srcset");
            if (srcset) add(el, "img", srcset, "srcset", order);
            order += 1;
        });

        document.querySelectorAll("*").forEach((el) => {
            const bg = getComputedStyle(el).backgroundImage || "";
            if (bg && bg !== "none") add(el, "background", bg, "background-image", order++);
        });

        return out;
    """
    raw_candidates = driver.execute_script(script)
    candidates = []

    for raw in raw_candidates:
        for candidate_url in _extract_urls(raw.get("url") or ""):
            item = dict(raw)
            item["url"] = urljoin(page_url, candidate_url)
            candidates.append(item)

    return sorted(candidates, key=lambda item: (item.get("order", 0), item.get("y", 0)))


def _extract_urls(value):
    value = (value or "").strip()
    if not value or value.startswith("data:"):
        return []

    if value.startswith("url(") or "url(" in value:
        return [match.strip("\"' ") for match in re.findall(r"url\((.*?)\)", value) if match.strip()]

    if "," in value and any(part.strip().split(" ")[-1].endswith("w") for part in value.split(",")):
        best = None
        best_width = -1
        for part in value.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            width = _parse_srcset_width(bits[-1])
            if width >= best_width:
                best = bits[0]
                best_width = width
        return [best] if best else []

    return [value.split()[0]]


def _parse_srcset_width(value):
    match = re.match(r"(\d+)w$", value or "")
    if match:
        return int(match.group(1))
    return 0


def _dedupe_candidates(candidates):
    seen = set()
    unique = []

    for item in candidates:
        url = _normalize_url(item.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        item = dict(item)
        item["url"] = url
        unique.append(item)

    return unique


def _normalize_url(url):
    return (url or "").strip()


def _download_candidates(
    driver,
    candidates,
    progress_callback,
    max_retries,
    max_images,
    debug_folder,
    report,
    referer,
    target_folder,
):
    saved = []
    viewer_total = int(report.get("viewer_image_count") or 0)
    if max_images and viewer_total:
        viewer_total = min(viewer_total, int(max_images))
    total = viewer_total or len(candidates)

    for idx, candidate in enumerate(candidates, start=1):
        if max_images and len(saved) >= max_images:
            break

        url = candidate["url"]
        skip_reason = _candidate_skip_reason(candidate)
        if skip_reason:
            _report_ignored(report, candidate, skip_reason)
            continue

        download_started = time.perf_counter()
        data = _download_url(url, referer, max_retries)
        if not data:
            data = _fetch_with_browser(driver, url)
        report["timings"]["download_seconds"] += time.perf_counter() - download_started

        if not data:
            _report_ignored(report, candidate, "download_failed")
            continue

        validation_started = time.perf_counter()
        valid, info_or_reason, image = _validate_image_bytes(
            data,
            chapter_candidate=bool(candidate.get("isChapterCandidate")),
        )
        report["timings"]["validation_seconds"] += (
            time.perf_counter() - validation_started
        )
        if not valid:
            _report_ignored(report, candidate, info_or_reason)
            continue

        file_path = os.path.join(target_folder, f"{len(saved) + 1:03}.png")
        save_started = time.perf_counter()
        image.save(file_path, "PNG")
        image.close()
        report["timings"]["image_save_seconds"] += time.perf_counter() - save_started

        item = {
            "url": url,
            "path": file_path,
            "width": info_or_reason["width"],
            "height": info_or_reason["height"],
            "source": candidate.get("source"),
            "order": candidate.get("order"),
            "is_chapter_candidate": bool(candidate.get("isChapterCandidate")),
        }
        report["downloaded"].append(item)
        report["total_downloaded"] = len(report["downloaded"])
        saved.append(file_path)

        if progress_callback:
            progress_callback(len(saved), max_images or total, "Baixando imagens")

    report["total_ignored"] = len(report["ignored"])
    report["download_gate"] = _build_download_gate(report)
    return saved


def _candidate_skip_reason(candidate):
    url = (candidate.get("url") or "").lower()
    label = " ".join(
        str(candidate.get(key) or "").lower() for key in ("className", "id", "alt", "source")
    )
    width = max(
        int(candidate.get("width") or 0),
        int(candidate.get("declaredWidth") or 0),
        int(candidate.get("naturalWidth") or 0),
    )
    height = max(
        int(candidate.get("height") or 0),
        int(candidate.get("declaredHeight") or 0),
        int(candidate.get("naturalHeight") or 0),
    )

    if not url.startswith(("http://", "https://")):
        return "not_http_url"

    if ".gif" in url or "giphy.com" in url:
        return "animated_or_external_asset"

    if any(token in url for token in ("spacer", "blank", "placeholder", "logo", "banner", "avatar", "sprite")):
        return "asset_or_placeholder_url"

    if any(token in label for token in ("logo", "avatar", "banner", "ad", "advert", "profile")):
        return "asset_or_ad_element"

    if candidate.get("isChapterCandidate"):
        if width and height and (
            width < MIN_IMAGE_WIDTH
            or height < MIN_CHAPTER_IMAGE_HEIGHT
            or width * height < MIN_CHAPTER_IMAGE_AREA
        ):
            return "too_small_chapter_candidate"
    elif width and height and (width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT):
        return "too_small_dom_size"

    return None


def _download_url(url, referer, max_retries):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }

    for _ in range(max_retries):
        try:
            response = requests.get(url, timeout=20, headers=headers)
            if response.status_code == 200 and response.content:
                return response.content
        except Exception:
            time.sleep(0.3)

    return None


def _fetch_with_browser(driver, url):
    script = """
        const url = arguments[0];
        const callback = arguments[1];
        fetch(url)
            .then(r => r.blob())
            .then(b => {
                const reader = new FileReader();
                reader.onloadend = () => callback(reader.result.split(',')[1]);
                reader.readAsDataURL(b);
            })
            .catch(() => callback(null));
    """
    try:
        b64 = driver.execute_async_script(script, url)
        if b64:
            return base64.b64decode(b64)
    except Exception:
        return None
    return None


def _validate_image_bytes(data, chapter_candidate=False):
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
    except Exception as exc:
        return False, f"invalid_image: {exc}", None

    width, height = image.size
    minimum_height = MIN_CHAPTER_IMAGE_HEIGHT if chapter_candidate else MIN_IMAGE_HEIGHT
    minimum_area = MIN_CHAPTER_IMAGE_AREA if chapter_candidate else MIN_IMAGE_AREA
    if width < MIN_IMAGE_WIDTH or height < minimum_height or width * height < minimum_area:
        image.close()
        return False, "too_small_image", None

    if not chapter_candidate and _is_nearly_blank(image):
        image.close()
        return False, "nearly_blank_or_flat_image", None

    return True, {"width": width, "height": height}, image


def _is_nearly_blank(image):
    small = image.resize((32, 32))
    stat = ImageStat.Stat(small)
    extrema = small.convert("L").getextrema()
    mean = sum(stat.mean) / len(stat.mean)
    variance = sum(stat.var) / len(stat.var)
    return variance < 18 and (mean < 12 or mean > 243 or (extrema[1] - extrema[0]) < 10)


def _report_ignored(report, candidate, reason):
    report["ignored"].append(
        {
            "url": candidate.get("url"),
            "reason": reason,
            "width": candidate.get("naturalWidth") or candidate.get("width"),
            "height": candidate.get("naturalHeight") or candidate.get("height"),
            "declared_width": candidate.get("declaredWidth"),
            "declared_height": candidate.get("declaredHeight"),
            "is_chapter_candidate": bool(candidate.get("isChapterCandidate")),
            "source": candidate.get("source"),
            "order": candidate.get("order"),
        }
    )
    report["total_ignored"] = len(report["ignored"])


def _write_download_report(debug_folder, report):
    path = os.path.join(debug_folder, "downloaded_images.json")
    with open(path, "w", encoding="utf-8") as file:
        dump_json(report, file, ensure_ascii=False, indent=2)
    _write_download_artifacts(
        debug_folder,
        report,
        [item.get("path") for item in report.get("downloaded", []) if item.get("path")],
    )


def _build_download_gate(report):
    viewer_urls = list(dict.fromkeys(report.get("viewer_urls") or []))
    requested_max = report.get("requested_max_images")
    expected_urls = viewer_urls[: int(requested_max)] if requested_max else viewer_urls
    downloaded = report.get("downloaded") or []
    downloaded_urls = {str(item.get("url") or "") for item in downloaded}
    chapter_downloaded = [item for item in downloaded if item.get("is_chapter_candidate")]
    missing_urls = [url for url in expected_urls if url not in downloaded_urls]
    reasons = []
    if not downloaded:
        reasons.append("no_valid_images")
    if viewer_urls and missing_urls:
        reasons.append("viewer_images_missing")
    if expected_urls and len(chapter_downloaded) != len(expected_urls):
        reasons.append("viewer_count_mismatch")
    orders = [int(item.get("order") or 0) for item in chapter_downloaded]
    if orders and orders != sorted(orders):
        reasons.append("chapter_order_not_monotonic")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "expected_viewer_images": len(expected_urls),
        "total_viewer_images": len(viewer_urls),
        "downloaded_viewer_images": len(chapter_downloaded),
        "missing_viewer_images": len(missing_urls),
        "missing_url_samples": missing_urls[:12],
        "order_monotonic": not orders or orders == sorted(orders),
    }


def _write_download_artifacts(debug_folder, report, image_paths):
    folder = os.path.abspath(debug_folder)
    os.makedirs(folder, exist_ok=True)
    report_path = os.path.join(folder, "download_report.json")
    with open(report_path, "w", encoding="utf-8") as file:
        dump_json(report, file, ensure_ascii=False, indent=2)
    gate = report.get("download_gate") or {}
    lines = [
        "Tradutor.Ia - relatorio de download",
        f"URL: {report.get('url', '')}",
        f"Estrategia: {report.get('collection_strategy', '')}",
        f"Imagens no DOM: {report.get('total_dom_images', 0)}",
        f"Candidatos: {report.get('total_candidates', 0)}",
        f"URLs unicas: {report.get('total_unique_urls', 0)}",
        f"Imagens esperadas no leitor: {gate.get('expected_viewer_images', 0)}",
        f"Imagens validas baixadas: {report.get('total_downloaded', 0)}",
        f"Imagens do leitor baixadas: {gate.get('downloaded_viewer_images', 0)}",
        f"Imagens do leitor ausentes: {gate.get('missing_viewer_images', 0)}",
        f"Ordem preservada: {'sim' if gate.get('order_monotonic') else 'nao'}",
        f"Selenium chegou ao fim: {'sim' if (report.get('scroll_diagnostics') or {}).get('reached_document_end') else 'nao'}",
        f"Lazy loading completo: {'sim' if report.get('lazy_loading_fully_loaded') else 'nao'}",
        f"Download gate: {'aprovado' if gate.get('passed') else 'reprovado'}",
        f"Motivos: {', '.join(gate.get('reasons') or []) or 'nenhum'}",
    ]
    with open(os.path.join(folder, "download_report.txt"), "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    _write_download_html_report(
        report,
        os.path.join(folder, "download_report.html"),
    )
    _write_download_contact_sheet(
        image_paths,
        os.path.join(folder, "download_contact_sheet.jpg"),
    )


def _write_download_html_report(report, output_path):
    gate = report.get("download_gate") or {}
    ignored = report.get("ignored") or []
    downloaded = report.get("downloaded") or []
    reason_counts = Counter(str(item.get("reason") or "unknown") for item in ignored)
    timing = report.get("timings") or {}
    scroll = report.get("scroll_diagnostics") or {}

    def esc(value):
        return html.escape(str(value or ""))

    reason_rows = "\n".join(
        f"<tr><td>{esc(reason)}</td><td>{count}</td></tr>"
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ) or "<tr><td colspan='2'>Nenhuma rejeicao</td></tr>"
    ignored_rows = "\n".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{esc(item.get('reason'))}</td>"
        f"<td>{esc(item.get('source'))}</td>"
        f"<td>{esc(item.get('order'))}</td>"
        f"<td>{esc(item.get('width'))}x{esc(item.get('height'))}</td>"
        f"<td><code>{esc(item.get('url'))}</code></td>"
        "</tr>"
        for index, item in enumerate(ignored[:300], start=1)
    ) or "<tr><td colspan='6'>Nenhuma rejeicao</td></tr>"
    downloaded_rows = "\n".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{esc(item.get('order'))}</td>"
        f"<td>{esc(item.get('width'))}x{esc(item.get('height'))}</td>"
        f"<td>{esc(item.get('source'))}</td>"
        f"<td><code>{esc(item.get('path'))}</code></td>"
        "</tr>"
        for index, item in enumerate(downloaded[:300], start=1)
    ) or "<tr><td colspan='5'>Nenhuma imagem baixada</td></tr>"
    gate_reasons = ", ".join(gate.get("reasons") or []) or "nenhum"
    html_doc = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tradutor.Ia - Relatorio de download</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, Segoe UI, Arial, sans-serif; }}
    body {{ margin: 0; background: #101115; color: #f1f5f9; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1, h2 {{ margin: 0 0 14px; }}
    .muted {{ color: #94a3b8; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: 18px 0 28px; }}
    .card {{ background: #171a22; border: 1px solid #283244; border-radius: 16px; padding: 16px; box-shadow: 0 14px 40px rgba(0,0,0,.22); }}
    .value {{ font-size: 28px; font-weight: 800; color: #74f2ce; }}
    .ok {{ color: #5ee787; }}
    .bad {{ color: #ff7b72; }}
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0 28px; background: #151821; border-radius: 14px; overflow: hidden; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #273042; text-align: left; vertical-align: top; }}
    th {{ background: #1e2633; color: #cbd5e1; }}
    code {{ white-space: normal; word-break: break-all; color: #a5f3fc; }}
  </style>
</head>
<body>
<main>
  <h1>Tradutor.Ia - Relatorio de download</h1>
  <p class="muted"><code>{esc(report.get('url'))}</code></p>
  <div class="grid">
    <section class="card"><div class="muted">Download gate</div><div class="value {'ok' if gate.get('passed') else 'bad'}">{'aprovado' if gate.get('passed') else 'reprovado'}</div><div class="muted">Motivos: {esc(gate_reasons)}</div></section>
    <section class="card"><div class="muted">Imagens no DOM</div><div class="value">{esc(report.get('total_dom_images'))}</div></section>
    <section class="card"><div class="muted">Candidatos</div><div class="value">{esc(report.get('total_candidates'))}</div></section>
    <section class="card"><div class="muted">URLs unicas</div><div class="value">{esc(report.get('total_unique_urls'))}</div></section>
    <section class="card"><div class="muted">Viewer images</div><div class="value">{esc(report.get('viewer_image_count'))}</div></section>
    <section class="card"><div class="muted">Baixadas</div><div class="value">{esc(report.get('total_downloaded'))}</div></section>
    <section class="card"><div class="muted">Rejeitadas</div><div class="value">{esc(report.get('total_ignored'))}</div></section>
    <section class="card"><div class="muted">Estrategia</div><div class="value">{esc(report.get('collection_strategy'))}</div></section>
  </div>
  <h2>Completude</h2>
  <table>
    <tr><th>Esperadas no leitor</th><td>{esc(gate.get('expected_viewer_images'))}</td></tr>
    <tr><th>Baixadas do leitor</th><td>{esc(gate.get('downloaded_viewer_images'))}</td></tr>
    <tr><th>Ausentes do leitor</th><td>{esc(gate.get('missing_viewer_images'))}</td></tr>
    <tr><th>Ordem preservada</th><td>{'sim' if gate.get('order_monotonic') else 'nao'}</td></tr>
    <tr><th>Manifesto do viewer completo</th><td>{'sim' if report.get('viewer_manifest_complete') else 'nao'}</td></tr>
    <tr><th>Selenium chegou ao fim</th><td>{'sim' if scroll.get('reached_document_end') else 'nao'}</td></tr>
    <tr><th>Lazy loading completo</th><td>{'sim' if report.get('lazy_loading_fully_loaded') else 'nao'}</td></tr>
    <tr><th>Rodadas de scroll</th><td>{esc(scroll.get('rounds'))}/{esc(scroll.get('max_rounds'))}</td></tr>
    <tr><th>Imagens DOM apos scroll</th><td>{esc(scroll.get('final_dom_image_count'))}</td></tr>
    <tr><th>Altura final da pagina</th><td>{esc(scroll.get('final_scroll_height'))}</td></tr>
    <tr><th>Tempo coleta</th><td>{float(timing.get('collection_seconds') or 0):.2f}s</td></tr>
    <tr><th>Tempo total</th><td>{float(timing.get('total_seconds') or 0):.2f}s</td></tr>
  </table>
  <h2>Motivos de rejeicao</h2>
  <table><tr><th>Motivo</th><th>Quantidade</th></tr>{reason_rows}</table>
  <h2>Rejeicoes - amostra ate 300</h2>
  <table><tr><th>#</th><th>Motivo</th><th>Fonte</th><th>Ordem</th><th>Tamanho</th><th>URL</th></tr>{ignored_rows}</table>
  <h2>Imagens baixadas - amostra ate 300</h2>
  <table><tr><th>#</th><th>Ordem</th><th>Tamanho</th><th>Fonte</th><th>Arquivo</th></tr>{downloaded_rows}</table>
</main>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as file:
        file.write(html_doc)


def _write_download_contact_sheet(image_paths, output_path, columns=5, thumb_width=150):
    if not image_paths:
        return ""
    rows = (len(image_paths) + columns - 1) // columns
    cell_height = 210
    sheet = Image.new("RGB", (columns * thumb_width, rows * cell_height), "#111114")
    for index, path in enumerate(image_paths):
        try:
            with Image.open(path) as source:
                preview = source.convert("RGB")
                preview.thumbnail((thumb_width - 8, cell_height - 28))
                x = (index % columns) * thumb_width + (thumb_width - preview.width) // 2
                y = (index // columns) * cell_height + 20
                sheet.paste(preview, (x, y))
        except Exception:
            continue
    sheet.save(output_path, "JPEG", quality=82, optimize=True)
    return output_path
