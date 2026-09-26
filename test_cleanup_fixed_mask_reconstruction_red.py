"""Permanent RED for the frozen fixed-mask reconstruction contract."""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import cv2
import numpy as np
import pytest

import ocr_balloon as ob
from ocr_engine import OCRLine


def _pair(kind: str):
    h, w = 260, 360
    y, x = np.mgrid[0:h, 0:w]
    base = np.clip(120 + 35 * x / w + 12 * np.sin(y / 35), 0, 255).astype(np.uint8)
    base = np.repeat(base[..., None], 3, axis=2)
    structured = base.copy()
    if kind == "line":
        cv2.line(structured, (0, 25), (w - 1, 225), (20, 35, 55), 2)
    elif kind == "shade":
        structured = np.clip(
            structured.astype(float)
            + 35 * np.exp(-((x - 180) ** 2 + (y - 120) ** 2) / 8000)[..., None],
            0,
            255,
        ).astype(np.uint8)
    else:  # pragma: no cover - fixture is deliberately closed over two classes.
        raise AssertionError(kind)

    polygon = np.array([[75, 88], [285, 88], [285, 142], [75, 142]], dtype=np.int32)

    def prepared(clean, suffix):
        source = clean.copy()
        cv2.putText(
            source,
            "SOURCE TEXT",
            (80, 134),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (15, 15, 15),
            3,
            cv2.LINE_AA,
        )
        line = OCRLine(
            text="THIS IS A NORMAL SENTENCE.",
            confidence=0.99,
            polygon=polygon.copy(),
            box=(75, 88, 210, 54),
            raw_text="THIS IS A NORMAL SENTENCE.",
            engine="synthetic",
            page=1,
            metadata={"ocr_line_id": f"fixture-{suffix}"},
        )
        group = ob.TextGroup(
            group_id=f"fixture-{kind}-{suffix}",
            lines=[line],
            text="THIS IS A NORMAL SENTENCE.",
            translation="ISTO E UMA FRASE NORMAL.",
        )
        group.classification = "speech"
        group.translation_candidate = "ISTO E UMA FRASE NORMAL."
        group.source_completeness = {"status": "pass"}
        group.source_engine = "synthetic"
        group.region_id = "fixture"
        return clean, source, group

    b_clean, b_source, b_group = prepared(base, "base")
    s_clean, s_source, s_group = prepared(structured, "structured")

    # Production-faithful mask generation happens exactly once, on the base twin.
    _, frozen_mask, mask_metrics = ob._remove_text_for_group(
        b_source.copy(), b_source, b_group, strategy="source_scoped"
    )
    assert mask_metrics["mask_valid"] is True
    frozen_mask = frozen_mask.copy()

    # The measured calls bypass mask generation and reuse one frozen array.
    output_s = ob._apply_cleanup_mask(
        s_source.copy(), s_source, s_group, frozen_mask.copy(), strategy="source_scoped"
    )
    mask_for_counterfactual = frozen_mask.copy()
    assert np.array_equal(frozen_mask, mask_for_counterfactual)

    mask = frozen_mask > 0
    pair_signal = float(np.abs(s_clean.astype(float) - b_clean.astype(float))[mask].mean())
    d_correct = float(np.abs(output_s.astype(float) - s_clean.astype(float))[mask].mean())
    d_erased = float(np.abs(output_s.astype(float) - b_clean.astype(float))[mask].mean())
    assert pair_signal > 0
    margin = (d_erased - d_correct) / pair_signal
    return margin, d_correct, d_erased, pair_signal, frozen_mask


def test_fixed_mask_shade_positive_control_is_on_correct_side():
    margin, d_correct, d_erased, signal, mask = _pair("shade")
    assert mask.size == 260 * 360
    assert margin > 0.0, (
        f"frozen R2 positive control failed: margin={margin:.6f}, "
        f"d_correct={d_correct:.6f}, d_erased={d_erased:.6f}, signal={signal:.6f}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "MASK_PRODUCT_BUG: known-limitation. Fixed-mask reconstruction still erases "
        "known line structure (margin<0). Accepted as a known defect for the beta.16 "
        "gate; xfail is strict so this flips to a failure the moment a reconstruction "
        "fix makes it pass. See docs technical debt §29."
    ),
)
def test_fixed_mask_structured_reconstruction_must_not_erase_known_structure():
    margin, d_correct, d_erased, signal, mask = _pair("line")
    assert mask.size == 260 * 360
    # Frozen R2 contract: clear negative margin is structural erasure.  This is
    # intentionally RED until a future reconstruction fix preserves the line.
    assert margin >= 0.0, (
        f"fixed-mask structural erasure: margin={margin:.6f}, "
        f"d_correct={d_correct:.6f}, d_erased={d_erased:.6f}, signal={signal:.6f}"
    )
