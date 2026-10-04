"""Canonical user-facing output naming.

User artifacts are named by the LOGICAL PAGE INDEX (zero-padded), never by a worker/VPS
technical name, job id, or accidental download order. This holds for local and remote clean
results alike because the pipeline persists by page index, not by source/worker filename.
Collisions are impossible: two identically-named sources map to distinct page indices.
"""
from yomu.runtime.artifact_contract import ArtifactType, artifact_relative_path


def test_clean_image_named_by_logical_page_index():
    assert artifact_relative_path(ArtifactType.CLEAN_IMAGE, page=1, total_pages=20) == "images/clean/001.png"
    assert artifact_relative_path(ArtifactType.CLEAN_IMAGE, page=17, total_pages=20) == "images/clean/017.png"


def test_translated_image_named_by_logical_page_index():
    assert artifact_relative_path(ArtifactType.TRANSLATED_IMAGE, page=3, total_pages=9) == "images/translated/003.png"


def test_width_grows_for_large_chapters_so_order_is_stable():
    assert artifact_relative_path(ArtifactType.CLEAN_IMAGE, page=5, total_pages=1200) == "images/clean/0005.png"
    assert artifact_relative_path(ArtifactType.CLEAN_IMAGE, page=1000, total_pages=1200) == "images/clean/1000.png"


def test_name_is_independent_of_source_filename_and_worker_id():
    # Same page index -> same canonical name regardless of what the source/worker was called;
    # the user never receives a job-id/blob/gemini_raw name as the canonical artifact.
    for page in (1, 2, 42):
        name = artifact_relative_path(ArtifactType.CLEAN_IMAGE, page=page, total_pages=100)
        assert name.startswith("images/clean/") and name.endswith(".png")
        assert "job" not in name and "blob" not in name and "gemini" not in name


def test_distinct_pages_never_collide():
    seen = {artifact_relative_path(ArtifactType.CLEAN_IMAGE, page=p, total_pages=50) for p in range(1, 51)}
    assert len(seen) == 50  # one unique path per logical page -> no overwrite
