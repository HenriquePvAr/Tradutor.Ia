import pytest
import requests

from yomu.clean.gemini_fallback.remote_client import HardCleanRemoteClient, RemoteTransportError


@pytest.mark.parametrize(("exception", "expected"), [
    (requests.exceptions.ConnectionError("[Errno 10061] refused"), "CONNECTION_REFUSED"),
    (requests.exceptions.ConnectionError("[Errno 10054] reset"), "CONNECTION_RESET"),
    (requests.exceptions.Timeout("secret host and token"), "HTTP_TIMEOUT"),
])
def test_remote_client_raises_sanitized_transport_code(monkeypatch, tmp_path, exception, expected):
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    monkeypatch.setattr("yomu.clean.gemini_fallback.remote_client.requests.post",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(exception))
    with pytest.raises(RemoteTransportError) as caught:
        HardCleanRemoteClient().submit(image)
    assert caught.value.safe_error_code == expected
    assert str(caught.value) == expected
    assert "secret" not in str(caught.value)


def test_remote_client_classifies_http_status(monkeypatch, tmp_path):
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    response = requests.Response()
    response.status_code = 503
    response.url = "http://127.0.0.1/private?token=hidden"
    monkeypatch.setattr("yomu.clean.gemini_fallback.remote_client.requests.post", lambda *_a, **_k: response)
    with pytest.raises(RemoteTransportError) as caught:
        HardCleanRemoteClient().submit(image)
    assert caught.value.safe_error_code == "HTTP_ERROR_503"
    assert "token" not in str(caught.value)
