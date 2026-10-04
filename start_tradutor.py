"""Compatibility launcher; implementation lives in :mod:`yomu.commands.start_tradutor`."""
from yomu.commands import start_tradutor as _implementation
import sys as _sys

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
