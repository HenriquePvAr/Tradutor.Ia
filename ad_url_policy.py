"""Strict URL policy for Yomu passive-ad placements and top-level navigation."""

from urllib.parse import unquote, urlsplit


LEGACY_GITHUB_HOST = "henriquepvar.github.io"
CANONICAL_AD_HOST = "yomusekai.com.br"
GAME_DEALS_HOST = "game-deals-alpha.vercel.app"
ALLOWED_AD_HOSTS = frozenset({LEGACY_GITHUB_HOST, CANONICAL_AD_HOST, GAME_DEALS_HOST})


def _ad_url_parts(url):
    if not isinstance(url, str) or not url or any(ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.lower() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.hostname.lower() not in ALLOWED_AD_HOSTS):
            return None
    except (ValueError, UnicodeError):
        return None
    path = parsed.path
    decoded = unquote(path)
    if (not path.startswith("/ad/") or "\\" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))):
        return None
    return parsed, parsed.hostname.lower(), path


def canonical_ad_url(url):
    """Canonicalize only the known GitHub Pages legacy origin; reject unsafe URLs."""
    parts = _ad_url_parts(url)
    if parts is None:
        return None
    parsed, host, _path = parts
    target_host = CANONICAL_AD_HOST if host == LEGACY_GITHUB_HOST else host
    return f"https://{target_host}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


def is_allowed_ad_url(url):
    return _ad_url_parts(url) is not None


def is_allowed_ad_navigation(requested_url, final_url):
    """Allow same-origin navigation, plus only the legacy GitHub→canonical redirect."""
    requested = _ad_url_parts(requested_url)
    final = _ad_url_parts(final_url)
    if requested is None or final is None:
        return False
    requested_parts, requested_host, requested_path = requested
    final_parts, final_host, final_path = final
    if requested_path != final_path or requested_parts.query != final_parts.query:
        return False
    if requested_host == final_host:
        return True
    return requested_host == LEGACY_GITHUB_HOST and final_host == CANONICAL_AD_HOST
