"""Compatibility seam for psd-tools' renamed PixelLayer.frompil argument."""

from __future__ import annotations

import inspect
from typing import Any

from PIL import Image


def pixel_layer_from_pil(
    image: Image.Image,
    parent: Any,
    layer_name: str,
    **kwargs: Any,
) -> Any:
    """Create a named pixel layer across the supported psd-tools APIs."""
    from psd_tools.api.layers import PixelLayer

    parameters = inspect.signature(PixelLayer.frompil).parameters
    if "layer_name" in parameters:
        name_argument = "layer_name"
    elif "name" in parameters:
        name_argument = "name"
    else:
        raise RuntimeError("unsupported_psd_tools_pixel_layer_api")
    return PixelLayer.frompil(image, parent, **{name_argument: layer_name}, **kwargs)
