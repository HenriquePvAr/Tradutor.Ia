"""Pure helpers used by the local NiceGUI runner.

This module intentionally has no NiceGUI dependency.  Keeping command building,
URL parsing and log parsing here makes the desktop UI small and testable.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from output_manifest import MANIFEST_FILENAME, sanitize_run_slug, sanitize_source_url
from runtime_paths import output_root, runtime_root


REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = output_root()
HISTORY_PATH = runtime_root() / "ui_history.json"

# The canonical set of selectable translation providers, in one place.  It used
# to be a `{"nemotron", "riva"}` literal repeated across six modules, which is
# exactly the shape that lets a new provider be accepted at one boundary and
# rejected at the next.  `deepl` is the beta default since Full E2E #11 cleared
# its quality_optimized output; Nemotron and Riva remain first-class explicit
# choices.  This constant is the single source of truth: UI, job creation and
# the runner read it instead of repeating a provider literal.
TRANSLATION_PROVIDERS = frozenset({"nemotron", "riva", "deepl"})
INTERNAL_TRANSLATION_PROVIDERS = frozenset({"yomu_backend"})
DEFAULT_TRANSLATION_PROVIDER = "deepl"


def normalize_translation_provider(value: object) -> str:
    """Canonical provider id, or "" when nothing was asked for.

    Raises for a value that was asked for but is not a provider: an unknown
    provider must never quietly collapse onto the default.
    """
    provider = str(value or "").strip().lower()
    if not provider:
        return ""
    if provider not in TRANSLATION_PROVIDERS and provider not in INTERNAL_TRANSLATION_PROVIDERS:
        raise ValueError("nvidia_translation_provider_invalid")
    return provider

_TECHNICAL_OUTPUT_MARKERS = {
    "benchmark",
    "cache",
    "cli",
    "debug",
    "fast",
    "final",
    "fix",
    "force",
    "forced",
    "full",
    "output",
    "preview",
    "quality",
    "recovery",
    "region",
    "regression",
    "rerun",
    "slice",
    "temp",
    "test",
}
_CHAPTER_OUTPUT_PATTERN = re.compile(
    r"^(?:ep|episode|chapter|ch|cap|capitulo|page|pages|pag|pagina)\d*$",
    re.IGNORECASE,
)

# Two-group patterns keep their label and mask only the value; one-group patterns are
# replaced whole.
_SECRET_PATTERNS = (
    re.compile(r"(?i)(NVIDIA_API_KEY\s*[=:]\s*)([^\s'\"]+)"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)([^\s,;]+)"),
    re.compile(
        r"(?i)((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|"
        r"password|passwd|pwd)\s*[=:]\s*)([^\s'\";,&]+)"),
    re.compile(r"(?i)((?:set-)?cookie\s*:\s*)(\S.*)"),
    re.compile(r"\b(nvapi-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b"),
)
_URL_IN_DIAGNOSTIC = re.compile(r"(?i)https?://[^\s<>\"'\]\[(){}]+")


def clean_url(value: str) -> str:
    match = re.search(r"https?://[^\s\])]+", str(value or ""))
    return match.group(0) if match else str(value or "").strip()


def sanitize_output_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", unquote(str(value or "")))
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:80] or "webtoon_chapter"


def build_run_output_slug(chapter_slug: object, run_id: object) -> str:
    """Return the only nested output path shape accepted for new UI runs.

    The runner accepts ``--output`` as either a folder name under ``output/`` or a path.
    New UI jobs need one extra directory level to keep repeated real runs immutable, but
    that must not reopen arbitrary path handling.  Therefore this helper emits exactly
    ``<safe_chapter>/<safe_run>`` and nothing user-path-shaped.
    """

    chapter = sanitize_output_name(str(chapter_slug or "webtoon_chapter"))
    run = sanitize_run_slug(run_id)
    if not run:
        raise ValueError("invalid_run_id")
    return f"{chapter}/{run}"


def resolve_run_output_folder(output_root: Path, chapter_slug: object, run_id: object) -> Path:
    """Resolve the immutable run folder and prove it remains under ``output_root``."""

    root = Path(output_root).resolve()
    relative = Path(build_run_output_slug(chapter_slug, run_id))
    if relative.is_absolute() or len(relative.parts) != 2:
        raise ValueError("invalid_output_path")
    folder = (root / relative).resolve()
    try:
        folder.relative_to(root)
    except ValueError as exc:
        raise ValueError("invalid_output_path") from exc
    return folder


def suggest_chapter_details(url: str) -> dict[str, str]:
    """Infer editable human and filesystem labels from a chapter-like URL."""

    parsed = urlparse(clean_url(url))
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[-1].casefold() == "viewer":
        parts.pop()

    work = parts[-2] if len(parts) >= 2 else "Webtoon"
    chapter = parts[-1] if parts else "chapter"
    query = parse_qs(parsed.query)
    episode_number = next(
        (
            str(query[key][0]).strip()
            for key in ("episode_no", "episode", "chapter_no", "chapter", "ep", "cap", "capitulo")
            if query.get(key) and str(query[key][0]).strip()
        ),
        "",
    )

    work_label = re.sub(r"[-_]+", " ", work).strip().title() or "Webtoon"
    chapter_label = re.sub(r"[-_]+", " ", chapter).strip()
    chapter_match = re.search(
        r"\b(episode|chapter|capitulo|cap|ep|ch)\s*(\d+)\b",
        chapter_label,
        flags=re.IGNORECASE,
    )
    if chapter_match:
        prefix = chapter_match.group(1).casefold()
        normalized_prefix = {
            "episode": "Episode",
            "chapter": "Chapter",
            "capitulo": "Capitulo",
            "cap": "CAP",
            "ep": "EP",
            "ch": "CH",
        }[prefix]
        chapter_label = f"{normalized_prefix} {chapter_match.group(2)}"
    elif episode_number:
        chapter_label = f"Episode {episode_number}"
    else:
        chapter_label = chapter_label.title()
    title = f"{work_label} - {chapter_label}" if chapter_label else work_label
    return {"title": title, "slug": sanitize_output_name(f"{work}_{chapter}")}


def infer_series_details(
    *,
    url: str = "",
    chapter_name: str = "",
    output_slug: str = "",
) -> dict[str, str]:
    """Infer a work identity without treating technical output names as works."""

    cleaned_url = clean_url(url)
    if cleaned_url.startswith(("http://", "https://")):
        parsed = urlparse(cleaned_url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if parts and parts[-1].casefold() == "viewer":
            parts.pop()
        if len(parts) >= 2:
            slug = sanitize_output_name(parts[-2])
            return {
                "name": re.sub(r"[-_]+", " ", parts[-2]).strip().title(),
                "slug": slug,
            }

    human_name = str(chapter_name or "").strip()
    human_parts = re.split(r"\s(?:-|\u2014)\s", human_name, maxsplit=1)
    if len(human_parts) > 1 and human_parts[0].strip():
        series_name = human_parts[0].strip()
        return {"name": series_name, "slug": sanitize_output_name(series_name)}
    if re.search(r"\s[-—]\s", human_name):
        series_name = re.split(r"\s[-—]\s", human_name, maxsplit=1)[0].strip()
        if series_name:
            return {"name": series_name, "slug": sanitize_output_name(series_name)}

    tokens = [token for token in sanitize_output_name(output_slug or human_name).split("_") if token]
    cutoff = len(tokens)
    for index, token in enumerate(tokens):
        if (
            token in _TECHNICAL_OUTPUT_MARKERS
            or token.isdigit()
            or _CHAPTER_OUTPUT_PATTERN.fullmatch(token)
        ):
            cutoff = index
            break
    useful_tokens = tokens[:cutoff] or tokens
    slug = "_".join(useful_tokens) or "serie_sem_nome"
    return {"name": slug.replace("_", " ").title(), "slug": slug}


def build_run_command(
    *,
    url: str,
    mode: str,
    output: str,
    full: bool,
    max_images: int | None,
    use_cache: bool,
    force: bool,
    use_context: bool,
    source_candidate_ids: list[str] | tuple[str, ...] | None = None,
    open_output: bool = False,
    download_only: bool = False,
    translation_provider: str | None = None,
    python_executable: str | None = None,
    output_path: Path | None = None,
) -> list[str]:
    from chapter_source import select_adapter

    cleaned_url = clean_url(url)
    if not cleaned_url.startswith(("http://", "https://")):
        raise ValueError("A URL precisa começar com http:// ou https://.")
    # Explicit source selection: an unregistered host is rejected here, before a job is
    # persisted, instead of failing obscurely inside the Webtoons runner.
    adapter = select_adapter(cleaned_url)
    adapter.validate_url(cleaned_url)
    cleaned_url = adapter.normalize_url(cleaned_url)
    # A recognised host alone is not enough to queue work.  Specific adapters intentionally
    # distinguish a reader chapter from a series index/profile, and accepting the latter here
    # would create a background browser job with no valid chapter contract.
    adapter.validate_url(cleaned_url)
    adapter.validate_path(cleaned_url)
    if mode not in {"fast", "quality"}:
        raise ValueError("O modo precisa ser fast ou quality.")
    if use_cache and force:
        raise ValueError("Cache e reprocessamento forçado são mutuamente exclusivos.")
    if not full and (max_images is None or int(max_images) <= 0):
        raise ValueError("Informe uma quantidade positiva de páginas para o teste parcial.")

    runner = str(getattr(adapter, "runner", "run_webtoon.py") or "run_webtoon.py")
    # Let a registered adapter describe its runner through the source contract, while still
    # refusing a page-controlled/path-like value.  URL adapters are internal code, but this
    # small validation keeps a future profile/plugin from turning a job command into an
    # arbitrary executable path.
    plan = adapter.build_command({"url": cleaned_url, "mode": mode, "output": output})
    if isinstance(plan, dict):
        proposed = str(plan.get("runner") or runner)
        if proposed == Path(proposed).name and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", proposed):
            runner = proposed

    if output_path is not None and not output_path.is_absolute():
        raise ValueError("authoritative_output_path_must_be_absolute")
    executable = python_executable or sys.executable
    # A frozen desktop executable cannot execute a source-file path as its second
    # argument.  Route the pipeline through the launcher’s internal-child dispatcher;
    # this keeps the exact same argv while avoiding the invalid
    # ``YomuSekai.exe run_webtoon.py`` shape seen in packaged runs.
    if bool(getattr(sys, "frozen", False)):
        command = [executable, "--internal-child", "pipeline", cleaned_url, "--mode", mode, "--output"]
    else:
        command = [executable, str(REPO_ROOT / runner), cleaned_url, "--mode", mode, "--output"]
    command.extend([
        str(output_path.resolve()) if output_path is not None else (
            build_run_output_slug(*str(output).split("/", 1))
            if "/" in str(output)
            else sanitize_output_name(output)
        ),
    ])
    if force:
        command.append("--force")
    elif use_cache:
        command.append("--cache")
    if not full:
        command.extend(["--max-images", str(int(max_images))])
    if not use_context:
        command.append("--no-context")
    if download_only:
        command.append("--download-only")
    provider = normalize_translation_provider(translation_provider)
    if provider:
        command.extend(["--translation-provider", provider])
    for candidate_id in source_candidate_ids or []:
        value = str(candidate_id or "").strip()
        if value:
            command.extend(["--source-candidate-id", value])
    if open_output:
        command.append("--open-output")
    return command


def requested_translation_provider(configuration: object) -> str:
    """The single canonical provider a job asked for, or "" when it never asked.

    Persisted job configuration is the only source of truth here: a rebuild must never fall
    back to the process environment or the runtime default, which is exactly how an explicit
    ``riva`` job silently executed Nemotron.
    """
    config = configuration if isinstance(configuration, dict) else {}
    provenance = config.get("provider_provenance")
    requested = (provenance or {}).get("provider_requested") if isinstance(provenance, dict) else ""
    return str(requested or config.get("translation_provider") or "").strip().lower()


def command_translation_provider(command: object) -> str:
    """The provider a concrete argv would actually run with, or "" when absent."""
    args = [str(value) for value in (command or [])]
    if "--translation-provider" not in args:
        return ""
    index = args.index("--translation-provider") + 1
    return args[index].strip().lower() if index < len(args) else ""


def assert_command_provider(command: list[str], configuration: object) -> list[str]:
    """Fail closed when a command lost or changed the provider the job requested.

    Applied to every rebuild and once more to the final argv, so a future rebuild caller that
    forgets ``translation_provider`` is caught before any provider call happens.
    """
    requested = requested_translation_provider(configuration)
    if not requested:
        return command                       # legacy/implicit job: nothing was requested
    actual = command_translation_provider(command)
    if not actual:
        raise ValueError("provider_argument_missing")
    if actual != requested:
        raise ValueError("provider_mismatch")
    return command


def record_command_provider(configuration: object, command: list[str], field: str) -> None:
    """Stamp which provider a persisted command actually carries.

    ``provider_requested`` alone proved insufficient: the stored submit command said ``riva``
    while the executed argv said nothing at all.  Recording the initial and final command
    provider makes that divergence visible in provenance instead of only in a post-mortem.
    """
    provenance = (configuration or {}).get("provider_provenance") if isinstance(configuration, dict) else None
    if isinstance(provenance, dict):
        provenance[field] = command_translation_provider(command)


def mask_secrets(text: str) -> str:
    masked = str(text or "")
    for pattern in _SECRET_PATTERNS:
        replacement = (
            r"\1[SEGREDO MASCARADO]" if pattern.groups == 2 else "[SEGREDO MASCARADO]"
        )
        masked = pattern.sub(replacement, masked)
    return masked


def sanitize_diagnostic_text(text: str) -> str:
    """Mask secrets and strip URL query/credential material from persisted diagnostics."""
    masked = mask_secrets(text)

    def replace_url(match: re.Match[str]) -> str:
        value = sanitize_source_url(match.group(0))
        return value or "[URL PROTEGIDA]"

    return _URL_IN_DIAGNOSTIC.sub(replace_url, masked)


def env_status(env_path: Path | None = None) -> dict[str, bool]:
    path = env_path or REPO_ROOT / ".env"
    configured = bool(os.getenv("NVIDIA_API_KEY", "").strip())
    if path.is_file() and not configured:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if raw_line.lstrip().startswith("#") or "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            if key.strip() == "NVIDIA_API_KEY":
                value = value.strip().strip("'\"")
                configured = bool(value and value != "sua_chave_aqui")
                break
    return {"env_exists": path.is_file(), "nvidia_configured": configured}


def quality_requires_review(
    quality_validation: dict[str, Any] | None = None,
    *,
    manual_review_count: int | None = None,
) -> bool:
    """Return whether a technically completed run needs quality review."""

    quality = quality_validation if isinstance(quality_validation, dict) else {}
    if manual_review_count is None:
        manual_review_count = quality.get("manual_review_required_groups", 0)
    try:
        manual_count = int(manual_review_count or 0)
    except (TypeError, ValueError):
        manual_count = 0
    passed = _coerce_optional_bool(quality.get("passed"))
    return manual_count > 0 or passed is False


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def derive_final_run_status(
    *,
    technical_success: bool,
    cancelled: bool = False,
    quality_validation: dict[str, Any] | None = None,
    manual_review_count: int | None = None,
) -> str:
    """Map technical completion and quality gate into a terminal status.

    A PDF may still be generated for inspection, but a failed quality gate or
    explicit manual-review items must not be represented as a clean success.
    Missing quality data remains compatible with older history records.
    """

    if cancelled:
        return "cancelled"
    if not technical_success:
        return "error"
    if quality_requires_review(
        quality_validation,
        manual_review_count=manual_review_count,
    ):
        return "review_required"
    return "finished"


@dataclass
class ProgressSnapshot:
    stage: str = "Preparando"
    # The stage the current/total counter was actually read from. The stage label advances
    # monotonically on keyword matches, so without this a counter emitted by an earlier
    # stage (99/99 downloads) would be displayed under a later label (Tradução).
    counter_stage: str = ""
    current: int = 0
    total: int = 0
    percent: float = 0.0
    pages: int = 0
    groups: int = 0
    errors: int = 0
    last_message: str = "Aguardando início"
    important_lines: list[str] = field(default_factory=list)


_STAGES = (
    (("selenium", "coleta", "baixando", "download"), "Baixando imagens", 0.08),
    (("validando", "validação", "validacao"), "Validando imagens", 0.18),
    ((" ocr", "ocr ", "ocr:", "ocr -"), "OCR", 0.28),
    (("classifica", "agrup"), "Classificação", 0.48),
    # The stage is provider-neutral: the runner now emits "Tradução:", and the
    # NVIDIA-suffixed needles are kept only so historical logs and persisted job
    # progress from Nemotron/Riva runs still resolve to the same stage.
    # Provider/configuration lines such as "Usando NVIDIA API"/"Usando DeepL" are
    # emitted while OCR is still running and carry no ``:`` marker, so they do not
    # match. ``NVIDIA:`` is the stable ASCII suffix that survives a Windows child
    # console corrupting only the accented word before it.
    (("tradução:", "traducao:", "tradução nvidia:", "traducao nvidia:", " nvidia:",
      "traduzindo"), "Tradução", 0.58),
    (("inpaint", "render", "redesen", "salvando página"), "Renderização", 0.78),
    (("gerando pdf", "pdf:"), "Geração de PDF", 0.9),
    (("relatório", "relatorio", "quality_report", "compare sheet"), "Relatórios", 0.95),
    (("execução concluída", "execucao concluida", "finalizado"), "Finalizado", 1.0),
)
_STAGE_RANK = {stage: index for index, (_, stage, _) in enumerate(_STAGES)}


def parse_progress_line(line: str, snapshot: ProgressSnapshot) -> ProgressSnapshot:
    clean = sanitize_diagnostic_text(line).strip()
    if not clean:
        return snapshot
    lowered = clean.casefold()
    fraction = re.search(
        r"(?P<label>[A-Za-zÀ-ÿ _-]{2,35})\s*[:#-]?\s*(?P<current>\d+)\s*/\s*(?P<total>\d+)",
        clean,
    )
    for needles, stage, base in _STAGES:
        if any(needle in lowered for needle in needles):
            if _STAGE_RANK.get(stage, 0) >= _STAGE_RANK.get(snapshot.stage, 0):
                snapshot.stage = stage
                snapshot.percent = max(snapshot.percent, base)
            break

    if fraction:
        current = int(fraction.group("current"))
        total = max(1, int(fraction.group("total")))
        snapshot.current, snapshot.total = current, total
        snapshot.counter_stage = snapshot.stage
        local = min(1.0, current / total)
        if snapshot.stage == "Baixando imagens":
            snapshot.percent = max(snapshot.percent, 0.08 + local * 0.1)
        elif snapshot.stage in {"OCR", "Classificação", "Tradução", "Renderização"}:
            snapshot.percent = max(snapshot.percent, 0.25 + local * 0.58)
        else:
            snapshot.percent = max(snapshot.percent, local * 0.85)

    page_match = re.search(r"(?:página|pagina)\s+(\d+)\s*/\s*(\d+)", lowered)
    if page_match:
        snapshot.pages = max(snapshot.pages, int(page_match.group(1)))
    groups_match = re.search(r"grupos?\s+traduzidos?\s*[:=]\s*(\d+)", lowered)
    if groups_match:
        snapshot.groups = int(groups_match.group(1))
    errors_match = re.search(r"(?:erros?|páginas? com erro)\s*[:=]\s*(\d+)", lowered)
    if errors_match:
        snapshot.errors = int(errors_match.group(1))

    important = fraction or any(
        token in lowered
        for token in (
            "erro",
            "fallback",
            "pdf",
            "conclu",
            "página",
            "pagina",
            "baixando",
            "validando",
            "nvidia",
            "relatorio",
            "relatório",
        )
    )
    if important:
        snapshot.last_message = clean[-260:]
        snapshot.important_lines = (snapshot.important_lines + [clean])[-30:]
    snapshot.percent = min(1.0, max(0.0, snapshot.percent))
    return snapshot


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def find_output_artifacts(output_folder: Path) -> dict[str, str]:
    folder = Path(output_folder).resolve()
    report = load_json(folder / "timing_report.json")

    def first_existing(*candidates: Any) -> str:
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if not path.is_absolute():
                path = folder / path
            try:
                resolved = path.resolve()
                # Run artifacts are opened/served by the UI.  A report or manifest must
                # never point that operation at an arbitrary filesystem location.
                resolved.relative_to(folder)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                return str(resolved)
        return ""

    # A run states where its PDF is, so the name never has to be rebuilt here.
    # Older outputs carry no manifest, so the timing report and finally any PDF in
    # the folder still resolve them, whatever they were called.
    manifest = load_json(folder / MANIFEST_FILENAME)
    return {
        "pdf_path": first_existing(
            manifest.get("pdf_path"),
            manifest.get("pdf_filename"),
            report.get("pdf_path"),
            *folder.glob("*.pdf"),
        ),
        "quality_report_path": first_existing(report.get("quality_report_html"), folder / "quality_report.html"),
        "compare_sheet_path": first_existing(report.get("preview_compare_sheet"), folder / "compare_sheet.jpg"),
        "contact_sheet_path": first_existing(report.get("preview_contact_sheet"), folder / "contact_sheet.jpg"),
        "session_context_path": first_existing(folder / "session_context.json"),
        "timing_report_path": first_existing(folder / "timing_report.txt"),
        "manifest_path": first_existing(folder / "run_manifest.json"),
    }
