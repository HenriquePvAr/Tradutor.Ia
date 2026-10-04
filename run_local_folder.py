"""Compatibility launcher; implementation lives in :mod:`yomu.commands.run_local_folder`."""
from yomu.commands import run_local_folder as _implementation
import sys as _sys

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
