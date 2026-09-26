"""Contract for the in-app chapter reader: run binding, path safety, read-only.

The reader is the default way to read a finished chapter, so the artifact it shows
must be the one belonging to the History card that was clicked -- not the newest run
of the same chapter, and not any other file on the machine. Everything here is
hermetic: PDFs are generated from Pillow at test time, nothing is downloaded, and no
provider, network or remote store is touched.
"""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import app_ui
import pdf_reader
from community_auth import RequestPrincipal
from job_store import JobStatus, JobStore
from ui_bridge import UiBridge


def _write_pdf(path: Path, sizes, colour=(40, 40, 40)) -> Path:
    """A deterministic image-only PDF, exactly the shape the pipeline produces."""

    pages = [Image.new("RGB", size, colour) for size in sizes]
    pages[0].save(path, "PDF", save_all=True, append_images=pages[1:])
    for page in pages:
        page.close()
    return path


class Harness:
    """A bridge with a real job store and a confined output root."""

    def __init__(self, tmp_path: Path):
        self.output = tmp_path / "output"
        self.output.mkdir()
        self.store = JobStore(tmp_path / "jobs.sqlite3")
        self.bridge = UiBridge.__new__(UiBridge)
        self.bridge.store = self.store
        self.bridge.output_root = self.output
        self.bridge._job_record = lambda job: {
            "job_id": job["id"],
            "run_id": job["run_id"],
            "pdf_path": job["configuration"].get("pdf_path", ""),
            "chapter_name": job["configuration"].get("chapter_name", ""),
            "slug": job["configuration"].get("slug", ""),
            "status": job["configuration"].get("status", "finished"),
            "review_status": job["configuration"].get("review_status", ""),
            "quality_gate": job["configuration"].get("quality_gate", True),
        }

    def add_job(self, job_id: str, *, owner="owner-a", pdf_path="", **fields) -> str:
        configuration = {
            "job_type": "translation",
            "community_owner_id": owner,
            "ownership_schema_version": 1,
            "pdf_path": str(pdf_path),
        }
        configuration.update(fields)
        return self.store.create_job(
            job_id=job_id,
            source_url="",
            output_dir=str(self.output),
            command=["offline-fixture"],
            configuration=configuration,
            initial_status=JobStatus.QUEUED,
        )

    def close(self):
        self.store.close()


@pytest.fixture()
def harness(tmp_path):
    built = Harness(tmp_path)
    try:
        yield built
    finally:
        built.close()


# ---------------------------------------------------------------- parsing ----

def test_pages_are_extracted_with_their_own_dimensions(tmp_path):
    pdf = _write_pdf(tmp_path / "mixed.pdf", [(800, 1800), (1200, 700), (600, 2400)])
    document = pdf_reader.parse_document(pdf)
    assert document.page_count == 3
    assert [(page.width, page.height) for page in document.pages] == [
        (800, 1800), (1200, 700), (600, 2400)]
    for number in (1, 2, 3):
        payload = document.page_bytes(number)
        assert payload[:2] == b"\xff\xd8"
        with Image.open(io.BytesIO(payload)) as image:
            assert image.size == (document.page(number).width,
                                  document.page(number).height)


def test_a_long_chapter_parses_without_decoding_any_page(tmp_path):
    pdf = _write_pdf(tmp_path / "long.pdf", [(300, 400)] * 84)
    document = pdf_reader.parse_document(pdf)
    assert document.page_count == 84
    # Parsing only walks the page tree: page 84 is reachable without touching 1..83.
    assert document.page_bytes(84)[:2] == b"\xff\xd8"


@pytest.mark.parametrize("number", [0, -1, 85, "2", 1.0, True])
def test_page_bounds_never_crash_and_never_wrap(tmp_path, number):
    pdf = _write_pdf(tmp_path / "bounds.pdf", [(300, 400)] * 3)
    document = pdf_reader.parse_document(pdf)
    with pytest.raises(IndexError):
        document.page(number if not isinstance(number, bool) else number)


def test_a_pdf_that_is_not_page_images_is_refused_rather_than_guessed(tmp_path):
    pdf = tmp_path / "text.pdf"
    pdf.write_bytes(b"%PDF-1.4\nnot really a document\n%%EOF\n")
    with pytest.raises(pdf_reader.UnsupportedPdf):
        pdf_reader.parse_document(pdf)


def test_thumbnails_are_small_previews_of_the_real_page(tmp_path):
    pdf = _write_pdf(tmp_path / "thumb.pdf", [(1600, 2400)])
    thumbnail = pdf_reader.page_thumbnail(pdf, 1)
    with Image.open(io.BytesIO(thumbnail)) as image:
        assert image.width == pdf_reader.THUMBNAIL_WIDTH
    assert len(thumbnail) < len(pdf_reader.parse_document(pdf).page_bytes(1))


# ------------------------------------------------------------ run binding ----

def test_each_run_of_the_same_chapter_opens_its_own_pdf(harness):
    chapter = harness.output / "shadow-slave-1"
    (chapter / "run-a").mkdir(parents=True)
    (chapter / "run-b").mkdir(parents=True)
    first = _write_pdf(chapter / "run-a" / "chapter.pdf", [(300, 400)], colour=(10, 10, 10))
    second = _write_pdf(chapter / "run-b" / "chapter.pdf", [(300, 400)] * 2, colour=(240, 240, 240))
    harness.add_job("e1" * 16, pdf_path=first, chapter_name="Shadow Slave 1")
    harness.add_job("b" * 32, pdf_path=second, chapter_name="Shadow Slave 1")

    document_a = harness.bridge.reader_document_for_owner("owner-a", "e1" * 16)
    document_b = harness.bridge.reader_document_for_owner("owner-a", "b" * 32)
    assert (document_a["page_count"], document_b["page_count"]) == (1, 2)
    assert harness.bridge.reader_page_for_owner("owner-a", "e1" * 16, 1) != \
        harness.bridge.reader_page_for_owner("owner-a", "b" * 32, 1)


def test_a_review_required_run_is_still_readable(harness):
    folder = harness.output / "review"
    folder.mkdir()
    pdf = _write_pdf(folder / "chapter.pdf", [(300, 400)] * 2)
    harness.add_job("c" * 32, pdf_path=pdf, status="review_required", quality_gate=False)
    document = harness.bridge.reader_document_for_owner("owner-a", "c" * 32)
    assert document["page_count"] == 2
    assert document["review_required"] is True
    assert harness.bridge.reader_page_for_owner("owner-a", "c" * 32, 2)[:2] == b"\xff\xd8"


def test_a_run_without_a_pdf_reports_unavailable_instead_of_a_blank_viewer(harness):
    harness.add_job("d" * 32, pdf_path="")
    with pytest.raises(ValueError, match="artifact_unavailable"):
        harness.bridge.reader_document_for_owner("owner-a", "d" * 32)


def test_a_recorded_pdf_that_no_longer_exists_reports_unavailable(harness):
    folder = harness.output / "gone"
    folder.mkdir()
    harness.add_job("e" * 32, pdf_path=folder / "chapter.pdf")
    with pytest.raises(ValueError, match="artifact_unavailable"):
        harness.bridge.reader_pdf_for_owner("owner-a", "e" * 32)


def test_a_pdf_this_parser_cannot_read_still_opens_through_the_browser(harness):
    folder = harness.output / "opaque"
    folder.mkdir()
    pdf = folder / "chapter.pdf"
    pdf.write_bytes(b"%PDF-1.4\nopaque but real\n%%EOF\n")
    harness.add_job("f" * 32, pdf_path=pdf)
    document = harness.bridge.reader_document_for_owner("owner-a", "f" * 32)
    assert document["mode"] == "embed"
    assert document["page_count"] == 0


# ---------------------------------------------------------------- security ----

def test_another_owner_can_never_reach_the_artifact(harness):
    folder = harness.output / "private"
    folder.mkdir()
    pdf = _write_pdf(folder / "chapter.pdf", [(300, 400)])
    harness.add_job("1" * 32, owner="owner-a", pdf_path=pdf)
    with pytest.raises(ValueError, match="artifact_not_found"):
        harness.bridge.reader_pdf_for_owner("owner-b", "1" * 32)


@pytest.mark.parametrize("job_id", [
    "", "unknown", "../../.env", r"..\..\Windows\win.ini", "%2e%2e%2fsecret",
    r"C:\Users\someone\secret.pdf", "e1" * 16,
])
def test_no_client_string_resolves_to_a_file(harness, job_id):
    with pytest.raises(ValueError, match="artifact_not_found"):
        harness.bridge.reader_pdf_for_owner("owner-a", job_id)


def test_a_recorded_path_outside_the_output_root_is_refused(harness, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    pdf = _write_pdf(outside / "chapter.pdf", [(300, 400)])
    harness.add_job("2" * 32, pdf_path=pdf)
    with pytest.raises(ValueError, match="artifact_not_found"):
        harness.bridge.reader_pdf_for_owner("owner-a", "2" * 32)


def test_a_traversal_inside_the_recorded_path_is_refused(harness, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    pdf = _write_pdf(outside / "secret.pdf", [(300, 400)])
    harness.add_job("3" * 32, pdf_path=harness.output / ".." / "elsewhere" / "secret.pdf")
    assert pdf.is_file()
    with pytest.raises(ValueError, match="artifact_not_found"):
        harness.bridge.reader_pdf_for_owner("owner-a", "3" * 32)


@pytest.mark.parametrize("name,payload", [
    ("run_manifest.json", b'{"run_id": "x"}'),
    ("quality_report.html", b"<html>report</html>"),
    ("chapter.pdf", b"<html>not a pdf at all</html>"),
])
def test_only_a_real_pdf_can_leave_the_reader_endpoint(harness, name, payload):
    folder = harness.output / "artifacts"
    folder.mkdir()
    artifact = folder / name
    artifact.write_bytes(payload)
    harness.add_job("4" * 32, pdf_path=artifact)
    with pytest.raises(ValueError, match="artifact_unavailable"):
        harness.bridge.reader_pdf_for_owner("owner-a", "4" * 32)


def test_the_reader_never_reveals_a_filesystem_path(harness):
    folder = harness.output / "quiet"
    folder.mkdir()
    pdf = _write_pdf(folder / "chapter.pdf", [(300, 400)])
    harness.add_job("5" * 32, pdf_path=pdf, chapter_name="CapÃ­tulo")
    document = harness.bridge.reader_document_for_owner("owner-a", "5" * 32)
    serialised = repr(document)
    assert str(folder) not in serialised
    assert "pdf_path" not in document
    assert document["title"] == "CapÃ­tulo"


# ------------------------------------------------------------- read only ----

def test_reading_a_chapter_never_touches_the_artifact(harness):
    folder = harness.output / "immutable"
    folder.mkdir()
    pdf = _write_pdf(folder / "chapter.pdf", [(400, 600)] * 5)
    harness.add_job("6" * 32, pdf_path=pdf)
    before = hashlib.sha256(pdf.read_bytes()).hexdigest()
    before_mtime = pdf.stat().st_mtime_ns

    harness.bridge.reader_document_for_owner("owner-a", "6" * 32)
    for page in range(1, 6):
        harness.bridge.reader_page_for_owner("owner-a", "6" * 32, page)
        harness.bridge.reader_thumbnail_for_owner("owner-a", "6" * 32, page)

    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before
    assert pdf.stat().st_mtime_ns == before_mtime
    assert sorted(item.name for item in folder.iterdir()) == ["chapter.pdf"]


# ------------------------------------------------------------- endpoints ----

def test_every_reader_endpoint_is_owner_scoped():
    import inspect

    for endpoint in (app_ui.api_reader_document, app_ui.api_reader_page,
                     app_ui.api_reader_thumbnail, app_ui.api_reader_pdf):
        source = inspect.getsource(endpoint)
        assert "_owned_ui_job(request, job_id)" in source, endpoint.__name__
        assert "request" in inspect.signature(endpoint).parameters


def test_endpoints_translate_missing_artifacts_into_a_usable_error(harness, monkeypatch):
    principal = RequestPrincipal(
        user_id="owner-a", authenticated=True, roles=frozenset({"user"}),
        auth_source="local_test", session_id="session-a")
    monkeypatch.setattr(app_ui, "_ui_principal", lambda request, mutate=False: principal)
    monkeypatch.setattr(app_ui, "BRIDGE", harness.bridge)

    folder = harness.output / "endpoint"
    folder.mkdir()
    pdf = _write_pdf(folder / "chapter.pdf", [(300, 400)] * 2)
    harness.add_job("7" * 32, pdf_path=pdf)
    harness.add_job("8" * 32, pdf_path="")

    document = app_ui.api_reader_document(SimpleNamespace(), "7" * 32)
    assert document["page_count"] == 2
    page = app_ui.api_reader_page(SimpleNamespace(), "7" * 32, 1)
    assert page.media_type == "image/jpeg"
    assert page.body[:2] == b"\xff\xd8"
    thumb = app_ui.api_reader_thumbnail(SimpleNamespace(), "7" * 32, 2)
    assert thumb.media_type == "image/jpeg"

    with pytest.raises(app_ui.HTTPException) as missing:
        app_ui.api_reader_document(SimpleNamespace(), "8" * 32)
    assert missing.value.status_code == 404
    assert missing.value.detail["code"] == "artifact_unavailable"

    with pytest.raises(app_ui.HTTPException) as unknown:
        app_ui.api_reader_document(SimpleNamespace(), "9" * 32)
    assert unknown.value.status_code == 404

    with pytest.raises(app_ui.HTTPException) as out_of_range:
        app_ui.api_reader_page(SimpleNamespace(), "7" * 32, 99)
    assert out_of_range.value.detail["code"] == "page_not_found"


def test_the_pdf_fallback_is_served_as_a_pdf_with_range_support(harness, monkeypatch):
    principal = RequestPrincipal(
        user_id="owner-a", authenticated=True, roles=frozenset({"user"}),
        auth_source="local_test", session_id="session-a")
    monkeypatch.setattr(app_ui, "_ui_principal", lambda request, mutate=False: principal)
    monkeypatch.setattr(app_ui, "BRIDGE", harness.bridge)
    folder = harness.output / "fallback"
    folder.mkdir()
    pdf = _write_pdf(folder / "chapter.pdf", [(300, 400)])
    harness.add_job("e1" * 16, pdf_path=pdf)

    response = app_ui.api_reader_pdf(SimpleNamespace(), "e1" * 16)
    assert response.media_type == "application/pdf"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Starlette's FileResponse is the range-capable path; a raw byte body is not.
    assert type(response).__name__ == "FileResponse"


# ------------------------------------------------------- ui integration ----

READER_JS = Path(app_ui.ROOT) / "static" / "chapter_reader.js"
UI_JS = Path(app_ui.ROOT) / "static" / "tradutor_ui.js"
SHELL = Path(app_ui.ROOT) / "ui" / "ui_shell.html"


def test_history_reads_inside_the_app_and_keeps_the_external_viewer_secondary():
    source = UI_JS.read_text(encoding="utf-8")
    assert "data-action=\"read\"" in source
    assert "tradutor-open-reader" in source
    # The external viewer survives, but no longer as the primary read action. It is now the
    # format-aware "Abrir PDF" action (offered only for a PDF job), kept after the in-app read.
    assert "actionButton('Abrir PDF', 'pdf', record.pdf_path)" in source
    read_index = source.index("readAction(record)")
    external_index = source.index("actionButton('Abrir PDF', 'pdf', record.pdf_path)")
    assert read_index < external_index


def test_the_reader_lives_inside_the_application_shell():
    shell = SHELL.read_text(encoding="utf-8")
    assert 'data-tab="leitor"' in shell
    assert 'class="panel-view" id="view-leitor"' in shell
    # A tab of the shell, never a second window or a detached page.
    reader = READER_JS.read_text(encoding="utf-8")
    assert "window.open" not in reader
    assert "http://" not in reader and "https://" not in reader


def test_the_shell_loads_the_reader_module():
    source = (Path(app_ui.ROOT) / "app_ui.py").read_text(encoding="utf-8")
    assert "CHAPTER_READER_ASSET" in source
    assert "_asset_url(CHAPTER_READER_ASSET)" in source


def test_reader_state_contract_runs_in_node():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(Path(app_ui.ROOT) / "test_chapter_reader.mjs")],
        cwd=str(app_ui.ROOT), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_thumbnails_are_loaded_lazily_and_pages_are_not_prerendered():
    reader = READER_JS.read_text(encoding="utf-8")
    assert "IntersectionObserver" in reader
    assert "dataset.src" in reader
    assert "loading = 'lazy'" in reader.replace("'lazy'", "'lazy'")

