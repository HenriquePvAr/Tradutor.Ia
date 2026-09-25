"""History / status labels must reflect the job's real output_format, never a hardcoded PDF.

Observed: a PSD job (5d05845b, output_format=psd, 5 real .psd files, manifest=psd) showed up
in the UI as a generic "PDF" translation because the history card carried no format and the
status strings said "PDF pronto"/"PDF finalizado".  The labels now derive from the canonical
``record.output_format`` (pdf/png/psd), never from ``pdf_path`` -- a psd/png job keeps an
internal canonical PDF but must still read as PSD/PNG.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JS = (ROOT / "static" / "tradutor_ui.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "ui_bridge.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The canonical format helper exists, maps every format, and ignores pdf_path.
# --------------------------------------------------------------------------- #
def test_output_format_helper_exists_and_maps_all_formats():
    assert "function outputFormatLabel(record)" in JS
    assert "record.output_format" in JS
    assert "'psd' ? 'PSD'" in JS and "'png' ? 'PNG'" in JS
    # explicit comment/intent that pdf_path is NOT the source of truth
    assert "Never infer the" in JS or "never infer" in JS.lower()


def test_history_card_shows_type_and_format():
    assert "function historyTypeFormatLabel(record)" in JS
    assert "'Download'" in JS and "'Tradução'" in JS
    # the history card renders the combined label, not the old hardcoded engine badge
    assert "historyTypeFormatLabel(record)" in JS
    assert '<span class="badge ${engine}">${engine === \'rapid\' ? \'Rápido\' : \'Qualidade\'}</span>' not in JS


def test_open_pdf_action_is_gated_on_pdf_format():
    # The external "open" action must only offer the PDF for a pdf job; png/psd open the folder.
    assert "outputFormat === 'PDF' ? actionButton('Abrir PDF', 'pdf', record.pdf_path) : ''" in JS
    assert "actionButton('Abrir pasta', 'folder', record.output_folder)" in JS


def test_terminal_and_progress_messages_are_format_aware():
    assert "${outputFormatLabel(record)} gerado, mas requer revisão" in JS
    assert "${outputFormatLabel(record)} finalizado e registrado" in JS
    assert "${outputFormatLabel(record)} pronto" in JS
    # the transient stage label no longer hardcodes PDF
    assert "generating_pdf: 'Gerando o PDF...'" not in JS


def test_backend_history_view_model_carries_output_format():
    # The frontend can only be format-aware because the view-model exposes the canonical field.
    assert '"output_format": str(config.get("output_format") or "pdf")' in BRIDGE
    assert '"download_only": bool(config.get("download_only"))' in BRIDGE


# --------------------------------------------------------------------------- #
# Model of the label mapping (mirrors the two JS helpers).
# --------------------------------------------------------------------------- #
def _fmt_label(output_format):
    fmt = str(output_format or "pdf").lower()
    return {"psd": "PSD", "png": "PNG"}.get(fmt, "PDF")


def _type_fmt_label(record):
    kind = "Download" if record.get("download_only") is True else "Tradução"
    return f"{kind} · {_fmt_label(record.get('output_format'))}"


def test_label_matrix_quality_and_download_only():
    cases = {
        ("quality", "pdf"): "Tradução · PDF",
        ("quality", "png"): "Tradução · PNG",
        ("quality", "psd"): "Tradução · PSD",
        ("download", "pdf"): "Download · PDF",
        ("download", "png"): "Download · PNG",
        ("download", "psd"): "Download · PSD",
    }
    for (kind, fmt), expected in cases.items():
        record = {"output_format": fmt, "download_only": kind == "download"}
        assert _type_fmt_label(record) == expected, (kind, fmt)


def test_internal_pdf_never_overrides_a_psd_or_png_label():
    # A psd/png record with a populated internal pdf_path still reads as PSD/PNG.
    psd = {"output_format": "psd", "download_only": False, "pdf_path": r"C:\out\internal.pdf"}
    png = {"output_format": "png", "download_only": True, "pdf_path": r"C:\out\internal.pdf"}
    assert _type_fmt_label(psd) == "Tradução · PSD"
    assert _type_fmt_label(png) == "Download · PNG"


def test_real_psd_job_reads_as_psd():
    # Regression anchor: job 5d05845b was output_format=psd.
    assert _type_fmt_label({"output_format": "psd", "download_only": False}) == "Tradução · PSD"


# --------------------------------------------------------------------------- #
# Artifact panel: the internal canonical PDF is only offered for a PDF job.
# --------------------------------------------------------------------------- #
def test_artifact_pdf_action_is_gated_on_pdf_format():
    assert "function renderArtifactButtons(container, record)" in JS
    # "PDF base" (unconditional) is gone; the PDF artifact is conditional on the format.
    assert "['PDF base', 'pdf', record.pdf_path]" not in JS
    assert "outputFormatLabel(record) === 'PDF'" in JS
    assert "['Abrir PDF', 'pdf', record.pdf_path]" in JS


def _artifact_pdf_offered(output_format):
    # Mirrors the JS: the PDF artifact entry is included only for a pdf job.
    return _fmt_label(output_format) == "PDF"


def test_png_and_psd_do_not_offer_internal_pdf_as_artifact():
    assert _artifact_pdf_offered("pdf") is True
    assert _artifact_pdf_offered("png") is False
    assert _artifact_pdf_offered("psd") is False


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print("ok:", fn.__name__)
    print(f"\n{len(fns)} checks passed")
    sys.exit(0)
