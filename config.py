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

# The engine a run will *really* use, as opposed to the one this process happens
# to hold. ``OCR_ENGINE`` above is an *output* of the decision below: the job
# process writes it when a run starts. A process that never starts a run - the
# UI - still carries the packaging default, and reporting that as fact is how the
# settings panel came to show "paddle . Ativo" for a chapter read by RapidOCR.
# Anything that displays or records the engine asks here instead.
BETA_OCR_ENGINE = "rapidocr"
BETA_OCR_ENGINES = frozenset({"rapidocr", "paddle", "paddle_mobile"})


def effective_ocr_engine():
    """The Beta engine, unless an explicit supported override names another."""
    override = _env_str("TRADUTOR_OCR_ENGINE_OVERRIDE", "").strip().lower()
    return override if override in BETA_OCR_ENGINES else BETA_OCR_ENGINE


OCR_FALLBACK_ENGINE = _env_str("OCR_FALLBACK_ENGINE", "paddle").lower()
OCR_HYBRID_FALLBACK = _env_bool("OCR_HYBRID_FALLBACK", True)
# Paddle remains available only for explicit legacy diagnostics. The Beta
# default is RapidOCR primary plus bounded RapidOCR recovery, then fail-closed
# review if evidence is still unusable.
OCR_LEGACY_PADDLE_FALLBACK = _env_bool("OCR_LEGACY_PADDLE_FALLBACK", False)
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
PTBR_NATURALIZATION_MODE = _env_str("PTBR_NATURALIZATION_MODE", "selective").lower()

# HuggingFace/local translation settings.
HF_MODEL = _env_str("HF_MODEL", "Helsinki-NLP/opus-mt-mul-pt")
NLLB_MODEL_DIR = _env_str("NLLB_MODEL_DIR", r"C:\Users\Henrique\Downloads\NLLB_200")

# NVIDIA OpenAI-compatible API settings.
NVIDIA_API_KEY = _env_str("NVIDIA_API_KEY", "")
NVIDIA_API_KEYS_JSON = _env_str("NVIDIA_API_KEYS_JSON", "")
NVIDIA_BASE_URL = _env_str("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_TRANSLATION_PROVIDER = _env_str("NVIDIA_TRANSLATION_PROVIDER", "nemotron").lower()
NVIDIA_TRANSLATION_MODEL = _env_str(
    "NVIDIA_TRANSLATION_MODEL",
    "nvidia/nemotron-3-super-120b-a12b",
)
NVIDIA_RIVA_TRANSLATION_MODEL = _env_str(
    "NVIDIA_RIVA_TRANSLATION_MODEL",
    "nvidia/riva-translate-4b-instruct-v2",
)
NVIDIA_TRANSLATION_BATCH_SIZE = _env_int("NVIDIA_TRANSLATION_BATCH_SIZE", 20)
NVIDIA_RIVA_MAX_BATCH_ITEMS = _env_int("NVIDIA_RIVA_MAX_BATCH_ITEMS", 8)
NVIDIA_RIVA_MAX_BATCH_SOURCE_TOKENS = _env_int("NVIDIA_RIVA_MAX_BATCH_SOURCE_TOKENS", 600)
NVIDIA_RIVA_OUTPUT_TOKEN_FLOOR = _env_int("NVIDIA_RIVA_OUTPUT_TOKEN_FLOOR", 96)
# Ceiling is a safety cap on a *response* budget, not a per-source cap: a full
# 600-source-token Riva batch needs ~1.5k output tokens, so 512 was structurally
# impossible and truncated the reply before the JSON envelope closed.
NVIDIA_RIVA_OUTPUT_TOKEN_CEILING = _env_int("NVIDIA_RIVA_OUTPUT_TOKEN_CEILING", 2048)
NVIDIA_RIVA_OUTPUT_TOKEN_RATIO = _env_float("NVIDIA_RIVA_OUTPUT_TOKEN_RATIO", 2.4)
NVIDIA_RIVA_OUTPUT_TOKEN_ITEM_OVERHEAD = _env_int("NVIDIA_RIVA_OUTPUT_TOKEN_ITEM_OVERHEAD", 12)
NVIDIA_RIVA_OUTPUT_TOKEN_PER_ITEM_FLOOR = _env_int("NVIDIA_RIVA_OUTPUT_TOKEN_PER_ITEM_FLOOR", 24)
NVIDIA_RIVA_LOGICAL_REQUEST_ATTEMPT_LIMIT = _env_int("NVIDIA_RIVA_LOGICAL_REQUEST_ATTEMPT_LIMIT", 3)
NVIDIA_RIVA_FORMAT_RETRY_LIMIT = _env_int("NVIDIA_RIVA_FORMAT_RETRY_LIMIT", 1)
NVIDIA_RIVA_LOGICAL_BATCH_TIMEOUT_SECONDS = _env_float("NVIDIA_RIVA_LOGICAL_BATCH_TIMEOUT_SECONDS", 60.0)
# DeepL API settings (selectable provider; deliberately not the default here).
# The base URL is used verbatim: there is no automatic promotion from the Free
# host to the paid one, so an exhausted Free quota fails closed instead of
# silently starting to bill.
DEEPL_API_KEY = _env_str("DEEPL_API_KEY", "")
DEEPL_API_BASE_URL = _env_str("DEEPL_API_BASE_URL", "https://api-free.deepl.com")
DEEPL_MODEL_TYPE = _env_str("DEEPL_MODEL_TYPE", "quality_optimized").strip().lower()
# DeepL's documented request maximum is 128 KiB; 120 KiB leaves transport
# headroom, and chunking measures the serialized body rather than counting items.
DEEPL_MAX_REQUEST_BYTES = _env_int("DEEPL_MAX_REQUEST_BYTES", 120 * 1024)
DEEPL_CONNECT_TIMEOUT_SECONDS = _env_float("DEEPL_CONNECT_TIMEOUT_SECONDS", 10.0)
DEEPL_READ_TIMEOUT_SECONDS = _env_float("DEEPL_READ_TIMEOUT_SECONDS", 120.0)
DEEPL_WRITE_TIMEOUT_SECONDS = _env_float("DEEPL_WRITE_TIMEOUT_SECONDS", 30.0)
DEEPL_POOL_TIMEOUT_SECONDS = _env_float("DEEPL_POOL_TIMEOUT_SECONDS", 10.0)
DEEPL_TOTAL_TIMEOUT_SECONDS = _env_float("DEEPL_TOTAL_TIMEOUT_SECONDS", 150.0)

NVIDIA_MAX_REQUESTS_PER_MINUTE = _env_int("NVIDIA_MAX_REQUESTS_PER_MINUTE", 20)
NVIDIA_CONNECT_TIMEOUT_SECONDS = _env_float("NVIDIA_CONNECT_TIMEOUT_SECONDS", 10.0)
NVIDIA_READ_TIMEOUT_SECONDS = _env_float("NVIDIA_READ_TIMEOUT_SECONDS", 120.0)
NVIDIA_TOTAL_TIMEOUT_SECONDS = _env_float("NVIDIA_TOTAL_TIMEOUT_SECONDS", 150.0)
NVIDIA_WRITE_TIMEOUT_SECONDS = _env_float("NVIDIA_WRITE_TIMEOUT_SECONDS", 30.0)
NVIDIA_POOL_TIMEOUT_SECONDS = _env_float("NVIDIA_POOL_TIMEOUT_SECONDS", 10.0)
NVIDIA_TRANSPORT_RETRY_LIMIT = _env_int("NVIDIA_TRANSPORT_RETRY_LIMIT", 4)
NVIDIA_JSON_RETRY_LIMIT = _env_int("NVIDIA_JSON_RETRY_LIMIT", 3)
NVIDIA_RETRY_BACKOFF_SECONDS = _env_float("NVIDIA_RETRY_BACKOFF_SECONDS", 1.0)
NVIDIA_CIRCUIT_FAILURE_THRESHOLD = _env_int("NVIDIA_CIRCUIT_FAILURE_THRESHOLD", 3)
NVIDIA_CIRCUIT_RECOVERY_SECONDS = _env_float("NVIDIA_CIRCUIT_RECOVERY_SECONDS", 60.0)
NVIDIA_CIRCUIT_HALF_OPEN_MAX_CALLS = _env_int("NVIDIA_CIRCUIT_HALF_OPEN_MAX_CALLS", 1)
NVIDIA_CIRCUIT_SUCCESS_THRESHOLD = _env_int("NVIDIA_CIRCUIT_SUCCESS_THRESHOLD", 1)
NVIDIA_REVISION_REGION_TIMEOUT_SECONDS = _env_float("NVIDIA_REVISION_REGION_TIMEOUT_SECONDS", 120.0)
NVIDIA_REVISION_DIAGNOSTIC_MODE = _env_bool("NVIDIA_REVISION_DIAGNOSTIC_MODE", False)

# Full chapter revision policy. The reviewer sends one region per request; these
# knobs keep the full run to suspicious regions, bound concurrency to the rate
# limit, cache valid responses, and retry only transient transport failures.
QUALITY_REVISION_SUSPICIOUS_ONLY = _env_bool("QUALITY_REVISION_SUSPICIOUS_ONLY", True)
QUALITY_REVISION_CONCURRENCY = min(3, max(1, _env_int("QUALITY_REVISION_CONCURRENCY", 2)))
QUALITY_REVISION_CACHE = _env_bool("QUALITY_REVISION_CACHE", True)
QUALITY_REVISION_MAX_RETRY = min(2, max(0, _env_int("QUALITY_REVISION_MAX_RETRY", 1)))
QUALITY_REVISION_LOW_OCR_CONFIDENCE = _env_float("QUALITY_REVISION_LOW_OCR_CONFIDENCE", 0.75)

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

# RapidOCR is the primary engine of the fast path, so its quality signal has to
# be actionable without doubling the OCR bill. A suspicious region is re-read
# once, by RapidOCR only, and only when the diagnostics carry real corruption
# evidence rather than merely unusual vocabulary. Replaying this policy over the
# persisted diagnostics of a full chapter keeps the retry rate near 15% of
# regions instead of the ~40% the broader fallback policy would request.
RAPIDOCR_REGION_RECOVERY = _env_bool("RAPIDOCR_REGION_RECOVERY", True)
RAPIDOCR_RECOVERY_MIN_QUALITY_SCORE = _env_float(
    "RAPIDOCR_RECOVERY_MIN_QUALITY_SCORE", 0.35
)
RAPIDOCR_RECOVERY_MAX_REGIONS_PER_PAGE = max(
    0, _env_int("RAPIDOCR_RECOVERY_MAX_REGIONS_PER_PAGE", 12)
)

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
# Separate, more lenient hard gate (TDD #84F32R) for the rendered *effect*
# footprint (glow/shadow), which is allowed to graze the safe area edge the
# way many already-accepted balloon styles already do, but must never bleed
# severely into unrelated artwork/panel content.
MAX_EFFECT_OVERFLOW_RATIO = _env_float("MAX_EFFECT_OVERFLOW_RATIO", 0.20)
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
# Art reconstruction safety.  A flat colour fill - and the OCR-line-quadrilateral
# masks that make one look rectangular - is only defensible when the artwork
# immediately around the cleanup mask really is one flat tone.  The bound is the
# 5th-95th percentile luminance spread of that clean ring: measured real speech
# balloons and narration boxes stay at or under ~24, while smoke, fabric and open
# illustration start at ~43.  Keep it tunable, real scans vary.
MAX_FLAT_FILL_RING_SPREAD = _env_float("MAX_FLAT_FILL_RING_SPREAD", 30.0)
MIN_FLAT_FILL_RING_PIXELS = max(1, _env_int("MIN_FLAT_FILL_RING_PIXELS", 64))
FLAT_FILL_RING_RADIUS = max(1, _env_int("FLAT_FILL_RING_RADIUS", 12))
REJECT_FLAT_PATCH_ON_TEXTURED_ART = _env_bool(
    "REJECT_FLAT_PATCH_ON_TEXTURED_ART",
    True,
)
# A reconstruction whose interior texture collapses to this fraction of the
# surrounding source texture reads as a synthetic block, not as artwork.
MAX_FLAT_PATCH_TEXTURE_RATIO = _env_float("MAX_FLAT_PATCH_TEXTURE_RATIO", 0.25)
# The ratio alone cannot carry this decision: the context ring still contains the
# source lettering, which inflates the surrounding texture, and inpainting never
# reproduces per-pixel film grain.  A legitimate reconstruction of dark noisy
# artwork measures ~2.8-3.8 interior Laplacian energy while the flat fills this
# gate exists to catch measure 0.0 - one single colour.  So a patch is only
# condemned when it is near-uniform in absolute terms *and* far below its
# surroundings; smoother-than-the-original is reconstruction, not a synthetic
# block.
MAX_FLAT_PATCH_ABSOLUTE_TEXTURE = _env_float(
    "MAX_FLAT_PATCH_ABSOLUTE_TEXTURE",
    1.0,
)
MIN_FLAT_PATCH_COMPONENT_AREA = max(
    1,
    _env_int("MIN_FLAT_PATCH_COMPONENT_AREA", 400),
)
# Art *fidelity* is a separate axis from art *safety*.  A reconstruction can be
# demonstrably non-destructive - no flat patch, no seam, no surviving source
# lettering - and still be visibly smoother than the artwork it replaced, which
# is what non-generative inpainting produces over a large lettering footprint.
# That is a review outcome, never a reason to withhold the render and put the
# English source back on the page.  The bound sits well above the destructive
# ratio (MAX_FLAT_PATCH_TEXTURE_RATIO): the real #84 pages 5 and 6 measure
# 0.37/0.29 against their own surroundings, while page 25 and page 6's caption
# measure 2.34/1.40 and stay clean.
MIN_ART_FIDELITY_TEXTURE_RATIO = _env_float(
    "MIN_ART_FIDELITY_TEXTURE_RATIO",
    0.55,
)
# Art reconstruction seams (ART-SEAM-DETECTOR-001).  A reconstruction can remove
# every source glyph, avoid a flat rectangle and still be unacceptable because it
# left a visible boundary where the artwork never had one.  The evidence is taken
# in a narrow band around the cleanup mask and is always reconstruction-relative:
# a balloon outline, a panel border or a character contour crossing the boundary
# is a *source* edge and must not be read as a seam.
DETECT_RECONSTRUCTION_SEAMS = _env_bool("DETECT_RECONSTRUCTION_SEAMS", True)
SEAM_BAND_RADIUS = max(2, _env_int("SEAM_BAND_RADIUS", 4))
MIN_SEAM_BAND_PIXELS = max(1, _env_int("MIN_SEAM_BAND_PIXELS", 48))
# How much further the luminance may jump across the mask boundary than it
# already moves over the same distance in the untouched art just outside it.  A
# smooth gradient reconstructed smoothly scores ~0; a flat block dropped into
# that same gradient scores ~23.
MAX_SEAM_LUMINANCE_STEP = _env_float("MAX_SEAM_LUMINANCE_STEP", 12.0)
# Texture energy immediately inside the boundary as a fraction of the untouched
# context.  Inpainting always smooths, so this is deliberately generous; it
# exists to catch texture that stops dead at the mask edge.
MIN_SEAM_TEXTURE_RATIO = _env_float("MIN_SEAM_TEXTURE_RATIO", 0.35)
# An inpaint halo is a ring that follows the mask contour and belongs to neither
# side of it.  What makes it reconstruction evidence rather than an edge is that
# it differs from the reconstructed interior *and* from the untouched context; a
# legitimate source contour crossing the boundary differs from only one of them.
MAX_SEAM_BOUNDARY_HALO_DELTA = _env_float("MAX_SEAM_BOUNDARY_HALO_DELTA", 18.0)
# #81 already proved that one naive texture ratio produces false positives, so a
# single signal grazing its bound is not enough to withhold a reconstruction.
# Evidence counts as high confidence when a second signal corroborates it, or
# when one signal reaches twice its own bound - a texture ratio of 0.0 means the
# texture stopped dead at the mask edge, which is maximal by construction.
# Measured: the destructive Page 25 rectangle scores 1.0, a flat block in a
# gradient 1.0, an inpaint halo 3.49, while a flat two-tone caption whose fill
# tone differs slightly from its neighbour scores 0.26 and stays accepted.
SEAM_HIGH_CONFIDENCE_SCORE = _env_float("SEAM_HIGH_CONFIDENCE_SCORE", 1.0)
MAX_NEW_DARK_COMPONENT_AREA = max(1, _env_int("MAX_NEW_DARK_COMPONENT_AREA", 120))
MAX_NEW_DARK_PIXEL_RATIO = _env_float("MAX_NEW_DARK_PIXEL_RATIO", 0.04)
TEXTURED_CAPTION_OVERLAY = _env_bool("TEXTURED_CAPTION_OVERLAY", True)
# Last-resort cleanup for proven ordinary speech whose broad masks were refused
# on nonuniform artwork.  It never widens a mask: it only measures art risk
# against the owned source-line evidence instead of the source text bbox.
SOURCE_SCOPED_SPEECH_CLEANUP = _env_bool("SOURCE_SCOPED_SPEECH_CLEANUP", True)
MAX_SOURCE_SCOPED_PAGE_AREA_RATIO = _env_float(
    "MAX_SOURCE_SCOPED_PAGE_AREA_RATIO",
    0.08,
)
ADVANCED_ART_INPAINTING = _env_bool("ADVANCED_ART_INPAINTING", True)
ADVANCED_ART_INPAINT_MODEL_ID = _env_str(
    "ADVANCED_ART_INPAINT_MODEL_ID",
    "anime_manga_lama_large_jit",
)
ADVANCED_ART_INPAINT_MODEL_PATH = _env_str(
    "ADVANCED_ART_INPAINT_MODEL_PATH",
    os.path.join(
        os.getenv("LOCALAPPDATA") or os.getcwd(),
        "TradutorIA",
        "models",
        "anime_manga_lama_large.pt",
    ),
)
ADVANCED_ART_INPAINT_MODEL_SHA256 = _env_str(
    "ADVANCED_ART_INPAINT_MODEL_SHA256",
    "479d3afdcb7ed2fd944ed4ebcc39ca45b33491f0f2e43eb1000bd623cfb41823",
).lower()
ADVANCED_ART_INPAINT_MAX_ATTEMPTS_PER_REGION = max(
    0,
    _env_int("ADVANCED_ART_INPAINT_MAX_ATTEMPTS_PER_REGION", 1),
)
CAPTION_OVERLAY_OPACITY = min(
    0.95,
    max(0.35, _env_float("CAPTION_OVERLAY_OPACITY", 0.94)),
)
POST_RENDER_OCR_VALIDATION = _env_bool("POST_RENDER_OCR_VALIDATION", False)
