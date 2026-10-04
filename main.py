"""Compatibility launcher; implementation lives in :mod:`yomu.commands.legacy_gui`."""
from yomu.commands import legacy_gui as _implementation
import sys as _sys

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
