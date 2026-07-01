"""Local NiceGUI interface for running Tradutor.Ia chapters."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nicegui import app, ui

from ui_helpers import (
    OUTPUT_ROOT,
    REPO_ROOT,
    ProgressSnapshot,
    build_run_command,
    env_status,
    find_output_artifacts,
    load_json,
    mask_secrets,
    parse_progress_line,
    sanitize_output_name,
    suggest_chapter_details,
)
from ui_history import UIHistoryStore, utc_now


APP_PORT = int(os.getenv("TRADUTOR_UI_PORT", "8080"))
HISTORY = UIHistoryStore()
ACTIVE_PROCESSES: set[asyncio.subprocess.Process] = set()


CSS = r"""
:root {
  --bg: oklch(14% 0.012 245);
  --surface-1: oklch(18% 0.014 245);
  --surface-2: oklch(22% 0.016 245);
  --surface-3: oklch(27% 0.018 245);
  --border: oklch(31% 0.018 245);
  --border-strong: oklch(42% 0.02 245);
  --text-primary: oklch(95% 0.008 245);
  --text-secondary: oklch(73% 0.015 245);
  --text-tertiary: oklch(56% 0.015 245);
  --accent: oklch(84% 0.2 151);
  --accent-hover: oklch(89% 0.2 151);
  --accent-ink: oklch(18% 0.04 151);
  --cyan: oklch(75% 0.14 218);
  --danger: oklch(68% 0.21 27);
  --warning: oklch(80% 0.17 82);
  --radius-sm: 8px;
  --radius-md: 14px;
  --radius-lg: 22px;
  --ease: cubic-bezier(.32,.72,0,1);
  --font-display: "Bahnschrift", "Aptos Display", "Segoe UI", sans-serif;
  --font-body: "Aptos", "Segoe UI Variable", "Segoe UI", sans-serif;
  --font-mono: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
}

html, body, #app { min-height: 100%; background: var(--bg); color: var(--text-primary); }
body {
  font-family: var(--font-body);
  background:
    radial-gradient(circle at 78% -12%, oklch(35% 0.07 182 / .28), transparent 31rem),
    radial-gradient(circle at -12% 72%, oklch(24% 0.04 225 / .32), transparent 34rem),
    var(--bg);
}
body::after {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  opacity: .035; mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.92' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}
.q-layout, .q-page-container, .q-page { background: transparent !important; }
.app-shell { width: min(1480px, 100%); margin: 0 auto; padding: 22px 28px 56px; }
.brand-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin: 14px 0 20px; }
.brand-mark { display: flex; align-items: center; gap: 14px; }
.brand-glyph {
  display: grid; place-items: center; width: 46px; height: 46px; border-radius: 50%;
  color: var(--accent-ink); background: var(--accent); font-family: var(--font-mono);
  font-weight: 800; letter-spacing: -.08em; box-shadow: 0 0 34px oklch(84% .2 151 / .18);
}
.brand-name { margin: 0; font: 780 clamp(1.55rem, 3vw, 2.15rem)/.95 var(--font-display); letter-spacing: -.055em; }
.brand-subtitle { color: var(--text-secondary); font-size: .83rem; letter-spacing: .015em; margin-top: 6px; }
.status-chip {
  min-width: 120px; justify-content: center; padding: 8px 13px; border: 1px solid var(--border);
  border-radius: 999px; color: var(--text-secondary); background: oklch(19% .014 245 / .82);
  font: 700 .68rem/1 var(--font-mono); letter-spacing: .11em; text-transform: uppercase;
}
.status-chip[data-state="running"] { color: var(--accent); border-color: oklch(84% .2 151 / .36); }
.status-chip[data-state="finished"] { color: var(--cyan); border-color: oklch(75% .14 218 / .38); }
.status-chip[data-state="error"] { color: var(--danger); border-color: oklch(68% .21 27 / .38); }

.nav-tabs {
  padding: 5px; border: 1px solid var(--border); border-radius: var(--radius-md);
  background: oklch(18% .014 245 / .84); backdrop-filter: blur(18px); margin-bottom: 22px;
}
.nav-tabs .q-tab { min-height: 42px; border-radius: 10px; color: var(--text-secondary); }
.nav-tabs .q-tab--active { color: var(--text-primary); background: var(--surface-3); }
.nav-tabs .q-tab__indicator { display: none; }
.q-tab-panels, .q-panel { background: transparent !important; }
.q-tab-panel { padding: 0 !important; }

.hero-grid { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(310px, .75fr); gap: 18px; align-items: start; }
.stack { display: flex; flex-direction: column; gap: 16px; }
.surface {
  width: 100%; border: 1px solid var(--border); border-radius: var(--radius-lg);
  background: linear-gradient(145deg, oklch(19% .015 245 / .96), oklch(17% .012 245 / .92));
  padding: clamp(18px, 2.4vw, 28px); box-shadow: inset 0 1px oklch(100% 0 0 / .025);
}
.surface-accent { border-top: 2px solid var(--accent); }
.eyebrow { color: var(--accent); font: 720 .64rem/1 var(--font-mono); letter-spacing: .16em; text-transform: uppercase; }
.section-title { font: 720 clamp(1.1rem, 2vw, 1.45rem)/1.1 var(--font-display); letter-spacing: -.035em; margin: 10px 0 6px; }
.section-copy { max-width: 68ch; color: var(--text-secondary); font-size: .88rem; line-height: 1.55; }
.field-grid { display: grid; width: 100%; grid-template-columns: 1.25fr .75fr; gap: 12px; margin-top: 18px; }
.field-grid > * { width: 100%; min-width: 0; }
.field-grid-one { grid-template-columns: 1fr; }
.control-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.mode-grid { display: grid; width: 100%; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 16px; }
.mode-copy { min-height: 94px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-2); }
.mode-copy strong { display: block; color: var(--text-primary); margin-bottom: 5px; }
.mode-copy span { color: var(--text-secondary); font-size: .78rem; line-height: 1.45; }
.mode-copy-recommended { border-color: oklch(84% .2 151 / .3); }
.micro-label { color: var(--text-tertiary); font: 650 .62rem/1 var(--font-mono); letter-spacing: .11em; text-transform: uppercase; margin: 18px 0 8px; }
.quick-button { min-width: 44px; min-height: 38px; border-radius: 999px !important; border: 1px solid var(--border) !important; color: var(--text-secondary) !important; }
.primary-action {
  min-height: 52px; padding: 0 24px !important; border-radius: var(--radius-md) !important;
  background: var(--accent) !important; color: var(--accent-ink) !important;
  font-weight: 800 !important; letter-spacing: -.01em; transition: transform .18s var(--ease), background .18s var(--ease);
}
.primary-action:hover { transform: translateY(-2px); background: var(--accent-hover) !important; }
.secondary-action { min-height: 52px; border-radius: var(--radius-md) !important; border: 1px solid var(--border-strong) !important; color: var(--text-secondary) !important; }
.secondary-action:disabled { opacity: .42; }

.q-field--outlined .q-field__control { border-radius: var(--radius-md); background: oklch(15% .012 245 / .66); }
.q-field--outlined .q-field__control::before { border-color: var(--border); }
.q-field--outlined:hover .q-field__control::before { border-color: var(--border-strong); }
.q-field--focused .q-field__control::after { border-color: var(--accent) !important; }
.q-field__label, .q-field__native, .q-field__input { color: var(--text-primary) !important; }
.q-field__label { color: var(--text-secondary) !important; }
.q-toggle__inner--truthy { color: var(--accent) !important; }

.progress-rail { height: 8px !important; border-radius: 999px; overflow: hidden; background: var(--surface-3) !important; }
.progress-rail .q-linear-progress__model { background: var(--accent) !important; }
.progress-meta { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: end; margin: 16px 0 10px; }
.progress-stage { font: 700 1.02rem/1.2 var(--font-display); }
.progress-percent { color: var(--accent); font: 700 1.55rem/1 var(--font-mono); letter-spacing: -.06em; }
.last-message { min-height: 42px; color: var(--text-secondary); font: .72rem/1.5 var(--font-mono); overflow-wrap: anywhere; }
.metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 9px; margin-top: 16px; }
.metric { padding: 13px; border: 1px solid var(--border); border-radius: 12px; background: oklch(15% .012 245 / .48); }
.metric-value { font: 720 1.32rem/1 var(--font-mono); color: var(--text-primary); }
.metric-label { margin-top: 7px; color: var(--text-tertiary); font-size: .68rem; }
.result-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.result-actions .q-btn { min-height: 40px; border: 1px solid var(--border); border-radius: 10px; color: var(--text-secondary); }

.history-toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) auto; gap: 12px; margin-bottom: 16px; }
.history-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; }
.history-card { padding: 19px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-1); }
.history-card:hover { border-color: var(--border-strong); transform: translateY(-1px); transition: .18s var(--ease); }
.history-title { font: 690 1.04rem/1.2 var(--font-display); letter-spacing: -.025em; }
.history-meta { color: var(--text-tertiary); font: .65rem/1.45 var(--font-mono); margin: 7px 0 13px; }
.history-stats { display: flex; flex-wrap: wrap; gap: 6px; }
.history-stat { padding: 5px 8px; border-radius: 999px; background: var(--surface-3); color: var(--text-secondary); font-size: .67rem; }
.empty-state { min-height: 230px; display: grid; place-items: center; text-align: center; color: var(--text-secondary); border: 1px dashed var(--border-strong); border-radius: var(--radius-lg); }

.settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 14px; }
.setting-row { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 14px 0; border-bottom: 1px solid var(--border); }
.setting-row:last-child { border-bottom: 0; }
.setting-name { color: var(--text-secondary); }
.setting-value { font: 700 .75rem/1 var(--font-mono); }
.ok { color: var(--accent); } .bad { color: var(--danger); }
.alert-box { padding: 14px 16px; border: 1px solid oklch(80% .17 82 / .35); border-radius: var(--radius-md); color: var(--warning); background: oklch(25% .04 82 / .16); }

.log-shell { overflow: hidden; padding: 0; }
.log-toolbar { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--border); }
.live-log { height: min(64vh, 650px); padding: 14px; background: oklch(11% .01 245) !important; color: oklch(82% .03 170) !important; font: .71rem/1.55 var(--font-mono); }

*:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 900px) {
  .app-shell { padding: 14px 14px 40px; }
  .hero-grid, .settings-grid { grid-template-columns: 1fr; }
  .brand-row { align-items: flex-start; }
  .history-grid { grid-template-columns: 1fr; }
}
@media (max-width: 620px) {
  .brand-subtitle { max-width: 230px; }
  .field-grid, .mode-grid, .history-toolbar { grid-template-columns: 1fr; }
  .nav-tabs .q-tabs__content { justify-content: space-around; }
  .nav-tabs .q-tab__label { display: none; }
  .nav-tabs .q-tab { padding: 0 13px; min-width: 52px; }
  .surface { border-radius: 17px; padding: 17px; }
  .metric-grid { grid-template-columns: 1fr 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; }
}
"""


def _format_seconds(value: Any) -> str:
    try:
        seconds = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        seconds = 0.0
    minutes, remainder = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}min {remainder:02d}s"
    return f"{seconds:.1f}s"


def _open_path(path_value: str, *, select: bool = False) -> None:
    path = Path(path_value or "").resolve()
    if not path.exists():
        ui.notify("Arquivo não encontrado.", type="warning")
        return
    try:
        if os.name == "nt":
            if select and path.is_file():
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(path.as_uri())
    except OSError as exc:
        ui.notify(f"Não foi possível abrir: {exc}", type="negative")


class RunnerPage:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.process_started = 0.0
        self.active_record: dict[str, Any] | None = None
        self.latest_record: dict[str, Any] | None = None
        self.progress = ProgressSnapshot()
        self.log_lines: list[str] = []
        self.status = "ready"
        self.history_filter = ""
        self.history_status = "all"
        self.refs: dict[str, Any] = {}
        self._build()

    def _build(self) -> None:
        ui.add_css(CSS)
        ui.colors(primary="#4ff59a", secondary="#5ed9e8", negative="#f16f65")
        ui.dark_mode(True)
        with ui.column().classes("app-shell"):
            with ui.row().classes("brand-row w-full"):
                with ui.row().classes("brand-mark"):
                    ui.label("T.").classes("brand-glyph")
                    with ui.column().classes("gap-0"):
                        ui.label("Tradutor.Ia").classes("brand-name")
                        ui.label("OCR + NVIDIA, sem o ritual do PowerShell").classes("brand-subtitle")
                self.refs["status"] = ui.label("Pronto").classes("status-chip").props('data-state="ready"')

            with ui.tabs().classes("nav-tabs w-full") as tabs:
                new_tab = ui.tab("Nova tradução", icon="add_circle_outline")
                history_tab = ui.tab("Capítulos traduzidos", icon="library_books")
                settings_tab = ui.tab("Configurações", icon="tune")
                logs_tab = ui.tab("Logs", icon="terminal")
            self.refs["tabs"] = tabs
            self.refs["new_tab"] = new_tab

            with ui.tab_panels(tabs, value=new_tab).classes("w-full"):
                with ui.tab_panel(new_tab):
                    self._build_new_translation()
                with ui.tab_panel(history_tab):
                    self._build_history()
                with ui.tab_panel(settings_tab):
                    self._build_settings()
                with ui.tab_panel(logs_tab):
                    self._build_logs()

        ui.timer(1.0, self._tick_elapsed)

    def _build_new_translation(self) -> None:
        with ui.element("main").classes("hero-grid"):
            with ui.column().classes("stack"):
                with ui.card().classes("surface surface-accent"):
                    ui.label("01 / CAPÍTULO").classes("eyebrow")
                    ui.label("Do link ao PDF, numa única tela.").classes("section-title")
                    ui.label(
                        "Cole o capítulo, revise o nome sugerido e escolha entre velocidade híbrida ou máxima cautela."
                    ).classes("section-copy")
                    self.refs["url"] = (
                        ui.input(
                            "URL do capítulo",
                            placeholder="Cole a URL do capítulo Webtoon",
                            on_change=self._on_url_change,
                        )
                        .classes("w-full mt-5")
                        .props('outlined data-testid="chapter-url"')
                    )
                    with ui.element("div").classes("field-grid"):
                        self.refs["chapter_name"] = (
                            ui.input("Nome do capítulo")
                            .classes("w-full")
                            .props('outlined data-testid="chapter-name"')
                        )
                        self.refs["output"] = (
                            ui.input("Pasta de saída", on_change=self._sanitize_output_live)
                            .classes("w-full")
                            .props('outlined data-testid="output-folder"')
                        )

                with ui.card().classes("surface"):
                    ui.label("02 / MOTOR").classes("eyebrow")
                    ui.label("Escolha o equilíbrio.").classes("section-title")
                    self.refs["mode"] = (
                        ui.toggle(
                            {"fast": "Rápido", "quality": "Qualidade"},
                            value="fast",
                        )
                        .classes("w-full")
                        .props('spread no-caps data-testid="mode-selector"')
                    )
                    with ui.element("div").classes("mode-grid"):
                        with ui.element("div").classes("mode-copy mode-copy-recommended"):
                            ui.html("<strong>Rápido · recomendado</strong><span>RapidOCR híbrido, com fallback regional para Paddle e gate de qualidade.</span>")
                        with ui.element("div").classes("mode-copy"):
                            ui.html("<strong>Qualidade</strong><span>PaddleOCR em todas as páginas. Mais lento e mais conservador.</span>")

                    ui.label("Escopo").classes("micro-label")
                    self.refs["scope"] = (
                        ui.toggle(
                            {"full": "Capítulo completo", "partial": "Testar uma parte"},
                            value="full",
                            on_change=self._on_scope_change,
                        )
                        .classes("w-full")
                        .props('spread no-caps data-testid="scope-selector"')
                    )
                    self.refs["partial_box"] = ui.column().classes("w-full gap-2")
                    self.refs["partial_box"].set_visibility(False)
                    with self.refs["partial_box"]:
                        self.refs["max_images"] = (
                            ui.number("Quantidade de páginas/imagens", value=5, min=1, step=1)
                            .classes("w-full")
                            .props('outlined data-testid="max-images"')
                        )
                        with ui.row().classes("control-row"):
                            for amount in (3, 5, 20, 50):
                                ui.button(
                                    str(amount),
                                    on_click=lambda value=amount: self._set_max_images(value),
                                ).props("flat dense no-caps").classes("quick-button")

                with ui.card().classes("surface"):
                    ui.label("03 / OPÇÕES").classes("eyebrow")
                    ui.label("Como esta execução deve se comportar?").classes("section-title")
                    self.refs["cache"] = ui.switch(
                        "Usar cache", value=True, on_change=self._cache_changed
                    ).props('data-testid="use-cache"')
                    self.refs["force"] = ui.switch(
                        "Forçar reprocessamento", value=False, on_change=self._force_changed
                    ).props('data-testid="force-run"')
                    self.refs["context"] = ui.switch(
                        "Usar contexto temporário do capítulo", value=True
                    )
                    self.refs["open_folder"] = ui.switch("Abrir pasta ao finalizar", value=False)
                    self.refs["open_pdf"] = ui.switch("Abrir PDF ao finalizar", value=False)
                    with ui.row().classes("control-row mt-4"):
                        self.refs["start"] = (
                            ui.button("Iniciar tradução", icon="play_arrow", on_click=self._start_run)
                            .classes("primary-action")
                            .props('no-caps data-testid="start-run"')
                        )
                        self.refs["cancel"] = (
                            ui.button("Cancelar", icon="stop", on_click=self._cancel_run)
                            .classes("secondary-action")
                            .props('outline no-caps data-testid="cancel-run"')
                        )
                        self.refs["cancel"].disable()

            with ui.column().classes("stack"):
                with ui.card().classes("surface"):
                    ui.label("EXECUÇÃO AO VIVO").classes("eyebrow")
                    with ui.element("div").classes("progress-meta"):
                        self.refs["stage"] = ui.label("Preparando").classes("progress-stage")
                        self.refs["percent"] = ui.label("0%").classes("progress-percent")
                    self.refs["progress_bar"] = ui.linear_progress(value=0, show_value=False).classes("progress-rail w-full")
                    self.refs["last_message"] = ui.label("Aguardando uma nova tradução.").classes("last-message mt-3")
                    self.refs["elapsed"] = ui.label("Tempo decorrido · 0.0s").classes("micro-label")
                    with ui.element("div").classes("metric-grid"):
                        self._metric("pages", "0", "Páginas")
                        self._metric("groups", "0", "Grupos traduzidos")
                        self._metric("errors", "0", "Erros")
                        self._metric("gate", "—", "Gate de qualidade")

                with ui.card().classes("surface"):
                    ui.label("RESULTADO").classes("eyebrow")
                    self.refs["result_title"] = ui.label("Nenhum capítulo processado nesta sessão.").classes("section-title")
                    self.refs["result_copy"] = ui.label(
                        "Quando terminar, PDF, relatórios e contexto aparecerão aqui."
                    ).classes("section-copy")
                    self.refs["result_actions"] = ui.row().classes("result-actions")
                    self._render_result_actions({})

    def _metric(self, key: str, value: str, label: str) -> None:
        with ui.element("div").classes("metric"):
            self.refs[f"metric_{key}"] = ui.label(value).classes("metric-value")
            ui.label(label).classes("metric-label")

    def _build_history(self) -> None:
        with ui.card().classes("surface"):
            ui.label("BIBLIOTECA LOCAL").classes("eyebrow")
            ui.label("Capítulos traduzidos").classes("section-title")
            ui.label("Histórico privado deste computador. Nada daqui vai para o Git.").classes("section-copy")
            with ui.element("div").classes("history-toolbar mt-4"):
                self.refs["history_search"] = ui.input(
                    "Filtrar por nome",
                    on_change=lambda event: self._set_history_filter(event.value),
                ).props("outlined clearable")
                self.refs["history_status"] = ui.select(
                    {"all": "Todos", "finished": "Finalizados", "error": "Com erro"},
                    value="all",
                    on_change=lambda event: self._set_history_status(event.value),
                ).props("outlined")
            self._history_cards()

    @ui.refreshable
    def _history_cards(self) -> None:
        records = HISTORY.discover_outputs()
        query = self.history_filter.casefold().strip()
        if query:
            records = [record for record in records if query in str(record.get("chapter_name", "")).casefold()]
        if self.history_status != "all":
            records = [record for record in records if record.get("status") == self.history_status]
        if not records:
            with ui.element("div").classes("empty-state"):
                ui.html("<div><strong>Nenhum capítulo por aqui.</strong><br><span>A primeira tradução vai inaugurar esta estante.</span></div>")
            return
        with ui.element("div").classes("history-grid"):
            for record in records:
                self._history_card(record)

    def _history_card(self, record: dict[str, Any]) -> None:
        with ui.element("article").classes("history-card"):
            ui.label(str(record.get("chapter_name") or record.get("slug") or "Capítulo")).classes("history-title")
            date = str(record.get("finished_at") or record.get("started_at") or "")[:16].replace("T", " · ")
            ui.label(f"{date}  /  {str(record.get('mode') or '—').upper()}  /  {record.get('status', '—')}").classes("history-meta")
            with ui.element("div").classes("history-stats"):
                ui.label(f"{record.get('pages_processed', 0)} páginas").classes("history-stat")
                ui.label(f"{record.get('groups_translated', 0)} grupos").classes("history-stat")
                ui.label(_format_seconds(record.get("total_seconds"))).classes("history-stat")
                gate = record.get("quality_gate")
                ui.label(f"gate {'aprovado' if gate is True else 'pendente' if gate in ('', None) else 'reprovado'}").classes("history-stat")
            with ui.row().classes("result-actions"):
                self._artifact_button("PDF", record.get("pdf_path"), "picture_as_pdf")
                self._artifact_button("Pasta", record.get("output_folder"), "folder_open")
                self._artifact_button("Qualidade", record.get("quality_report_path"), "fact_check")
                self._artifact_button("Compare", record.get("compare_sheet_path"), "compare")
                self._artifact_button("Contexto", record.get("session_context_path"), "data_object")
                ui.button(
                    "Cache",
                    icon="replay",
                    on_click=lambda item=dict(record): self._reuse_record(item, force=False),
                ).props("flat dense no-caps")
                ui.button(
                    "Do zero",
                    icon="restart_alt",
                    on_click=lambda item=dict(record): self._reuse_record(item, force=True),
                ).props("flat dense no-caps")

    def _build_settings(self) -> None:
        status = env_status()
        with ui.element("div").classes("settings-grid"):
            with ui.card().classes("surface"):
                ui.label("AMBIENTE").classes("eyebrow")
                ui.label("Pronto para processar?").classes("section-title")
                self._setting("Arquivo .env", "Encontrado" if status["env_exists"] else "Ausente", status["env_exists"])
                self._setting(
                    "NVIDIA_API_KEY",
                    "Configurada" if status["nvidia_configured"] else "Não configurada",
                    status["nvidia_configured"],
                )
                self._setting("Segredo exibido", "Nunca", True)
                if not status["env_exists"] or not status["nvidia_configured"]:
                    ui.label("Configure o arquivo .env antes de processar.").classes("alert-box mt-4")
            with ui.card().classes("surface"):
                ui.label("PADRÕES").classes("eyebrow")
                ui.label("Preferências locais").classes("section-title")
                self._setting("Modo padrão", "fast", True)
                self._setting("Pasta de saída", "output/", True)
                self._setting("Contexto por padrão", "Ativado", True)
                self._setting("Cache por padrão", "Ativado", True)
                self._setting("Porta da interface", str(APP_PORT), True)

    def _setting(self, name: str, value: str, healthy: bool) -> None:
        with ui.element("div").classes("setting-row"):
            ui.label(name).classes("setting-name")
            ui.label(value).classes(f"setting-value {'ok' if healthy else 'bad'}")

    def _build_logs(self) -> None:
        with ui.card().classes("surface log-shell"):
            with ui.element("div").classes("log-toolbar"):
                with ui.column().classes("gap-0"):
                    ui.label("SAÍDA DO PIPELINE").classes("eyebrow")
                    ui.label("Logs em tempo real, com segredos mascarados.").classes("section-copy")
                with ui.row().classes("control-row"):
                    ui.button("Copiar", icon="content_copy", on_click=self._copy_logs).props("flat dense no-caps")
                    ui.button("Limpar visual", icon="delete_sweep", on_click=self._clear_logs).props("flat dense no-caps")
            self.refs["log"] = ui.log(max_lines=1800).classes("live-log w-full")

    def _on_url_change(self, event: Any) -> None:
        value = str(event.value or "")
        if not value.startswith(("http://", "https://", "[http")):
            return
        details = suggest_chapter_details(value)
        self.refs["chapter_name"].value = details["title"]
        self.refs["output"].value = details["slug"]

    def _sanitize_output_live(self, event: Any) -> None:
        value = str(event.value or "")
        if value and any(character in value for character in " /\\:;áéíóúãõçÁÉÍÓÚÃÕÇ"):
            self.refs["output"].value = sanitize_output_name(value)

    def _on_scope_change(self, event: Any) -> None:
        self.refs["partial_box"].set_visibility(event.value == "partial")

    def _set_max_images(self, value: int) -> None:
        self.refs["max_images"].value = value

    def _cache_changed(self, event: Any) -> None:
        if event.value:
            self.refs["force"].value = False

    def _force_changed(self, event: Any) -> None:
        if event.value:
            self.refs["cache"].value = False

    def _set_history_filter(self, value: str) -> None:
        self.history_filter = str(value or "")
        self._history_cards.refresh()

    def _set_history_status(self, value: str) -> None:
        self.history_status = str(value or "all")
        self._history_cards.refresh()

    async def _start_run(self) -> None:
        if self.process and self.process.returncode is None:
            ui.notify("Já existe uma tradução em andamento.", type="warning")
            return
        status = env_status()
        if not status["env_exists"] or not status["nvidia_configured"]:
            ui.notify("Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.", type="negative")
            return

        try:
            url = str(self.refs["url"].value or "").strip()
            output = sanitize_output_name(str(self.refs["output"].value or ""))
            full = self.refs["scope"].value == "full"
            max_images = None if full else int(self.refs["max_images"].value or 0)
            command = build_run_command(
                url=url,
                mode=str(self.refs["mode"].value),
                output=output,
                full=full,
                max_images=max_images,
                use_cache=bool(self.refs["cache"].value),
                force=bool(self.refs["force"].value),
                use_context=bool(self.refs["context"].value),
                open_output=False,
                python_executable=sys.executable,
            )
        except (ValueError, TypeError) as exc:
            ui.notify(str(exc), type="negative")
            return

        output_folder = (OUTPUT_ROOT / output).resolve()
        run_id = str(uuid.uuid4())
        self.active_record = {
            "id": run_id,
            "chapter_name": str(self.refs["chapter_name"].value or output),
            "slug": output,
            "url": url,
            "mode": str(self.refs["mode"].value),
            "scope": "full" if full else "partial",
            "max_images": max_images,
            "cache_mode": "force" if self.refs["force"].value else "cache",
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
        HISTORY.upsert(self.active_record)
        self.process_started = time.monotonic()
        self.progress = ProgressSnapshot(last_message="Preparando o pipeline…")
        self._clear_logs()
        self._set_status("running", "Rodando")
        self.refs["start"].disable()
        self.refs["cancel"].enable()
        self._update_progress_ui()
        self._append_log("$ " + " ".join(json.dumps(part) if " " in part else part for part in command))

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=creationflags,
            )
            ACTIVE_PROCESSES.add(self.process)
            assert self.process.stdout is not None
            while True:
                raw_line = await self.process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                self._append_log(line)
                self.progress = parse_progress_line(line, self.progress)
                self._update_progress_ui()
            return_code = await self.process.wait()
            await self._finish_run(return_code)
        except Exception as exc:
            self._append_log(f"Erro ao iniciar a execução: {mask_secrets(str(exc))}")
            await self._finish_run(1, error=str(exc))

    async def _cancel_run(self) -> None:
        if not self.process or self.process.returncode is not None:
            return
        self._append_log("Cancelamento solicitado; encerrando o processo com segurança…")
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=8)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        if self.active_record:
            self.active_record["status"] = "cancelled"
        await self._finish_run(self.process.returncode or -1, cancelled=True)

    async def _finish_run(self, return_code: int, *, error: str = "", cancelled: bool = False) -> None:
        elapsed = max(0.0, time.monotonic() - self.process_started)
        record = dict(self.active_record or {})
        output_folder = Path(record.get("output_folder") or OUTPUT_ROOT)
        report = load_json(output_folder / "timing_report.json")
        artifacts = find_output_artifacts(output_folder)
        quality = report.get("quality_validation") or {}
        finished = return_code == 0 and bool(artifacts.get("pdf_path")) and not cancelled
        record.update(
            {
                "finished_at": utc_now(),
                "total_seconds": float(report.get("total_seconds") or elapsed),
                "status": "finished" if finished else "cancelled" if cancelled else "error",
                **artifacts,
                "pages_processed": int(report.get("processed_images") or self.progress.pages or 0),
                "groups_translated": int(report.get("groups_translated") or self.progress.groups or 0),
                "sfx_preserved": int(report.get("groups_ignored_sfx_decorative") or 0),
                "errors": int(report.get("pages_with_error") or (0 if finished else 1)),
                "quality_gate": quality.get("passed", ""),
                "last_message": mask_secrets(error or self.progress.last_message),
            }
        )
        self.latest_record = HISTORY.upsert(record)
        self.active_record = None
        if self.process is not None:
            ACTIVE_PROCESSES.discard(self.process)
        self.process = None
        self.refs["start"].enable()
        self.refs["cancel"].disable()
        self._history_cards.refresh()

        if finished:
            self.progress.stage = "Finalizado"
            self.progress.percent = 1.0
            self._set_status("finished", "Finalizado")
            self.refs["result_title"].text = "PDF pronto para revisão."
            self.refs["result_copy"].text = (
                f"{record['pages_processed']} páginas · {record['groups_translated']} grupos · "
                f"{_format_seconds(record['total_seconds'])}"
            )
            ui.notify("Tradução finalizada.", type="positive")
            if self.refs["open_pdf"].value and record.get("pdf_path"):
                _open_path(record["pdf_path"])
            if self.refs["open_folder"].value:
                _open_path(record["output_folder"])
        else:
            self._set_status("error" if not cancelled else "ready", "Erro" if not cancelled else "Cancelado")
            self.refs["result_title"].text = "Execução interrompida." if cancelled else "A execução encontrou um erro."
            self.refs["result_copy"].text = record.get("last_message") or "Consulte os logs para detalhes."
            ui.notify("Execução cancelada." if cancelled else "A tradução falhou; veja os logs.", type="warning" if cancelled else "negative")
        self.refs["metric_pages"].text = str(record.get("pages_processed", 0))
        self.refs["metric_groups"].text = str(record.get("groups_translated", 0))
        self.refs["metric_errors"].text = str(record.get("errors", 0))
        gate = record.get("quality_gate")
        self.refs["metric_gate"].text = "Aprovado" if gate is True else "Pendente" if gate in (None, "") else "Reprovado"
        self.progress.pages = int(record.get("pages_processed", 0))
        self.progress.groups = int(record.get("groups_translated", 0))
        self.progress.errors = int(record.get("errors", 0))
        self._render_result_actions(record)
        self._update_progress_ui()

    def _update_progress_ui(self) -> None:
        self.refs["stage"].text = self.progress.stage
        self.refs["percent"].text = f"{round(self.progress.percent * 100)}%"
        self.refs["progress_bar"].value = self.progress.percent
        self.refs["last_message"].text = self.progress.last_message
        self.refs["metric_pages"].text = str(self.progress.pages)
        self.refs["metric_groups"].text = str(self.progress.groups)
        self.refs["metric_errors"].text = str(self.progress.errors)

    def _tick_elapsed(self) -> None:
        if self.process and self.process.returncode is None:
            self.refs["elapsed"].text = f"Tempo decorrido · {_format_seconds(time.monotonic() - self.process_started)}"

    def _append_log(self, line: str) -> None:
        safe_line = mask_secrets(line)
        self.log_lines = (self.log_lines + [safe_line])[-3000:]
        self.refs["log"].push(safe_line)

    def _copy_logs(self) -> None:
        payload = json.dumps("\n".join(self.log_lines))
        ui.run_javascript(f"navigator.clipboard.writeText({payload})")
        ui.notify("Logs copiados.", type="positive")

    def _clear_logs(self) -> None:
        self.log_lines.clear()
        if "log" in self.refs:
            self.refs["log"].clear()

    def _set_status(self, state: str, label: str) -> None:
        self.status = state
        self.refs["status"].text = label
        self.refs["status"].props(f'data-state="{state}"')

    def _render_result_actions(self, record: dict[str, Any]) -> None:
        container = self.refs["result_actions"]
        container.clear()
        with container:
            self._artifact_button("Abrir PDF", record.get("pdf_path"), "picture_as_pdf")
            self._artifact_button("Abrir pasta", record.get("output_folder"), "folder_open")
            self._artifact_button("Relatório", record.get("quality_report_path"), "fact_check")
            self._artifact_button("Compare", record.get("compare_sheet_path"), "compare")
            self._artifact_button("Contexto", record.get("session_context_path"), "data_object")

    def _artifact_button(self, label: str, path_value: Any, icon: str) -> None:
        path = Path(str(path_value or ""))
        if not path_value or not path.exists():
            return
        ui.button(
            label,
            icon=icon,
            on_click=lambda value=str(path.resolve()): _open_path(value),
        ).props("flat dense no-caps")

    def _reuse_record(self, record: dict[str, Any], *, force: bool) -> None:
        self.refs["url"].value = record.get("url", "")
        self.refs["chapter_name"].value = record.get("chapter_name", "")
        self.refs["output"].value = record.get("slug", "")
        self.refs["mode"].value = record.get("mode", "fast")
        partial = record.get("scope") == "partial" or bool(record.get("max_images"))
        self.refs["scope"].value = "partial" if partial else "full"
        self.refs["partial_box"].set_visibility(partial)
        if partial:
            self.refs["max_images"].value = int(record.get("max_images") or record.get("pages_processed") or 5)
        self.refs["force"].value = force
        self.refs["cache"].value = not force
        self.refs["tabs"].value = self.refs["new_tab"]
        ui.notify("Execução carregada. Revise e inicie quando quiser.", type="info")


@ui.page("/")
def index() -> None:
    RunnerPage()


@app.on_shutdown
async def shutdown_processes() -> None:
    for process in tuple(ACTIVE_PROCESSES):
        if process.returncode is None:
            process.terminate()
    if ACTIVE_PROCESSES:
        await asyncio.gather(
            *(process.wait() for process in tuple(ACTIVE_PROCESSES)),
            return_exceptions=True,
        )
    ACTIVE_PROCESSES.clear()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=APP_PORT,
        title="Tradutor.Ia · Local Runner",
        dark=True,
        language="pt-BR",
        show=True,
        reload=False,
        show_welcome_message=False,
    )
