"""Pin Requests sockets to the public DNS answer validated for that request.

The URL and urllib3 pool retain the original hostname, so Host, certificate checks, and TLS
SNI continue to use the hostname. Only the TCP socket's destination is replaced with a
validated numeric address. All customization is confined to each mounted adapter instance.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
import ipaddress
import math
import socket
import time
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy
from urllib3.exceptions import NewConnectionError

from yomu.download.sources.chapter_source import resolved_public_addresses


class PinnedRequestError(ValueError):
    """A sanitized fail-closed reason from the pinned Requests boundary."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class _PinnedDestination:
    host: str
    port: int
    addresses: tuple[str, ...]
    connect_deadline: float | None = None


_ACTIVE_DESTINATION: ContextVar[_PinnedDestination | None] = ContextVar(
    "yomu_pinned_requests_destination", default=None)
_INSTALLED = "_yomu_pinned_requests_installed"
_ORIGINAL_SEND = "_yomu_pinned_requests_original_send"
MAX_PINNED_ADDRESSES = 16


def _normal_host(value: Any) -> str:
    return str(value or "").rstrip(".").casefold()


class _PinnedConnectionMixin:
    """urllib3 connection mixin that never resolves the destination hostname."""

    def _new_conn(self):
        pin = _ACTIVE_DESTINATION.get()
        if (pin is None or _normal_host(getattr(self, "host", "")) != pin.host
                or int(getattr(self, "port", 0) or 0) != pin.port):
            raise NewConnectionError(self, "No validated destination for this request")

        last_error: OSError | None = None
        for raw_address in pin.addresses:
            remaining = (pin.connect_deadline - time.monotonic()
                         if pin.connect_deadline is not None else None)
            if remaining is not None and remaining <= 0:
                break
            address = ipaddress.ip_address(raw_address)
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                timeout = getattr(self, "timeout", None)
                if hasattr(timeout, "connect_timeout"):
                    timeout = timeout.connect_timeout
                if remaining is not None:
                    timeout = remaining if timeout is None else min(float(timeout), remaining)
                if timeout is not None and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                source_address = getattr(self, "source_address", None)
                if source_address:
                    sock.bind(source_address)
                for option in getattr(self, "socket_options", None) or ():
                    sock.setsockopt(*option)
                sockaddr = ((str(address), pin.port, 0, 0) if address.version == 6
                            else (str(address), pin.port))
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last_error = exc
                sock.close()

        message = "Validated public address connection failed"
        if last_error is not None:
            raise NewConnectionError(self, message) from last_error
        raise NewConnectionError(self, "No validated public address")


def _pinned_pool_class(base_pool: type) -> type:
    base_connection = getattr(base_pool, "ConnectionCls", None)
    if not isinstance(base_connection, type):
        raise PinnedRequestError("pinned_transport_unavailable")
    connection = type(
        f"Pinned{base_connection.__name__}",
        (_PinnedConnectionMixin, base_connection),
        {"__module__": __name__},
    )
    return type(
        f"Pinned{base_pool.__name__}",
        (base_pool,),
        {"ConnectionCls": connection, "__module__": __name__},
    )


def _install_adapter(adapter: Any) -> None:
    if getattr(adapter, _INSTALLED, False):
        return
    if not isinstance(adapter, HTTPAdapter):
        raise PinnedRequestError("pinned_transport_unavailable")
    manager = getattr(adapter, "poolmanager", None)
    pool_classes = getattr(manager, "pool_classes_by_scheme", None)
    if not isinstance(pool_classes, dict):
        raise PinnedRequestError("pinned_transport_unavailable")

    # Retire any plain pools created before installation so cached connections cannot bypass
    # the replacement pool classes. clear() closes those pools without changing SSL settings.
    clear = getattr(manager, "clear", None)
    if not callable(clear):
        raise PinnedRequestError("pinned_transport_unavailable")
    clear()

    # Copy the manager's own map. Do not modify urllib3 module/class globals or replace the
    # adapter, so its configured SSL context, retries, pool sizing, and subclass behavior stay.
    pinned_classes = pool_classes.copy()
    for scheme in ("http", "https"):
        base_pool = pool_classes.get(scheme)
        if base_pool is None:
            raise PinnedRequestError("pinned_transport_unavailable")
        pinned_classes[scheme] = _pinned_pool_class(base_pool)
    manager.pool_classes_by_scheme = pinned_classes

    original_send = adapter.send

    def guarded_send(request, **kwargs):
        pin = _ACTIVE_DESTINATION.get()
        parsed = urlparse(str(getattr(request, "url", "") or ""))
        try:
            port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
        except ValueError:
            port = 0
        if (pin is None or parsed.username or parsed.password
                or parsed.scheme.casefold() not in {"http", "https"}
                or _normal_host(parsed.hostname) != pin.host or port != pin.port):
            raise requests.exceptions.ConnectionError(
                "Request has no matching validated public destination")
        if select_proxy(str(request.url), kwargs.get("proxies") or {}):
            raise requests.exceptions.ProxyError(
                "Proxy routing is unavailable for pinned public requests")
        timeout = kwargs.get("timeout")
        if hasattr(timeout, "connect_timeout"):
            timeout = timeout.connect_timeout
        elif isinstance(timeout, (tuple, list)):
            timeout = timeout[0] if timeout else None
        try:
            connect_timeout = float(timeout)
        except (TypeError, ValueError, OverflowError):
            connect_timeout = 0.0
        if not math.isfinite(connect_timeout) or connect_timeout <= 0:
            raise requests.exceptions.ConnectTimeout(
                "A finite connect timeout is required for pinned public requests")
        token = _ACTIVE_DESTINATION.set(replace(
            pin, connect_deadline=time.monotonic() + connect_timeout))
        try:
            return original_send(request, **kwargs)
        finally:
            _ACTIVE_DESTINATION.reset(token)

    adapter.send = guarded_send
    try:
        setattr(adapter, _ORIGINAL_SEND, original_send)
        setattr(adapter, _INSTALLED, True)
    except Exception as exc:
        raise PinnedRequestError("pinned_transport_unavailable") from exc


def install_pinned_requests(session: Any) -> None:
    """Install per-adapter pinning, or fail closed for an unknown session/adapter."""
    if (not callable(getattr(session, "mount", None))
            or not callable(getattr(session, "get_adapter", None))):
        raise PinnedRequestError("pinned_transport_unavailable")
    mounts = getattr(session, "adapters", None)
    if not callable(getattr(mounts, "values", None)):
        raise PinnedRequestError("pinned_transport_unavailable")
    try:
        adapters = list(mounts.values())
    except Exception as exc:
        raise PinnedRequestError("pinned_transport_unavailable") from exc
    if not adapters or any(adapter is None for adapter in adapters):
        raise PinnedRequestError("pinned_transport_unavailable")
    for adapter in {id(value): value for value in adapters}.values():
        _install_adapter(adapter)
    try:
        setattr(session, _INSTALLED, True)
    except Exception as exc:
        raise PinnedRequestError("pinned_transport_unavailable") from exc


class _PinnedRequestScope:
    def __init__(self, session: Any, url: str):
        self.session = session
        self.url = url
        self.token = None

    def __enter__(self):
        if not getattr(self.session, _INSTALLED, False):
            raise PinnedRequestError("pinned_transport_unavailable")
        try:
            parsed = urlparse(str(self.url or ""))
            host = parsed.hostname
            scheme = parsed.scheme.casefold()
            port = parsed.port or (443 if scheme == "https" else 80)
        except (TypeError, ValueError):
            raise PinnedRequestError("invalid_destination") from None
        if (scheme not in {"http", "https"} or not host
                or parsed.username or parsed.password):
            raise PinnedRequestError("invalid_destination")
        try:
            selected_adapter = self.session.get_adapter(self.url)
        except Exception:
            raise PinnedRequestError("pinned_transport_unavailable") from None
        if not getattr(selected_adapter, _INSTALLED, False):
            raise PinnedRequestError("pinned_transport_unavailable")
        addresses = resolved_public_addresses(host)
        if not addresses:
            raise PinnedRequestError("public_address_unavailable")
        if len(addresses) > MAX_PINNED_ADDRESSES:
            raise PinnedRequestError("too_many_public_addresses")
        # resolved_public_addresses rejects the whole answer if any member is non-global;
        # validate again here before it becomes a socket target.
        try:
            if any(not ipaddress.ip_address(value).is_global for value in addresses):
                raise PinnedRequestError("public_address_unavailable")
        except ValueError:
            raise PinnedRequestError("public_address_unavailable") from None
        destination = _PinnedDestination(_normal_host(host), port, tuple(addresses))
        self.token = _ACTIVE_DESTINATION.set(destination)
        return destination

    def __exit__(self, exc_type, exc, traceback):
        if self.token is not None:
            _ACTIVE_DESTINATION.reset(self.token)
            self.token = None
        return False


def pinned_request(session: Any, url: str) -> _PinnedRequestScope:
    """Scope one already-authorized URL to its freshly validated public DNS answer."""
    return _PinnedRequestScope(session, url)
