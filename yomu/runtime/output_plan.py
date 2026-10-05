"""Output Plan -- the single source of truth for *what a job is asked to produce*.

Before this module the selective behaviour was four independent, ad-hoc booleans
(``download_only``, ``translation_enabled``, ``output_profile``, ``output_format``)
resolved by hand in ``ui_bridge._normalize_payload``. This centralizes the decision:
a user picks a *preset* (or a custom set of operations), and ``normalize_output_plan``
applies the dependency rules and maps the result back onto those existing knobs, so all
the already-proven plumbing (provider gate, YK commit, profile artifacts) is reused, not
rewritten.

CLEAN is modelled as one abstract operation with **no provider branching**. The internal
Clean orchestrator tries the local strategy first and may use the safe Gemini adapter only
for an explicitly eligible hard reconstruction. OutputPlan never names a provider.
"""
from __future__ import annotations

from dataclasses import dataclass

from yomu.runtime.artifact_contract import OutputProfile


class Operation:
    """A unit of user-visible work. Order here is pipeline order (for display)."""
    TRANSLATE = "translate"   # OCR source text -> PT-BR (the only provider/YK cost)
    CLEAN = "clean"           # erase source text, reconstruct background
    TYPESET = "typeset"       # burn translated text onto the cleaned page
    PDF = "pdf"               # bundle pages into the final PDF
    PSD = "psd"               # editable native Type-Layer PSD (professional)
    QC = "qc"                 # quality report + linguistic review manifest


# Separate namespace consumed by workspace authorization and the worker.
EXECUTION_OPERATIONS = (
    "analyze_metadata", "download_assets", "run_ocr", "translate",
    "reconstruct", "generate_pdf",
)


ALL_OPERATIONS = (
    Operation.TRANSLATE, Operation.CLEAN, Operation.TYPESET,
    Operation.PDF, Operation.PSD, Operation.QC,
)

# Dependency rules: an operation on the left pulls in the operations on the right.
# Resolved transitively by ``normalize_output_plan``.
_REQUIRES = {
    Operation.TYPESET: (Operation.TRANSLATE,),            # can't typeset untranslated text
    Operation.PSD: (Operation.TRANSLATE, Operation.TYPESET),  # type layers carry the translation
    Operation.QC: (Operation.TRANSLATE, Operation.CLEAN, Operation.TYPESET),
}
# A page producer is required for a PDF; without one, PDF is dropped (nothing to bundle).
_PAGE_PRODUCERS = frozenset({Operation.TYPESET, Operation.CLEAN})
# Operations that require the professional profile.
_PROFESSIONAL_ONLY = frozenset({Operation.PSD, Operation.QC})


@dataclass(frozen=True)
class OutputPlan:
    preset: str
    operations: frozenset            # normalized, dependency-closed
    requested_operations: frozenset  # exactly what the caller asked for (pre-closure)
    executable: bool                 # runnable by the current pipeline HEAD
    not_executable_reason: str = ""

    def has(self, op: str) -> bool:
        return op in self.operations

    # --- mapping onto the existing, already-proven config knobs -------------------
    @property
    def translation_enabled(self) -> bool:
        return Operation.TRANSLATE in self.operations

    @property
    def download_only(self) -> bool:
        return not self.operations

    @property
    def output_profile(self) -> str:
        if self.operations & _PROFESSIONAL_ONLY:
            return OutputProfile.PROFESSIONAL_QC
        return OutputProfile.AUTOMATIC

    @property
    def translate_only(self) -> bool:
        """Translation is the only deliverable: no clean, no typeset, no page output."""
        return (Operation.TRANSLATE in self.operations
                and not (self.operations & {Operation.CLEAN, Operation.TYPESET,
                                            Operation.PDF, Operation.PSD}))

    @property
    def output_format(self) -> str:
        if Operation.PSD in self.operations:
            return "psd"
        if Operation.PDF in self.operations:
            return "pdf"
        if self.translate_only:
            return "translation"  # textual deliverable, no image/PDF
        return "png"  # image-only deliverable (e.g. Apenas Clean)

    @property
    def execution_operations(self) -> frozenset:
        """Map output intent to the internal operations that must be authorized."""
        if self.download_only:
            return frozenset({"analyze_metadata", "download_assets"})
        execution = {"analyze_metadata", "download_assets"}
        if self.operations:
            execution.add("run_ocr")
        if self.operations & {Operation.CLEAN, Operation.TYPESET, Operation.PSD, Operation.QC}:
            execution.add("reconstruct")
        if Operation.TRANSLATE in self.operations:
            execution.add("translate")
        if Operation.PDF in self.operations:
            execution.add("generate_pdf")
        return frozenset(execution)

    def to_config(self) -> dict:
        """The subset of job-configuration knobs this plan drives. Merged into the
        job config by the caller; every key is one the pipeline already consumes."""
        return {
            "output_plan": self.preset,
            "requested_operations": sorted(self.requested_operations),
            "effective_operations": sorted(self.operations),
            "execution_operations": sorted(self.execution_operations),
            "translation_enabled": self.translation_enabled,
            "download_only": self.download_only,
            "output_profile": self.output_profile,
            "output_format": self.output_format,
        }


# Preset name -> (requested operations, PT-BR label, description). These are the
# tester-facing choices. ``normalize_output_plan`` closes dependencies and decides
# executability; presets below are written as the user intent, not the closure.
PRESETS: dict[str, tuple[frozenset, str, str]] = {
    "traducao_pronta": (
        frozenset({Operation.TRANSLATE, Operation.CLEAN, Operation.TYPESET, Operation.PDF}),
        "Tradução pronta",
        "Traduz, limpa, aplica o texto e gera o PDF final.",
    ),
    "apenas_clean": (
        frozenset({Operation.CLEAN}),
        "Apenas Clean",
        "Remove o texto original e entrega as páginas limpas. Não traduz (0 créditos).",
    ),
    "apenas_traducao": (
        frozenset({Operation.TRANSLATE}),
        "Apenas tradução",
        "Entrega apenas o texto traduzido, sem limpar nem remontar as páginas.",
    ),
    "traducao_clean": (
        frozenset({Operation.TRANSLATE, Operation.CLEAN}),
        "Tradução + Clean",
        "Traduz e limpa as páginas, sem aplicar o texto (para diagramação manual).",
    ),
    "profissional": (
        frozenset({Operation.TRANSLATE, Operation.CLEAN, Operation.TYPESET,
                   Operation.PDF, Operation.PSD, Operation.QC}),
        # Product label is "Completo"; the preset identifier stays "profissional" and the
        # backend profile stays "professional_qc" (semantics unchanged).
        "Completo",
        "Gera todos os arquivos: PDF, imagens limpas, PSD editável e revisão.",
    ),
    "download": (
        frozenset(),
        "Apenas download",
        "Baixa e valida as páginas originais, sem OCR, tradução, limpeza ou PDF.",
    ),
    "personalizado": (
        frozenset(),  # filled from the caller's explicit operation set
        "Personalizado",
        "Você escolhe exatamente quais etapas gerar.",
    ),
}

DEFAULT_PRESET = "traducao_pronta"


def _close_dependencies(requested: frozenset) -> frozenset:
    ops = set(requested)
    changed = True
    while changed:
        changed = False
        for op in list(ops):
            for dep in _REQUIRES.get(op, ()):
                if dep not in ops:
                    ops.add(dep)
                    changed = True
    # PDF with nothing to bundle is meaningless -> drop it.
    if Operation.PDF in ops and not (ops & _PAGE_PRODUCERS):
        ops.discard(Operation.PDF)
    return frozenset(ops)


def _executability(ops: frozenset) -> tuple[bool, str]:
    """Which operation sets the pipeline can run today.

    All five product modes are executable:
      - clean-only and translate+clean via the clean/typeset decoupling;
      - translate-only via the skip-render path that emits the translation manifest and a
        translation-based completion/YK contract;
      - automatic and professional unchanged.
    An empty set (download) is handled before the pipeline. There is no non-executable
    combination left; the function remains the single gate for any future one."""
    return True, ""


def normalize_output_plan(preset: str | None = None, *,
                          operations: "frozenset | set | list | tuple | None" = None) -> OutputPlan:
    """Resolve a preset (or a custom operation set) into a dependency-closed OutputPlan.

    ``operations`` is honoured for the ``personalizado`` preset (or when ``preset`` is
    omitted). Unknown presets fall back to the safe default. Invalid operation names are
    ignored rather than raising, so a stale UI field can never hard-fail a job.
    """
    name = str(preset or "").strip().lower()
    if operations is not None and name in ("", "personalizado", "custom"):
        requested = frozenset(o for o in operations if o in ALL_OPERATIONS)
        name = "personalizado"
    elif name in PRESETS and name != "personalizado":
        requested = PRESETS[name][0]
    elif name == "personalizado":
        requested = frozenset()  # custom with nothing chosen == download
    else:
        name = DEFAULT_PRESET
        requested = PRESETS[DEFAULT_PRESET][0]
    ops = _close_dependencies(requested)
    executable, reason = _executability(ops)
    return OutputPlan(
        preset=name,
        operations=ops,
        requested_operations=requested,
        executable=executable,
        not_executable_reason=reason,
    )


def plan_artifact_types(operations) -> tuple:
    """User-facing artifact types a set of operations is expected to produce.

    Used to declare the manifest's *requested* artifacts from the plan instead of the
    profile, so a clean-only run does not falsely owe a translated image / PDF. Returns
    contract artifact-type strings (see artifact_contract.ArtifactType).
    """
    ops = set(operations or ())
    arts = []
    # Translate-only: the deliverable is the persisted translation data, not an image.
    if (Operation.TRANSLATE in ops
            and not (ops & {Operation.CLEAN, Operation.TYPESET, Operation.PDF, Operation.PSD})):
        return ("translation_manifest",)
    if Operation.CLEAN in ops:
        arts.append("clean_image")
    if Operation.TYPESET in ops:
        arts.append("translated_image")
    if Operation.PDF in ops:
        arts.append("final_pdf")
    if Operation.PSD in ops:
        arts.append("editable_psd")
    if Operation.QC in ops:
        arts.extend(("quality_report", "review_manifest"))
    return tuple(arts)


def plan_from_config(config: dict | None) -> OutputPlan:
    """Reconstruct the plan a legacy/live job ran under, from its stored knobs.

    Used by the finish screen / manifest so a job created before this contract still
    reports a coherent plan. Prefers an explicit stored preset, else infers from knobs.
    """
    config = config if isinstance(config, dict) else {}
    stored = str(config.get("output_plan") or "").strip().lower()
    if stored in PRESETS:
        if stored == "personalizado":
            return normalize_output_plan(stored, operations=config.get("effective_operations"))
        return normalize_output_plan(stored)
    # Infer from the historical knobs.
    if bool(config.get("download_only")):
        return normalize_output_plan("download")
    ops = set()
    if config.get("translation_enabled") is True:
        ops.update({Operation.TRANSLATE, Operation.TYPESET})
    fmt = str(config.get("output_format") or "pdf").lower()
    profile = str(config.get("output_profile") or OutputProfile.AUTOMATIC).lower()
    if profile == OutputProfile.PROFESSIONAL_QC:
        ops.update({Operation.CLEAN, Operation.PSD, Operation.QC})
    if fmt == "pdf" and ops:
        ops.add(Operation.PDF)
    return normalize_output_plan("personalizado", operations=ops)


def demo() -> None:
    """Runnable self-check of the dependency rules and knob mapping."""
    # Dependency closure: typeset pulls translate; qc/psd pull the full set.
    assert normalize_output_plan("profissional").operations == frozenset(ALL_OPERATIONS)
    tp = normalize_output_plan("traducao_pronta")
    assert tp.operations == frozenset({Operation.TRANSLATE, Operation.CLEAN,
                                       Operation.TYPESET, Operation.PDF})
    assert tp.output_profile == OutputProfile.AUTOMATIC and tp.output_format == "pdf"
    assert tp.translation_enabled and not tp.download_only

    # Apenas Clean: no translation -> 0 provider/0 YK, image deliverable, executable today.
    clean = normalize_output_plan("apenas_clean")
    assert clean.operations == frozenset({Operation.CLEAN})
    assert clean.translation_enabled is False
    assert clean.output_format == "png"
    assert clean.executable is True

    # Download: empty ops -> download_only.
    dl = normalize_output_plan("download")
    assert dl.download_only is True and not dl.operations

    # translate+clean and translate-only are both executable now.
    assert normalize_output_plan("traducao_clean").executable is True
    ato = normalize_output_plan("apenas_traducao")
    assert ato.executable is True
    assert ato.translate_only is True
    assert ato.output_format == "translation"
    assert plan_artifact_types(ato.operations) == ("translation_manifest",)

    # Profissional implies professional profile + psd format.
    pro = normalize_output_plan("profissional")
    assert pro.output_profile == OutputProfile.PROFESSIONAL_QC
    assert pro.output_format == "psd"

    # Custom: typeset alone closes to translate+typeset and is executable.
    custom = normalize_output_plan("personalizado", operations={Operation.TYPESET})
    assert custom.operations == frozenset({Operation.TRANSLATE, Operation.TYPESET})
    assert custom.executable is True

    # Custom PDF with no page producer drops PDF.
    only_pdf = normalize_output_plan("personalizado", operations={Operation.PDF})
    assert Operation.PDF not in only_pdf.operations

    # Round-trip from legacy knobs.
    rt = plan_from_config({"download_only": True})
    assert rt.download_only is True
    print("output_plan demo: OK")


if __name__ == "__main__":
    demo()
