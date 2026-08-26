import _test_bootstrap  # noqa: F401

import unittest

import benchmark_pipeline
from output_manifest import (
    build_run_manifest,
    load_verified_run_manifest,
    sanitize_physical_quality,
)
from pathlib import Path
import tempfile
import json

from ui_helpers import build_run_command


def _state(item):
    return {
        "index": 1,
        "debug_data": {
            "items": [
                {
                    "id": "BALAO_1",
                    "classification": "speech",
                    "clean_text": "I'VE TURNED INTO A BOSS MONSTER.",
                    "translation": "",
                    "translation_candidate": "",
                    "translation_valid": True,
                    "translation_final_state": "skipped_with_reason",
                    "translation_final_reason": "translation_not_selected",
                    "manual_review_required": True,
                    "preserved_original": True,
                    "redrawn": False,
                    **item,
                }
            ]
        },
    }


def _manifest(*, folder=None, quality_validation=None, quality_passed=False):
    pdf_path = str((folder or Path("chapter")).joinpath("chapter.pdf")) if folder else "chapter.pdf"
    return build_run_manifest(
        run_id="run-1",
        created_at="2026-08-13T00:00:00+00:00",
        source_url="https://example.invalid/chapter",
        commit_hash="abc",
        branch="fix/main-e2e-findings",
        pipeline_version="test",
        model="nvidia/riva-translate-4b-instruct-v2",
        final_status="finished" if quality_passed else "review_required",
        quality_passed=quality_passed,
        manual_review_count=0 if quality_passed else 1,
        rejected_count=0,
        pdf_path=pdf_path,
        slug="chapter",
        quality_validation=quality_validation,
    )


class ProviderProvenanceTests(unittest.TestCase):
    def test_requested_riva_cannot_silently_execute_nemotron(self):
        class Translator:
            stats = {
                "provider_name": "nemotron",
                "model": "nvidia/nemotron-3-super-120b-a12b",
            }

        with self.assertRaisesRegex(RuntimeError, "provider_mismatch"):
            benchmark_pipeline.resolve_provider_provenance(Translator(), "riva")

    def test_explicit_nemotron_still_resolves_normally(self):
        class Translator:
            stats = {
                "provider_name": "nemotron",
                "model": "nvidia/nemotron-3-super-120b-a12b",
            }

        provenance = benchmark_pipeline.resolve_provider_provenance(
            Translator(), "nemotron"
        )

        self.assertEqual(provenance["provider_requested"], "nemotron")
        self.assertEqual(provenance["provider_effective"], "nemotron")
        self.assertFalse(provenance["provider_mismatch"])

    def test_ui_command_persists_explicit_riva_provider(self):
        command = build_run_command(
            url=(
                "https://www.webtoons.com/en/action/"
                "the-returned-c-rank-tank-wont-die/episode-51/"
                "viewer?title_no=8474&episode_no=51"
            ),
            mode="fast",
            output="chapter",
            full=True,
            max_images=None,
            use_cache=False,
            force=True,
            use_context=True,
            translation_provider="riva",
        )

        self.assertIn("--translation-provider", command)
        self.assertEqual(command[command.index("--translation-provider") + 1], "riva")

    def test_manifest_records_provider_provenance_without_paths_or_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = build_run_manifest(
                run_id="run-1",
                created_at="2026-08-13T00:00:00+00:00",
                source_url="https://example.invalid/chapter?token=secret",
                commit_hash="abc",
                branch="fix/main-e2e-findings",
                pipeline_version="test",
                model="nvidia/riva-translate-4b-instruct-v2",
                final_status="finished",
                quality_passed=True,
                manual_review_count=0,
                rejected_count=0,
                pdf_path=str(folder / "chapter.pdf"),
                slug="chapter",
                provider_provenance={
                    "provider_requested": "riva",
                    "provider_effective": "riva",
                    "provider_model": "nvidia/riva-translate-4b-instruct-v2",
                    "provider_source": "run_argument",
                    "provider_fallback_used": False,
                    "provider_fallback_reason": "",
                },
            )
            (folder / "run_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            loaded = load_verified_run_manifest(folder)

        self.assertEqual(
            loaded["provider_provenance"]["provider_requested"], "riva"
        )
        self.assertEqual(
            loaded["provider_provenance"]["provider_effective"], "riva"
        )
        self.assertFalse(loaded["provider_provenance"]["provider_mismatch"])
        self.assertNotIn("token=secret", json.dumps(loaded))

    def test_provider_mismatch_is_explicit_invalid_provenance(self):
        manifest = build_run_manifest(
            run_id="run-1",
            created_at="2026-08-13T00:00:00+00:00",
            source_url="https://example.invalid/chapter",
            commit_hash="abc",
            branch="fix/main-e2e-findings",
            pipeline_version="test",
            model="nvidia/nemotron-3-super-120b-a12b",
            final_status="review_required",
            quality_passed=False,
            manual_review_count=1,
            rejected_count=0,
            pdf_path="chapter.pdf",
            slug="chapter",
            provider_provenance={
                "provider_requested": "riva",
                "provider_effective": "nemotron",
                "provider_model": "nvidia/nemotron-3-super-120b-a12b",
                "provider_source": "runtime_default",
                "provider_fallback_used": False,
            },
        )

        self.assertTrue(manifest["provider_provenance"]["provider_mismatch"])


class PhysicalResidualGateTests(unittest.TestCase):
    def test_review_retained_normal_english_fails_physical_gate(self):
        accounting = benchmark_pipeline._physical_residual_accounting([_state({})])

        self.assertEqual(accounting["physical_regions_expected"], 1)
        self.assertEqual(accounting["physical_regions_review_source_retained"], 1)
        self.assertEqual(accounting["physical_source_residual_count"], 1)
        self.assertFalse(accounting["physical_gate_passed"])
        self.assertEqual(accounting["physical_source_residual_group_ids"], ["p001:BALAO_1"])

    def test_successful_translation_render_passes_physical_gate(self):
        accounting = benchmark_pipeline._physical_residual_accounting([
            _state({
                "translation": "EU ME TORNEI UM MONSTRO CHEFE.",
                "translation_candidate": "EU ME TORNEI UM MONSTRO CHEFE.",
                "translation_final_state": "translated",
                "translation_final_reason": "ok",
                "manual_review_required": False,
                "preserved_original": False,
                "redrawn": True,
            })
        ])

        self.assertEqual(accounting["physical_regions_translated"], 1)
        self.assertEqual(accounting["physical_source_residual_count"], 0)
        self.assertTrue(accounting["physical_gate_passed"])

    def test_renderer_failure_is_not_a_silent_physical_pass(self):
        accounting = benchmark_pipeline._physical_residual_accounting([
            _state({
                "translation": "EU ME TORNEI UM MONSTRO CHEFE.",
                "translation_candidate": "EU ME TORNEI UM MONSTRO CHEFE.",
                "translation_final_state": "translated",
                "translation_final_reason": "ok",
                "manual_review_required": False,
                "preserved_original": False,
                "redrawn": False,
            })
        ])

        self.assertEqual(accounting["physical_regions_render_failed"], 1)
        self.assertEqual(accounting["physical_source_residual_count"], 1)
        self.assertFalse(accounting["physical_gate_passed"])

    def test_legitimate_preserved_source_is_not_counted_as_review_residual(self):
        accounting = benchmark_pipeline._physical_residual_accounting([
            _state({
                "clean_text": "PAEHYEOK",
                "translation": "",
                "translation_candidate": "",
                "translation_valid": True,
                "translation_final_state": "skipped_with_reason",
                "translation_final_reason": benchmark_pipeline.PROPER_NAME_ONLY_REASON,
                "manual_review_required": False,
                "preserved_original": True,
                "redrawn": False,
            })
        ])

        self.assertEqual(accounting["physical_regions_preserved"], 1)
        self.assertEqual(accounting["physical_regions_review_source_retained"], 0)
        self.assertEqual(accounting["physical_source_residual_count"], 0)
        self.assertTrue(accounting["physical_gate_passed"])


class PhysicalManifestPersistenceTests(unittest.TestCase):
    def test_review_required_physical_evidence_is_persisted_without_clean_pass(self):
        accounting = {
            "physical_regions_expected": 96,
            "physical_regions_translated": 91,
            "physical_regions_preserved": 0,
            "physical_regions_review_source_retained": 5,
            "physical_regions_rendered_with_review": 0,
            "physical_regions_render_failed": 0,
            "physical_regions_other_explicit": 0,
            "physical_source_residual_count": 5,
            "physical_source_residual_group_ids": [f"p001:BALAO_{i}" for i in range(1, 6)],
            "ordinary_story_physical_residual_count": 2,
            "ordinary_story_physical_residual_ids": ["p001:BALAO_1", "p001:BALAO_2"],
            "physical_gate_passed": False,
        }

        manifest = _manifest(quality_validation={"physical_quality": accounting})

        self.assertEqual(manifest["physical_quality"], accounting)
        self.assertFalse(manifest["physical_quality"]["physical_gate_passed"])
        self.assertEqual(manifest["physical_quality"]["physical_source_residual_count"], 5)

    def test_clean_physical_pass_is_persisted(self):
        accounting = {
            "physical_regions_expected": 96,
            "physical_regions_translated": 96,
            "physical_regions_preserved": 0,
            "physical_regions_review_source_retained": 0,
            "physical_regions_rendered_with_review": 0,
            "physical_regions_render_failed": 0,
            "physical_regions_other_explicit": 0,
            "physical_source_residual_count": 0,
            "physical_source_residual_group_ids": [],
            "ordinary_story_physical_residual_count": 0,
            "ordinary_story_physical_residual_ids": [],
            "physical_gate_passed": True,
        }

        manifest = _manifest(quality_validation={"physical_quality": accounting}, quality_passed=True)

        self.assertEqual(manifest["physical_quality"], accounting)
        self.assertTrue(manifest["physical_quality"]["physical_gate_passed"])

    def test_render_failure_physical_evidence_is_persisted_as_failed_gate(self):
        accounting = benchmark_pipeline._physical_residual_accounting([
            _state({
                "translation": "EU ME TORNEI UM MONSTRO CHEFE.",
                "translation_candidate": "EU ME TORNEI UM MONSTRO CHEFE.",
                "translation_final_state": "translated",
                "translation_final_reason": "ok",
                "manual_review_required": False,
                "preserved_original": False,
                "redrawn": False,
            })
        ])

        manifest = _manifest(quality_validation={"physical_quality": accounting})

        self.assertEqual(manifest["physical_quality"], accounting)
        self.assertEqual(manifest["physical_quality"]["physical_regions_render_failed"], 1)
        self.assertFalse(manifest["physical_quality"]["physical_gate_passed"])

    def test_missing_physical_data_does_not_create_false_pass(self):
        manifest = _manifest(quality_validation={"passed": True}, quality_passed=True)

        self.assertNotIn("physical_quality", manifest)

    def test_report_and_manifest_physical_projection_match(self):
        accounting = benchmark_pipeline._physical_residual_accounting([_state({})])
        quality_validation = {
            "physical_quality": accounting,
            "physical_source_residual_count": accounting["physical_source_residual_count"],
            "physical_source_residual_group_ids": accounting["physical_source_residual_group_ids"],
            "physical_gate_passed": accounting["physical_gate_passed"],
        }

        manifest = _manifest(quality_validation=quality_validation)

        self.assertEqual(
            sanitize_physical_quality(quality_validation),
            manifest["physical_quality"],
        )

    def test_pipeline_manifest_writer_uses_quality_validation_physical_source(self):
        accounting = benchmark_pipeline._physical_residual_accounting([_state({})])

        class Translator:
            model = "nvidia/riva-translate-4b-instruct-v2"

        manifest = benchmark_pipeline._output_run_manifest(
            Path("chapter"),
            {
                "run_signature": "sig",
                "url": "https://example.invalid/chapter",
                "status": "review_required",
                "pdf_path": "chapter/chapter.pdf",
                "quality_validation": {
                    "passed": False,
                    "manual_review_required_groups": 1,
                    "physical_quality": accounting,
                    "physical_source_residual_count": accounting[
                        "physical_source_residual_count"
                    ],
                    "physical_gate_passed": accounting["physical_gate_passed"],
                },
            },
            Translator(),
        )

        self.assertEqual(manifest["physical_quality"], accounting)
        self.assertFalse(manifest["quality_passed"])

    def test_loaded_manifest_preserves_physical_quality_block(self):
        accounting = benchmark_pipeline._physical_residual_accounting([_state({})])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = _manifest(
                folder=folder,
                quality_validation={"physical_quality": accounting},
            )
            (folder / "run_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            loaded = load_verified_run_manifest(folder)

        self.assertEqual(loaded["physical_quality"], accounting)


if __name__ == "__main__":
    unittest.main()
