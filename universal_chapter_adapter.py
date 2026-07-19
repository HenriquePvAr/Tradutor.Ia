"""Generic, explainable chapter-reader analysis for public pages.

This module does not fetch a chapter.  It observes candidates already exposed by the browser,
groups them into possible readers and returns a sanitised decision.  Downloading remains a
separate, bounded operation and only receives the accepted/confirmed candidate URLs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import base64
import binascii
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from chapter_source import (
    AUTHENTICATION_REQUIRED,
    CHALLENGE_REQUIRED,
    INCOMPLETE_DOWNLOAD,
    NO_CHAPTER_IMAGES,
    REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
    SUPPORTED_GENERIC_HIGH_CONFIDENCE,
    SUPPORTED_SPECIFIC_ADAPTER,
    UNSUPPORTED_CANVAS_READER,
    UNSUPPORTED_CROSS_ORIGIN_READER,
    UNSUPPORTED_LOW_CONFIDENCE,
    looks_like_challenge,
)

HIGH_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.60
MAX_CANDIDATES = 1_200
MAX_JSON_BYTES = 512_000
MAX_CANVAS_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_CANVAS_TOTAL_BYTES = 32 * 1024 * 1024
MAX_CANVAS_CAPTURES = 8
MAX_DOM_CANDIDATES = 700
MAX_NETWORK_CANDIDATES = 300
MAX_JSON_SCRIPTS = 12
MAX_CDP_EVENTS = 2_000
MAX_CDP_JSON_RESPONSES = 8
MAX_NETWORK_METADATA = 300
# Kept in sync with the shared transport default. A reader larger than this cannot be
# downloaded completely in one run, so it must stop before OCR rather than truncate.
MAX_AUTOMATIC_PAGES = 400

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp")
_NEGATIVE_TOKENS = (
    "logo", "favicon", "avatar", "icon", "sprite", "thumbnail", "thumb", "banner",
    "advert", "advertisement", "promo", "recommend", "comment", "footer", "header",
    "button", "tracking", "pixel", "product", "cover",
)
_READER_TOKENS = ("reader", "chapter", "manga", "comic", "webtoon", "page", "pages")
_AUTH_MARKERS = ("sign in", "log in", "login required", "authentication required")
_INCOMPLETE_COVERAGE_WARNINGS = frozenset({
    "candidate_limit", "dom_scan_limit", "json_manifest_limit", "canvas_capture_limit",
    "canvas_capture_too_large", "canvas_capture_unavailable", "scroll_incomplete",
    "page_limit_exceeded", "network_resource_limit", "iframe_limit", "iframe_depth_limit",
    "network_log_limit", "network_json_limit",
})
_COLLECTOR_WARNINGS = _INCOMPLETE_COVERAGE_WARNINGS | frozenset({
    "cross_origin_iframe", "cross_origin_reader",
})
_SAFE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def _number(value: Any) -> int:
    try:
        number = float(value or 0)
        return max(0, int(number)) if math.isfinite(number) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _signed_number(value: Any, default: int = -1) -> int:
    try:
        number = float(value)
        return int(number) if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_reason(value: Any, fallback: str = "unsafe_resource") -> str:
    reason = str(value or "").strip().casefold()
    return reason if _SAFE_REASON_RE.fullmatch(reason) else fallback


def _sanitize_warnings(values: Iterable[Any]) -> list[str]:
    warnings: list[str] = []
    for value in values:
        warning = str(value or "").strip()
        safe = warning if warning in _COLLECTOR_WARNINGS else "collector_warning"
        if safe not in warnings and len(warnings) < 32:
            warnings.append(safe)
    return warnings


def _clean_url(value: Any, page_url: str) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith(("data:", "javascript:", "file:")):
        return ""
    return urljoin(page_url, raw)


def _urls_from_value(value: Any) -> list[str]:
    """Extract image URLs from srcset, CSS url() or a direct value without executing it."""
    raw = str(value or "").strip()
    if not raw:
        return []
    if "url(" in raw:
        return [match.strip(" '\"") for match in re.findall(r"url\((.*?)\)", raw) if match.strip()]
    if "," in raw:
        values: list[tuple[int, str]] = []
        for part in raw.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            width = 0
            if bits[-1].endswith("w") and bits[-1][:-1].isdigit():
                width = int(bits[-1][:-1])
            values.append((width, bits[0]))
        if values:
            return [max(values, key=lambda item: item[0])[1]]
    return [raw.split()[0]]


def _safe_path_fingerprint(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    digest = hashlib.sha256(path.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{parsed.hostname or ''}:{digest}"


def _candidate_id(url: str, source: str, order: int) -> str:
    # Signed CDN query strings rotate between the preview and the runner.  IDs deliberately
    # bind the observed host/path/order, not a token-bearing query, so a legitimate renewal
    # does not look like a different page.  The runner still re-clusters and validates it.
    parsed = urlparse(url)
    stable_url = (
        f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path}"
        if parsed.scheme.casefold() in {"http", "https"} else str(url)
    )
    payload = f"{stable_url}\0{source}\0{order}".encode("utf-8", "ignore")
    return hashlib.sha256(payload).hexdigest()[:20]


def _resource_identity(url: str) -> str:
    """Identify one exact remote asset without retaining a query string in diagnostics.

    A query can carry either a rotating credential *or* a logical page number.  Hash it in
    memory so distinct query-addressed pages cannot disappear before the download gate, while
    never returning its raw value from this module.  A same-position fallback below still
    collapses two signed variants of one observed DOM page.
    """
    parsed = urlparse(str(url or ""))
    if parsed.scheme.casefold() in {"http", "https"}:
        query_hash = hashlib.sha256(parsed.query.encode("utf-8", "ignore")).hexdigest()[:20]
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path}?{query_hash}"
    return str(url or "")


def _resource_slot(candidate: ImageCandidate) -> str:
    """Return a query-free position marker for one browser-observed reader element."""

    parsed = urlparse(candidate.url)
    if parsed.scheme.casefold() in {"http", "https"}:
        # A non-zero layout position is stronger than collector order. This remains entirely
        # local to one analysis and is never exposed in reports.
        position = f"y:{candidate.y}" if candidate.y else f"order:{candidate.order}"
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path}\0{position}"
    return candidate.url


def _filename_sequence_key(url: str) -> str:
    path = urlparse(url).path.casefold()
    return re.sub(r"\d+", "#", path)


def cluster_evidence_id(value: str) -> str:
    """Opaque, stable cluster evidence suitable for reports and reusable profiles.

    A DOM id/class is page-controlled and can accidentally contain a token or URL.  Keep
    that raw value only in the in-memory cluster used for this analysis; public diagnostics
    and profiles receive a one-way identifier instead.
    """
    digest = hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest()[:20]
    return f"cluster:{digest}"


@dataclass(frozen=True)
class ImageCandidate:
    """One potential page. ``url`` stays in memory; public diagnostics use ``public``."""

    id: str
    url: str
    source: str
    order: int
    y: int = 0
    width: int = 0
    height: int = 0
    natural_width: int = 0
    natural_height: int = 0
    container: str = ""
    class_name: str = ""
    element_id: str = ""
    alt: str = ""
    context: str = ""
    network_order: int = -1
    content_type: str = ""
    origin: str = "dom"
    visible: bool = True
    attribute_names: tuple[str, ...] = field(default_factory=tuple)
    # Canvas bytes are intentionally in-memory only.  The public/SQLite diagnostic carries
    # its opaque id and dimensions, never a data URI or pixels.
    canvas_data: bytes = field(default=b"", repr=False, compare=False)

    @property
    def effective_width(self) -> int:
        return max(self.width, self.natural_width)

    @property
    def effective_height(self) -> int:
        return max(self.height, self.natural_height)

    @property
    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "host": urlparse(self.url).hostname or "",
            "path_fingerprint": _safe_path_fingerprint(self.url),
            "source": self.source,
            "order": self.order,
            "width": self.effective_width,
            "height": self.effective_height,
            "origin": self.origin,
            "visible": self.visible,
        }


@dataclass
class CandidateCluster:
    key: str
    candidates: list[ImageCandidate] = field(default_factory=list)
    score: float = 0.0
    signals: list[str] = field(default_factory=list)
    exclusions: Counter[str] = field(default_factory=Counter)

    def public(self) -> dict[str, Any]:
        return {
            "key": cluster_evidence_id(self.key),
            "score": round(self.score, 3),
            "count": len(self.candidates),
            "signals": list(self.signals),
            "exclusions": dict(self.exclusions),
            "candidate_ids": [candidate.id for candidate in self.candidates],
        }


@dataclass
class SourceAnalysis:
    adapter: str
    final_host: str
    outcome: str
    confidence: float
    accepted: list[ImageCandidate] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[CandidateCluster] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    canvas_detected: int = 0
    canvas_captured: int = 0
    profile_used: bool = False
    network_metadata: list[dict[str, Any]] = field(default_factory=list)

    @property
    def requires_review(self) -> bool:
        return self.outcome == REVIEW_REQUIRED_MEDIUM_CONFIDENCE

    @property
    def can_download(self) -> bool:
        return self.outcome in {SUPPORTED_SPECIFIC_ADAPTER, SUPPORTED_GENERIC_HIGH_CONFIDENCE}

    def public(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "final_host": self.final_host,
            "outcome": self.outcome,
            "confidence": round(self.confidence, 3),
            "candidate_count": sum(len(cluster.candidates) for cluster in self.clusters)
            + len(self.discarded),
            "accepted_count": len(self.accepted),
            "discarded_count": len(self.discarded),
            "accepted": [candidate.public for candidate in self.accepted],
            "discarded": list(self.discarded),
            "clusters": [cluster.public() for cluster in self.clusters],
            "warnings": list(self.warnings),
            "canvas_detected": self.canvas_detected,
            "canvas_captured": self.canvas_captured,
            "profile_used": self.profile_used,
            "network_metadata": list(self.network_metadata),
        }


def extract_manifest_urls(payload: Any, *, page_url: str, limit: int = MAX_CANDIDATES) -> list[str]:
    """Extract likely image URLs from already-parsed JSON, bounded and side-effect free."""
    found: list[str] = []
    seen: set[str] = set()

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 16 or len(found) >= limit:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                key_hint = str(key).casefold()
                if isinstance(item, str) and any(token in key_hint for token in
                                                 ("image", "page", "src", "url", "file")):
                    for raw in _urls_from_value(item):
                        url = _clean_url(raw, page_url)
                        if url.startswith(("http://", "https://")) and url not in seen:
                            seen.add(url)
                            found.append(url)
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)

    visit(payload)
    return found


def extract_manifest_urls_from_text(text: str, *, page_url: str) -> list[str]:
    raw = str(text or "")
    if len(raw.encode("utf-8", "ignore")) > MAX_JSON_BYTES:
        return []
    try:
        return extract_manifest_urls(json.loads(raw), page_url=page_url)
    except (TypeError, ValueError):
        return []


def _negative_reason(candidate: ImageCandidate) -> str:
    haystack = " ".join((candidate.url, candidate.class_name, candidate.element_id,
                           candidate.alt, candidate.context)).casefold()
    for token in _NEGATIVE_TOKENS:
        if token in haystack:
            return token
    if candidate.effective_width and candidate.effective_height:
        if candidate.effective_width <= 4 and candidate.effective_height <= 4:
            return "tracking_pixel"
        if candidate.effective_width < 120 or candidate.effective_height < 120:
            return "small_dimensions"
    return ""


def _cluster_key(candidate: ImageCandidate) -> str:
    if candidate.container:
        return f"container:{candidate.container}"
    if candidate.canvas_data:
        return "canvas:visible_reader"
    parsed = urlparse(candidate.url)
    return f"path:{parsed.hostname or ''}:{_filename_sequence_key(candidate.url)}"


def _cluster_score(cluster: CandidateCluster) -> tuple[float, list[str]]:
    candidates = cluster.candidates
    if not candidates:
        return 0.0, []
    score = 0.0
    signals: list[str] = []
    count = len(candidates)
    if count >= 3:
        score += 0.28
        signals.append("multiple_images")
    elif count == 2:
        score += 0.16
        signals.append("paired_images")
    sizes = [(candidate.effective_width, candidate.effective_height) for candidate in candidates]
    large = [size for size in sizes if size[0] >= 320 and size[1] >= 220]
    if len(large) >= max(1, (count + 1) // 2):
        score += 0.18
        signals.append("reader_sized_images")
    tall = [height / max(width, 1) for width, height in sizes if width and height]
    if tall and sum(value >= 1.2 for value in tall) >= max(1, len(tall) // 2):
        score += 0.10
        signals.append("vertical_pages")
    containers = " ".join(candidate.container.casefold() for candidate in candidates)
    contexts = " ".join(candidate.context.casefold() for candidate in candidates)
    if any(token in containers or token in contexts for token in _READER_TOKENS):
        score += 0.16
        signals.append("reader_container")
    domains = {urlparse(candidate.url).hostname for candidate in candidates}
    if len(domains) == 1:
        score += 0.06
        signals.append("same_resource_host")
    patterns = {_filename_sequence_key(candidate.url) for candidate in candidates}
    if len(patterns) == 1:
        score += 0.07
        signals.append("sequential_path_pattern")
    ordered = sorted(candidates, key=lambda candidate: (candidate.y, candidate.order))
    if len(ordered) >= 2 and all(
        ordered[index].y <= ordered[index + 1].y
        for index in range(len(ordered) - 1)
    ):
        score += 0.06
        signals.append("vertical_dom_order")
    if sum(candidate.network_order >= 0 for candidate in candidates) >= max(1, count // 2):
        score += 0.08
        signals.append("observed_network_resources")
    negatives = sum(cluster.exclusions.values())
    if negatives:
        # One banner nested in an otherwise coherent reader should not erase several strong
        # page signals; repeated interface material still moves the result to manual review.
        score -= min(0.30, 0.06 * negatives)
        signals.append("interface_penalty")
    return max(0.0, min(1.0, score)), signals


def _attribute_names(value: Any) -> tuple[str, ...]:
    """Keep only a bounded list of attribute *names*, never page-controlled values."""
    if not isinstance(value, (list, tuple)):
        return ()
    names: list[str] = []
    for item in value:
        name = str(item or "").casefold()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) and name not in names:
            names.append(name)
        if len(names) >= 16:
            break
    return tuple(names)


def _to_candidate(raw: dict[str, Any], page_url: str, order: int) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    raw_value = raw.get("url") or raw.get("src") or ""
    source = str(raw.get("source") or "dom")[:40]
    if source == "canvas_capture" and str(raw_value).startswith("data:image/"):
        data = _decode_canvas_data_uri(str(raw_value))
        if not data:
            return candidates
        digest = hashlib.sha256(data).hexdigest()[:20]
        synthetic_url = f"canvas://{digest}"
        actual_order = _number(raw.get("order")) or order
        candidates.append(ImageCandidate(
            id=_candidate_id(synthetic_url, source, actual_order),
            url=synthetic_url,
            source=source,
            order=actual_order,
            y=_number(raw.get("y")),
            width=_number(raw.get("width")),
            height=_number(raw.get("height")),
            natural_width=_number(raw.get("naturalWidth") or raw.get("natural_width")),
            natural_height=_number(raw.get("naturalHeight") or raw.get("natural_height")),
            container=str(raw.get("container") or raw.get("containerKey") or "")[:160],
            class_name=str(raw.get("className") or "")[:240],
            element_id=str(raw.get("id") or "")[:120],
            alt=str(raw.get("alt") or "")[:240],
            context=str(raw.get("context") or "")[:240],
            network_order=_signed_number(raw.get("network_order"), -1),
            content_type="image/png",
            origin="canvas_capture",
            visible=True,
            attribute_names=_attribute_names(raw.get("attributeNames")),
            canvas_data=data,
        ))
        return candidates
    for raw_url in _urls_from_value(raw_value):
        url = _clean_url(raw_url, page_url)
        if not url.startswith(("http://", "https://")):
            continue
        actual_order = _number(raw.get("order")) or order
        candidates.append(ImageCandidate(
            id=_candidate_id(url, source, actual_order),
            url=url,
            source=source,
            order=actual_order,
            y=_number(raw.get("y")),
            width=_number(raw.get("width")),
            height=_number(raw.get("height")),
            natural_width=_number(raw.get("naturalWidth") or raw.get("natural_width")),
            natural_height=_number(raw.get("naturalHeight") or raw.get("natural_height")),
            container=str(raw.get("container") or raw.get("containerKey") or "")[:160],
            class_name=str(raw.get("className") or "")[:240],
            element_id=str(raw.get("id") or "")[:120],
            alt=str(raw.get("alt") or "")[:240],
            context=str(raw.get("context") or "")[:240],
            network_order=_signed_number(raw.get("network_order"), -1),
            content_type=str(raw.get("content_type") or "")[:80],
            origin=str(raw.get("origin") or "dom")[:40],
            visible=bool(raw.get("visible", True)),
            attribute_names=_attribute_names(raw.get("attributeNames")),
        ))
    return candidates


def _decode_canvas_data_uri(value: str) -> bytes:
    """Decode a bounded local canvas capture without accepting arbitrary ``data:`` URLs."""
    prefix, marker, encoded = str(value or "").partition(",")
    if not marker or not prefix.casefold().startswith("data:image/"):
        return b""
    if ";base64" not in prefix.casefold():
        return b""
    # Base64 expands by 4/3.  Reject before decoding to avoid a renderer-controlled memory
    # allocation.  The browser collection script enforces the same ceiling.
    if len(encoded) > ((MAX_CANVAS_CAPTURE_BYTES * 4) // 3) + 16:
        return b""
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error):
        return b""
    return data if 12 <= len(data) <= MAX_CANVAS_CAPTURE_BYTES else b""


def analyse_candidates(
    page_url: str,
    raw_candidates: Iterable[dict[str, Any]],
    *,
    adapter: Any,
    final_url: str = "",
    page_text: str = "",
    warnings: Iterable[str] = (),
    canvas_detected: int = 0,
    canvas_captured: int = 0,
    profile: dict[str, Any] | None = None,
    network_metadata: Iterable[dict[str, Any]] = (),
) -> SourceAnalysis:
    """Cluster candidates and return an explainable, non-fetching decision."""
    final_url = final_url or page_url
    final_host = urlparse(final_url).hostname or ""
    safe_warnings = _sanitize_warnings(warnings)
    safe_network_metadata = [dict(item) for item in network_metadata
                             if isinstance(item, dict)][:MAX_NETWORK_METADATA]
    if looks_like_challenge(page_text):
        return SourceAnalysis(adapter=adapter.name, final_host=final_host,
                              outcome=CHALLENGE_REQUIRED, confidence=0.0,
                              warnings=safe_warnings, canvas_detected=canvas_detected,
                              canvas_captured=canvas_captured,
                              network_metadata=safe_network_metadata)
    lowered_text = str(page_text or "").casefold()
    if any(marker in lowered_text for marker in _AUTH_MARKERS):
        return SourceAnalysis(adapter=adapter.name, final_host=final_host,
                              outcome=AUTHENTICATION_REQUIRED, confidence=0.0,
                              warnings=safe_warnings, canvas_detected=canvas_detected,
                              canvas_captured=canvas_captured,
                              network_metadata=safe_network_metadata)
    # A cross-origin frame which itself carries reader evidence cannot be inspected safely.
    # Do not call a partial sibling DOM high-confidence just because it happens to score well.
    if "cross_origin_reader" in safe_warnings:
        return SourceAnalysis(adapter=adapter.name, final_host=final_host,
                              outcome=UNSUPPORTED_CROSS_ORIGIN_READER, confidence=0.0,
                              warnings=safe_warnings, canvas_detected=canvas_detected,
                              canvas_captured=canvas_captured,
                              network_metadata=safe_network_metadata)

    clusters: dict[str, CandidateCluster] = {}
    discarded: list[dict[str, Any]] = []
    seen_resources: set[str] = set()
    seen_slots: set[str] = set()
    for index, raw in enumerate(raw_candidates):
        if index >= MAX_CANDIDATES:
            discarded.append({"reason": "candidate_limit", "count": 1})
            if "candidate_limit" not in safe_warnings:
                safe_warnings.append("candidate_limit")
            break
        if not isinstance(raw, dict):
            discarded.append({"reason": "malformed_candidate"})
            continue
        try:
            converted = _to_candidate(raw, page_url, index)
        except Exception:  # browser/JSON shape must not crash the job
            discarded.append({"reason": "malformed_candidate"})
            continue
        for candidate in converted:
            identity = _resource_identity(candidate.url)
            slot = _resource_slot(candidate)
            if identity in seen_resources or slot in seen_slots:
                discarded.append({"id": candidate.id, "reason": "duplicate_resource"})
                continue
            seen_resources.add(identity)
            seen_slots.add(slot)
            key = _cluster_key(candidate)
            reason = _negative_reason(candidate)
            if reason:
                # Filter obvious interface material before DNS validation. A hostile page
                # cannot turn a long ad list into unbounded resolver work.
                clusters.setdefault(key, CandidateCluster(key=key)).exclusions[reason] += 1
                discarded.append({"id": candidate.id, "reason": reason, **candidate.public})
                continue
            if not candidate.canvas_data:
                try:
                    validate_observed = getattr(adapter, "validate_observed_url", None)
                    if callable(validate_observed):
                        validate_observed(candidate.url)
                except Exception as exc:  # source errors are deliberately reduced to a reason
                    discarded.append({"id": candidate.id,
                                      "reason": _safe_reason(getattr(exc, "detail", ""))})
                    continue
            # A JSON manifest often carries only URLs while the DOM images carry the
            # semantic reader container. If the filename family and host agree, join that
            # manifest entry to the existing reader cluster instead of treating it as an
            # unrelated singleton.
            if not candidate.container:
                pattern = _filename_sequence_key(candidate.url)
                host = urlparse(candidate.url).hostname
                for known_key, known_cluster in clusters.items():
                    if any(
                        urlparse(existing.url).hostname == host
                        and _filename_sequence_key(existing.url) == pattern
                        for existing in known_cluster.candidates
                    ):
                        key = known_key
                        break
            clusters.setdefault(key, CandidateCluster(key=key)).candidates.append(candidate)

    cluster_list = list(clusters.values())
    profile_host = str((profile or {}).get("host") or "").casefold()
    profile_key = str((profile or {}).get("container_evidence") or "")
    profile_usable = bool(
        not adapter.is_specific and profile_host == final_host.casefold() and profile_key
    )
    profile_used = False
    for cluster in cluster_list:
        cluster.score, cluster.signals = _cluster_score(cluster)
        # A profile only annotates a cluster that independently matches fresh DOM evidence.
        # It never authorises a host, changes fresh ranking, or promotes review to automatic.
        if profile_usable and cluster_evidence_id(cluster.key) == profile_key:
            cluster.signals.append("validated_profile_evidence")
            profile_used = True
    # Select only from fresh reader evidence. A previously saved profile can annotate the
    # matching cluster, but cannot alter the ranking or move it across a confidence boundary.
    cluster_list.sort(key=lambda cluster: (-cluster.score, -len(cluster.candidates), cluster.key))
    if not cluster_list:
        outcome = UNSUPPORTED_CANVAS_READER if canvas_detected else NO_CHAPTER_IMAGES
        return SourceAnalysis(adapter=adapter.name, final_host=final_host, outcome=outcome,
                              confidence=0.0, discarded=discarded, clusters=[],
                              warnings=safe_warnings, canvas_detected=canvas_detected,
                              canvas_captured=canvas_captured,
                              network_metadata=safe_network_metadata)

    selected = cluster_list[0]
    accepted = sorted(selected.candidates, key=lambda candidate: (candidate.y, candidate.order))
    if len(accepted) > MAX_AUTOMATIC_PAGES and "page_limit_exceeded" not in safe_warnings:
        safe_warnings.append("page_limit_exceeded")
    coverage_incomplete = any(warning in _INCOMPLETE_COVERAGE_WARNINGS for warning in safe_warnings)
    if coverage_incomplete:
        # We know there may be pages outside the observed set. Do not offer a misleading
        # confirmation of a partial chapter and never start OCR from a truncated surface.
        outcome = INCOMPLETE_DOWNLOAD
    elif adapter.is_specific:
        outcome = SUPPORTED_SPECIFIC_ADAPTER
    elif selected.score >= HIGH_CONFIDENCE:
        outcome = SUPPORTED_GENERIC_HIGH_CONFIDENCE
    elif selected.score >= MEDIUM_CONFIDENCE:
        outcome = REVIEW_REQUIRED_MEDIUM_CONFIDENCE
    else:
        outcome = UNSUPPORTED_LOW_CONFIDENCE
    # Do not grant resource access until a complete cluster has a usable outcome. The policy
    # remains in memory and disappears with this adapter/run.
    if outcome in {SUPPORTED_SPECIFIC_ADAPTER, SUPPORTED_GENERIC_HIGH_CONFIDENCE,
                   REVIEW_REQUIRED_MEDIUM_CONFIDENCE}:
        for candidate in accepted:
            if not candidate.canvas_data:
                adapter.authorize_related_url(candidate.url)
    return SourceAnalysis(
        adapter=adapter.name,
        final_host=final_host,
        outcome=outcome,
        confidence=selected.score,
        accepted=accepted if outcome != UNSUPPORTED_LOW_CONFIDENCE else [],
        discarded=discarded,
        clusters=cluster_list,
        warnings=safe_warnings,
        canvas_detected=canvas_detected,
        canvas_captured=canvas_captured,
        profile_used=profile_used,
        network_metadata=safe_network_metadata,
    )


_COLLECTION_SCRIPT = r"""
const out = []; const warnings = []; const json = []; let order = 0; let canvasDetected = 0; let canvasCaptured = 0; let canvasBytes = 0; let jsonChars = 0;
const maxDomCandidates = 700; const maxNetworkCandidates = 300; const maxJsonScripts = 12; const maxJsonChars = 256000;
const maxCanvasDataLength = 22369640; const maxCanvasCaptures = 8; const maxCanvasBytes = 33554432;
const maxIframeDocuments = 24; const maxIframeDepth = 8; let iframeDocuments = 0; const scannedRoots = new WeakSet();
const attrs = ['src','data-src','data-lazy-src','data-original','data-url','data-image','data-image-url','data-page'];
const jsonAttrs = ['data-pages','data-images','data-manifest','data-reader-data'];
const warn = value => { if (!warnings.includes(value) && warnings.length < 32) warnings.push(value); };
const each = (root, selector, callback, limit=2000) => { const nodes=root.querySelectorAll(selector); for (let i=0; i<nodes.length && i<limit; i++) callback(nodes[i], i); if (nodes.length > limit) warn('dom_scan_limit'); };
const add = (el, tag, url, source, context='') => {
  if (!url) return;
  if (out.length >= maxDomCandidates) { warn('candidate_limit'); return false; }
  const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : {width:0,height:0,top:0};
  const style = getComputedStyle(el); const visible = style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0;
  const chain = []; let parent = el;
  for (let i = 0; parent && i < 5; i++, parent = parent.parentElement) {
    const value = `${parent.id || ''} ${parent.className || ''}`.trim(); if (value) chain.push(value);
  }
  const container = chain.find(value => /reader|chapter|manga|comic|webtoon|pages?/i.test(value)) || chain[0] || '';
  const attributeNames = attrs.filter(name => el.hasAttribute && el.hasAttribute(name));
  out.push({tag, url, source, order: order++, y: Math.round((window.scrollY || 0) + rect.top),
    width: Math.round(rect.width || el.naturalWidth || el.width || 0), height: Math.round(rect.height || el.naturalHeight || el.height || 0),
    naturalWidth: el.naturalWidth || el.width || 0, naturalHeight: el.naturalHeight || el.height || 0,
    container, className: String(el.className || ''), id: el.id || '', alt: el.alt || '', attributeNames, visible, context: `${context} ${chain.join(' ')}`.slice(0,240), origin:'dom'});
  return true;
};
const scan = (root, context='', iframeDepth=0) => {
  if (!root || !root.querySelectorAll) return;
  if (scannedRoots.has(root)) return;
  scannedRoots.add(root);
  each(root, 'img', el => { attrs.forEach(name => add(el,'img',el.getAttribute(name),name,context)); add(el,'img',el.currentSrc,'currentSrc',context); add(el,'img',el.getAttribute('srcset'),'srcset',context); });
  each(root, 'picture source, source[srcset]', el => add(el,'source',el.getAttribute('srcset'),'source.srcset',context));
  each(root, 'a[href]', el => { const href=el.getAttribute('href') || ''; if (/\.(jpe?g|png|webp|avif|gif)(?:[?#]|$)/i.test(href)) add(el,'link',href,'direct_image_link',context); });
  each(root, '*', el => { const bg=getComputedStyle(el).backgroundImage || ''; if (bg && bg !== 'none') add(el,'background',bg,'background-image',context); jsonAttrs.forEach(name => { const value=el.getAttribute && el.getAttribute(name); if (value && value.length <= maxJsonChars && (value.trim().startsWith('{') || value.trim().startsWith('['))) { if (json.length < maxJsonScripts && jsonChars + value.length <= maxJsonChars) { json.push(value); jsonChars += value.length; } else warn('json_manifest_limit'); } }); if (el.shadowRoot) scan(el.shadowRoot, `${context} shadow`, iframeDepth); });
  each(root, 'canvas', el => { const rect=el.getBoundingClientRect(); const style=getComputedStyle(el); const visible=style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || 1) > 0; if (visible && rect.width >= 200 && rect.height >= 200) { canvasDetected++; if (canvasCaptured >= maxCanvasCaptures || canvasBytes >= maxCanvasBytes) { warn('canvas_capture_limit'); return; } try { const data=el.toDataURL('image/png'); const payloadLength=Math.max(0, data.length-data.indexOf(',')-1); const byteEstimate=Math.ceil(payloadLength*0.75); if (data.length <= maxCanvasDataLength && canvasBytes+byteEstimate <= maxCanvasBytes && add(el,'canvas',data,'canvas_capture',context)) { canvasCaptured++; canvasBytes+=byteEstimate; } else { warn('canvas_capture_too_large'); } } catch (_) { warn('canvas_capture_unavailable'); } } });
  each(root, 'iframe', (frame, index) => { try { if (frame.contentDocument && frame.contentDocument.location.origin === location.origin) { if (iframeDepth >= maxIframeDepth) { warn('iframe_depth_limit'); return; } if (iframeDocuments >= maxIframeDocuments) { warn('iframe_limit'); return; } iframeDocuments++; scan(frame.contentDocument, `${context} iframe_${index}`, iframeDepth + 1); } else { const rect=frame.getBoundingClientRect ? frame.getBoundingClientRect() : {width:0,height:0}; const hint=`${frame.src || ''} ${frame.title || ''} ${frame.id || ''} ${frame.className || ''}`; if (rect.width >= 240 && rect.height >= 180 && /reader|chapter|manga|comic|webtoon|pages?/i.test(hint)) warn('cross_origin_reader'); else warn('cross_origin_iframe'); } } catch (_) { warn('cross_origin_iframe'); } });
};
scan(document);
const allResources = performance.getEntriesByType('resource'); const imageResources = allResources.filter(entry => entry.initiatorType === 'img' || /\.(jpe?g|png|webp|avif|gif)(?:[?#]|$)/i.test(entry.name)); if (imageResources.length > maxNetworkCandidates) warn('network_resource_limit'); const resources = imageResources.slice(-maxNetworkCandidates).map((entry,index) => ({url:entry.name, source:'network_image', order:100000+index, network_order:index, content_type:'', origin:'network'}));
each(document, 'script[type="application/json"], script#__NEXT_DATA__', el => { const text=el.textContent || ''; if (text.length <= maxJsonChars && jsonChars + text.length <= maxJsonChars && json.length < maxJsonScripts) { json.push(text); jsonChars += text.length; } else { warn('json_manifest_limit'); } }, maxJsonScripts);
return {candidates:out, resources, json, warnings:[...new Set(warnings)], canvasDetected, canvasCaptured, pageText:(document.body && document.body.innerText || '').slice(0,20000)};
"""


def _response_content_type(response: dict[str, Any]) -> str:
    raw = str(response.get("mimeType") or response.get("mime_type") or "")
    return raw.split(";", 1)[0].strip().casefold()[:80]


def _response_content_length(response: dict[str, Any]) -> int:
    headers = response.get("headers")
    if not isinstance(headers, dict):
        return 0
    for name, value in headers.items():
        if str(name).casefold() != "content-length":
            continue
        try:
            size = int(str(value))
        except (TypeError, ValueError):
            return 0
        return size if 0 <= size <= 1024 * 1024 * 1024 else 0
    return 0


def _is_json_content_type(content_type: str) -> bool:
    return content_type == "application/json" or content_type.endswith("+json")


def _is_image_response(content_type: str, url: str) -> bool:
    return content_type.startswith("image/") or bool(
        re.search(r"\.(?:jpe?g|png|webp|avif|gif)(?:[?#]|$)", url, re.IGNORECASE))


def _performance_message(entry: Any) -> tuple[str, dict[str, Any]]:
    """Decode one Chrome performance-log envelope without retaining headers or bodies."""
    if not isinstance(entry, dict):
        return "", {}
    raw = entry.get("message")
    if not isinstance(raw, str) or len(raw) > 256_000:
        return "", {}
    try:
        outer = json.loads(raw)
    except (TypeError, ValueError):
        return "", {}
    message = outer.get("message") if isinstance(outer, dict) else None
    if not isinstance(message, dict):
        return "", {}
    method = str(message.get("method") or "")
    params = message.get("params")
    return method, params if isinstance(params, dict) else {}


def _collect_cdp_network(driver: Any, page_url: str) -> dict[str, Any]:
    """Collect bounded CDP/performance evidence from requests the browser already made.

    This function never makes an HTTP request.  It intentionally discards response headers,
    query strings, request IDs and JSON bodies after extracting recognised page URLs; only a
    compact sanitised metadata record survives in the analysis report.
    """
    get_log = getattr(driver, "get_log", None)
    if not callable(get_log):
        return {"candidates": [], "json_candidates": [], "metadata": [], "warnings": []}
    try:
        entries = get_log("performance")
    except Exception:  # noqa: BLE001 - performance logging is optional per driver
        return {"candidates": [], "json_candidates": [], "metadata": [], "warnings": []}
    if not isinstance(entries, list):
        return {"candidates": [], "json_candidates": [], "metadata": [], "warnings": []}

    warnings: list[str] = []
    if len(entries) > MAX_CDP_EVENTS:
        warnings.append("network_log_limit")
        entries = entries[-MAX_CDP_EVENTS:]
    candidates: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    json_requests: list[tuple[str, str]] = []
    for event_index, entry in enumerate(entries):
        method, params = _performance_message(entry)
        if method != "Network.responseReceived":
            continue
        response = params.get("response")
        if not isinstance(response, dict):
            continue
        url = _clean_url(response.get("url"), page_url)
        if not url.startswith(("http://", "https://")):
            continue
        content_type = _response_content_type(response)
        status = _number(response.get("status"))
        initiator = str(params.get("type") or "")[:40]
        metadata.append({
            "host": urlparse(url).hostname or "",
            "path_fingerprint": _safe_path_fingerprint(url),
            "content_type": content_type,
            "content_length": _response_content_length(response),
            "status": status,
            "order": len(metadata),
            "initiator": initiator,
            "timestamp_ms": _number(params.get("timestamp")) * 1000,
        })
        if len(metadata) >= MAX_NETWORK_METADATA:
            warnings.append("network_resource_limit")
            break
        if _is_image_response(content_type, url):
            candidates.append({
                "url": url, "source": "cdp_network_image", "order": 300000 + event_index,
                "network_order": event_index, "content_type": content_type,
                "origin": "network_cdp",
            })
        request_id = str(params.get("requestId") or "")
        if status == 200 and request_id and _is_json_content_type(content_type):
            if len(json_requests) < MAX_CDP_JSON_RESPONSES:
                json_requests.append((request_id, url))
            elif "network_json_limit" not in warnings:
                warnings.append("network_json_limit")

    get_body = getattr(driver, "execute_cdp_cmd", None)
    json_candidates: list[dict[str, Any]] = []
    if callable(get_body):
        for request_index, (request_id, source_url) in enumerate(json_requests):
            try:
                payload = get_body("Network.getResponseBody", {"requestId": request_id})
            except Exception:  # noqa: BLE001 - body collection is optional and fail-closed
                continue
            text = payload.get("body") if isinstance(payload, dict) else ""
            if not isinstance(text, str) or len(text.encode("utf-8", "ignore")) > MAX_JSON_BYTES:
                if "network_json_limit" not in warnings:
                    warnings.append("network_json_limit")
                continue
            for page_index, url in enumerate(extract_manifest_urls_from_text(text, page_url=source_url)):
                if len(json_candidates) >= MAX_NETWORK_CANDIDATES:
                    if "network_resource_limit" not in warnings:
                        warnings.append("network_resource_limit")
                    break
                json_candidates.append({
                    "url": url, "source": "network_json_manifest",
                    "order": 400000 + request_index * MAX_CANDIDATES + page_index,
                    "origin": "network_json",
                })
    return {"candidates": candidates[:MAX_NETWORK_CANDIDATES],
            "json_candidates": json_candidates[:MAX_NETWORK_CANDIDATES],
            "metadata": metadata[:MAX_NETWORK_METADATA], "warnings": warnings}


def collect_from_driver(driver: Any, page_url: str) -> dict[str, Any]:
    """Collect bounded DOM, performance/CDP and recognised JSON evidence from one reader."""
    payload = driver.execute_script(_COLLECTION_SCRIPT) or {}
    if not isinstance(payload, dict):
        payload = {}
    dom = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    resources = payload.get("resources") if isinstance(payload.get("resources"), list) else []
    cdp = _collect_cdp_network(driver, page_url)
    network = list(resources[:MAX_NETWORK_CANDIDATES]) + list(cdp["candidates"])
    if len(network) > MAX_NETWORK_CANDIDATES:
        network = network[:MAX_NETWORK_CANDIDATES]
    raw = list(dom[:MAX_DOM_CANDIDATES]) + network
    json_candidates: list[dict[str, Any]] = []
    json_blocks = payload.get("json") if isinstance(payload.get("json"), list) else []
    for text in json_blocks[:MAX_JSON_SCRIPTS]:
        if not isinstance(text, str) or len(text.encode("utf-8", "ignore")) > MAX_JSON_BYTES:
            continue
        for index, url in enumerate(extract_manifest_urls_from_text(text, page_url=page_url)):
            if len(json_candidates) >= MAX_NETWORK_CANDIDATES:
                break
            json_candidates.append({"url": url, "source": "json_manifest",
                                    "order": 200000 + index, "origin": "json"})
    json_candidates.extend(cdp["json_candidates"])
    raw.extend(json_candidates[:MAX_NETWORK_CANDIDATES])
    warnings = _sanitize_warnings([*(payload.get("warnings") or []), *cdp["warnings"]])
    if len(dom) > MAX_DOM_CANDIDATES and "dom_scan_limit" not in warnings:
        warnings.append("dom_scan_limit")
    if (len(resources) > MAX_NETWORK_CANDIDATES
            or len(cdp["candidates"]) >= MAX_NETWORK_CANDIDATES) and "network_resource_limit" not in warnings:
        warnings.append("network_resource_limit")
    return {
        "candidates": raw[:MAX_CANDIDATES],
        "dom_candidates": list(dom[:MAX_DOM_CANDIDATES]),
        "network_candidates": network,
        "json_candidates": json_candidates[:MAX_NETWORK_CANDIDATES],
        "network_metadata": cdp["metadata"],
        "page_text": str(payload.get("pageText") or ""),
        "warnings": warnings,
        "canvas_detected": _number(payload.get("canvasDetected")),
        "canvas_captured": _number(payload.get("canvasCaptured")),
    }


def analyse_driver(
    driver: Any,
    page_url: str,
    adapter: Any,
    *,
    profile: dict[str, Any] | None = None,
    extra_warnings: Iterable[str] = (),
) -> SourceAnalysis:
    """Observe a loaded reader and produce the same pure analysis used by fixtures."""
    final_url = str(getattr(driver, "current_url", "") or page_url)
    adapter.validate_redirect(final_url)
    collected = collect_from_driver(driver, final_url)
    return analyse_candidates(final_url, collected["candidates"], adapter=adapter,
                              final_url=final_url, page_text=collected["page_text"],
                              warnings=[*collected["warnings"], *list(extra_warnings)],
                              canvas_detected=collected["canvas_detected"],
                              canvas_captured=collected["canvas_captured"], profile=profile,
                              network_metadata=collected["network_metadata"])
