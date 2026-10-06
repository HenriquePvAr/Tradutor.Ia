from __future__ import annotations

import time
from pathlib import Path
import requests


class HardCleanRemoteClient:
    def __init__(self, base_url: str = "http://127.0.0.1:18777"):
        self.base_url = base_url.rstrip("/")

    def submit(self, image: str | Path, authorized_mask: str | Path | None = None, **fields):
        with open(image, "rb") as handle:
            files = {"image": (Path(image).name, handle, "image/png")}
            if authorized_mask is not None:
                with open(authorized_mask, "rb") as mask_handle:
                    files["authorized_mask"] = (Path(authorized_mask).name, mask_handle, "image/png")
                    response = requests.post(f"{self.base_url}/v1/hard-clean/jobs", files=files, data=fields, timeout=30)
            else:
                response = requests.post(f"{self.base_url}/v1/hard-clean/jobs", files=files, data=fields, timeout=30)
        response.raise_for_status()
        return response.json()

    def status(self, job_id: str):
        response = requests.get(f"{self.base_url}/v1/hard-clean/jobs/{job_id}", timeout=30)
        response.raise_for_status()
        return response.json()

    def wait(self, job_id: str, timeout: float = 600, poll: float = 2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status(job_id)
            if status["status"] in {"COMPLETED", "REVIEW_REQUIRED", "FAILED", "AUTH_INTERACTION_REQUIRED"}:
                return status
            time.sleep(poll)
        raise TimeoutError(job_id)

    def download_result(self, job_id: str, destination: str | Path):
        response = requests.get(f"{self.base_url}/v1/hard-clean/jobs/{job_id}/result", timeout=60)
        response.raise_for_status()
        Path(destination).write_bytes(response.content)
        return destination
