"""Planning helpers for targeted, persistent quality-review child runs.

The planner is deliberately read-only.  It derives a bounded set of targets from the
persisted chapter artifacts and never opens a provider, downloads a page, or mutates a
PDF.  The queue/runner layer consumes only this explicit plan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from chapter_quality_revision import target_text_is_safe


TRANSLATABLE_CLASSES = frozenset({"speech", "thought", "narration", "unknown"})
PENDING_VISUAL_STATES = frozenset(
    {"rejected_visual_regression", "manual_review", "pending", "failed"}
)
PENDING_REVIEW_ACTIONS = frozenset({"manual_review"})


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default
    return value


def _page_number(page: dict[str, Any]) -> int:
    try:
        return int(page.get("index") or page.get("sequence_index") or 0)
    except (TypeError, ValueError):
        return 0


def _region_key(page: int, item: dict[str, Any]) -> str:
    raw = str(item.get("region_id") or item.get("id") or "").strip()
    if raw.startswith(f"p{page:03d}:"):
        return raw
    return f"p{page:03d}:{raw or 'region'}"


def _page_from_region_id(region_id: str) -> int:
    match = re.match(r"^p(\d{3,5}):", str(region_id or ""))
    return int(match.group(1)) if match else 0


def _region_catalog(output_dir: Path) -> dict[str, dict[str, Any]]:
    progress = _read_json(output_dir / "progress.json", {})
    catalog: dict[str, dict[str, Any]] = {}
    for page in progress.get("pages") or [] if isinstance(progress, dict) else []:
        if not isinstance(page, dict):
            continue
        number = _page_number(page)
        debug = page.get("debug_data") if isinstance(page.get("debug_data"), dict) else {}
        for item in debug.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = _region_key(number, item)
            classification = str(item.get("classification") or "unknown").strip().casefold()
            catalog[key] = {
                "region_id": key,
                "page": number,
                "classification": classification,
                "source_text": str(
                    item.get("clean_text") or item.get("text")
                    or item.get("repaired_text") or item.get("raw_text") or ""
                ),
                "current_translation": str(
                    item.get("translation") or item.get("translation_candidate") or ""
                ),
                "inside_balloon": bool(
                    item.get("inside_balloon")
                    or item.get("balloon_evidence")
                    or classification in {"speech", "thought"}
                ),
                "background_type": str(item.get("background_type") or "unknown")[:48],
                "bounding_box": list(item.get("bounding_box") or item.get("bbox") or [])[:4],
            }
    return catalog


def _latest_revision_root(output_dir: Path) -> Path | None:
    pointer = _read_json(output_dir / "quality_revision" / "latest_revision.json", {})
    manifest = Path(str(pointer.get("manifest_path") or "")) if isinstance(pointer, dict) else Path()
    if manifest.is_file():
        return manifest.parent
    revision_id = str(pointer.get("revision_id") or "") if isinstance(pointer, dict) else ""
    candidate = output_dir / "quality_revision" / revision_id
    return candidate if revision_id and candidate.is_dir() else None


def _translation_is_usable(source: str, candidate: str) -> bool:
    value = str(candidate or "").strip()
    if not value or not target_text_is_safe(value):
        return False
    normalized_source = " ".join(str(source or "").split()).casefold()
    normalized_candidate = " ".join(value.split()).casefold()
    return not normalized_source or normalized_source != normalized_candidate


def build_pending_region_plan(output_dir: str | Path) -> dict[str, Any]:
    """Return only unresolved translatable regions from persisted review evidence."""
    output = Path(output_dir)
    catalog = _region_catalog(output)
    root = _latest_revision_root(output)
    if root is None:
        raise ValueError("quality_revision_missing")
    contextual = _read_json(root / "contextual_translation_review.json", {})
    render = _read_json(root / "incremental_render_audit.json", {})
    reviews = {
        str(item.get("region_id") or ""): item
        for item in (contextual.get("reviews") or [])
        if isinstance(item, dict) and item.get("region_id")
    }
    visual_states = render.get("region_visual_states") if isinstance(render, dict) else {}
    visual_states = visual_states if isinstance(visual_states, dict) else {}

    keys = set(reviews) | set(visual_states)
    targets: list[dict[str, Any]] = []
    for key in sorted(keys, key=lambda value: (_page_from_region_id(value), value)):
        base = dict(catalog.get(key) or {})
        review = reviews.get(key) or {}
        visual = visual_states.get(key) if isinstance(visual_states.get(key), dict) else {}
        action = str(review.get("action") or "").casefold()
        visual_state = str(visual.get("state") or "").casefold()
        if action not in PENDING_REVIEW_ACTIONS and visual_state not in PENDING_VISUAL_STATES:
            continue
        classification = str(base.get("classification") or "unknown").casefold()
        if classification not in TRANSLATABLE_CLASSES:
            continue
        source = str(base.get("source_text") or visual.get("source_text") or "")
        current = str(base.get("current_translation") or visual.get("previous_translation") or "")
        proposed = str(
            review.get("revised_translation")
            or visual.get("proposed_translation")
            or visual.get("applied_translation")
            or ""
        )
        reusable = proposed if _translation_is_usable(source, proposed) else (
            current if _translation_is_usable(source, current) else ""
        )
        requires_provider = not bool(reusable)
        reason_code = str(
            visual.get("reason_code") or review.get("reason_code") or "review_required"
        )[:80]
        target = {
            **base,
            "region_id": key,
            "page": int(base.get("page") or _page_from_region_id(key)),
            "review_action": action,
            "visual_state": visual_state,
            "reason_code": reason_code,
            "proposed_translation": proposed,
            "translation_to_reuse": reusable,
            "requires_provider": requires_provider,
            "work_kind": (
                "translation_and_reconstruction" if requires_provider
                else "reconstruction_only"
            ),
            "previous_strategies": [
                str(metric.get("strategy") or metric.get("method") or "")
                for metric in (visual.get("cleanup_metrics") or [])
                if isinstance(metric, dict) and (metric.get("strategy") or metric.get("method"))
            ],
            "previous_score": visual.get("overall_confidence") or visual.get("confidence"),
        }
        targets.append(target)

    pages = sorted({int(item["page"]) for item in targets if int(item.get("page") or 0) > 0})
    provider_count = sum(1 for item in targets if item["requires_provider"])
    reconstruction_count = len(targets) - provider_count
    return {
        "schema_version": 1,
        "revision_root": str(root),
        "region_count": len(targets),
        "page_count": len(pages),
        "pages": pages,
        "reconstruction_only": reconstruction_count,
        "provider_required": provider_count,
        "estimated_provider_requests": provider_count,
        "targets": targets,
    }
