from yomu.observability.client import ObservabilityClient


def test_progress_is_throttled():
    client = ObservabilityClient("http://127.0.0.1:1", progress_interval=60)
    try:
        assert client.emit("REMOTE_PROGRESS", job_id="job-1", status="GENERATING") is True
        assert client.emit("REMOTE_PROGRESS", job_id="job-1", status="GENERATING") is False
    finally:
        client.close()
