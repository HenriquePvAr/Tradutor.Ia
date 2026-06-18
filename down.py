import base64
import io
import json
import os
import re
import shutil
import stat
import time
from urllib.parse import urljoin

import requests
from PIL import Image, ImageStat
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import CHROMEDRIVER_PATH, MAX_RETRIES_DOWNLOAD, TEMP_FOLDER


MIN_IMAGE_WIDTH = 480
MIN_IMAGE_HEIGHT = 220
MIN_IMAGE_AREA = 180_000


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
):
    max_retries = max_retries or MAX_RETRIES_DOWNLOAD

    if os.path.exists(TEMP_FOLDER):
        force_remove(TEMP_FOLDER)

    os.makedirs(TEMP_FOLDER, exist_ok=True)
    if debug_folder:
        os.makedirs(debug_folder, exist_ok=True)

    report = {
        "url": url,
        "total_dom_images": 0,
        "total_candidates": 0,
        "total_unique_urls": 0,
        "total_downloaded": 0,
        "total_ignored": 0,
        "ignored": [],
        "downloaded": [],
    }

    driver = _create_driver()
    try:
        driver.get(url)
        time.sleep(4)
        _scroll_incrementally(driver)
        candidates = _collect_image_candidates(driver, url)
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
        )
    finally:
        try:
            driver.quit()
        finally:
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

    for _ in range(max_rounds):
        height = driver.execute_script("return document.body.scrollHeight || 0")
        image_count = driver.execute_script("return document.images ? document.images.length : 0")
        viewport = driver.execute_script("return window.innerHeight || 900")
        current_y = driver.execute_script("return window.scrollY || 0")

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
):
    saved = []
    total = len(candidates)

    for idx, candidate in enumerate(candidates, start=1):
        if max_images and len(saved) >= max_images:
            break

        url = candidate["url"]
        skip_reason = _candidate_skip_reason(candidate)
        if skip_reason:
            _report_ignored(report, candidate, skip_reason)
            continue

        data = _download_url(url, referer, max_retries)
        if not data:
            data = _fetch_with_browser(driver, url)

        if not data:
            _report_ignored(report, candidate, "download_failed")
            continue

        valid, info_or_reason, image = _validate_image_bytes(data)
        if not valid:
            _report_ignored(report, candidate, info_or_reason)
            continue

        file_path = os.path.join(TEMP_FOLDER, f"{len(saved) + 1:03}.png")
        image.save(file_path, "PNG")
        image.close()

        item = {
            "url": url,
            "path": file_path,
            "width": info_or_reason["width"],
            "height": info_or_reason["height"],
            "source": candidate.get("source"),
            "order": candidate.get("order"),
        }
        report["downloaded"].append(item)
        report["total_downloaded"] = len(report["downloaded"])
        saved.append(file_path)

        if progress_callback:
            progress_callback(len(saved), max_images or total, "Baixando imagens")

    report["total_ignored"] = len(report["ignored"])
    return saved


def _candidate_skip_reason(candidate):
    url = (candidate.get("url") or "").lower()
    label = " ".join(
        str(candidate.get(key) or "").lower() for key in ("className", "id", "alt", "source")
    )
    width = int(candidate.get("naturalWidth") or candidate.get("width") or 0)
    height = int(candidate.get("naturalHeight") or candidate.get("height") or 0)

    if not url.startswith(("http://", "https://")):
        return "not_http_url"

    if any(token in url for token in ("spacer", "blank", "placeholder", "logo", "banner", "avatar")):
        return "asset_or_placeholder_url"

    if any(token in label for token in ("logo", "avatar", "banner", "ad", "advert", "profile")):
        return "asset_or_ad_element"

    if width and height and (width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT):
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


def _validate_image_bytes(data):
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
    except Exception as exc:
        return False, f"invalid_image: {exc}", None

    width, height = image.size
    if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT or width * height < MIN_IMAGE_AREA:
        image.close()
        return False, "too_small_image", None

    if _is_nearly_blank(image):
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
            "source": candidate.get("source"),
            "order": candidate.get("order"),
        }
    )
    report["total_ignored"] = len(report["ignored"])


def _write_download_report(debug_folder, report):
    path = os.path.join(debug_folder, "downloaded_images.json")
    with open(path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
