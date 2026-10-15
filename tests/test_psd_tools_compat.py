from __future__ import annotations

import inspect

import pytest
from PIL import Image
from psd_tools.api.layers import PixelLayer

from yomu.export.psd_tools_compat import pixel_layer_from_pil


@pytest.mark.parametrize(("api", "name_parameter"), [("old", "layer_name"), ("new", "name")])
def test_pixel_layer_compat_preserves_name_for_supported_apis(
    monkeypatch, api: str, name_parameter: str,
) -> None:
    calls = []
    marker = object()

    if api == "old":
        def frompil(pil_im, psd_file=None, layer_name="Layer", **kwargs):
            calls.append((pil_im, psd_file, layer_name, kwargs))
            return marker
    else:
        def frompil(image, parent, name="Layer", **kwargs):
            calls.append((image, parent, name, kwargs))
            return marker

    monkeypatch.setattr(PixelLayer, "frompil", staticmethod(frompil))
    assert name_parameter in inspect.signature(PixelLayer.frompil).parameters
    image = Image.new("RGBA", (2, 2), (1, 2, 3, 255))
    parent = object()
    assert pixel_layer_from_pil(image, parent, "ORIGINAL_ART", top=3) is marker
    assert calls == [(image, parent, "ORIGINAL_ART", {"top": 3})]


def test_pixel_layer_compat_rejects_unknown_api(monkeypatch) -> None:
    def unsupported(image, parent, label="Layer"):
        return object()

    monkeypatch.setattr(PixelLayer, "frompil", staticmethod(unsupported))
    with pytest.raises(RuntimeError, match="unsupported_psd_tools_pixel_layer_api"):
        pixel_layer_from_pil(Image.new("RGBA", (1, 1)), object(), "Layer")
