import argparse
import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse


def build_parser():
    parser = argparse.ArgumentParser(
        description="Executa um capitulo com os modos simples fast ou quality.",
    )
    parser.add_argument("url", nargs="?", help="URL do capitulo.")
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
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        args = _interactive_args(parser)
    else:
        args = parser.parse_args(argv)

    if not args.url:
        parser.error("informe a URL do capitulo")
    args.url = _clean_url(args.url)
    if not args.url.startswith(("http://", "https://")):
        parser.error("a URL deve comecar com http:// ou https://")
    if args.max_images is not None and args.max_images <= 0:
        parser.error("--max-images deve ser maior que zero")
    if args.no_context and (args.keep_context or args.delete_context_after):
        parser.error("--no-context nao pode ser combinado com opcoes de manutencao do contexto")
    if args.keep_context and args.delete_context_after:
        parser.error("use apenas --keep-context ou --delete-context-after")

    output_folder = _resolve_output_folder(args.output, args.url)
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
    )

    print(f"Capitulo: {args.url}")
    print(f"Modo: {args.mode} ({engine})")
    print(f"Cache: {'ignorado' if args.force else 'ativado'}")
    print(f"Contexto: {'desativado' if args.no_context else context_path}")
    print(f"Saida: {output_folder}")

    from benchmark_pipeline import run_benchmark

    report = run_benchmark(benchmark_args)
    pdf_path = Path(report.get("pdf_path") or "")
    if args.delete_context_after and pdf_path.is_file() and context_path.exists():
        context_path.unlink()
        print(f"Contexto temporario removido: {context_path}")

    print("\nExecucao concluida")
    print(f"PDF: {pdf_path if pdf_path else 'nao gerado'}")
    if context_path.exists():
        print(f"Contexto: {context_path}")
    print(f"Relatorio: {report.get('timing_report_txt')}")

    if args.open_output:
        _open_folder(output_folder)
    return report


def _run_download_only(args, output_folder):
    from down import download_images

    input_folder = output_folder / "input"
    output_folder.mkdir(parents=True, exist_ok=True)
    max_images = args.max_images
    print(f"Download-only: {args.url}")
    print(f"Escopo: {'capitulo completo' if max_images is None else f'{max_images} imagens'}")
    print(f"Saida: {output_folder}")
    image_paths = download_images(
        args.url,
        max_images=max_images,
        debug_folder=str(output_folder),
        target_folder=str(input_folder),
        force=bool(args.force),
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
