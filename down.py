import html
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from PIL import Image, ImageStat
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import (
    CHROMEDRIVER_PATH,
    MAX_RETRIES_DOWNLOAD,
    SELENIUM_CLEANUP_TIMEOUT_SECONDS,
    SELENIUM_QUIT_TIMEOUT_SECONDS,
    TEMP_FOLDER,
)
from download_transport import preflight_browser_navigation
from pipeline_cache import atomic_write_json

try:
    import psutil
except ImportError:  # pragma: no cover - safe degraded path.
    psutil = None


MIN_IMAGE_WIDTH = 480
MIN_IMAGE_HEIGHT = 220
MIN_IMAGE_AREA = 180_000
MIN_CHAPTER_IMAGE_HEIGHT = 1
MIN_CHAPTER_IMAGE_AREA = MIN_IMAGE_WIDTH

# Generic pagination is deliberately a narrow, opt-in-by-evidence collector.  It does not
# click arbitrary UI controls: only a same-origin URL whose *only* changing query parameter
# is a conventional numeric page index can be followed.  These limits apply in addition to
# Selenium's page/script timeouts and the global image/page budgets.
MAX_PAGINATED_READER_FOLLOWS = 24
MAX_PAGINATED_READER_SECONDS = 45.0
MAX_PAGINATED_READER_SCROLL_ROUNDS = 18
PAGINATED_READER_SETTLE_SECONDS = 0.35
_PAGINATION_QUERY_KEYS = frozenset({
    "page", "p", "pg", "page_no", "page_num", "page_number", "pageindex", "page_index",
})


def force_remove(path):
    if not os.path.exists(path):
        return

    def remove_readonly(func, readonly_path, _):
        os.chmod(readonly_path, stat.S_IWRITE)
        func(readonly_path)

    shutil.rmtree(path, onerror=remove_readonly)


def download_images(
    url,
    progress_callback=None,
    max_retries=None,
    max_images=None,
    debug_folder=None,
    target_folder=None,
    force=True,
    approved_candidate_ids=None,
):
    from chapter_source import SourceError, select_adapter

    max_retries = max_retries or MAX_RETRIES_DOWNLOAD
    target_folder = target_folder or TEMP_FOLDER
    total_started = time.perf_counter()

    report = {
        "url": _sanitized_url(url),
        # Remote runs always declare their provenance explicitly.  Local-folder input
        # materialises its own report and never passes through this browser downloader.
        "source_type": "url",
        "total_dom_images": 0,
        "total_candidates": 0,
        "total_unique_urls": 0,
        "total_downloaded": 0,
        "total_ignored": 0,
        "requested_max_images": max_images,
        "collection_strategy": "incremental_scroll",
        "adapter": "",
        "adapter_name": "",
        "adapter_version": "",
        # Only the configured transport names are retained.  Cookie-bearing sessions,
        # request URLs and headers remain in memory and are never report metadata.
        "transport_metadata": {"configured": [], "count": 0},
        # This is updated only from accepted image downloads.  It is deliberately
        # distinct from ``transport_metadata.configured``: a configured browser
        # fallback is not evidence that the fallback actually fetched a page.
        "transport_name": "none",
        "transport_usage": {"successful": [], "counts": {}, "count": 0},
        "pagination": {
            "status": "not_attempted",
            "pages_observed": 1,
            "followed_pages": 0,
            "reason": "",
        },
        "viewer_image_count": 0,
        "viewer_unique_urls": 0,
        "ignored": [],
        "downloaded": [],
        "target_folder": os.path.abspath(target_folder),
        "timings": {
            "collection_seconds": 0.0,
            "download_seconds": 0.0,
            "validation_seconds": 0.0,
            "image_save_seconds": 0.0,
            "total_seconds": 0.0,
        },
    }

    driver = None
    ownership = {}
    transports = []
    try:
        # Fail-closed source selection. Invalid URLs and preflight failures are recorded as
        # coded download outcomes when this is the runner's fresh re-analysis.
        adapter = select_adapter(url)
        adapter.validate_url(url)
        adapter.validate_path(url)
        url = adapter.normalize_url(url)
        report["url"] = _sanitized_url(url)
        report["adapter"] = _safe_report_metadata(getattr(adapter, "name", ""), "unknown")
        # Keep the legacy report key for existing consumers and the explicit provenance key
        # consumed by the output manifest. Both are safe adapter identifiers only.
        report["adapter_name"] = report["adapter"]
        report["adapter_version"] = _safe_report_metadata(
            getattr(adapter, "adapter_version", ""), "unknown")
        # Selenium would otherwise follow the chain before exposing ``current_url``. Resolve
        # and validate every top-level redirect with a bounded, cookie-free request first.
        navigation_url = preflight_browser_navigation(adapter, url)
        driver = _create_driver()
        ownership = _capture_driver_ownership(driver)
        collection_started = time.perf_counter()
        _set_browser_timeouts(driver)
        driver.get(navigation_url)
        time.sleep(4)
        current_url = getattr(driver, "current_url", "")
        if not isinstance(current_url, str) or not current_url.startswith(("http://", "https://")):
            raise SourceError("unsupported_source", "invalid_browser_final_url")
        # Selenium follows the navigation itself; validate its final public destination before
        # looking at any reader content. Universal adapters scope it to this in-memory run.
        adapter.validate_redirect(current_url)
        viewer_snapshot = _viewer_image_snapshot(driver, adapter)
        report["viewer_image_count"] = viewer_snapshot["image_count"]
        report["viewer_unique_urls"] = len(viewer_snapshot["urls"])
        report["viewer_urls"] = [_sanitized_url(value) for value in viewer_snapshot["urls"]]
        report["viewer_manifest_complete"] = bool(viewer_snapshot["complete_manifest"])
        scroll_diagnostics = _scroll_incrementally(driver)
        report["scroll_diagnostics"] = scroll_diagnostics
        viewer_snapshot_after_scroll = _viewer_image_snapshot(driver, adapter)
        report["viewer_image_count_after_scroll"] = viewer_snapshot_after_scroll["image_count"]
        report["viewer_unique_urls_after_scroll"] = len(viewer_snapshot_after_scroll["urls"])
        report["lazy_loading_fully_loaded"] = bool(
            scroll_diagnostics.get("reached_document_end")
            and viewer_snapshot_after_scroll["complete_manifest"]
            and len(viewer_snapshot_after_scroll["urls"]) >= len(viewer_snapshot["urls"])
        )
        if len(viewer_snapshot_after_scroll["urls"]) > len(viewer_snapshot["urls"]):
            viewer_snapshot = viewer_snapshot_after_scroll
            report["viewer_image_count"] = viewer_snapshot["image_count"]
            report["viewer_unique_urls"] = len(viewer_snapshot["urls"])
            report["viewer_urls"] = [_sanitized_url(value) for value in viewer_snapshot["urls"]]
        if viewer_snapshot["complete_manifest"]:
            report["collection_strategy"] = "direct_viewer_manifest"
        # Every adapter, including a registered specific reader, owns the same analysis
        # contract.  This prevents an older DOM-only shortcut from bypassing coverage limits,
        # network/JSON evidence, candidate IDs or the accepted-reader manifest.
        source_warnings = _scroll_coverage_warnings(scroll_diagnostics, adapter)
        source_analysis = adapter.analyze(
            {
                "driver": driver,
                "page_url": current_url,
                "lazy_slot_resolver": _webtoons_lazy_resolver(
                    cancel_check=None,
                    on_progress=None,
                ),
            },
            profile=_load_source_profile(current_url, adapter),
            extra_warnings=source_warnings)
        source_analysis, pagination_diagnostics = _maybe_collect_paginated_reader(
            driver,
            adapter,
            source_analysis,
            page_url=current_url,
            profile=_load_source_profile(current_url, adapter),
        )
        report["pagination"] = pagination_diagnostics
        if pagination_diagnostics.get("followed_pages"):
            # BrowserSessionTransport must retain the browser's final same-origin reader
            # context.  It is never persisted; reports expose only the safe counters above.
            current_url = str(getattr(driver, "current_url", "") or current_url)
            report["collection_strategy"] = "adapter_accepted_paginated_manifest"
        report["source_analysis"] = _public_source_analysis(source_analysis)
        report["source_outcome"] = _safe_report_metadata(
            getattr(source_analysis, "outcome", ""), "source_not_ready")
        report["source_confidence"] = _safe_float(getattr(source_analysis, "confidence", 0.0))
        _fail_open_coverage_guard(source_analysis, source_warnings)
        observed_candidates = list(getattr(source_analysis, "accepted", []) or [])
        observed_candidate_count = len(observed_candidates)
        selected_candidates = _selected_source_candidates(
            source_analysis, approved_candidate_ids)
        selected_before_max_images = len(selected_candidates)
        if max_images:
            selected_candidates = selected_candidates[:int(max_images)]
        candidates = [_accepted_candidate_to_download(candidate) for candidate in selected_candidates]
        if approved_candidate_ids:
            # A reviewer's submitted opaque-id order is an intentional page order, not a
            # set.  Preserve it through the legacy downloader shape without exposing URLs.
            for selected_order, candidate in enumerate(candidates, start=1):
                candidate["order"] = selected_order
        selected_ids = [item["candidate_id"] for item in candidates]
        report["source_selection"] = {
            "candidate_ids": selected_ids,
            "automatic": not bool(approved_candidate_ids),
            "observed_candidate_count": observed_candidate_count,
            "confirmed_candidate_count": selected_before_max_images,
            "manual_subset": bool(
                approved_candidate_ids
                and selected_before_max_images < observed_candidate_count
            ),
            "fresh_candidate_count": len(selected_ids),
        }
        # Completeness is measured against the adapter's accepted reader manifest, never all
        # images exposed by a page.  IDs remain opaque even when two signed URLs share a path.
        report["expected_chapter_candidate_ids"] = selected_ids
        report["expected_chapter_urls"] = [_sanitized_url(item["url"]) for item in candidates]
        if not pagination_diagnostics.get("followed_pages"):
            report["collection_strategy"] = "adapter_accepted_manifest"
        report["timings"]["collection_seconds"] = (
            time.perf_counter() - collection_started
        )
        report["total_dom_images"] = sum(1 for item in candidates if item.get("tag") == "img")
        report["total_candidates"] = len(candidates)
        unique_candidates = _dedupe_candidates(candidates)
        report["total_unique_urls"] = len(unique_candidates)
        if force and os.path.exists(target_folder):
            force_remove(target_folder)
        os.makedirs(target_folder, exist_ok=True)
        if debug_folder:
            os.makedirs(debug_folder, exist_ok=True)

        from download_transport import build_transports

        # One transport set per chapter: file count, byte and duration budgets cannot reset
        # for every page, and BrowserSessionTransport is the only browser-backed fallback.
        transports = build_transports(adapter, driver=driver, page_url=current_url)
        report["transport_metadata"] = _transport_report_metadata(transports)
        paths = _download_candidates(
            driver,
            unique_candidates,
            progress_callback,
            max_retries,
            max_images,
            debug_folder,
            report,
            referer=current_url,
            target_folder=target_folder,
            transports=transports,
        )
        if not report.get("download_valid"):
            raise SourceError("incomplete_download", "download_gate")
        report["teardown"] = _pending_teardown_diagnostic(ownership)
        report["timings"]["total_seconds"] = time.perf_counter() - total_started
        if debug_folder:
            _persist_download_metadata(debug_folder, report)
        return paths
    except SourceError as exc:
        # The job runner consumes this code to distinguish a controlled source/download
        # outcome from a generic pipeline crash. Deliberately omit ``detail``: it may have
        # come from a server or browser and is not an output-side diagnostic contract.
        report["failure"] = {"code": str(exc.code)}
        raise
    except Exception:
        # Browser/driver faults have no stable external detail, but they still need a
        # terminal, sanitised reason for the runner and UI rather than a stuck job.
        report["failure"] = {"code": "source_not_ready"}
        raise
    finally:
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass
        if driver is not None:
            _refresh_driver_ownership(ownership)
            report["teardown"] = _bounded_driver_teardown(driver, ownership)
        else:
            report["teardown"] = {
                "status": "not_started", "timeout_occurred": False,
                "fallback_status": "not_needed",
            }
        if report["teardown"].get("status") != "success":
            print(
                "Aviso: teardown Selenium concluido com diagnostico "
                f"{report['teardown'].get('status')} "
                f"(fallback={report['teardown'].get('fallback_status')}).",
                flush=True,
            )
        report["timings"]["total_seconds"] = time.perf_counter() - total_started
        if debug_folder:
            _write_download_report(debug_folder, report)


def analyze_chapter_source(url, *, cancel_check=None, on_progress=None):
    """Analyse a reader without creating output files or starting OCR.

    This is the UI preflight seam. It makes only the normal browser navigation requested by
    the user, performs the same SSRF/redirect checks as the downloader, and returns a
    sanitised ``SourceAnalysis``. No candidate is downloaded here.
    """
    from chapter_source import SourceError, select_adapter

    adapter = select_adapter(url)
    adapter.validate_url(url)
    adapter.validate_path(url)
    normalized = adapter.normalize_url(url)
    navigation_url = preflight_browser_navigation(adapter, normalized)
    if cancel_check and cancel_check():
        raise SourceError("cancelled", "before_source_analysis")
    driver = None
    ownership = {}
    try:
        driver = _create_driver()
        ownership = _capture_driver_ownership(driver)
        _set_browser_timeouts(driver)
        driver.get(navigation_url)
        if cancel_check and cancel_check():
            raise SourceError("cancelled", "during_source_analysis")
        final_url = getattr(driver, "current_url", "")
        if not isinstance(final_url, str) or not final_url.startswith(("http://", "https://")):
            raise SourceError("unsupported_source", "invalid_browser_final_url")
        # The preflight has no browser cookies. Revalidate Chrome's actual final destination
        # before scrolling or inspecting its DOM.
        adapter.validate_redirect(final_url)
        time.sleep(4)
        scroll_diagnostics = _scroll_incrementally(driver, cancel_check=cancel_check)
        if cancel_check and cancel_check():
            raise SourceError("cancelled", "during_source_analysis")
        # Registered adapters do not get a weaker completeness policy than the universal
        # fallback.  An incomplete scroll is a reviewable, terminal source outcome for every
        # source; it must never become a partial chapter merely because its host is known.
        source_warnings = _scroll_coverage_warnings(scroll_diagnostics, adapter)
        source_analysis = adapter.analyze(
            {
                "driver": driver,
                "page_url": final_url,
                "lazy_slot_resolver": _webtoons_lazy_resolver(
                    cancel_check=cancel_check,
                    on_progress=on_progress,
                ),
            },
            profile=_load_source_profile(final_url, adapter),
            extra_warnings=source_warnings)
        source_analysis, _ = _maybe_collect_paginated_reader(
            driver,
            adapter,
            source_analysis,
            page_url=final_url,
            profile=_load_source_profile(final_url, adapter),
            cancel_check=cancel_check,
        )
        # A source review may show only small data-URI previews derived from already-visible
        # DOM images. This performs no image request and failures leave the diagnosis intact.
        from universal_chapter_adapter import attach_review_thumbnails

        source_analysis = attach_review_thumbnails(driver, source_analysis)
        return source_analysis
    finally:
        if driver is not None:
            _refresh_driver_ownership(ownership)
            _bounded_driver_teardown(driver, ownership)


def _webtoons_lazy_resolver(*, cancel_check=None, on_progress=None,
                            limits=None, clock=None, sleep=None):
    """Return a context resolver for the Webtoons reader, or a no-op for other adapters."""
    def resolve(adapter, driver, page_url, collected):
        strategy = str(getattr(adapter, "collection_strategy", "") or "")
        coverage = str(getattr(adapter, "coverage_strategy", "") or "")
        if (
            str(getattr(adapter, "name", "") or "") != "webtoons"
            or strategy != "adapter_specific"
            or coverage != "reader_container"
        ):
            return collected
        counts = collected.get("slot_counts") if isinstance(collected, dict) else {}
        if not isinstance(counts, dict) or int(counts.get("pending") or 0) <= 0:
            return collected

        from chapter_source import SourceError, slot_counts
        from lazy_slot_resolver import COUNTER_STAGE, resolve_lazy_reader_slots
        from webtoons_reader_bridge import WebtoonsReaderBridge

        bridge = WebtoonsReaderBridge(
            driver,
            adapter,
            cancel_check=cancel_check,
            on_progress=on_progress,
            sleep=sleep or time.sleep,
            clock=clock or time.monotonic,
        )

        def progress(event):
            payload = dict(event or {})
            payload["stage"] = COUNTER_STAGE
            payload["counter_stage"] = COUNTER_STAGE
            payload["pending_count"] = int(payload.get("pending") or 0)
            payload["message"] = (
                f"Carregando páginas do leitor: "
                f"{int(payload.get('current') or 0)}/{int(payload.get('total') or 0)}"
            )
            if on_progress:
                on_progress(payload)

        bridge.on_progress = progress
        result = resolve_lazy_reader_slots(
            adapter=adapter,
            read_slots=bridge.read_slots,
            scroll_to=bridge.scroll_to,
            reader_bounds=bridge.reader_bounds,
            cancel_check=bridge.cancel_check,
            on_progress=progress,
            limits=limits,
            clock=bridge.clock,
            sleep=bridge.sleep,
        )
        if result.cancelled:
            raise SourceError("cancelled", "during_lazy_resolution")
        resolved_candidates = result.resolved_candidates
        warnings = list((collected or {}).get("warnings") or [])
        for warning in result.warnings:
            if warning not in warnings:
                warnings.append(warning)
        counts_after = slot_counts(result.slots)
        if counts_after["pending"] > 0 and "scroll_incomplete" not in warnings:
            warnings.append("scroll_incomplete")
        if result.cancelled and "cancelled" not in warnings:
            warnings.append("cancelled")
        payload = {
            **dict(collected or {}),
            "dom_candidates": resolved_candidates,
            "reader_slots": result.slots,
            "slot_counts": counts_after,
            "lazy_resolution": result.public(),
            "warnings": warnings,
        }
        if on_progress:
            public = result.public()
            on_progress({
                "stage": COUNTER_STAGE,
                "counter_stage": COUNTER_STAGE,
                "current": public["slots_resolved"],
                "total": public["slots_total"],
                "pending_count": public["slots_pending"],
                "rejected_count": public["slots_rejected"],
                "rounds": public["rounds"],
                "reached_reader_end": public["reached_reader_end"],
                "stabilized": public["stabilized"],
                "heartbeat_at": time.time(),
                "message": (
                    f"Carregando páginas do leitor: "
                    f"{public['slots_resolved']}/{public['slots_total']}"
                ),
            })
        return payload

    return resolve



def driver_download_allowed(env=None) -> bool:
    """Whether the official Selenium Manager may resolve a missing ChromeDriver.

    Opt-in and exact-match: only the literal "1" enables it, so a stray "true" or "0" in an
    environment file cannot silently turn a browser download on.
    """
    values = os.environ if env is None else env
    return str(values.get("TRADUTOR_ALLOW_DRIVER_DOWNLOAD", "")).strip() == "1"


def _create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--window-size=1400,2200")
    # ``collect_from_driver`` can consume Chrome's bounded performance log as additional
    # browser-observed evidence.  Old/embedded Selenium option objects need not support this,
    # so capability setup is strictly best-effort and never changes browser behaviour otherwise.
    set_capability = getattr(chrome_options, "set_capability", None)
    if callable(set_capability):
        try:
            set_capability("goog:loggingPrefs", {"performance": "ALL"})
        except Exception:
            pass

    driver_path = CHROMEDRIVER_PATH if CHROMEDRIVER_PATH and os.path.isfile(CHROMEDRIVER_PATH) else (
        shutil.which("chromedriver") or shutil.which("chromedriver.exe")
    )
    if driver_path:
        return webdriver.Chrome(service=Service(driver_path), options=chrome_options)
    if driver_download_allowed():
        # Opt-in only. Selenium Manager is the official resolver bundled with Selenium; it
        # fetches from Chrome for Testing and caches under Selenium's own directory. Left
        # implicit this would give source analysis an unrelated network side effect, which
        # is why the default below still fails closed.
        return webdriver.Chrome(service=Service(), options=chrome_options)
    raise RuntimeError(
        "ChromeDriver local indisponivel. Defina CHROMEDRIVER_PATH, instale-o no PATH, "
        "ou habilite explicitamente TRADUTOR_ALLOW_DRIVER_DOWNLOAD=1 para permitir que o "
        "Selenium Manager oficial resolva o driver."
    )


def _set_browser_timeouts(driver, timeout_seconds=45):
    """Bound browser calls without requiring a Selenium-specific driver in unit tests."""
    for method_name in ("set_page_load_timeout", "set_script_timeout"):
        method = getattr(driver, method_name, None)
        if callable(method):
            try:
                method(timeout_seconds)
            except Exception:
                pass


def _load_source_profile(url, adapter):
    """Return an exact-host hint only for the generic fallback; profiles grant no access."""
    if getattr(adapter, "is_specific", False):
        return None
    try:
        from source_profile import SourceProfileStore

        return SourceProfileStore().load(urlparse(str(url or "")).hostname or "")
    except Exception:
        return None


_SAFE_REPORT_METADATA_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")


def _safe_report_metadata(value, fallback=""):
    """Keep report labels bounded and free of page-controlled strings."""
    text = str(value or "").strip()
    return text if _SAFE_REPORT_METADATA_RE.fullmatch(text) else fallback


def _safe_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _safe_nonnegative_int(value):
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _scroll_coverage_warnings(scroll_diagnostics, adapter=None):
    """Downloader-owned coverage warning, using the strategy the adapter declares.

    The document-end rule is right for an unknown page: anything below the last observed
    image could still be chapter content, so stopping early must fail closed.

    It is wrong for a registered reader. Such a page ends its reader and then continues with
    recommendations and a footer, so the document end sits well past the last page and is
    never reached — and a reader delivered fully loaded shows no growth at all. Both are
    normal, and judging them by document end reported a complete chapter as truncated.

    For "reader_container", stabilisation is the evidence: the collector watched the reader
    stop changing for the required number of rounds. A run that merely exhausted its round
    or time budget while still changing is not stabilised, so it still warns.
    """
    diagnostics = scroll_diagnostics if isinstance(scroll_diagnostics, dict) else {}
    stabilized = bool(diagnostics.get("stabilized"))
    strategy = str(getattr(adapter, "coverage_strategy", "generic_document") or "generic_document")
    if strategy == "reader_container":
        return () if stabilized else ("scroll_incomplete",)
    if diagnostics.get("reached_document_end") and stabilized:
        return ()
    return ("scroll_incomplete",)


def _source_analysis_warnings(source_analysis):
    values = getattr(source_analysis, "warnings", ()) or ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    return {str(value or "").strip() for value in values if str(value or "").strip()}


def _automatic_page_limit():
    """Use the classifier's shared ceiling without duplicating the policy constant."""
    try:
        from universal_chapter_adapter import MAX_AUTOMATIC_PAGES

        return max(1, int(MAX_AUTOMATIC_PAGES))
    except (ImportError, TypeError, ValueError):  # pragma: no cover - defensive import seam
        return 400


def _coverage_failure_reason(source_analysis, scroll_warnings=()):
    """Detect incomplete coverage even when a custom adapter accidentally says usable."""
    if "scroll_incomplete" in set(scroll_warnings or ()):
        return "scroll_incomplete"
    warnings = _source_analysis_warnings(source_analysis)
    for reason in (
        "page_limit_exceeded",
        "scroll_incomplete",
        "pagination_incomplete",
        "lazy_resolution_timeout",
        "lazy_resolution_max_rounds",
        "reader_dom_changed",
    ):
        if reason in warnings:
            return reason
    accepted = list(getattr(source_analysis, "accepted", []) or [])
    if len(accepted) > _automatic_page_limit():
        return "page_limit_exceeded"
    return ""


def _analysis_can_download(source_analysis):
    return bool(getattr(source_analysis, "can_download", False))


def _fail_open_coverage_guard(source_analysis, scroll_warnings=()):
    """Stop automatic execution when a usable analysis conflicts with incomplete coverage.

    Pre-analysis intentionally returns a non-usable diagnostic so the UI can persist a clear
    terminal reason.  The later selection gate rejects the same evidence unconditionally,
    including manual-review submissions, so returning the diagnostic here cannot permit a
    partial download.
    """
    reason = _coverage_failure_reason(source_analysis, scroll_warnings)
    if reason and _analysis_can_download(source_analysis):
        from chapter_source import INCOMPLETE_DOWNLOAD, SourceError

        raise SourceError(INCOMPLETE_DOWNLOAD, reason)


def _public_source_analysis(source_analysis):
    """Use adapter diagnostics when available, with a deliberately small safe fallback."""
    public = getattr(source_analysis, "public", None)
    if callable(public):
        value = public()
        if isinstance(value, dict):
            return value
    accepted = list(getattr(source_analysis, "accepted", []) or [])
    return {
        "adapter": _safe_report_metadata(getattr(source_analysis, "adapter", ""), "unknown"),
        "outcome": _safe_report_metadata(getattr(source_analysis, "outcome", ""), "source_not_ready"),
        "confidence": _safe_float(getattr(source_analysis, "confidence", 0.0)),
        "accepted_count": len(accepted),
        "warnings": sorted(
            _safe_report_metadata(value, "collector_warning")
            for value in _source_analysis_warnings(source_analysis)
        )[:32],
    }


# This script never calls ``click`` nor evaluates a page-provided handler. It inspects explicit
# anchors plus visible next-like buttons only to detect incomplete coverage: a button without a
# verifiable href is never activated, but it cannot make the initial surface look complete.
_PAGINATION_CONTROL_SCRIPT = r"""
const controls = [];
const nextLabel = /^(?:next|next page|page next|›|»|→)$/i;
const numericPage = /^(?:page\s*)?[1-9][0-9]{0,4}$/i;
for (const control of document.querySelectorAll('a[href], button, [role="button"]')) {
  const isAnchor = control.tagName === 'A' && control.hasAttribute('href');
  const rel = (control.getAttribute('rel') || '').toLowerCase().split(/\s+/);
  const label = [
    control.getAttribute('aria-label') || '',
    control.getAttribute('title') || '',
    control.textContent || '',
  ].join(' ').replace(/\s+/g, ' ').trim();
  const relNext = rel.includes('next');
  const labelledNext = nextLabel.test(label);
  const numbered = numericPage.test(label);
  if (!relNext && !labelledNext && !numbered) continue;
  const style = getComputedStyle(control);
  const rect = control.getBoundingClientRect();
  controls.push({
    href: isAnchor ? (control.href || '') : '',
    interactive: !isAnchor,
    relNext,
    labelledNext,
    numbered,
    visible: Boolean(rect.width > 0 && rect.height > 0 && style.display !== 'none'
      && style.visibility !== 'hidden'),
    disabled: Boolean(control.hasAttribute('disabled')
      || control.getAttribute('aria-disabled') === 'true'),
    target: control.getAttribute('target') || '',
    download: control.hasAttribute('download'),
  });
  if (controls.length >= 32) break;
}
return controls;
"""


def _pagination_origin_key(url):
    """Return the browser-origin tuple, or ``None`` for an unsafe control URL."""
    try:
        parsed = urlparse(str(url or ""))
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, int(port)


def _canonical_pagination_url(url):
    """Normalize a safe reader URL only for in-memory cycle detection."""
    origin = _pagination_origin_key(url)
    if origin is None:
        return ""
    try:
        parsed = urlparse(str(url or ""))
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return ""
    scheme, host, port = origin
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    return urlunparse((
        scheme,
        netloc,
        parsed.path or "/",
        parsed.params,
        urlencode(sorted(pairs)),
        "",
    ))


def _pagination_query_parts(url):
    """Return stable query evidence and one bounded numeric page index, if present."""
    try:
        parsed = urlparse(str(url or ""))
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError):
        return None
    page_pairs = [
        (key.casefold(), value)
        for key, value in pairs
        if key.casefold() in _PAGINATION_QUERY_KEYS
    ]
    if len(page_pairs) > 1:
        return None
    stable = tuple(sorted(
        (key, value) for key, value in pairs
        if key.casefold() not in _PAGINATION_QUERY_KEYS
    ))
    if not page_pairs:
        return parsed, stable, "", 1
    key, raw_page = page_pairs[0]
    if not raw_page.isascii() or not raw_page.isdigit():
        return None
    page_number = int(raw_page)
    if not 1 <= page_number <= 10_000:
        return None
    return parsed, stable, key, page_number


def _same_chapter_pagination_target(current_url, candidate_url):
    """Return a canonical target only for an unequivocal next page of this chapter.

    Generic readers have no trustworthy site-specific chapter grammar.  Therefore the
    accepted shape is deliberately narrow: identical browser origin and path, identical
    non-page query pairs, and exactly the next value of one conventional numeric page key.
    A ``next chapter`` link necessarily fails this proof and is never visited.
    """
    current_origin = _pagination_origin_key(current_url)
    target_origin = _pagination_origin_key(candidate_url)
    current = _pagination_query_parts(current_url)
    target = _pagination_query_parts(candidate_url)
    if not current_origin or current_origin != target_origin or not current or not target:
        return ""
    current_parsed, current_stable, current_key, current_page = current
    target_parsed, target_stable, target_key, target_page = target
    if current_parsed.path != target_parsed.path or current_parsed.params != target_parsed.params:
        return ""
    if current_stable != target_stable or not target_key:
        return ""
    if current_key and current_key != target_key:
        return ""
    if target_page != current_page + 1:
        return ""
    return _canonical_pagination_url(candidate_url)


def _looks_like_unverified_pagination_control(current_url, candidate_url):
    """Whether a page-shaped, same-origin control was present but could not be proven safe."""
    if _pagination_origin_key(current_url) != _pagination_origin_key(candidate_url):
        return False
    target = _pagination_query_parts(candidate_url)
    return bool(target and target[2])


def _same_chapter_numbered_page_control(current_url, candidate_url):
    """Whether a non-next numeric control belongs to this verified page set.

    A normal pager exposes links for later page numbers alongside the immediate next page.
    Those links are not ambiguity by themselves; only the immediate successor is followed.
    """
    if _pagination_origin_key(current_url) != _pagination_origin_key(candidate_url):
        return False
    current = _pagination_query_parts(current_url)
    target = _pagination_query_parts(candidate_url)
    if not current or not target:
        return False
    current_parsed, current_stable, current_key, _ = current
    target_parsed, target_stable, target_key, _ = target
    return bool(
        target_key
        and current_parsed.path == target_parsed.path
        and current_parsed.params == target_parsed.params
        and current_stable == target_stable
        and (not current_key or current_key == target_key)
    )


def _read_pagination_controls(driver):
    """Read a bounded, inert snapshot of explicit controls; never execute a page action."""
    try:
        raw_controls = driver.execute_script(_PAGINATION_CONTROL_SCRIPT)
    except Exception:
        return []
    if not isinstance(raw_controls, (list, tuple)):
        return []
    controls = []
    for raw in raw_controls[:32]:
        if not isinstance(raw, dict):
            continue
        href = str(raw.get("href") or "").strip()
        interactive = bool(raw.get("interactive"))
        if (not href and not interactive) or len(href) > 4096:
            continue
        controls.append({
            "href": href,
            "interactive": interactive,
            "rel_next": bool(raw.get("relNext")),
            "labelled_next": bool(raw.get("labelledNext")),
            "numbered": bool(raw.get("numbered")),
            "visible": bool(raw.get("visible")),
            "disabled": bool(raw.get("disabled")),
            "target": str(raw.get("target") or ""),
            "download": bool(raw.get("download")),
        })
    return controls


def _select_safe_pagination_target(current_url, controls):
    """Choose one non-ambiguous next page, or return a coded no-navigation decision."""
    safe_targets = {}
    unverified = False
    for control in controls or ():
        if not isinstance(control, dict):
            continue
        if (not control.get("visible") or control.get("disabled") or control.get("target")
                or control.get("download")):
            continue
        candidate_url = str(control.get("href") or "")
        if not candidate_url:
            # A visible next/page button can indicate another reader surface, but it has no
            # destination whose origin and chapter identity we can prove. Never click it;
            # fail closed instead of reporting a potentially partial surface as complete.
            if control.get("rel_next") or control.get("labelled_next") or control.get("numbered"):
                unverified = True
            continue
        canonical = _same_chapter_pagination_target(current_url, candidate_url)
        if canonical:
            safe_targets.setdefault(canonical, candidate_url)
        elif (control.get("numbered") and not control.get("rel_next")
              and not control.get("labelled_next")
              and _same_chapter_numbered_page_control(current_url, candidate_url)):
            # Keep a conventional [1] [2] [3] pager deterministic: only page N+1 is
            # navigated, while the other same-chapter numeric links are inert evidence.
            continue
        elif (_looks_like_unverified_pagination_control(current_url, candidate_url)
              or control.get("rel_next") or control.get("labelled_next")
              or control.get("numbered")):
            # A visible next-like control that cannot prove the same chapter is evidence of
            # an uncollected surface. A next-chapter link is never followed and cannot be
            # misreported as a completed one-page reader.
            unverified = True
    if len(safe_targets) == 1:
        canonical, target = next(iter(safe_targets.items()))
        return "safe", target, canonical
    if len(safe_targets) > 1 or unverified:
        return "unverified", "", ""
    return "none", "", ""


def _pagination_analysis_eligible(source_analysis):
    """Only build a multi-page manifest from accepted adapter-owned page evidence."""
    if not list(getattr(source_analysis, "accepted", []) or []):
        return False
    if _analysis_can_download(source_analysis):
        return True
    try:
        from chapter_source import REVIEW_REQUIRED_MEDIUM_CONFIDENCE

        return str(getattr(source_analysis, "outcome", "") or "") == REVIEW_REQUIRED_MEDIUM_CONFIDENCE
    except ImportError:  # pragma: no cover - chapter_source is always available at runtime
        return False


def _mark_pagination_incomplete(source_analysis):
    """Attach the one public coverage warning without exposing a page-controlled target."""
    warnings = list(getattr(source_analysis, "warnings", ()) or ())
    if "pagination_incomplete" not in warnings:
        warnings.append("pagination_incomplete")
    try:
        source_analysis.warnings = warnings
    except Exception:
        # The downloader still gets a fail-closed diagnostic from its local return value. A
        # non-standard immutable adapter result cannot be rewritten safely.
        pass
    return source_analysis


def _pagination_candidate_raw(candidate, page_index, order):
    """Rebuild minimal in-memory evidence for the aggregate adapter analysis."""
    y = _safe_nonnegative_int(_accepted_value(candidate, "y", 0))
    return {
        "url": str(_accepted_value(candidate, "url", "") or ""),
        "source": str(_accepted_value(candidate, "source", "adapter_candidate") or "adapter_candidate"),
        "order": int(order),
        # Page-local Y positions must not interleave adjacent paginated views.
        "y": page_index * 10_000_000 + y,
        "width": _safe_nonnegative_int(_accepted_value(candidate, "width", 0)),
        "height": _safe_nonnegative_int(_accepted_value(candidate, "height", 0)),
        "naturalWidth": _safe_nonnegative_int(_accepted_value(candidate, "natural_width", 0)),
        "naturalHeight": _safe_nonnegative_int(_accepted_value(candidate, "natural_height", 0)),
        "container": str(_accepted_value(candidate, "container", "") or ""),
        "className": str(_accepted_value(candidate, "class_name", "") or ""),
        "id": str(_accepted_value(candidate, "element_id", "") or ""),
        "alt": str(_accepted_value(candidate, "alt", "") or ""),
        "context": str(_accepted_value(candidate, "context", "") or ""),
        "network_order": _safe_nonnegative_int(_accepted_value(candidate, "network_order", 0)),
        "contentType": str(_accepted_value(candidate, "content_type", "") or ""),
        "origin": str(_accepted_value(candidate, "origin", "dom") or "dom"),
        "visible": bool(_accepted_value(candidate, "visible", True)),
        "attributeNames": list(_accepted_value(candidate, "attribute_names", ()) or ()),
        "canvas_data": _accepted_value(candidate, "canvas_data", b""),
    }


def _aggregate_paginated_analyses(page_url, analyses, adapter, profile=None):
    """Re-score only accepted page manifests into one complete adapter-owned selection."""
    from universal_chapter_adapter import analyse_candidates

    raw_candidates = []
    seen_ids = set()
    for page_index, analysis in enumerate(analyses):
        for candidate in list(getattr(analysis, "accepted", []) or []):
            candidate_id = _accepted_candidate_id(candidate)
            # A canvas capture is valid within one inspected reader surface but cannot be
            # reconstructed as a page-local browser observation after navigation.  Refuse
            # the aggregate rather than silently dropping its pixels.
            if (_accepted_value(candidate, "canvas_data", b"") or not candidate_id
                    or candidate_id in seen_ids):
                return None
            seen_ids.add(candidate_id)
            raw_candidates.append(_pagination_candidate_raw(
                candidate, page_index, len(raw_candidates)))
    if not raw_candidates:
        return None
    aggregate = analyse_candidates(
        page_url,
        raw_candidates,
        adapter=adapter,
        final_url=page_url,
        profile=profile,
        cluster_score=getattr(adapter, "score_cluster", None),
    )
    aggregate.page_manifest = adapter.build_page_manifest(aggregate.accepted)
    return aggregate


def _pagination_diagnostic(status, *, pages_observed=1, followed_pages=0, reason=""):
    return {
        "status": _safe_report_metadata(status, "blocked"),
        "pages_observed": _safe_nonnegative_int(pages_observed),
        "followed_pages": _safe_nonnegative_int(followed_pages),
        "reason": _safe_report_metadata(reason, ""),
    }


def _maybe_collect_paginated_reader(
        driver,
        adapter,
        source_analysis,
        *,
        page_url,
        profile=None,
        cancel_check=None,
):
    """Collect a small generic query-paginated reader without ever clicking arbitrary UI.

    Site-specific adapters keep their own reader semantics.  The generic fallback may follow
    only a page-shaped same-origin ``href`` proven by ``_same_chapter_pagination_target``.  A
    cycle, ambiguous/unsafe page-shaped control, timeout, partial scroll, or unusable later
    page turns into ``pagination_incomplete`` so the normal coverage gate refuses a partial
    download rather than guessing.
    """
    if getattr(adapter, "is_specific", False):
        return source_analysis, _pagination_diagnostic("not_applicable")
    if _coverage_failure_reason(source_analysis):
        return source_analysis, _pagination_diagnostic("blocked", reason="initial_coverage")
    if not _pagination_analysis_eligible(source_analysis):
        return source_analysis, _pagination_diagnostic("not_applicable")

    control_state, target_url, target_key = _select_safe_pagination_target(
        page_url, _read_pagination_controls(driver))
    if control_state == "none":
        return source_analysis, _pagination_diagnostic("not_needed")
    if control_state != "safe":
        return (_mark_pagination_incomplete(source_analysis),
                _pagination_diagnostic("blocked", reason="unverified_control"))

    from chapter_source import SourceError

    started = time.monotonic()
    deadline = started + MAX_PAGINATED_READER_SECONDS
    analyses = [source_analysis]
    seen_pages = {_canonical_pagination_url(page_url)}
    followed = 0
    current_url = page_url

    def blocked(reason):
        return (_mark_pagination_incomplete(source_analysis),
                _pagination_diagnostic(
                    "blocked", pages_observed=len(analyses), followed_pages=followed, reason=reason))

    while target_url:
        if cancel_check and cancel_check():
            raise SourceError("cancelled", "during_source_analysis")
        if followed >= MAX_PAGINATED_READER_FOLLOWS:
            return blocked("page_limit")
        if time.monotonic() >= deadline:
            return blocked("time_limit")
        if not target_key or target_key in seen_pages:
            return blocked("cycle")
        try:
            adapter.validate_navigation_url(target_url)
            adapter.validate_path(target_url)
            # ``get`` is intentional: it avoids executing a page-controlled click handler.
            driver.get(target_url)
            followed += 1
            time.sleep(PAGINATED_READER_SETTLE_SECONDS)
            final_url = str(getattr(driver, "current_url", "") or "")
            if _canonical_pagination_url(final_url) != target_key:
                return blocked("redirected")
            adapter.validate_redirect(final_url)
        except SourceError as exc:
            if exc.code == "cancelled":
                raise
            return blocked("invalid_target")
        except Exception:
            return blocked("navigation_failed")

        stop_reason = {"value": ""}

        def stop_for_budget():
            if cancel_check and cancel_check():
                stop_reason["value"] = "cancelled"
                return True
            if time.monotonic() >= deadline:
                stop_reason["value"] = "time_limit"
                return True
            return False

        try:
            scroll = _scroll_incrementally(
                driver,
                max_rounds=MAX_PAGINATED_READER_SCROLL_ROUNDS,
                stable_rounds=3,
                cancel_check=stop_for_budget,
            )
        except SourceError:
            if stop_reason["value"] == "cancelled":
                raise
            return blocked(stop_reason["value"] or "scroll_failed")
        if _scroll_coverage_warnings(scroll, adapter):
            return blocked("scroll_incomplete")
        try:
            page_analysis = adapter.analyze(
                {"driver": driver, "page_url": final_url},
                profile=_load_source_profile(final_url, adapter),
                extra_warnings=(),
            )
        except SourceError as exc:
            if exc.code == "cancelled":
                raise
            return blocked("page_analysis_failed")
        except Exception:
            return blocked("page_analysis_failed")
        if _coverage_failure_reason(page_analysis) or not _pagination_analysis_eligible(page_analysis):
            return blocked("page_analysis_unusable")
        analyses.append(page_analysis)
        seen_pages.add(target_key)
        current_url = final_url
        control_state, target_url, target_key = _select_safe_pagination_target(
            current_url, _read_pagination_controls(driver))
        if control_state == "none":
            break
        if control_state != "safe":
            return blocked("unverified_control")

    aggregate = _aggregate_paginated_analyses(page_url, analyses, adapter, profile=profile)
    if aggregate is None:
        return blocked("candidate_collision")
    if _coverage_failure_reason(aggregate) or not _pagination_analysis_eligible(aggregate):
        return blocked("aggregate_unusable")
    return aggregate, _pagination_diagnostic(
        "complete", pages_observed=len(analyses), followed_pages=followed)


def _accepted_value(candidate, name, default=None):
    if isinstance(candidate, dict):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _accepted_candidate_id(candidate):
    return str(_accepted_value(candidate, "id", "") or "").strip()


def _selected_source_candidates(source_analysis, approved_candidate_ids=None):
    """Validate the fresh adapter-owned accepted set before a download begins.

    High-confidence analysis proceeds automatically. A medium-confidence result requires a
    non-empty UI-confirmed subset, which is checked against this fresh DOM snapshot. All
    other outcomes stop before a transport is constructed.
    """
    from chapter_source import (
        INCOMPLETE_DOWNLOAD,
        REVIEW_REQUIRED_MEDIUM_CONFIDENCE,
        SourceError,
    )

    selected_ids = list(dict.fromkeys(
        str(value).strip() for value in (approved_candidate_ids or []) if str(value).strip()
    ))
    outcome = str(getattr(source_analysis, "outcome", "") or "")
    accepted = list(getattr(source_analysis, "accepted", []) or [])
    coverage_reason = _coverage_failure_reason(source_analysis)
    if coverage_reason:
        raise SourceError(INCOMPLETE_DOWNLOAD, coverage_reason)
    if len(accepted) > _automatic_page_limit():
        raise SourceError(INCOMPLETE_DOWNLOAD, "page_limit_exceeded")
    if outcome == REVIEW_REQUIRED_MEDIUM_CONFIDENCE and not selected_ids:
        raise SourceError(REVIEW_REQUIRED_MEDIUM_CONFIDENCE, "source_confirmation_required")
    if not getattr(source_analysis, "can_download", False) and outcome != REVIEW_REQUIRED_MEDIUM_CONFIDENCE:
        raise SourceError(outcome or "unsupported_low_confidence", "source_analysis")
    if not accepted:
        raise SourceError(INCOMPLETE_DOWNLOAD, "no_accepted_candidates")
    accepted_ids = [_accepted_candidate_id(candidate) for candidate in accepted]
    if not all(accepted_ids) or len(set(accepted_ids)) != len(accepted_ids):
        # Approved IDs must address exactly one fresh reader page.  This applies to automatic
        # and manual paths alike, so a buggy specific adapter cannot make the gate ambiguous.
        raise SourceError(INCOMPLETE_DOWNLOAD, "ambiguous_candidate_ids")
    if not selected_ids:
        return accepted
    accepted_by_id = {candidate_id: candidate for candidate_id, candidate in
                      zip(accepted_ids, accepted)}
    if any(candidate_id not in accepted_by_id for candidate_id in selected_ids):
        raise SourceError(INCOMPLETE_DOWNLOAD, "approved_candidate_missing")
    return [accepted_by_id[candidate_id] for candidate_id in selected_ids]


def _selected_universal_candidates(source_analysis, approved_candidate_ids=None):
    """Compatibility alias for callers predating the adapter-wide analysis contract."""
    return _selected_source_candidates(source_analysis, approved_candidate_ids)


def _accepted_candidate_to_download(candidate):
    """Convert only an accepted opaque page into the legacy download input shape."""
    from chapter_source import INCOMPLETE_DOWNLOAD, SourceError

    candidate_id = _accepted_candidate_id(candidate)
    # IDs are persisted and may later be submitted by the UI, so reject a page-controlled URL
    # or whitespace-bearing identifier instead of attempting to rewrite it.
    if not _SAFE_REPORT_METADATA_RE.fullmatch(candidate_id):
        raise SourceError(INCOMPLETE_DOWNLOAD, "invalid_accepted_candidate_id")
    url = str(_accepted_value(candidate, "url", "") or "").strip()
    canvas_data = _accepted_value(candidate, "canvas_data", b"")
    has_canvas = isinstance(canvas_data, (bytes, bytearray)) and bool(canvas_data)
    scheme = urlparse(url).scheme.casefold()
    if not url or (not has_canvas and scheme not in {"http", "https"}) or (has_canvas and scheme != "canvas"):
        raise SourceError(INCOMPLETE_DOWNLOAD, "invalid_accepted_candidate")
    origin = _safe_report_metadata(_accepted_value(candidate, "origin", ""), "")
    return {
        "candidate_id": candidate_id,
        "url": url,
        "source": _safe_report_metadata(_accepted_value(candidate, "source", ""), "adapter_candidate"),
        "order": _safe_nonnegative_int(_accepted_value(candidate, "order", 0)),
        "y": _safe_nonnegative_int(_accepted_value(candidate, "y", 0)),
        "width": _safe_nonnegative_int(_accepted_value(candidate, "width", 0)),
        "height": _safe_nonnegative_int(_accepted_value(candidate, "height", 0)),
        "naturalWidth": _safe_nonnegative_int(_accepted_value(candidate, "natural_width", 0)),
        "naturalHeight": _safe_nonnegative_int(_accepted_value(candidate, "natural_height", 0)),
        "tag": "img" if origin == "dom" else "",
        "isChapterCandidate": True,
        "inContainer": bool(_accepted_value(candidate, "container", "")),
        "canvas_data": bytes(canvas_data) if has_canvas else b"",
    }


def _transport_report_metadata(transports):
    names = [
        _safe_report_metadata(getattr(transport, "name", ""), "unknown")
        for transport in (transports or [])
    ]
    return {"configured": names, "count": len(names)}


def _primary_transport_name(metadata):
    """Compatibility helper for callers that need a configured transport label.

    Run provenance must use the observed ``transport_name`` populated by
    ``_record_successful_transport`` instead.  Configuration order is not proof
    that a fallback was used.
    """
    names = (metadata or {}).get("configured") if isinstance(metadata, dict) else ()
    return _safe_report_metadata((names or ["none"])[0], "none")


def _record_successful_transport(report, name):
    """Record a safe, actually-used transport for one accepted chapter page.

    Per-page report entries retain only a fixed transport label.  They never
    include request URLs, cookies, headers or session metadata.  The run-level
    name is the sole winner when all saved pages used it, otherwise ``mixed``.
    """
    safe_name = _safe_report_metadata(name, "unknown")
    usage = report.get("transport_usage")
    if not isinstance(usage, dict):
        usage = {"successful": [], "counts": {}, "count": 0}
        report["transport_usage"] = usage
    successful = usage.get("successful")
    if not isinstance(successful, list):
        successful = []
        usage["successful"] = successful
    successful.append(safe_name)
    counts = Counter(str(value) for value in successful if str(value))
    usage["counts"] = dict(sorted(counts.items()))
    usage["count"] = len(successful)
    report["transport_name"] = (
        next(iter(counts)) if len(counts) == 1 else "mixed" if counts else "none"
    )


def _pending_teardown_diagnostic(ownership):
    return {
        "status": "pending",
        "duration_seconds": 0.0,
        "timeout_seconds": float(SELENIUM_QUIT_TIMEOUT_SECONDS),
        "timeout_occurred": False,
        "thread_daemon": True,
        "thread_alive_after_cleanup": False,
        "fallback_attempted": False,
        "fallback_status": "not_needed",
        "matched_count": 0,
        "terminated_count": 0,
        "killed_count": 0,
        "skipped_ownership_unproven_count": 0,
        "remaining_count": 0,
        "exception_type": "",
        "fallback_exception_type": "",
        "service_pid": ownership.get("service_pid"),
        "profile_detected": bool(ownership.get("profile_path")),
    }


def _bounded_driver_teardown(
    driver,
    ownership,
    timeout_seconds=None,
    cleanup_timeout_seconds=None,
    cleanup_callback=None,
):
    timeout_seconds = _finite_timeout(
        SELENIUM_QUIT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds,
        default=SELENIUM_QUIT_TIMEOUT_SECONDS,
        minimum=0.001,
        maximum=300.0,
    )
    cleanup_timeout_seconds = _finite_timeout(
        (
            SELENIUM_CLEANUP_TIMEOUT_SECONDS
            if cleanup_timeout_seconds is None
            else cleanup_timeout_seconds
        ),
        default=SELENIUM_CLEANUP_TIMEOUT_SECONDS,
        minimum=0.001,
        maximum=30.0,
    )
    cleanup_callback = cleanup_callback or _cleanup_owned_processes
    result = _pending_teardown_diagnostic(ownership)
    result["timeout_seconds"] = timeout_seconds
    started = time.perf_counter()
    completed = threading.Event()
    worker_result = {}

    def run_quit():
        try:
            driver.quit()
        except BaseException as exc:  # noqa: BLE001 - thread must always signal completion.
            worker_result["exception_type"] = type(exc).__name__
        finally:
            completed.set()

    worker = threading.Thread(
        target=run_quit,
        name="selenium-driver-quit",
        daemon=True,
    )
    worker.start()
    if completed.wait(timeout_seconds):
        result["status"] = (
            "exception" if worker_result.get("exception_type") else "success"
        )
        result["exception_type"] = worker_result.get("exception_type", "")
    else:
        result["status"] = "timeout"
        result["timeout_occurred"] = True
        result["fallback_attempted"] = True
        try:
            fallback = cleanup_callback(
                ownership,
                timeout_seconds=cleanup_timeout_seconds,
            ) or {}
            for key in (
                "matched_count",
                "terminated_count",
                "killed_count",
                "skipped_ownership_unproven_count",
                "remaining_count",
            ):
                result[key] = int(fallback.get(key, 0) or 0)
            result["fallback_status"] = str(
                fallback.get("status") or "failed"
            )
        except BaseException as exc:  # noqa: BLE001 - preserve caller on cleanup failure.
            result["fallback_status"] = "failed"
            result["fallback_exception_type"] = type(exc).__name__
        completed.wait(min(cleanup_timeout_seconds, 0.25))

    result["thread_alive_after_cleanup"] = worker.is_alive()
    result["duration_seconds"] = round(time.perf_counter() - started, 6)
    return result


def _capture_driver_ownership(driver, process_api=None):
    process_api = psutil if process_api is None else process_api
    ownership = {
        "service_pid": None,
        "service_create_time": None,
        "service_executable": "",
        "profile_path": "",
        "known_processes": {},
    }
    service_process = getattr(getattr(driver, "service", None), "process", None)
    service_pid = getattr(service_process, "pid", None)
    if not service_pid or process_api is None:
        return ownership
    try:
        process = process_api.Process(int(service_pid))
        snapshot = _process_snapshot(process)
    except Exception:
        return ownership
    if not snapshot:
        return ownership
    ownership["service_pid"] = snapshot["pid"]
    ownership["service_create_time"] = snapshot["create_time"]
    ownership["service_executable"] = snapshot.get("executable", "")
    ownership["known_processes"][snapshot["pid"]] = {
        "create_time": snapshot["create_time"],
        "name": snapshot.get("name", ""),
        "executable": snapshot.get("executable", ""),
        "profile_path": snapshot.get("profile_path", ""),
    }
    _refresh_driver_ownership(ownership, process_api=process_api)
    return ownership


def _refresh_driver_ownership(ownership, process_api=None):
    process_api = psutil if process_api is None else process_api
    service_pid = ownership.get("service_pid")
    service_created = ownership.get("service_create_time")
    if process_api is None or not service_pid or service_created is None:
        return ownership
    try:
        service = process_api.Process(int(service_pid))
        service_snapshot = _process_snapshot(service)
        if not service_snapshot or not _same_create_time(
            service_snapshot.get("create_time"), service_created
        ):
            return ownership
        descendants = service.children(recursive=True)
    except Exception:
        return ownership

    snapshots = [service_snapshot]
    for process in descendants:
        snapshot = _process_snapshot(process)
        if snapshot:
            snapshots.append(snapshot)
    profile_path = ownership.get("profile_path", "")
    if not profile_path:
        profile_path = next(
            (
                snapshot.get("profile_path", "")
                for snapshot in snapshots
                if snapshot.get("profile_path")
            ),
            "",
        )
        ownership["profile_path"] = profile_path
    known = ownership.setdefault("known_processes", {})
    for snapshot in snapshots:
        known[snapshot["pid"]] = {
            "create_time": snapshot["create_time"],
            "name": snapshot.get("name", ""),
            "executable": snapshot.get("executable", ""),
            "profile_path": snapshot.get("profile_path", ""),
        }
    return ownership


def _process_snapshot(process):
    try:
        pid = int(process.pid)
        created = float(process.create_time())
    except Exception:
        return None
    try:
        ppid = int(process.ppid())
    except Exception:
        ppid = 0
    try:
        name = str(process.name() or "")
    except Exception:
        name = ""
    try:
        executable = str(process.exe() or "")
    except Exception:
        executable = ""
    try:
        command_line = process.cmdline()
    except Exception:
        command_line = []
    return {
        "pid": pid,
        "ppid": ppid,
        "create_time": created,
        "name": name,
        "executable": executable,
        "profile_path": _extract_user_data_dir(command_line),
    }


def _extract_user_data_dir(command_line):
    values = list(command_line or [])
    for index, value in enumerate(values):
        token = str(value or "").strip()
        if token.startswith("--user-data-dir="):
            return _normalize_profile_path(token.split("=", 1)[1])
        if token == "--user-data-dir" and index + 1 < len(values):
            return _normalize_profile_path(values[index + 1])
    return ""


def _normalize_profile_path(value):
    text = str(value or "").strip().strip("\"'")
    return os.path.normcase(os.path.normpath(text)) if text else ""


def _same_create_time(left, right):
    try:
        return abs(float(left) - float(right)) <= 0.01
    except (TypeError, ValueError):
        return False


def _finite_timeout(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if not math.isfinite(parsed):
        parsed = float(default)
    return min(float(maximum), max(float(minimum), parsed))


def _process_matches_ownership(snapshot, ownership, descendant_pids=None):
    if not snapshot or not ownership:
        return False
    service_pid = ownership.get("service_pid")
    service_created = ownership.get("service_create_time")
    if not service_pid or service_created is None:
        return False
    pid = int(snapshot.get("pid") or 0)
    created = snapshot.get("create_time")
    known = ownership.get("known_processes") or {}
    expected = known.get(pid)
    if pid == int(service_pid) and not expected:
        return False
    if expected and not _same_create_time(created, expected.get("create_time")):
        return False
    if expected:
        expected_name = str(expected.get("name") or "")
        if expected_name and expected_name.casefold() != str(
            snapshot.get("name") or ""
        ).casefold():
            return False
        expected_executable = str(expected.get("executable") or "")
        current_executable = str(snapshot.get("executable") or "")
        if (
            expected_executable
            and os.path.normcase(expected_executable)
            != os.path.normcase(current_executable)
        ):
            return False

    if pid == int(service_pid):
        return _same_create_time(created, service_created)

    if pid in set(descendant_pids or ()):
        return float(created or 0.0) >= float(service_created) - 0.01

    owned_profile = _normalize_profile_path(ownership.get("profile_path"))
    current_profile = _normalize_profile_path(snapshot.get("profile_path"))
    return bool(expected and owned_profile and current_profile == owned_profile)


def _cleanup_owned_processes(ownership, timeout_seconds=None, process_api=None):
    process_api = psutil if process_api is None else process_api
    timeout_seconds = _finite_timeout(
        SELENIUM_CLEANUP_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds,
        default=SELENIUM_CLEANUP_TIMEOUT_SECONDS,
        minimum=0.001,
        maximum=30.0,
    )
    result = {
        "status": "skipped",
        "matched_count": 0,
        "terminated_count": 0,
        "killed_count": 0,
        "skipped_ownership_unproven_count": 0,
        "remaining_count": 0,
    }
    if process_api is None or not ownership.get("service_pid"):
        result["skipped_ownership_unproven_count"] = 1
        return result

    processes, descendant_pids, skipped = _collect_owned_processes(
        ownership,
        process_api,
    )
    result["matched_count"] = len(processes)
    result["skipped_ownership_unproven_count"] = skipped
    if not processes:
        return result

    terminate_requested = []
    service_pid = int(ownership["service_pid"])
    for process in sorted(processes, key=lambda item: item.pid == service_pid):
        snapshot = _process_snapshot(process)
        if not _process_matches_ownership(snapshot, ownership, descendant_pids):
            result["skipped_ownership_unproven_count"] += 1
            continue
        try:
            process.terminate()
            terminate_requested.append(process)
        except Exception:
            result["skipped_ownership_unproven_count"] += 1

    gone, alive = process_api.wait_procs(
        terminate_requested,
        timeout=timeout_seconds,
    )
    result["terminated_count"] = len(gone)
    kill_requested = []
    for process in alive:
        snapshot = _process_snapshot(process)
        if not _process_matches_ownership(snapshot, ownership, descendant_pids):
            result["skipped_ownership_unproven_count"] += 1
            continue
        try:
            process.kill()
            kill_requested.append(process)
        except Exception:
            result["skipped_ownership_unproven_count"] += 1
    result["killed_count"] = len(kill_requested)
    _, still_alive = process_api.wait_procs(
        kill_requested,
        timeout=timeout_seconds,
    )
    result["remaining_count"] = len(still_alive)
    result["status"] = (
        "completed"
        if not still_alive and not result["skipped_ownership_unproven_count"]
        else "partial"
    )
    return result


def _collect_owned_processes(ownership, process_api):
    service_pid = int(ownership["service_pid"])
    descendant_pids = set()
    descendants = []
    candidate_ids = set(int(pid) for pid in (ownership.get("known_processes") or {}))
    candidate_ids.add(service_pid)
    try:
        service = process_api.Process(service_pid)
        service_snapshot = _process_snapshot(service)
        if _process_matches_ownership(service_snapshot, ownership):
            descendants = service.children(recursive=True)
            descendant_pids = {int(process.pid) for process in descendants}
            candidate_ids.update(descendant_pids)
    except Exception:
        pass

    known = ownership.setdefault("known_processes", {})
    for process in descendants:
        snapshot = _process_snapshot(process)
        if not snapshot:
            continue
        known[snapshot["pid"]] = {
            "create_time": snapshot["create_time"],
            "name": snapshot.get("name", ""),
            "executable": snapshot.get("executable", ""),
            "profile_path": snapshot.get("profile_path", ""),
        }

    matched = []
    skipped = 0
    for pid in candidate_ids:
        try:
            process = process_api.Process(pid)
        except Exception:
            continue
        snapshot = _process_snapshot(process)
        if _process_matches_ownership(snapshot, ownership, descendant_pids):
            matched.append(process)
        else:
            skipped += 1
    return matched, descendant_pids, skipped


def _scroll_incrementally(driver, max_rounds=90, stable_rounds=5, cancel_check=None):
    stable = 0
    last_height = 0
    last_image_count = 0
    rounds = 0
    initial_height = int(driver.execute_script("return document.body.scrollHeight || 0") or 0)
    initial_image_count = int(
        driver.execute_script("return document.images ? document.images.length : 0") or 0
    )

    for round_index in range(max_rounds):
        if cancel_check and cancel_check():
            from chapter_source import SourceError

            raise SourceError("cancelled", "during_source_analysis")
        rounds = round_index + 1
        height = int(driver.execute_script("return document.body.scrollHeight || 0") or 0)
        image_count = int(
            driver.execute_script("return document.images ? document.images.length : 0") or 0
        )
        viewport = int(driver.execute_script("return window.innerHeight || 900") or 900)
        current_y = int(driver.execute_script("return window.scrollY || 0") or 0)

        if height <= last_height and image_count <= last_image_count:
            stable += 1
        else:
            stable = 0

        if stable >= stable_rounds and current_y + viewport >= height - 8:
            break

        last_height = max(last_height, height)
        last_image_count = max(last_image_count, image_count)
        next_y = min(current_y + int(viewport * 0.82), max(height - viewport, 0))
        driver.execute_script("window.scrollTo(0, arguments[0]);", next_y)
        time.sleep(1.0)

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    if cancel_check and cancel_check():
        from chapter_source import SourceError

        raise SourceError("cancelled", "during_source_analysis")
    time.sleep(2.0)
    final_height = int(driver.execute_script("return document.body.scrollHeight || 0") or 0)
    final_image_count = int(
        driver.execute_script("return document.images ? document.images.length : 0") or 0
    )
    final_viewport = int(driver.execute_script("return window.innerHeight || 900") or 900)
    final_y = int(driver.execute_script("return window.scrollY || 0") or 0)
    return {
        "rounds": int(rounds),
        "max_rounds": int(max_rounds),
        "stable_rounds_required": int(stable_rounds),
        "stable_rounds_observed": int(stable),
        "initial_scroll_height": int(initial_height),
        "final_scroll_height": int(final_height),
        "initial_dom_image_count": int(initial_image_count),
        "final_dom_image_count": int(final_image_count),
        "final_scroll_y": int(final_y),
        "viewport_height": int(final_viewport),
        "reached_document_end": bool(final_y + final_viewport >= final_height - 8),
        "stabilized": bool(stable >= stable_rounds),
    }


def _viewer_image_snapshot(driver, adapter=None):
    """Return the chapter image manifest exposed by the adapter's reader.

    Selectors are supplied by the source adapter: the downloader holds no site-specific
    knowledge, so adding a source never means editing this function.
    """
    selectors = (adapter.reader_selectors() if adapter else {}) or {}
    image_selector = selectors.get("image") or "img"
    payload = driver.execute_script(
        r"""
        const images = [...document.querySelectorAll(arguments[0])];
        const lazyAttributes = [
            'data-url', 'data-src', 'data-original', 'data-lazy-src', 'data-image-url',
            'data-original-src', 'data-lazy', 'data-lazyload', 'data-actualsrc', 'data-cfsrc'
        ];
        const urls = images.flatMap((el) => {
            const declaredSrc = el.getAttribute('src') || '';
            // ``currentSrc`` is the browser's selected responsive image; retain the declared
            // value too because a reader may swap it only after a lazy-load callback.
            const values = [el.currentSrc || '', declaredSrc, declaredSrc ? (el.src || '') : ''];
            lazyAttributes.forEach((name) => values.push(el.getAttribute(name) || ''));
            return values;
        }).filter(Boolean);
        return {
            imageCount: images.length,
            urls: [...new Set(urls)],
        };
        """,
        image_selector,
    ) or {}
    image_count = int(payload.get("imageCount") or 0)
    urls = [str(value) for value in payload.get("urls") or [] if value]
    return {
        "image_count": image_count,
        "urls": urls,
        "complete_manifest": bool(image_count and len(urls) == image_count),
    }


def _collect_image_candidates(driver, page_url, adapter=None):
    selectors = (adapter.reader_selectors() if adapter else {}) or {}
    container_selector = selectors.get("container") or ""
    image_selector = selectors.get("image") or "img"
    script = r"""
        const CONTAINER_SELECTOR = arguments[0] || "";
        const IMAGE_SELECTOR = arguments[1] || "";
        const out = [];
        const add = (el, tag, url, source, order) => {
            if (!url) return;
            const rect = el.getBoundingClientRect();
            out.push({
                tag,
                url,
                source,
                order,
                width: Math.round(rect.width || el.naturalWidth || 0),
                height: Math.round(rect.height || el.naturalHeight || 0),
                naturalWidth: el.naturalWidth || 0,
                naturalHeight: el.naturalHeight || 0,
                declaredWidth: Math.round(parseFloat(el.getAttribute('width') || '0')),
                declaredHeight: Math.round(parseFloat(el.getAttribute('height') || '0')),
                isChapterCandidate: Boolean(
                    (IMAGE_SELECTOR && el.matches(IMAGE_SELECTOR)) ||
                    (CONTAINER_SELECTOR && el.closest(CONTAINER_SELECTOR))
                ),
                inContainer: Boolean(CONTAINER_SELECTOR && el.closest(CONTAINER_SELECTOR)),
                y: Math.round((window.scrollY || 0) + rect.top),
                className: el.className || "",
                id: el.id || "",
                alt: el.alt || ""
            });
        };

        let order = 0;
        document.querySelectorAll("img").forEach((el) => {
            [
                "src",
                "data-src",
                "data-original",
                "data-url",
                "data-lazy-src",
                "data-image-url"
            ].forEach((name) => add(el, "img", el.getAttribute(name), name, order));

            const srcset = el.getAttribute("srcset");
            if (srcset) add(el, "img", srcset, "srcset", order);
            order += 1;
        });

        document.querySelectorAll("*").forEach((el) => {
            const bg = getComputedStyle(el).backgroundImage || "";
            if (bg && bg !== "none") add(el, "background", bg, "background-image", order++);
        });

        return out;
    """
    raw_candidates = driver.execute_script(script, container_selector, image_selector)
    candidates = []

    for raw in raw_candidates:
        for candidate_url in _extract_urls(raw.get("url") or ""):
            item = dict(raw)
            item["url"] = urljoin(page_url, candidate_url)
            candidates.append(item)

    return sorted(candidates, key=lambda item: (item.get("order", 0), item.get("y", 0)))


def _extract_urls(value):
    value = (value or "").strip()
    if not value or value.startswith("data:"):
        return []

    if value.startswith("url(") or "url(" in value:
        return [match.strip("\"' ") for match in re.findall(r"url\((.*?)\)", value) if match.strip()]

    if "," in value and any(part.strip().split(" ")[-1].endswith("w") for part in value.split(",")):
        best = None
        best_width = -1
        for part in value.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            width = _parse_srcset_width(bits[-1])
            if width >= best_width:
                best = bits[0]
                best_width = width
        return [best] if best else []

    return [value.split()[0]]


def _parse_srcset_width(value):
    match = re.match(r"(\d+)w$", value or "")
    if match:
        return int(match.group(1))
    return 0


def _dedupe_candidates(candidates):
    seen = set()
    unique = []

    for item in candidates:
        url = _normalize_url(item.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        item = dict(item)
        item["url"] = url
        unique.append(item)

    return unique


def _normalize_url(url):
    return (url or "").strip()


def _sanitized_url(url):
    """Stable diagnostic reference without query strings, signed paths or credentials."""
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme.casefold() == "canvas":
        return f"canvas:{(parsed.netloc or parsed.path or '')[:24]}"
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    fingerprint = hashlib.sha256((parsed.path or "/").encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{parsed.scheme.casefold()}://{host}/{fingerprint}"


def _download_candidates(
    driver,
    candidates,
    progress_callback,
    max_retries,
    max_images,
    debug_folder,
    report,
    referer,
    target_folder,
    transports=None,
):
    saved = []
    content_hashes: set[str] = set()
    # The adapter-owned accepted manifest is authoritative, including a completed bounded
    # pagination pass; the raw DOM count only describes the first reader surface.
    viewer_total = len(report.get("expected_chapter_candidate_ids") or []) or int(
        report.get("viewer_image_count") or 0)
    if max_images and viewer_total:
        viewer_total = min(viewer_total, int(max_images))
    total = viewer_total or len(candidates)

    for idx, candidate in enumerate(candidates, start=1):
        if max_images and len(saved) >= max_images:
            break

        url = candidate["url"]
        skip_reason = _candidate_skip_reason(candidate)
        if skip_reason:
            _report_ignored(report, candidate, skip_reason)
            continue

        download_started = time.perf_counter()
        canvas_data = candidate.get("canvas_data")
        if isinstance(canvas_data, (bytes, bytearray)):
            data = bytes(canvas_data)
            from download_transport import reserve_local_content

            reserve_local_content(transports, len(data))
            used_transport = "canvas"
        else:
            data = _download_url(url, referer, max_retries, transports=transports)
            used_transport = getattr(_download_url, "last_transport_name", "")
        report["timings"]["download_seconds"] += time.perf_counter() - download_started

        if not data:
            reason = getattr(_download_url, "last_failure", "download_failed")
            _report_ignored(report, candidate, str(reason or "download_failed"))
            continue

        validation_started = time.perf_counter()
        valid, info_or_reason, image = _validate_image_bytes(
            data,
            chapter_candidate=bool(candidate.get("isChapterCandidate")),
        )
        report["timings"]["validation_seconds"] += (
            time.perf_counter() - validation_started
        )
        if not valid:
            _report_ignored(report, candidate, info_or_reason)
            continue

        digest = hashlib.sha256(data).hexdigest()
        if digest in content_hashes:
            image.close()
            _report_ignored(report, candidate, "duplicate_image_bytes")
            continue
        content_hashes.add(digest)

        file_path = os.path.join(target_folder, f"{len(saved) + 1:03}.png")
        save_started = time.perf_counter()
        image.save(file_path, "PNG")
        image.close()
        report["timings"]["image_save_seconds"] += time.perf_counter() - save_started

        item = {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "url": _sanitized_url(url),
            "path": file_path,
            "transport_name": _safe_report_metadata(used_transport, "unknown"),
            "width": info_or_reason["width"],
            "height": info_or_reason["height"],
            "source": candidate.get("source"),
            "order": candidate.get("order"),
            "sha256": digest,
            "is_chapter_candidate": bool(candidate.get("isChapterCandidate")),
        }
        report["downloaded"].append(item)
        _record_successful_transport(report, item["transport_name"])
        report["total_downloaded"] = len(report["downloaded"])
        saved.append(file_path)

        if progress_callback:
            progress_callback(len(saved), max_images or total, "Baixando imagens")

    report["total_ignored"] = len(report["ignored"])
    report["download_gate"] = _build_download_gate(report)
    report["download_valid"] = bool(report["download_gate"].get("passed"))
    return saved


def _candidate_skip_reason(candidate):
    if isinstance(candidate.get("canvas_data"), (bytes, bytearray)):
        return None
    url = (candidate.get("url") or "").lower()
    label = " ".join(
        str(candidate.get(key) or "").lower() for key in ("className", "id", "alt", "source")
    )
    width = max(
        int(candidate.get("width") or 0),
        int(candidate.get("declaredWidth") or 0),
        int(candidate.get("naturalWidth") or 0),
    )
    height = max(
        int(candidate.get("height") or 0),
        int(candidate.get("declaredHeight") or 0),
        int(candidate.get("naturalHeight") or 0),
    )

    if not url.startswith(("http://", "https://")):
        return "not_http_url"

    if ".gif" in url or "giphy.com" in url:
        return "animated_or_external_asset"

    if any(token in url for token in ("spacer", "blank", "placeholder", "logo", "banner", "avatar", "sprite")):
        return "asset_or_placeholder_url"

    if any(token in label for token in ("logo", "avatar", "banner", "ad", "advert", "profile")):
        return "asset_or_ad_element"

    if candidate.get("isChapterCandidate"):
        if width and height and (
            width < MIN_IMAGE_WIDTH
            or height < MIN_CHAPTER_IMAGE_HEIGHT
            or width * height < MIN_CHAPTER_IMAGE_AREA
        ):
            return "too_small_chapter_candidate"
    elif width and height and (width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT):
        return "too_small_dom_size"

    return None


def _download_url(url, referer, max_retries, transports=None):
    """Fetch through bounded transports, trying each in order.

    The previous implementation was an unbounded ``requests.get``: no size ceiling and, more
    importantly, redirects followed silently — so a 302 could walk the fetch to an address
    the URL validation had already rejected. Coded failures (access denied, rate limited,
    challenge) propagate instead of being retried blindly.
    """
    from chapter_source import ChallengeRequired, SourceError
    from download_transport import LimitExceeded, RequestsTransport

    if transports is None:
        from chapter_source import select_adapter

        transports = [RequestsTransport(select_adapter(url))]

    # Function attributes are a narrow compatibility seam for the existing bytes-only
    # caller. Reset them before every fetch so a previous candidate cannot be credited
    # when this one fails or a test replaces the transport list.
    _download_url.last_failure = ""
    _download_url.last_transport_name = ""
    last_error = None
    for transport in transports:
        for _ in range(max(1, max_retries)):
            try:
                result = transport.fetch(url, referer=referer)
                _download_url.last_transport_name = _safe_report_metadata(
                    getattr(transport, "name", ""), "unknown")
                return result.content
            except LimitExceeded:
                raise                     # chapter budget is terminal; never retry/fallback
            except ChallengeRequired:
                raise                     # interactive challenge: stop, never work around
            except SourceError as exc:
                last_error = exc
                if exc.code != "invalid_image_response":
                    break                 # denied/rate-limited: retrying will not help
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001 - transport-level fault, try the next
                last_error = exc
                time.sleep(0.3)
    if last_error is not None:
        # Sanitized: only the failure code or class name — never a URL, cookie or header.
        _download_url.last_failure = getattr(last_error, "code", None) or type(last_error).__name__
    return None


def _validate_image_bytes(data, chapter_candidate=False):
    """Validate actual bytes before the legacy visual-quality checks.

    ``image_validation`` owns signature, markup and full-decode validation.  The remaining
    checks are chapter-contextual (large page vs. incidental DOM image), so they stay here.
    """
    from chapter_source import SourceError
    from image_validation import validate_image_bytes

    minimum_height = MIN_CHAPTER_IMAGE_HEIGHT if chapter_candidate else MIN_IMAGE_HEIGHT
    try:
        validated = validate_image_bytes(
            data,
            min_width=MIN_IMAGE_WIDTH,
            min_height=minimum_height,
            # A proven chapter strip may be short and compress to well below one KiB. It
            # still passes magic, full decode, width and contextual visual checks below.
            min_bytes=12 if chapter_candidate else 1024,
        )
        image = Image.open(io.BytesIO(validated.data))
        image.load()
        image = image.convert("RGB")
    except SourceError as exc:
        return False, f"{exc.code}:{exc.detail}", None
    except Exception as exc:  # pragma: no cover - Pillow errors are covered above
        return False, f"invalid_image:{type(exc).__name__}", None

    width, height = image.size
    minimum_area = MIN_CHAPTER_IMAGE_AREA if chapter_candidate else MIN_IMAGE_AREA
    if width < MIN_IMAGE_WIDTH or height < minimum_height or width * height < minimum_area:
        image.close()
        return False, "too_small_image", None

    if not chapter_candidate and _is_nearly_blank(image):
        image.close()
        return False, "nearly_blank_or_flat_image", None

    return True, {"width": width, "height": height}, image


def _is_nearly_blank(image):
    small = image.resize((32, 32))
    stat = ImageStat.Stat(small)
    extrema = small.convert("L").getextrema()
    mean = sum(stat.mean) / len(stat.mean)
    variance = sum(stat.var) / len(stat.var)
    return variance < 18 and (mean < 12 or mean > 243 or (extrema[1] - extrema[0]) < 10)


def _report_ignored(report, candidate, reason):
    report["ignored"].append(
        {
            "url": _sanitized_url(candidate.get("url")),
            "reason": reason,
            "width": candidate.get("naturalWidth") or candidate.get("width"),
            "height": candidate.get("naturalHeight") or candidate.get("height"),
            "declared_width": candidate.get("declaredWidth"),
            "declared_height": candidate.get("declaredHeight"),
            "is_chapter_candidate": bool(candidate.get("isChapterCandidate")),
            "source": candidate.get("source"),
            "order": candidate.get("order"),
        }
    )
    report["total_ignored"] = len(report["ignored"])


def _persist_download_metadata(debug_folder, report):
    folder = Path(debug_folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_json(folder / "downloaded_images.json", report)
    atomic_write_json(folder / "download_report.json", report)


def _write_download_report(debug_folder, report):
    _persist_download_metadata(debug_folder, report)
    _write_download_artifacts(
        debug_folder,
        report,
        [item.get("path") for item in report.get("downloaded", []) if item.get("path")],
    )


def _build_download_gate(report):
    expected_candidate_ids = report.get("expected_chapter_candidate_ids")
    if isinstance(expected_candidate_ids, list):
        expected_ids = [str(value or "") for value in expected_candidate_ids if str(value or "")]
        downloaded_ids = {
            str(item.get("candidate_id") or "")
            for item in (report.get("downloaded") or [])
            if str(item.get("candidate_id") or "")
        }
        chapter_downloaded = [
            item for item in (report.get("downloaded") or [])
            if item.get("is_chapter_candidate")
        ]
        missing_ids = [candidate_id for candidate_id in expected_ids if candidate_id not in downloaded_ids]
        reasons = []
        if not chapter_downloaded:
            reasons.append("no_valid_images")
        if missing_ids:
            reasons.append("viewer_images_missing")
        if len(chapter_downloaded) != len(expected_ids):
            reasons.append("viewer_count_mismatch")
        orders = [int(item.get("order") or 0) for item in chapter_downloaded]
        if orders and orders != sorted(orders):
            reasons.append("chapter_order_not_monotonic")
        return {
            "passed": not reasons,
            "reasons": reasons,
            "expected_viewer_images": len(expected_ids),
            "total_viewer_images": len(expected_ids),
            "downloaded_viewer_images": len(chapter_downloaded),
            "missing_viewer_images": len(missing_ids),
            "missing_candidate_id_samples": missing_ids[:12],
            "order_monotonic": not orders or orders == sorted(orders),
        }
    raw_expected = (
        report.get("expected_chapter_urls")
        if "expected_chapter_urls" in report
        else report.get("viewer_urls") or []
    )
    viewer_urls = list(dict.fromkeys(raw_expected or []))
    requested_max = report.get("requested_max_images")
    expected_urls = viewer_urls[: int(requested_max)] if requested_max else viewer_urls
    downloaded = report.get("downloaded") or []
    downloaded_urls = {str(item.get("url") or "") for item in downloaded}
    chapter_downloaded = [item for item in downloaded if item.get("is_chapter_candidate")]
    missing_urls = [url for url in expected_urls if url not in downloaded_urls]
    reasons = []
    if not downloaded:
        reasons.append("no_valid_images")
    if viewer_urls and missing_urls:
        reasons.append("viewer_images_missing")
    if expected_urls and len(chapter_downloaded) != len(expected_urls):
        reasons.append("viewer_count_mismatch")
    orders = [int(item.get("order") or 0) for item in chapter_downloaded]
    if orders and orders != sorted(orders):
        reasons.append("chapter_order_not_monotonic")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "expected_viewer_images": len(expected_urls),
        "total_viewer_images": len(viewer_urls),
        "downloaded_viewer_images": len(chapter_downloaded),
        "missing_viewer_images": len(missing_urls),
        "missing_url_samples": missing_urls[:12],
        "order_monotonic": not orders or orders == sorted(orders),
    }


def _write_download_artifacts(debug_folder, report, image_paths):
    folder = os.path.abspath(debug_folder)
    os.makedirs(folder, exist_ok=True)
    gate = report.get("download_gate") or {}
    lines = [
        "Tradutor.Ia - relatorio de download",
        f"URL: {report.get('url', '')}",
        f"Estrategia: {report.get('collection_strategy', '')}",
        f"Imagens no DOM: {report.get('total_dom_images', 0)}",
        f"Candidatos: {report.get('total_candidates', 0)}",
        f"URLs unicas: {report.get('total_unique_urls', 0)}",
        f"Imagens esperadas no leitor: {gate.get('expected_viewer_images', 0)}",
        f"Imagens validas baixadas: {report.get('total_downloaded', 0)}",
        f"Imagens do leitor baixadas: {gate.get('downloaded_viewer_images', 0)}",
        f"Imagens do leitor ausentes: {gate.get('missing_viewer_images', 0)}",
        f"Ordem preservada: {'sim' if gate.get('order_monotonic') else 'nao'}",
        f"Selenium chegou ao fim: {'sim' if (report.get('scroll_diagnostics') or {}).get('reached_document_end') else 'nao'}",
        f"Lazy loading completo: {'sim' if report.get('lazy_loading_fully_loaded') else 'nao'}",
        f"Download gate: {'aprovado' if gate.get('passed') else 'reprovado'}",
        f"Motivos: {', '.join(gate.get('reasons') or []) or 'nenhum'}",
    ]
    with open(os.path.join(folder, "download_report.txt"), "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")
    _write_download_html_report(
        report,
        os.path.join(folder, "download_report.html"),
    )
    _write_download_contact_sheet(
        image_paths,
        os.path.join(folder, "download_contact_sheet.jpg"),
    )


def _write_download_html_report(report, output_path):
    gate = report.get("download_gate") or {}
    ignored = report.get("ignored") or []
    downloaded = report.get("downloaded") or []
    reason_counts = Counter(str(item.get("reason") or "unknown") for item in ignored)
    timing = report.get("timings") or {}
    scroll = report.get("scroll_diagnostics") or {}

    def esc(value):
        return html.escape(str(value or ""))

    reason_rows = "\n".join(
        f"<tr><td>{esc(reason)}</td><td>{count}</td></tr>"
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ) or "<tr><td colspan='2'>Nenhuma rejeicao</td></tr>"
    ignored_rows = "\n".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{esc(item.get('reason'))}</td>"
        f"<td>{esc(item.get('source'))}</td>"
        f"<td>{esc(item.get('order'))}</td>"
        f"<td>{esc(item.get('width'))}x{esc(item.get('height'))}</td>"
        f"<td><code>{esc(item.get('url'))}</code></td>"
        "</tr>"
        for index, item in enumerate(ignored[:300], start=1)
    ) or "<tr><td colspan='6'>Nenhuma rejeicao</td></tr>"
    downloaded_rows = "\n".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td>{esc(item.get('order'))}</td>"
        f"<td>{esc(item.get('width'))}x{esc(item.get('height'))}</td>"
        f"<td>{esc(item.get('source'))}</td>"
        f"<td><code>{esc(item.get('path'))}</code></td>"
        "</tr>"
        for index, item in enumerate(downloaded[:300], start=1)
    ) or "<tr><td colspan='5'>Nenhuma imagem baixada</td></tr>"
    gate_reasons = ", ".join(gate.get("reasons") or []) or "nenhum"
    html_doc = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tradutor.Ia - Relatorio de download</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, Segoe UI, Arial, sans-serif; }}
    body {{ margin: 0; background: #101115; color: #f1f5f9; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1, h2 {{ margin: 0 0 14px; }}
    .muted {{ color: #94a3b8; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin: 18px 0 28px; }}
    .card {{ background: #171a22; border: 1px solid #283244; border-radius: 16px; padding: 16px; box-shadow: 0 14px 40px rgba(0,0,0,.22); }}
    .value {{ font-size: 28px; font-weight: 800; color: #74f2ce; }}
    .ok {{ color: #5ee787; }}
    .bad {{ color: #ff7b72; }}
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0 28px; background: #151821; border-radius: 14px; overflow: hidden; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #273042; text-align: left; vertical-align: top; }}
    th {{ background: #1e2633; color: #cbd5e1; }}
    code {{ white-space: normal; word-break: break-all; color: #a5f3fc; }}
  </style>
</head>
<body>
<main>
  <h1>Tradutor.Ia - Relatorio de download</h1>
  <p class="muted"><code>{esc(report.get('url'))}</code></p>
  <div class="grid">
    <section class="card"><div class="muted">Download gate</div><div class="value {'ok' if gate.get('passed') else 'bad'}">{'aprovado' if gate.get('passed') else 'reprovado'}</div><div class="muted">Motivos: {esc(gate_reasons)}</div></section>
    <section class="card"><div class="muted">Imagens no DOM</div><div class="value">{esc(report.get('total_dom_images'))}</div></section>
    <section class="card"><div class="muted">Candidatos</div><div class="value">{esc(report.get('total_candidates'))}</div></section>
    <section class="card"><div class="muted">URLs unicas</div><div class="value">{esc(report.get('total_unique_urls'))}</div></section>
    <section class="card"><div class="muted">Viewer images</div><div class="value">{esc(report.get('viewer_image_count'))}</div></section>
    <section class="card"><div class="muted">Baixadas</div><div class="value">{esc(report.get('total_downloaded'))}</div></section>
    <section class="card"><div class="muted">Rejeitadas</div><div class="value">{esc(report.get('total_ignored'))}</div></section>
    <section class="card"><div class="muted">Estrategia</div><div class="value">{esc(report.get('collection_strategy'))}</div></section>
  </div>
  <h2>Completude</h2>
  <table>
    <tr><th>Esperadas no leitor</th><td>{esc(gate.get('expected_viewer_images'))}</td></tr>
    <tr><th>Baixadas do leitor</th><td>{esc(gate.get('downloaded_viewer_images'))}</td></tr>
    <tr><th>Ausentes do leitor</th><td>{esc(gate.get('missing_viewer_images'))}</td></tr>
    <tr><th>Ordem preservada</th><td>{'sim' if gate.get('order_monotonic') else 'nao'}</td></tr>
    <tr><th>Manifesto do viewer completo</th><td>{'sim' if report.get('viewer_manifest_complete') else 'nao'}</td></tr>
    <tr><th>Selenium chegou ao fim</th><td>{'sim' if scroll.get('reached_document_end') else 'nao'}</td></tr>
    <tr><th>Lazy loading completo</th><td>{'sim' if report.get('lazy_loading_fully_loaded') else 'nao'}</td></tr>
    <tr><th>Rodadas de scroll</th><td>{esc(scroll.get('rounds'))}/{esc(scroll.get('max_rounds'))}</td></tr>
    <tr><th>Imagens DOM apos scroll</th><td>{esc(scroll.get('final_dom_image_count'))}</td></tr>
    <tr><th>Altura final da pagina</th><td>{esc(scroll.get('final_scroll_height'))}</td></tr>
    <tr><th>Tempo coleta</th><td>{float(timing.get('collection_seconds') or 0):.2f}s</td></tr>
    <tr><th>Tempo total</th><td>{float(timing.get('total_seconds') or 0):.2f}s</td></tr>
  </table>
  <h2>Motivos de rejeicao</h2>
  <table><tr><th>Motivo</th><th>Quantidade</th></tr>{reason_rows}</table>
  <h2>Rejeicoes - amostra ate 300</h2>
  <table><tr><th>#</th><th>Motivo</th><th>Fonte</th><th>Ordem</th><th>Tamanho</th><th>URL</th></tr>{ignored_rows}</table>
  <h2>Imagens baixadas - amostra ate 300</h2>
  <table><tr><th>#</th><th>Ordem</th><th>Tamanho</th><th>Fonte</th><th>Arquivo</th></tr>{downloaded_rows}</table>
</main>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as file:
        file.write(html_doc)


def _write_download_contact_sheet(image_paths, output_path, columns=5, thumb_width=150):
    if not image_paths:
        return ""
    rows = (len(image_paths) + columns - 1) // columns
    cell_height = 210
    sheet = Image.new("RGB", (columns * thumb_width, rows * cell_height), "#111114")
    for index, path in enumerate(image_paths):
        try:
            with Image.open(path) as source:
                preview = source.convert("RGB")
                preview.thumbnail((thumb_width - 8, cell_height - 28))
                x = (index % columns) * thumb_width + (thumb_width - preview.width) // 2
                y = (index // columns) * cell_height + 20
                sheet.paste(preview, (x, y))
        except Exception:
            continue
    sheet.save(output_path, "JPEG", quality=82, optimize=True)
    return output_path
