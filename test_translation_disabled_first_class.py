def test_disabled_translation_never_enters_legacy_factory(monkeypatch, capsys):
    import benchmark_pipeline

    def fail_factory(*args, **kwargs):  # pragma: no cover - assertion guard
        raise AssertionError("translator factory must not run when disabled")

    monkeypatch.setattr(benchmark_pipeline, "get_translator", fail_factory)
    translator, ocr_lang = benchmark_pipeline._resolve_translation_runtime(
        "deepl", translation_enabled=False
    )
    assert translator is None
    assert ocr_lang == "eng"
    assert "provider_init=0" in capsys.readouterr().out


def test_enabled_translation_keeps_explicit_provider_contract(monkeypatch):
    import benchmark_pipeline

    sentinel = object()
    monkeypatch.setattr(benchmark_pipeline, "_build_translation_provider", lambda *a, **k: sentinel)
    monkeypatch.setattr(benchmark_pipeline, "get_translator", lambda *a, **k: ("translator", "eng"))
    translator, ocr_lang = benchmark_pipeline._resolve_translation_runtime(
        "yomu_backend", translation_enabled=True
    )
    assert (translator, ocr_lang) == ("translator", "eng")


def test_enabled_translation_uses_backend_bridge_even_with_legacy_deepl_label(monkeypatch):
    import benchmark_pipeline

    sentinel = object()
    calls = []
    monkeypatch.setattr(benchmark_pipeline, "_build_translation_provider", lambda provider, **kw: (calls.append(provider) or sentinel))
    monkeypatch.setattr(benchmark_pipeline, "get_translator", lambda *a, **k: (k["translation_provider"], "eng"))
    translator, _ = benchmark_pipeline._resolve_translation_runtime("deepl", translation_enabled=True)
    assert translator is sentinel
    assert calls == ["yomu_backend"]


def test_disabled_translation_has_first_class_provider_provenance():
    import benchmark_pipeline

    provenance = benchmark_pipeline.resolve_provider_provenance(
        None, "deepl", translation_enabled=False
    )
    assert provenance["provider_source"] == "disabled"
    assert provenance["provider_disabled"] is True
    assert provenance["provider_effective"] == ""
    assert provenance["provider_mismatch"] is False


def test_pipeline_spawn_uses_hidden_console_helper():
    from pathlib import Path

    source = (Path(__file__).with_name("job_runner.py")).read_text(encoding="utf-8")
    assert "build_background_process_options" in source
    assert "PIPELINE_SPAWN_BEGIN" in source
    assert "shell=True" not in source


def test_background_spawn_contract_is_shell_free_and_captures_streams():
    from process_options import build_background_process_options

    options = build_background_process_options(stdout=-1, stderr=-2)
    assert options["shell"] is False
    assert options["stdout"] == -1
    assert options["stderr"] == -2
