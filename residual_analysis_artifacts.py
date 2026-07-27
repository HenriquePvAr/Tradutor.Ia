"""Deterministic, content-addressed residual-analysis artifacts.

The contract stores the exact residual pixels and every identity-affecting
detector input. It deliberately has no knowledge of chapters, page numbers,
phrases, coordinates, users or historical component ordinals.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

SCHEMA_VERSION = "1"
BITMAP_MAGIC = b"RABM"
BITMAP_VERSION = 1
BITMAP_HEADER = struct.Struct(">4sBIII32s")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_bitmap_bytes(mask: np.ndarray) -> bytes:
    binary = (np.asarray(mask) > 0).astype(np.uint8)
    if binary.ndim != 2:
        raise ValueError("residual_bitmap_must_be_2d")
    height, width = binary.shape
    payload = np.packbits(binary.reshape(-1), bitorder="big").tobytes()
    payload_hash = hashlib.sha256(payload).digest()
    return BITMAP_HEADER.pack(
        BITMAP_MAGIC, BITMAP_VERSION, width, height, len(payload), payload_hash
    ) + payload


def decode_canonical_bitmap(encoded: bytes) -> np.ndarray:
    if len(encoded) < BITMAP_HEADER.size:
        raise ValueError("residual_bitmap_header_truncated")
    magic, version, width, height, payload_length, payload_hash = (
        BITMAP_HEADER.unpack(encoded[:BITMAP_HEADER.size]))
    if magic != BITMAP_MAGIC or version != BITMAP_VERSION:
        raise ValueError("residual_bitmap_contract_unsupported")
    payload = encoded[BITMAP_HEADER.size:]
    if len(payload) != payload_length:
        raise ValueError("residual_bitmap_payload_length_mismatch")
    if hashlib.sha256(payload).digest() != payload_hash:
        raise ValueError("residual_bitmap_payload_hash_mismatch")
    pixels = width * height
    unpacked = np.unpackbits(
        np.frombuffer(payload, dtype=np.uint8), bitorder="big")[:pixels]
    return unpacked.reshape((height, width)).astype(np.uint8) * 255


def default_detector_contract(*, detector_name: str,
                              detector_version: str,
                              implementation_revision: str,
                              connectivity: int = 8) -> dict[str, Any]:
    if connectivity not in (4, 8):
        raise ValueError("component_connectivity_unsupported")
    return {
        "detector_name": str(detector_name),
        "detector_version": str(detector_version),
        "implementation_revision": str(implementation_revision),
        "color_space": "BGR_uint8_then_binary_layers",
        "dtype": "uint8",
        "endianness": "not_applicable_for_uint8",
        "connectivity": connectivity,
        "threshold_contract": {
            "binary_foreground": "value_gt_zero",
            "rounding": "none",
            "clipping": "uint8_0_255",
        },
        "morphology_contract": {
            "validation_halo_kernel": "ones_uint8",
            "validation_halo_kernel_size": [5, 5],
            "validation_halo_dilations": 1,
            "residual_erosions": 0,
            "residual_dilations": 0,
            "border_policy": "opencv_default_constant",
        },
        "operation_order": [
            "binary_normalize_layers",
            "union_text_core_outline_antialias",
            "subtract_base_mask",
            "intersect_validation_halo",
            "extract_connected_components",
            "derive_content_addressed_components",
        ],
        "component_extraction_contract": {
            "algorithm": "opencv_connectedComponentsWithStats",
            "foreground": 1,
            "background": 0,
            "label_type": "CV_32S",
        },
        "component_sort_contract": {
            "identity": "content_addressed_independent_of_order",
            "display_order": ["bounds_y", "bounds_x", "component_id"],
        },
        "library_versions": {
            "opencv": cv2.__version__,
            "numpy": np.__version__,
            "python": platform.python_version(),
        },
    }


def _component_manifest(analysis_id: str, residual: np.ndarray,
                        connectivity: int) -> dict[str, Any]:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(
        (residual > 0).astype(np.uint8), connectivity, cv2.CV_32S)
    components: list[dict[str, Any]] = []
    union = np.zeros_like(residual, dtype=np.uint8)
    for label in range(1, count):
        x, y, width, height, area = [int(value) for value in stats[label]]
        local = (labels[y:y + height, x:x + width] == label).astype(np.uint8) * 255
        encoded = canonical_bitmap_bytes(local)
        bitmap_hash = _sha256(encoded)
        bounds = [x, y, x + width, y + height]
        identity_payload = _canonical_json({
            "analysis_id": analysis_id,
            "bounds": bounds,
            "component_bitmap_hash": bitmap_hash,
        })
        component_id = _sha256(identity_payload)
        union[labels == label] = 255
        contour_data, _ = cv2.findContours(
            local, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
        contour_points = sorted(
            [point[0].tolist() for contour in contour_data for point in contour],
            key=lambda point: (point[1], point[0]))
        components.append({
            "component_id": component_id,
            "residual_analysis_id": analysis_id,
            "component_bitmap_hash": bitmap_hash,
            "component_payload_hash": _sha256(encoded[BITMAP_HEADER.size:]),
            "bounds": bounds,
            "pixel_count": area,
            "centroid": [
                round(float(centroids[label][0]), 6),
                round(float(centroids[label][1]), 6),
            ],
            "contour_hash": _sha256(_canonical_json(contour_points)),
            "touches_border": bool(
                x == 0 or y == 0
                or x + width == residual.shape[1]
                or y + height == residual.shape[0]),
            "seed_relations": [],
            "classification": "unclassified",
            "confidence": 0.0,
            "reason_codes": [],
        })
    if not np.array_equal(union > 0, residual > 0):
        raise ValueError("component_union_mismatch")
    components.sort(key=lambda item: (
        item["bounds"][1], item["bounds"][0], item["component_id"]))
    manifest_core = {
        "schema_version": SCHEMA_VERSION,
        "residual_analysis_id": analysis_id,
        "component_count": len(components),
        "component_ids": sorted(item["component_id"] for item in components),
        "display_order": [item["component_id"] for item in components],
        "components": components,
        "union_matches_residual": True,
        "components_are_disjoint": True,
    }
    return {
        **manifest_core,
        "component_manifest_hash": _sha256(_canonical_json(manifest_core)),
    }


def create_residual_analysis(*, residual_bitmap: np.ndarray,
                             owner: str, job_id: str, run_id: str,
                             revision_id: str, page_id: str, region_id: str,
                             source_page_hash: str, source_crop_hash: str,
                             previous_preview_hash: str, base_mask_hash: str,
                             validation_halo_hash: str, ocr_geometry_hash: str,
                             detector_contract: dict[str, Any],
                             image_dimensions: list[int],
                             pixel_origin: list[int],
                             analysis_generation: str = "successor_analysis",
                             supersedes_for_future_processing: str = "") -> dict[str, Any]:
    encoded = canonical_bitmap_bytes(residual_bitmap)
    decoded = decode_canonical_bitmap(encoded)
    if not np.array_equal(decoded, (np.asarray(residual_bitmap) > 0).astype(np.uint8) * 255):
        raise ValueError("canonical_bitmap_roundtrip_failed")
    bitmap_hash = _sha256(encoded)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "source_page_hash": source_page_hash,
        "source_crop_hash": source_crop_hash,
        "previous_preview_hash": previous_preview_hash,
        "base_mask_hash": base_mask_hash,
        "validation_halo_hash": validation_halo_hash,
        "ocr_geometry_hash": ocr_geometry_hash,
        "detector_contract": detector_contract,
        "image_dimensions": image_dimensions,
        "pixel_origin": pixel_origin,
        "residual_bitmap_hash": bitmap_hash,
        "analysis_generation": analysis_generation,
        "supersedes_for_future_processing": supersedes_for_future_processing,
    }
    analysis_id = _sha256(_canonical_json(identity))
    connectivity = int(detector_contract["connectivity"])
    component_manifest = _component_manifest(
        analysis_id, decoded, connectivity)
    artifact = {
        **identity,
        "residual_analysis_id": analysis_id,
        "owner": str(owner),
        "job_id": str(job_id),
        "run_id": str(run_id),
        "revision_id": str(revision_id),
        "page_id": str(page_id),
        "region_id": str(region_id),
        "residual_bitmap_encoding": {
            "magic": BITMAP_MAGIC.decode("ascii"),
            "version": BITMAP_VERSION,
            "row_order": "top_to_bottom",
            "bit_order": "big",
            "channel_count": 1,
            "dtype": "binary_uint8",
            "endianness": "big_endian_header",
            "compression": "none",
            "payload_length": len(encoded) - BITMAP_HEADER.size,
            "encoded_length": len(encoded),
        },
        "residual_pixel_count": int(np.count_nonzero(decoded)),
        "component_count": component_manifest["component_count"],
        "component_manifest_hash": component_manifest["component_manifest_hash"],
        "status": "reproducible_successor",
    }
    return {
        "artifact": artifact,
        "bitmap_bytes": encoded,
        "bitmap": decoded,
        "component_manifest": component_manifest,
    }


def validate_analysis_bundle(bundle: dict[str, Any]) -> None:
    artifact = bundle["artifact"]
    encoded = bundle["bitmap_bytes"]
    bitmap = decode_canonical_bitmap(encoded)
    if _sha256(encoded) != artifact["residual_bitmap_hash"]:
        raise ValueError("residual_bitmap_hash_mismatch")
    if int(np.count_nonzero(bitmap)) != int(artifact["residual_pixel_count"]):
        raise ValueError("residual_pixel_count_mismatch")
    regenerated = _component_manifest(
        artifact["residual_analysis_id"], bitmap,
        int(artifact["detector_contract"]["connectivity"]))
    if regenerated["component_manifest_hash"] != artifact["component_manifest_hash"]:
        raise ValueError("component_manifest_hash_mismatch")
    if regenerated["component_ids"] != bundle["component_manifest"]["component_ids"]:
        raise ValueError("component_identity_mismatch")


def write_analysis_bundle(bundle: dict[str, Any], root: str | Path,
                          stem: str) -> dict[str, str]:
    validate_analysis_bundle(bundle)
    output = Path(root)
    output.mkdir(parents=True, exist_ok=True)
    bitmap_path = output / f"{stem}_residual_bitmap.bin"
    png_path = output / f"{stem}_residual_bitmap.png"
    analysis_path = output / f"{stem}_residual_analysis.json"
    components_path = output / f"{stem}_component_manifest.json"
    bitmap_path.write_bytes(bundle["bitmap_bytes"])
    cv2.imwrite(str(png_path), bundle["bitmap"])
    analysis_path.write_text(
        json.dumps(bundle["artifact"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    components_path.write_text(
        json.dumps(bundle["component_manifest"], ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return {
        "residual_bitmap_asset": bitmap_path.name,
        "residual_bitmap_png": png_path.name,
        "analysis_manifest": analysis_path.name,
        "component_manifest": components_path.name,
    }


class ResidualAnalysisStore:
    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS residual_analyses (
                residual_analysis_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                job_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                region_id TEXT NOT NULL,
                residual_bitmap_hash TEXT NOT NULL,
                component_manifest_hash TEXT NOT NULL,
                residual_bitmap_asset TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                component_manifest_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def persist(self, bundle: dict[str, Any], *,
                residual_bitmap_asset: str) -> dict[str, Any]:
        validate_analysis_bundle(bundle)
        artifact = bundle["artifact"]
        existing = self.get(artifact["residual_analysis_id"])
        if existing:
            return existing
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO residual_analyses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            artifact["residual_analysis_id"], artifact["owner"],
            artifact["job_id"], artifact["run_id"], artifact["revision_id"],
            artifact["page_id"], artifact["region_id"],
            artifact["residual_bitmap_hash"],
            artifact["component_manifest_hash"], residual_bitmap_asset,
            json.dumps(artifact, ensure_ascii=False, sort_keys=True),
            json.dumps(bundle["component_manifest"], ensure_ascii=False, sort_keys=True),
            artifact["status"], now, SCHEMA_VERSION,
        ))
        self._conn.commit()
        return self.get(artifact["residual_analysis_id"])

    def get(self, analysis_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM residual_analyses WHERE residual_analysis_id=?",
            (str(analysis_id),)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["analysis"] = json.loads(result.pop("analysis_json"))
        result["component_manifest"] = json.loads(
            result.pop("component_manifest_json"))
        return result

    def latest_for_region(self, *, owner: str, job_id: str, run_id: str,
                          revision_id: str, region_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("""
            SELECT residual_analysis_id FROM residual_analyses
            WHERE owner=? AND job_id=? AND run_id=? AND revision_id=? AND region_id=?
            ORDER BY created_at DESC, residual_analysis_id DESC LIMIT 1
        """, (
            str(owner), str(job_id), str(run_id), str(revision_id),
            str(region_id))).fetchone()
        return self.get(row[0]) if row else None

    def close(self) -> None:
        self._conn.close()
