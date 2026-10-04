"""Compatibility launcher; implementation lives in :mod:`yomu.runtime.worker_service`."""
from yomu.runtime import worker_service as _implementation
import sys as _sys

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
_sys.modules[__name__] = _implementation
