"""Translate-only completion + YK contract (finalization layer).

Proves that a translate-only job's validity -- and therefore YK consumption -- is tied to the
persisted translation (yk_reservation.translation_ready), NOT to any visual artifact. No real
provider/YK/Supabase: translation_ready is mocked to represent "translation persisted or not".
"""
import test_support._test_bootstrap as _test_bootstrap  # noqa: F401

from pathlib import Path
from unittest import mock

import yomu.runtime.job_runner as job_runner


def _cfg(plan):
    from yomu.runtime.output_plan import normalize_output_plan
    return normalize_output_plan(plan).to_config()


def test_config_detects_translate_only_and_not_other_modes():
    assert job_runner._config_is_translate_only(_cfg("apenas_traducao")) is True
    for other in ("apenas_clean", "traducao_clean", "traducao_pronta", "profissional"):
        assert job_runner._config_is_translate_only(_cfg(other)) is False
    assert job_runner._config_is_translate_only({}) is False  # legacy


def test_translation_contract_valid_only_when_translation_persisted(tmp_path):
    out = tmp_path / "job"
    out.mkdir()
    with mock.patch.object(job_runner, "_translation_output_contract", wraps=job_runner._translation_output_contract):
        with mock.patch("yomu.runtime.yk_reservation.translation_ready", return_value=True), \
             mock.patch("yomu.runtime.yk_reservation.read_reservation", return_value={"job_id": "j"}):
            ok, reason = job_runner._translation_output_contract(out)
            assert ok is True and reason == "translation_persisted"
        with mock.patch("yomu.runtime.yk_reservation.translation_ready", return_value=False), \
             mock.patch("yomu.runtime.yk_reservation.read_reservation", return_value={"job_id": "j"}):
            ok, reason = job_runner._translation_output_contract(out)
            assert ok is False and reason == "translation_not_persisted"


def test_translate_only_yk_validity_follows_translation_persistence(tmp_path):
    """The economic signal: output_valid (=> YK consumed) is True iff translation persisted,
    regardless of the absence of pdf/png/psd."""
    out = tmp_path / "job"
    out.mkdir()
    # persisted -> valid -> YK may consume
    with mock.patch("yomu.runtime.yk_reservation.translation_ready", return_value=True), \
         mock.patch("yomu.runtime.yk_reservation.read_reservation", return_value={"job_id": "j"}):
        assert job_runner._translation_output_contract(out)[0] is True
    # not persisted (e.g. provider failed) -> invalid -> YK released
    with mock.patch("yomu.runtime.yk_reservation.translation_ready", return_value=False), \
         mock.patch("yomu.runtime.yk_reservation.read_reservation", return_value={"job_id": "j"}):
        assert job_runner._translation_output_contract(out)[0] is False


def test_translation_contract_never_requires_visual_artifact(tmp_path):
    """An empty output dir (no pdf/png/psd) with a persisted translation is still valid."""
    out = tmp_path / "job"
    out.mkdir()
    assert not any(out.iterdir())  # no visual artifacts at all
    with mock.patch("yomu.runtime.yk_reservation.translation_ready", return_value=True), \
         mock.patch("yomu.runtime.yk_reservation.read_reservation", return_value={"job_id": "j"}):
        ok, _ = job_runner._translation_output_contract(out)
    assert ok is True

