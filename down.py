"""Compatibility launcher; implementation lives in :mod:`yomu.download.down`."""
from yomu.download import down as _implementation
import sys as _sys

_sys.modules[__name__] = _implementation
