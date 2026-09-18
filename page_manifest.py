"""Canonical final-page identity for the existing materialized manifest.

This is a value-only view over ``image_entries``.  It does not change download,
smart-split, OCR, translation or output ownership; it makes the identity contract
explicit before any future incremental producer is attempted.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class FinalPageManifestItem:
    logical_page_index: int
    stable_page_id: str
    source_item_id: str
    split_index: int
    path: str
    metadata: tuple[tuple[str, str], ...] = ()
    source_ranges: tuple[str, ...] = ()


def _stable_id(source_item_id: str, split_index: int, logical_index: int,
               source_ranges: Iterable[str] = ()) -> str:
    ranges = tuple(sorted(str(item) for item in source_ranges if item))
    # Preserve legacy IDs when no richer provenance exists; multi-source items
    # include canonical ranges to prevent primary-source collisions.
    if ranges:
        raw = f"{source_item_id}|split={split_index}|logical={logical_index}|ranges={ranges}".encode()
    else:
        raw = f"{source_item_id}|split={split_index}|logical={logical_index}".encode()
    return "page-" + hashlib.sha256(raw).hexdigest()[:20]


def materialize_final_manifest(entries: Iterable[dict[str, Any]]) -> tuple[FinalPageManifestItem, ...]:
    """Build deterministic identity from already-final ``image_entries``.

    Completion order is deliberately ignored: logical order is the explicit ``index``
    (or stable input order), while source/split identifiers are retained for audit.
    """
    prepared = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        logical = int(entry.get("index") or position)
        source = str(entry.get("source_item_id") or entry.get("candidate_id") or
                     entry.get("source_index") or logical)
        split = int(entry.get("split_index") or entry.get("split") or 0)
        metadata = tuple(sorted((str(k), str(v)) for k, v in entry.items()
                                if k in {"width", "height", "source", "order"} and v is not None))
        raw_ranges = entry.get("source_ranges") or entry.get("provenance") or ()
        if isinstance(raw_ranges, str):
            source_ranges = tuple(part for part in raw_ranges.split(";") if part)
        else:
            source_ranges = tuple(str(part) for part in raw_ranges)
        prepared.append(FinalPageManifestItem(
            logical_page_index=logical,
            stable_page_id=_stable_id(source, split, logical, source_ranges),
            source_item_id=source,
            split_index=split,
            path=path,
            metadata=metadata,
            source_ranges=source_ranges,
        ))
    return tuple(sorted(prepared, key=lambda item: (item.logical_page_index, item.split_index, item.stable_page_id)))


def manifest_snapshot(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"logical_page_index": item.logical_page_index,
         "stable_page_id": item.stable_page_id,
         "source_item_id": item.source_item_id,
         "split_index": item.split_index,
         "source_ranges": list(item.source_ranges),
         "metadata": dict(item.metadata)}
        for item in materialize_final_manifest(entries)
    ]


def to_image_entry(item: FinalPageManifestItem) -> dict[str, Any]:
    """Compatibility adapter for the existing materialized pipeline."""
    metadata = dict(item.metadata)
    return {
        "index": item.logical_page_index,
        "sequence_index": item.logical_page_index,
        "original_index": item.logical_page_index,
        "path": item.path,
        "source_item_id": item.source_item_id,
        "split_index": item.split_index,
        "stable_page_id": item.stable_page_id,
        "source_ranges": list(item.source_ranges),
        **metadata,
    }


def to_ocr_job(item: FinalPageManifestItem) -> dict[str, Any]:
    """Create the minimum OCR job shape consumed by the current runner."""
    return {
        "index": item.logical_page_index,
        "image_path": item.path,
        "stable_page_id": item.stable_page_id,
    }
