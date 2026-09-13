"""Normalized OCR contract shared by every OCR engine (RapidOCR, NVIDIA OCR, ...).

The pipeline already has an internal per-line contract (``ocr_engine.OCRLine``)
that every downstream stage (grouping/recovery/reconstruction) consumes. This
module does not replace it -- it wraps it with a page-level envelope so a new
engine (NVIDIA OCR) can produce results the rest of the pipeline understands
without spreading engine-specific knowledge past the provider boundary.

``OCRRegion`` mirrors ``OCRLine`` field-for-field (text/confidence/bbox/engine)
plus a ``region_id`` so a normalized result can be produced from engines that
do not have their own ``OCRLine`` (e.g. NVIDIA OCR's HTTP JSON response).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OCRRegion:
    region_id: str
    bbox: tuple  # (x0, y0, x1, y1) pixel ints, matches OCRLine.box
    text: str
    confidence: float
    engine: str
    polygon: object = None  # optional np.ndarray; falls back to a bbox rectangle

    def to_ocr_line(self, page: int | None = None):
        import numpy as np
        from ocr_engine import OCRLine

        x0, y0, x1, y1 = self.bbox
        polygon = (
            self.polygon
            if self.polygon is not None
            else np.array(
                [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32
            )
        )
        return OCRLine(
            text=self.text,
            confidence=self.confidence,
            polygon=polygon,
            box=tuple(self.bbox),
            raw_text=self.text,
            engine=self.engine,
            page=page,
            metadata={
                "initial_ocr_engine": self.engine or None,
                "current_ocr_engine": self.engine or None,
                "recovery_engine": None,
                "recovered": False,
            },
        )


@dataclass
class OCRPageResult:
    page_id: int
    engine: str
    width: int
    height: int
    regions: list = field(default_factory=list)  # list[OCRRegion]
    error: str | None = None  # sanitized reason code (e.g. "nvidia_ocr_timeout"); never a raw payload

    @property
    def region_count(self) -> int:
        return len(self.regions)

    def to_ocr_lines(self):
        return [region.to_ocr_line(page=self.page_id) for region in self.regions]

    @classmethod
    def from_ocr_lines(cls, lines, *, page_id, engine, width=0, height=0):
        regions = [
            OCRRegion(
                region_id=f"{page_id}-{index}",
                bbox=tuple(getattr(line, "box", (0, 0, 0, 0))),
                text=getattr(line, "text", ""),
                confidence=float(getattr(line, "confidence", 0.0) or 0.0),
                engine=getattr(line, "engine", "") or engine,
                polygon=getattr(line, "polygon", None),
            )
            for index, line in enumerate(lines or [])
        ]
        return cls(page_id=page_id, engine=engine, width=width, height=height, regions=regions)
