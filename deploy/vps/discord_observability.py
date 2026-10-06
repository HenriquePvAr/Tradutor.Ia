"""Compatibility import for the canonical VPS observability dispatcher."""
from research.gemini_vps_web_worker.discord_observability import (
    _accept,
    _content,
    _deliver,
    _event_time,
    _webhook,
    ingest,
    observability_health,
    router,
    requests,
    time,
)

__all__ = ["router", "ingest", "observability_health"]
