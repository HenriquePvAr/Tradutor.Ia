import argparse
import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from output_manifest import sanitize_source_url


REPO_ROOT = Path(__file__).resolve().parent
_LOCAL_SNAPSHOT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_MAX_LOCAL_MANIFEST_BYTES = 2 * 1024 * 1024


def build_parser():
    parser = argparse.ArgumentParser(
        description="Executa um capitulo com os modos simples fast ou quality.",
    )
    parser.add_argument("url", nargs="?", help="URL do capitulo.")
    parser.add_argument(
        "--local-folder",
        metavar="DIRETORIO",
        help=(
            "Pasta local de paginas, dentro de uma raiz LOCAL_INPUT_ROOTS permitida. "
            "As imagens sao validadas e copiadas para um snapshot interno antes do processamento."
        ),
    )
    # This is deliberately hidden from normal help.  It is the hand-off from the local job
    # runner, not a way to give the pipeline an arbitrary filesystem path.  The value is
    # revalidated below as one direct child of the owned local-snapshot workspace.
    parser.add_argument("--input-manifest", metavar="MANIFEST", help=argparse.SUPPRESS)
    parser.add_argument(
        "--mode",
        choices=("fast", "quality"),
        default="fast",
        help="fast usa RapidOCR hibrido; quality usa PaddleOCR.",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--cache",
        action="store_true",
        help="Reutiliza download, OCR, traducao e paginas validas (padrao).",
    )
    cache_group.add_argument(
        "--force",
        action="store_true",
        help="Ignora os caches de download, OCR, traducao e renderizacao.",
    )
    parser.add_argument(
        "--output",
        help="Nome da pasta dentro de output/ ou caminho de saida.",
    )
    parser.add_argument("--no-context", action="store_true", help="Desativa o contexto do capitulo.")
    parser.add_argument(
        "--keep-context",
        action="store_true",
        help="Mantem session_context.json (este ja e o comportamento padrao).",
    )
    parser.add_argument(
        "--delete-context-after",
        action="store_true",
        help="Apaga session_context.json somente depois que o PDF for gerado.",
    )
    parser.add_argument(
        "--open-output",
        action="store_true",
        help="Abre a pasta de saida quando a execucao terminar.",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        help="Limita paginas para teste; sem esta opcao processa o capitulo completo.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Coleta, valida e audita imagens sem executar OCR, traducao ou PDF.",
    )
    parser.add_argument(
        "--source-candidate-id",
        action="append",
        default=[],
        help="ID de uma pagina aprovada na revisao de fonte (uso interno da UI).",
    )
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        args = _interactive_args(parser)
    else:
        args = parser.parse_args(argv)

    source_type = _prepare_source(args, parser)
    if args.max_images is not None and args.max_images <= 0:
        parser.error("--max-images deve ser maior que zero")
    if args.no_context and (args.keep_context or args.delete_context_after):
        parser.error("--no-context nao pode ser combinado com opcoes de manutencao do contexto")
    if args.keep_context and args.delete_context_after:
        parser.error("use apenas --keep-context ou --delete-context-after")

    try:
        output_folder = (
            _resolve_local_output_folder(args.output, args.local_snapshot_ref)
            if source_type == "local_folder"
            else _resolve_output_folder(args.output, args.url)
        )
    except ValueError:
        parser.error("a saida da pasta local deve ficar dentro de output/")
    context_path = output_folder / "session_context.json"
    if args.download_only:
        return _run_download_only(args, output_folder)

    engine = _configure_mode(args.mode)
    full = args.max_images is None

    benchmark_args = SimpleNamespace(
        url=args.url,
        max_images=args.max_images if args.max_images is not None else 1,
        full=full,
        debug_folder=str(output_folder / "debug"),
        keep_debug=False,
        fast=True,
        benchmark=True,
        force=bool(args.force),
        force_download=bool(args.force),
        page_indices="",
        output_folder=str(output_folder),
        ocr_engine=engine,
        use_context=not args.no_context,
        session_context_path=str(context_path),
        source_candidate_ids=list(args.source_candidate_id or []),
        local_manifest_path=str(getattr(args, "local_manifest_path", "") or ""),
    )

    print(f"Capitulo: {sanitize_source_url(args.url)}")
    print(f"Modo: {args.mode} ({engine})")
    print(f"Cache: {'ignorado' if args.force else 'ativado'}")
    print(f"Contexto: {'desativado' if args.no_context else context_path}")
    print(f"Saida: {output_folder}")

    report = _run_benchmark(benchmark_args)
    pdf_path = Path(report.get("pdf_path") or "")
    if args.delete_context_after and pdf_path.is_file() and context_path.exists():
        context_path.unlink()
        print(f"Contexto temporario removido: {context_path}")

    from ui_helpers import derive_final_run_status

    final_status = derive_final_run_status(
        technical_success=bool(report.get("pdf_path")),
        quality_validation=report.get("quality_validation") or {},
    )
    if final_status == "review_required":
        print("\nExecucao concluida, mas requer revisao de qualidade")
    else:
        print("\nExecucao concluida")
    print(f"PDF: {pdf_path if pdf_path else 'nao gerado'}")
    if context_path.exists():
        print(f"Contexto: {context_path}")
    print(f"Relatorio: {report.get('timing_report_txt')}")

    if args.open_output:
        _open_folder(output_folder)
    return report


def _run_download_only(args, output_folder):
    if getattr(args, "local_manifest_path", ""):
        return _run_local_download_only(args, output_folder)

    from down import download_images

    input_folder = output_folder / "input"
    output_folder.mkdir(parents=True, exist_ok=True)
    max_images = args.max_images
    print(f"Download-only: {sanitize_source_url(args.url)}")
    print(f"Escopo: {'capitulo completo' if max_images is None else f'{max_images} imagens'}")
    print(f"Saida: {output_folder}")
    image_paths = download_images(
        args.url,
        max_images=max_images,
        debug_folder=str(output_folder),
        target_folder=str(input_folder),
        force=bool(args.force),
        approved_candidate_ids=list(args.source_candidate_id or []),
        progress_callback=lambda current, total, message: print(
            f"{message}: {current}/{total}",
            flush=True,
        ),
    )
    report_path = output_folder / "downloaded_images.json"
    with report_path.open("r", encoding="utf-8") as file:
        report = json.load(file)
    gate = report.get("download_gate") or {}
    print(f"Imagens validas: {len(image_paths)}")
    print(f"Download gate: {'aprovado' if gate.get('passed') else 'reprovado'}")
    print(f"Relatorio JSON: {output_folder / 'download_report.json'}")
    print(f"Relatorio HTML: {output_folder / 'download_report.html'}")
    print(f"Downloaded images: {report_path}")
    print(f"Contact sheet: {output_folder / 'download_contact_sheet.jpg'}")
    if args.open_output:
        _open_folder(output_folder)
    if not gate.get("passed"):
        raise RuntimeError(
            "Download gate reprovado: " + ", ".join(gate.get("reasons") or [])
        )
    return report


def _run_local_download_only(args, output_folder):
    """Materialise an already-owned local snapshot without importing the downloader.

    ``materialize_snapshot`` verifies the generated names, hashes and image bytes again.  It
    cannot dereference a user folder or a URL, and it writes only beneath the local output
    root that ``_resolve_local_output_folder`` has already constrained.
    """

    from local_folder_input import materialize_snapshot
    from pipeline_cache import atomic_write_json

    input_folder = output_folder / "input"
    max_images = args.max_images
    print(f"Download-only: {sanitize_source_url(args.url)}")
    print(f"Escopo: {'capitulo completo' if max_images is None else f'{max_images} imagens'}")
    print(f"Saida: {output_folder}")
    image_paths, report = materialize_snapshot(
        args.local_manifest_path,
        input_folder,
        max_images=max_images,
        clear_existing=bool(args.force),
    )
    output_folder.mkdir(parents=True, exist_ok=True)
    report_path = output_folder / "downloaded_images.json"
    atomic_write_json(report_path, report)
    gate = report.get("download_gate") or {}
    print(f"Imagens validas: {len(image_paths)}")
    print(f"Download gate: {'aprovado' if gate.get('passed') else 'reprovado'}")
    print(f"Downloaded images: {report_path}")
    if args.open_output:
        _open_folder(output_folder)
    if not gate.get("passed"):
        raise RuntimeError(
            "Download gate reprovado: " + ", ".join(gate.get("reasons") or [])
        )
    return report


def _prepare_source(args, parser):
    """Choose one source type and keep raw local paths out of run-facing arguments.

    URL support remains exactly as before.  A local folder is copied into an owned snapshot
    first; all later pipeline stages receive only an opaque ``local-folder:`` reference and
    an internal manifest path.  The latter is never written to a manifest, job DTO or console
    line by this runner.
    """

    raw_url = str(getattr(args, "url", "") or "").strip()
    raw_folder = str(getattr(args, "local_folder", "") or "").strip()
    raw_manifest = str(getattr(args, "input_manifest", "") or "").strip()
    source_count = sum(bool(value) for value in (raw_url, raw_folder, raw_manifest))
    if source_count != 1:
        parser.error("informe exatamente uma URL ou uma pasta local")

    args.local_manifest_path = ""
    args.local_snapshot_ref = ""
    if raw_url:
        args.url = _clean_url(raw_url)
        if not args.url.startswith(("http://", "https://")):
            parser.error("a URL deve comecar com http:// ou https://")
        return "url"

    if args.source_candidate_id:
        parser.error("--source-candidate-id nao se aplica a uma pasta local")
    try:
        if raw_folder:
            manifest_path, source_reference, snapshot_ref = _snapshot_local_folder(raw_folder)
        else:
            manifest_path, source_reference, snapshot_ref = _owned_local_manifest(raw_manifest)
    except Exception:
        # LocalFolderError inherits ValueError, but a filesystem race can also surface as a
        # lower-level exception.  Collapse every local-intake failure to this stable public
        # message rather than risking an absolute source path in a traceback.
        parser.error("entrada local recusada")

    args.url = source_reference
    args.local_manifest_path = str(manifest_path)
    args.local_snapshot_ref = snapshot_ref
    return "local_folder"


def _snapshot_local_folder(folder):
    """Create an immutable internal snapshot and return path-free run provenance."""

    from local_folder_input import local_source_reference, snapshot_workspace_root
    from local_folder_source import LocalFolderChapterAdapter

    snapshot = LocalFolderChapterAdapter().snapshot(folder, snapshot_workspace_root())
    return (
        Path(snapshot.manifest_path).resolve(strict=True),
        local_source_reference(snapshot.analysis.source_fingerprint),
        snapshot.workspace.name,
    )


def _owned_local_manifest(value):
    """Resolve a local snapshot manifest only when it has the owned direct-child layout."""

    from local_folder_input import local_source_reference, snapshot_workspace_root

    root = Path(snapshot_workspace_root()).resolve()
    supplied = Path(str(value or "")).expanduser()
    if not supplied.is_absolute():
        supplied = root / supplied
    manifest_path = supplied.resolve(strict=True)
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("manifest_outside_workspace") from exc
    if manifest_path.name != "manifest.json" or manifest_path.parent.parent != root:
        raise ValueError("invalid_snapshot_layout")
    snapshot_ref = manifest_path.parent.name
    if not _LOCAL_SNAPSHOT_REF_RE.fullmatch(snapshot_ref):
        raise ValueError("invalid_snapshot_ref")

    expected = root / snapshot_ref / "manifest.json"
    if _is_reparse_point(expected.parent) or _is_reparse_point(expected):
        raise ValueError("snapshot_reparse_point")
    if expected.resolve(strict=True) != manifest_path:
        raise ValueError("snapshot_layout_changed")
    try:
        size = manifest_path.stat().st_size
    except OSError as exc:
        raise ValueError("snapshot_manifest_unreadable") from exc
    if size <= 0 or size > _MAX_LOCAL_MANIFEST_BYTES:
        raise ValueError("snapshot_manifest_size")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("snapshot_manifest_unreadable") from exc
    if not isinstance(payload, dict) or str(payload.get("snapshot_id") or "") != snapshot_ref:
        raise ValueError("snapshot_manifest_identity")
    return manifest_path, local_source_reference(payload.get("source_fingerprint")), snapshot_ref


def _is_reparse_point(path):
    """Reject symlinks/junctions at the CLI-to-owned-workspace boundary."""

    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def _resolve_local_output_folder(value, snapshot_ref):
    """Keep local snapshots' materialised pages under the repository output root."""

    root = (REPO_ROOT / "output").resolve()
    if not value:
        suffix = re.sub(r"[^A-Za-z0-9_-]+", "", str(snapshot_ref or ""))[:24]
        candidate = root / f"local_chapter_{suffix or 'run'}"
    else:
        path = Path(value).expanduser()
        if path.is_absolute():
            candidate = path.resolve()
        elif path.parts and path.parts[0].casefold() == "output":
            candidate = (REPO_ROOT / path).resolve()
        else:
            candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("output_outside_root") from exc
    return candidate


def _run_benchmark(benchmark_args):
    """Small seam so local CLI routing is testable without importing the real pipeline."""

    from benchmark_pipeline import run_benchmark

    return run_benchmark(benchmark_args)


def _interactive_args(parser):
    print("Tradutor.Ia - modo interativo")
    url = input("URL do capitulo: ").strip()
    mode = input("Modo [fast/quality] (fast): ").strip().lower() or "fast"
    while mode not in {"fast", "quality"}:
        mode = input("Digite fast ou quality: ").strip().lower()
    cache_choice = input("Usar cache ou forcar reprocessamento? [cache/force] (cache): ").strip().lower()
    force = cache_choice == "force"
    default_output = _chapter_slug(_clean_url(url))
    output = input(f"Pasta de saida ({default_output}): ").strip() or default_output
    return parser.parse_args(
        [url, "--mode", mode, "--output", output] + (["--force"] if force else ["--cache"])
    )


def _configure_mode(mode):
    engine = "rapidocr" if mode == "fast" else "paddle"
    os.environ["OCR_ENGINE"] = engine
    os.environ["OCR_FALLBACK_ENGINE"] = "paddle"
    os.environ["OCR_HYBRID_FALLBACK"] = "True"
    os.environ["RAPIDOCR_ENABLED"] = "True" if engine == "rapidocr" else "False"

    import config

    config.OCR_ENGINE = engine
    config.OCR_FALLBACK_ENGINE = "paddle"
    config.OCR_HYBRID_FALLBACK = True
    config.RAPIDOCR_ENABLED = engine == "rapidocr"
    if engine == "rapidocr":
        config.POST_RENDER_OCR_VALIDATION = True
    return engine


def _resolve_output_folder(value, url):
    if not value:
        return (Path("output") / _chapter_slug(url)).resolve()
    path = Path(value).expanduser()
    if path.is_absolute() or (path.parts and path.parts[0].lower() == "output"):
        return path.resolve()
    return (Path("output") / path).resolve()


def _chapter_slug(url):
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part and part.lower() != "viewer"]
    useful = parts[-2:] if len(parts) >= 2 else parts[-1:]
    raw = "_".join(useful) or "webtoon_chapter"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-").lower()
    return slug or "webtoon_chapter"


def _clean_url(value):
    match = re.search(r"https?://[^\s\])]+", str(value or ""))
    return match.group(0) if match else str(value or "").strip()


def _open_folder(path):
    path = Path(path).resolve()
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.as_uri())
    except OSError as exc:
        print(f"Nao foi possivel abrir a pasta automaticamente: {exc}")


if __name__ == "__main__":
    main()
