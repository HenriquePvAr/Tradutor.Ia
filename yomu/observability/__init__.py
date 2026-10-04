"""Privacy-safe Windows to private-VPS observability."""

from .client import ObservabilityClient
from .events import build_event, redact_event

__all__ = ["ObservabilityClient", "build_event", "redact_event"]
