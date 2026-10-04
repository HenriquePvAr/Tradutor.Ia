"""Inventory root files and statically map imports of root Python modules."""

from __future__ import annotations

import ast
import json
import subprocess
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports" / "repository_reorganization_v2"


def git(*args: str) -> set[str]:
    result = subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                            capture_output=True, text=True)
    return {line.replace("\\", "/") for line in result.stdout.splitlines() if line}


def module_category(name: str) -> tuple[str, str]:
    n = name.removesuffix(".py").lower()
    rules = [
        ("test_support", {"_test_bootstrap", "_test_processes", "conftest", "fake_pipeline", "hermetic_runtime", "offline_test_guard", "local_test_auth"}),
        ("entrypoint", {"app_ui", "desktop_app", "main", "start_tradutor", "run_local_folder", "run_webtoon", "down"}),
        ("clean", {"advanced_art_inpainting", "art_text_inpainting", "font_fidelity", "glyph_art_boundary_review", "human_mask_decisions", "learned_rescue", "multiscale_patch_synthesis", "reference_guided_reconstruction", "residual_analysis_artifacts", "residual_component_reviews", "residual_mask_revisions", "selective_artifact_reconstruction", "selective_review_retry", "source_glyph_envelope", "visual_refinement_provider_guard", "visual_review_decisions"}),
        ("ocr", {"benchmark_ocr_engines", "fast_ocr_policy", "nvidia_ocr_provider", "ocr_balloon", "ocr_contract", "ocr_engine", "ocr_hybrid_scheduler", "ocr_line_provenance", "ocr_memory_policy", "ocr_parallel", "ocr_streaming", "source_completeness"}),
        ("translation", {"human_translation_decisions", "linguistic_audit", "linguistic_triage", "natural_ptbr_refinement", "provider_execution", "semantic_fidelity", "server_translation_authority", "translation_proxy", "translator_deepl", "translator_nllb", "translator_nvidia", "translation_region_audit", "refinement_selection_decisions"}),
        ("psd", {"editable_psd_writer", "pdf", "pdf_naming", "pdf_reader", "png_output", "professional_psd", "psd_exporter"}),
        ("download", {"browser_runtime", "canonical_source_identity", "chapter_source", "down", "download_transport", "dynamic_resolver_process", "http_source_discovery", "image_validation", "lazy_slot_resolver", "local_folder_input", "local_folder_job", "local_folder_source", "local_images_source", "scrapling_reader_resolver", "source_profile", "universal_chapter_adapter", "webtoons_reader_bridge"}),
        ("community", {"canonical_social_identity", "chapter_asset_repository", "community_api", "community_auth", "community_authorization", "community_http", "community_publication_metadata", "community_publish_runner", "community_service", "community_storage", "community_storage_reconcile", "community_store", "drive_auth", "google_drive_factory", "google_drive_storage", "google_drive_transport", "legacy_publication_drive_adapter", "legacy_publication_migration_executor", "legacy_publication_migrator", "legacy_publication_postgres_adapter", "legacy_storage_upload_reservation", "social_asset_reconcile", "social_asset_retention", "social_content", "social_http", "social_pdf_http", "social_pdf_publishing", "social_repository", "supabase_social", "publish_authorization"}),
        ("auth", {"beta_license", "commercial_device", "commercial_foundation", "device_control", "desktop_identity", "local_test_auth", "secure_auth_context", "supabase_auth", "jwks_network_diagnostics"}),
        ("runtime", {"adaptive_scheduler", "app_version", "artifact_contract", "beta_control_plane", "build_profile", "installer_update", "job_completion", "job_failure_diagnostic", "job_runner", "job_store", "local_environment", "output_manifest", "page_manifest", "process_launcher", "process_options", "process_tree", "progress", "progression_service", "provider_transport", "resource_monitor", "request_observability", "runtime_contract", "runtime_paths", "runner_start_gate", "session_context", "update_bootstrap", "update_installer", "update_manifest", "update_transport", "worker_service", "worker_supervisor", "yk_reservation", "yomu_backend_provider"}),
        ("review", {"audit_decisions", "audit_registry", "chapter_quality_revision", "human_typography_decisions", "preview_gates", "region_taxonomy", "review_item", "review_manifest", "review_reexport", "review_reexport_runner", "review_rerun", "review_rerun_runner", "source_analysis_phase", "source_analysis_progress", "source_readiness", "source_support_report"}),
        ("ui", {"ui_bridge", "ui_helpers", "ui_history"}),
        ("utilities", {"ad_url_policy", "ayet_rewarded", "fake_pipeline", "json_utils", "manga_translation_pipeline", "pipeline_cache", "pipeline_streaming", "pipeline_telemetry", "smart_split_incremental"}),
        ("domain", {"page_processing_contracts"}),
    ]
    for category, members in rules:
        if n in members:
            if n in {"benchmark_ocr_engines", "benchmark_environment", "benchmark_pipeline", "classification_profiler", "performance_validation_harness", "quality_regression_diagnostic", "cleanup_forensic_capture", "generate_manual_review_report"}:
                return "tool_or_research", "tools/ or research/ (verify runtime imports)"
            return category, f"yomu/{category}/"
    if n in {"benchmark_environment", "benchmark_ocr_engines", "classification_profiler", "cleanup_forensic_capture", "generate_manual_review_report", "local_media_preview_harness", "performance_validation_harness", "quality_regression_diagnostic"}:
        return "tool_or_research", "tools/ or research/"
    if n == "benchmark_pipeline":
        return "pipeline", "yomu/pipeline/"
    if n == "local_media_registry":
        return "ui", "yomu/ui/"
    if n == "request_observability":
        return "runtime", "yomu/runtime/"
    if n == "page_processing_contracts":
        return "core", "yomu/core/"
    if n == "social_asset_maintenance_cli":
        return "script", "scripts/"
    if n == "config":
        return "configuration", "yomu/config/"
    if n == "sitecustomize":
        return "test_support", "thin root bootstrap shim"
    if n.startswith(("update_", "installer_")):
        return "runtime", "yomu/runtime/"
    return "unclassified", "manual review"


def import_names(tree: ast.AST) -> tuple[set[str], list[dict[str, object]]]:
    names: set[str] = set()
    details: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                names.add(top)
                details.append({"module": top, "line": node.lineno, "kind": "import"})
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".", 1)[0]
            names.add(top)
            details.append({"module": top, "line": node.lineno, "kind": "from"})
    return names, details


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    tracked = git("ls-files")
    root_files = sorted((p for p in ROOT.iterdir() if p.is_file()), key=lambda p: p.name.lower())
    py_modules = {p.stem: p for p in root_files if p.suffix.lower() == ".py"}
    repo_py = [ROOT / p for p in sorted(git("ls-files", "*.py")) if (ROOT / p).is_file()]
    importers: dict[str, set[str]] = defaultdict(set)
    edges: dict[str, set[str]] = defaultdict(set)
    parse_failures = []
    module_records = {}
    for path in repo_py:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            parse_failures.append({"path": path.relative_to(ROOT).as_posix(), "error": str(exc)})
            continue
        names, details = import_names(tree)
        rel = path.relative_to(ROOT).as_posix()
        local_mod = path.stem if path.parent == ROOT else None
        for name in names & py_modules.keys():
            importers[name].add(rel)
            if local_mod and name != local_mod:
                edges[local_mod].add(name)
        if local_mod:
            module_records[local_mod] = {
                "path": rel,
                "lines": len(path.read_text(encoding="utf-8").splitlines()),
                "imports_root_modules": sorted(names & py_modules.keys()),
                "import_sites": [item for item in details if item["module"] in py_modules],
            }

    file_records = []
    for path in root_files:
        name = path.name
        ext = path.suffix.lower()
        category, target = module_category(name) if ext == ".py" else ("non_python", "classify by extension/purpose")
        rel = name
        file_records.append({
            "name": name,
            "extension": ext or "<none>",
            "tracked": rel in tracked,
            "purpose": category,
            "module_category": category,
            "importers": sorted(importers.get(path.stem, set())) if ext == ".py" else [],
            "entrypoint": name in {"app_ui.py", "desktop_app.py", "main.py", "start_tradutor.py", "run_local_folder.py", "run_webtoon.py"},
            "packaging_dependency": any(name in (ROOT / p).read_text(encoding="utf-8", errors="ignore") for p in tracked if p.endswith((".spec", ".iss", ".yml", ".yaml", ".ps1", ".bat")) and (ROOT / p).is_file()),
            "public_api_evidence": bool(importers.get(path.stem)) if ext == ".py" else False,
            "test_support": category == "test_support",
            "runtime": ext == ".py" and category not in {"test_support", "entrypoint", "tool_or_research"},
            "r_and_d": category == "tool_or_research",
            "script": category == "entrypoint" or category == "tool_or_research",
            "config": ext in {".json", ".toml", ".ini", ".cfg", ".yaml", ".yml", ".spec"},
            "doc": ext in {".md", ".rst", ".txt"},
            "can_move": ext == ".py" and name not in {"conftest.py", "sitecustomize.py"},
            "target_package": target,
            "shim_required": name in {"app_ui.py", "desktop_app.py", "main.py", "start_tradutor.py", "run_local_folder.py", "run_webtoon.py", "down.py"},
            "bytes": path.stat().st_size,
        })

    dependency_map = {
        "root_python_module_count": len(py_modules),
        "repository_python_files_analyzed": len(repo_py),
        "parse_failures": parse_failures,
        "root_modules": module_records,
        "edges": [{"source": src, "target": dst} for src in sorted(edges) for dst in sorted(edges[src])],
        "importers": {name: sorted(paths) for name, paths in sorted(importers.items())},
        "highly_imported_modules": sorted(
            ({"module": name, "importer_count": len(paths), "importers": sorted(paths)}
             for name, paths in importers.items()), key=lambda item: (-item["importer_count"], item["module"])
        ),
    }
    (REPORTS / "root_inventory_before.json").write_text(json.dumps({
        "root": str(ROOT), "direct_file_count": len(root_files),
        "direct_python_file_count": len(py_modules), "files": file_records,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (REPORTS / "python_module_dependency_map.json").write_text(json.dumps(dependency_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"root_files": len(root_files), "root_python": len(py_modules),
                      "repo_python_analyzed": len(repo_py), "root_import_edges": sum(map(len, edges.values())),
                      "parse_failures": len(parse_failures), "reports": str(REPORTS)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
