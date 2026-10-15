"""Actual Requests/urllib3 socket-boundary tests with network connects intercepted."""

import socket
import ssl
from unittest import mock

import pytest
import requests
import urllib3.connection
import urllib3.poolmanager
from requests.adapters import HTTPAdapter

import yomu.download.sources.chapter_source as chapter_source
from yomu.download.download_transport import (
    CloudscraperTransport, DownloadLimits, RequestsTransport,
)
from yomu.download.pinned_requests import PinnedRequestError, pinned_request
from yomu.download.sources.canonical_source_identity import RequestsMetadataTransport
from yomu.download.sources.chapter_source import GenericImageChapterAdapter


HOST = "rebinding.example"
URL = f"http://{HOST}/image.png"


def _dns_for(*addresses):
    def resolve(host, port, *args, **kwargs):
        assert host == HOST, f"unexpected DNS lookup: {host}"
        assert port is None, f"hostname was resolved again at connect time: {port}"
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP,
             "", (address, 0, 0, 0) if ":" in address else (address, 0))
            for address in addresses
        ]
    return resolve


def _adapter():
    return GenericImageChapterAdapter(name="pinned-test", allowed_hosts=(HOST,))


def test_actual_requests_socket_uses_public_address_not_connect_time_dns():
    connects = []

    def intercepted(_sock, target):
        connects.append(target)
        raise OSError("synthetic connect interception")

    session = requests.Session()
    transport = RequestsTransport(_adapter(), session=session)
    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34")),
              mock.patch.object(socket.socket, "connect", intercepted)):
            with pytest.raises(requests.exceptions.ConnectionError):
                transport.fetch(URL)
    finally:
        transport.close()

    assert connects == [("93.184.216.34", 80)]


@pytest.mark.parametrize("pool_setup", ["cached", "specific-prefix"])
def test_existing_cached_and_more_specific_http_adapters_are_pinned(pool_setup):
    session = requests.Session()
    if pool_setup == "specific-prefix":
        adapter = HTTPAdapter()
        session.mount(f"http://{HOST}/", adapter)
    else:
        adapter = session.get_adapter("http://")
        prepared = session.prepare_request(requests.Request("GET", URL))
        if hasattr(adapter, "get_connection_with_tls_context"):
            pool = adapter.get_connection_with_tls_context(prepared, verify=True, cert=None)
        else:  # requests 2.32 compatibility
            pool = adapter.get_connection(URL)
        old_connection = pool._new_conn()
        pool._put_conn(old_connection)
    transport = RequestsTransport(_adapter(), session=session)
    connects = []

    def intercepted(_sock, target):
        connects.append(target)
        raise OSError("synthetic connect interception")

    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34")),
              mock.patch.object(socket.socket, "connect", intercepted)):
            with pytest.raises(requests.exceptions.ConnectionError):
                transport.fetch(URL)
        assert session.get_adapter(URL) is adapter
        assert connects == [("93.184.216.34", 80)]
    finally:
        transport.close()


def test_adapter_mounted_after_setup_is_rejected_before_connect():
    session = requests.Session()
    transport = RequestsTransport(_adapter(), session=session)
    session.mount(f"http://{HOST}/", HTTPAdapter())
    connects = []
    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34")),
              mock.patch.object(socket.socket, "connect",
                                side_effect=lambda _sock, target: connects.append(target))):
            with pytest.raises(Exception, match="pinned_transport_unavailable"):
                transport.fetch(URL)
    finally:
        transport.close()
    assert connects == []


def test_existing_adapter_tls_and_pool_configuration_is_preserved_per_instance():
    session = requests.Session()
    adapter = session.get_adapter("https://")
    manager = adapter.poolmanager
    global_class_map = urllib3.poolmanager.pool_classes_by_scheme
    global_classes = global_class_map.copy()
    original_pool_settings = manager.connection_pool_kw.copy()
    original_ssl_context = object()
    manager.connection_pool_kw["ssl_context"] = original_ssl_context
    transport = RequestsTransport(_adapter(), session=session)
    try:
        assert session.get_adapter("https://") is adapter
        assert adapter.poolmanager.connection_pool_kw["ssl_context"] is original_ssl_context
        assert urllib3.poolmanager.pool_classes_by_scheme is global_class_map
        assert urllib3.poolmanager.pool_classes_by_scheme == global_classes
        assert adapter.poolmanager.pool_classes_by_scheme is not global_class_map
    finally:
        transport.close()
        manager.connection_pool_kw.clear()
        manager.connection_pool_kw.update(original_pool_settings)


def test_multiple_public_candidates_share_one_connect_deadline():
    session = requests.Session()
    transport = RequestsTransport(
        _adapter(), session=session,
        limits=DownloadLimits(connect_timeout=0.5, read_timeout=1.0))
    timeouts = []
    connected = []
    original_settimeout = socket.socket.settimeout

    def capture_timeout(sock, value):
        timeouts.append(value)
        return original_settimeout(sock, value)

    def fail_connect(_sock, target):
        connected.append(target)
        import time
        time.sleep(0.02)
        raise OSError("synthetic connection failure")

    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34", "8.8.8.8")),
              mock.patch.object(socket.socket, "settimeout", capture_timeout),
              mock.patch.object(socket.socket, "connect", fail_connect)):
            with pytest.raises(requests.exceptions.ConnectionError):
                transport.fetch(URL)
    finally:
        transport.close()
    assert {target[0] for target in connected} == {"93.184.216.34", "8.8.8.8"}
    assert len(timeouts) == 2
    assert timeouts[1] < timeouts[0]


@pytest.mark.parametrize("answers", [
    ("93.184.216.34", "127.0.0.1"),
    ("2606:4700:4700::1111", "fd00::1"),
    ("::1",),
])
def test_private_mixed_or_loopback_dns_is_rejected_before_socket_connect(answers):
    connects = []
    session = requests.Session()
    transport = RequestsTransport(_adapter(), session=session)
    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for(*answers)),
              mock.patch.object(socket.socket, "connect",
                                side_effect=lambda _sock, target: connects.append(target))):
            with pytest.raises(Exception):
                transport.fetch(URL)
    finally:
        transport.close()
    assert connects == []


def test_https_keeps_original_hostname_for_sni_and_pool_host():
    session = requests.Session()
    transport = RequestsTransport(_adapter(), session=session)
    adapter = session.get_adapter("https://")
    pool = adapter.poolmanager.connection_from_url("https://rebinding.example/secure")
    connection = pool._new_conn()
    connected = []
    tls_identity = []

    def intercepted(_sock, target):
        connected.append(target)

    class WrappedSocket:
        def __init__(self, sock):
            self.socket = sock
            self.is_verified = True

        def __iter__(self):
            # urllib3 2.8 returns the SSL wrapper as a tuple; 2.7 exposes an
            # object with these same attributes. Preserve both real APIs.
            yield self.socket
            yield self.is_verified

    def fake_tls(**kwargs):
        tls_identity.append((kwargs.get("server_hostname"), kwargs.get("assert_hostname"),
                             kwargs.get("cert_reqs"), kwargs.get("ssl_context")))
        return WrappedSocket(kwargs["sock"])

    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("2606:4700:4700::1111")),
              mock.patch.object(socket.socket, "connect", intercepted),
              mock.patch.object(urllib3.connection.http2_probe,
                                "acquire_and_get", return_value=False),
              mock.patch.object(urllib3.connection,
                                "_ssl_wrap_socket_and_match_hostname", side_effect=fake_tls)):
            with pinned_request(session, "https://rebinding.example/secure"):
                connection.connect()
        assert connected == [("2606:4700:4700::1111", 443, 0, 0)]
        assert connection.host == "rebinding.example"
        assert tls_identity == [("rebinding.example", None, ssl.CERT_REQUIRED,
                                 adapter.poolmanager.connection_pool_kw.get("ssl_context"))]
    finally:
        connection.close()
        transport.close()


def test_explicit_proxy_is_rejected_before_connection():
    session = requests.Session()
    transport = RequestsTransport(_adapter(), session=session)
    connects = []
    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34")),
              mock.patch.object(socket.socket, "connect",
                                side_effect=lambda _sock, target: connects.append(target))):
            with pinned_request(session, URL):
                with pytest.raises(requests.exceptions.ProxyError):
                    session.get(URL, proxies={"http": "http://127.0.0.1:8888"})
    finally:
        transport.close()
    assert connects == []


def test_credentials_in_url_and_unknown_unmountable_session_fail_closed():
    session = requests.Session()
    transport = RequestsTransport(_adapter(), session=session)
    try:
        with pytest.raises(PinnedRequestError):
            with pinned_request(session, "http://user:secret@rebinding.example/image.png"):
                pass
    finally:
        transport.close()

    class UnknownSession:
        def get(self, *args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("unknown session must fail before request")

    with pytest.raises(Exception, match="pinned_transport_unavailable"):
        RequestsTransport(_adapter(), session=UnknownSession())


def test_metadata_and_cloudscraper_callers_use_the_same_pinned_boundary():
    connects = []

    def intercepted(_sock, target):
        connects.append(target)
        raise OSError("synthetic connect interception")

    metadata = RequestsMetadataTransport(_adapter())
    scraper = CloudscraperTransport(
        _adapter(), session_factory=requests.Session)
    try:
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34")),
              mock.patch.object(socket.socket, "connect", intercepted)):
            with pytest.raises(Exception):
                metadata.fetch_page(URL)
        assert connects == [("93.184.216.34", 80)]
        connects.clear()
        with (mock.patch.object(chapter_source.socket, "getaddrinfo",
                                side_effect=_dns_for("93.184.216.34")),
              mock.patch.object(socket.socket, "connect", intercepted)):
            with pytest.raises(requests.exceptions.ConnectionError):
                scraper.fetch(URL)
        assert connects == [("93.184.216.34", 80)]
    finally:
        metadata.close()
        scraper.close()
