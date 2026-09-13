"""Forensic provenance for OCR lines, from raw detection to renderer input.

The pipeline rewrites line objects in place (``_candidate_from_line`` replaces
``OCRLine.text``) and replaces whole line lists between passes (speech-container
re-OCR, RapidOCR region recovery, selective fallbacks, full-page fallback).  Once
that happened, no artifact still held the historical text/geometry, so a group
that silently lost part of a source line could not be reconstructed afterwards.

This module only *observes*: it copies the evidence when a boundary is crossed
and never feeds anything back into a pipeline decision.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
ARTIFACT_FILENAME = "ocr_line_provenance.json"

# Deliberately small and explicit: every recorded operation is one of these.
OPERATIONS = frozenset(
    {
        "ocr_raw",
        "ocr_normalized",
        "ocr_retry_replacement",
        "line_split",
        "line_merge",
        "line_filtered",
        "line_injected",
        "group_member",
        "group_geometry",
        "render_input",
    }
)

# Artifacts travel outside the machine that produced them; keep them bounded.
MAX_EVENTS_PER_PAGE = 2000
MAX_TEXT_CHARS = 400

_ACTIVE = threading.local()


def _text(value):
    return str(value or "")[:MAX_TEXT_CHARS]


def _bbox(value):
    try:
        return [int(part) for part in tuple(value)[:4]]
    except (TypeError, ValueError):
        return []


def _fingerprint(line, page=None):
    raw = "|".join(
        (
            str(page if page is not None else getattr(line, "page", "") or ""),
            str(getattr(line, "engine", "") or ""),
            str(getattr(line, "raw_text", "") or getattr(line, "text", "") or ""),
            str(_bbox(getattr(line, "box", ()))),
        )
    )
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def ensure_line_ids(lines, page=None):
    """Attach a stable ``ocr_line_id`` to every line; return ``(id, parent_id)``.

    Identity survives in-place text normalization (the fingerprint is built from
    ``raw_text`` and the box), and a derived line whose geometry moved gets a new
    id that points back at the line it was derived from.
    """

    results = []
    for line in lines or []:
        metadata = dict(getattr(line, "metadata", None) or {})
        fingerprint = _fingerprint(line, page)
        line_id = str(metadata.get("ocr_line_id") or "")
        parent_id = ""
        if not line_id or metadata.get("ocr_line_fingerprint") != fingerprint:
            parent_id = line_id
            line_id = f"L{fingerprint}"
            metadata["ocr_line_id"] = line_id
            metadata["ocr_line_fingerprint"] = fingerprint
            if parent_id and parent_id != line_id:
                metadata["ocr_line_parent_ids"] = [parent_id]
            try:
                line.metadata = metadata
            except AttributeError:
                pass
        results.append((line_id, parent_id if parent_id != line_id else ""))
    return results


def snapshot_line(line, page=None):
    """Return a frozen plain-dict copy of a line's forensic evidence."""

    (line_id, _parent), = ensure_line_ids([line], page)
    metadata = getattr(line, "metadata", None) or {}
    return {
        "line_id": line_id,
        "text": _text(getattr(line, "text", "")),
        "raw_text": _text(getattr(line, "raw_text", "")),
        "bbox": _bbox(getattr(line, "box", ())),
        "confidence": round(float(getattr(line, "confidence", 0.0) or 0.0), 4),
        "engine": str(getattr(line, "engine", "") or ""),
        "page": getattr(line, "page", None),
        "repair_reason": str(getattr(line, "repair_reason", "") or "")[:120],
        "parent_ids": [str(item) for item in (metadata.get("ocr_line_parent_ids") or [])],
    }


class ProvenanceRecorder:
    """Collects per-page line provenance for one run."""

    def __init__(self):
        self.pages = {}
        self._current = None

    def merge_from(self, other: "ProvenanceRecorder"):
        """Merge a completed page-scoped recorder into this run recorder."""
        for index in sorted(other.pages):
            source = other.pages[index]
            target = self._page(index)
            target["raw_lines"].extend(source.get("raw_lines", []))
            target["events"].extend(source.get("events", []))
            target["render_inputs"].extend(source.get("render_inputs", []))
            target["_seen"].update(source.get("_seen", set()))
            target["_pass"] = max(target.get("_pass", 0), source.get("_pass", 0))
            for group_id, group in source.get("groups", {}).items():
                target["groups"].setdefault(group_id, group)

    def _page(self, index):
        key = int(index) if index is not None else 0
        return self.pages.setdefault(
            key,
            {
                "page": key,
                "raw_lines": [],
                "events": [],
                "groups": {},
                "render_inputs": [],
                "_seen": set(),
                "_pass": 0,
            },
        )

    @contextmanager
    def page(self, index):
        previous = self._current
        self._current = self._page(index)
        try:
            yield self._current
        finally:
            self._current = previous

    def to_dict(self):
        pages = []
        for key in sorted(self.pages):
            page = self.pages[key]
            pages.append(
                {
                    "page": page["page"],
                    "raw_lines": page["raw_lines"],
                    "events": page["events"],
                    "groups": [page["groups"][gid] for gid in sorted(page["groups"])],
                    "render_inputs": page["render_inputs"],
                }
            )
        return {"schema_version": SCHEMA_VERSION, "pages": pages}

    def summary(self):
        raw = sum(len(page["raw_lines"]) for page in self.pages.values())
        members = [
            member
            for page in self.pages.values()
            for group in page["groups"].values()
            for member in group["member_line_ids"]
        ]
        traceable = [
            member
            for page in self.pages.values()
            for group in page["groups"].values()
            for member in group["member_line_ids"]
            if member in page["_seen"]
        ]
        render_inputs = sum(len(page["render_inputs"]) for page in self.pages.values())
        render_traceable = sum(
            1
            for page in self.pages.values()
            for item in page["render_inputs"]
            if item["member_line_ids"] and all(mid in page["_seen"] for mid in item["member_line_ids"])
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "pages": len(self.pages),
            "raw_lines": raw,
            "group_members": len(members),
            "traceable_group_members": len(traceable),
            "render_inputs": render_inputs,
            "traceable_render_inputs": render_traceable,
            "events": sum(len(page["events"]) for page in self.pages.values()),
        }


def activate(recorder=None):
    """Install a recorder for the current thread and return it."""

    _ACTIVE.recorder = recorder if recorder is not None else ProvenanceRecorder()
    return _ACTIVE.recorder


def deactivate():
    _ACTIVE.recorder = None


def active():
    return getattr(_ACTIVE, "recorder", None)


@contextmanager
def page(index):
    recorder = active()
    if recorder is None:
        yield None
        return
    with recorder.page(index) as current:
        yield current


def _current():
    recorder = active()
    return None if recorder is None else recorder._current


def current_page():
    """The page being recorded, or ``None`` outside a recorded page.

    Exposed so a consumer can read the evidence while it is still being
    collected; it is still observation only, nothing here writes back.
    """

    return _current()


def record_event(operation, *, line_id="", parent_ids=(), before=None, after=None, reason="", group_id=""):
    current = _current()
    if current is None:
        return None
    if operation not in OPERATIONS:
        raise ValueError(f"unknown provenance operation: {operation}")
    if len(current["events"]) >= MAX_EVENTS_PER_PAGE:
        return None
    event = {"operation": operation}
    if line_id:
        event["line_id"] = str(line_id)
    if parent_ids:
        event["parent_ids"] = [str(item) for item in parent_ids if item]
    if group_id:
        event["group_id"] = str(group_id)
    if before is not None:
        event["before"] = before
    if after is not None:
        event["after"] = after
    if reason:
        event["reason"] = str(reason)[:120]
    current["events"].append(event)
    return event


def record_input_lines(lines, *, origin="ocr_engine", reason=""):
    """Snapshot the line list entering a pass, attributing anything new."""

    current = _current()
    if current is None:
        return []
    identities = ensure_line_ids(lines, current["page"])
    first_pass = not current["_seen"]
    current["_pass"] += 1
    incoming = []
    for line, (line_id, parent_id) in zip(lines or [], identities):
        incoming.append(line_id)
        if line_id in current["_seen"]:
            continue
        current["_seen"].add(line_id)
        snapshot = snapshot_line(line, current["page"])
        snapshot["origin"] = str(origin)
        snapshot["pass"] = current["_pass"]
        current["raw_lines"].append(snapshot)
        record_event(
            "ocr_raw" if first_pass else "line_injected",
            line_id=line_id,
            parent_ids=[parent_id] if parent_id else (),
            after=snapshot,
            reason="" if first_pass else (reason or origin),
        )
        # The engine repairs text before the line ever reaches this recorder, so the
        # first snapshot is also the boundary where that repair becomes visible.
        if snapshot["raw_text"] and snapshot["raw_text"] != snapshot["text"]:
            record_event(
                "ocr_normalized",
                line_id=line_id,
                before={"text": snapshot["raw_text"]},
                after={"text": snapshot["text"]},
                reason=snapshot["repair_reason"] or "ocr_engine_text_repair",
            )
    return incoming


def record_replacement(previous_lines, new_lines, *, reason):
    """Record that a whole line list was swapped for another one."""

    current = _current()
    if current is None:
        return
    previous_ids = [line_id for line_id, _ in ensure_line_ids(previous_lines, current["page"])]
    dropped = [line_id for line_id in previous_ids if line_id not in set(
        line_id for line_id, _ in ensure_line_ids(new_lines, current["page"])
    )]
    record_input_lines(new_lines, origin=reason, reason=reason)
    record_event(
        "ocr_retry_replacement",
        parent_ids=previous_ids,
        before={"line_count": len(previous_ids)},
        after={"line_count": len(new_lines or [])},
        reason=reason,
    )
    for line_id in dropped:
        record_event("line_filtered", line_id=line_id, reason=f"replaced_by_{reason}")


def record_normalization(line, before_text, after_text, reason=""):
    if before_text == after_text:
        return
    current = _current()
    if current is None:
        return
    (line_id, _parent), = ensure_line_ids([line], current["page"])
    record_event(
        "ocr_normalized",
        line_id=line_id,
        before={"text": _text(before_text)},
        after={"text": _text(after_text)},
        reason=reason,
    )


def record_filtered(line, reason):
    current = _current()
    if current is None:
        return
    (line_id, _parent), = ensure_line_ids([line], current["page"])
    record_event("line_filtered", line_id=line_id, reason=reason, before=snapshot_line(line, current["page"]))


def record_group(group):
    """Snapshot group membership, text and geometry at the grouping boundary."""

    current = _current()
    if current is None:
        return None
    lines = list(getattr(group, "lines", None) or [])
    member_ids = [line_id for line_id, _ in ensure_line_ids(lines, current["page"])]
    group_id = str(getattr(group, "group_id", "") or "")
    snapshot = {
        "group_id": group_id,
        "member_line_ids": member_ids,
        "member_lines": [snapshot_line(line, current["page"]) for line in lines],
        "text": _text(getattr(group, "text", "")),
        "bbox": _bbox(getattr(group, "box", ())),
    }
    previous = current["groups"].get(group_id)
    current["groups"][group_id] = snapshot
    if previous is None:
        record_event(
            "group_member",
            group_id=group_id,
            parent_ids=member_ids,
            after={"text": snapshot["text"], "bbox": snapshot["bbox"]},
        )
    elif previous["bbox"] != snapshot["bbox"] or previous["text"] != snapshot["text"]:
        record_event(
            "group_geometry",
            group_id=group_id,
            parent_ids=member_ids,
            before={"text": previous["text"], "bbox": previous["bbox"]},
            after={"text": snapshot["text"], "bbox": snapshot["bbox"]},
            reason="regrouped",
        )
    return snapshot


def record_render_input(group, *, lines=None, text=None, bbox=None, reason=""):
    """Snapshot the exact representation handed to the renderer/cleaner."""

    current = _current()
    if current is None:
        return None
    render_lines = list(lines if lines is not None else (getattr(group, "lines", None) or []))
    member_ids = [line_id for line_id, _ in ensure_line_ids(render_lines, current["page"])]
    entry = {
        "group_id": str(getattr(group, "group_id", "") or ""),
        "member_line_ids": member_ids,
        "render_input_lines": [snapshot_line(line, current["page"]) for line in render_lines],
        "render_input_text": _text(getattr(group, "text", "") if text is None else text),
        "render_input_translation": _text(getattr(group, "translation", "")),
        "render_input_bbox": _bbox(getattr(group, "box", ()) if bbox is None else bbox),
        "draw_box": _bbox(getattr(group, "draw_box", None) or ()),
    }
    current["render_inputs"].append(entry)
    record_event(
        "render_input",
        group_id=entry["group_id"],
        parent_ids=member_ids,
        after={
            "text": entry["render_input_text"],
            "bbox": entry["render_input_bbox"],
        },
        reason=reason,
    )
    return entry


def write_artifact(output_folder, recorder=None):
    """Persist the provenance artifact; return its summary."""

    recorder = recorder if recorder is not None else active()
    if recorder is None:
        return {}
    path = Path(output_folder) / ARTIFACT_FILENAME
    payload = recorder.to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    summary = recorder.summary()
    summary["artifact"] = ARTIFACT_FILENAME
    summary["artifact_bytes"] = path.stat().st_size
    return summary


def load_artifact(output_folder):
    """Read a run's provenance, or report it as unavailable for legacy runs.

    Legacy runs predate this evidence.  They report ``unavailable`` and are never
    reconstructed from final groups.
    """

    path = Path(output_folder) / ARTIFACT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "unavailable", "reason": "no_provenance_artifact"}
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return {"status": "unavailable", "reason": "unsupported_schema_version"}
    return {"status": "available", **payload}
