"""Run one already-snapshotted local-folder chapter through the normal pipeline.

The job/UI boundary gives this runner an opaque snapshot identifier rather than a filesystem
path.  The runner resolves that identifier only beneath the application-owned snapshot root,
then delegates to :mod:`run_webtoon`'s local-manifest route.  No browser, downloader or
network client is imported before this gate succeeds.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_SNAPSHOT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Processa um snapshot local previamente validado.",
    )
    parser.add_argument(
        "--snapshot-ref",
        required=True,
        help="Referencia opaca de um snapshot local criado pelo aplicativo.",
    )
    parser.add_argument("--output", required=True, help="Pasta dentro de output/.")
    parser.add_argument("--mode", choices=("fast", "quality"), default="fast")
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument("--cache", action="store_true")
    cache_group.add_argument("--force", action="store_true")
    parser.add_argument("--no-context", action="store_true")
    parser.add_argument("--keep-context", action="store_true")
    parser.add_argument("--delete-context-after", action="store_true")
    parser.add_argument("--open-output", action="store_true")
    parser.add_argument("--max-images", type=int)
    parser.add_argument("--download-only", action="store_true")
    # The manifest is authoritative.  This flag is retained only for the existing job-command
    # contract and cannot turn off logical-page handling.
    parser.add_argument("--logical-pages", action="store_true", help=argparse.SUPPRESS)
    return parser


def resolve_snapshot_manifest(snapshot_ref: str) -> Path:
    """Return one owned manifest from an opaque direct-child workspace reference.

    A path, traversal segment, junction or missing workspace is a terminal input refusal.
    Keeping this check before importing ``run_webtoon`` is deliberate: malformed job data
    cannot initialise a downloader or any provider-facing module.
    """

    reference = str(snapshot_ref or "").strip()
    if not _SNAPSHOT_REF_RE.fullmatch(reference):
        raise ValueError("invalid_snapshot_ref")

    from local_folder_input import snapshot_workspace_root

    root = Path(snapshot_workspace_root()).resolve()
    workspace = root / reference
    manifest = workspace / "manifest.json"
    if _is_reparse_point(workspace) or _is_reparse_point(manifest):
        raise ValueError("snapshot_reparse_point")
    try:
        resolved = manifest.resolve(strict=True)
    except OSError as exc:
        raise ValueError("snapshot_missing") from exc
    if resolved.parent != workspace.resolve() or resolved.parent.parent != root:
        raise ValueError("snapshot_outside_workspace")
    return resolved


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_images is not None and args.max_images <= 0:
        print("--max-images deve ser maior que zero", file=sys.stderr)
        return 2
    if args.no_context and (args.keep_context or args.delete_context_after):
        print("--no-context nao pode ser combinado com opcoes de contexto", file=sys.stderr)
        return 2
    if args.keep_context and args.delete_context_after:
        print("use apenas --keep-context ou --delete-context-after", file=sys.stderr)
        return 2
    try:
        manifest = resolve_snapshot_manifest(args.snapshot_ref)
    except Exception:
        # The reference is user/job-controlled.  Do not echo it (or an internal path) to a
        # worker log, UI status or terminal transcript.
        print("Entrada local recusada: snapshot_ref_invalido", file=sys.stderr)
        return 2

    delegated = [
        "--input-manifest", str(manifest),
        "--output", str(args.output),
        "--mode", str(args.mode),
    ]
    if args.force:
        delegated.append("--force")
    elif args.cache:
        delegated.append("--cache")
    if args.max_images is not None:
        delegated.extend(["--max-images", str(args.max_images)])
    if args.download_only:
        delegated.append("--download-only")
    if args.no_context:
        delegated.append("--no-context")
    if args.keep_context:
        delegated.append("--keep-context")
    if args.delete_context_after:
        delegated.append("--delete-context-after")
    if args.open_output:
        delegated.append("--open-output")

    # Import only after the opaque-reference gate.  ``run_webtoon`` validates the exact
    # manifest layout, source fingerprint and output root a second time before invoking the
    # pipeline.
    from run_webtoon import main as run_webtoon_main

    run_webtoon_main(delegated)
    return 0


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


if __name__ == "__main__":
    raise SystemExit(main())
