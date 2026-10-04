"""Apply one dependency-aware root-module move batch and rewrite Python imports."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports" / "repository_reorganization_v2"
KEEP_ROOT = {"conftest.py", "sitecustomize.py"}
SHIMS = {
    "app_ui.py": "yomu/ui/app.py",
    "desktop_app.py": "yomu/desktop/app.py",
    "down.py": "yomu/download/down.py",
    "main.py": "yomu/commands/legacy_gui.py",
    "run_local_folder.py": "yomu/commands/run_local_folder.py",
    "run_webtoon.py": "yomu/commands/run_webtoon.py",
    "start_tradutor.py": "yomu/commands/start_tradutor.py",
    "worker_service.py": "yomu/runtime/worker_service.py",
}
OVERRIDES = {
    "ad_url_policy.py": "yomu/security/ad_url_policy.py",
    "app_version.py": "yomu/runtime/version.py",
    "benchmark_environment.py": "tools/performance/benchmark_environment.py",
    "benchmark_ocr_engines.py": "tools/performance/benchmark_ocr_engines.py",
    "benchmark_pipeline.py": "yomu/pipeline/benchmark_pipeline.py",
    "classification_profiler.py": "yomu/ocr/classification_profiler.py",
    "cleanup_forensic_capture.py": "tools/research/cleanup_forensic_capture.py",
    "config.py": "yomu/config/settings.py",
    "conftest.py": "conftest.py",
    "fake_pipeline.py": "test_support/fake_pipeline.py",
    "generate_manual_review_report.py": "tools/reports/generate_manual_review_report.py",
    "hermetic_runtime.py": "test_support/hermetic_runtime.py",
    "image_validation.py": "yomu/download/image_validation.py",
    "job_runner.py": "yomu/runtime/job_runner.py",
    "job_store.py": "yomu/runtime/job_store.py",
    "local_media_preview_harness.py": "tools/harnesses/local_media_preview_harness.py",
    "local_media_registry.py": "yomu/ui/local_media_registry.py",
    "local_test_auth.py": "test_support/local_test_auth.py",
    "manga_translation_pipeline.py": "yomu/pipeline/legacy_gui_pipeline.py",
    "offline_test_guard.py": "test_support/offline_test_guard.py",
    "page_processing_contracts.py": "yomu/core/page_processing_contracts.py",
    "progress.py": "yomu/ui/progress.py",
    "performance_validation_harness.py": "tools/harnesses/performance_validation_harness.py",
    "quality_regression_diagnostic.py": "tools/research/quality_regression_diagnostic.py",
    "request_observability.py": "yomu/runtime/request_observability.py",
    "sitecustomize.py": "sitecustomize.py",
    "social_asset_maintenance_cli.py": "scripts/social_asset_maintenance_cli.py",
    "smart_split_incremental.py": "yomu/ocr/splitting/smart_split_incremental.py",
    "source_support_report.py": "yomu/review/source_support_report.py",
    "worker_service.py": "yomu/runtime/worker_service.py",
    "_test_bootstrap.py": "test_support/_test_bootstrap.py",
    "_test_processes.py": "test_support/_test_processes.py",
}
TOOL_ROOTS = ("tools/", "scripts/", "test_support/")


def run_git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=True).stdout


def target_for(record: dict) -> str:
    name = record["name"]
    if name in SHIMS:
        return SHIMS[name]
    if name in OVERRIDES:
        return OVERRIDES[name]
    if name in KEEP_ROOT:
        return name
    stem = Path(name).stem
    category = record["module_category"]
    bases = {
        "auth": "yomu/auth",
        "clean": "yomu/clean/ownership",
        "community": "yomu/community",
        "download": "yomu/download",
        "domain": "yomu/core",
        "entrypoint": "yomu/commands",
        "ocr": "yomu/ocr",
        "psd": "yomu/export",
        "review": "yomu/review",
        "runtime": "yomu/runtime",
        "test_support": "test_support",
        "translation": "yomu/translation",
        "ui": "yomu/ui",
        "utilities": "yomu/core",
        "pipeline": "yomu/pipeline",
        "core": "yomu/core",
        "configuration": "yomu/config",
        "script": "scripts",
    }
    base = bases.get(category)
    if category == "tool_or_research":
        base = "tools/research"
    if name in {"advanced_art_inpainting.py", "art_text_inpainting.py", "multiscale_patch_synthesis.py"}:
        base = "yomu/clean/inpainting"
    if name in {"reference_guided_reconstruction.py", "selective_artifact_reconstruction.py", "learned_rescue.py"}:
        base = "yomu/clean/reconstruction"
    if name in {"residual_analysis_artifacts.py", "residual_component_reviews.py", "residual_mask_revisions.py", "human_mask_decisions.py", "glyph_art_boundary_review.py", "source_glyph_envelope.py"}:
        base = "yomu/clean/ownership"
    if name.startswith("legacy_publication_") or name == "legacy_storage_upload_reservation.py":
        base = "yomu/community/legacy"
    if name in {"scrapling_reader_resolver.py", "dynamic_resolver_process.py"}:
        base = "yomu/download/readers"
    if name in {"local_folder_input.py", "local_folder_job.py", "local_folder_source.py", "local_images_source.py"}:
        base = "yomu/download/local"
    if name in {"canonical_source_identity.py", "chapter_source.py", "universal_chapter_adapter.py", "source_profile.py"}:
        base = "yomu/download/sources"
    if name in {"google_drive_factory.py", "google_drive_storage.py", "google_drive_transport.py", "drive_auth.py"}:
        base = "yomu/community/storage"
    if name in {"provider_execution.py", "provider_transport.py", "translation_proxy.py", "server_translation_authority.py", "yomu_backend_provider.py"}:
        base = "yomu/translation/providers"
    if name in {"translator_deepl.py", "translator_nllb.py", "translator_nvidia.py"}:
        base = "yomu/translation/providers"
    if name in {"review_reexport.py", "review_reexport_runner.py", "review_rerun.py", "review_rerun_runner.py"}:
        base = "yomu/review/workflows"
    if name in {"output_manifest.py", "page_manifest.py", "pdf.py", "pdf_reader.py", "pdf_naming.py", "png_output.py", "editable_psd_writer.py", "professional_psd.py", "psd_exporter.py"}:
        base = "yomu/export"
    if name in {"benchmark_pipeline.py", "manga_translation_pipeline.py", "pipeline_cache.py", "pipeline_streaming.py", "pipeline_telemetry.py", "session_context.py"}:
        base = "yomu/pipeline"
    if name in {"benchmark_environment.py", "benchmark_ocr_engines.py", "cleanup_forensic_capture.py", "quality_regression_diagnostic.py", "performance_validation_harness.py", "local_media_preview_harness.py"}:
        base = "tools/harnesses" if "harness" in name else "tools/research"
    if not base:
        raise ValueError(f"No canonical target assigned: {name} ({category})")
    return f"{base}/{stem}.py"


def batch_for(target: str) -> str:
    if target.startswith(("yomu/clean/", "yomu/ocr/", "yomu/translation/", "yomu/export/", "yomu/core/", "test_support/")):
        return "batch1_semantic_and_test_support"
    if target.startswith(("yomu/download/", "yomu/runtime/", "yomu/pipeline/", "yomu/config/", "yomu/commands/", "yomu/security/")):
        return "batch2_runtime_and_sources"
    return "batch3_ui_community_auth_and_tools"


def canonical_import_map(records: list[dict]) -> dict[str, str]:
    result = {}
    for record in records:
        name = record["name"]
        if not name.endswith(".py") or name in KEEP_ROOT or name in {"conftest.py", "sitecustomize.py"}:
            continue
        path = target_for(record)
        result[Path(name).stem] = path[:-3].replace("/", ".")
    return result


def plan(records: list[dict]) -> list[dict]:
    rows = []
    for record in records:
        if not record["name"].endswith(".py"):
            continue
        target = target_for(record)
        rows.append({"source": record["name"], "target": target,
                     "category": record["module_category"], "batch": batch_for(target),
                     "importers": record.get("importers", []),
                     "shim": record["name"] in SHIMS})
    return rows


def module_alias(alias: ast.alias, mapping: dict[str, str]) -> str:
    full = alias.name
    target = mapping.get(full)
    if not target:
        return f"import {full}" + (f" as {alias.asname}" if alias.asname else "")
    bound = alias.asname or full.split(".", 1)[0]
    return f"import {target} as {bound}"


def rewrite_python_imports(mapping: dict[str, str], selected: set[str]) -> list[dict[str, object]]:
    changed = []
    tracked = sorted(p for p in run_git("ls-files", "*.py").splitlines() if (ROOT / p).is_file())
    for rel in tracked:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(original, filename=rel)
        except SyntaxError:
            continue
        lines = original.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))

        def pos(line: int, col: int) -> int:
            raw = lines[line - 1]
            prefix = raw.encode("utf-8")[:col].decode("utf-8")
            return offsets[line - 1] + len(prefix)

        edits: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                selected_aliases = [a for a in node.names if a.name in selected]
                if not selected_aliases:
                    continue
                indent = re.match(r"\s*", lines[node.lineno - 1]).group(0)
                stmts = [module_alias(a, mapping) for a in node.names]
                replacement = ("\n" + indent).join(stmts)
                edits.append((pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset), replacement))
            elif isinstance(node, ast.ImportFrom) and node.module in selected:
                segment = ast.get_source_segment(original, node) or ""
                replacement = re.sub(r"(\bfrom\s+)" + re.escape(node.module) + r"(\s+import\b)",
                                     lambda m: m.group(1) + mapping[node.module] + m.group(2), segment, count=1)
                edits.append((pos(node.lineno, node.col_offset), pos(node.end_lineno, node.end_col_offset), replacement))
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                func = node.func
                is_dynamic_import = (
                    isinstance(func, ast.Name) and func.id == "__import__"
                ) or (
                    isinstance(func, ast.Attribute) and func.attr in {"import_module", "find_spec"}
                )
                is_mock_patch = (
                    isinstance(func, ast.Attribute) and func.attr == "patch"
                ) or (
                    isinstance(func, ast.Name) and func.id == "patch"
                )
                if not (is_dynamic_import or is_mock_patch):
                    continue
                value = node.args[0].value
                old = value.split(".", 1)[0]
                if old not in selected:
                    continue
                new = mapping[old] + value[len(old):]
                constant = node.args[0]
                start, end = pos(constant.lineno, constant.col_offset), pos(constant.end_lineno, constant.end_col_offset)
                quote = original[start:end][:1]
                replacement = quote + new + quote if quote in {"'", '"'} else repr(new)
                edits.append((start, end, replacement))
                # __import__("package.submodule") returns only "package" unless
                # a fromlist is supplied. Preserve the old direct-module semantics.
                if (isinstance(func, ast.Name) and func.id == "__import__"
                        and not any(keyword.arg == "fromlist" for keyword in node.keywords)):
                    call_end = pos(node.end_lineno, node.end_col_offset)
                    edits.append((call_end - 1, call_end - 1, ', fromlist=["*"]'))
        if not edits:
            continue
        rewritten = original
        for start, end, replacement in sorted(edits, reverse=True):
            rewritten = rewritten[:start] + replacement + rewritten[end:]
        if rewritten != original:
            path.write_text(rewritten, encoding="utf-8", newline="")
            changed.append({"path": rel, "edit_count": len(edits)})
    return changed


def write_shim(root_name: str, target: str) -> None:
    canonical = target[:-3].replace("/", ".")
    module_path = canonical.split(".")
    package, leaf = ".".join(module_path[:-1]), module_path[-1]
    impl = f"from {package} import {leaf} as _implementation\n"
    body = (
        f'"""Compatibility launcher; implementation lives in :mod:`{canonical}`."""\n'
        f"{impl}"
        "import sys as _sys\n\n"
        *(
            ('if __name__ == "__main__":\n'
             '    raise SystemExit(_implementation.main())\n')
            if root_name != "down.py" else ()
        ),
        "_sys.modules[__name__] = _implementation\n"
    )
    (ROOT / root_name).write_text(body, encoding="utf-8", newline="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", choices=("all", "batch1_semantic_and_test_support", "batch2_runtime_and_sources", "batch3_ui_community_auth_and_tools"), default="all")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    inventory_path = REPORTS / "root_inventory_before.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    records = inventory["files"]
    rows = plan(records)
    module_map = canonical_import_map(records)
    (REPORTS / "move_plan.json").write_text(json.dumps({"base_head": run_git("rev-parse", "HEAD").strip(), "modules": rows, "canonical_import_map": module_map}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.plan_only:
        print(json.dumps({"modules_to_move": sum(r["source"] != r["target"] for r in rows), "batches": {b: sum(r["batch"] == b for r in rows) for b in sorted({r["batch"] for r in rows})}}, indent=2))
        return 0

    selected_rows = [r for r in rows if r["source"] != r["target"] and (args.batch == "all" or r["batch"] == args.batch)]
    selected = {Path(r["source"]).stem for r in selected_rows}
    existing = {Path(r["source"]).stem for r in rows if (ROOT / r["source"]).exists()}
    missing = sorted(selected - existing)
    if missing:
        raise SystemExit(f"source modules are not at root: {missing}")
    for row in selected_rows:
        target = ROOT / row["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(ROOT), "mv", row["source"], row["target"]], check=True)
    for directory in sorted({(ROOT / row["target"]).parent for row in selected_rows}, key=lambda p: len(p.parts)):
        current = directory
        while current != ROOT and ROOT in current.parents:
            init = current / "__init__.py"
            if not init.exists():
                init.write_text('"""Yomu package."""\n', encoding="utf-8")
            current = current.parent
    selected_mapping = {key: value for key, value in module_map.items() if key in selected}
    import_changes = rewrite_python_imports(selected_mapping, selected)
    shim_rows = [r for r in selected_rows if r["source"] in SHIMS]
    for row in shim_rows:
        write_shim(row["source"], row["target"])
    log_path = REPORTS / "moves_executed.json"
    prior = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"batches": []}
    prior.setdefault("batches", []).append({
        "batch": args.batch,
        "moved": [{"source": r["source"], "target": r["target"]} for r in selected_rows],
        "rewritten_python_files": import_changes,
        "compatibility_shims": [r["source"] for r in shim_rows],
    })
    log_path.write_text(json.dumps(prior, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"batch": args.batch, "moved": len(selected_rows), "rewritten_python_files": len(import_changes), "shims": [r["source"] for r in shim_rows]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
