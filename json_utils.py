import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np


def to_json_safe(value):
    """Convert nested Python/NumPy values to JSON-native types."""
    if isinstance(value, np.ndarray):
        return to_json_safe(value.tolist())

    if isinstance(value, np.generic):
        return to_json_safe(value.item())

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, Mapping):
        return {
            str(to_json_safe(key)): to_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_safe(item) for item in value]

    return str(value)


def dump_json(payload, file, **kwargs):
    json.dump(to_json_safe(payload), file, **kwargs)


def dumps_json(payload, **kwargs):
    return json.dumps(to_json_safe(payload), **kwargs)
