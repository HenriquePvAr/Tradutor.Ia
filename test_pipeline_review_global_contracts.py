"""Contracts for canonical pipeline progress and safe review bulk actions."""

from __future__ import annotations

import _test_bootstrap  # noqa: F401

import json
import sys
import tempfile
import unittest
from pathlib import Path

from chapter_quality_revision import ChapterQualityRevision, ContextualNvidiaReviewer, REVIEW_SCHEMA_VERSION
from job_store import JobStatus, JobStore


ROOT = Path(__file__).resolve().parent
JS = ROOT / "static" / "tradutor_ui.js"
SHELL = ROOT / "ui" / "ui_shell.html"


class PipelineCanonicalUiContracts(unittest.TestCase):
    def test_pipeline_uses_canonical_stage_aliases_for_new_backend_stages(self) -> None:
        source = JS.read_text(encoding="utf-8")
        for marker in (
            "const stageAliases = {",
            "reading_text: 'ocr'",
            "translating: 'translate'",
            "redrawing: 'render'",
            "generating_pdf: 'pdf'",
            "quality_review: 'quality_review'",
            "review_required: 'quality_review'",
        ):
            self.assertIn(marker, source)

    def test_preview_is_rendered_from_canonical_pipeline_state(self) -> None:
        source = JS.read_text(encoding="utf-8")
        render_progress = source[
            source.index("function renderProgress("):source.index("function shouldRenderSourceReview")
        ]
        self.assertIn("renderPipelinePreview(state);", render_progress)
        self.assertNotIn("aguardando início", render_progress)
        self.assertIn("buildPipelineState(runtime, visibleProgress)", source)
        self.assertIn("appState.currentPipelineState = pipelineState", source)

    def test_stage_list_contains_quality_review_step_and_explicit_states(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn('data-stage="quality_review"', shell)
        self.assertIn("item.dataset.state =", source)
        self.assertIn("'completed'", source)
        self.assertIn("'active'", source)
        self.assertIn("'future'", source)


class QualityReviewBulkContracts(unittest.TestCase):
    def test_frontend_exposes_safe_bulk_controls(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        for marker in (
            "qualityReviewSelectAll",
            "acceptLowRiskReview",
            "acceptAllReview",
            "undoBulkReview",
            "globalAiReview",
            "/api/ui/quality-review/bulk-action",
            "Aceitar baixo risco",
            "Esta seleção inclui itens de alto risco",
        ):
            self.assertIn(marker, shell + source)

    def test_job_store_bulk_review_actions_are_atomic_and_restore_previous_values(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3")
            try:
                job_id = store.create_job(
                    source_url="https://example.test/chapter",
                    output_dir=str(Path(folder) / "output"),
                    command=["python"],
                    configuration={"job_type": "translation"},
                )
                store.transition(job_id, JobStatus.CLAIMING)
                store.transition(job_id, JobStatus.STARTING)
                store.transition(job_id, JobStatus.RUNNING)
                store.transition(job_id, JobStatus.REVIEW_REQUIRED)
                store.record_review_actions_bulk(job_id, {"p1:i1": "reviewed", "p1:i2": "preserved_original"})
                self.assertEqual(
                    store.review_actions(job_id),
                    {"p1:i1": "reviewed", "p1:i2": "preserved_original"},
                )
                store.record_review_actions_bulk(job_id, {"p1:i1": "pending", "p1:i2": "reviewed"})
                self.assertEqual(store.review_actions(job_id), {"p1:i2": "reviewed"})
            finally:
                store.close()

    def test_no_chapter_specific_translation_hardcode_in_runtime(self) -> None:
        runtime = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (ROOT / "static").glob("*.js")
        ) + "\n" + (ROOT / "ui_bridge.py").read_text(encoding="utf-8", errors="ignore")
        runtime += "\n" + (ROOT / "chapter_quality_revision.py").read_text(encoding="utf-8", errors="ignore")
        forbidden = (
            "REAL COFFEE",
            "PRECINCT",
            "CODE BLACK",
            "I AM",
            "THE INFECTED",
            "THE NIGHTMARE SPELL",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, runtime)
        self.assertNotIn("shadow_slave", runtime.lower())
        self.assertNotIn('if text ==', runtime)
        self.assertNotIn('if source_text ==', runtime)


class FakeContextualReviewer:
    model = "fake-contextual-reviewer"

    def __init__(self) -> None:
        self.requests = 0

    def review_batch(self, records, glossary):
        self.requests += 1
        result = []
        for record in reversed(records):
            result.append({
                "region_id": record["region_id"],
                "action": "manual_review" if record["region_id"].endswith("REGION_002") else "keep",
                "revised_translation": record["current_translation"],
                "reason_code": "fake_review",
                "confidence": 0.99,
                "risk": "high" if record["region_id"].endswith("REGION_002") else "low",
                "terminology": [],
            })
        return result


class FakeRewriteReviewer:
    model = "fake-rewrite-reviewer"

    def __init__(self) -> None:
        self.requests = 0

    def review_batch(self, records, glossary, **_kwargs):
        self.requests += 1
        result = []
        for record in records:
            result.append({
                "region_id": record["region_id"],
                "action": "rewrite" if record["region_id"].endswith("REGION_001") else "manual_review",
                "revised_translation": "Olá de verdade.",
                "reason_code": "safe_test_rewrite" if record["region_id"].endswith("REGION_001") else "needs_review",
                "confidence": 0.99,
                "risk": "low" if record["region_id"].endswith("REGION_001") else "high",
                "terminology": [],
            })
        return result


class ReviewContractParserContracts(unittest.TestCase):
    def _records(self):
        return [
            {"region_id": "p001:REGION_001", "source_text": "HELLO", "current_translation": "Olá"},
            {"region_id": "p001:REGION_002", "source_text": "WAIT", "current_translation": "Espere"},
        ]

    def _envelope(self, results=None, batch_id="batch-1"):
        if results is None:
            results = [
                {
                    "region_id": "p001:REGION_001",
                    "action": "keep",
                    "revised_translation": "",
                    "reason_code": "already_good",
                    "confidence": 0.98,
                    "risk": "low",
                    "terminology": [],
                },
                {
                    "region_id": "p001:REGION_002",
                    "action": "rewrite",
                    "revised_translation": "Espere um pouco.",
                    "reason_code": "natural_ptbr",
                    "confidence": 0.99,
                    "risk": "low",
                    "terminology": [],
                },
            ]
        return {"schema_version": REVIEW_SCHEMA_VERSION, "batch_id": batch_id, "results": results}

    def _parse(self, payload, batch_id="batch-1"):
        reviewer = ContextualNvidiaReviewer()
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return reviewer._parse_contract_response(text, self._records(), batch_id)

    def test_structured_contract_accepts_valid_json_and_out_of_order_ids(self) -> None:
        envelope = self._envelope(results=list(reversed(self._envelope()["results"])))
        parsed = self._parse(envelope)
        self.assertTrue(parsed.valid)
        self.assertEqual({item["region_id"] for item in parsed.items}, {"p001:REGION_001", "p001:REGION_002"})

    def test_structured_contract_tolerates_single_markdown_fence_and_prose(self) -> None:
        parsed = self._parse("Observação.\n```json\n" + json.dumps(self._envelope(), ensure_ascii=False) + "\n```\nFim.")
        self.assertTrue(parsed.valid)
        self.assertIn("prose_before_json", parsed.categories)
        self.assertIn("prose_after_json", parsed.categories)

    def test_structured_contract_rejects_truncated_json(self) -> None:
        parsed = self._parse('{"schema_version": "1.0", "batch_id": "batch-1", "results": [')
        self.assertFalse(parsed.valid)
        self.assertIn("truncated_json", parsed.categories)

    def test_structured_contract_rejects_wrong_root(self) -> None:
        parsed = self._parse([])
        self.assertFalse(parsed.valid)
        self.assertIn("wrong_root_type", parsed.categories)

    def test_structured_contract_requires_exact_region_id_set(self) -> None:
        results = self._envelope()["results"][:1]
        parsed = self._parse(self._envelope(results=results))
        self.assertFalse(parsed.valid)
        self.assertIn("missing_regions", parsed.categories)

    def test_structured_contract_rejects_extra_duplicate_and_unknown_ids(self) -> None:
        results = self._envelope()["results"]
        results = results + [{**results[0]}, {**results[0], "region_id": "p999:REGION_X"}]
        parsed = self._parse(self._envelope(results=results))
        self.assertFalse(parsed.valid)
        self.assertIn("duplicate_region_id", parsed.categories)
        self.assertIn("unknown_region_id", parsed.categories)
        self.assertIn("extra_regions", parsed.categories)

    def test_structured_contract_rejects_invalid_fields(self) -> None:
        bad = self._envelope()["results"]
        bad[0]["action"] = "accept"
        bad[0]["confidence"] = 1.2
        bad[0]["risk"] = "certain"
        bad[1]["revised_translation"] = ""
        parsed = self._parse(self._envelope(results=bad))
        self.assertFalse(parsed.valid)
        self.assertIn("invalid_action", parsed.categories)
        self.assertIn("invalid_confidence", parsed.categories)
        self.assertIn("invalid_risk", parsed.categories)
        self.assertIn("empty_translation", parsed.categories)

    def test_structured_contract_never_maps_by_position(self) -> None:
        results = [
            {
                "region_id": "unknown",
                "action": "rewrite",
                "revised_translation": "Texto posicional.",
                "reason_code": "bad_id",
                "confidence": 0.99,
                "risk": "low",
                "terminology": [],
            }
        ]
        parsed = self._parse(self._envelope(results=results))
        self.assertFalse(parsed.valid)
        self.assertEqual(parsed.items, [])
        self.assertIn("unknown_region_id", parsed.categories)


class FakeProviderReviewer(ContextualNvidiaReviewer):
    def __init__(self, responses):
        super().__init__()
        self.api_key = "configured-for-test"
        self.responses = list(responses)
        self.sent = []

    def _post_chat_completion(self, request_payload, started, purpose, **kwargs):
        self.requests += 1
        self.sent.append(purpose)
        content = self.responses.pop(0)
        return {
            "purpose": purpose,
            "status_http": 200,
            "duration_seconds": 0.01,
            "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            "raw_body": "",
            "finish_reason": "stop",
            "provider_error": None,
        }


class FakeTransportFailureReviewer(FakeProviderReviewer):
    def _post_chat_completion(self, request_payload, started, purpose, **kwargs):
        self.requests += 1
        self.sent.append(purpose)
        return {
            "purpose": purpose,
            "status_http": None,
            "duration_seconds": 0.01,
            "content": "",
            "raw_body": "",
            "finish_reason": None,
            "provider_error": "nvidia_review_request_failed",
            "provider_error_detail": "synthetic transport failure",
        }


class ReviewContractRecoveryContracts(unittest.TestCase):
    def _records(self):
        return [
            {"region_id": "p001:REGION_001", "source_text": "HELLO", "current_translation": "Olá"},
            {"region_id": "p001:REGION_002", "source_text": "WAIT", "current_translation": "Espere"},
        ]

    def _envelope(self, ids=None, batch_id="batch-1"):
        ids = ids or ["p001:REGION_001", "p001:REGION_002"]
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": batch_id,
            "results": [
                {
                    "region_id": rid,
                    "action": "keep",
                    "revised_translation": "",
                    "reason_code": "already_good",
                    "confidence": 0.97,
                    "risk": "low",
                    "terminology": [],
                }
                for rid in ids
            ],
        }

    def test_repair_is_attempted_once_for_invalid_batch(self) -> None:
        reviewer = FakeProviderReviewer(["not json", self._envelope()])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review", "repair"])
        self.assertEqual(reviewer.repaired_batches, 1)
        self.assertEqual({item["region_id"] for item in reviews}, {"p001:REGION_001", "p001:REGION_002"})

    def test_batch_falls_back_to_individual_regions_after_failed_repair(self) -> None:
        reviewer = FakeProviderReviewer([
            "not json",
            "still not json",
            self._envelope(ids=["p001:REGION_001"], batch_id="batch-1-region-01"),
            self._envelope(ids=["p001:REGION_002"], batch_id="batch-1-region-02"),
        ])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review", "repair", "individual_fallback", "individual_fallback"])
        self.assertEqual(reviewer.fallback_individual, 2)
        self.assertTrue(all(item["contract_path"] == "individual_fallback" for item in reviews))

    def test_failed_contract_returns_manual_review_without_creating_translation(self) -> None:
        reviewer = FakeProviderReviewer(["not json", "still not json"])
        reviews = reviewer.review_batch([self._records()[0]], {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review", "repair"])
        self.assertEqual(reviews[0]["action"], "manual_review")
        self.assertEqual(reviews[0]["revised_translation"], "")
        self.assertIn("invalid_json", reviews[0]["contract_categories"])

    def test_transport_failure_does_not_attempt_repair_or_region_fallback(self) -> None:
        reviewer = FakeTransportFailureReviewer([])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1")
        self.assertEqual(reviewer.sent, ["review"])
        self.assertEqual(reviewer.requests, 1)
        self.assertTrue(all(item["action"] == "manual_review" for item in reviews))
        self.assertTrue(all(item["reason_code"] == "nvidia_review_request_failed" for item in reviews))

    def test_provider_timeout_is_classified_without_json_parse_noise(self) -> None:
        self.assertEqual(ContextualNvidiaReviewer._provider_error_categories("nvidia_review_timeout"), ["timeout"])

    def test_subprocess_uses_current_environment_python_not_base_python(self) -> None:
        executable = ContextualNvidiaReviewer._subprocess_python_executable()
        self.assertEqual(Path(executable), Path(sys.executable))

    def test_diagnostic_mode_disables_repair_and_individual_fallback(self) -> None:
        reviewer = FakeProviderReviewer(["not json"])
        reviews = reviewer.review_batch(self._records(), {}, batch_id="batch-1", diagnostic_mode=True)
        self.assertEqual(reviewer.sent, ["review"])
        self.assertEqual(reviewer.requests, 1)
        self.assertEqual(reviewer.invalid_batches, 1)
        self.assertTrue(all(item["action"] == "manual_review" for item in reviews))

    def test_timeout_config_keeps_subprocess_above_http_read_timeout(self) -> None:
        reviewer = ContextualNvidiaReviewer()
        config = reviewer._timeout_config()
        self.assertGreater(config["subprocess_seconds"], config["read_seconds"])
        self.assertLess(config["connect_seconds"], config["read_seconds"])

    def test_prompt_records_are_compact_without_cutting_core_text(self) -> None:
        reviewer = ContextualNvidiaReviewer()
        record = {
            "region_id": "p001:REGION_001",
            "source_text": "A" * 220,
            "current_translation": "B" * 180,
            "previous_context": "P" * 500,
            "next_context": "N" * 500,
            "text_type": "dialogue",
            "ocr_confidence": 0.81234,
        }
        compact = reviewer._prompt_record(record)
        self.assertEqual(compact["source_text"], record["source_text"])
        self.assertEqual(compact["current_translation"], record["current_translation"])
        self.assertLessEqual(len(compact["previous_context"]), 161)
        self.assertLessEqual(len(compact["next_context"]), 161)
        self.assertNotIn("page_id", compact)
        self.assertNotIn("glossary", compact)

    def test_glossary_is_filtered_to_terms_used_by_the_batch(self) -> None:
        reviewer = ContextualNvidiaReviewer()
        glossary = {"terms": [
            {"term": "Sunny", "category": "name", "count": 9, "policy": "preserve"},
            {"term": "UnusedTerm", "category": "name", "count": 9, "policy": "preserve"},
        ]}
        compact = reviewer._compact_glossary(glossary, [{"source_text": "Sunny speaks.", "current_translation": ""}])
        self.assertEqual([item["term"] for item in compact["terms"]], ["Sunny"])

    def test_minimal_health_check_uses_same_structured_contract(self) -> None:
        reviewer = FakeProviderReviewer([{
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": "health-check",
            "results": [],
        }])
        result = reviewer.health_check(batch_id="health-check")
        self.assertTrue(result["ok"])
        self.assertEqual(reviewer.sent, ["health_check"])

    def test_requests_use_honored_structured_output_mechanism(self) -> None:
        captured: list[dict] = []

        class RecordingReviewer(FakeProviderReviewer):
            def _post_chat_completion(self, request_payload, started, purpose, **kwargs):
                captured.append(request_payload)
                return super()._post_chat_completion(request_payload, started, purpose, **kwargs)

        reviewer = RecordingReviewer([{
            "schema_version": REVIEW_SCHEMA_VERSION,
            "batch_id": "batch-1",
            "results": [{
                "region_id": "p001:REGION_001",
                "action": "keep",
                "revised_translation": "",
                "reason_code": "ok",
                "confidence": 0.9,
                "risk": "low",
                "terminology": [],
            }],
        }])
        reviewer.review_batch(self._records(), {}, batch_id="batch-1", diagnostic_mode=True)
        payload = captured[0]
        # The endpoint ignores nvext.guided_json for this model but honors
        # response_format json_schema strict, so the payload must use it.
        self.assertNotIn("nvext", payload)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertFalse(payload["chat_template_kwargs"]["thinking"])


class FullChapterQualityRevisionContracts(unittest.TestCase):
    def _fixture_output(self, root: Path) -> Path:
        from PIL import Image

        output = root / "output"
        output.mkdir()
        pdf = output / "chapter.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fake hermetic pdf\n" + b"0" * 2048)
        image = output / "page_001.jpg"
        Image.new("RGB", (240, 320), "white").save(image, "JPEG")
        progress = {
            "pdf_path": str(pdf),
            "pages": [
                {
                    "index": 1,
                    "sequence_index": 1,
                    "image_path": str(image),
                    "output_path": str(image),
                    "debug_data": {
                        "image_path": str(image),
                        "items": [
                            {
                                "id": "BALAO_1",
                                "region_id": "REGION_001",
                                "clean_text": "HELLO THERE",
                                "translation": "Olá.",
                                "classification": "speech",
                                "confidence": 0.99,
                                "bounding_box": [20, 20, 160, 80],
                                "sent_to_nvidia": True,
                                "redrawn": True,
                                "translation_final_state": "translated",
                            },
                            {
                                "id": "BALAO_2",
                                "region_id": "REGION_002",
                                "clean_text": "WHAT IS THIS?",
                                "translation": "WHAT IS THIS?",
                                "classification": "speech",
                                "confidence": 0.91,
                                "bounding_box": [20, 100, 180, 160],
                                "sent_to_nvidia": True,
                                "redrawn": False,
                                "manual_review_required": True,
                                "translation_final_state": "manual_review",
                            },
                            {
                                "id": "SFX_1",
                                "region_id": "REGION_003",
                                "clean_text": "BANG",
                                "translation": "BANG",
                                "classification": "sfx",
                                "confidence": 0.95,
                                "bounding_box": [20, 180, 180, 220],
                                "preserved_original": True,
                            },
                        ],
                    },
                }
            ],
        }
        quality = {
            "summary": {
                "pdf_path": str(pdf),
                "quality_validation": {
                    "smart_split_details": [
                        {"page": 1, "requires_review": True, "safe_band": False, "band_score": 1.2}
                    ]
                },
            },
            "pages": [{"index": 1}],
        }
        (output / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        (output / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
        return output

    def test_revision_run_uses_existing_artifacts_and_preserves_original_pdf_without_identical_copy(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            original = output / "chapter.pdf"
            before = original.read_bytes()
            revision = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            )
            status = revision.start()
            self.assertEqual(status["parent_job_id"], "job-1")
            self.assertEqual(status["parent_run_id"], "run-1")
            self.assertEqual(status["source_pdf_path"], str(original))
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(status["reviewed_pdf_path"], "")
            self.assertEqual(status["reviewed_pdf_sha256"], "")
            self.assertEqual(status["no_reviewed_pdf_reason"], "no_safe_changes_applied")
            self.assertEqual(status["total_pages"], 1)
            self.assertEqual(status["total_regions"], 3)
            self.assertEqual(status["publication_created"], False)

    def test_revision_creates_versioned_pdf_only_when_safe_changes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            original = output / "chapter.pdf"
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeRewriteReviewer,
            ).start()
            reviewed = Path(status["reviewed_pdf_path"])
            self.assertTrue(reviewed.is_file())
            self.assertEqual(reviewed.name, "chapter_reviewed_v2.pdf")
            self.assertNotEqual(reviewed.name, original.name)
            self.assertEqual(status["safe_changes_applied"], 1)
            self.assertEqual(status["publication_created"], False)

    def test_revision_sends_one_region_per_request(self) -> None:
        # The model is 100% ID-complete only with one region per request; batching
        # lets it silently omit regions, so both canary and full revision use bs=1.
        revision = ChapterQualityRevision("unused", job_id="job-1", run_id="run-1")
        records = [{"region_id": f"p001:REGION_{idx:03d}", "source_text": "HELLO", "current_translation": "Olá"} for idx in range(10)]
        self.assertEqual(revision._revision_batch_size(records[:1], canary=True), 1)
        self.assertEqual(revision._revision_batch_size(records[:10], canary=True), 1)
        self.assertEqual(revision._revision_batch_size(records[:10], canary=False), 1)

    def test_revision_filters_progress_pages_to_quality_report_pages(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            progress_path = output / "progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            extra = json.loads(json.dumps(progress["pages"][0]))
            extra["index"] = 2
            extra["sequence_index"] = 2
            extra["debug_data"]["items"][0]["region_id"] = "REGION_EXTRA"
            progress["pages"].append(extra)
            progress_path.write_text(json.dumps(progress), encoding="utf-8")
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            ).start()
            self.assertEqual(status["total_pages"], 1)
            self.assertEqual(status["total_regions"], 3)

    def test_revision_validates_region_ids_and_keeps_high_risk_manual(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = self._fixture_output(Path(folder))
            status = ChapterQualityRevision(
                output,
                job_id="job-1",
                run_id="run-1",
                reviewer_factory=FakeContextualReviewer,
            ).start()
            latest = json.loads((output / "quality_revision" / "latest_revision.json").read_text(encoding="utf-8"))
            contextual = json.loads(Path(latest["manifest_path"]).with_name("contextual_translation_review.json").read_text(encoding="utf-8"))
            by_id = {item["region_id"]: item for item in contextual["reviews"]}
            self.assertIn("p001:REGION_001", by_id)
            self.assertEqual(by_id["p001:REGION_002"]["action"], "manual_review")
            self.assertEqual(status["safe_changes_applied"], 0)
            self.assertEqual(status["status"], "review_required")

    def test_frontend_exposes_full_chapter_revision_action(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn("REVISAR CAPÍTULO INTEIRO", shell)
        self.assertIn("/api/ui/quality-review/revision/start", source)
        self.assertIn("pollQualityRevisionStatus", source)
        self.assertIn("qualityRevisionStatus", shell)

    def test_frontend_exposes_developer_only_contract_canary_action(self) -> None:
        shell = SHELL.read_text(encoding="utf-8")
        source = JS.read_text(encoding="utf-8")
        self.assertIn("TESTAR CONTRATO NVIDIA", shell)
        self.assertIn('id="nvidiaContractCanary" hidden', shell)
        self.assertIn("qualityReviewDeveloperMode", source)
        self.assertIn("/api/ui/quality-review/revision/canary/start", source)
        self.assertIn("tradutorDeveloperMode", source)
        self.assertIn("maxRegions = passed && reviewed >= 3", source)
        self.assertIn("passed && reviewed >= 1 ? 3 : 1", source)

    def test_nvidia_review_accepts_region_id_keyed_json(self) -> None:
        parsed = {
            "p001:REGION_002": {
                "action": "manual_review",
                "revised_translation": "",
                "reason_code": "needs_context",
                "confidence": 0.2,
                "risk": "high",
            }
        }
        items = ContextualNvidiaReviewer.review_items_from_parsed(parsed)
        self.assertEqual(items[0]["region_id"], "p001:REGION_002")
        self.assertEqual(items[0]["action"], "manual_review")

    def test_nvidia_review_translation_only_mapping_is_fail_closed(self) -> None:
        items = ContextualNvidiaReviewer.review_items_from_parsed({
            "p001:REGION_002": "Tradução sem contrato estruturado."
        })
        self.assertEqual(items[0]["region_id"], "p001:REGION_002")
        self.assertEqual(items[0]["action"], "manual_review")
        self.assertEqual(items[0]["risk"], "high")
        self.assertEqual(items[0]["reason_code"], "non_contract_translation_only_response")


if __name__ == "__main__":
    unittest.main()
