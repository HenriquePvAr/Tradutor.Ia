"""Runtime bridge between the custom local frontend and ``run_webtoon.py``.

The browser never executes the pipeline directly.  This module validates UI
payloads, starts one shell-free subprocess at a time, masks logs, persists the
local history/profile and exposes serializable state to NiceGUI API routes.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ui_helpers import (
    OUTPUT_ROOT,
    REPO_ROOT,
    ProgressSnapshot,
    build_run_command,
    clean_url,
    derive_final_run_status,
    env_status,
    find_output_artifacts,
    load_json,
    mask_secrets,
    parse_progress_line,
    sanitize_output_name,
    suggest_chapter_details,
)
from ui_history import UIHistoryStore, utc_now


PROFILE_PATH = REPO_ROOT / ".cache" / "ui_profile.json"
PROFILE_MEDIA_DIR = REPO_ROOT / ".cache" / "ui_profile"
MAX_LOG_LINES = 3000
MAX_PROFILE_MEDIA_BYTES = 12 * 1024 * 1024
PROFILE_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        seconds = 0.0
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}min {remainder:02d}s" if minutes else f"{seconds:.1f}s"


def _read_env_file() -> dict[str, str]:
    path = REPO_ROOT / ".env"
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "não instalado"


def _profile_default() -> dict[str, Any]:
    return {
        "display_name": "você",
        "title": "",
        "pronouns": "",
        "status": "online",
        "status_text": "",
        "bio": "",
        "avatar_mode": "letter",
        "avatar_color": "#c5372c",
        "banner": "ink",
        "avatar_media_path": "",
        "avatar_media_type": "",
        "avatar_media_name": "",
        "avatar_media_size": 0,
        "banner_media_path": "",
        "banner_media_type": "",
        "banner_media_name": "",
        "banner_media_size": 0,
        "created_at": utc_now(),
    }


class UiBridge:
    """Own the single local worker, queue and persistent UI state."""

    def __init__(self) -> None:
        self.history_store = UIHistoryStore()
        self.history = self.history_store.discover_outputs()
        self.profile = self._load_profile()
        self.queue: list[dict[str, Any]] = []
        self.process: asyncio.subprocess.Process | None = None
        self.runner_task: asyncio.Task[Any] | None = None
        self.queue_running = False
        self.cancel_requested = False
        self.status = "ready"
        self.active_record: dict[str, Any] | None = None
        self.latest_record: dict[str, Any] | None = self.history[0] if self.history else None
        self.progress = ProgressSnapshot()
        self.started_monotonic = 0.0
        self.logs: list[dict[str, Any]] = []
        self.log_sequence = 0
        self.history_revision = 1
        self._lock = asyncio.Lock()

    def bootstrap(self, cursor: int = 0) -> dict[str, Any]:
        return {
            **self.runtime_state(cursor),
            "history": self.history,
            "profile": self._profile_payload(),
            "settings": self.settings(),
            "community": {"available": False, "posts": 0},
        }

    def runtime_state(self, cursor: int = 0) -> dict[str, Any]:
        elapsed = (
            max(0.0, time.monotonic() - self.started_monotonic)
            if self.status == "running" and self.started_monotonic
            else float((self.latest_record or {}).get("total_seconds") or 0)
        )
        current = int(self.progress.current or 0)
        total = int(self.progress.total or 0)
        stage_fraction = (current / total) if current and total else None
        return {
            "status": self.status,
            "active": self.active_record,
            "latest": self.latest_record,
            "progress": {
                "stage": self.progress.stage,
                "stage_key": self._stage_key(self.progress.stage),
                "current": current,
                "total": total,
                "fraction": stage_fraction,
                "indeterminate": stage_fraction is None and self.status == "running",
                "pages": int(self.progress.pages or 0),
                "groups": int(self.progress.groups or 0),
                "errors": int(self.progress.errors or 0),
                "last_message": self.progress.last_message,
                "elapsed_seconds": elapsed,
                "elapsed_label": _format_seconds(elapsed),
            },
            "logs": [entry for entry in self.logs if int(entry["seq"]) > int(cursor or 0)],
            "log_cursor": self.log_sequence,
            "queue": self.queue,
            "queue_running": self.queue_running,
            "history_revision": self.history_revision,
        }

    def settings(self) -> dict[str, Any]:
        values = _read_env_file()
        env = env_status()
        model = os.getenv("NVIDIA_TRANSLATION_MODEL") or values.get(
            "NVIDIA_TRANSLATION_MODEL", "nvidia/nemotron-3-super-120b-a12b"
        )
        return {
            "env_exists": env["env_exists"],
            "nvidia_configured": env["nvidia_configured"],
            "translation_mode": os.getenv("TRANSLATION_MODE") or values.get("TRANSLATION_MODE", "nvidia"),
            "translation_model": model,
            "translation_batch_size": os.getenv("NVIDIA_TRANSLATION_BATCH_SIZE")
            or values.get("NVIDIA_TRANSLATION_BATCH_SIZE", "20"),
            "max_requests_per_minute": os.getenv("NVIDIA_MAX_REQUESTS_PER_MINUTE")
            or values.get("NVIDIA_MAX_REQUESTS_PER_MINUTE", "20"),
            "ocr_engine": os.getenv("OCR_ENGINE") or values.get("OCR_ENGINE", "paddle"),
            "rapidocr_min_confidence": os.getenv("RAPIDOCR_MIN_CONFIDENCE")
            or values.get("RAPIDOCR_MIN_CONFIDENCE", "0.55"),
            "ocr_parallel": os.getenv("OCR_PARALLEL") or values.get("OCR_PARALLEL", "False"),
            "ocr_text_repair_mode": os.getenv("OCR_TEXT_REPAIR_MODE")
            or values.get("OCR_TEXT_REPAIR_MODE", "conservative"),
            "translate_sfx": os.getenv("TRANSLATE_SFX") or values.get("TRANSLATE_SFX", "False"),
            "prioritize_enclosed_text": os.getenv("PRIORITIZE_ENCLOSED_TEXT")
            or values.get("PRIORITIZE_ENCLOSED_TEXT", "True"),
            "rapidocr_available": bool(
                importlib.util.find_spec("rapidocr_onnxruntime")
                or importlib.util.find_spec("rapidocr")
            ),
            "paddle_available": bool(importlib.util.find_spec("paddleocr")),
            "python_version": platform.python_version(),
            "nicegui_version": _package_version("nicegui"),
            "rapidocr_version": _package_version("rapidocr-onnxruntime"),
            "paddleocr_version": _package_version("paddleocr"),
            "port": int(os.getenv("TRADUTOR_UI_PORT", "8080")),
        }

    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self.runner_task and not self.runner_task.done():
                raise ValueError("Já existe uma tradução ou fila em andamento.")
            normalized = self._normalize_payload(payload)
            self.cancel_requested = False
            self.runner_task = asyncio.create_task(self._single_worker(normalized))
        return {"ok": True, "run_id": normalized["id"]}

    async def _single_worker(self, payload: dict[str, Any]) -> None:
        try:
            await self._execute(payload)
        finally:
            self.runner_task = None

    async def cancel(self, *, queue: bool = False) -> dict[str, Any]:
        self.cancel_requested = True
        if queue:
            self.queue_running = False
            for item in self.queue:
                if item.get("status") == "waiting":
                    item["status"] = "cancelled"
        process = self.process
        if process and process.returncode is None:
            self._append_log("Cancelamento solicitado; encerrando o subprocesso com segurança.", "warn")
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        return {"ok": True}

    def add_queue_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_payload({**payload, "full": True}, require_environment=False)
        normalized["status"] = "waiting"
        normalized["error"] = ""
        self.queue.append(normalized)
        return normalized

    def remove_queue_item(self, item_id: str) -> None:
        self.queue = [
            item
            for item in self.queue
            if not (item.get("id") == item_id and item.get("status") == "waiting")
        ]

    def clear_queue(self) -> None:
        if self.queue_running:
            self.queue = [item for item in self.queue if item.get("status") == "processing"]
        else:
            self.queue.clear()

    async def start_queue(self) -> dict[str, Any]:
        async with self._lock:
            if self.runner_task and not self.runner_task.done():
                raise ValueError("Já existe uma tradução ou fila em andamento.")
            if not any(item.get("status") == "waiting" for item in self.queue):
                raise ValueError("A fila não tem itens aguardando.")
            status = env_status()
            if not status["env_exists"] or not status["nvidia_configured"]:
                raise ValueError("Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.")
            self.cancel_requested = False
            self.queue_running = True
            self.runner_task = asyncio.create_task(self._queue_worker())
        return {"ok": True}

    async def _queue_worker(self) -> None:
        try:
            for item in self.queue:
                if self.cancel_requested or not self.queue_running:
                    break
                if item.get("status") != "waiting":
                    continue
                item["status"] = "processing"
                try:
                    result = await self._execute(item)
                    item["status"] = result.get("status", "error")
                    item["record_id"] = result.get("id", "")
                except Exception as exc:  # keep the queue moving
                    item["status"] = "error"
                    item["error"] = mask_secrets(str(exc))
                    self._append_log(f"Item da fila falhou: {exc}", "error")
            if self.cancel_requested:
                for item in self.queue:
                    if item.get("status") == "waiting":
                        item["status"] = "cancelled"
        finally:
            self.queue_running = False
            self.runner_task = None

    async def _execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = build_run_command(
            url=payload["url"],
            mode=payload["mode"],
            output=payload["slug"],
            full=payload["full"],
            max_images=payload.get("max_images"),
            use_cache=payload["use_cache"],
            force=payload["force"],
            use_context=payload["use_context"],
            open_output=payload["open_output"],
            python_executable=sys.executable,
        )
        output_folder = (OUTPUT_ROOT / payload["slug"]).resolve()
        record = {
            "id": payload["id"],
            "chapter_name": payload["chapter_name"],
            "slug": payload["slug"],
            "url": payload["url"],
            "mode": payload["mode"],
            "scope": "full" if payload["full"] else "partial",
            "max_images": payload.get("max_images"),
            "cache_mode": "force" if payload["force"] else "cache",
            "started_at": utc_now(),
            "finished_at": "",
            "total_seconds": 0,
            "status": "running",
            "output_folder": str(output_folder),
            "pages_processed": 0,
            "groups_translated": 0,
            "errors": 0,
            "quality_gate": "",
        }
        self.active_record = record
        self.latest_record = record
        self.status = "running"
        self.started_monotonic = time.monotonic()
        self.progress = ProgressSnapshot(last_message="Preparando o pipeline local…")
        self.history_store.upsert(record)
        self._refresh_history()
        self._append_log("$ " + " ".join(json.dumps(part) if " " in part else part for part in command), "command")

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        error_message = ""
        return_code = 1
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=creationflags,
                env=environment,
            )
            assert self.process.stdout is not None
            while True:
                raw_line = await self.process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                self._append_log(line)
                previous_stage = self.progress.stage
                previous_current = self.progress.current
                previous_total = self.progress.total
                self.progress = parse_progress_line(line, self.progress)
                if self.progress.stage != previous_stage and not self._line_has_fraction(line):
                    self.progress.current = 0
                    self.progress.total = 0
                elif self.progress.stage == previous_stage and not self._line_has_fraction(line):
                    self.progress.current = previous_current
                    self.progress.total = previous_total
            return_code = await self.process.wait()
        except Exception as exc:
            error_message = mask_secrets(str(exc))
            self._append_log(f"Erro ao iniciar a execução: {error_message}", "error")
        finally:
            self.process = None

        cancelled = self.cancel_requested and return_code != 0
        elapsed = max(0.0, time.monotonic() - self.started_monotonic)
        report = load_json(output_folder / "timing_report.json")
        artifacts = find_output_artifacts(output_folder)
        quality = report.get("quality_validation") or {}
        technical_finished = return_code == 0 and bool(artifacts.get("pdf_path")) and not cancelled
        final_status = derive_final_run_status(
            technical_success=technical_finished,
            cancelled=cancelled,
            quality_validation=quality,
        )
        record.update(
            {
                "finished_at": utc_now(),
                "total_seconds": float(report.get("total_seconds") or elapsed),
                "status": final_status,
                **artifacts,
                "pages_processed": int(report.get("processed_images") or self.progress.pages or 0),
                "groups_translated": int(report.get("groups_translated") or self.progress.groups or 0),
                "sfx_preserved": int(report.get("groups_ignored_sfx_decorative") or 0),
                "errors": int(report.get("pages_with_error") or (0 if technical_finished else 1)),
                "quality_gate": quality.get("passed", ""),
                "last_message": error_message or self.progress.last_message,
            }
        )
        self.latest_record = self.history_store.upsert(record)
        self.active_record = None
        self.status = final_status if final_status in {"finished", "review_required", "error"} else "ready"
        if final_status in {"finished", "review_required"}:
            self.progress.stage = "Revisão necessária" if final_status == "review_required" else "Finalizado"
            self.progress.current = record["pages_processed"]
            self.progress.total = record["pages_processed"]
            self.progress.pages = record["pages_processed"]
            self.progress.groups = record["groups_translated"]
            self.progress.errors = record["errors"]
            if final_status == "review_required":
                self.progress.last_message = f"PDF gerado para revisão: {record.get('pdf_path', '')}"
                self._append_log("Execução finalizada com revisão de qualidade pendente.", "warn")
            else:
                self.progress.last_message = f"PDF pronto: {record.get('pdf_path', '')}"
                self._append_log("Execução finalizada com artefatos reais.", "success")
        elif cancelled:
            self.progress.last_message = "Execução cancelada pelo usuário."
        self._refresh_history()
        return record

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed_status = {"online", "away", "busy", "offline"}
        profile = _profile_default()
        profile.update(self.profile)
        profile.update(
            {
                "display_name": str(payload.get("display_name") or "você")[:40],
                "title": str(payload.get("title") or "")[:40],
                "pronouns": str(payload.get("pronouns") or "")[:30],
                "status": str(payload.get("status") or "online")
                if str(payload.get("status") or "online") in allowed_status
                else "online",
                "status_text": str(payload.get("status_text") or "")[:60],
                "bio": str(payload.get("bio") or "")[:190],
                "avatar_mode": "image"
                if str(payload.get("avatar_mode") or "letter") == "image"
                else "letter",
                "avatar_color": str(payload.get("avatar_color") or "#c5372c")[:16],
                "banner": str(payload.get("banner") or "ink")[:20],
            }
        )
        self.profile = profile
        self._write_profile()
        return self._profile_payload()

    def save_profile_media(
        self,
        kind: str,
        *,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        if kind not in {"avatar", "banner"}:
            raise ValueError("Tipo de mídia de perfil inválido.")
        suffix = Path(filename or "").suffix.casefold()
        expected_type = PROFILE_MEDIA_TYPES.get(suffix)
        if not expected_type or content_type.casefold().split(";", 1)[0] != expected_type:
            raise ValueError("Use PNG, JPG, JPEG, WEBP, GIF, MP4 ou WEBM.")
        if not content or len(content) > MAX_PROFILE_MEDIA_BYTES:
            raise ValueError("A mídia deve ter no máximo 12 MB.")

        PROFILE_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        target = (PROFILE_MEDIA_DIR / f"{kind}{suffix}").resolve()
        if PROFILE_MEDIA_DIR.resolve() not in target.parents:
            raise ValueError("Nome de mídia inválido.")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        for candidate in PROFILE_MEDIA_DIR.glob(f"{kind}.*"):
            if candidate.resolve() != target and candidate.suffix != ".tmp":
                candidate.unlink(missing_ok=True)

        self.profile[f"{kind}_media_path"] = str(target)
        self.profile[f"{kind}_media_type"] = expected_type
        self.profile[f"{kind}_media_name"] = Path(filename).name[:120]
        self.profile[f"{kind}_media_size"] = len(content)
        self.profile[f"{kind}_media_updated_at"] = utc_now()
        if kind == "avatar":
            self.profile["avatar_mode"] = "image"
        else:
            self.profile["banner"] = "custom"
        self._write_profile()
        return self._profile_payload()

    def remove_profile_media(self, kind: str) -> dict[str, Any]:
        if kind not in {"avatar", "banner"}:
            raise ValueError("Tipo de mídia de perfil inválido.")
        path = self.profile_media_path(kind)
        if path:
            path.unlink(missing_ok=True)
        for key in ("path", "type", "name", "size", "updated_at"):
            self.profile.pop(f"{kind}_media_{key}", None)
        self.profile[f"{kind}_media_path"] = ""
        self.profile[f"{kind}_media_type"] = ""
        self.profile[f"{kind}_media_name"] = ""
        self.profile[f"{kind}_media_size"] = 0
        if kind == "avatar":
            self.profile["avatar_mode"] = "letter"
        else:
            self.profile["banner"] = "ink"
        self._write_profile()
        return self._profile_payload()

    def profile_media_path(self, kind: str) -> Path | None:
        if kind not in {"avatar", "banner"}:
            return None
        raw_path = str(self.profile.get(f"{kind}_media_path") or "")
        if not raw_path:
            return None
        path = Path(raw_path).resolve()
        media_root = PROFILE_MEDIA_DIR.resolve()
        if media_root not in path.parents or not path.is_file():
            return None
        return path

    def open_artifact(self, path_value: str, *, select: bool = False) -> None:
        path = Path(str(path_value or "")).expanduser().resolve()
        output_root = OUTPUT_ROOT.resolve()
        if path != output_root and output_root not in path.parents:
            raise ValueError("A interface só abre artefatos dentro de output/.")
        if not path.exists():
            raise ValueError("Arquivo ou pasta não encontrado.")
        if os.name == "nt":
            if select and path.is_file():
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(path.as_uri())

    async def shutdown(self) -> None:
        await self.cancel(queue=True)

    def _normalize_payload(
        self,
        payload: dict[str, Any],
        *,
        require_environment: bool = True,
    ) -> dict[str, Any]:
        if require_environment:
            status = env_status()
            if not status["env_exists"] or not status["nvidia_configured"]:
                raise ValueError("Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.")
        url = clean_url(str(payload.get("url") or "")).strip()
        details = suggest_chapter_details(url)
        mode = str(payload.get("mode") or "fast")
        full = bool(payload.get("full", True))
        max_images = None if full else int(payload.get("max_images") or 0)
        force = bool(payload.get("force", False))
        use_cache = bool(payload.get("use_cache", not force))
        slug = sanitize_output_name(str(payload.get("slug") or details["slug"]))
        build_run_command(
            url=url,
            mode=mode,
            output=slug,
            full=full,
            max_images=max_images,
            use_cache=use_cache,
            force=force,
            use_context=bool(payload.get("use_context", True)),
        )
        return {
            "id": str(payload.get("id") or uuid.uuid4()),
            "url": url,
            "chapter_name": str(payload.get("chapter_name") or details["title"])[:120],
            "slug": slug,
            "mode": mode,
            "full": full,
            "max_images": max_images,
            "use_cache": use_cache,
            "force": force,
            "use_context": bool(payload.get("use_context", True)),
            "open_output": bool(payload.get("open_output", False)),
        }

    def _refresh_history(self) -> None:
        self.history = self.history_store.discover_outputs()
        self.history_revision += 1

    def _load_profile(self) -> dict[str, Any]:
        profile = _profile_default()
        try:
            stored = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                profile.update(stored)
        except (OSError, ValueError, TypeError):
            pass
        profile.pop("avatar_emoji", None)
        profile.pop("avatar_media", None)
        profile.pop("banner_media", None)
        if profile.get("avatar_mode") not in {"letter", "image"}:
            profile["avatar_mode"] = "letter"
        return profile

    def _write_profile(self) -> None:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROFILE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(PROFILE_PATH)

    def _profile_payload(self) -> dict[str, Any]:
        payload = dict(self.profile)
        for kind in ("avatar", "banner"):
            path = self.profile_media_path(kind)
            updated = str(payload.get(f"{kind}_media_updated_at") or "")
            payload[f"{kind}_media_url"] = (
                f"/api/ui/profile/media/{kind}?v={updated}"
                if path
                else ""
            )
        return payload

    def _append_log(self, line: str, kind: str = "info") -> None:
        safe = mask_secrets(str(line or ""))
        self.log_sequence += 1
        self.logs.append(
            {
                "seq": self.log_sequence,
                "time": datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S"),
                "kind": kind,
                "text": safe,
            }
        )
        self.logs = self.logs[-MAX_LOG_LINES:]

    @staticmethod
    def _line_has_fraction(line: str) -> bool:
        import re

        return bool(re.search(r"\d+\s*/\s*\d+", line))

    @staticmethod
    def _stage_key(stage: str) -> str:
        return {
            "Preparando": "prepare",
            "Baixando imagens": "download",
            "Validando imagens": "validation",
            "OCR": "ocr",
            "Classificação": "classification",
            "Tradução NVIDIA": "translate",
            "Renderização": "render",
            "Geração de PDF": "pdf",
            "Relatórios": "reports",
            "Finalizado": "final",
        }.get(stage, "prepare")
