"""Hermetic TDD coverage for safe selective artifact reconstruction tooling."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import hashlib
import json
from pathlib import Path

import pytest

from job_store import JobStatus, JobStore


PDF_BYTES = b"%PDF-1.4\nsynthetic reconstruction\n%%EOF\n"


class SpyRenderer:
    def __init__(self, *, fail: bool = False, reason: str = "render boom"):
        self.fail = fail
        self.reason = reason
        self.calls: list[dict] = []

    def __call__(self, *, source_page: Path, destination: Path, page: dict, item: dict, candidate: str) -> dict:
        self.calls.append({
            "source_page": source_page,
            "destination": destination,
            "page_index": page["index"],
            "region_id": item["region_id"],
            "candidate": candidate,
        })
        if self.fail:
            raise RuntimeError(self.reason)
        destination.write_bytes(b"PNG:" + candidate.encode("utf-8"))
        return {"visual_validation_passed": True, "text_overflow_ratio": 0.0}


class SpyPdfBuilder:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, image_paths, pdf_path):
        self.calls.append([str(path) for path in image_paths])
        if self.fail:
            raise RuntimeError("pdf boom")
        Path(pdf_path).write_bytes(PDF_BYTES)


class SpyQualityBuilder:
    def __init__(self, *, fail: bool = False, passed: bool = True, run_id: str | None = None,
                 sha256: str | None = None, size: int | None = None):
        self.fail = fail
        self.passed = passed
        self.run_id = run_id
        self.sha256 = sha256
        self.size = size
        self.calls = 0

    def __call__(self, *, run_id: str, artifact_sha256: str, artifact_size_bytes: int,
                 pages: list[dict], source: dict) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("quality boom")
        effective_run = self.run_id if self.run_id is not None else run_id
        effective_sha = self.sha256 if self.sha256 is not None else artifact_sha256
        effective_size = self.size if self.size is not None else artifact_size_bytes
        return {
            "summary": {
                "run_id": effective_run,
                "artifact_sha256": effective_sha,
                "artifact_size_bytes": effective_size,
                "quality_validation": {
                    "passed": self.passed,
                    "status": "passed" if self.passed else "review_required",
                    "manual_review_required_groups": 0 if self.passed else 1,
                    "run_id": effective_run,
                    "artifact_sha256": effective_sha,
                    "artifact_size_bytes": effective_size,
                },
            },
            "pages": pages,
        }


class StaleOnSecondSourceReadStore:
    def __init__(self, inner: JobStore, source_job_id: str):
        self.inner = inner
        self.source_job_id = source_job_id
        self.source_reads = 0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def get_job(self, job_id: str):
        job = self.inner.get_job(job_id)
        if job_id != self.source_job_id or not job:
            return job
        self.source_reads += 1
        if self.source_reads >= 2:
            changed = dict(job)
            changed["run_id"] = "changed-run"
            return changed
        return job


class FailPromotionUpdateStore:
    def __init__(self, inner: JobStore):
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def update_fields(self, job_id: str, **fields):
        if str(fields.get("pdf_path") or "").endswith("artifact.pdf"):
            raise RuntimeError("db promotion boom")
        return self.inner.update_fields(job_id, **fields)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture(tmp_path: Path, *, candidate: str = "SE FOR PRECISO, EU VOU.",
             extra_candidate: str = "", missing_candidate: bool = False,
             missing_geometry: bool = False, missing_source_page: bool = False,
             missing_non_target: bool = False, current_run: str = "source-run") -> tuple[JobStore, str, Path, str]:
    output = tmp_path / "output"
    pages = output / "pages"
    source = output / "source"
    pages.mkdir(parents=True)
    source.mkdir(parents=True)
    for index in (1, 2, 3):
        if index != 2 or not missing_source_page:
            (source / f"page_{index:03d}.png").write_bytes(f"SOURCE{index}".encode("ascii"))
        if index != 3 or not missing_non_target:
            (pages / f"page_{index:03d}.png").write_bytes(f"PAGE{index}".encode("ascii"))
    item = {
        "id": "BALAO_TEST",
        "region_id": "REGION_TEST_001",
        "classification": "narration",
        "clean_text": "IF NEEDED, I WILL GO.",
        "original_text": "IF NEEDED, I WILL GO.",
        "translation": "IF NEEDED, I WILL GO.",
        "translation_candidate": "" if missing_candidate else candidate,
        "translation_valid": False,
        "translation_validation_reason": "mixed_language_tokens:FOR",
        "translation_final_state": "manual_review",
        "preserved_original": True,
        "manual_review_required": True,
    }
    if extra_candidate:
        item["rejected_translation"] = extra_candidate
    if not missing_geometry:
        item["bounding_box"] = [10, 20, 120, 40]
    progress_pages = []
    for index in (1, 2, 3):
        progress_pages.append({
            "index": index,
            "output_path": str(pages / f"page_{index:03d}.png"),
            "image_path": str(source / f"page_{index:03d}.png"),
            "status": "completed",
            "debug_data": {"items": [item] if index == 2 else []},
        })
    (output / "progress.json").write_text(json.dumps({
        "status": "review_required",
        "pdf_path": str(output / "chapter.pdf"),
        "pages": progress_pages,
    }), encoding="utf-8")
    old_pdf = b"%PDF-1.4\nold synthetic blocked artifact\n%%EOF\n"
    old_sha = hashlib.sha256(old_pdf).hexdigest()
    (output / "quality_report.json").write_text(json.dumps({
        "summary": {
            "pdf_path": str(output / "chapter.pdf"),
            "run_id": current_run,
            "artifact_sha256": old_sha,
            "artifact_size_bytes": len(old_pdf),
            "quality_validation": {
                "passed": False,
                "status": "review_required",
                "manual_review_required_groups": 1,
                "run_id": current_run,
                "artifact_sha256": old_sha,
                "artifact_size_bytes": len(old_pdf),
            },
        },
        "pages": [{"index": 1}, {"index": 2}, {"index": 3}],
    }), encoding="utf-8")
    (output / "chapter.pdf").write_bytes(old_pdf)
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id = store.create_job(
        source_url="https://example.invalid/synthetic",
        output_dir=str(output),
        command=["synthetic"],
        configuration={"job_type": "translation", "community_owner_id": "owner-a"},
        run_id=current_run,
    )
    claimed = store.claim_next_job("synthetic-worker", 1)
    assert claimed and claimed["id"] == job_id
    worker = claimed["worker_id"]
    store.transition(job_id, JobStatus.STARTING, expected_worker=worker)
    store.transition(job_id, JobStatus.RUNNING, expected_worker=worker)
    store.transition(
        job_id,
        JobStatus.FINISHED,
        expected_worker=worker,
        exit_code=0,
        pdf_path=str(output / "chapter.pdf"),
        manifest_path=str(output / "progress.json"),
        quality_report_path=str(output / "quality_report.json"),
    )
    (output / "job_manifest.json").write_text(json.dumps({
        "job_id": job_id,
        "run_id": current_run,
        "status": JobStatus.FINISHED,
        "exit_code": 0,
        "output_dir": str(output),
        "pdf_path": str(output / "chapter.pdf"),
    }), encoding="utf-8")
    return store, job_id, output, _sha(candidate)


def _service(tmp_path: Path, store: JobStore, *, renderer=None, pdf=None, quality=None):
    from selective_artifact_reconstruction import SelectiveArtifactReconstructor

    return SelectiveArtifactReconstructor(
        store,
        workspace_root=tmp_path,
        renderer=renderer or SpyRenderer(),
        pdf_builder=pdf or SpyPdfBuilder(),
        quality_builder=quality or SpyQualityBuilder(),
    )


def _request(job_id: str, candidate_hash: str, *, source_run_id: str = "source-run") -> dict:
    return {
        "source_job_id": job_id,
        "source_run_id": source_run_id,
        "page": 2,
        "region_id": "REGION_TEST_001",
        "candidate_sha256": candidate_hash,
        "reason": "synthetic false positive recovery",
    }


def test_service_imports_after_tdd_green(tmp_path):
    from selective_artifact_reconstruction import SelectiveArtifactReconstructor

    assert SelectiveArtifactReconstructor.__name__ == "SelectiveArtifactReconstructor"


def test_historical_artifact_is_never_overwritten_and_quality_precedes_promotion(tmp_path):
    store, job_id, output, candidate_hash = _fixture(tmp_path)
    renderer = SpyRenderer()
    pdf = SpyPdfBuilder()
    quality = SpyQualityBuilder()
    service = _service(tmp_path, store, renderer=renderer, pdf=pdf, quality=quality)
    old_pdf = output / "chapter.pdf"
    old_bytes = old_pdf.read_bytes()

    result = service.reconstruct(_request(job_id, candidate_hash))

    assert old_pdf.read_bytes() == old_bytes
    assert result["status"] == JobStatus.FINISHED
    assert result["reconstruction_status"] == "completed"
    assert result["artifact_sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert result["artifact_size_bytes"] == len(PDF_BYTES)
    assert quality.calls == 1
    assert Path(result["pdf_path"]).is_file()


def test_invalid_candidate_denied_before_render(tmp_path):
    store, job_id, _output, candidate_hash = _fixture(tmp_path, candidate="FOR REAL, EU VOU.")
    renderer = SpyRenderer()
    service = _service(tmp_path, store, renderer=renderer)

    with pytest.raises(Exception, match="reconstruction_candidate_invalid"):
        service.reconstruct(_request(job_id, candidate_hash))

    assert renderer.calls == []


def test_ambiguous_candidate_denied(tmp_path):
    store, job_id, _output, candidate_hash = _fixture(
        tmp_path, candidate="SE FOR PRECISO, EU VOU.", extra_candidate="SE NECESSÁRIO, EU VOU.")
    service = _service(tmp_path, store)

    with pytest.raises(Exception, match="reconstruction_candidate_ambiguous"):
        service.reconstruct(_request(job_id, candidate_hash))


@pytest.mark.parametrize("flag,reason", [
    ("missing_candidate", "reconstruction_candidate_missing"),
    ("missing_geometry", "reconstruction_geometry_missing"),
    ("missing_source_page", "reconstruction_source_page_missing"),
    ("missing_non_target", "reconstruction_page_order_incomplete"),
])
def test_missing_critical_evidence_fails_closed(tmp_path, flag, reason):
    store, job_id, _output, candidate_hash = _fixture(tmp_path, **{flag: True})
    service = _service(tmp_path, store)

    with pytest.raises(Exception, match=reason):
        service.reconstruct(_request(job_id, candidate_hash))


def test_non_target_pages_reused_in_order(tmp_path):
    store, job_id, _output, candidate_hash = _fixture(tmp_path)
    renderer = SpyRenderer()
    pdf = SpyPdfBuilder()
    result = _service(tmp_path, store, renderer=renderer, pdf=pdf).reconstruct(
        _request(job_id, candidate_hash))

    assert len(renderer.calls) == 1
    assert renderer.calls[0]["page_index"] == 2
    assert pdf.calls[0][0].endswith("page_001.png")
    assert ".tmp-" in pdf.calls[0][1]
    assert pdf.calls[0][1].endswith("page_002.png")
    assert result["page_path"].endswith("page_002.png")
    assert pdf.calls[0][2].endswith("page_003.png")


def test_pdf_failure_cleans_temp_and_does_not_promote(tmp_path):
    store, job_id, output, candidate_hash = _fixture(tmp_path)
    service = _service(tmp_path, store, pdf=SpyPdfBuilder(fail=True))

    with pytest.raises(Exception, match="reconstruction_pdf_failed"):
        service.reconstruct(_request(job_id, candidate_hash))

    recon_root = output / "reconstructions"
    assert not recon_root.exists() or not list(recon_root.glob("*/artifact.pdf"))


def test_renderer_font_or_fit_failure_cleans_temp_and_does_not_promote(tmp_path):
    store, job_id, output, candidate_hash = _fixture(tmp_path)
    service = _service(tmp_path, store, renderer=SpyRenderer(fail=True, reason="font unavailable"))

    with pytest.raises(Exception, match="reconstruction_render_failed"):
        service.reconstruct(_request(job_id, candidate_hash))

    recon_root = output / "reconstructions"
    assert not recon_root.exists() or not list(recon_root.glob("*/artifact.pdf"))


def test_quality_failure_or_mismatch_denies_promotion(tmp_path):
    store, job_id, output, candidate_hash = _fixture(tmp_path)
    service = _service(tmp_path, store, quality=SpyQualityBuilder(passed=False))

    with pytest.raises(Exception, match="reconstruction_quality_failed"):
        service.reconstruct(_request(job_id, candidate_hash))

    recon_root = output / "reconstructions"
    assert not recon_root.exists() or not list(recon_root.glob("*/artifact.pdf"))


def test_quality_hash_size_and_run_binding_are_required(tmp_path):
    store, job_id, _output, candidate_hash = _fixture(tmp_path)
    bad_quality = SpyQualityBuilder(run_id="source-run")
    service = _service(tmp_path, store, quality=bad_quality)

    with pytest.raises(Exception, match="reconstruction_quality_run_mismatch"):
        service.reconstruct(_request(job_id, candidate_hash))

    bad_hash = SpyQualityBuilder(sha256="0" * 64)
    with pytest.raises(Exception, match="reconstruction_quality_artifact_mismatch"):
        _service(tmp_path, store, quality=bad_hash).reconstruct(_request(job_id, candidate_hash))


def test_source_change_before_promotion_is_denied_without_artifact(tmp_path):
    store, job_id, output, candidate_hash = _fixture(tmp_path)
    stale_store = StaleOnSecondSourceReadStore(store, job_id)
    service = _service(tmp_path, stale_store)

    with pytest.raises(Exception, match="reconstruction_source_stale"):
        service.reconstruct(_request(job_id, candidate_hash))

    recon_root = output / "reconstructions"
    assert not recon_root.exists() or not list(recon_root.glob("*/artifact.pdf"))


def test_db_promotion_failure_removes_promoted_artifact(tmp_path):
    store, job_id, output, candidate_hash = _fixture(tmp_path)
    failing_store = FailPromotionUpdateStore(store)
    service = _service(tmp_path, failing_store)

    with pytest.raises(RuntimeError, match="db promotion boom"):
        service.reconstruct(_request(job_id, candidate_hash))

    recon_root = output / "reconstructions"
    assert not recon_root.exists() or not list(recon_root.glob("*/artifact.pdf"))


def test_stale_source_run_denied(tmp_path):
    store, job_id, _output, candidate_hash = _fixture(tmp_path, current_run="new-source-run")
    service = _service(tmp_path, store)

    with pytest.raises(Exception, match="reconstruction_source_stale"):
        service.reconstruct(_request(job_id, candidate_hash, source_run_id="source-run"))


def test_idempotent_retry_reuses_completed_artifact(tmp_path):
    store, job_id, _output, candidate_hash = _fixture(tmp_path)
    service = _service(tmp_path, store)

    first = service.reconstruct(_request(job_id, candidate_hash))
    second = service.reconstruct(_request(job_id, candidate_hash))

    assert first["reconstruction_id"] == second["reconstruction_id"]
    assert first["artifact_sha256"] == second["artifact_sha256"]


def test_publication_resolution_denies_historical_but_accepts_fresh_reconstruction(tmp_path):
    from community_api import ArtifactBindingError, CommunityApi
    from community_auth import RequestPrincipal

    store, job_id, output, candidate_hash = _fixture(tmp_path)
    result = _service(tmp_path, store).reconstruct(_request(job_id, candidate_hash))
    api = CommunityApi(store, community_db_path=tmp_path / "community.sqlite3", output_root=output)
    principal = RequestPrincipal("owner-a", True, auth_source="test")
    try:
        with pytest.raises(ArtifactBindingError, match="quality_gate_required"):
            api._resolve_translation_job(job_id, principal)
        resolved = api._resolve_translation_job(result["reconstruction_id"], principal)
    finally:
        api.close()

    assert resolved["source_job_id"] == result["reconstruction_id"]
    assert resolved["source_run_id"] == result["run_id"]
    assert resolved["pdf_sha256"] == result["artifact_sha256"]
