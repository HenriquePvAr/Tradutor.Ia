import os

from local_environment import load_local_environment


load_local_environment()


def _env_str(name, default=""):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


# Optional external tools.
TESSERACT_CMD = _env_str("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
CHROMEDRIVER_PATH = _env_str("CHROMEDRIVER_PATH", "")

# Configure pytesseract only when the Python package exists. PaddleOCR is the
# default OCR engine; Tesseract remains an optional fallback.
try:
    import pytesseract

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
except ImportError:
    pytesseract = None


# Optional font used when drawing translations.
FONT_PATH = _env_str("FONT_PATH", None)

# Temporary folders. They are created/cleaned by the pipeline modules.
TEMP_FOLDER = _env_str("TEMP_FOLDER", "capitulo_temp")
TEMP_OUT = _env_str("TEMP_OUT", TEMP_FOLDER + "_out")

# Download/OCR parameters.
MAX_RETRIES_DOWNLOAD = _env_int("MAX_RETRIES_DOWNLOAD", 5)
SELENIUM_QUIT_TIMEOUT_SECONDS = min(
    300.0,
    max(1.0, _env_float("SELENIUM_QUIT_TIMEOUT_SECONDS", 20.0)),
)
SELENIUM_CLEANUP_TIMEOUT_SECONDS = min(
    30.0,
    max(0.25, _env_float("SELENIUM_CLEANUP_TIMEOUT_SECONDS", 3.0)),
)
OCR_CONF_THRESHOLD = _env_int("OCR_CONF_THRESHOLD", 15)
OCR_ENGINE = _env_str("OCR_ENGINE", "paddle").lower()
OCR_FALLBACK_ENGINE = _env_str("OCR_FALLBACK_ENGINE", "paddle").lower()
OCR_HYBRID_FALLBACK = _env_bool("OCR_HYBRID_FALLBACK", True)
RAPIDOCR_ENABLED = _env_bool("RAPIDOCR_ENABLED", False)
RAPIDOCR_MIN_CONFIDENCE = _env_float("RAPIDOCR_MIN_CONFIDENCE", 0.55)
RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK = _env_bool(
    "RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK",
    True,
)
RAPIDOCR_PAGE_FALLBACK = _env_bool("RAPIDOCR_PAGE_FALLBACK", True)
OCR_TEXT_REPAIR = _env_bool("OCR_TEXT_REPAIR", True)
OCR_TEXT_REPAIR_MODE = _env_str("OCR_TEXT_REPAIR_MODE", "conservative").lower()
# Fast mode has an explicit heavy-fallback budget.  The safe default keeps the
# predictable RapidOCR path and requires an opt-in before loading native Paddle
# fallbacks that can take minutes on a single page.
FAST_OCR_HEAVY_FALLBACK = _env_bool("FAST_OCR_HEAVY_FALLBACK", False)
FAST_OCR_MODE = _env_bool("FAST_OCR_MODE", False)
FAST_OCR_PAGE_TIMEOUT_SECONDS = min(
    900.0, max(1.0, _env_float("FAST_OCR_PAGE_TIMEOUT_SECONDS", 45.0))
)
FAST_OCR_REGION_TIMEOUT_SECONDS = min(
    300.0, max(1.0, _env_float("FAST_OCR_REGION_TIMEOUT_SECONDS", 12.0))
)
FAST_OCR_FULL_FALLBACK_MAX_PAGES = max(
    0, _env_int("FAST_OCR_FULL_FALLBACK_MAX_PAGES", 0)
)
FAST_OCR_FULL_FALLBACK_MAX_REGIONS = max(
    0, _env_int("FAST_OCR_FULL_FALLBACK_MAX_REGIONS", 4)
)
FAST_OCR_TOTAL_FALLBACK_BUDGET_SECONDS = min(
    3600.0,
    max(0.0, _env_float("FAST_OCR_TOTAL_FALLBACK_BUDGET_SECONDS", 60.0)),
)
TRANSLATE_SFX = _env_bool("TRANSLATE_SFX", False)
PRIORITIZE_ENCLOSED_TEXT = _env_bool("PRIORITIZE_ENCLOSED_TEXT", True)


# Translation mode: google, huggingface, or nvidia.
TRANSLATION_MODE = _env_str("TRANSLATION_MODE", "nvidia").lower()

# HuggingFace/local translation settings.
HF_MODEL = _env_str("HF_MODEL", "Helsinki-NLP/opus-mt-mul-pt")
NLLB_MODEL_DIR = _env_str("NLLB_MODEL_DIR", r"C:\Users\Henrique\Downloads\NLLB_200")

# NVIDIA OpenAI-compatible API settings.
NVIDIA_API_KEY = _env_str("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = _env_str("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_TRANSLATION_MODEL = _env_str(
    "NVIDIA_TRANSLATION_MODEL",
    "nvidia/nemotron-3-super-120b-a12b",
)
NVIDIA_TRANSLATION_BATCH_SIZE = _env_int("NVIDIA_TRANSLATION_BATCH_SIZE", 20)
NVIDIA_MAX_REQUESTS_PER_MINUTE = _env_int("NVIDIA_MAX_REQUESTS_PER_MINUTE", 20)
NVIDIA_CONNECT_TIMEOUT_SECONDS = _env_float("NVIDIA_CONNECT_TIMEOUT_SECONDS", 10.0)
NVIDIA_READ_TIMEOUT_SECONDS = _env_float("NVIDIA_READ_TIMEOUT_SECONDS", 120.0)
NVIDIA_TOTAL_TIMEOUT_SECONDS = _env_float("NVIDIA_TOTAL_TIMEOUT_SECONDS", 150.0)
NVIDIA_REVISION_REGION_TIMEOUT_SECONDS = _env_float("NVIDIA_REVISION_REGION_TIMEOUT_SECONDS", 120.0)
NVIDIA_REVISION_DIAGNOSTIC_MODE = _env_bool("NVIDIA_REVISION_DIAGNOSTIC_MODE", False)

# Controlled Webtoon test mode.
TEST_MODE = _env_str("TEST_MODE", "False").lower() in ("1", "true", "yes", "on")
TEST_URL = _env_str(
    "TEST_URL",
    "https://www.webtoons.com/en/romance/i-shall-conquer-the-unruly-beasts/episode-1/viewer?title_no=10299&episode_no=1",
)
TEST_MAX_IMAGES = _env_int("TEST_MAX_IMAGES", 20)
DEBUG_VISUAL = _env_str("DEBUG_VISUAL", "False").lower() in ("1", "true", "yes", "on")
DEBUG_FOLDER = _env_str("DEBUG_FOLDER", "debug")

# Performance/caching settings. Conservative defaults keep visual behavior
# unchanged while allowing expensive chapter runs to resume safely.
FULL_FAST_MODE = _env_bool("FULL_FAST_MODE", True)
ENABLE_OCR_CACHE = _env_bool("ENABLE_OCR_CACHE", True)
ENABLE_TRANSLATION_CACHE = _env_bool("ENABLE_TRANSLATION_CACHE", True)
ENABLE_IMAGE_PROCESS_CACHE = _env_bool("ENABLE_IMAGE_PROCESS_CACHE", True)
ENABLE_DOWNLOAD_CACHE = _env_bool("ENABLE_DOWNLOAD_CACHE", True)
CACHE_ROOT = _env_str("CACHE_ROOT", ".cache")

OCR_PARALLEL = _env_bool("OCR_PARALLEL", True)
# Model-backed OCR is conservative by default. The legacy names remain
# supported; TRADUTOR_* is the explicit public policy surface.
OCR_WORKERS = max(1, _env_int("TRADUTOR_OCR_WORKERS", _env_int("OCR_WORKERS", 1)))
ADAPTIVE_PARALLELISM = _env_bool("ADAPTIVE_PARALLELISM", True)
RESOURCE_MONITORING = _env_bool("RESOURCE_MONITORING", False)
RESOURCE_MONITOR_INTERVAL_SECONDS = max(
    0.25,
    _env_float("RESOURCE_MONITOR_INTERVAL_SECONDS", 1.0),
)
MIN_OCR_WORKERS = max(1, _env_int("MIN_OCR_WORKERS", 1))
MAX_OCR_WORKERS = max(MIN_OCR_WORKERS, _env_int("MAX_OCR_WORKERS", OCR_WORKERS))
OCR_WORKER_INITIAL_PEAK_MB = max(256.0, _env_float("OCR_WORKER_INITIAL_PEAK_MB", 1800.0))
TRADUTOR_MAX_MEMORY_MB = max(0.0, _env_float("TRADUTOR_MAX_MEMORY_MB", 0.0))
TRADUTOR_OCR_MEMORY_RESERVE_MB = max(
    256.0, _env_float("TRADUTOR_OCR_MEMORY_RESERVE_MB", 4096.0)
)
MEMORY_SAFETY_MARGIN_PERCENT = max(
    0.0,
    _env_float("MEMORY_SAFETY_MARGIN_PERCENT", 20.0),
)
MIN_SYSTEM_RESERVE_GB = max(1.0, _env_float("MIN_SYSTEM_RESERVE_GB", 4.0))
PIPELINE_MEMORY_RESERVE_GB = max(0.5, _env_float("PIPELINE_MEMORY_RESERVE_GB", 1.0))
MEMORY_PRESSURE_ELEVATED_PERCENT = _env_float("MEMORY_PRESSURE_ELEVATED_PERCENT", 72.0)
MEMORY_PRESSURE_HIGH_PERCENT = _env_float("MEMORY_PRESSURE_HIGH_PERCENT", 82.0)
MEMORY_PRESSURE_CRITICAL_PERCENT = _env_float("MEMORY_PRESSURE_CRITICAL_PERCENT", 90.0)
CPU_PRESSURE_HIGH_PERCENT = _env_float("CPU_PRESSURE_HIGH_PERCENT", 92.0)
WORKER_SCALE_UP_COOLDOWN_SECONDS = max(
    0.0,
    _env_float("WORKER_SCALE_UP_COOLDOWN_SECONDS", 20.0),
)
WORKER_SCALE_DOWN_COOLDOWN_SECONDS = max(
    0.0,
    _env_float("WORKER_SCALE_DOWN_COOLDOWN_SECONDS", 8.0),
)
OCR_QUEUE_MULTIPLIER = max(1, _env_int("OCR_QUEUE_MULTIPLIER", 2))
CLASSIFICATION_PROFILING = _env_bool("CLASSIFICATION_PROFILING", False)

TRANSLATION_PARALLEL = _env_bool("TRANSLATION_PARALLEL", True)
TRANSLATION_WORKERS = max(1, _env_int("TRANSLATION_WORKERS", 2))

SKIP_NO_TEXT_IMAGES = _env_bool("SKIP_NO_TEXT_IMAGES", True)
NO_TEXT_SKIP_CONSERVATIVE = _env_bool("NO_TEXT_SKIP_CONSERVATIVE", True)

SAVE_FULL_DEBUG = _env_bool("SAVE_FULL_DEBUG", False)
SAVE_COMPARE_SAMPLES = _env_bool("SAVE_COMPARE_SAMPLES", True)
SAVE_DEBUG_ONLY_ERRORS = _env_bool("SAVE_DEBUG_ONLY_ERRORS", True)

# Official Webtoon assets are arbitrary transport slices. Rebuild a continuous
# chapter into logical pages before OCR so balloons and narration are not cut at
# source-file boundaries.
SMART_WEBTOON_PDF_SPLIT = _env_bool("SMART_WEBTOON_PDF_SPLIT", True)
SMART_PDF_TARGET_HEIGHT = max(800, _env_int("SMART_PDF_TARGET_HEIGHT", 1800))
SMART_PDF_MIN_HEIGHT = max(600, _env_int("SMART_PDF_MIN_HEIGHT", 1050))
SMART_PDF_MAX_HEIGHT = max(
    SMART_PDF_TARGET_HEIGHT,
    _env_int("SMART_PDF_MAX_HEIGHT", 2400),
)

# Experimental RapidOCR quality guardrails. These defaults favor visual safety
# over speed whenever RapidOCR is selected from the CLI/env.
OCR_QUALITY_CONTROL = _env_bool("OCR_QUALITY_CONTROL", True)
OCR_REGION_SELECTIVE_FALLBACK = _env_bool("OCR_REGION_SELECTIVE_FALLBACK", True)
OCR_GROUP_FALLBACK_MAX_GROUPS = max(0, _env_int("OCR_GROUP_FALLBACK_MAX_GROUPS", 8))
OCR_GROUP_FALLBACK_PADDING = max(8, _env_int("OCR_GROUP_FALLBACK_PADDING", 34))
OCR_GROUP_MIN_QUALITY_SCORE = _env_float("OCR_GROUP_MIN_QUALITY_SCORE", 0.62)

TRANSLATION_VALIDATION = _env_bool("TRANSLATION_VALIDATION", True)
TRANSLATION_RETRY_ON_MIXED_LANGUAGE = _env_bool(
    "TRANSLATION_RETRY_ON_MIXED_LANGUAGE",
    True,
)
TRANSLATION_MAX_RETRIES = max(0, _env_int("TRANSLATION_MAX_RETRIES", 2))

TEXT_MASK_PADDING = max(0, _env_int("TEXT_MASK_PADDING", 3))
MAX_MASK_EXPANSION = max(1, _env_int("MAX_MASK_EXPANSION", 8))
STRICT_MASK_BOUNDS = _env_bool("STRICT_MASK_BOUNDS", True)
WHITE_BALLOON_FLAT_FILL = _env_bool("WHITE_BALLOON_FLAT_FILL", True)
MASK_COMPONENT_BASED = _env_bool("MASK_COMPONENT_BASED", True)
ALLOW_LARGE_RECTANGLE_MASK = _env_bool("ALLOW_LARGE_RECTANGLE_MASK", False)

TEXT_SAFE_PADDING = max(0, _env_int("TEXT_SAFE_PADDING", 12))
MIN_FONT_SIZE = max(6, _env_int("MIN_FONT_SIZE", 12))
MAX_FONT_SIZE = max(MIN_FONT_SIZE, _env_int("MAX_FONT_SIZE", 42))
MAX_TEXT_OVERFLOW_RATIO = _env_float("MAX_TEXT_OVERFLOW_RATIO", 0.01)
AUTO_LINE_WRAP = _env_bool("AUTO_LINE_WRAP", True)
AUTO_FONT_SHRINK = _env_bool("AUTO_FONT_SHRINK", True)

VISUAL_DIFF_VALIDATION = _env_bool("VISUAL_DIFF_VALIDATION", True)
VISUAL_QA_STRICT = _env_bool("VISUAL_QA_STRICT", True)
VISUAL_DIFF_THRESHOLD = max(1, _env_int("VISUAL_DIFF_THRESHOLD", 26))
MAX_OUTSIDE_CHANGE_RATIO = _env_float("MAX_OUTSIDE_CHANGE_RATIO", 0.002)
MAX_OUTSIDE_COMPONENT_AREA = max(1, _env_int("MAX_OUTSIDE_COMPONENT_AREA", 120))
MAX_MASK_TO_TEXT_AREA_RATIO = _env_float("MAX_MASK_TO_TEXT_AREA_RATIO", 3.0)
REJECT_BALLOON_BORDER_DAMAGE = _env_bool("REJECT_BALLOON_BORDER_DAMAGE", True)
REJECT_TEXT_OVERFLOW = _env_bool("REJECT_TEXT_OVERFLOW", True)
WHITE_BACKGROUND_MIN_BRIGHTNESS = _env_float("WHITE_BACKGROUND_MIN_BRIGHTNESS", 205.0)
WHITE_BACKGROUND_MAX_STD = _env_float("WHITE_BACKGROUND_MAX_STD", 38.0)
WHITE_BACKGROUND_MAX_SATURATION = _env_float("WHITE_BACKGROUND_MAX_SATURATION", 42.0)
WHITE_BACKGROUND_MIN_RATIO = _env_float("WHITE_BACKGROUND_MIN_RATIO", 0.70)
WHITE_BACKGROUND_MAX_TEXTURE = _env_float("WHITE_BACKGROUND_MAX_TEXTURE", 10.0)
WHITE_BACKGROUND_MAX_EDGE_DENSITY = _env_float("WHITE_BACKGROUND_MAX_EDGE_DENSITY", 0.12)
WHITE_BACKGROUND_MAX_DIAGONAL_LINES = max(
    0,
    _env_int("WHITE_BACKGROUND_MAX_DIAGONAL_LINES", 2),
)
WHITE_ENCLOSURE_MIN_BRIGHTNESS = _env_float("WHITE_ENCLOSURE_MIN_BRIGHTNESS", 195.0)
WHITE_ENCLOSURE_MIN_RATIO = _env_float("WHITE_ENCLOSURE_MIN_RATIO", 0.70)
WHITE_ENCLOSURE_MAX_DARK_RATIO = _env_float("WHITE_ENCLOSURE_MAX_DARK_RATIO", 0.20)
WHITE_ENCLOSURE_MAX_SATURATION = _env_float("WHITE_ENCLOSURE_MAX_SATURATION", 28.0)
WHITE_STYLIZED_ENCLOSURE_MIN_BRIGHTNESS = _env_float(
    "WHITE_STYLIZED_ENCLOSURE_MIN_BRIGHTNESS",
    130.0,
)
WHITE_STYLIZED_ENCLOSURE_MIN_RATIO = _env_float(
    "WHITE_STYLIZED_ENCLOSURE_MIN_RATIO",
    0.28,
)
WHITE_STYLIZED_ENCLOSURE_MAX_DARK_RATIO = _env_float(
    "WHITE_STYLIZED_ENCLOSURE_MAX_DARK_RATIO",
    0.45,
)
WHITE_STYLIZED_ENCLOSURE_MAX_SATURATION = _env_float(
    "WHITE_STYLIZED_ENCLOSURE_MAX_SATURATION",
    8.0,
)
MAX_TEXTURED_MASK_GROUP_RATIO = _env_float("MAX_TEXTURED_MASK_GROUP_RATIO", 0.18)
MAX_TEXTURED_MASK_COMPONENT_RATIO = _env_float(
    "MAX_TEXTURED_MASK_COMPONENT_RATIO",
    0.10,
)
REJECT_WHITE_PATCH_OUTSIDE_BALLOON = _env_bool(
    "REJECT_WHITE_PATCH_OUTSIDE_BALLOON",
    True,
)
REJECT_DARK_BLOTCH_ON_TEXTURED_ART = _env_bool(
    "REJECT_DARK_BLOTCH_ON_TEXTURED_ART",
    True,
)
MAX_NEW_DARK_COMPONENT_AREA = max(1, _env_int("MAX_NEW_DARK_COMPONENT_AREA", 120))
MAX_NEW_DARK_PIXEL_RATIO = _env_float("MAX_NEW_DARK_PIXEL_RATIO", 0.04)
TEXTURED_CAPTION_OVERLAY = _env_bool("TEXTURED_CAPTION_OVERLAY", True)
CAPTION_OVERLAY_OPACITY = min(
    0.95,
    max(0.35, _env_float("CAPTION_OVERLAY_OPACITY", 0.94)),
)
POST_RENDER_OCR_VALIDATION = _env_bool("POST_RENDER_OCR_VALIDATION", False)
