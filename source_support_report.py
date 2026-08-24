"""Sanitized source-compatibility support reports.

The reporter contract is metadata-only. It records that a user explicitly asked to send an
unsupported source for future adapter review; it never includes cookies, tokens, browser
storage, downloaded pages or chapter image URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from app_version import PRODUCT_VERSION


SECRET_MARKERS = (
    "authorization",
    "bearer",
    "cookie",
    "jwt",
    "refresh_token",
    "access_token",
    "password",
    "localstorage",
    "sessionstorage",
)


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    for marker in SECRET_MARKERS:
        if marker in text.casefold():
            return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text)[:limit]


def normalize_report_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.hostname or ""
    if not host:
        return ""
    netloc = host.casefold()
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.casefold(), netloc, path, "", parsed.query, ""))


@dataclass(frozen=True)
class SourceSupportReport:
    url: str
    domain: str
    detected_adapter: str
    failure_reason_code: str
    application_version: str
    timestamp: str
    user_note: str = ""

    def public(self) -> dict[str, str]:
        return {
            "url": self.url,
            "domain": self.domain,
            "detected_adapter": self.detected_adapter,
            "failure_reason_code": self.failure_reason_code,
            "application_version": self.application_version,
            "timestamp": self.timestamp,
            "user_note": self.user_note,
        }


def build_source_support_report(payload: dict[str, Any]) -> SourceSupportReport:
    url = normalize_report_url(str(payload.get("url") or ""))
    if not url:
        raise ValueError("invalid_url")
    parsed = urlparse(url)
    reason = _safe_text(payload.get("failure_reason_code") or payload.get("reason_code"),
                        limit=80)
    if not reason:
        raise ValueError("failure_reason_required")
    return SourceSupportReport(
        url=url,
        domain=(parsed.hostname or "").casefold(),
        detected_adapter=_safe_text(payload.get("detected_adapter"), limit=80),
        failure_reason_code=reason,
        application_version=_safe_text(
            payload.get("application_version") or PRODUCT_VERSION, limit=80),
        timestamp=_safe_text(payload.get("timestamp"), limit=80)
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        user_note=_safe_text(payload.get("user_note"), limit=500),
    )


@dataclass
class SourceSupportReportResult:
    queued: bool
    duplicate: bool
    report_id: str
    remote_delivery: str = "local_outbox"

    def public(self) -> dict[str, Any]:
        return {
            "ok": True,
            "queued": self.queued,
            "duplicate": self.duplicate,
            "report_id": self.report_id,
            "remote_delivery": self.remote_delivery,
        }


class SourceSupportReporter:
    def submit(self, report: SourceSupportReport) -> SourceSupportReportResult:
        raise NotImplementedError


@dataclass
class InMemorySourceSupportReporter(SourceSupportReporter):
    reports: dict[str, SourceSupportReport] = field(default_factory=dict)

    def submit(self, report: SourceSupportReport) -> SourceSupportReportResult:
        key = report_key(report)
        duplicate = key in self.reports
        self.reports.setdefault(key, report)
        return SourceSupportReportResult(
            queued=True, duplicate=duplicate, report_id=key, remote_delivery="memory")


class LocalOutboxSourceSupportReporter(SourceSupportReporter):
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def submit(self, report: SourceSupportReport) -> SourceSupportReportResult:
        key = report_key(report)
        existing = self._existing_keys()
        duplicate = key in existing
        if not duplicate:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"report_id": key, **report.public()},
                    ensure_ascii=False, sort_keys=True) + "\n")
        return SourceSupportReportResult(
            queued=True, duplicate=duplicate, report_id=key, remote_delivery="local_outbox")

    def _existing_keys(self) -> set[str]:
        if not self.path.exists():
            return set()
        result: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            report_id = str(item.get("report_id") or "")
            if report_id:
                result.add(report_id)
        return result


def report_key(report: SourceSupportReport) -> str:
    return "sr_" + hashlib.sha256(
        f"{report.url}\0{report.failure_reason_code}".encode("utf-8")
    ).hexdigest()[:32]
