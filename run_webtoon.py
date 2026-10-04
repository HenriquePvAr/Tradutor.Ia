"""Compatibility launcher; implementation lives in :mod:`yomu.commands.run_webtoon`."""
from yomu.commands import run_webtoon as _implementation
import sys as _sys

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
