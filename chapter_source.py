"""Safe source adapters for public chapter URLs.

Known sources keep their own reader knowledge. Public HTTP(S) URLs without a known adapter
may use ``UniversalChapterAdapter``, but it is a controlled fallback: the submitted host
passes the same SSRF checks as a known source and image/CDN hosts are authorised only after
reader analysis observes them. The generic adapter never promises support for every site and
never bypasses a challenge or authentication wall.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

# Stable, sanitized failure codes. The UI maps these to messages; a job must always end on
# one of these rather than sitting in queued/running forever.
UNSUPPORTED_SOURCE = "unsupported_source"
SOURCE_NOT_READY = "source_not_ready"
CHALLENGE_REQUIRED = "challenge_required"
SOURCE_ACCESS_DENIED = "source_access_denied"
SOURCE_RATE_LIMITED = "source_rate_limited"
NO_CHAPTER_IMAGES = "no_chapter_images"
# A registered adapter claimed the source (as opposed to a refusal).
SUPPORTED_SPECIFIC_ADAPTER = "supported_specific_adapter"
INVALID_IMAGE_RESPONSE = "invalid_image_response"
INCOMPLETE_DOWNLOAD = "incomplete_download"
AUTHENTICATION_REQUIRED = "authentication_required"
UNSUPPORTED_CANVAS_READER = "unsupported_canvas_reader"
UNSUPPORTED_CROSS_ORIGIN_READER = "unsupported_cross_origin_reader"
SUPPORTED_GENERIC_HIGH_CONFIDENCE = "supported_generic_high_confidence"
REVIEW_REQUIRED_MEDIUM_CONFIDENCE = "review_required_medium_confidence"
UNSUPPORTED_LOW_CONFIDENCE = "unsupported_low_confidence"

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_IMAGE_MIME = ("image/jpeg", "image/png", "image/webp", "image/avif", "image/gif")
MAX_OBSERVED_RESOURCE_HOSTS = 64

# One browser observation can include performance logs, whose reads are destructive on some
# Selenium backends.  Adapter hooks therefore share the same in-flight evidence through a
# context-local cache; concurrent jobs never see each other's browser or candidate data.
_ACTIVE_COLLECTION: ContextVar[dict[tuple[int, str], dict[str, Any]] | None] = ContextVar(
    "chapter_source_active_collection", default=None
)

# Interactive challenges we refuse to work around. Detecting one is a terminal, honest stop.
CHALLENGE_MARKERS = (
    "cf-challenge", "cf_chl", "turnstile", "hcaptcha", "recaptcha",
    "checking your browser", "verify you are human", "challenge-platform",
)


class SourceError(ValueError):
    """A sanitized, coded failure. Never carries cookies, headers or credentials."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class UnsupportedSource(SourceError):
    def __init__(self, host: str = ""):
        super().__init__(UNSUPPORTED_SOURCE, host)
        self.host = host


class ChallengeRequired(SourceError):
    """An interactive challenge stands in the way. We stop; we never solve it."""

    def __init__(self, detail: str = ""):
        super().__init__(CHALLENGE_REQUIRED, detail)


class ChapterSourceAdapter(Protocol):
    name: str
    adapter_version: str
    allowed_hosts: tuple[str, ...]

    def supports(self, url: str) -> bool: ...
    def normalize_url(self, url: str) -> str: ...
    def validate_url(self, url: str) -> None: ...
    def validate_navigation_url(self, url: str) -> None: ...
    def validate_observed_url(self, url: str) -> None: ...
    def validate_path(self, url: str) -> None: ...
    def reader_selectors(self) -> dict[str, str]: ...
    def classify_candidate(self, candidate: dict[str, Any]) -> str: ...
    def exclude_candidate(self, candidate: dict[str, Any]) -> str: ...
    def authorize_related_url(self, url: str) -> None: ...
    def analyze(self, context: Any, *, profile: dict[str, Any] | None = None,
                extra_warnings: tuple[str, ...] = ()) -> Any: ...
    def wait_until_ready(self, browser: Any) -> None: ...
    def collect_dom_candidates(self, browser: Any, *, page_url: str = "") -> list[dict[str, Any]]: ...
    def collect_network_candidates(self, browser: Any, *, page_url: str = "") -> list[dict[str, Any]]: ...
    def collect_json_candidates(self, browser: Any, *, page_url: str = "") -> list[dict[str, Any]]: ...
    def cluster_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def score_cluster(self, cluster: Any) -> float | None: ...
    def build_page_manifest(self, cluster: Any) -> dict[str, Any]: ...
    def build_command(self, job: dict[str, Any]) -> dict[str, str]: ...
    def sanitize_error(self, error: BaseException) -> str: ...


def raw_host_of(url: str) -> str:
    """Lowercased hostname as it appears in a URL, or ``''`` when unparseable."""
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def host_of(url: str) -> str:
    """Canonical host for registered adapters (``www.`` aliases their root host)."""
    host = raw_host_of(url)
    return host[4:] if host.startswith("www.") else host


def resolved_public_addresses(host: str) -> tuple[str, ...]:
    """Return a stable public DNS answer, or ``()`` for an unsafe/unresolved host.

    The result is deliberately re-evaluated whenever an adapter validates a URL.  The
    universal adapter remembers the first answer per host and rejects a changed answer as a
    DNS-rebinding signal.  Callers never persist the addresses.
    """
    if not host:
        return ()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return ()
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError, OSError):
            return ()                       # cannot resolve → refuse
        candidates.extend(str(info[4][0]) for info in infos)
    public: set[str] = set()
    for raw in candidates:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return ()
        # ``is_global`` is stricter than a hand-maintained collection of special ranges and
        # covers loopback, private, link-local, multicast, documentation/reserved and
        # unspecified addresses.
        if not address.is_global:
            return ()
        public.add(str(address))
    return tuple(sorted(public))


def is_private_host(host: str) -> bool:
    """True for loopback/private/link-local/reserved targets, by literal or DNS."""
    return not bool(resolved_public_addresses(host))


def looks_like_challenge(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


@dataclass
class BaseAdapter:
    """Default behaviour. A concrete source overrides only what differs."""

    name: str = "base"
    # This small, non-sensitive contract version lets a later retry/review distinguish
    # adapter behaviour without persisting a reader URL, cookie or selector payload.
    adapter_version: str = "1"
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    # Resource CDNs are explicit adapter-owned policy, distinct from reader hosts.  They are
    # valid only after a candidate is classified as chapter content; they never make a CDN a
    # selectable chapter source.
    resource_hosts: tuple[str, ...] = field(default_factory=tuple)
    runner: str = "run_webtoon.py"
    chapter_path_markers: tuple[str, ...] = field(default_factory=tuple)
    # Selectors are per-source data, not downloader logic.
    container_selector: str = ""
    image_selector: str = "img"
    min_image_width: int = 200
    min_image_height: int = 200
    is_specific: bool = True

    @staticmethod
    def _matches_host(host: str, hosts: tuple[str, ...]) -> bool:
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in hosts)

    @staticmethod
    def _validate_public_url(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme.casefold() not in ALLOWED_SCHEMES:
            raise SourceError(UNSUPPORTED_SOURCE, "scheme")
        if parsed.username or parsed.password:
            raise SourceError(UNSUPPORTED_SOURCE, "credentials_in_url")
        # ``www`` is an allowed-host alias for specific adapters, but it is not a DNS
        # alias.  Resolve the literal hostname that the browser/transport will connect to.
        raw_host = raw_host_of(url)
        if not raw_host:
            raise SourceError(UNSUPPORTED_SOURCE, "missing_host")
        if is_private_host(raw_host):
            raise SourceError(UNSUPPORTED_SOURCE, "private_host")
        return host_of(url)

    # ---- host / url ---------------------------------------------------------
    def supports(self, url: str) -> bool:
        host = host_of(url)
        # Exact host or a dot-suffix subdomain. Never a raw string suffix, which would let
        # "evil-webtoons.com" or "webtoons.com.evil.net" impersonate an allowed host.
        return self._matches_host(host, self.allowed_hosts)

    def normalize_url(self, url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        return urlunparse((
            (parsed.scheme or "https").lower(), (parsed.netloc or "").lower(),
            parsed.path, parsed.params, parsed.query, "",
        ))

    def validate_url(self, url: str) -> None:
        host = self._validate_public_url(url)
        if not (self._matches_host(host, self.allowed_hosts)
                or self._matches_host(host, self.resource_hosts)):
            raise UnsupportedSource(host)

    def validate_navigation_url(self, url: str) -> None:
        """Only a registered reader host may be the top-level browser destination."""
        host = self._validate_public_url(url)
        if not self._matches_host(host, self.allowed_hosts):
            raise UnsupportedSource(host)

    def validate_observed_url(self, url: str) -> None:
        """Validate a browser-observed resource without granting it new authority."""
        self.validate_url(url)

    def validate_path(self, url: str) -> None:
        """The URL must look like a chapter, not a series index or a profile."""
        if not self.chapter_path_markers:
            return
        path = urlparse(str(url or "")).path.casefold()
        if not any(marker in path for marker in self.chapter_path_markers):
            raise SourceError(UNSUPPORTED_SOURCE, "not_a_chapter_url")

    def validate_redirect(self, final_url: str) -> None:
        """A browser redirect must still be an allowed reader *chapter* destination."""
        self.validate_navigation_url(final_url)
        self.validate_path(final_url)

    def authorize_related_url(self, url: str) -> None:
        """Known adapters only authorise resources on their registered hosts."""
        self.validate_url(url)

    # ---- reader knowledge ---------------------------------------------------
    def reader_selectors(self) -> dict[str, str]:
        return {"container": self.container_selector, "image": self.image_selector}

    def classify_candidate(self, candidate: dict[str, Any]) -> str:
        """'chapter' | 'interface' — evidence-based, not class-name-based.

        Class names on these sites churn, so membership combines container context,
        rendered size and DOM order rather than trusting any single fragile signal.
        """
        if self.exclude_candidate(candidate):
            return "interface"
        if candidate.get("inContainer"):
            return "chapter"
        width = int(candidate.get("naturalWidth") or candidate.get("width") or 0)
        height = int(candidate.get("naturalHeight") or candidate.get("height") or 0)
        if width >= self.min_image_width and height >= self.min_image_height:
            return "chapter"
        return "interface"

    def exclude_candidate(self, candidate: dict[str, Any]) -> str:
        """Non-empty reason when this is clearly interface furniture, not a page."""
        haystack = " ".join(str(candidate.get(key) or "") for key in
                            ("url", "className", "id", "alt")).casefold()
        for reason in ("logo", "favicon", "banner", "avatar", "sprite", "icon",
                       "thumbnail", "thumb", "advert", "ads", "promo", "recommend",
                       "footer", "header", "button", "comment", "pixel", "tracking"):
            if reason in haystack:
                return reason
        width = int(candidate.get("naturalWidth") or candidate.get("width") or 0)
        height = int(candidate.get("naturalHeight") or candidate.get("height") or 0)
        if 0 < width <= 2 and 0 < height <= 2:
            return "tracking_pixel"
        return ""

    def absolutize(self, base_url: str, candidate_url: str) -> str:
        return urljoin(base_url, str(candidate_url or "").strip())

    def sanitize_error(self, error: BaseException) -> str:
        """Only the class name or a known code — never a message that may carry a URL."""
        return getattr(error, "code", None) or type(error).__name__

    # ---- analysis contract -------------------------------------------------
    # The default delegates to the bounded generic collector. Site adapters can override
    # individual hooks without making the downloader contain per-site branches. Imports are
    # local because universal_chapter_adapter imports the stable constants from this module.
    def wait_until_ready(self, browser: Any) -> None:
        """Optional readiness hook for a specific reader; the safe default does nothing."""

    def analyze(self, context: Any, *, profile: dict[str, Any] | None = None,
                extra_warnings: tuple[str, ...] = ()) -> Any:
        """Analyse a driver/context without fetching any selected page image.

        Callers may pass a browser directly or a small mapping with ``driver``/``page_url``.
        That boundary keeps adapter-specific analysis independent from downloader internals.
        """
        if isinstance(context, dict):
            browser = context.get("driver") or context.get("browser")
            page_url = str(context.get("page_url") or context.get("url") or "")
        else:
            browser = context
            page_url = str(getattr(browser, "current_url", "") or "")
        if browser is None or not page_url:
            raise SourceError(SOURCE_NOT_READY, "analysis_context")
        self.validate_redirect(page_url)
        self.wait_until_ready(browser)
        # Collect exactly once, then route that immutable observation through the public
        # adapter hooks.  This makes an adapter override effective without re-reading the
        # performance log three times (which can otherwise consume it on the first call).
        collected = self._collected_payload(browser, page_url)
        key = (id(browser), page_url)
        token = _ACTIVE_COLLECTION.set({key: collected})
        try:
            dom = self.collect_dom_candidates(browser, page_url=page_url)
            network = self.collect_network_candidates(browser, page_url=page_url)
            json_candidates = self.collect_json_candidates(browser, page_url=page_url)
            raw_candidates = self.cluster_candidates([
                *list(dom or ()), *list(network or ()), *list(json_candidates or ()),
            ])
            if not isinstance(raw_candidates, (list, tuple)):
                raise SourceError(SOURCE_NOT_READY, "invalid_cluster_candidates")
            from universal_chapter_adapter import analyse_collected

            analysis = analyse_collected(
                page_url,
                {
                    **collected,
                    "candidates": list(raw_candidates),
                    "dom_candidates": list(dom or ()),
                    "network_candidates": list(network or ()),
                    "json_candidates": list(json_candidates or ()),
                },
                self,
                profile=profile,
                extra_warnings=extra_warnings,
                cluster_score=self.score_cluster,
            )
            analysis.page_manifest = self.build_page_manifest(analysis.accepted)
            return analysis
        finally:
            _ACTIVE_COLLECTION.reset(token)

    @staticmethod
    def _collected_payload(browser: Any, page_url: str) -> dict[str, Any]:
        active = _ACTIVE_COLLECTION.get()
        cached = (active or {}).get((id(browser), page_url))
        if cached is not None:
            return cached
        from universal_chapter_adapter import collect_from_driver

        return collect_from_driver(
            browser, page_url or str(getattr(browser, "current_url", "") or ""))

    def collect_dom_candidates(self, browser: Any, *, page_url: str = "") -> list[dict[str, Any]]:
        """Expose bounded DOM evidence to an adapter override or an offline fixture."""
        return list(self._collected_payload(browser, page_url).get("dom_candidates") or [])

    def collect_network_candidates(self, browser: Any, *, page_url: str = "") -> list[dict[str, Any]]:
        """Expose bounded browser-observed network image evidence only."""
        return list(self._collected_payload(browser, page_url).get("network_candidates") or [])

    def collect_json_candidates(self, browser: Any, *, page_url: str = "") -> list[dict[str, Any]]:
        """Expose only recognised JSON/data candidates; arbitrary script text is never eval'd."""
        return list(self._collected_payload(browser, page_url).get("json_candidates") or [])

    def cluster_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compatibility hook for a specific adapter with its own deterministic grouping."""
        return list(candidates)

    def score_cluster(self, cluster: Any) -> float | None:
        """Optional calibrated site-specific score; ``None`` keeps generic scoring."""
        return None

    def build_page_manifest(self, cluster: Any) -> dict[str, Any]:
        """Produce an opaque selection manifest, never raw candidate URLs."""
        values = getattr(cluster, "candidates", cluster)
        ids: list[str] = []
        for value in values if isinstance(values, (list, tuple)) else ():
            candidate_id = getattr(value, "id", "")
            if not candidate_id and isinstance(value, dict):
                candidate_id = value.get("id") or ""
            if candidate_id:
                ids.append(str(candidate_id))
        return {"adapter": self.name, "adapter_version": self.adapter_version,
                "candidate_ids": ids}

    def build_command(self, job: dict[str, Any]) -> dict[str, str]:
        """Describe the adapter-owned runner selection without producing shell syntax."""
        return {"runner": self.runner, "adapter": self.name,
                "adapter_version": self.adapter_version}


# Webtoons keeps its own selectors; the downloader no longer knows about them.
WEBTOONS = BaseAdapter(
    name="webtoons",
    allowed_hosts=("webtoons.com", "webtoon.com"),
    resource_hosts=("webtoon-phinf.pstatic.net",),
    runner="run_webtoon.py",
    chapter_path_markers=("/viewer", "/episode"),
    container_selector="#_imageList, .viewer_img, .viewer_lst",
    image_selector="#_imageList img, .viewer_img img._images",
)


class GenericImageChapterAdapter(BaseAdapter):
    """Template for a site whose reader is plain lazy-loaded images.

    Not registered by default and never a fallback: it must be instantiated with the
    specific hosts an operator has both support for and the right to read.
    """

    def __init__(self, *, name: str, allowed_hosts: tuple[str, ...],
                 container_selector: str = "", chapter_path_markers: tuple[str, ...] = ()):
        super().__init__(
            name=name, allowed_hosts=allowed_hosts, runner="run_webtoon.py",
            chapter_path_markers=chapter_path_markers,
            container_selector=container_selector, image_selector="img",
        )


_VORTEXSCANS_HOST = "vortexscans.org"
_VORTEXSCANS_CHAPTER_PATH = re.compile(
    r"^/series/[a-z0-9][a-z0-9-]*/chapter-[a-z0-9][a-z0-9._-]*/?$",
    re.IGNORECASE,
)


class VortexScansAdapter(BaseAdapter):
    """Conservative adapter for VortexScans chapter URLs.

    The adapter intentionally claims only the literal public reader host.  It does not
    promote arbitrary CDNs merely because a page refers to them: an additional resource host
    can be added only after its reader behaviour is separately verified and covered by tests.
    """

    adapter_version = "1"

    def __init__(self):
        super().__init__(
            name="vortexscans",
            allowed_hosts=(_VORTEXSCANS_HOST,),
            runner="run_webtoon.py",
            container_selector=(
                ".reading-content, .chapter-content, #chapter-content, .reader-area"
            ),
            image_selector=(
                ".reading-content img, .chapter-content img, #chapter-content img, "
                ".reader-area img"
            ),
        )
        # Each selected adapter is a fresh instance, so these run-local grants cannot leak
        # across jobs.  A public CDN is never a selectable reader host; it becomes fetchable
        # only after this browser observation places it in the accepted reader cluster.
        self._observed_resource_hosts: set[str] = set()
        self._authorized_resource_hosts: set[str] = set()

    @staticmethod
    def _has_exact_host(url: str) -> bool:
        return raw_host_of(url) == _VORTEXSCANS_HOST

    def supports(self, url: str) -> bool:
        # Unlike BaseAdapter's shared suffix rule, this source is intentionally not a
        # wildcard for unverified subdomains or a ``www`` alias.
        return self._has_exact_host(url)

    def _validate_exact_vortex_url(self, url: str) -> None:
        self._validate_public_url(url)
        if not self._has_exact_host(url):
            raise UnsupportedSource(raw_host_of(url))

    def validate_url(self, url: str) -> None:
        host = raw_host_of(url)
        if self._has_exact_host(url):
            self._validate_exact_vortex_url(url)
            return
        self._validate_public_url(url)
        if host not in self._authorized_resource_hosts:
            raise UnsupportedSource(host)

    def validate_navigation_url(self, url: str) -> None:
        self._validate_exact_vortex_url(url)

    def validate_path(self, url: str) -> None:
        """Accept only a normal, generic ``/series/<slug>/chapter-<slug>`` path."""
        try:
            path = urlparse(str(url or "")).path
        except ValueError:
            path = ""
        if not _VORTEXSCANS_CHAPTER_PATH.fullmatch(path):
            raise SourceError(UNSUPPORTED_SOURCE, "not_a_chapter_url")

    def validate_observed_url(self, url: str) -> None:
        """Validate an observed public asset without granting it download authority yet."""
        host = raw_host_of(url)
        self._validate_public_url(url)
        if host:
            if (host not in self._observed_resource_hosts
                    and len(self._observed_resource_hosts) >= MAX_OBSERVED_RESOURCE_HOSTS):
                raise SourceError(UNSUPPORTED_SOURCE, "too_many_resource_hosts")
            self._observed_resource_hosts.add(host)

    def authorize_related_url(self, url: str) -> None:
        host = raw_host_of(url)
        self._validate_public_url(url)
        if host not in self._observed_resource_hosts:
            raise SourceError(UNSUPPORTED_SOURCE, "unobserved_resource_host")
        self._authorized_resource_hosts.add(host)


class UniversalChapterAdapter(BaseAdapter):
    """A public-reader fallback with an ephemeral, evidence-based resource policy.

    It is instantiated per submitted URL. It accepts the public source host and resource
    hosts observed in the selected reader cluster; it never turns a generic public URL into
    permission to fetch arbitrary public hosts.
    """

    def __init__(self, source_url: str):
        parsed = urlparse(str(source_url or "").strip())
        source_host = raw_host_of(source_url)
        super().__init__(
            name="universal",
            allowed_hosts=(),
            runner="run_webtoon.py",
            container_selector="",
            image_selector="img",
            min_image_width=200,
            min_image_height=200,
            is_specific=False,
        )
        self._source_host = source_host
        self._related_hosts: set[str] = {source_host} if source_host else set()
        self._source_scheme = parsed.scheme.casefold()
        self._dns_answers: dict[str, tuple[str, ...]] = {}

    def supports(self, url: str) -> bool:
        parsed = urlparse(str(url or "").strip())
        return bool(parsed.hostname and parsed.scheme.casefold() in ALLOWED_SCHEMES)

    @staticmethod
    def _url_host(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme.casefold() not in ALLOWED_SCHEMES:
            raise SourceError(UNSUPPORTED_SOURCE, "scheme")
        if parsed.username or parsed.password:
            raise SourceError(UNSUPPORTED_SOURCE, "credentials_in_url")
        host = raw_host_of(url)
        if not host:
            raise SourceError(UNSUPPORTED_SOURCE, "missing_host")
        return host

    def _validate_public(self, url: str) -> str:
        host = self._url_host(url)
        addresses = resolved_public_addresses(host)
        if not addresses:
            raise SourceError(UNSUPPORTED_SOURCE, "private_host")
        previous = self._dns_answers.get(host)
        if previous is not None and previous != addresses:
            raise SourceError(UNSUPPORTED_SOURCE, "dns_rebinding")
        self._dns_answers[host] = addresses
        return host

    def validate_url(self, url: str) -> None:
        host = self._validate_public(url)
        if host not in self._related_hosts:
            raise SourceError(UNSUPPORTED_SOURCE, "unrelated_resource_host")

    def validate_observed_url(self, url: str) -> None:
        # Observation can see hundreds of DOM resources. Validate each new host once here;
        # selected resources and every actual fetch are revalidated later, so this bounds
        # DNS work without turning a transient observation into an access grant.
        host = self._url_host(url)
        if host in self._dns_answers:
            return
        if len(self._dns_answers) >= MAX_OBSERVED_RESOURCE_HOSTS:
            raise SourceError(UNSUPPORTED_SOURCE, "too_many_resource_hosts")
        self._validate_public(url)

    def validate_navigation_url(self, url: str) -> None:
        # The submitted/redirected reader itself is allowed to change hosts only after the
        # public DNS answer is checked.  Resource hosts never become navigation targets.
        self._validate_public(url)

    def validate_redirect(self, final_url: str) -> None:
        # A top-level redirect comes from the user-supplied navigation. It may move to a
        # different public reader host, but each hop is revalidated and scoped to this run.
        self.validate_navigation_url(final_url)
        host = raw_host_of(final_url)
        self._source_host = host
        self._related_hosts.add(host)

    def authorize_related_url(self, url: str) -> None:
        """Grant one observed public reader resource to this in-memory run only."""
        host = self._validate_public(url)
        self._related_hosts.add(host)

    @property
    def related_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(self._related_hosts))


# Registry order is resolution order. Specific adapters always win over the fallback.
VORTEXSCANS = VortexScansAdapter()
ADAPTERS: tuple[BaseAdapter, ...] = (WEBTOONS, VORTEXSCANS)


def select_adapter(url: str) -> BaseAdapter:
    for adapter in ADAPTERS:
        if adapter.supports(url):
            # Vortex keeps ephemeral, per-run CDN grants.  Returning a fresh adapter avoids
            # cross-job host authority without changing the stable registry/prototype API.
            if isinstance(adapter, VortexScansAdapter):
                return VortexScansAdapter()
            return adapter
    return UniversalChapterAdapter(url)


def supported_hosts() -> list[str]:
    return sorted({host for adapter in ADAPTERS for host in adapter.allowed_hosts})
