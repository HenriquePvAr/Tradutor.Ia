"""Optional local advanced art reconstruction for hard textured regions.

The adapter intentionally stays tiny and product-owned.  It does not import or
vendor the GPL manga-image-translator runtime used during #84F28 benchmarking.

Runtime contract:
- model files are external assets, never Git-tracked product code;
- a model is loaded only after an exact SHA256 match;
- failure means unavailable/review, not force-render;
- inference is local only.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

import config


@dataclass(frozen=True)
class AdvancedInpaintDescriptor:
    model_id: str
    model_version: str
    expected_sha256: str
    expected_size_bytes: int
    backend: str
    source_url: str
    code_license: str
    model_license: str


ANIME_MANGA_LAMA_LARGE_JIT = AdvancedInpaintDescriptor(
    model_id="anime_manga_lama_large_jit",
    model_version="sanster-iopaint-anime-manga-big-lama-pt",
    expected_sha256=(
        "479d3afdcb7ed2fd944ed4ebcc39ca45b33491f0f2e43eb1000bd623cfb41823"
    ),
    expected_size_bytes=205_717_870,
    backend="torchscript",
    source_url=(
        "https://github.com/Sanster/models/releases/download/"
        "AnimeMangaInpainting/anime-manga-big-lama.pt"
    ),
    code_license="Apache-2.0 adapter pattern; product-owned minimal loader",
    model_license="NEEDS FUTURE REVIEW for public redistribution",
)


class AdvancedInpaintUnavailable(RuntimeError):
    pass


class AdvancedInpaintIntegrityError(RuntimeError):
    pass


class AdvancedArtInpainter:
    """Lazy, reusable local LaMa-family adapter."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        descriptor: AdvancedInpaintDescriptor = ANIME_MANGA_LAMA_LARGE_JIT,
        device: str = "cpu",
    ):
        self.descriptor = descriptor
        self.model_path = Path(model_path or config.ADVANCED_ART_INPAINT_MODEL_PATH)
        self.device = device
        self._model = None
        self._lock = threading.Lock()
        self.last_load_seconds: float | None = None

    def available(self) -> bool:
        return self.model_path.exists()

    def verify_integrity(self) -> str:
        if not self.model_path.exists():
            raise AdvancedInpaintUnavailable("MODEL_UNAVAILABLE")
        digest = _sha256_file(self.model_path)
        expected = str(
            config.ADVANCED_ART_INPAINT_MODEL_SHA256
            or self.descriptor.expected_sha256
        ).lower()
        if digest.lower() != expected:
            raise AdvancedInpaintIntegrityError("MODEL_INTEGRITY_FAILED")
        return digest.lower()

    def load(self):
        with self._lock:
            if self._model is not None:
                return self._model
            digest = self.verify_integrity()
            started = time.perf_counter()
            try:
                import torch

                model = torch.jit.load(
                    str(self.model_path),
                    map_location=self.device,
                ).eval()
                self._model = model
                self.last_load_seconds = time.perf_counter() - started
                return self._model
            except Exception as exc:  # pragma: no cover - exact torch errors vary
                raise AdvancedInpaintUnavailable("MODEL_LOAD_FAILED") from exc

    def unload(self):
        with self._lock:
            self._model = None

    def reconstruct(
        self,
        image_bgr: np.ndarray,
        mask: np.ndarray,
        *,
        debug_capture: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        """Return a clean image composited only inside ``mask``.

        ``mask > 0`` means REMOVE / RECONSTRUCT.

        ``debug_capture``, when given a dict, is populated with the raw model
        output (``pred_bgr``, at page size and location, uncomposited) and the
        context crop box, so a caller can compare RAW model output against the
        COMPOSITED result in the same offline run (#84F30 evidence).  This is
        opt-in and diagnostic only: normal callers pass nothing, inference and
        the returned ``composited``/``telemetry`` are unchanged either way, and
        nothing is persisted to disk unless the caller chooses to save it.
        """
        if image_bgr is None or mask is None or not np.any(mask > 0):
            raise ValueError("invalid_image_or_mask")
        model = self.load()
        import torch

        mask_u8 = (mask > 0).astype(np.uint8) * 255
        x, y, w, h, margin = _context_crop(mask_u8, image_bgr.shape)
        crop_bgr = image_bgr[y : y + h, x : x + w]
        crop_mask = mask_u8[y : y + h, x : x + w]
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        padded_rgb, original_h, original_w = _pad_to_modulo(rgb, 8)
        padded_mask, _, _ = _pad_to_modulo(crop_mask, 8)
        img_t = torch.from_numpy(_norm_img(padded_rgb)).unsqueeze(0).to(self.device)
        mask_t = torch.from_numpy(_norm_img(padded_mask)).unsqueeze(0).to(self.device)
        mask_t = (mask_t > 0).float()
        started = time.perf_counter()
        with torch.no_grad():
            pred = model(img_t, mask_t)
        inference_seconds = time.perf_counter() - started
        pred_rgb = pred[0].permute(1, 2, 0).detach().cpu().numpy()
        pred_rgb = np.clip(pred_rgb * 255, 0, 255).astype(np.uint8)
        pred_rgb = pred_rgb[:original_h, :original_w]
        pred_bgr = cv2.cvtColor(pred_rgb, cv2.COLOR_RGB2BGR)
        composited = image_bgr.copy()
        target_crop = composited[y : y + h, x : x + w]
        target_crop[crop_mask > 0] = pred_bgr[crop_mask > 0]
        composited[y : y + h, x : x + w] = target_crop
        if debug_capture is not None:
            raw_full = image_bgr.copy()
            raw_full[y : y + h, x : x + w] = pred_bgr
            debug_capture["raw_pred_bgr_full"] = raw_full
            debug_capture["raw_pred_bgr_crop"] = pred_bgr.copy()
            debug_capture["context_crop_xywh"] = [int(x), int(y), int(w), int(h)]
        telemetry = {
            "advanced_inpaint_used": True,
            "model_id": self.descriptor.model_id,
            "model_version": self.descriptor.model_version,
            "model_sha256": self.verify_integrity(),
            "model_hash_verified": True,
            "device": self.device,
            "backend": self.descriptor.backend,
            "inference_ms": int(round(inference_seconds * 1000)),
            "load_ms": (
                int(round(self.last_load_seconds * 1000))
                if self.last_load_seconds is not None else None
            ),
            "mask_semantics": "255/remove_reconstruct",
            "context_crop_xywh": [int(x), int(y), int(w), int(h)],
            "context_margin": int(margin),
        }
        return composited, telemetry


_DEFAULT_INPAINTER: AdvancedArtInpainter | None = None


def get_default_inpainter() -> AdvancedArtInpainter:
    global _DEFAULT_INPAINTER
    if _DEFAULT_INPAINTER is None:
        _DEFAULT_INPAINTER = AdvancedArtInpainter()
    return _DEFAULT_INPAINTER


def reset_default_inpainter_for_tests():
    global _DEFAULT_INPAINTER
    _DEFAULT_INPAINTER = None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_img(np_img: np.ndarray) -> np.ndarray:
    if len(np_img.shape) == 2:
        np_img = np_img[:, :, None]
    return np_img.transpose((2, 0, 1)).astype("float32") / 255.0


def _pad_to_modulo(img: np.ndarray, mod: int) -> tuple[np.ndarray, int, int]:
    h, w = img.shape[:2]
    ph = (mod - h % mod) % mod
    pw = (mod - w % mod) % mod
    if ph or pw:
        img = cv2.copyMakeBorder(img, 0, ph, 0, pw, cv2.BORDER_REFLECT)
    return img, h, w


def _context_crop(mask: np.ndarray, image_shape) -> tuple[int, int, int, int, int]:
    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        raise ValueError("empty_mask")
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    span = max(x1 - x0, y1 - y0)
    margin = max(32, int(span * 0.45))
    height, width = image_shape[:2]
    x0 = max(0, x0 - margin)
    y0 = max(0, y0 - margin)
    x1 = min(width, x1 + margin)
    y1 = min(height, y1 + margin)
    return x0, y0, x1 - x0, y1 - y0, margin
