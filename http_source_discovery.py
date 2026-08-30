"""HTTP-first chapter-page discovery.

The browser-based analysis in ``down.py`` (``analyze_chapter_source``) is correct for every
source but expensive and, on a machine with a broken Chrome/TPM stack, sometimes unusable at
all. Some readers (proven for VortexScans) render every page as a plain ``<img>`` in the
*initial* HTTP response, so the same reader-container evidence Selenium would collect from the
live DOM is already sitting in one bounded GET.

This module is the seam that tries that cheaper path first. It reuses the adapter's own
container/image knowledge (``collect_dom_candidates_from_html``) and the existing candidate
scoring pipeline (``analyse_candidates``) unchanged -- the only new work here is fetching the
page over plain HTTP and asking the adapter to look at the static markup instead of a live DOM.

Returns ``None`` -- never raises for an ordinary "this doesn't apply" case -- whenever:
  * the adapter has no static-HTML collector (default for every adapter but Vortex), or
  * the bounded fetch itself fails (network error, timeout, blocked, wrong content-type), or
  * the resulting analysis is not immediately usable (low confidence, needs review, etc).
In every one of those cases the caller is expected to fall back to the existing
browser-based ``analyze_chapter_source``, so a `None` here can never leave a chapter stuck.
"""

from __future__ import annotations

from typing import Any

from canonical_source_identity import RequestsMetadataTransport
from chapter_source import SourceError


def _has_http_collector(adapter: Any) -> bool:
    """True only for an adapter that actually overrode the static-HTML collector.

    Every adapter inherits ``BaseAdapter.collect_dom_candidates_from_html`` (always callable,
    always returns ``None``), so a bare ``callable(...)`` check can never tell "unsupported"
    apart from "supported but this page didn't match". Comparing the bound method against the
    base implementation is what actually gates the HTTP fetch to adapters that opted in.
    """
    from chapter_source import BaseAdapter

    method = getattr(type(adapter), "collect_dom_candidates_from_html", None)
    base_method = BaseAdapter.collect_dom_candidates_from_html
    return callable(method) and method is not base_method


def discover_via_http(adapter: Any, url: str, *, cancel_check=None) -> Any | None:
    """Return a usable ``SourceAnalysis`` from a static HTTP fetch, or ``None``."""
    if not _has_http_collector(adapter):
        return None
    transport = RequestsMetadataTransport(adapter)
    try:
        html = transport.fetch_page(url)
    except Exception:
        # Any transport failure (SSRF/redirect refusal, timeout, bad content-type, size cap,
        # HTTP error) is a controlled "HTTP discovery not possible here", not a job failure --
        # the browser path gets the same URL next.
        return None
    finally:
        transport.close()
    if cancel_check and cancel_check():
        raise SourceError("cancelled", "during_http_source_discovery")
    raw_candidates = adapter.collect_dom_candidates_from_html(html, url)
    if not raw_candidates:
        return None

    from universal_chapter_adapter import analyse_candidates

    analysis = analyse_candidates(
        url, raw_candidates, adapter=adapter, final_url=url,
        cluster_score=adapter.score_cluster,
    )
    if not analysis.can_download:
        # Static HTML did not produce a confident, complete manifest (e.g. the page changed
        # shape). Do not gamble a partial chapter -- the browser fallback re-analyses fresh.
        return None
    analysis.page_manifest = adapter.build_page_manifest(analysis.accepted)
    analysis.reader_diagnostics = {
        **analysis.reader_diagnostics, "discovery_transport": "http"}
    return analysis
