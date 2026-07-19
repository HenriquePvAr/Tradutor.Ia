"""Selenium bridge for Webtoons lazy reader slots.

The lazy-slot resolver is intentionally browser-free. This module is the small adapter
between an already-open Selenium driver and the resolver operations it needs.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from chapter_source import NO_CHAPTER_IMAGES, SourceError


class WebtoonsReaderBridge:
    """Expose one Webtoons reader as injected resolver operations."""

    def __init__(
        self,
        driver: Any,
        adapter: Any,
        *,
        cancel_check: Callable[[], bool] | None = None,
        on_progress: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.driver = driver
        self.adapter = adapter
        self.cancel_check = cancel_check or (lambda: False)
        self.on_progress = on_progress
        self.clock = clock
        self.sleep = sleep

    def read_slots(self) -> list[dict[str, Any]]:
        """Read only image descendants of the Webtoons reader, in DOM order."""
        selectors = self.adapter.reader_selectors()
        container = str(selectors.get("container") or "#_imageList")
        image = str(selectors.get("image") or "img")
        payload = self.driver.execute_script(_READ_SLOTS_SCRIPT, container, image) or {}
        if not isinstance(payload, dict) or not payload.get("found"):
            raise SourceError(NO_CHAPTER_IMAGES, "reader_container_absent")
        slots = payload.get("slots")
        return list(slots) if isinstance(slots, list) else []

    def reader_bounds(self) -> tuple[int, int]:
        """Return top/bottom bounds for the reader; extra metrics remain diagnostics."""
        selectors = self.adapter.reader_selectors()
        container = str(selectors.get("container") or "#_imageList")
        payload = self.driver.execute_script(_READER_BOUNDS_SCRIPT, container) or {}
        if not isinstance(payload, dict) or not payload.get("found"):
            raise SourceError(NO_CHAPTER_IMAGES, "reader_container_absent")
        top = int(payload.get("reader_top") or 0)
        bottom = int(payload.get("reader_bottom") or top)
        return top, max(top, bottom)

    def scroll_to(self, position: int) -> None:
        """Scroll inside the reader, never to the document footer or recommendations."""
        selectors = self.adapter.reader_selectors()
        container = str(selectors.get("container") or "#_imageList")
        self.driver.execute_script(_SCROLL_TO_SCRIPT, container, int(position))


_READ_SLOTS_SCRIPT = r"""
const CONTAINER = arguments[0] || "#_imageList";
const IMAGE = arguments[1] || "img";
const root = document.querySelector(CONTAINER);
if (!root) return {found: false, slots: []};
const images = Array.from(root.querySelectorAll(IMAGE));
const resolve = value => {
  try { return value ? new URL(value, document.baseURI).href : ""; }
  catch (_) { return ""; }
};
return {
  found: true,
  slots: images.map((el, index) => {
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : {top:0,width:0,height:0};
    const currentSrc = el.currentSrc || "";
    const src = el.getAttribute("src") || "";
    const dataUrl = el.getAttribute("data-url") || "";
    const dataSrc = el.getAttribute("data-src") || "";
    const url = resolve(currentSrc || src || dataUrl || dataSrc);
    return {
      tag: "img",
      url,
      currentSrc: resolve(currentSrc),
      src: resolve(src),
      data_url: resolve(dataUrl),
      data_src: resolve(dataSrc),
      source: currentSrc ? "currentSrc" : (src ? "src" : (dataUrl ? "data-url" : "data-src")),
      order: index,
      width: Math.round(rect.width || el.width || 0),
      height: Math.round(rect.height || el.height || 0),
      naturalWidth: el.naturalWidth || 0,
      naturalHeight: el.naturalHeight || 0,
      complete: !!el.complete,
      y: Math.round((window.scrollY || 0) + rect.top),
      inContainer: true,
      isChapterCandidate: true,
      container: root.id || root.className || CONTAINER,
      className: String(el.className || ""),
      id: el.id || "",
      alt: el.alt || "",
      context: `${root.id || ""} ${root.className || ""}`.trim()
    };
  })
};
"""


_READER_BOUNDS_SCRIPT = r"""
const CONTAINER = arguments[0] || "#_imageList";
const root = document.querySelector(CONTAINER);
if (!root) return {found: false};
const rect = root.getBoundingClientRect();
const scrollY = window.scrollY || 0;
const top = Math.max(0, Math.round(scrollY + rect.top));
const bottom = Math.max(top, Math.round(scrollY + rect.bottom));
return {
  found: true,
  reader_top: top,
  reader_bottom: bottom,
  viewport_height: window.innerHeight || document.documentElement.clientHeight || 0,
  document_height: Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0
  )
};
"""


_SCROLL_TO_SCRIPT = r"""
const CONTAINER = arguments[0] || "#_imageList";
const requested = Number(arguments[1] || 0);
const root = document.querySelector(CONTAINER);
if (!root) return false;
const rect = root.getBoundingClientRect();
const scrollY = window.scrollY || 0;
const top = Math.max(0, Math.round(scrollY + rect.top));
const bottom = Math.max(top, Math.round(scrollY + rect.bottom));
const viewport = window.innerHeight || document.documentElement.clientHeight || 0;
const maxTarget = Math.max(top, bottom - Math.max(1, viewport));
const target = Math.min(Math.max(top, Math.round(requested)), maxTarget);
window.scrollTo({top: target, behavior: "instant"});
return true;
"""
