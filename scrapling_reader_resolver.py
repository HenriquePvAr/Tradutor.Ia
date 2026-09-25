"""Optional, selected-site dynamic reader resolver.

This module is deliberately inert when Scrapling is not installed.  It is not a second
downloader: it returns the normal ``SourceAnalysis`` candidate model so the existing
validated downloader remains the only component that fetches page images.
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
import pickle
import re
import signal
import subprocess
import struct
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

_DYNAMIC_IMPORT_ERROR = ""
try:  # optional dependency; importing the application must never require Scrapling
    from scrapling.fetchers import DynamicFetcher as _DynamicFetcher
except Exception as exc:  # pragma: no cover - exercised in minimal installs
    _DynamicFetcher = None
    # Keep the public surface sanitized.  The exact import message can contain local
    # paths and is intentionally never written to diagnostics.
    _DYNAMIC_IMPORT_ERROR = type(exc).__name__[:80]


SELECTED_DYNAMIC_HOSTS = frozenset({"comix.to", "www.comix.to"})
MAX_PAGES = 400
MAX_NETWORK_EVENTS = 256
MAX_BROWSER_RESPONSE_BODY_BYTES = 8 * 1024 * 1024
ALLOWED_BROWSER_IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_CANONICAL_ATTEMPTS = 3
# One monotonic deadline covers browser startup, page actions, materialization and any
# bounded full-resolution retry. Playwright's per-operation timeout is shortened to the
# remaining budget so a stuck browser operation raises into Scrapling's cleanup path.
DYNAMIC_RESOLVER_DEADLINE_SECONDS = 180.0
DYNAMIC_RESOLVER_CLEANUP_RESERVE_SECONDS = 3.0
# Missing-page (canonical materialization) budget model.  Each unresolved logical page
# gets its OWN bounded budget so one slow page cannot starve the pages after it; the
# chapter-level ceiling is proportional to the number of unresolved pages (with a hard
# safety cap).  This replaces the old single ~30s deadline shared by every page.
MISSING_PAGE_BUDGET_SECONDS = 25.0
MISSING_CHAPTER_BASE_OVERHEAD_SECONDS = 8.0
MISSING_CHAPTER_MAX_BUDGET_SECONDS = 220.0
# Canonical materialization is intermittently flaky (a discovered page can stay an
# unmaterialized ``scrapling_dom`` placeholder). A fresh full pass almost always
# completes it, so that one transient mode gets a bounded whole-resolution retry.
MAX_RESOLUTION_ATTEMPTS = 3
MAX_SCROLL_STEPS = 120
SCROLL_STABLE_ROUNDS = 3
_PAGE_SELECTOR = "img.rpage-page__img"


def _expected_page_indices(control_count: int) -> list[int]:
    """Build the reader's logical page set from public controls, not a chapter constant."""
    return list(range(1, max(0, int(control_count)) + 1))


def _target_page_indices(control_count: int, requested_count: int | None = None) -> list[int]:
    expected = _expected_page_indices(control_count)
    return expected if requested_count is None else expected[:max(0, int(requested_count))]


def _missing_chapter_budget_seconds(unresolved_count: int) -> float:
    """Chapter-level materialization ceiling, proportional to the number of unresolved
    pages (with a hard safety cap).  Never a single fixed budget shared by every page."""
    return min(
        MISSING_CHAPTER_MAX_BUDGET_SECONDS,
        MISSING_CHAPTER_BASE_OVERHEAD_SECONDS
        + max(0, int(unresolved_count)) * MISSING_PAGE_BUDGET_SECONDS,
    )


# Headroom added on top of the pending-page budget before a fresh full retry may start:
# it reserves margin for browser startup jitter and the final cleanup pass so a retry is
# only attempted when it can realistically finish inside the global deadline.
RETRY_BUDGET_SAFETY_MARGIN_SECONDS = 15.0


def _materialization_retry_decision(
    *, pending_count: int, prev_pending_count: int | None,
    remaining_budget: float, attempt: int, max_attempts: int, pending_budget: float,
) -> str:
    """Decide what a pass that left pages unmaterialized should do next.

    Returns ``retry`` / ``no_progress`` / ``budget_insufficient`` / ``failed``.

    * ``no_progress`` -- a *retry* pass that did not shrink the pending set versus the
      previous pass, with a real positive pending count on both.  Repeating an identical
      pass cannot help, so fail closed immediately instead of burning the deadline.  A
      first pass -- or a pass whose pending count is unavailable (0) -- is never classified
      no_progress: an intermittently-flaky first pass is still allowed its bounded retry
      (Comix materialization recovers on a later full pass ~half the time).
    * ``budget_insufficient`` -- the remaining budget cannot fund another bounded full pass
      with its cleanup margin reserved.
    * ``retry`` -- another bounded pass is worthwhile and affordable.
    * ``failed`` -- attempts are exhausted.
    """
    if (prev_pending_count is not None and pending_count > 0
            and pending_count >= prev_pending_count):
        return "no_progress"
    if attempt >= max_attempts:
        return "failed"
    if remaining_budget < pending_budget + RETRY_BUDGET_SAFETY_MARGIN_SECONDS:
        return "budget_insufficient"
    return "retry"


def _resolution_failure_code(candidates: list, final_missing: list) -> str | None:
    """Classify a resolution outcome into a fail-closed error code, or None when complete.

    Distinguishes the three real failure modes so a materialization timeout is never
    mislabeled as an empty discovery:
      * no media discovered at all            -> no_reader_images
      * unmaterialized scrapling_dom left in  -> canonical_materialization_failed
      * expected logical page never finished  -> canonical_materialization_timeout
    """
    if not candidates:
        return "no_reader_images"
    if any(str(item.get("source") or "") == "scrapling_dom" for item in candidates):
        return "canonical_materialization_failed"
    if final_missing:
        return "canonical_materialization_timeout"
    return None


def _merge_logical_candidates(
    preload_candidates: list[dict[str, Any]],
    fallback_candidates: list[dict[str, Any]],
    control_count: int,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Merge indexed reader candidates and fail closed on any missing logical index."""
    by_index = {
        int(item["logical_page_index"]): item
        for item in preload_candidates
        if item.get("logical_page_index") is not None
    }
    by_index.update({
        int(item["logical_page_index"]): item for item in fallback_candidates
        if item.get("logical_page_index") is not None
    })
    expected = _expected_page_indices(control_count)
    missing = [index for index in expected if index not in by_index]
    return [by_index[index] for index in sorted(by_index)], missing


def _merge_materialized_attempts(
    previous: list[dict[str, Any]], current: list[dict[str, Any]], control_count: int,
    requested_count: int | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Retain verified pages across a bounded fresh-browser retry and return only
    canonical indices that still need materialization. New materialized candidates win;
    a fresh DOM placeholder can never replace a previously materialized page.
    """
    by_index: dict[int, dict[str, Any]] = {}
    unindexed: list[dict[str, Any]] = []
    seen_unindexed: set[str] = set()
    for item in [*previous, *current]:
        index = item.get("logical_page_index")
        source = str(item.get("source") or "")
        if index is None:
            if requested_count is not None:
                continue
            key = str(item.get("url") or "")
            if key not in seen_unindexed:
                unindexed.append(item)
                seen_unindexed.add(key)
            continue
        index = int(index)
        if requested_count is not None and index > requested_count:
            continue
        existing = by_index.get(index)
        if existing is None or source != "scrapling_dom":
            by_index[index] = item
    merged = [by_index[index] for index in sorted(by_index)] + unindexed
    pending = [
        index for index in _target_page_indices(control_count, requested_count)
        if index not in by_index or str(by_index[index].get("source") or "") == "scrapling_dom"
    ]
    return merged, pending


class DynamicReaderError(RuntimeError):
    """Stable, sanitized resolver failure."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DynamicReaderCapabilities:
    available: bool
    scrapling_version: str = ""
    browser_mode: str = "real_chrome_ephemeral"
    selected_hosts: tuple[str, ...] = tuple(sorted(SELECTED_DYNAMIC_HOSTS))
    import_error: str = ""


def discover_system_browser() -> tuple[str, str] | None:
    """Return a known system Chrome/Edge executable without downloading anything."""
    candidates = [
        ("Chrome", Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe"),
        ("Chrome", Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe"),
        ("Chrome", Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        ("Edge", Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe"),
        ("Edge", Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe"),
        ("Edge", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"),
    ]
    for kind, path in candidates:
        if str(path) and path.is_file():
            return kind, str(path)
    return None


def capabilities() -> DynamicReaderCapabilities:
    if _DynamicFetcher is None:
        return DynamicReaderCapabilities(False, import_error=_DYNAMIC_IMPORT_ERROR)
    version = ""
    try:
        from importlib.metadata import version as package_version
        version = package_version("scrapling")
    except Exception:
        pass
    return DynamicReaderCapabilities(True, version)


def supports_url(url: str) -> bool:
    return (urlparse(str(url or "")).hostname or "").lower() in SELECTED_DYNAMIC_HOSTS


def _adaptive_root() -> Path:
    root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(root) / "YomuSekai" / "scraping" / "adaptive" / "comix"


def _store_selector_profile(selector: str, resource_host: str) -> None:
    """Persist only selector evidence, never HTML, cookies, tokens, or image bytes."""
    if not selector or not resource_host:
        return
    path = _adaptive_root()
    path.mkdir(parents=True, exist_ok=True)
    target = path / "profile.json"
    payload = {
        "schema": 1,
        "site": "comix.to",
        "selector": selector[:120],
        "resource_host": resource_host[:160],
        "updated_unix": int(time.time()),
    }
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _append_telemetry(event: str, **fields: Any) -> None:
    """Best-effort local telemetry with an allow-list of non-sensitive fields."""
    allowed = {"event", "site", "status", "pages", "selector", "elapsed_ms", "error_code",
               "attempt", "max_attempts", "pending_count", "pending_indices", "missing_count",
               "stage", "remaining_ms", "remaining_budget_ms", "new_pages", "materialized_count",
               "body_capture_count", "body_read_failed", "body_skip_dedup",
               "body_skip_content_type", "body_skip_oversize", "body_promoted",
               "body_read_total", "body_read_ms", "body_promoted_total",
               "body_rejected_total", "body_promotion_rate", "canonical_index",
               "expected_count", "final_missing_count",
               "discovery_ms", "navigation_ms", "render_wait_ms", "canvas_capture_ms",
               "response_body_ms", "total_materialization_ms", "result_source", "reader_image_host_count",
               "attempt_elapsed_ms", "pending_before", "pending_after", "new_materialized",
               "failure_stage", "trace_id", "navigation_result", "candidate_source",
               "candidate_url_presence", "requested_control_exists", "requested_control_disabled",
               "control_count", "active_control_index", "container_exists", "container_visible",
               "canvas_count", "canvas_width", "canvas_height", "image_response_seen",
               "body_candidate_seen", "last_failure_reason", "nav_attempt_count", "reader_page_number",
               "page_root_tag", "page_root_class", "page_root_child_count", "page_root_image_count",
               "image_complete", "image_natural_width", "image_natural_height", "image_page_number",
               "image_src_present",
               "active_container_data_page", "capture_failure_reason",
               "already_materialized_revisited", "retry_pending_indices", "materialization_target_count",
               "out_of_scope_materialized_count"}
    safe = {key: value for key, value in fields.items() if key in allowed}
    safe["event"] = event[:64]
    safe.setdefault("site", "comix.to")
    try:
        root = (Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
                / "YomuSekai" / "diagnostics")
        root.mkdir(parents=True, exist_ok=True)
        with (root / "scrapling_reader.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe, sort_keys=True) + "\n")
    except Exception:
        pass


def _safe_host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def _sanitized_network_event(url: str, resource_type: str, status: int) -> dict[str, Any]:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if "/api/v1/chapters/" in path:
        path = "/api/v1/chapters/{id}"
    elif resource_type == "image":
        path = "/image-resource"
    else:
        path = re.sub(r"\d+", "#", path)[:120]
    return {"host": (parsed.hostname or "")[:160], "path": path,
            "resource_type": resource_type[:24], "status": int(status or 0)}


def _extract_candidates(page: Any, page_url: str, events: list[dict[str, Any]],
                        cancel_check: Callable[[], bool] | None) -> tuple[list[dict[str, Any]], str]:
    if cancel_check and cancel_check():
        raise DynamicReaderError("cancelled")
    # Execute in the page context.  Values are bounded and contain only URLs/dimensions;
    # full HTML and response bodies never leave the browser.
    script = """
    (selectors) => {
      const nodes = Array.from(document.querySelectorAll(selectors));
        return nodes.slice(0, 400).map((el, index) => {
          const r = el.getBoundingClientRect();
          return {url: el.currentSrc || el.src || '', order:index, y:Math.round(r.y),
            width:Math.round(r.width), height:Math.round(r.height),
            naturalWidth:el.naturalWidth||0, naturalHeight:el.naturalHeight||0,
            alt: String(el.getAttribute('alt') || '').slice(0,80),
            className:String(el.className||'').slice(0,240),
            container:'comix-reader', context:'reader', visible:!!(r.width&&r.height)};
      });
    }
    """
    try:
        rows = page.evaluate(script, _PAGE_SELECTOR) or []
    except Exception as exc:
        raise DynamicReaderError("structure_changed") from exc
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        item = dict(row)
        match = re.search(r"(?:^|\b)page\s+(\d+)(?:\b|$)", str(item.get("alt") or ""), re.I)
        if match:
            item["logical_page_index"] = int(match.group(1))
        item["order"] = len(candidates)
        item["source"] = "scrapling_dom"
        item["origin"] = "dynamic_dom"
        candidates.append(item)
    if not candidates:
        raise DynamicReaderError("no_reader_images")
    selector = _PAGE_SELECTOR
    resource_host = _safe_host(candidates[0]["url"])
    _store_selector_profile(selector, resource_host)
    return candidates, selector


def _read_dom_state(page: Any, selector: str) -> dict[str, Any] | None:
    """Read bounded reader state used by the normal scroll walk.

    Comix can keep only a small virtualized window of image nodes in the DOM.  The
    resolver therefore records the current nodes, scroll metrics and lazy attributes,
    but never reads HTML or response bodies.
    """
    script = """
    (selector) => {
      const nodes = Array.from(document.querySelectorAll(selector));
      const rows = nodes.slice(0, 400).map((el, index) => {
        const r = el.getBoundingClientRect();
        const srcset = el.getAttribute('srcset') || el.getAttribute('data-srcset') || '';
        const srcsetUrl = srcset.split(',')[0].trim().split(/\\s+/)[0] || '';
        const src = el.currentSrc || el.src || el.getAttribute('data-src') ||
          el.getAttribute('data-lazy-src') || el.getAttribute('data-original') || srcsetUrl || '';
        return {
          url: src, order: index, y: Math.round(r.y + window.scrollY),
          width: Math.round(r.width), height: Math.round(r.height),
          naturalWidth: el.naturalWidth || 0, naturalHeight: el.naturalHeight || 0,
          className: String(el.className || '').slice(0, 240),
          src_present: !!el.getAttribute('src') || !!el.currentSrc,
          data_src_present: !!el.getAttribute('data-src') || !!el.getAttribute('data-lazy-src'),
          srcset_present: !!el.getAttribute('srcset') || !!el.getAttribute('data-srcset'),
          data_original_present: !!el.getAttribute('data-original'),
          parent_class: String(el.parentElement?.className || '').slice(0, 160),
          ancestor_class: String(el.parentElement?.parentElement?.className || '').slice(0, 160)
        };
      });
      const reader = document.querySelector('[data-comix-reader], .reader, #reader');
      const rect = reader ? reader.getBoundingClientRect() : null;
      return {
        rows,
        slot_count: nodes.length,
        placeholder_count: rows.filter(row => !row.url).length,
        scroll_y: Math.round(window.scrollY || 0),
        viewport_height: Math.round(window.innerHeight || 0),
        document_height: Math.round(document.documentElement.scrollHeight || document.body.scrollHeight || 0),
        reader_top: Math.round((rect?.top || 0) + window.scrollY),
        reader_bottom: Math.round((rect?.bottom || 0) + window.scrollY),
        at_bottom: (window.scrollY + window.innerHeight) >= (document.documentElement.scrollHeight - 8),
        lazy_attributes: rows.flatMap(row => [
          row.data_src_present ? 'data-src' : '',
          row.srcset_present ? 'srcset' : '',
          row.data_original_present ? 'data-original' : ''
        ]).filter(Boolean).filter((value, index, all) => all.indexOf(value) === index)
      };
    }
    """
    try:
        state = page.evaluate(script, selector)
    except Exception as exc:
        raise DynamicReaderError("structure_changed") from exc
    return state if isinstance(state, dict) and isinstance(state.get("rows"), list) else None


def _read_reader_overlay_state(page: Any) -> dict[str, Any]:
    """Return DOM-backed overlay state before any canonical viewport capture."""
    script = """() => {
      const dialogs = [...document.querySelectorAll('[role="dialog"], dialog, .modal, .drawer')];
      const visible = node => {
        const style = getComputedStyle(node);
        const r = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' &&
          Number(style.opacity || 1) > 0 && r.width > 0 && r.height > 0;
      };
      const settings = dialogs.find(node => visible(node) &&
        (/settings|preload|reading direction/i.test(String(node.innerText || '')) ||
         !!node.querySelector('input[name="preload"]')));
      const blocking = dialogs.filter(node => visible(node));
      return {
        settings_modal_visible: !!settings,
        blocking_overlay_visible: blocking.length > 0,
        visible_dialog_count: blocking.length,
        settings_selector: settings ? (settings.id ? '#' + settings.id :
          settings.className ? '.' + String(settings.className).trim().split(/\\s+/)[0] :
          '[role="dialog"]') : ''
      };
    }"""
    try:
        value = page.evaluate(script) or {}
    except Exception:
        return {"settings_modal_visible": True, "blocking_overlay_visible": True,
                "visible_dialog_count": -1, "settings_selector": "unknown"}
    return value if isinstance(value, dict) else {}


def _overlay_blocks_capture(overlay: dict[str, Any]) -> bool:
    """A visible Settings or blocking overlay must gate every canonical capture."""
    return bool(overlay.get("settings_modal_visible")
                or overlay.get("blocking_overlay_visible"))


def _close_reader_settings(page: Any) -> dict[str, Any]:
    """Close the public reader settings surface and verify it is gone in the DOM."""
    before = _read_reader_overlay_state(page)
    if before.get("settings_modal_visible") or before.get("blocking_overlay_visible"):
        page.evaluate("""() => {
          const scope = document.querySelector('.rpage-modal--settings')
            || document.querySelector('[role="dialog"]')
            || document.querySelector('dialog')
            || document;
          const button = scope.querySelector('button[aria-label="Close settings"]');
          if (button) { button.click(); return true; }
          const fallback = [...document.querySelectorAll('button,[role="button"]')]
            .find(node => /close|fechar|\\u00d7/i.test(String(node.getAttribute('aria-label') || node.innerText || '')));
          if (fallback) { fallback.click(); return true; }
          return false;
        }""")
        page.wait_for_timeout(250)
    after = _read_reader_overlay_state(page)
    return {
        "before": before,
        "after": after,
        "closed": not bool(after.get("settings_modal_visible") or
                            after.get("blocking_overlay_visible")),
    }


def _configure_preload_all(
    page: Any,
    *,
    candidates: list[dict[str, Any]],
    selector: str,
    url: str,
    events: list,
    cancel_check: Callable[[], bool] | None,
    browser_bodies: dict,
    pending_responses: dict[str, dict[str, Any]],
    body_stats: dict[str, Any],
    previously_materialized_indices: set[int],
    promoted_resource_indices: dict[str, int],
    page_body_elapsed_ms: dict[int, float],
    page_discovery_elapsed_ms: dict[int, float] | None = None,
    response_seen_indices: set[int] | None = None,
    body_candidate_indices: set[int] | None = None,
    page_action_started: float,
    reader_image_hosts: set[str],
    scroll_diagnostics: dict[str, Any],
    requested_count: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Switch the public reader to "Preload all" and guarantee the Settings surface
    is closed before any downstream canonical capture.

    Lifecycle contract (the historical bug this fixes was that the close and the
    fail-closed gate lived inside a ``try`` whose broad ``except`` swallowed them):

      * preload configuration runs inside ``try`` and may fail — partial preload is
        acceptable because the missing-page loop resolves the rest;
      * ``_close_reader_settings`` runs in ``finally`` so it executes whether preload
        succeeded or threw, and a close failure never masks the preload exception;
      * the fail-closed overlay gate lives *after* the try/except/finally, so a
        leftover modal always raises and can never be swallowed here.
    """
    preload_stage = "read_state"
    current: dict[str, Any] = {}
    try:
        state = page.evaluate("""() => ({
          settings: !!document.querySelector('button[title="Settings"]'),
          some: !!document.querySelector('input[name="preload"][value="some"]'),
          all: !!document.querySelector('input[name="preload"][value="all"]'),
          checked: document.querySelector('input[name="preload"]:checked')?.value || null,
          direction: document.querySelector('[role="radio"][aria-checked="true"]')?.innerText || null,
          controls: document.querySelectorAll('button[aria-label^="Go to page "]').length
        })""") or {}
        if not state.get("some") or not state.get("all"):
            preload_stage = "open_settings"
            page.evaluate("""() => {
              const button = document.querySelector('button[title="Settings"]');
              if (button) button.click();
            }""")
            page.wait_for_timeout(400)
            state = page.evaluate("""() => ({
              some: !!document.querySelector('input[name="preload"][value="some"]'),
              all: !!document.querySelector('input[name="preload"][value="all"]'),
              checked: document.querySelector('input[name="preload"]:checked')?.value || null,
              direction: document.querySelector('[role="radio"][aria-checked="true"]')?.innerText || null,
              controls: document.querySelectorAll('button[aria-label^="Go to page "]').length
            })""") or {}
        scroll_diagnostics.update({
            "preload_mode_initial": str(state.get("checked") or "unknown"),
            "preload_control_found": bool(state.get("all")),
            "reading_direction": str(state.get("direction") or "unknown"),
        })
        scroll_diagnostics["page_control_count"] = int(state.get("controls") or 0)
        if requested_count is not None:
            # Do not switch to the reader's full preload mode for a bounded request.
            scroll_diagnostics["preload_mode_discovery"] = str(state.get("checked") or "unknown")
            state["all"] = False
        if state.get("all"):
            preload_stage = "select_all"
            page.evaluate("""() => {
              const input = document.querySelector('input[name="preload"][value="all"]');
              if (input && !input.checked) {
                input.click();
                input.dispatchEvent(new Event('change', {bubbles: true}));
              }
            }""")
            preload_stage = "polling"
            stable = 0
            previous_count = len(candidates)
            for _ in range(20):
                page.wait_for_timeout(350)
                current = page.evaluate("""() => ({
                  checked: document.querySelector('input[name="preload"]:checked')?.value || null,
                  count: document.querySelectorAll('img.rpage-page__img').length,
                  loaded: [...document.querySelectorAll('img.rpage-page__img')]
                    .filter(img => img.complete && img.naturalWidth > 0).length,
                  controls: document.querySelectorAll('button[aria-label^="Go to page "]').length
                })""") or {}
                count = int(current.get("count") or 0)
                if count == previous_count and count == int(current.get("loaded") or 0):
                    stable += 1
                else:
                    stable = 0
                previous_count = count
                if stable >= 3:
                    break
            # Record the control count now so it survives even if _extract_candidates
            # throws below (the missing-page accounting depends on it).
            scroll_diagnostics["page_control_count"] = int(current.get("controls") or 0)
            preload_stage = "extract_candidates"
            expanded, selector = _extract_candidates(page, url, events, cancel_check)
            discovered_elapsed_ms = (time.monotonic() - page_action_started) * 1000
            if page_discovery_elapsed_ms is not None:
                for item in expanded:
                    index = item.get("logical_page_index")
                    if index is not None:
                        page_discovery_elapsed_ms.setdefault(int(index), discovered_elapsed_ms)
            preload_stage = "promote"
            promoted_expanded = _promote_observed_response_bodies(
                expanded, pending_responses, browser_bodies, body_stats,
                previously_materialized_indices=previously_materialized_indices,
                promoted_resource_indices=promoted_resource_indices,
                page_body_elapsed_ms=page_body_elapsed_ms,
                response_seen_indices=response_seen_indices,
                body_candidate_indices=body_candidate_indices,
                reader_image_hosts=reader_image_hosts,
                requested_count=requested_count)
            candidates = (promoted_expanded if len(promoted_expanded) >= len(candidates)
                          else candidates)
            scroll_diagnostics.update({
                "preload_mode_discovery": str(current.get("checked") or "unknown"),
                "preload_all_dom_count": len(expanded),
                "preload_all_loaded_count": int(current.get("loaded") or 0),
            })
    except Exception as exc:
        # Record the preload failure with a safe, path-free diagnostic.  Crucially we
        # neither close Settings here nor re-raise: closing is the finally's job, and
        # the fail-closed gate below owns the abort decision.
        scroll_diagnostics.update({
            "preload_mode_discovery": "failed",
            "preload_error": type(exc).__name__,
            "preload_exception_type": type(exc).__name__,
            "preload_exception_message": re.sub(r"\s+", " ", str(exc)).strip()[:200],
            "preload_exception_stage": preload_stage,
        })
    finally:
        # Close the Settings surface whether preload succeeded or threw.  The helper is
        # idempotent (no-op when no modal is present); a close failure is recorded but
        # never masks the original preload exception path.
        scroll_diagnostics["settings_close_finally_entered"] = True
        try:
            settings_gate = _close_reader_settings(page)
            before_state = settings_gate.get("before") or {}
            scroll_diagnostics["settings_close_helper_called"] = True
            scroll_diagnostics["settings_close_clicked"] = _overlay_blocks_capture(before_state)
            scroll_diagnostics["settings_modal_selector"] = str(
                before_state.get("settings_selector") or "")
            scroll_diagnostics["settings_modal_visible_during_capture"] = not bool(
                settings_gate.get("closed"))
        except Exception as close_exc:
            scroll_diagnostics["settings_close_helper_called"] = True
            scroll_diagnostics["settings_close_exception_type"] = type(close_exc).__name__
            scroll_diagnostics["settings_close_exception_message"] = (
                re.sub(r"\s+", " ", str(close_exc)).strip()[:200])
    # Fail-closed overlay gate — OUTSIDE the try/except/finally so it can never be
    # swallowed by the preload except.  A visible Settings/blocking overlay would
    # contaminate every downstream canonical capture, so refuse to continue.
    overlay_after = _read_reader_overlay_state(page)
    blocked = _overlay_blocks_capture(overlay_after)
    scroll_diagnostics["settings_visible_after_finally"] = blocked
    scroll_diagnostics["overlay_gate_after_preload"] = "FAIL" if blocked else "PASS"
    if blocked:
        raise DynamicReaderError("reader_overlay_visible")
    return candidates, selector


def _capture_rendered_canvas_candidate(
    page: Any, page_index: int, state: dict[str, Any],
    diagnostic: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Materialize a Canvas page from a fresh headless reader frame.

    The isolated Canvas surface can remain stale after Comix reuses a reader node.  The
    authoritative source is therefore the current reader viewport, cropped only by the
    target DOM geometry.  If the page cannot fit without changing horizontal layout, or
    if the crop would leave the screenshot bounds, this fails closed.
    """
    expected_width = int(state.get("canvas_width") or state.get("width") or 0)
    expected_height = int(state.get("canvas_height") or state.get("height") or 0)
    def failed(reason: str) -> None:
        if diagnostic is not None:
            diagnostic["reason"] = reason
    def capture_element() -> bytes | None:
        # The visible viewport can clip a valid, tall reader canvas even after the
        # target was scrolled into view. Playwright's element screenshot captures
        # exactly this reader-owned render node, without capturing adjacent pages.
        selector = (
            f'main.rpage-main [data-page="{int(page_index)}"] '
            'canvas.rpage-page__img, '
            f'main.rpage-main [data-page="{int(page_index)}"] '
            'img.rpage-page__img'
        )
        try:
            locator = page.locator(selector)
            if int(locator.count()) != 1:
                return None
            return bytes(locator.screenshot(type="png", timeout=5000))
        except Exception:
            return None
    geometry_script = """(index) => {
      const reader = document.querySelector('main.rpage-main');
      const target = reader?.querySelector('[data-page="' + index + '"]');
      const render = target?.querySelector('canvas.rpage-page__img') ||
        target?.querySelector('img.rpage-page__img');
      if (!reader || !target || !render) return null;
      const rr = reader.getBoundingClientRect();
      const tr = render.getBoundingClientRect();
      return {
        reader: {x: rr.x, y: rr.y, width: rr.width, height: rr.height},
        target: {x: tr.x, y: tr.y, width: tr.width, height: tr.height},
        viewport: {width: innerWidth, height: innerHeight},
        visual: {x: visualViewport?.offsetLeft || 0, y: visualViewport?.offsetTop || 0},
        dpr: devicePixelRatio,
        native: {width: Number(render.naturalWidth || render.width || 0),
          height: Number(render.naturalHeight || render.height || 0)}
      };
    }"""
    try:
        geometry = page.evaluate(geometry_script, page_index) or {}
        if not geometry:
            failed("geometry_missing")
            return None
        expected_width = expected_width or int(geometry["native"]["width"] or 0)
        expected_height = expected_height or int(geometry["native"]["height"] or 0)
        viewport = geometry["viewport"]
        target = geometry["target"]
        dpr = float(geometry.get("dpr") or 1)
        required_height = int(target["height"] + 24)
        if int(viewport["height"]) < required_height:
            setter = getattr(page, "set_viewport_size", None)
            if not callable(setter):
                failed("viewport_resize_unavailable")
                return None
            setter({"width": int(viewport["width"]), "height": required_height})
            page.wait_for_timeout(80)
            page.evaluate("""() => new Promise(resolve => {
              requestAnimationFrame(() => requestAnimationFrame(resolve));
            })""")
            geometry = page.evaluate(geometry_script, page_index) or {}
            if not geometry:
                failed("geometry_missing_after_resize")
                return None
            target = geometry["target"]
            viewport = geometry["viewport"]
            dpr = float(geometry.get("dpr") or dpr)
        tolerance = 1.0
        element_capture_used = False
        if (target["x"] < -tolerance or target["y"] < -tolerance or
                target["x"] + target["width"] > viewport["width"] + tolerance or
                target["y"] + target["height"] > viewport["height"] + tolerance):
            image_bytes = capture_element()
            if image_bytes is None:
                failed("target_outside_viewport")
                return None
            element_capture_used = True
        else:
            image_bytes = page.screenshot(type="png")
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(image_bytes)) as source:
            source = source.convert("RGBA")
            source_width, source_height = source.size
            if element_capture_used:
                cropped = source
            else:
                visual = geometry.get("visual") or {"x": 0, "y": 0}
                left = round((target["x"] - float(visual.get("x") or 0)) * dpr)
                top = round((target["y"] - float(visual.get("y") or 0)) * dpr)
                right = round((target["x"] + target["width"] - float(visual.get("x") or 0)) * dpr)
                bottom = round((target["y"] + target["height"] - float(visual.get("y") or 0)) * dpr)
                if left < 0 or top < 0 or right > source_width or bottom > source_height:
                    image_bytes = capture_element()
                    if image_bytes is None:
                        failed("crop_outside_screenshot")
                        return None
                    element_capture_used = True
                    with Image.open(BytesIO(image_bytes)) as element_image:
                        cropped = element_image.convert("RGBA")
                else:
                    cropped = source.crop((left, top, right, bottom))
            crop_width, crop_height = cropped.size
            if not crop_width or not crop_height:
                failed("empty_crop")
                return None
            if expected_width and expected_height:
                crop_aspect = crop_width / crop_height
                native_aspect = expected_width / expected_height
                if abs(crop_aspect - native_aspect) > 0.01:
                    failed("crop_aspect_mismatch")
                    return None
                if (crop_width < expected_width or crop_height < expected_height) and not element_capture_used:
                    failed("crop_below_native_size")
                    return None
                if (crop_width, crop_height) != (expected_width, expected_height):
                    cropped = cropped.resize((expected_width, expected_height), Image.Resampling.LANCZOS)
            encoded = BytesIO()
            cropped.save(encoded, format="PNG")
            image_bytes = encoded.getvalue()
    except Exception as exc:
        failed(f"capture_exception_{type(exc).__name__[:48]}")
        return None
    if len(image_bytes) < 24 or image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        failed("invalid_png_capture")
        return None
    capture_width, capture_height = struct.unpack(">II", image_bytes[16:24])
    return {
        "url": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
        "source": "canvas_capture",
        "origin": "logical_page_navigation",
        "order": page_index - 1,
        "logical_page_index": page_index,
        "y": page_index,
        "width": expected_width,
        "height": expected_height,
        "naturalWidth": expected_width,
        "naturalHeight": expected_height,
        "alt": f"Page {page_index}",
        "className": "rpage-page__img",
        "container": "comix-reader",
        "context": "reader",
        "visible": True,
        "canvas_data": image_bytes,
        "capture_method": ("reader_element_screenshot" if element_capture_used
                            else "headless_dynamic_viewport_crop"),
        "capture_width": capture_width,
        "capture_height": capture_height,
    }


def _should_read_response_body(
    *, resource_type: str, resource_url: str,
    content_type_header: str | None, content_length_header: str | None,
    already_captured: bool, reader_observed: bool = False,
) -> tuple[bool, str]:
    """Decide, from cheap metadata, whether to pull a network response body.

    Reading a body (``response.body()``) copies the full image bytes out of the browser
    and is the single largest cost of the resolver; most of those bytes never become a
    canonical page.  This gate skips ONLY bodies that ``_promote_browser_response_candidate``
    would reject anyway, so it can never drop a page that could have been promoted:

    * an URL not observed on the selected reader's page-image nodes;
    * a response whose URL was already captured (an identical CDN image -> ``dedup``);
    * a declared ``content-length`` over the promotion cap (``oversize``, rejected at 692);
    * a declared ``content-type`` that is not an allowed browser image (``content_type``,
      rejected at 695).

    A missing header is never a reason to skip (we read and let promotion decide).  Returns
    ``(read, skip_reason)`` where ``skip_reason`` is ``""`` when ``read`` is True and
    ``not_page_image`` for a resource outside the exact HTTPS URLs observed in the reader.
    """
    parsed = urlparse(str(resource_url or ""))
    if (resource_type != "image" or parsed.scheme.casefold() != "https"
            or not reader_observed):
        return False, "not_page_image"
    if already_captured:
        return False, "dedup"
    declared_type = str(content_type_header or "").split(";", 1)[0].strip().casefold()
    if declared_type and declared_type not in ALLOWED_BROWSER_IMAGE_CONTENT_TYPES:
        return False, "content_type"
    try:
        declared_length = int(content_length_header) if content_length_header else 0
    except (TypeError, ValueError):
        declared_length = 0
    if declared_length > MAX_BROWSER_RESPONSE_BODY_BYTES:
        return False, "oversize"
    return True, ""


def _promote_observed_response_bodies(
    candidates: list[dict[str, Any]], pending_responses: dict[str, dict[str, Any]],
    browser_bodies: dict[str, dict[str, Any]], body_stats: dict[str, Any],
    *, previously_materialized_indices: set[int] | None = None,
    promoted_resource_indices: dict[str, int] | None = None,
    page_body_elapsed_ms: dict[int, float] | None = None,
    reader_image_hosts: set[str] | None = None,
    requested_count: int | None = None,
    response_seen_indices: set[int] | None = None,
    body_candidate_indices: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Read bodies lazily, only after an exact HTTPS image URL is observed on the
    selected Comix reader page nodes. Response callbacks stay metadata-only/nonblocking.
    """
    observed = {
        str(item.get("url") or ""): item for item in candidates
        if str(item.get("context") or "") == "reader"
        and str(item.get("container") or "") == "comix-reader"
        and item.get("logical_page_index") is not None
        and (requested_count is None or int(item.get("logical_page_index")) <= requested_count)
    }
    observed_urls = set(observed)
    already_materialized = previously_materialized_indices or set()
    promoted_by_url: dict[str, dict[str, Any]] = {}
    for resource_url in sorted(observed_urls):
        page_index = observed[resource_url].get("logical_page_index")
        if page_index is None and promoted_resource_indices is not None:
            page_index = promoted_resource_indices.get(resource_url)
        if reader_image_hosts is not None:
            host = _safe_host(resource_url)
            if host:
                reader_image_hosts.add(host)
        if page_index is not None and int(page_index) in already_materialized:
            pending_responses.pop(resource_url, None)
            body_stats["skip_dedup"] = body_stats.get("skip_dedup", 0) + 1
            continue
        response_info = pending_responses.pop(resource_url, None)
        cached_body = browser_bodies.get(resource_url)
        if not response_info and not cached_body:
            continue
        if page_index is not None and response_seen_indices is not None:
            response_seen_indices.add(int(page_index))
        if page_index is not None and body_candidate_indices is not None:
            body_candidate_indices.add(int(page_index))
        if cached_body:
            body = cached_body.get("body", b"")
            content_type = str(cached_body.get("content_type") or "")
        else:
            read, reason = _should_read_response_body(
                resource_type=response_info.get("resource_type", ""),
                resource_url=resource_url,
                content_type_header=response_info.get("content_type"),
                content_length_header=response_info.get("content_length"),
                already_captured=False,
                reader_observed=True,
            )
            if not read:
                key = f"skip_{reason}"
                body_stats[key] = body_stats.get(key, 0) + 1
                continue
            body_stats["read_total"] += 1
            started = time.monotonic()
            try:
                body = response_info["response"].body()
                read_ms = (time.monotonic() - started) * 1000
                body_stats["last_read_ms"] = read_ms
                body_stats["read_ms"] += read_ms
                if 0 < len(body) <= MAX_BROWSER_RESPONSE_BODY_BYTES:
                    cached_body = {
                        "body": body,
                        "content_type": str(response_info.get("content_type") or ""),
                    }
                    browser_bodies[resource_url] = cached_body
                    body_stats["captured"] += 1
                else:
                    body_stats["read_failed"] += 1
                    body_stats["reject_empty_or_oversize"] += 1
                    continue
            except Exception:
                read_ms = (time.monotonic() - started) * 1000
                body_stats["last_read_ms"] = read_ms
                body_stats["read_ms"] += read_ms
                body_stats["read_failed"] += 1
                body_stats["reject_body_unavailable"] += 1
                continue
            content_type = str(cached_body.get("content_type") or "")
        matching = next((item for item in candidates if str(item.get("url") or "") == resource_url), None)
        if matching is None:
            continue
        promoted = _promote_browser_response_candidate(
            matching, body, content_type)
        if promoted is None:
            body_stats["reject_not_promotable"] += 1
            continue
        promoted_by_url[resource_url] = promoted
        body_stats["promoted"] += 1
        if page_index is not None and promoted_resource_indices is not None:
            promoted_resource_indices[resource_url] = int(page_index)
        if page_index is not None and page_body_elapsed_ms is not None:
            page_body_elapsed_ms[int(page_index)] = round(
                page_body_elapsed_ms.get(int(page_index), 0.0)
                + float(body_stats.get("last_read_ms") or 0), 1)
    return [promoted_by_url.get(str(item.get("url") or ""), item) for item in candidates]


def _promote_browser_response_candidate(
    raw: dict[str, Any], body: bytes, content_type: str | None = None
) -> dict[str, Any] | None:
    """Turn the browser's already-received IMG response into a local candidate.

    This never performs a request.  It is only called for a URL/currentSrc observed in
    the normal browser session, after the reader has established the logical page identity.
    """
    if not body or len(body) > MAX_BROWSER_RESPONSE_BODY_BYTES:
        return None
    source_content_type = str(content_type or "").split(";", 1)[0].strip().casefold()
    if source_content_type not in ALLOWED_BROWSER_IMAGE_CONTENT_TYPES:
        return None
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(body)) as decoded:
            decoded.load()
            width, height = (int(decoded.width), int(decoded.height))
            if not width or not height:
                return None
            if source_content_type == "image/png":
                canonical_body = body
            else:
                encoded = BytesIO()
                decoded.convert("RGBA").save(encoded, format="PNG")
                canonical_body = encoded.getvalue()
    except Exception:
        return None
    if len(canonical_body) < 24 or canonical_body[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    promoted = dict(raw)
    promoted["url"] = "data:image/png;base64," + base64.b64encode(canonical_body).decode("ascii")
    promoted["source"] = "browser_response_body"
    promoted["origin"] = "browser_response_body"
    promoted["capture_method"] = "browser_response_body"
    promoted["width"] = int(width)
    promoted["height"] = int(height)
    promoted["naturalWidth"] = int(width)
    promoted["naturalHeight"] = int(height)
    promoted["source_content_type"] = source_content_type
    promoted["canonical_content_type"] = "image/png"
    promoted["transcoded"] = source_content_type != "image/png"
    promoted["materialization_method"] = "browser_response_body"
    return promoted


def _read_reader_render_state(page: Any, page_index: int) -> dict[str, Any]:
    """Read logical identity and a cheap pixel fingerprint for one reader canvas."""
    script = """
    (index) => {
      const wanted = String(index);
      const controls = [...document.querySelectorAll('button[aria-label^="Go to page "]')];
      const active = controls.find((button) => {
        const aria = String(button.getAttribute('aria-current') || '').toLowerCase();
        const state = String(button.getAttribute('data-active') || '').toLowerCase();
        return aria === 'true' || aria === 'page' || state === 'true' ||
          button.classList.contains('active') || button.classList.contains('selected') ||
          button.classList.contains('is-active');
      });
      const activeMatch = String(active?.getAttribute('aria-label') || '').match(/(\\d+)\\s*$/);
      const reader = document.querySelector('main.rpage-main');
      const root = reader?.querySelector('[data-page="' + wanted + '"]');
      const canvas = root?.querySelector('canvas.rpage-page__img');
      const image = root?.querySelector('img.rpage-page__img');
      const render = canvas || image;
      const imagePageMatch = String(image?.getAttribute('alt') || '')
        .match(/(?:^|\\b)page\\s+(\\d+)(?:\\b|$)/i);
      const rootRect = root?.getBoundingClientRect();
      let fingerprint = '';
      if (canvas && canvas.width && canvas.height) {
        const context = canvas.getContext('2d', {willReadFrequently: true});
        if (context) {
          // Downsample once, then read one compact pixel buffer. Sampling hundreds of
          // 1x1 regions separately made every readiness poll issue hundreds of sync
          // Canvas IPC calls and dominated serial materialization time.
          const sampleWidth = 24;
          const sampleHeight = 32;
          const sample = document.createElement('canvas');
          sample.width = sampleWidth;
          sample.height = sampleHeight;
          const sampleContext = sample.getContext('2d', {willReadFrequently: true});
          sampleContext.drawImage(canvas, 0, 0, sampleWidth, sampleHeight);
          const pixels = sampleContext.getImageData(0, 0, sampleWidth, sampleHeight).data;
          let hash = 2166136261;
          for (let i = 0; i < pixels.length; i++) {
            hash ^= pixels[i];
            hash = Math.imul(hash, 16777619);
          }
          fingerprint = (hash >>> 0).toString(16);
        }
      } else if (image?.complete && image.naturalWidth && image.naturalHeight) {
        // IMG-mode pages are canonical reader output too. Keep this identity in memory
        // only; the source URL is never part of telemetry.
        fingerprint = String(image.currentSrc || image.src ||
          `${image.naturalWidth}x${image.naturalHeight}`);
      }
      const canvasLabel = String(canvas?.getAttribute('aria-label') || '');
      const labelMatch = canvasLabel.match(/(?:page|página)\\s*(\\d+)/i);
      return {
        requested_index: index,
        active_control_index: activeMatch ? Number(activeMatch[1]) : null,
        active_container_data_page: root ? Number(root.getAttribute('data-page')) : null,
        canvas_aria_label: canvasLabel,
        canvas_label_index: labelMatch ? Number(labelMatch[1]) : null,
        canvas_width: canvas?.width || image?.naturalWidth || 0,
        canvas_height: canvas?.height || image?.naturalHeight || 0,
        canvas_pixel_hash: fingerprint,
        fingerprint_sample_area: '24x32_grid',
        container_exists: !!root,
        container_visible: !!rootRect && rootRect.width > 0 && rootRect.height > 0 &&
          rootRect.bottom > 0 && rootRect.top < innerHeight,
        control_count: controls.length,
        requested_control_exists: controls.some(button =>
          String(button.getAttribute('aria-label') || '') === 'Go to page ' + wanted),
        requested_control_disabled: controls.find(button =>
          String(button.getAttribute('aria-label') || '') === 'Go to page ' + wanted)?.disabled || false,
        canvas_count: root?.querySelectorAll('canvas.rpage-page__img').length || 0,
        page_root_tag: String(root?.tagName || '').slice(0, 24),
        page_root_class: String(root?.className || '').slice(0, 100),
        page_root_child_count: root?.childElementCount || 0,
        page_root_image_count: root?.querySelectorAll('img.rpage-page__img').length || 0,
        image_complete: !!image?.complete,
        image_natural_width: image?.naturalWidth || 0,
        image_natural_height: image?.naturalHeight || 0,
        image_page_number: imagePageMatch ? Number(imagePageMatch[1]) : null,
        image_src_present: !!(image?.currentSrc || image?.src),
        image_source_url: String(image?.currentSrc || image?.src || ''),
        canvas_in_requested_container: !!render,
        render_kind: canvas ? 'canvas' : image ? 'img' : 'none',
        render_settled: !!root && !!render && !root.classList.contains('is-loading') && !!fingerprint
      };
    }
    """
    state = page.evaluate(script, page_index) or {}
    if not isinstance(state, dict):
        return {}
    # Do not screenshot here: Playwright may scroll the virtualized reader while
    # taking an element screenshot, changing the active logical page underneath
    # the lifecycle probe.  The backing-store fingerprint is side-effect free.
    state["fingerprint_source"] = "canvas_backing_pixels"
    return state


def _navigate_reader_to_page(page: Any, page_index: int, *, dom_click: bool = False) -> dict[str, Any]:
    """Use the visible Comix page control normally, then a bounded targeted scroll."""
    selector = f'button[aria-label="Go to page {int(page_index)}"]'
    result: dict[str, Any] = {"control_found": False, "click_method": "none", "click_error": ""}
    try:
        control = page.locator(selector)
        count = int(control.count())
        result["control_found"] = count > 0
        if count:
            if dom_click:
                page.evaluate("""(index) => {
                  const button = document.querySelector(
                    'button[aria-label="Go to page ' + index + '"]');
                  if (button) button.click();
                }""", int(page_index))
                result["click_method"] = "reader_dom_click_retry"
            else:
                try:
                    control.first.scroll_into_view_if_needed(timeout=1200)
                    control.first.click(timeout=1800)
                    result["click_method"] = "playwright_control_click"
                except Exception as exc:
                    result["click_error"] = type(exc).__name__[:60]
                    try:
                        page.evaluate("""(index) => {
                          const button = document.querySelector(
                            'button[aria-label="Go to page ' + index + '"]');
                          if (button) button.click();
                        }""", int(page_index))
                        result["click_method"] = "reader_dom_click_fallback"
                    except Exception as fallback_exc:
                        result["click_error"] = type(fallback_exc).__name__[:60]
            # Some pages are absent from the reader's active preload window until the
            # actual page container intersects the viewport. Scroll only this target.
            page.evaluate("""(index) => {
              const target = document.querySelector('main.rpage-main')
                ?.querySelector('[data-page="' + index + '"]');
              if (target) target.scrollIntoView({block:'center', inline:'nearest'});
            }""", int(page_index))
            result["click_method"] += "+targeted_scroll"
        else:
            # A virtualized page/control may be attached only after the corresponding
            # reader container is brought into view. This scroll is limited to one index.
            page.evaluate("""(index) => {
              const target = document.querySelector('main.rpage-main')
                ?.querySelector('[data-page="' + index + '"]');
              if (target) target.scrollIntoView({block:'center', inline:'nearest'});
            }""", int(page_index))
            result["click_method"] = "targeted_page_scroll"
    except Exception as exc:
        result["click_error"] = type(exc).__name__[:60]
    return result


def _scroll_normal_reader(page: Any, selector: str, initial: list[dict[str, Any]],
                          cancel_check: Callable[[], bool] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Walk the public reader normally and accumulate pages across reused DOM slots."""
    seen: dict[str, dict[str, Any]] = {}
    first_state = _read_dom_state(page, selector)
    if first_state is None:
        return initial, {"scroll_iterations": 0, "initial_slot_count": len(initial)}

    def add_rows(rows: list[dict[str, Any]]) -> int:
        added = 0
        for row in rows:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            identity = url.split("#", 1)[0]
            if identity not in seen:
                item = dict(row)
                item["url"] = url
                item["source"] = "scrapling_dom"
                item["origin"] = "dynamic_dom"
                item["order"] = len(seen)
                seen[identity] = item
                added += 1
        return added

    add_rows(first_state.get("rows", []))
    initial_slots = int(first_state.get("slot_count") or len(initial))
    initial_urls = tuple(seen)
    lazy_attributes = set(first_state.get("lazy_attributes") or [])
    scroll_iterations = 0
    stable_rounds = 0
    last_target = -1

    for _ in range(MAX_SCROLL_STEPS):
        if cancel_check and cancel_check():
            raise DynamicReaderError("cancelled")
        state = _read_dom_state(page, selector)
        if state is None:
            break
        lazy_attributes.update(state.get("lazy_attributes") or [])
        added = add_rows(state.get("rows", []))
        bottom = max(
            int(state.get("reader_bottom") or 0),
            int(state.get("document_height") or 0),
        )
        current = int(state.get("scroll_y") or 0)
        viewport = max(400, int(state.get("viewport_height") or 800))
        target = max(current, min(bottom, current + int(viewport * 0.85)))
        at_bottom = bool(state.get("at_bottom")) or target <= current or target >= bottom - 8
        if added == 0 and at_bottom:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= SCROLL_STABLE_ROUNDS:
            break
        if target == last_target and at_bottom:
            break
        last_target = target
        try:
            page.evaluate("(y) => window.scrollTo(0, y)", target)
            page.wait_for_timeout(300)
        except Exception as exc:
            raise DynamicReaderError("browser_action_failed") from exc
        scroll_iterations += 1

    final_state = _read_dom_state(page, selector) or first_state
    lazy_attributes.update(final_state.get("lazy_attributes") or [])
    add_rows(final_state.get("rows", []))
    diagnostics = {
        "scroll_iterations": scroll_iterations,
        "initial_slot_count": initial_slots,
        "final_slot_count": int(final_state.get("slot_count") or initial_slots),
        "initial_match_count": len(initial_urls),
        "unique_page_urls_seen": len(seen),
        "dom_nodes_reused": bool(initial_slots == int(final_state.get("slot_count") or initial_slots)
                                  and len(seen) > initial_slots),
        "image_urls_change_while_slots_stay_constant": bool(
            initial_slots == int(final_state.get("slot_count") or initial_slots)
            and len(seen) > initial_slots),
        "new_nodes_append_on_scroll": bool(int(final_state.get("slot_count") or initial_slots) > initial_slots),
        "lazy_attributes": sorted(lazy_attributes),
        "placeholder_count_initial": int(first_state.get("placeholder_count") or 0),
    }
    return list(seen.values()), diagnostics


def _resolve_inline(url: str, *, adapter: Any, cancel_check: Callable[[], bool] | None = None,
                    timeout: float = 30.0,
                    deadline_seconds: float | None = None,
                    max_pages: int | None = None) -> Any:
    """Run the selected reader in the disposable resolver child process."""
    if not supports_url(url):
        raise DynamicReaderError("site_not_selected")
    if _DynamicFetcher is None:
        _append_telemetry("dynamic_resolution_failed", status="error",
                          error_code="capability_unavailable")
        raise DynamicReaderError("capability_unavailable")
    try:
        adapter.validate_navigation_url(url)
        adapter.validate_path(url)
    except Exception as exc:
        raise DynamicReaderError("navigation_rejected") from exc
    events: list[dict[str, Any]] = []
    trace_id = uuid.uuid4().hex
    requested_count = (max(1, int(max_pages)) if max_pages is not None else None)
    browser_bodies: dict[str, dict[str, Any]] = {}
    pending_responses: dict[str, dict[str, Any]] = {}
    # Sanitized utility counters. Response callbacks retain only bounded metadata and
    # handles; body() is called later only for an exact URL observed in the Comix reader.
    body_stats: dict[str, Any] = {
        "captured": 0, "read_total": 0, "read_ms": 0.0, "read_failed": 0,
        "promoted": 0, "reject_empty_or_oversize": 0, "reject_body_unavailable": 0,
        "reject_not_promotable": 0, "skip_dedup": 0, "skip_content_type": 0,
        "skip_oversize": 0, "skip_not_page_image": 0,
    }
    page_discovery_elapsed_ms: dict[int, float] = {}
    preserved_candidates: list[dict[str, Any]] = []
    promoted_resource_indices: dict[str, int] = {}
    page_body_elapsed_ms: dict[int, float] = {}
    response_seen_indices: set[int] = set()
    body_candidate_indices: set[int] = set()
    page_stage_metrics: dict[int, dict[str, float | None]] = {}
    reader_image_hosts: set[str] = set()
    result_holder: dict[str, Any] = {}
    started = time.monotonic()
    effective_deadline = (
        DYNAMIC_RESOLVER_DEADLINE_SECONDS
        if deadline_seconds is None else max(0.0, float(deadline_seconds))
    )
    deadline = started + effective_deadline

    def remaining_seconds(stage: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _append_telemetry(
                "dynamic_fetch_stage_timeout", status="timeout", stage=stage,
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            raise DynamicReaderError("dynamic_resolver_deadline_exceeded", stage)
        return remaining

    def stage_start(stage: str) -> float:
        remaining = remaining_seconds(stage)
        _append_telemetry(
            "dynamic_fetch_stage_start", status="started", stage=stage,
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            remaining_ms=max(1, int(remaining * 1000)),
        )
        return time.monotonic()

    def stage_end(stage: str, stage_started: float) -> None:
        _append_telemetry(
            "dynamic_fetch_stage_end", status="completed", stage=stage,
            elapsed_ms=round((time.monotonic() - stage_started) * 1000, 1),
        )
        remaining_seconds(stage)

    def body_telemetry() -> dict[str, Any]:
        reads = int(body_stats.get("read_total") or 0)
        promoted = int(body_stats.get("promoted") or 0)
        rejected = max(0, reads - promoted)
        return {
            "body_read_total": reads,
            "body_read_ms": round(float(body_stats.get("read_ms") or 0), 1),
            "body_promoted_total": promoted,
            "body_rejected_total": rejected,
            "body_promotion_rate": round(promoted / reads, 4) if reads else 0.0,
            "body_capture_count": int(body_stats.get("captured") or 0),
            "body_read_failed": int(body_stats.get("read_failed") or 0),
            "body_skip_dedup": int(body_stats.get("skip_dedup") or 0),
            "body_skip_content_type": int(body_stats.get("skip_content_type") or 0),
            "body_skip_oversize": int(body_stats.get("skip_oversize") or 0),
            "reader_image_host_count": len(reader_image_hosts),
        }

    def page_setup(page: Any) -> None:
        setup_started = stage_start("page_setup")
        try:
            page.add_init_script("""
            (() => {
              const state = window.__yomuCanvasDiag = {
                draws: [], intersections: [], contexts: [], nav: null,
                epoch: 0
              };
              const compact = (canvas, method) => {
                const parent = canvas?.closest?.('[data-page]');
                const active = [...document.querySelectorAll('button[aria-label^="Go to page "]')]
                  .find(button => button.classList.contains('is-active') ||
                    button.classList.contains('active') || button.getAttribute('aria-current'));
                const match = String(active?.getAttribute('aria-label') || '').match(/(\\d+)\\s*$/);
                return {
                  t: performance.now(), method, epoch: state.epoch,
                  requested_index: state.nav?.index ?? null,
                  canvas_class: String(canvas?.className || '').slice(0, 120),
                  aria_label: String(canvas?.getAttribute?.('aria-label') || '').slice(0, 40),
                  data_page: parent ? Number(parent.getAttribute('data-page')) : null,
                  width: Number(canvas?.width || 0), height: Number(canvas?.height || 0),
                  active_index: match ? Number(match[1]) : null,
                };
              };
              const wrap = (proto, name) => {
                const original = proto?.[name];
                if (!original || original.__yomuWrapped) return;
                const wrapped = function(...args) {
                  try { if (state.draws.length < 2000) state.draws.push(compact(this.canvas, name)); }
                  catch (_) {}
                  return original.apply(this, args);
                };
                wrapped.__yomuWrapped = true;
                proto[name] = wrapped;
              };
              wrap(CanvasRenderingContext2D.prototype, 'drawImage');
              wrap(CanvasRenderingContext2D.prototype, 'putImageData');
              wrap(CanvasRenderingContext2D.prototype, 'clearRect');
              wrap(CanvasRenderingContext2D.prototype, 'fillRect');
              const originalGetContext = HTMLCanvasElement.prototype.getContext;
              HTMLCanvasElement.prototype.getContext = function(type, ...args) {
                try { if (state.contexts.length < 1000) state.contexts.push(String(type || '')); }
                catch (_) {}
                return originalGetContext.call(this, type, ...args);
              };
              const OriginalObserver = window.IntersectionObserver;
              if (OriginalObserver) {
                window.IntersectionObserver = class extends OriginalObserver {
                  constructor(callback, options) {
                    super((entries, observer) => {
                      try { for (const entry of entries) {
                        const page = entry.target?.getAttribute?.('data-page');
                        if (state.intersections.length < 2000)
                          state.intersections.push({t: performance.now(), page: page ? Number(page) : null,
                            is_intersecting: !!entry.isIntersecting, ratio: Number(entry.intersectionRatio || 0)});
                      }} catch (_) {}
                      return callback(entries, observer);
                    }, options);
                  }
                };
              }
              state.markNavigation = (index) => {
                state.epoch += 1;
                state.nav = {index, t: performance.now(), epoch: state.epoch, draws: state.draws.length};
                return state.nav;
              };
            })();
            """)
        except Exception:
            pass
        def on_response(response: Any) -> None:
            try:
                resource_url = str(response.url)
                resource_type = str(getattr(response, "request", None).resource_type
                                    if getattr(response, "request", None) else "other")
                if (("/api/v1/chapters/" in resource_url or resource_type == "image")
                        and len(events) < MAX_NETWORK_EVENTS):
                    events.append(_sanitized_network_event(resource_url, resource_type,
                                                            int(response.status)))
                try:
                    response_headers = response.headers or {}
                except Exception:
                    response_headers = {}
                parsed_url = urlparse(resource_url)
                content_type = str(response_headers.get("content-type") or "")
                if (resource_type == "image" and parsed_url.scheme.casefold() == "https"
                        and int(response.status) == 200
                        and len(pending_responses) < MAX_PAGES * 2
                        and resource_url not in pending_responses
                        and resource_url not in browser_bodies):
                    pending_responses[resource_url] = {
                        "response": response,
                        "resource_type": resource_type,
                        "content_type": content_type,
                        "content_length": response_headers.get("content-length"),
                    }
            except Exception:
                return
        try:
            page.on("response", on_response)
        except Exception as exc:
            raise DynamicReaderError("network_observer_unavailable") from exc
        stage_end("page_setup", setup_started)

    def page_action(page: Any) -> None:
        nonlocal preserved_candidates
        page_action_started = time.monotonic()
        action_started = stage_start("page_action_initial_wait")
        if cancel_check and cancel_check():
            raise DynamicReaderError("cancelled")
        try:
            page.wait_for_timeout(1200)
            page.wait_for_timeout(400)
        except Exception as exc:
            raise DynamicReaderError("browser_action_failed") from exc
        stage_end("page_action_initial_wait", action_started)
        discovery_started = stage_start("reader_discovery_and_preload")
        candidates, selector = _extract_candidates(page, url, events, cancel_check)
        discovered_elapsed = (time.monotonic() - page_action_started) * 1000
        for item in candidates:
            index = item.get("logical_page_index")
            if index is not None:
                page_discovery_elapsed_ms.setdefault(int(index), discovered_elapsed)
        previously_materialized_indices = {
            int(item["logical_page_index"])
            for item in preserved_candidates
            if item.get("logical_page_index") is not None
            and str(item.get("source") or "") != "scrapling_dom"
        }
        candidates = _promote_observed_response_bodies(
            candidates, pending_responses, browser_bodies, body_stats,
            previously_materialized_indices=previously_materialized_indices,
            promoted_resource_indices=promoted_resource_indices,
            page_body_elapsed_ms=page_body_elapsed_ms,
            reader_image_hosts=reader_image_hosts,
            requested_count=requested_count,
            response_seen_indices=response_seen_indices,
            body_candidate_indices=body_candidate_indices)
        scroll_diagnostics = {
            "initial_dom_count": len(candidates),
            "initial_slot_count": len(candidates),
            "preload_mode_initial": "unknown",
            "preload_mode_discovery": "not_attempted",
            "preload_control_found": False,
            "reading_direction": "unknown",
        }
        # The public reader deliberately defaults to a virtualized "Preload some"
        # window.  Switch to "Preload all" via the reader's own public control, always
        # close the Settings surface (finally), then fail closed if any overlay remains.
        # No API body is replayed, decrypted, or used as a source of truth.
        candidates, selector = _configure_preload_all(
            page, candidates=candidates, selector=selector, url=url,
            events=events, cancel_check=cancel_check,
            browser_bodies=browser_bodies, scroll_diagnostics=scroll_diagnostics,
            pending_responses=pending_responses, body_stats=body_stats,
            previously_materialized_indices=previously_materialized_indices,
            promoted_resource_indices=promoted_resource_indices,
            page_body_elapsed_ms=page_body_elapsed_ms,
            page_discovery_elapsed_ms=page_discovery_elapsed_ms,
            response_seen_indices=response_seen_indices,
            body_candidate_indices=body_candidate_indices,
            page_action_started=page_action_started,
            reader_image_hosts=reader_image_hosts,
            requested_count=requested_count,
        )
        if requested_count is not None:
            candidates = [
                item for item in candidates
                if item.get("logical_page_index") is not None
                and 1 <= int(item["logical_page_index"]) <= requested_count
            ]
        discovered_elapsed = (time.monotonic() - page_action_started) * 1000
        for item in candidates:
            index = item.get("logical_page_index")
            if index is not None:
                page_discovery_elapsed_ms.setdefault(int(index), discovered_elapsed)
        if requested_count is None and len(candidates) <= 3:
            candidates, walked = _scroll_normal_reader(
                page, selector, candidates, cancel_check)
            scroll_diagnostics.update(walked)
            scroll_diagnostics["discovery_strategy"] = "dom_scroll_accumulation"
        else:
            scroll_diagnostics["discovery_strategy"] = "public_reader_preload_all_dom"
        stage_end("reader_discovery_and_preload", discovery_started)
        scroll_diagnostics.update({
            "public_page_list_found": False,
            "public_page_list_count": 0,
            "page_control_count": int(scroll_diagnostics.get("page_control_count") or 0),
            "preload_all_resource_count": sum(
                1 for event in events if event.get("resource_type") == "image"
            ),
            "reader_page_resource_count": len(candidates),
            "normalized_page_count": len(candidates),
        })
        control_count = int(scroll_diagnostics.get("page_control_count") or 0)
        candidates, missing = _merge_materialized_attempts(
            preserved_candidates, candidates, control_count, requested_count)
        logical_indices = sorted({
            int(item["logical_page_index"])
            for item in candidates if item.get("logical_page_index") is not None
        })
        scroll_diagnostics["logical_page_indices"] = logical_indices
        scroll_diagnostics["missing_page_indices"] = missing
        fallback_candidates: list[dict[str, Any]] = []
        fallback_attempts: dict[int, list[str]] = {}
        fallback_attempt_diagnostics: dict[str, dict[str, Any]] = {}
        per_page_budget = MISSING_PAGE_BUDGET_SECONDS
        chapter_budget_seconds = _missing_chapter_budget_seconds(len(missing))
        chapter_deadline = min(time.monotonic() + chapter_budget_seconds, deadline)
        scroll_diagnostics["fallback_budget_seconds"] = chapter_budget_seconds
        scroll_diagnostics["fallback_per_page_budget_seconds"] = per_page_budget
        page_elapsed_ms: dict[str, float] = {}
        page_nav_diagnostics: dict[int, dict[str, Any]] = {}
        if missing:
            materialization_started = stage_start("missing_page_materialization")
            for page_index in missing:
                remaining_seconds("missing_page_materialization")
                if cancel_check and cancel_check():
                    raise DynamicReaderError("cancelled")
                if time.monotonic() >= chapter_deadline:
                    # Chapter safety ceiling reached.  Remaining pages stay unresolved and
                    # are enforced fail-closed after the loop — never aborted silently, and
                    # never charged to the pages we already resolved.
                    scroll_diagnostics["fallback_chapter_ceiling_hit"] = True
                    break
                # A fresh, bounded budget per page: a slow page cannot consume the budget
                # meant for the pages after it.
                page_started = time.monotonic()
                page_metrics: dict[str, float | None] = {
                    "navigation_ms": None, "render_wait_ms": None, "canvas_capture_ms": 0.0}
                page_deadline = min(page_started + per_page_budget, chapter_deadline)
                attempts: list[str] = []
                page_capture_failure_reason = ""
                navigation_action: dict[str, Any] = {
                    "control_found": False, "click_method": "none", "click_error": ""}
                last_state: dict[str, Any] = {}
                materialized_from_body: dict[str, Any] | None = None
                for attempt in range(1, MAX_CANONICAL_ATTEMPTS + 1):
                    remaining_seconds("missing_page_materialization")
                    try:
                        _read_reader_render_state(page, page_index)
                        # A retry starts a fresh logical/render/network epoch.  The page
                        # is deliberately re-navigated; no stale canvas or body is reused.
                        try:
                            navigation_started = time.monotonic()
                            page.evaluate("(index) => window.__yomuCanvasDiag?.markNavigation(index)", page_index)
                        except Exception:
                            pass
                        navigation_action = _navigate_reader_to_page(
                            page, page_index, dom_click=(attempt > 1))
                        last_state = _read_reader_render_state(page, page_index)
                        # Comix can reopen the settings surface when the virtualized
                        # reader jumps to a missing logical page.  Close it again
                        # before evaluating navigation/readiness predicates.
                        navigation_overlay = _close_reader_settings(page)
                        if not navigation_overlay.get("closed"):
                            readiness_stage = "OVERLAY_GATE"
                        loaded = False
                        stable_count = 0
                        last_hash = ""
                        readiness_stage = "NAVIGATION"
                        for _ in range(40):
                            remaining_seconds("missing_page_materialization")
                            if time.monotonic() >= page_deadline:
                                readiness_stage = "READINESS_WATCHDOG"
                                break
                            page.wait_for_timeout(100)
                            state = _read_reader_render_state(page, page_index)
                            lifecycle = page.evaluate(
                                """(index) => {
                                  const d = window.__yomuCanvasDiag || {};
                                  const nav = d.nav || {};
                                  const draws = (d.draws || []).filter(item =>
                                    Number(item.epoch || 0) === Number(nav.epoch || -1) &&
                                    Number(item.requested_index || 0) === Number(index) &&
                                    Number(item.data_page || 0) === Number(index));
                                  const last = draws[draws.length - 1];
                                  return {epoch: nav.epoch || null, draw_count: draws.length,
                                    first_draw_ms: draws.length ? draws[0].t - nav.t : null,
                                    last_draw_ms: last ? last.t - nav.t : null,
                                    no_draw_ms: last ? performance.now() - last.t : null};
                                }""", page_index) or {}
                            control_identity_ok = state.get("active_control_index") == page_index
                            visible_canvas_identity_ok = (
                                state.get("active_container_data_page") == page_index
                                and state.get("container_visible") is True
                                and state.get("canvas_in_requested_container") is True
                                and int(state.get("canvas_width") or 0) > 0
                                and int(state.get("canvas_height") or 0) > 0
                            )
                            identity_ok = (
                                (control_identity_ok or visible_canvas_identity_ok)
                                and state.get("active_container_data_page") == page_index
                                and state.get("canvas_in_requested_container") is True
                                and (not state.get("canvas_label_index")
                                     or state.get("canvas_label_index") == page_index)
                            )
                            last_state = state
                            if identity_ok and state.get("image_src_present"):
                                extracted_images, _ = _extract_candidates(
                                    page, url, events, cancel_check)
                                image_candidates = [item for item in extracted_images
                                    if item.get("logical_page_index") == page_index]
                                if not image_candidates:
                                    image_candidates = [item for item in extracted_images
                                        if str(item.get("url") or "")
                                        == str(state.get("image_source_url") or "")]
                                    if len(image_candidates) == 1:
                                        image_candidates[0]["logical_page_index"] = page_index
                                if image_candidates:
                                    promoted_images = _promote_observed_response_bodies(
                                        image_candidates, pending_responses, browser_bodies,
                                        body_stats,
                                        previously_materialized_indices=previously_materialized_indices,
                                        promoted_resource_indices=promoted_resource_indices,
                                        page_body_elapsed_ms=page_body_elapsed_ms,
                                        reader_image_hosts=reader_image_hosts,
                                        requested_count=requested_count,
                                        response_seen_indices=response_seen_indices,
                                        body_candidate_indices=body_candidate_indices)
                                    materialized_from_body = next((item for item in promoted_images
                                        if str(item.get("source") or "") == "browser_response_body"), None)
                                    if materialized_from_body is not None:
                                        fallback_candidates.append(materialized_from_body)
                                        attempts.append("pass")
                                        loaded = True
                                        break
                            if identity_ok and page_metrics["navigation_ms"] is None:
                                page_metrics["navigation_ms"] = round(
                                    (time.monotonic() - navigation_started) * 1000, 1)
                            overlay = _read_reader_overlay_state(page)
                            if _overlay_blocks_capture(overlay):
                                # Comix can reopen Settings when jumping to a missing
                                # logical page.  Re-close it (idempotent) and never allow
                                # canonical capture while any overlay is still visible.
                                readiness_stage = "OVERLAY_GATE"
                                _close_reader_settings(page)
                                continue
                            current_hash = str(state.get("canvas_pixel_hash") or "")
                            if identity_ok and current_hash and current_hash == last_hash:
                                stable_count += 1
                            else:
                                stable_count = 0
                            last_hash = current_hash
                            if not identity_ok:
                                readiness_stage = "NAVIGATION"
                            elif not state.get("render_settled"):
                                readiness_stage = "DRAW_WAIT"
                            elif stable_count < 1:
                                readiness_stage = "RAF_STABLE"
                            elif float(lifecycle.get("no_draw_ms") or 0) < 220 and int(
                                lifecycle.get("draw_count") or 0
                            ) > 0:
                                readiness_stage = "QUIESCENCE"
                            else:
                                readiness_stage = "FRAME_READY"
                            if (identity_ok and state.get("render_settled")
                                    and stable_count >= 1
                                    and (int(lifecycle.get("draw_count") or 0) == 0
                                         or float(lifecycle.get("no_draw_ms") or 0) >= 220)):
                                loaded = True
                                navigation_ms = float(page_metrics["navigation_ms"] or 0)
                                page_metrics["render_wait_ms"] = round(max(
                                    0.0, (time.monotonic() - navigation_started) * 1000
                                    - navigation_ms), 1)
                                break
                        if not loaded:
                            attempts.append("render_timeout")
                            fallback_attempt_diagnostics[str(page_index)] = {
                                "timeout_substage": readiness_stage,
                                "draw_hook_missed": bool(
                                    int(lifecycle.get("draw_count") or 0) == 0
                                ),
                            }
                            if time.monotonic() >= page_deadline:
                                # This page's own bounded budget is spent; stop retrying it
                                # and move on.  The page stays unresolved and is caught by
                                # the fail-closed final_missing check — never silently.
                                scroll_diagnostics["fallback_last_page_timeout_substage"] = readiness_stage
                                break
                            continue
                        if materialized_from_body is not None:
                            break
                        capture_started = time.monotonic()
                        page.evaluate(
                            """() => new Promise(resolve => {
                              let count = 0;
                              const frame = () => { count += 1; count >= 2 ? resolve(count) : requestAnimationFrame(frame); };
                              requestAnimationFrame(frame);
                            })"""
                        )
                        state = _read_reader_render_state(page, page_index)
                        capture_diag: dict[str, str] = {}
                        candidate = _capture_rendered_canvas_candidate(
                            page, page_index, state, diagnostic=capture_diag
                        )
                        page.evaluate("""() => new Promise(resolve => {
                          requestAnimationFrame(() => requestAnimationFrame(resolve));
                        })""")
                        stable_capture_diag: dict[str, str] = {}
                        stable_candidate = _capture_rendered_canvas_candidate(
                            page, page_index, _read_reader_render_state(page, page_index),
                            diagnostic=stable_capture_diag,
                        )
                        if candidate is None or stable_candidate is None:
                            page_capture_failure_reason = (
                                stable_capture_diag.get("reason")
                                or capture_diag.get("reason")
                                or "capture_returned_none"
                            )
                            attempts.append("capture_failed:" + page_capture_failure_reason)
                            continue
                        first_bytes = bytes(candidate.get("canvas_data") or b"")
                        second_bytes = bytes(stable_candidate.get("canvas_data") or b"")
                        if (
                            candidate.get("capture_width") != stable_candidate.get("capture_width")
                            or candidate.get("capture_height") != stable_candidate.get("capture_height")
                            or hashlib.sha256(first_bytes).digest()
                            != hashlib.sha256(second_bytes).digest()
                        ):
                            page_metrics["canvas_capture_ms"] = round(
                                float(page_metrics["canvas_capture_ms"] or 0)
                                + (time.monotonic() - capture_started) * 1000, 1)
                            page_capture_failure_reason = "frame_not_stable"
                            attempts.append("capture_failed:frame_not_stable")
                            continue
                        candidate = stable_candidate
                        candidate["render_diagnostics"] = page.evaluate(
                            """(index) => {
                              const d = window.__yomuCanvasDiag || {};
                              const nav = d.nav || {};
                              const draws = (d.draws || []).filter(item =>
                                Number(item.epoch || 0) === Number(nav.epoch || -1) &&
                                Number(item.requested_index || 0) === Number(index) &&
                                Number(item.data_page || 0) === Number(index));
                              const intersections = (d.intersections || []).filter(item =>
                                Number(item.t || 0) >= Number(nav.t || 0) && item.page === index);
                              return {
                                requested_index: index,
                                epoch: nav.epoch || null,
                                attempt_count: %d,
                                draw_call_count: draws.length,
                                first_draw_ms: draws.length ? Math.round(draws[0].t - nav.t) : null,
                                last_draw_ms: draws.length ? Math.round(draws[draws.length - 1].t - nav.t) : null,
                                intersection_callback_seen: intersections.length > 0,
                                intersection_events: intersections.length,
                                contexts: Array.from(new Set(d.contexts || [])).slice(0, 8)
                              };
                            }""" % attempt,
                            page_index,
                        ) or {}
                        fallback_candidates.append(candidate)
                        page_metrics["canvas_capture_ms"] = round(
                            float(page_metrics["canvas_capture_ms"] or 0)
                            + (time.monotonic() - capture_started) * 1000, 1)
                        attempts.append("pass")
                        break
                    except Exception as exc:
                        attempts.append(type(exc).__name__[:80])
                    if time.monotonic() >= page_deadline:
                        break
                page_elapsed_ms[str(page_index)] = round(
                    (time.monotonic() - page_started) * 1000, 1)
                fallback_attempts[page_index] = attempts
                if attempts and attempts[-1] != "pass":
                    state_diag = last_state if isinstance(last_state, dict) else {}
                    if page_capture_failure_reason:
                        failure_reason = "CAPTURE_" + page_capture_failure_reason.upper()[:72]
                    elif not navigation_action.get("control_found"):
                        failure_reason = "NAV_CONTROL_NOT_FOUND"
                    elif navigation_action.get("click_error") and navigation_action.get("click_method") == "none":
                        failure_reason = "NAV_CLICK_FAILED"
                    elif not (state_diag.get("active_control_index") == page_index
                              or (state_diag.get("active_container_data_page") == page_index
                                  and state_diag.get("container_visible"))):
                        failure_reason = "WRONG_ACTIVE_PAGE"
                    elif not state_diag.get("canvas_in_requested_container"):
                        failure_reason = "CANVAS_NOT_CREATED"
                    elif not state_diag.get("canvas_width") or not state_diag.get("canvas_height"):
                        failure_reason = "CANVAS_ZERO_SIZE"
                    else:
                        failure_reason = str(readiness_stage or "OTHER")[:80]
                    page_nav_diagnostics[page_index] = {
                        "navigation_result": navigation_action.get("click_method", "none"),
                        "requested_control_exists": bool(state_diag.get("requested_control_exists")),
                        "requested_control_disabled": bool(state_diag.get("requested_control_disabled")),
                        "control_count": int(state_diag.get("control_count") or 0),
                        "active_control_index": state_diag.get("active_control_index"),
                        "active_container_data_page": state_diag.get("active_container_data_page"),
                        "container_exists": bool(state_diag.get("container_exists")),
                        "container_visible": bool(state_diag.get("container_visible")),
                        "canvas_count": int(state_diag.get("canvas_count") or 0),
                        "canvas_width": int(state_diag.get("canvas_width") or 0),
                        "canvas_height": int(state_diag.get("canvas_height") or 0),
                        "page_root_tag": str(state_diag.get("page_root_tag") or "")[:24],
                        "page_root_class": str(state_diag.get("page_root_class") or "")[:100],
                        "page_root_child_count": int(state_diag.get("page_root_child_count") or 0),
                        "page_root_image_count": int(state_diag.get("page_root_image_count") or 0),
                        "image_complete": bool(state_diag.get("image_complete")),
                        "image_natural_width": int(state_diag.get("image_natural_width") or 0),
                        "image_natural_height": int(state_diag.get("image_natural_height") or 0),
                        "image_page_number": state_diag.get("image_page_number"),
                        "image_src_present": bool(state_diag.get("image_src_present")),
                        "capture_failure_reason": page_capture_failure_reason,
                        "last_failure_reason": failure_reason,
                    }
                page_stage_metrics[page_index] = {
                    **page_metrics,
                    "total_materialization_ms": page_elapsed_ms[str(page_index)],
                    "attempt": float(len(attempts)),
                }
                if page_index not in {int(item.get("logical_page_index") or 0)
                                      for item in fallback_candidates}:
                    page_diag = page_nav_diagnostics.get(page_index, {})
                    _append_telemetry(
                        "comix_page_navigation_diagnostic", trace_id=trace_id,
                        status="pending", canonical_index=page_index,
                        navigation_result=page_diag.get("navigation_result", "none"),
                        candidate_source=next((str(item.get("source") or "") for item in candidates
                                               if item.get("logical_page_index") == page_index), ""),
                        candidate_url_presence=any(item.get("logical_page_index") == page_index
                                                   and bool(item.get("url")) for item in candidates),
                        requested_control_exists=page_diag.get("requested_control_exists", False),
                        requested_control_disabled=page_diag.get("requested_control_disabled", False),
                        control_count=page_diag.get("control_count", 0),
                        active_control_index=page_diag.get("active_control_index"),
                        reader_page_number=page_diag.get("active_container_data_page"),
                        active_container_data_page=page_diag.get("active_container_data_page"),
                        container_exists=page_diag.get("container_exists", False),
                        container_visible=page_diag.get("container_visible", False),
                        canvas_count=page_diag.get("canvas_count", 0),
                        canvas_width=page_diag.get("canvas_width", 0),
                        canvas_height=page_diag.get("canvas_height", 0),
                        page_root_tag=page_diag.get("page_root_tag", ""),
                        page_root_class=page_diag.get("page_root_class", ""),
                        page_root_child_count=page_diag.get("page_root_child_count", 0),
                        page_root_image_count=page_diag.get("page_root_image_count", 0),
                        image_complete=page_diag.get("image_complete", False),
                        image_natural_width=page_diag.get("image_natural_width", 0),
                        image_natural_height=page_diag.get("image_natural_height", 0),
                        image_page_number=page_diag.get("image_page_number"),
                        image_src_present=page_diag.get("image_src_present", False),
                        capture_failure_reason=page_diag.get("capture_failure_reason", ""),
                        image_response_seen=page_index in response_seen_indices,
                        body_candidate_seen=page_index in body_candidate_indices,
                        last_failure_reason=page_diag.get("last_failure_reason", "OTHER"),
                        nav_attempt_count=len(attempts),
                        elapsed_ms=page_elapsed_ms[str(page_index)],
                    )
            stage_end("missing_page_materialization", materialization_started)
        if fallback_candidates:
            candidates, _ = _merge_logical_candidates(
                candidates, fallback_candidates, control_count)
        final_indices = sorted({
            int(item["logical_page_index"])
            for item in candidates
            if item.get("logical_page_index") is not None
        })
        scroll_diagnostics["logical_page_indices"] = final_indices
        final_missing = [index for index in _target_page_indices(control_count, requested_count)
                         if index not in final_indices]
        candidate_by_index = {
            int(item["logical_page_index"]): item
            for item in candidates if item.get("logical_page_index") is not None
        }
        for page_index in _target_page_indices(control_count, requested_count):
            item = candidate_by_index.get(page_index)
            result_source = str(item.get("source") or "pending") if item else "pending"
            page_metrics = page_stage_metrics.get(page_index, {})
            body_ms = float(page_body_elapsed_ms.get(page_index, 0.0))
            canvas_ms = float(page_metrics.get("canvas_capture_ms") or 0.0)
            fallback_total = float(page_metrics.get("total_materialization_ms") or 0.0)
            total_ms = round(body_ms + fallback_total, 1)
            _append_telemetry(
                "comix_page_materialization",
                status="success" if item and result_source != "scrapling_dom" else "pending",
                canonical_index=page_index,
                discovery_ms=round(page_discovery_elapsed_ms.get(page_index, 0.0), 1),
                navigation_ms=page_metrics.get("navigation_ms", 0.0) or 0.0,
                render_wait_ms=page_metrics.get("render_wait_ms", 0.0) or 0.0,
                response_body_ms=round(body_ms, 1),
                canvas_capture_ms=round(canvas_ms, 1),
                total_materialization_ms=total_ms,
                attempt=int(page_metrics.get("attempt") or 0),
                failure_stage=(fallback_attempt_diagnostics.get(str(page_index), {})
                               .get("timeout_substage") if result_source == "pending" else ""),
                result_source=result_source,
                trace_id=trace_id,
                navigation_result=(page_nav_diagnostics.get(page_index, {})
                                   .get("navigation_result", "not_attempted")),
                candidate_source=result_source,
                candidate_url_presence=bool(item and item.get("url")),
                requested_control_exists=(page_nav_diagnostics.get(page_index, {})
                                          .get("requested_control_exists", False)),
                requested_control_disabled=(page_nav_diagnostics.get(page_index, {})
                                            .get("requested_control_disabled", False)),
                control_count=(page_nav_diagnostics.get(page_index, {}).get("control_count", 0)),
                active_control_index=(page_nav_diagnostics.get(page_index, {})
                                      .get("active_control_index")),
                active_container_data_page=(page_nav_diagnostics.get(page_index, {})
                                            .get("active_container_data_page")),
                container_exists=(page_nav_diagnostics.get(page_index, {}).get("container_exists", False)),
                container_visible=(page_nav_diagnostics.get(page_index, {}).get("container_visible", False)),
                canvas_count=(page_nav_diagnostics.get(page_index, {}).get("canvas_count", 0)),
                canvas_width=(page_nav_diagnostics.get(page_index, {}).get("canvas_width", 0)),
                canvas_height=(page_nav_diagnostics.get(page_index, {}).get("canvas_height", 0)),
                page_root_tag=page_nav_diagnostics.get(page_index, {}).get("page_root_tag", ""),
                page_root_class=page_nav_diagnostics.get(page_index, {}).get("page_root_class", ""),
                page_root_child_count=page_nav_diagnostics.get(page_index, {}).get("page_root_child_count", 0),
                page_root_image_count=page_nav_diagnostics.get(page_index, {}).get("page_root_image_count", 0),
                image_complete=page_nav_diagnostics.get(page_index, {}).get("image_complete", False),
                image_natural_width=page_nav_diagnostics.get(page_index, {}).get("image_natural_width", 0),
                image_natural_height=page_nav_diagnostics.get(page_index, {}).get("image_natural_height", 0),
                image_page_number=page_nav_diagnostics.get(page_index, {}).get("image_page_number"),
                image_src_present=page_nav_diagnostics.get(page_index, {}).get("image_src_present", False),
                body_candidate_seen=page_index in body_candidate_indices,
                image_response_seen=page_index in response_seen_indices,
                reader_page_number=(page_nav_diagnostics.get(page_index, {})
                                    .get("active_container_data_page")),
                capture_failure_reason=(page_nav_diagnostics.get(page_index, {})
                                        .get("capture_failure_reason", "")),
                last_failure_reason=(page_nav_diagnostics.get(page_index, {})
                                     .get("last_failure_reason", "")),
                nav_attempt_count=int(page_metrics.get("attempt") or 0),
                already_materialized_revisited=0,
                retry_pending_indices=final_missing,
                materialization_target_count=len(_target_page_indices(control_count, requested_count)),
                out_of_scope_materialized_count=0,
            )
        scroll_diagnostics.update({
            "fallback_navigation_count": len(missing),
            "fallback_resolved_count": len(fallback_candidates),
            "fallback_resolved_indices": sorted(
                int(item["logical_page_index"]) for item in fallback_candidates
            ),
            "fallback_failed_indices": [
                index for index in missing
                if index not in {
                    int(item["logical_page_index"]) for item in fallback_candidates
                }
            ],
            "fallback_attempts": {str(index): values for index, values in fallback_attempts.items()},
            "fallback_attempt_diagnostics": fallback_attempt_diagnostics,
            "fallback_page_elapsed_ms": page_elapsed_ms,
            "final_resolved_count": len(final_indices),
            "missing_final_count": len(final_missing),
            "missing_page_indices": final_missing,
            "discovery_strategy": (
                "comix_reader_preload_all_plus_missing_page_fallback"
                if fallback_candidates else scroll_diagnostics.get("discovery_strategy")
            ),
            "normalized_page_count": len(candidates),
        })
        result_holder["candidates"] = candidates[:MAX_PAGES]
        preserved_candidates = list(result_holder["candidates"])
        result_holder["selector"] = selector
        result_holder["scroll_diagnostics"] = scroll_diagnostics
        result_holder["canvas_captured"] = len(fallback_candidates)
        result_holder["final_missing"] = final_missing

    browser = discover_system_browser()
    if browser is None:
        _append_telemetry("dynamic_resolution_failed", status="error", error_code="browser_executable_not_found")
        raise DynamicReaderError("browser_executable_not_found")
    # Bounded whole-resolution retry for the one proven-transient mode
    # (``canonical_materialization_failed``: a discovered page left as an unmaterialized
    # ``scrapling_dom`` placeholder).  A fresh pass almost always completes it.  Every other
    # outcome — no_reader_images, timeout, browser/capability/navigation errors — is
    # non-transient and never retried.  Fail-closed is preserved: an incomplete chapter is
    # never returned.
    candidates: list[dict[str, Any]] = []
    final_missing: list[int] = []
    prev_pending_count: int | None = None
    prev_materialized_count: int | None = None
    for attempt in range(1, MAX_RESOLUTION_ATTEMPTS + 1):
        remaining = remaining_seconds("dynamic_fetch")
        if attempt > 1:
            # Fresh pass: the page_action/page_setup closures write into these shared
            # containers, so reset them in place (the closures still hold the same objects).
            result_holder.clear()
            events.clear()
            browser_bodies.clear()
            pending_responses.clear()
        try:
            fetch_started = stage_start("dynamic_fetch")
            response = _DynamicFetcher.fetch(
                url, real_chrome=True, executable_path=browser[1], headless=True,
                timeout=max(1, int(min(float(timeout), remaining) * 1000)), wait=1200,
                network_idle=False, page_setup=page_setup, page_action=page_action,
                retries=1, retry_delay=0,
            )
            stage_end("dynamic_fetch", fetch_started)
        except DynamicReaderError as exc:
            _append_telemetry("dynamic_resolution_failed", status="error", error_code=exc.code)
            raise
        except Exception as exc:
            if time.monotonic() >= deadline:
                _append_telemetry(
                    "dynamic_fetch_stage_timeout", status="timeout", stage="dynamic_fetch",
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise DynamicReaderError("dynamic_resolver_deadline_exceeded", "dynamic_fetch") from exc
            text = type(exc).__name__.casefold()
            code = "browser_unavailable" if any(x in text for x in ("browser", "executable", "chrom")) else "dynamic_fetch_failed"
            raise DynamicReaderError(code) from exc
        candidates = result_holder.get("candidates") or []
        final_missing = result_holder.get("final_missing") or []
        # Single fail-closed decision point.  A Comix DOM image can be the scrambled
        # intermediate resource, and an unresolved logical page is an incomplete chapter —
        # never silently hand either downstream.  Each mode gets its own explicit code so a
        # materialization timeout is never mislabeled as an empty discovery.
        failure = _resolution_failure_code(candidates, final_missing)
        if failure is None:
            break  # complete: every logical page materialized; fall through to analysis
        if failure == "canonical_materialization_failed":
            pending = sorted({int(item.get("logical_page_index")) for item in candidates
                              if item.get("logical_page_index") is not None
                              and str(item.get("source") or "") == "scrapling_dom"})
            materialized_count = sum(
                1 for item in candidates
                if item.get("logical_page_index") is not None
                and str(item.get("source") or "") != "scrapling_dom")
            remaining_budget = max(0.0, deadline - time.monotonic())
            pending_budget = _missing_chapter_budget_seconds(len(pending))
            decision = _materialization_retry_decision(
                pending_count=len(pending), prev_pending_count=prev_pending_count,
                remaining_budget=remaining_budget, attempt=attempt,
                max_attempts=MAX_RESOLUTION_ATTEMPTS, pending_budget=pending_budget)
            telemetry = dict(
                attempt=attempt, max_attempts=MAX_RESOLUTION_ATTEMPTS,
                pending_count=len(pending), pending_indices=pending[:30],
                materialized_count=materialized_count,
                expected_count=len(_target_page_indices(
                    int((result_holder.get("scroll_diagnostics") or {}).get("page_control_count") or 0),
                    requested_count)),
                final_missing_count=len(final_missing),
                new_pages=(materialized_count if prev_materialized_count is None
                           else max(0, materialized_count - prev_materialized_count)),
                pending_before=prev_pending_count,
                pending_after=len(pending),
                new_materialized=(materialized_count if prev_materialized_count is None
                                  else max(0, materialized_count - prev_materialized_count)),
                remaining_budget_ms=int(remaining_budget * 1000),
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                # Response-body utility is emitted on the terminal telemetry too, so a
                # failing run (the common Comix case) still reports the capture reduction.
                **body_telemetry(),
                body_promoted=sum(1 for item in candidates
                                  if str(item.get("source") or "") == "browser_response_body"),
            )
            if decision == "no_progress":
                # A full pass that materialized nothing new cannot be helped by an
                # identical pass; fail closed with a precise code instead of a doomed
                # retry or a misleading "budget insufficient" message.
                _append_telemetry("dynamic_resolution_no_progress", status="error",
                                  error_code="canonical_materialization_no_progress", **telemetry)
                raise DynamicReaderError("canonical_materialization_no_progress")
            if decision == "budget_insufficient":
                _append_telemetry(
                    "dynamic_resolution_retry_skipped", status="skipped",
                    error_code="canonical_materialization_retry_budget_insufficient", **telemetry)
                raise DynamicReaderError("canonical_materialization_retry_budget_insufficient")
            if decision == "failed":
                _append_telemetry("dynamic_resolution_failed", status="error",
                                  error_code="canonical_materialization_failed", **telemetry)
                raise DynamicReaderError("canonical_materialization_failed")
            # decision == "retry": progress was made and the budget funds another bounded
            # pass; a fresh full pass almost always completes the remaining pending page.
            _append_telemetry("dynamic_resolution_retry", status="retry",
                              error_code="canonical_materialization_failed", **telemetry)
            prev_pending_count = len(pending)
            prev_materialized_count = materialized_count
            remaining_seconds("resolution_retry")
            continue
        if failure == "no_reader_images":
            raise DynamicReaderError("no_reader_images")
        if failure == "canonical_materialization_timeout":
            _append_telemetry("dynamic_resolution_failed", status="error",
                              error_code="canonical_materialization_timeout",
                              missing_count=len(final_missing),
                              elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            raise DynamicReaderError("canonical_materialization_timeout")
    # The opaque API wrapper is deliberately treated as observation-only.  We never read or
    # transform its body; DOM image URLs are the selected public source of truth.
    _append_telemetry(
        "dynamic_body_capture_summary", status="ok",
        **body_telemetry(),
        body_promoted=sum(1 for item in candidates
                          if str(item.get("source") or "") == "browser_response_body"),
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
    )
    remaining_seconds("analysis_finalize")
    from universal_chapter_adapter import analyse_candidates
    analysis = analyse_candidates(
        url, candidates, adapter=adapter, final_url=url,
        warnings=("scrapling_dynamic_dom", "opaque_xhr_not_decrypted"),
        canvas_detected=int(result_holder.get("canvas_captured") or 0),
        canvas_captured=int(result_holder.get("canvas_captured") or 0),
        network_metadata=events,
        reader_diagnostics={
            "resolver": "scrapling",
            "selected_site": "comix.to",
            "dynamic_selector": result_holder.get("selector", _PAGE_SELECTOR),
            "slots_total": len(candidates), "slots_resolved": len(candidates),
            "slots_pending": 0, "slots_rejected": 0,
            **(result_holder.get("scroll_diagnostics") or {}),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "opaque_xhr_observed": any(str(e.get("path") or "").startswith("/api/v1/chapters/")
                                        for e in events),
        },
        cluster_score=adapter.score_cluster,
    )
    analysis.collection_strategy = "scrapling_selected_dynamic_dom"
    analysis.coverage_strategy = "reader_container"
    analysis.profile_used = False
    remaining_seconds("analysis_finalize")
    _append_telemetry(
        "dynamic_resolution_succeeded", status="success", pages=len(candidates),
        selector=result_holder.get("selector", _PAGE_SELECTOR),
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
    )
    return analysis


def _create_kill_job() -> Any:
    """Create a Windows Job Object that kills the child and its browser descendants."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = ExtendedLimit()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    return (kernel32, handle)


def _job_active_processes(job: Any) -> int:
    if job is None:
        return 0
    import ctypes
    from ctypes import wintypes

    class BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32, handle = job
    kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    wintypes.LPVOID, wintypes.DWORD,
                                                    wintypes.LPVOID]
    info = BasicAccounting()
    if not kernel32.QueryInformationJobObject(
        handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
    ):
        raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
    return int(info.ActiveProcesses)


def _assign_child_to_job(job: Any, process: subprocess.Popen[Any]) -> None:
    if job is None:
        return
    kernel32, handle = job
    if not kernel32.AssignProcessToJobObject(handle, process._handle):  # noqa: SLF001
        import ctypes
        error = ctypes.get_last_error()
        raise OSError(error, "AssignProcessToJobObject failed")


def _terminate_child(process: subprocess.Popen[Any], job: Any, *, grace_seconds: float = 0.5) -> None:
    if job is not None:
        kernel32, handle = job
        kernel32.TerminateJobObject(handle, 1)
    elif process.poll() is None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=grace_seconds)
    if job is not None:
        end = time.monotonic() + max(1.0, grace_seconds)
        while _job_active_processes(job) and time.monotonic() < end:
            time.sleep(0.02)


def _close_job(job: Any) -> None:
    if job is not None:
        kernel32, handle = job
        kernel32.CloseHandle(handle)


def _run_child_command(command: list[str], *, deadline: float,
                       cancel_check: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Start behind a gate, place the process tree in a kill-on-close job, reap it."""
    job = _create_kill_job()
    process = None
    terminated = False
    cleanup_killed_descendants = False
    try:
        process = subprocess.Popen(
            command, cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            start_new_session=(os.name != "nt"),
        )
        _assign_child_to_job(job, process)
        assert process.stdin is not None
        process.stdin.write("go\n")
        process.stdin.flush()
        process.stdin.close()
        while process.poll() is None:
            if cancel_check and cancel_check():
                terminated = True
                _terminate_child(process, job)
                return {"returncode": process.returncode, "terminated": True,
                        "reaped": process.poll() is not None, "active_processes": _job_active_processes(job)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminated = True
                _terminate_child(process, job)
                return {"returncode": process.returncode, "terminated": True,
                        "reaped": process.poll() is not None, "active_processes": _job_active_processes(job)}
            time.sleep(min(0.05, remaining))
        process.wait()
        # A normal child may still have a stray browser descendant. Kill anything left
        # in the isolated job and wait until the kernel reports the tree empty. This is
        # cleanup after successful child exit, not a deadline/cancellation termination.
        if _job_active_processes(job):
            cleanup_killed_descendants = True
            _terminate_child(process, job)
        end = time.monotonic() + 1.0
        active = _job_active_processes(job)
        while active and time.monotonic() < end:
            time.sleep(0.02)
            active = _job_active_processes(job)
        return {"returncode": process.returncode, "terminated": terminated,
                "cleanup_killed_descendants": cleanup_killed_descendants,
                "reaped": process.poll() is not None, "active_processes": active}
    finally:
        if process is not None and process.poll() is None:
            _terminate_child(process, job)
        _close_job(job)


def _resolve_in_child_process(url: str, *, cancel_check: Callable[[], bool] | None,
                              timeout: float, max_pages: int | None = None) -> Any:
    """Supervise the sync Playwright resolver and forcibly reap its process tree."""
    from chapter_source import select_adapter

    started = time.monotonic()
    deadline = started + DYNAMIC_RESOLVER_DEADLINE_SECONDS
    with tempfile.TemporaryDirectory(prefix="yomu-dynamic-resolver-") as temp_dir:
        root = Path(temp_dir)
        request_path = root / "request.json"
        result_path = root / "result.pkl"
        status_path = root / "status.json"
        request_path.write_text(json.dumps({
            "url": url,
            "timeout": float(timeout),
            "deadline_seconds": max(
                1.0,
                DYNAMIC_RESOLVER_DEADLINE_SECONDS - DYNAMIC_RESOLVER_CLEANUP_RESERVE_SECONDS,
            ),
            "max_pages": max_pages,
        }), encoding="utf-8")
        if bool(getattr(sys, "frozen", False)):
            command = [sys.executable, "--internal-child", "dynamic-resolver",
                       str(request_path), str(result_path), str(status_path)]
        else:
            worker_script = Path(__file__).with_name("dynamic_resolver_process.py")
            command = [sys.executable, "-u", str(worker_script),
                       str(request_path), str(result_path), str(status_path)]
        try:
            outcome = _run_child_command(command, deadline=deadline, cancel_check=cancel_check)
        except Exception as exc:
            raise DynamicReaderError("dynamic_resolver_isolation_unavailable") from exc
        if not outcome["reaped"] or outcome["active_processes"]:
            raise DynamicReaderError("dynamic_resolver_child_cleanup_failed")
        if outcome["terminated"]:
            code = "cancelled" if cancel_check and cancel_check() else "dynamic_resolver_deadline_exceeded"
            _append_telemetry(
                "dynamic_resolution_failed", status="timeout" if code != "cancelled" else "cancelled",
                error_code=code, stage="child_process",
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            raise DynamicReaderError(code)
        if not status_path.is_file():
            raise DynamicReaderError("dynamic_resolver_child_failed")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "pass":
            reason = str(status.get("reason_code") or "dynamic_resolver_child_failed")[:80]
            raise DynamicReaderError(reason)
        if not result_path.is_file() or result_path.stat().st_size > 256 * 1024 * 1024:
            raise DynamicReaderError("dynamic_resolver_child_result_invalid")
        with result_path.open("rb") as stream:
            return pickle.load(stream)


def resolve(url: str, *, adapter: Any, cancel_check: Callable[[], bool] | None = None,
            timeout: float = 30.0, max_pages: int | None = None) -> Any:
    """Run the sync browser resolver behind a killable wall-clock boundary."""
    if not supports_url(url):
        raise DynamicReaderError("site_not_selected")
    if getattr(adapter, "name", "") not in {"comix", "ComixAdapter"}:
        raise DynamicReaderError("site_not_selected")
    if max_pages is None:
        return _resolve_in_child_process(url, cancel_check=cancel_check, timeout=timeout)
    return _resolve_in_child_process(
        url, cancel_check=cancel_check, timeout=timeout, max_pages=max_pages)
