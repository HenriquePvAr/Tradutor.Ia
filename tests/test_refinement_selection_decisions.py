import _test_bootstrap  # noqa: F401
import hashlib
import tempfile
import unittest
from pathlib import Path

from refinement_selection_decisions import RefinementSelectionStore, _hash


def result(owner="owner", text="Texto atual.", natural="Texto natural."):
    request = {
        "owner": owner, "job_id": "job", "run_id": "run", "revision_id": "revision",
        "page_id": "page", "region_id": "region", "source_text": "Source.",
        "request_hash": "request", "schema_version": "2",
        "prompt_contract_version": "natural-ptbr-refinement-v2",
        "response_schema_version": "natural-ptbr-response-v2",
    }
    payload = {"natural_ptbr": natural}
    return {"request": request, "status": "valid_suggestion", "result": payload,
            "result_hash": _hash(payload)}


def intent(action="select_option", option="natural", **extra):
    value = result()
    data = {
        "owner": "owner", "job_id": "job", "run_id": "run", "revision_id": "revision",
        "page_id": "page", "region_id": "region",
        "source_hash": hashlib.sha256(b"Source.").hexdigest(),
        "current_translation_before": "Texto atual.", "previous_decision_id": "previous",
        "selected_action": action, "selected_option": option, "result": value,
        "reviewer": "user_delegated_via_codex",
        "authorization": "explicit_user_authorization",
        "authorization_scope": "block_6g1d", "authorization_timestamp": "2026-01-01T00:00:00Z",
        "reason": "Decisão humana.", **extra,
    }
    return data


class SelectionDecisionTests(unittest.TestCase):
    def store(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = RefinementSelectionStore(Path(folder.name) / "jobs.sqlite3")
        self.addCleanup(store.close)
        return store

    def test_keep_current_is_append_only_and_does_not_change_effective_text(self):
        store = self.store()
        saved = store.confirm_batch(
            [intent("keep_current", "current")], plan_hash="plan")[0]
        self.assertEqual(saved["effective_translation_after"], "Texto atual.")
        self.assertEqual(saved["selected_action"], "keep_current")

    def test_select_natural_reads_persisted_result_without_changing_case(self):
        store = self.store()
        value = intent()
        value["result"]["result"]["natural_ptbr"] = "Forma Natural."
        saved = store.confirm_batch([value], plan_hash="plan")[0]
        self.assertEqual(saved["effective_translation_after"], "Forma Natural.")

    def test_second_confirmation_is_idempotent(self):
        store = self.store()
        value = intent()
        first = store.confirm_batch([value], plan_hash="plan")[0]
        second = store.confirm_batch([value], plan_hash="plan")[0]
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(len(store.list_for("job", "run", "revision", owner="owner")), 1)

    def test_invalid_result_owner_option_and_source_fail_closed(self):
        store = self.store()
        cases = [
            intent(result={**result(), "status": "provider_response_invalid"}),
            intent(owner="other"),
            intent(option="compact"),
            intent(source_hash="wrong"),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    store.confirm_batch([value], plan_hash="plan")

    def test_batch_failure_is_atomic(self):
        store = self.store()
        first = intent(region_id="one")
        first["result"]["request"]["region_id"] = "one"
        second = intent(region_id="two", source_hash="wrong")
        second["result"]["request"]["region_id"] = "two"
        with self.assertRaises(ValueError):
            store.confirm_batch([first, second], plan_hash="plan")
        self.assertEqual(store.list_for("job", "run", "revision", owner="owner"), [])

    def test_concurrent_different_selection_is_blocked(self):
        store = self.store()
        store.confirm_batch([intent()], plan_hash="one")
        changed = intent("keep_current", "current")
        with self.assertRaisesRegex(ValueError, "selection_concurrent_state_changed"):
            store.confirm_batch([changed], plan_hash="two")

    def test_no_provider_exists_in_selection_module(self):
        import refinement_selection_decisions as module
        self.assertFalse(hasattr(module, "TranslatorNvidiaBatch"))


if __name__ == "__main__":
    unittest.main()
