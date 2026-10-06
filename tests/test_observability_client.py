import time

import requests

from yomu.observability.client import ObservabilityClient


def test_progress_is_throttled():
    client = ObservabilityClient("http://127.0.0.1:1", progress_interval=60)
    try:
        assert client.emit("REMOTE_PROGRESS", job_id="job-1", status="GENERATING") is True
        assert client.emit("REMOTE_PROGRESS", job_id="job-1", status="GENERATING") is False
    finally:
        client.close()


def test_http_failure_is_recorded_and_does_not_escape(monkeypatch):
    def failed_post(*args, **kwargs):
        response = requests.Response()
        response.status_code = 404
        return response

    monkeypatch.setattr("yomu.observability.client.requests.post", failed_post)
    client = ObservabilityClient("http://127.0.0.1:18778", timeout=0.1)
    try:
        assert client.emit("JOB_STARTED", job_id="job") is True
        deadline = time.monotonic() + 1.0
        while client.delivery_status["delivery_failures"] == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.delivery_status == {"delivery_failures": 1, "last_http_status": 404}
    finally:
        client.close()
