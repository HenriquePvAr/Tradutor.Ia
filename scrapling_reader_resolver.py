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
import re
import struct
import tempfile
import time
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


def _missing_chapter_budget_seconds(unresolved_count: int) -> float:
    """Chapter-level materialization ceiling, proportional to the number of unresolved
    pages (with a hard safety cap).  Never a single fixed budget shared by every page."""
    return min(
        MISSING_CHAPTER_MAX_BUDGET_SECONDS,
        MISSING_CHAPTER_BASE_OVERHEAD_SECONDS
        + max(0, int(unresolved_count)) * MISSING_PAGE_BUDGET_SECONDS,
    )


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
               "attempt", "max_attempts", "pending_count", "pending_indices", "missing_count"}
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
    scroll_diagnostics: dict[str, Any],
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
          direction: document.querySelector('[role="radio"][aria-checked="true"]')?.innerText || null
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
              direction: document.querySelector('[role="radio"][aria-checked="true"]')?.innerText || null
            })""") or {}
        scroll_diagnostics.update({
            "preload_mode_initial": str(state.get("checked") or "unknown"),
            "preload_control_found": bool(state.get("all")),
            "reading_direction": str(state.get("direction") or "unknown"),
        })
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
            preload_stage = "promote"
            promoted_expanded: list[dict[str, Any]] = []
            for item in expanded:
                if str(item.get("source") or "") != "scrapling_dom":
                    promoted_expanded.append(item)
                    continue
                resource_url = str(item.get("url") or "")
                captured = browser_bodies.get(resource_url)
                promoted_expanded.append(
                    (_promote_browser_response_candidate(
                        item, captured.get("body", b""), captured.get("content_type")
                    ) if captured else None)
                    or item
                )
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


def _capture_rendered_canvas_candidate(page: Any, page_index: int, state: dict[str, Any]) -> dict[str, Any] | None:
    """Materialize a Canvas page from a fresh headless reader frame.

    The isolated Canvas surface can remain stale after Comix reuses a reader node.  The
    authoritative source is therefore the current reader viewport, cropped only by the
    target DOM geometry.  If the page cannot fit without changing horizontal layout, or
    if the crop would leave the screenshot bounds, this fails closed.
    """
    expected_width = int(state.get("canvas_width") or state.get("width") or 0)
    expected_height = int(state.get("canvas_height") or state.get("height") or 0)
    geometry_script = """(index) => {
      const reader = document.querySelector('main.rpage-main');
      const target = document.querySelector('[data-page="' + index + '"]');
      const render = target?.querySelector('canvas.rpage-page__img');
      if (!reader || !target || !render) return null;
      const rr = reader.getBoundingClientRect();
      const tr = render.getBoundingClientRect();
      return {
        reader: {x: rr.x, y: rr.y, width: rr.width, height: rr.height},
        target: {x: tr.x, y: tr.y, width: tr.width, height: tr.height},
        viewport: {width: innerWidth, height: innerHeight},
        visual: {x: visualViewport?.offsetLeft || 0, y: visualViewport?.offsetTop || 0},
        dpr: devicePixelRatio,
        native: {width: Number(render.width || 0), height: Number(render.height || 0)}
      };
    }"""
    try:
        geometry = page.evaluate(geometry_script, page_index) or {}
        if not geometry:
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
                return None
            setter({"width": int(viewport["width"]), "height": required_height})
            page.wait_for_timeout(80)
            page.evaluate("""() => new Promise(resolve => {
              requestAnimationFrame(() => requestAnimationFrame(resolve));
            })""")
            geometry = page.evaluate(geometry_script, page_index) or {}
            if not geometry:
                return None
            target = geometry["target"]
            viewport = geometry["viewport"]
            dpr = float(geometry.get("dpr") or dpr)
        tolerance = 1.0
        if (target["x"] < -tolerance or target["y"] < -tolerance or
                target["x"] + target["width"] > viewport["width"] + tolerance or
                target["y"] + target["height"] > viewport["height"] + tolerance):
            return None
        image_bytes = page.screenshot(type="png")
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(image_bytes)) as source:
            source = source.convert("RGBA")
            source_width, source_height = source.size
            visual = geometry.get("visual") or {"x": 0, "y": 0}
            left = round((target["x"] - float(visual.get("x") or 0)) * dpr)
            top = round((target["y"] - float(visual.get("y") or 0)) * dpr)
            right = round((target["x"] + target["width"] - float(visual.get("x") or 0)) * dpr)
            bottom = round((target["y"] + target["height"] - float(visual.get("y") or 0)) * dpr)
            if left < 0 or top < 0 or right > source_width or bottom > source_height:
                return None
            cropped = source.crop((left, top, right, bottom))
            crop_width, crop_height = cropped.size
            if not crop_width or not crop_height:
                return None
            if expected_width and expected_height:
                crop_aspect = crop_width / crop_height
                native_aspect = expected_width / expected_height
                if abs(crop_aspect - native_aspect) > 0.01:
                    return None
                if crop_width < expected_width or crop_height < expected_height:
                    return None
                if (crop_width, crop_height) != (expected_width, expected_height):
                    cropped = cropped.resize((expected_width, expected_height), Image.Resampling.LANCZOS)
            encoded = BytesIO()
            cropped.save(encoded, format="PNG")
            image_bytes = encoded.getvalue()
    except Exception:
        return None
    if len(image_bytes) < 24 or image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
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
        "capture_method": "headless_dynamic_viewport_crop",
        "capture_width": capture_width,
        "capture_height": capture_height,
    }


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
      const root = document.querySelector('[data-page="' + wanted + '"]');
      const canvas = root?.querySelector('canvas.rpage-page__img');
      let fingerprint = '';
      if (canvas && canvas.width && canvas.height) {
        const context = canvas.getContext('2d', {willReadFrequently: true});
        if (context) {
          // Sample a distributed grid over the backing store. Five fixed points
          // collide easily on dark/white borders and falsely classify a new page
          // as the previous canvas.
          const points = [];
          const sampleWidth = 24;
          const sampleHeight = 32;
          for (let sy = 0; sy < sampleHeight; sy++) {
            for (let sx = 0; sx < sampleWidth; sx++) {
              points.push([
                Math.min(canvas.width - 1, Math.floor((sx + 0.5) * canvas.width / sampleWidth)),
                Math.min(canvas.height - 1, Math.floor((sy + 0.5) * canvas.height / sampleHeight))
              ]);
            }
          }
          let hash = 2166136261;
          for (const [x,y] of points) {
            const pixel = context.getImageData(Math.max(0,x), Math.max(0,y), 1, 1).data;
            for (const value of pixel) { hash ^= value; hash = Math.imul(hash, 16777619); }
          }
          fingerprint = (hash >>> 0).toString(16);
        }
      }
      const canvasLabel = String(canvas?.getAttribute('aria-label') || '');
      const labelMatch = canvasLabel.match(/(?:page|página)\\s*(\\d+)/i);
      return {
        requested_index: index,
        active_control_index: activeMatch ? Number(activeMatch[1]) : null,
        active_container_data_page: root ? Number(root.getAttribute('data-page')) : null,
        canvas_aria_label: canvasLabel,
        canvas_label_index: labelMatch ? Number(labelMatch[1]) : null,
        canvas_width: canvas?.width || 0,
        canvas_height: canvas?.height || 0,
        canvas_pixel_hash: fingerprint,
        fingerprint_sample_area: '24x32_grid',
        container_exists: !!root,
        canvas_in_requested_container: !!canvas,
        render_settled: !!root && !!canvas && !root.classList.contains('is-loading') && !!fingerprint
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


def resolve(url: str, *, adapter: Any, cancel_check: Callable[[], bool] | None = None,
            timeout: float = 30.0) -> Any:
    """Resolve a selected dynamic reader into the regular SourceAnalysis model."""
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
    browser_bodies: dict[str, dict[str, Any]] = {}
    result_holder: dict[str, Any] = {}

    def page_setup(page: Any) -> None:
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
            if len(events) >= MAX_NETWORK_EVENTS:
                return
            try:
                resource_url = str(response.url)
                resource_type = str(getattr(response, "request", None).resource_type
                                    if getattr(response, "request", None) else "other")
                if "/api/v1/chapters/" in resource_url or resource_type == "image":
                    events.append(_sanitized_network_event(resource_url, resource_type,
                                                            int(response.status)))
                if resource_type == "image" and "jloo.wowpic1.store" in resource_url:
                    try:
                        body = response.body()
                        if 0 < len(body) <= MAX_BROWSER_RESPONSE_BODY_BYTES:
                            browser_bodies[resource_url] = {
                                "body": body,
                                "content_type": str(response.headers.get("content-type") or ""),
                            }
                    except Exception:
                        pass
            except Exception:
                return
        try:
            page.on("response", on_response)
        except Exception as exc:
            raise DynamicReaderError("network_observer_unavailable") from exc

    def page_action(page: Any) -> None:
        if cancel_check and cancel_check():
            raise DynamicReaderError("cancelled")
        try:
            page.wait_for_timeout(1200)
            page.wait_for_timeout(400)
        except Exception as exc:
            raise DynamicReaderError("browser_action_failed") from exc
        candidates, selector = _extract_candidates(page, url, events, cancel_check)
        promoted_candidates: list[dict[str, Any]] = []
        for item in candidates:
            if str(item.get("source") or "") != "scrapling_dom":
                promoted_candidates.append(item)
                continue
            resource_url = str(item.get("url") or "")
            captured = browser_bodies.get(resource_url)
            promoted = (
                _promote_browser_response_candidate(
                    item, captured.get("body", b""), captured.get("content_type")
                ) if captured else None
            )
            promoted_candidates.append(promoted or item)
        candidates = promoted_candidates
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
        )
        if len(candidates) <= 3:
            candidates, walked = _scroll_normal_reader(
                page, selector, candidates, cancel_check)
            scroll_diagnostics.update(walked)
            scroll_diagnostics["discovery_strategy"] = "dom_scroll_accumulation"
        else:
            scroll_diagnostics["discovery_strategy"] = "public_reader_preload_all_dom"
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
        logical_indices = sorted({
            int(item["logical_page_index"])
            for item in candidates
            if item.get("logical_page_index") is not None
        })
        control_count = int(scroll_diagnostics.get("page_control_count") or 0)
        missing = [index for index in _expected_page_indices(control_count)
                   if index not in logical_indices]
        scroll_diagnostics["logical_page_indices"] = logical_indices
        scroll_diagnostics["missing_page_indices"] = missing
        fallback_candidates: list[dict[str, Any]] = []
        fallback_attempts: dict[int, list[str]] = {}
        fallback_attempt_diagnostics: dict[str, dict[str, Any]] = {}
        per_page_budget = MISSING_PAGE_BUDGET_SECONDS
        chapter_budget_seconds = _missing_chapter_budget_seconds(len(missing))
        chapter_deadline = time.monotonic() + chapter_budget_seconds
        scroll_diagnostics["fallback_budget_seconds"] = chapter_budget_seconds
        scroll_diagnostics["fallback_per_page_budget_seconds"] = per_page_budget
        page_elapsed_ms: dict[str, float] = {}
        if missing:
            for page_index in missing:
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
                page_deadline = min(page_started + per_page_budget, chapter_deadline)
                attempts: list[str] = []
                for attempt in range(1, MAX_CANONICAL_ATTEMPTS + 1):
                    try:
                        _read_reader_render_state(page, page_index)
                        # A retry starts a fresh logical/render/network epoch.  The page
                        # is deliberately re-navigated; no stale canvas or body is reused.
                        try:
                            page.evaluate("(index) => window.__yomuCanvasDiag?.markNavigation(index)", page_index)
                        except Exception:
                            pass
                        page.evaluate(
                            """(index) => {
                            const button = document.querySelector(
                              'button[aria-label="Go to page ' + index + '"]');
                            if (button) button.click();
                          }""",
                            page_index,
                        )
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
                            identity_ok = (
                                state.get("active_control_index") == page_index
                                and state.get("active_container_data_page") == page_index
                                and state.get("canvas_in_requested_container") is True
                                and (not state.get("canvas_label_index")
                                     or state.get("canvas_label_index") == page_index)
                            )
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
                        page.evaluate(
                            """() => new Promise(resolve => {
                              let count = 0;
                              const frame = () => { count += 1; count >= 2 ? resolve(count) : requestAnimationFrame(frame); };
                              requestAnimationFrame(frame);
                            })"""
                        )
                        state = _read_reader_render_state(page, page_index)
                        candidate = _capture_rendered_canvas_candidate(page, page_index, state)
                        page.evaluate("""() => new Promise(resolve => {
                          requestAnimationFrame(() => requestAnimationFrame(resolve));
                        })""")
                        stable_candidate = _capture_rendered_canvas_candidate(
                            page, page_index, _read_reader_render_state(page, page_index)
                        )
                        if candidate is None or stable_candidate is None:
                            attempts.append("temporary_compositor_not_ready")
                            continue
                        first_bytes = bytes(candidate.get("canvas_data") or b"")
                        second_bytes = bytes(stable_candidate.get("canvas_data") or b"")
                        if (
                            candidate.get("capture_width") != stable_candidate.get("capture_width")
                            or candidate.get("capture_height") != stable_candidate.get("capture_height")
                            or hashlib.sha256(first_bytes).digest()
                            != hashlib.sha256(second_bytes).digest()
                        ):
                            attempts.append("frame_not_stable")
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
                        attempts.append("pass")
                        break
                    except Exception as exc:
                        attempts.append(type(exc).__name__[:80])
                    if time.monotonic() >= page_deadline:
                        break
                page_elapsed_ms[str(page_index)] = round(
                    (time.monotonic() - page_started) * 1000, 1)
                fallback_attempts[page_index] = attempts
        if fallback_candidates:
            candidates, _ = _merge_logical_candidates(
                candidates, fallback_candidates, control_count)
        final_indices = sorted({
            int(item["logical_page_index"])
            for item in candidates
            if item.get("logical_page_index") is not None
        })
        final_missing = [index for index in _expected_page_indices(control_count)
                         if index not in final_indices]
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
        result_holder["selector"] = selector
        result_holder["scroll_diagnostics"] = scroll_diagnostics
        result_holder["canvas_captured"] = len(fallback_candidates)
        result_holder["final_missing"] = final_missing

    started = time.perf_counter()
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
    for attempt in range(1, MAX_RESOLUTION_ATTEMPTS + 1):
        if attempt > 1:
            # Fresh pass: the page_action/page_setup closures write into these shared
            # containers, so reset them in place (the closures still hold the same objects).
            result_holder.clear()
            events.clear()
            browser_bodies.clear()
        try:
            response = _DynamicFetcher.fetch(
                url, real_chrome=True, executable_path=browser[1], headless=True,
                timeout=int(timeout * 1000), wait=1200,
                network_idle=False, page_setup=page_setup, page_action=page_action,
                retries=1, retry_delay=0,
            )
        except DynamicReaderError as exc:
            _append_telemetry("dynamic_resolution_failed", status="error", error_code=exc.code)
            raise
        except Exception as exc:
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
            exhausted = attempt >= MAX_RESOLUTION_ATTEMPTS
            _append_telemetry(
                "dynamic_resolution_failed" if exhausted else "dynamic_resolution_retry",
                status="error" if exhausted else "retry",
                error_code="canonical_materialization_failed",
                attempt=attempt, max_attempts=MAX_RESOLUTION_ATTEMPTS,
                pending_count=len(pending), pending_indices=pending[:30],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            if not exhausted:
                continue  # a fresh full pass almost always completes the pending page
            raise DynamicReaderError("canonical_materialization_failed")
        if failure == "no_reader_images":
            raise DynamicReaderError("no_reader_images")
        if failure == "canonical_materialization_timeout":
            _append_telemetry("dynamic_resolution_failed", status="error",
                              error_code="canonical_materialization_timeout",
                              missing_count=len(final_missing),
                              elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
            raise DynamicReaderError("canonical_materialization_timeout")
    # The opaque API wrapper is deliberately treated as observation-only.  We never read or
    # transform its body; DOM image URLs are the selected public source of truth.
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
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "opaque_xhr_observed": any(str(e.get("path") or "").startswith("/api/v1/chapters/")
                                        for e in events),
        },
        cluster_score=adapter.score_cluster,
    )
    analysis.collection_strategy = "scrapling_selected_dynamic_dom"
    analysis.coverage_strategy = "reader_container"
    analysis.profile_used = False
    _append_telemetry(
        "dynamic_resolution_succeeded", status="success", pages=len(candidates),
        selector=result_holder.get("selector", _PAGE_SELECTOR),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return analysis
