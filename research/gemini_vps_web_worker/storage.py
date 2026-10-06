from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import BinaryIO

from .models import Job, JobStatus, path_for_job


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        for name in ("pending", "processing", "completed", "review_required", "failed"):
            (self.root / "queue" / name).mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)

    def create(self, upload: BinaryIO, *, filename: str, job_id: str | None = None,
               metadata: dict | None = None, mask: BinaryIO | None = None) -> Job:
        job_id = job_id or str(uuid.uuid4())
        job_dir = path_for_job(self.root, job_id)
        if job_dir.exists():
            existing = self.get(job_id)
            if existing:
                return existing
            raise FileExistsError(job_id)
        job_dir.mkdir(parents=True)
        original = job_dir / (Path(filename).name or "original.bin")
        with original.open("wb") as target:
            while chunk := upload.read(1024 * 1024):
                target.write(chunk)
        if original.stat().st_size == 0:
            raise ValueError("zero-byte image")
        mask_path = None
        if mask is not None:
            mask_path = str(job_dir / "authorized_mask.png")
            with open(mask_path, "wb") as target:
                while chunk := mask.read(1024 * 1024):
                    target.write(chunk)
        from research.gemini_hard_clean_fallback.contracts import HardCleanJob
        digest = HardCleanJob.sha256(original)
        job = Job(job_id=job_id, source_sha256=digest, original_path=str(original),
                  authorized_mask_path=mask_path, metadata=metadata or {})
        self._write(job)
        self._atomic_queue(job_id, "pending")
        return job

    def _write(self, job: Job) -> None:
        target = path_for_job(self.root, job.job_id) / "job.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(job.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, target)

    def _atomic_queue(self, job_id: str, state: str) -> None:
        marker = path_for_job(self.root, job_id) / "queue.marker"
        target = self.root / "queue" / state / job_id
        temp = target.with_suffix(".tmp")
        temp.write_text(str(marker), encoding="utf-8")
        os.replace(temp, target)

    def get(self, job_id: str) -> Job | None:
        path = path_for_job(self.root, job_id) / "job.json"
        if not path.exists():
            return None
        return Job.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def update(self, job: Job, status: JobStatus | str | None = None, **changes) -> Job:
        if status is not None:
            job.status = status.value if isinstance(status, JobStatus) else str(status)
        for key, value in changes.items():
            setattr(job, key, value)
        self._write(job)
        return job

    def claim_next(self) -> Job | None:
        pending = self.root / "queue" / "pending"
        for marker in sorted(pending.iterdir()):
            if not marker.is_file():
                continue
            processing = self.root / "queue" / "processing" / marker.name
            try:
                os.replace(marker, processing)
            except FileNotFoundError:
                continue
            job = self.get(marker.name)
            if job:
                self.update(job, JobStatus.PREPARING)
                return job
        return None

    def move_marker(self, job_id: str, state: str) -> None:
        marker = self.root / "queue" / "processing" / job_id
        target = self.root / "queue" / state / job_id
        if marker.exists():
            os.replace(marker, target)

