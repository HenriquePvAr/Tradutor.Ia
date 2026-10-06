from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    UPLOADING = "UPLOADING"
    WAITING_PREVIEW = "WAITING_PREVIEW"
    PROMPTING = "PROMPTING"
    GENERATING = "GENERATING"
    RECOVERING_OUTPUT = "RECOVERING_OUTPUT"
    ALIGNING = "ALIGNING"
    COMPOSITING = "COMPOSITING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"
    AUTH_INTERACTION_REQUIRED = "AUTH_INTERACTION_REQUIRED"


@dataclass
class Job:
    job_id: str
    status: str = JobStatus.QUEUED.value
    source_sha256: str = ""
    original_path: str = ""
    normalized_path: str = ""
    raw_path: str = ""
    final_path: str = ""
    authorized_mask_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Job":
        return cls(**value)


def path_for_job(root: Path, job_id: str) -> Path:
    return root / "jobs" / job_id
