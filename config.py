import os

from dotenv import load_dotenv


load_dotenv()


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
OCR_CONF_THRESHOLD = _env_int("OCR_CONF_THRESHOLD", 15)
OCR_ENGINE = _env_str("OCR_ENGINE", "paddle").lower()
OCR_FALLBACK_ENGINE = _env_str("OCR_FALLBACK_ENGINE", "tesseract").lower()
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
OCR_WORKERS = max(1, _env_int("OCR_WORKERS", 2))

TRANSLATION_PARALLEL = _env_bool("TRANSLATION_PARALLEL", True)
TRANSLATION_WORKERS = max(1, _env_int("TRANSLATION_WORKERS", 2))

SKIP_NO_TEXT_IMAGES = _env_bool("SKIP_NO_TEXT_IMAGES", True)
NO_TEXT_SKIP_CONSERVATIVE = _env_bool("NO_TEXT_SKIP_CONSERVATIVE", True)

SAVE_FULL_DEBUG = _env_bool("SAVE_FULL_DEBUG", False)
SAVE_COMPARE_SAMPLES = _env_bool("SAVE_COMPARE_SAMPLES", True)
SAVE_DEBUG_ONLY_ERRORS = _env_bool("SAVE_DEBUG_ONLY_ERRORS", True)
