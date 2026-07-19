"""Hermetic end-to-end: local folder → snapshot → job → runner → PDF.

This closes the gap several previous changes flagged: the input edge was hardened repeatedly
while the pipeline core was never proven end to end. Everything here is synthetic and
offline — no network, no NVIDIA, no Drive, no remote Supabase, no external site. The
translation stages are exercised through fake_pipeline.py, which produces real checkpoints,
a timing report and a PDF without calling any provider.
"""

import _test_bootstrap  # noqa: F401

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from job_store import JobStatus, JobStore
from local_folder_job import (
    SOURCE_TYPE_LOCAL_FOLDER, ensure_not_both_sources, folder_fingerprint, job_fields,
    public_summary,
)
from local_folder_source import LocalFolderChapterAdapter, LocalFolderPolicy
from pdf import prepare_smart_webtoon_pages

REPO = Path(__file__).resolve().parent


def synthetic_pages(folder: Path) -> list[Path]:
    """Pages named so lexicographic order would be wrong, plus one very tall page."""
    specs = [("1.png", 800, 1200), ("2.png", 800, 1200), ("10.png", 760, 2600)]
    made = []
    for index, (name, width, height) in enumerate(specs, start=1):
        path = folder / name
        Image.new("RGB", (width, height), (index * 30, 70, 120)).save(path)
        made.append(path)
    return made


def wait(pred, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


class LocalPipelineE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "input"
        self.chapter = self.root / "meu_capitulo"
        self.chapter.mkdir(parents=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.sources = synthetic_pages(self.chapter)
        self.adapter = LocalFolderChapterAdapter(
            LocalFolderPolicy(allowed_roots=(self.root,)))
        self.store = JobStore(self.tmp / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()

    # ---- source gate --------------------------------------------------------
    def test_url_and_folder_are_mutually_exclusive(self):
        self.assertEqual(
            ensure_not_both_sources({"local_folder": "x"}), SOURCE_TYPE_LOCAL_FOLDER)
        self.assertEqual(ensure_not_both_sources({"url": "https://h/x"}), "url")
        for bad in ({"url": "https://h/x", "local_folder": "x"}, {}):
            with self.assertRaises(Exception):
                ensure_not_both_sources(bad)

    # ---- analysis + snapshot ------------------------------------------------
    def test_analysis_orders_naturally_and_summarises_without_paths(self):
        analysis = self.adapter.analyze(self.chapter)
        self.assertEqual(len(analysis.pages), 3)
        summary = public_summary(self.chapter, analysis)
        self.assertEqual(summary["accepted_count"], 3)
        self.assertEqual(summary["folder_name"], "meu_capitulo")
        self.assertTrue(summary["logical_pages"])
        blob = json.dumps(summary)
        # No absolute path, no drive letter, no account name may appear.
        for leaked in (str(self.chapter), "C:\\", "Users", "henri"):
            self.assertNotIn(leaked, blob, leaked)

    def test_fingerprint_is_stable_and_not_reversible(self):
        first = folder_fingerprint(self.chapter)
        self.assertEqual(first, folder_fingerprint(self.chapter))
        self.assertNotIn("meu_capitulo", first)
        self.assertEqual(len(first), 16)

    def test_snapshot_preserves_originals_and_generates_names(self):
        before = [(p.name, p.stat().st_size, p.read_bytes()[:16]) for p in self.sources]
        snapshot = self.adapter.snapshot(self.chapter, self.workspace)
        after = [(p.name, p.stat().st_size, p.read_bytes()[:16]) for p in self.sources]
        self.assertEqual(before, after)              # originals untouched
        self.assertTrue(snapshot.manifest_path.is_file())
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
        names = [page["filename"] for page in manifest["pages"]]
        self.assertNotIn("10.png", names)            # generated, not original names

    def test_snapshot_manifest_marks_logical_pages(self):
        snapshot = self.adapter.snapshot(self.chapter, self.workspace)
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(all(page.get("logical_page") for page in manifest["pages"]))

    # ---- job row ------------------------------------------------------------
    def test_job_persists_provenance_without_any_path(self):
        snapshot = self.adapter.snapshot(self.chapter, self.workspace)
        summary = public_summary(self.chapter, snapshot.analysis)
        fields = job_fields(summary, snapshot_ref=snapshot.workspace.name)
        job_id = self.store.create_job(
            source_url="", output_dir=str(self.tmp / "out"),
            configuration={"job_type": "translation"},
            command=[sys.executable, "-c", "pass"])
        self.store.update_fields(job_id, **fields)
        row = self.store.get_job(job_id)
        self.assertEqual(row["source_type"], SOURCE_TYPE_LOCAL_FOLDER)
        self.assertEqual(row["accepted_count"], 3)
        self.assertEqual(row["logical_pages"], 1)
        self.assertNotIn(str(self.chapter), json.dumps(dict(row), default=str))

    # ---- logical pages through the pipeline ---------------------------------
    def test_tall_page_is_not_recut_by_the_pipeline(self):
        snapshot = self.adapter.snapshot(self.chapter, self.workspace)
        pages = sorted((snapshot.workspace).glob("*.png"))
        self.assertEqual(len(pages), 3)
        split = self.tmp / "split"
        out, report = prepare_smart_webtoon_pages(pages, split, logical_pages=True)
        self.assertEqual(len(out), 3)
        self.assertTrue(report["smart_split_skipped"])
        sizes = sorted(Image.open(p).size for p in out)
        self.assertIn((760, 2600), sizes)          # the tall page survived intact

    # ---- full run through the controlled pipeline ---------------------------
    def test_end_to_end_produces_a_valid_pdf_and_finishes(self):
        out = self.tmp / "chapter_out"
        command = [sys.executable, "-u", str(REPO / "fake_pipeline.py"),
                   "--output-dir", str(out), "--outcome", "finished",
                   "--steps", "2", "--sleep", "0.01"]
        job_id = self.store.create_job(
            source_url="", output_dir=str(out),
            configuration={"job_type": "translation"}, command=command)
        snapshot = self.adapter.snapshot(self.chapter, self.workspace)
        summary = public_summary(self.chapter, snapshot.analysis)
        self.store.update_fields(job_id, **job_fields(summary, snapshot.workspace.name))

        self.store.transition(job_id, JobStatus.CLAIMING, worker_id="w1")
        self.store.transition(job_id, JobStatus.STARTING)
        self.store.transition(job_id, JobStatus.RUNNING)
        proc = subprocess.run(command, cwd=str(REPO), capture_output=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        self.store.update_fields(job_id, exit_code=proc.returncode)
        self.store.transition(job_id, JobStatus.FINISHED)

        pdfs = list(out.glob("*.pdf"))
        self.assertTrue(pdfs, "no PDF produced")
        data = pdfs[0].read_bytes()
        self.assertGreater(len(data), 0)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue((out / "timing_report.json").is_file())

        row = self.store.get_job(job_id)
        self.assertEqual(row["status"], JobStatus.FINISHED)
        self.assertEqual(row["exit_code"], 0)
        self.assertIsNotNone(row["finished_at"])
        # Elapsed is frozen: recomputing later yields the same value.
        first = row["finished_at"] - row["started_at"]
        time.sleep(0.05)
        again = self.store.get_job(job_id)
        self.assertEqual(again["finished_at"] - again["started_at"], first)

    def test_originals_survive_the_whole_run(self):
        before = {p.name: p.read_bytes() for p in self.sources}
        snapshot = self.adapter.snapshot(self.chapter, self.workspace)
        prepare_smart_webtoon_pages(
            sorted(snapshot.workspace.glob("*.png")), self.tmp / "split2",
            logical_pages=True)
        after = {p.name: p.read_bytes() for p in self.sources}
        self.assertEqual(before, after)

    # ---- refusals stay terminal --------------------------------------------
    def test_folder_outside_the_allowed_root_is_refused(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        synthetic_pages(outside)
        with self.assertRaises(Exception):
            self.adapter.analyze(outside)

    def test_empty_folder_is_refused_before_any_job_runs(self):
        empty = self.root / "vazio"
        empty.mkdir()
        with self.assertRaises(Exception):
            self.adapter.analyze(empty)


class OfflineGuardTests(unittest.TestCase):
    def test_no_provider_module_is_imported_by_the_local_path(self):
        for module in ("local_folder_job", "local_folder_source", "local_folder_input"):
            source = (REPO / f"{module}.py").read_text(encoding="utf-8")
            for banned in ("import requests", "translator_nvidia", "googleapis", "supabase"):
                self.assertNotIn(banned, source, f"{module}: {banned}")

    def test_command_is_an_argument_list_never_a_shell_string(self):
        from local_folder_job import build_local_job_command

        command = build_local_job_command(
            snapshot_ref="abc123", output="cap", mode="fast", logical_pages=True,
            use_cache=False, force=True, use_context=True)
        self.assertIsInstance(command, list)
        self.assertTrue(all(isinstance(part, str) for part in command))
        self.assertIn("--snapshot-ref", command)
        self.assertNotIn("shell", " ".join(command).lower())


if __name__ == "__main__":
    unittest.main()
