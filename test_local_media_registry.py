from pathlib import Path

from PIL import Image
import pytest

from local_media_registry import LocalMediaRegistry


def _image(path: Path, color: str = "red") -> Path:
    Image.new("RGB", (40, 30), color).save(path)
    return path


def test_registry_returns_opaque_public_model_and_resolves_order(tmp_path: Path) -> None:
    first, second = _image(tmp_path / "1.jpg"), _image(tmp_path / "2.jpg", "blue")
    registry = LocalMediaRegistry()
    selection = registry.register_selection([first, second], folder=tmp_path)
    items = selection["items"]
    assert all("path" not in item for item in items)
    assert items[0]["thumbnail_url"].startswith("/api/local-media/")
    assert registry.ordered_paths(selection["selection_id"], [items[1]["media_id"], items[0]["media_id"]]) == [str(second.resolve()), str(first.resolve())]


def test_registry_rejects_outside_path_and_revoked_ids(tmp_path: Path) -> None:
    inside, outside = _image(tmp_path / "in.png"), _image(tmp_path.parent / "out.png")
    registry = LocalMediaRegistry()
    with pytest.raises(ValueError, match="outside_selected_folder"):
        registry.register_selection([outside], folder=tmp_path)
    selection = registry.register_selection([inside], folder=tmp_path)
    media_id = selection["items"][0]["media_id"]
    assert registry.revoke(selection["selection_id"]) is True
    assert registry.get(media_id) is None
    with pytest.raises(ValueError, match="selection_invalid"):
        registry.ordered_paths(selection["selection_id"], [media_id])


def test_thumbnail_decodes_to_bounded_png(tmp_path: Path) -> None:
    image = _image(tmp_path / "page.jpg")
    registry = LocalMediaRegistry()
    selection = registry.register_selection([image])
    media = registry.get(selection["items"][0]["media_id"])
    assert media is not None
    data = registry.thumbnail_bytes(media, maximum=16)
    from io import BytesIO
    with Image.open(BytesIO(data)) as thumb:
        assert thumb.format == "PNG"
        assert max(thumb.size) <= 16


def test_loopback_endpoint_serves_only_registered_media(tmp_path: Path) -> None:
    """The route receives an opaque id; no path-shaped request input exists."""
    from fastapi import HTTPException
    from starlette.requests import Request
    import app_ui

    image = _image(tmp_path / "page.png")
    # The route imports the singleton lazily, so use that exact singleton.
    from local_media_registry import LOCAL_MEDIA_REGISTRY
    registry = LOCAL_MEDIA_REGISTRY
    selection = registry.register_selection([image])
    media_id = selection["items"][0]["media_id"]
    request = Request({
        "type": "http", "method": "GET", "path": "/api/local-media/x",
        "headers": [], "client": ("127.0.0.1", 50000), "scheme": "http",
        "server": ("127.0.0.1", 8181),
    })
    response = app_ui.api_local_media_thumbnail(media_id, request)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    registry.revoke(selection["selection_id"])
    with pytest.raises(HTTPException) as rejected:
        app_ui.api_local_media(media_id, request)
    assert rejected.value.status_code == 404
