import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageStat

import config
from json_utils import dump_json, dumps_json, to_json_safe
from ocr_engine import OCRLine


CACHE_FORMAT_VERSION = "perf-cache-v1"
OCR_CACHE_VERSION = "paddle-ocr-v1"
PROCESSED_CACHE_VERSION = "render-v1"
PRECHECK_CACHE_VERSION = "no-text-v1"


def cache_root():
    return Path(config.CACHE_ROOT).resolve()


def cache_folder(name):
    folder = cache_root() / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def stable_hash(payload):
    encoded = dumps_json(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_files(paths):
    records = []
    for path in paths:
        path = Path(path)
        if path.is_file():
            records.append({"name": path.name, "sha256": file_sha256(path)})
    return stable_hash(records)


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            dump_json(
                payload,
                file,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def atomic_copy(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temp_name)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def valid_image(path, min_width=100, min_height=100):
    if not path or not os.path.isfile(path):
        return False
    try:
        with Image.open(path) as image:
            image.load()
            if image.width < min_width or image.height < min_height:
                return False
            small = image.convert("L").resize((32, 32))
            return ImageStat.Stat(small).var[0] > 5
    except Exception:
        return False


def ocr_cache_key(image_hash, ocr_lang):
    return stable_hash(
        {
            "cache_format": CACHE_FORMAT_VERSION,
            "version": OCR_CACHE_VERSION,
            "image_sha256": image_hash,
            "ocr_engine": config.OCR_ENGINE,
            "ocr_fallback": config.OCR_FALLBACK_ENGINE,
            "ocr_lang": ocr_lang,
        }
    )


def ocr_cache_path(key):
    return cache_folder("ocr") / f"{key}.json"


def serialize_ocr_lines(lines):
    return [
        {
            "text": line.text,
            "confidence": float(line.confidence),
            "polygon": np.asarray(line.polygon).astype(int).tolist(),
            "box": [int(value) for value in line.box],
            "raw_text": line.raw_text,
        }
        for line in lines
    ]


def deserialize_ocr_lines(records):
    return [
        OCRLine(
            text=record.get("text", ""),
            confidence=float(record.get("confidence", 0.0)),
            polygon=np.asarray(record.get("polygon", []), dtype=np.int32),
            box=tuple(int(value) for value in record.get("box", (0, 0, 1, 1))),
            raw_text=record.get("raw_text", record.get("text", "")),
        )
        for record in records
    ]


def load_ocr_cache(key):
    payload = load_json(ocr_cache_path(key))
    if payload.get("key") != key:
        return None
    return deserialize_ocr_lines(payload.get("lines", [])), payload


def save_ocr_cache(key, image_hash, ocr_lang, lines, elapsed_seconds, precheck):
    atomic_write_json(
        ocr_cache_path(key),
        {
            "key": key,
            "image_sha256": image_hash,
            "ocr_lang": ocr_lang,
            "elapsed_seconds": round(float(elapsed_seconds), 6),
            "precheck": precheck,
            "lines": serialize_ocr_lines(lines),
        },
    )


def precheck_cache_key(image_hash):
    return stable_hash(
        {
            "cache_format": CACHE_FORMAT_VERSION,
            "version": PRECHECK_CACHE_VERSION,
            "image_sha256": image_hash,
            "conservative": config.NO_TEXT_SKIP_CONSERVATIVE,
        }
    )


def no_text_precheck(image_path, image_hash=None, force=False):
    image_hash = image_hash or file_sha256(image_path)
    key = precheck_cache_key(image_hash)
    path = cache_folder("ocr") / f"{key}.precheck.json"

    if not force:
        cached = load_json(path)
        if cached.get("key") == key:
            return cached

    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        result = {
            "key": key,
            "skip": False,
            "reason": "image_load_failed_run_ocr",
            "metrics": {},
        }
        atomic_write_json(path, result)
        return result

    height, width = image.shape[:2]
    scale = min(1.0, 640.0 / max(width, height))
    if scale < 1.0:
        small = cv2.resize(
            image,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = image

    blurred = cv2.GaussianBlur(small, (3, 3), 0)
    mean = float(np.mean(blurred))
    std = float(np.std(blurred))
    edges = cv2.Canny(blurred, 80, 180)
    edge_density = float(np.count_nonzero(edges) / max(1, edges.size))

    adaptive = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        21,
        9,
    )
    component_count, _, stats, _ = cv2.connectedComponentsWithStats(
        255 - adaptive,
        connectivity=8,
    )
    small_components = 0
    for index in range(1, component_count):
        _, _, component_width, component_height, area = stats[index]
        if (
            3 <= component_width <= max(8, small.shape[1] * 0.12)
            and 3 <= component_height <= max(8, small.shape[0] * 0.08)
            and 8 <= area <= max(40, small.size * 0.004)
        ):
            small_components += 1

    block_size = max(8, min(small.shape[:2]) // 12)
    local_ranges = []
    for y in range(0, small.shape[0], block_size):
        for x in range(0, small.shape[1], block_size):
            block = small[y : y + block_size, x : x + block_size]
            if block.size:
                local_ranges.append(float(np.percentile(block, 95) - np.percentile(block, 5)))
    high_contrast_blocks = sum(value >= 90 for value in local_ranges)

    nearly_flat = std < 7.0 and edge_density < 0.0025
    extreme_flat = (mean < 14 or mean > 241) and std < 11.0 and edge_density < 0.004
    no_text_structure = (
        edge_density < 0.0015
        and small_components <= 1
        and high_contrast_blocks == 0
        and std < 14.0
    )

    if config.NO_TEXT_SKIP_CONSERVATIVE:
        skip = nearly_flat or extreme_flat or no_text_structure
    else:
        skip = (
            nearly_flat
            or extreme_flat
            or no_text_structure
            or (
                edge_density < 0.004
                and small_components <= 2
                and high_contrast_blocks <= 1
                and std < 20.0
            )
        )

    if nearly_flat:
        reason = "nearly_flat_low_edges"
    elif extreme_flat:
        reason = "extreme_brightness_low_variation"
    elif no_text_structure:
        reason = "no_text_like_components"
    else:
        reason = "uncertain_run_ocr"

    result = {
        "key": key,
        "skip": bool(skip),
        "reason": reason,
        "metrics": {
            "mean": round(mean, 4),
            "std": round(std, 4),
            "edge_density": round(edge_density, 6),
            "small_components": int(small_components),
            "high_contrast_blocks": int(high_contrast_blocks),
        },
    }
    atomic_write_json(path, result)
    return result


def processed_cache_key(image_hash, pipeline_fingerprint, relevant_config):
    return stable_hash(
        {
            "cache_format": CACHE_FORMAT_VERSION,
            "version": PROCESSED_CACHE_VERSION,
            "image_sha256": image_hash,
            "pipeline_fingerprint": pipeline_fingerprint,
            "config": relevant_config,
        }
    )


def processed_cache_paths(key):
    folder = cache_folder("processed")
    return folder / f"{key}.png", folder / f"{key}.json"


def load_processed_cache(key):
    image_path, metadata_path = processed_cache_paths(key)
    metadata = load_json(metadata_path)
    if metadata.get("key") != key or not valid_image(image_path):
        return None
    return str(image_path), metadata


def save_processed_cache(key, output_path, metadata):
    image_path, metadata_path = processed_cache_paths(key)
    atomic_copy(output_path, image_path)
    payload = dict(metadata)
    payload["key"] = key
    payload["cached_image_path"] = str(image_path)
    atomic_write_json(metadata_path, payload)
