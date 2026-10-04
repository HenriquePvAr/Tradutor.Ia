"""Compatibility launcher; implementation lives in :mod:`yomu.ui.app`."""
from yomu.ui import app as _implementation
import sys as _sys

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
