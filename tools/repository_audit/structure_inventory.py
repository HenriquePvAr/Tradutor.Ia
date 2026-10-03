"""Generate before/after inventories for the repository layout audit."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.replace("\\", "/") for line in result.stdout.splitlines() if line]


def git_state(root: Path) -> tuple[set[str], set[str], set[str]]:
    tracked = set(git(root, "ls-files"))
    untracked = set(git(root, "ls-files", "--others", "--exclude-standard"))
    ignored = set(git(root, "ls-files", "--others", "--ignored", "--exclude-standard"))
    return tracked, untracked, ignored


def category(name: str, is_dir: bool) -> tuple[str, str]:
    lower = name.lower()
    if lower in {".env", ".env.local", ".modal.toml", ".oci"}:
        return "secret_or_local_credential", "SECRET_DO_NOT_COMMIT"
    if lower.startswith("test_") and not is_dir:
        return "automated_test", "MOVE_AND_COMMIT"
    if lower in {".venv", ".venv-beta", "node_modules", "__pycache__", ".cache", "build", "dist", "tmp", "temp", "output", "out", "checkpoints", "models"}:
        return "generated_or_local_runtime", "GENERATED_DO_NOT_COMMIT"
    if is_dir:
        known = {
            "apps": "application_package",
            "assets": "product_assets",
            "config": "configuration",
            "docs": "documentation",
            "experiments": "isolated_experiments",
            "packaging": "build_and_packaging",
            "reports": "canonical_or_generated_reports",
            "research": "research_code_and_evidence",
            "scripts": "operational_scripts",
            "static": "frontend_assets",
            "supabase": "database_and_edge_functions",
            "tests": "automated_tests",
            "tools": "developer_and_admin_tools",
            "ui": "desktop_ui",
        }
        return known.get(lower, "repository_directory"), "KEEP_ORGANIZED_DIRECTORY"
    suffix = Path(lower).suffix
    if suffix in {".py", ".mjs", ".js", ".ts", ".tsx", ".jsx"}:
        return "python_or_frontend_source", "KEEP_ROOT_LEGACY_RUNTIME" if not lower.startswith(("test_", "probe_", "debug_")) else "MOVE_AND_COMMIT"
    if suffix in {".md", ".rst", ".txt"}:
        return "documentation_or_project_metadata", "KEEP_ROOT_IF_CANONICAL"
    if suffix in {".json", ".toml", ".ini", ".cfg", ".yaml", ".yml"}:
        return "configuration_or_manifest", "KEEP_ROOT_IF_TOOLING_REQUIRES"
    if suffix in {".bat", ".ps1", ".sh"}:
        return "launcher_or_operational_script", "KEEP_ROOT_IF_ENTRYPOINT"
    return "other_root_file", "REVIEW"


def file_record(root: Path, relative: str, states: tuple[set[str], set[str], set[str]]) -> dict[str, object]:
    tracked, untracked, ignored = states
    path = root / relative
    status = "TRACKED" if relative in tracked else "UNTRACKED" if relative in untracked else "IGNORED" if relative in ignored else "UNCLASSIFIED"
    purpose, disposition = category(path.name, False)
    item: dict[str, object] = {
        "path": relative,
        "bytes": path.stat().st_size,
        "git_status": status,
        "purpose": purpose,
        "classification": "SECRET_DO_NOT_COMMIT" if purpose == "secret_or_local_credential" else disposition,
    }
    if purpose == "secret_or_local_credential":
        item["content_inspected"] = False
    return item


def inventory(root: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    states = git_state(root)
    entries: list[dict[str, object]] = []
    files: list[dict[str, object]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        rel = path.name
        if path.is_file():
            record = file_record(root, rel, states)
            files.append(record)
            entries.append(record)
            continue
        tracked, untracked, ignored = states
        tracked_descendants = any(item.startswith(rel + "/") for item in tracked)
        untracked_descendants = any(item.startswith(rel + "/") for item in untracked)
        ignored_descendants = any(item.startswith(rel + "/") for item in ignored)
        status = "TRACKED_CONTENT" if tracked_descendants else "UNTRACKED_CONTENT" if untracked_descendants else "IGNORED_CONTENT" if ignored_descendants else "EMPTY_OR_UNCLASSIFIED"
        purpose, disposition = category(rel, True)
        entries.append({
            "path": rel + "/",
            "type": "directory",
            "git_status": status,
            "purpose": purpose,
            "classification": disposition,
        })
    paths = sorted(states[0] | states[1])
    return entries, files, paths


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reference_map(root: Path) -> dict[str, object]:
    tracked, untracked, _ignored = git_state(root)
    test_names = {
        Path(path).name
        for path in tracked | untracked
        if Path(path).name.startswith("test_") and Path(path).suffix in {".py", ".mjs"}
    }
    pattern = re.compile(r"\btest_[A-Za-z0-9_]+\.(?:py|mjs)\b")
    eligible = {".py", ".mjs", ".js", ".ts", ".tsx", ".jsx", ".md", ".rst", ".toml", ".ini", ".cfg", ".json", ".yml", ".yaml", ".ps1", ".bat", ".sh", ".html"}
    matches: list[dict[str, object]] = []
    for relative in sorted(tracked | untracked):
        if relative.startswith("reports/repository_reorganization_v1/"):
            continue
        path = root / relative
        if not path.is_file() or path.suffix.lower() not in eligible:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        references = sorted({match.group(0) for line in lines for match in pattern.finditer(line)} & test_names)
        if references:
            matches.append({"source": relative, "references": references})
    return {"root": str(root), "moved_test_name_count": len(test_names), "files_with_references": len(matches), "matches": matches}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-root", type=Path, required=True)
    parser.add_argument("--after-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before = args.before_root.resolve()
    after = args.after_root.resolve()
    output = args.output.resolve()

    before_entries, before_files, before_paths = inventory(before)
    after_entries, after_files, after_paths = inventory(after)
    write_json(output / "root_inventory.json", {
        "source_of_before_state": str(before),
        "items": before_entries,
        "root_item_count": len(before_entries),
        "root_file_count": len(before_files),
    })
    write_json(output / "root_files_before.json", {"root": str(before), "files": before_files, "count": len(before_files)})
    write_json(output / "root_files_after.json", {"root": str(after), "files": after_files, "count": len(after_files)})
    write_json(output / "reference_map_before.json", reference_map(before))
    write_json(output / "reference_map_after.json", reference_map(after))
    for filename, root, paths in (
        ("repository_tree_before.txt", before, before_paths),
        ("repository_tree_after.txt", after, after_paths),
    ):
        (output / filename).write_text(
            f"Repository: {root}\n" + "\n".join(paths) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "root_items_before": len(before_entries),
        "root_files_before": len(before_files),
        "root_files_after": len(after_files),
        "tracked_or_untracked_paths_before": len(before_paths),
        "tracked_or_untracked_paths_after": len(after_paths),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
