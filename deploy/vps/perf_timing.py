"""Low-overhead monotonic phase timing for the Gemini performance experiment."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PhaseTimer:
    started_at: float = field(default_factory=time.monotonic)
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str) -> float:
        value = time.monotonic()
        self.marks[name] = value
        return value

    def durations_ms(self) -> dict[str, float]:
        ordered = [("request_enqueued_at", self.started_at)] + list(self.marks.items())
        result: dict[str, float] = {}
        for (left_name, left), (right_name, right) in zip(ordered, ordered[1:]):
            phase = right_name.removesuffix("_at")
            result[phase + "_ms"] = round(max(0.0, right - left) * 1000.0, 3)
        if self.marks:
            result["total_request_ms"] = round((max(self.marks.values()) - self.started_at) * 1000.0, 3)
        return result

    def export(self) -> dict:
        return {
            "clock": "monotonic",
            "started_at_monotonic": self.started_at,
            "marks_monotonic": dict(self.marks),
            "durations_ms": self.durations_ms(),
        }
