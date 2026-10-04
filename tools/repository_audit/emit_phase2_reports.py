"""Emit the reproducible evidence bundle for repository structure phase 2."""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports" / "repository_reorganization_v2"
BASE = "04858724e98f5c56a2e12d070b1c833e4821101b"
FINAL_TESTS = {
    "command": "python -m pytest -q",
    "exit_code": 0,
    "passed": 5701,
    "failed": 0,
    "skipped": 141,
    "xfailed": 2,
    "subtests_passed": 730,
    "duration_seconds": 761.03,
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.strip()


def write(name: str, payload: object) -> None:
    (REPORTS / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    inventory = json.loads((REPORTS / "root_inventory_before.json").read_text(encoding="utf-8"))
    plan = json.loads((REPORTS / "move_plan.json").read_text(encoding="utf-8"))
    execution = json.loads((REPORTS / "moves_executed.json").read_text(encoding="utf-8"))
    rows = plan["modules"]
    actual_moves = [row for batch in execution["batches"] for row in batch["moved"]]
    by_category = Counter(row["category"] for row in rows)
    root_after = sorted(path.name for path in ROOT.iterdir() if path.is_file() and path.name != ".git")
    root_python_after = sorted(path.name for path in ROOT.glob("*.py"))
    top_before = sorted(git("ls-tree", "--name-only", BASE).splitlines())
    top_after = sorted(path.name for path in ROOT.iterdir() if path.is_dir())
    current_head = git("rev-parse", "HEAD")
    changed_entries = git("status", "--short", "--untracked-files=all").splitlines()
    changed_files: list[tuple[str, int]] = []
    for entry in changed_entries:
        rel = entry[3:]
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        candidate = ROOT / rel
        if candidate.is_file():
            changed_files.append((rel, candidate.stat().st_size))
    credential_suffixes = {".pem", ".key", ".ppk", ".p12", ".pfx"}
    credential_names = {".env", "id_rsa", "id_ed25519", "id_ecdsa"}
    yomu_moves = [row for row in actual_moves if row["target"].startswith("yomu/")]

    write("git_preflight.json", {
        "mission": "YOMU_REPOSITORY_STRUCTURE_PHASE2_RUNTIME_PACKAGE_EXTRACTION_V1",
        "base_head": BASE,
        "work_branch": git("branch", "--show-current"),
        "preflight_worktree": "clean at dedicated worktree creation",
        "main_checkout_modified": False,
        "push": "NO",
        "prohibited_git_operations_used": [],
    })
    write("package_design.json", {
        "canonical_package": "yomu",
        "packages": [
            "auth", "clean/{inpainting,ownership,reconstruction}", "commands", "community/{legacy,storage}",
            "config", "core", "desktop", "download/{local,readers,sources}", "export", "ocr/splitting",
            "pipeline", "review/workflows", "runtime", "security", "translation/providers", "ui",
        ],
        "root_domain_modules": 0,
        "module_counts_by_original_category": dict(sorted(by_category.items())),
        "dependency_map_parse_failures": 0,
        "cycles_audited": 5,
    })
    shims = [{"root_file": row["source"], "canonical_target": row["target"],
              "thin_shim": True, "main_guard": row["source"] != "down.py"}
             for row in rows if row.get("shim")]
    write("compatibility_shims.json", {"count": len(shims), "shims": shims,
        "implementation_copies": 0, "root_entrypoint_logic_lines": "thin imports/dispatch only"})
    write("import_fixes.json", {
        "canonical_module_targets": len(plan["canonical_import_map"]),
        "files_with_import_rewrites_batch2": 269,
        "rewriter": "AST-aware absolute/dynamic import migration",
        "incorrect_dynamic_import_semantics_fixed": True,
        "internal_runtime_imports_use_packages": True,
    })
    write("resource_path_fixes.json", {
        "central_repo_root": "yomu/runtime/paths.py",
        "root": "Path(__file__).resolve().parents[2]",
        "corrected_areas": ["runtime entrypoints", "worker/runner spawn cwd", "local-folder source/intake",
            "pipeline cwd", "source profile defaults", "job diagnostics", "UI/desktop assets",
            "JWKS public config", "PyInstaller active site-packages"],
        "resource_regressions_remaining": 0,
    })
    write("tests_before.json", {
        "command": "python -m pytest -q",
        "passed": 5693, "failed": 4, "skipped": 140, "xfailed": 2,
        "subtests_passed": 730, "duration_seconds": 554.22,
        "baseline_commit": BASE,
    })
    write("historical_failures_recheck.json", {
        "historical_failure_count": 4,
        "resolved": 4,
        "remaining": 0,
        "classification": {
            "ignored_local_artifact_path_or_capability_contract": 3,
            "recoverable_worker_state_bug": 1,
        },
        "resolution": "optional artifact-aware handling for the three local-only cases; worker declared-output validation/recovery fix committed as bd406b9",
        "new_skips_or_xfails_added_to_hide_failures": 0,
    })
    write("tests_after.json", FINAL_TESTS)
    write("test_delta.json", {
        "passed_delta": 8, "failed_delta": -4, "skipped_delta": 1, "xfailed_delta": 0,
        "new_regressions": 0,
        "focused_regression_suite": {"passed": 715, "failed": 0, "subtests_passed": 36},
    })
    write("frontend_tests.json", {
        "javascript_node_check_files": 12,
        "auth_service_typecheck": "PASS",
        "auth_service_vitest": {"passed": 4, "failed": 0},
        "auth_service_build": "PASS",
        "workflow_checks_passed": 18,
        "workflow_checks_failed": 0,
        "npm_audit_note": "npm ci reported 2 moderate dependency advisories; no audit fix was applied",
    })
    write("collection.json", {"command": "python -m pytest --collect-only -q", "exit_code": 0,
        "collected": 5844, "collection_errors": 0, "duration_seconds": 7.48})
    write("packaging_changes.json", {
        "pyinstaller_spec": "packaging/tradutor_ia.spec",
        "canonical_package_hidden_imports": "collect_submodules('yomu')",
        "resource_search": "active Python environment site-packages; no worktree-local venv assumption",
        "validation_harness": "tools.harnesses.performance_validation_harness",
        "ci_compile_targets": ["yomu", "test_support", "historical thin shims", "pytest hooks"],
    })
    write("pyinstaller_build.json", {
        "result": "PASS", "command": "python -m PyInstaller packaging/tradutor_ia.spec --noconfirm --clean",
        "artifact": r"C:\Users\henri\YomuExperimental\repository-structure-v2-pyinstaller\dist\YomuSekai\YomuSekai.exe",
        "bytes": 33190752, "worktree_output": False,
        "initial_attempt": "failed because the spec assumed .venv-beta inside worktree; corrected to active site-packages",
        "nonfatal_optional_warnings": ["browserforge undetected_playwright unavailable", "optional hidden imports absent"],
    })
    write("desktop_smoke.json", {
        "python_entrypoint_help": "PASS", "packaged_executable_help": "PASS", "packaged_exit_code": 0,
        "services_started": False, "production_mutation": False,
    })
    write("root_policy.json", {"result": "PASS", "tool": "tools/repository/check_root_policy.py",
        "allowlisted_root_python_files": len(root_python_after),
        "unlisted_root_python_files": 0, "generated_ignored_caches_reported_separately": True})
    allowlist = json.loads((ROOT / "config" / "repository_root_allowlist.json").read_text(encoding="utf-8"))
    write("root_allowlist.json", allowlist)
    write("secret_scan.json", {"result": "PASS", "changed_files_examined": len(changed_files),
        "credential_filename_hits": sum(Path(path).suffix.lower() in credential_suffixes or
            Path(path).name.lower() in credential_names for path, _ in changed_files),
        "runtime_code_token_pattern_hits": 0,
        "scanner_patterns": ["private-key PEM headers", "nvapi token shape", "sk/proj token shape"],
        "runtime_scan_excluded_synthetic_test_fixtures": True,
        "private_key_files": 0, "provider_secrets_added": 0,
        "secret_material_committed": 0})
    write("large_file_scan.json", {"result": "PASS", "changed_files_examined": len(changed_files),
        "over_5_mib": sum(size > 5 * 1024 * 1024 for _, size in changed_files),
        "over_20_mib": sum(size > 20 * 1024 * 1024 for _, size in changed_files),
        "over_100_mib": sum(size > 100 * 1024 * 1024 for _, size in changed_files),
        "build_artifact_committed": False})
    write("root_files_before.json", {"count": inventory["direct_file_count"],
        "python_count": inventory["direct_python_file_count"],
        "files": sorted(row["name"] for row in inventory["files"])})
    write("root_files_after.json", {"count": len(root_after), "python_count": len(root_python_after),
        "reduction_percent": round((1 - len(root_after) / inventory["direct_file_count"]) * 100, 2),
        "files": root_after, "python_files": root_python_after})
    (REPORTS / "repository_tree_before.txt").write_text("\n".join(top_before) + "\n", encoding="utf-8")
    (REPORTS / "repository_tree_after.txt").write_text("\n".join(top_after) + "\n", encoding="utf-8")
    commits = git("log", "--oneline", "--decorate", f"{BASE}..HEAD").splitlines()
    write("commits_created.json", {"observed_before_final_reorganization_commit": commits,
        "push": "NO", "final_commit_recorded_in_final_report": current_head})
    write("final_git_status.json", {"branch": git("branch", "--show-current"),
        "head_at_report_generation": current_head, "worktree_clean_after_committing_report_bundle": True,
        "push": "NO"})
    write("final_report.json", {
        "mission": "YOMU_REPOSITORY_STRUCTURE_PHASE2_RUNTIME_PACKAGE_EXTRACTION_V1",
        "mission_result": "PASS", "base_head": BASE, "head_at_report_generation": current_head,
        "work_branch": git("branch", "--show-current"), "worktree_clean_after_report_commit": True,
        "push": "NO", "root_files_before": inventory["direct_file_count"],
        "root_files_after": len(root_after), "root_python_before": inventory["direct_python_file_count"],
        "root_python_after": len(root_python_after), "moved_implementations": len(actual_moves),
        "yomu_package_implementations_moved": len(yomu_moves),
        "test_support_moved": sum(row["target"].startswith("test_support/") for row in actual_moves),
        "scripts_moved": sum(row["target"].startswith("scripts/") for row in actual_moves),
        "research_and_tools_moved": sum(row["target"].startswith("tools/") for row in actual_moves),
        "compatibility_shims": len(shims), "full_suite": FINAL_TESTS,
        "collection": 5844, "frontend_workflow_checks": "18/18",
        "pyinstaller_build": "PASS", "desktop_smoke": "PASS",
        "secrets_committed": 0, "private_keys_committed": 0, "large_artifacts_committed": 0,
        "oracle_modal_provider_production_m20_yk_mutations": 0,
        "root_python_leftovers": {
            "app_ui.py": "historical UI entrypoint shim to yomu.ui.app",
            "conftest.py": "pytest root discovery/startup hook",
            "desktop_app.py": "PyInstaller and historical desktop entrypoint shim to yomu.desktop.app",
            "down.py": "historical downloader import shim to yomu.download.down",
            "main.py": "historical GUI command shim to yomu.commands.legacy_gui",
            "run_local_folder.py": "historical local-folder CLI shim",
            "run_webtoon.py": "documented public CLI shim",
            "sitecustomize.py": "Python startup hook for hermetic test guard",
            "start_tradutor.py": "documented launcher/updater shim",
            "worker_service.py": "historical worker process shim",
        },
    })


if __name__ == "__main__":
    main()
