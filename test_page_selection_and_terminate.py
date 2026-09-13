import _test_bootstrap  # noqa: F401

from unittest.mock import patch

import benchmark_pipeline
import job_runner


def test_explicit_max_images_is_not_expanded_by_smart_split(monkeypatch):
    monkeypatch.setattr(benchmark_pipeline.config, "SMART_WEBTOON_PDF_SPLIT", True)
    assert benchmark_pipeline._resolve_download_max_images(5, [], "", []) == 5


def test_approved_candidate_selection_remains_bounded():
    assert benchmark_pipeline._resolve_download_max_images(None, [], "", ["a", "b"]) is None


def test_terminate_treats_windows_invalid_handle_as_idempotent():
    class Proc:
        pid = 123
        exited = False
        def poll(self):
            return 0 if self.exited else None
        def send_signal(self, _signal):
            raise SystemError("kill returned a result with an exception set")
        def wait(self, timeout=None):
            self.exited = True
            return 0

    with patch.object(job_runner.os, "name", "nt"), patch.object(
        job_runner.process_tree, "terminate_tree"
    ) as terminate_tree:
        job_runner._terminate(Proc())
        terminate_tree.assert_not_called()
