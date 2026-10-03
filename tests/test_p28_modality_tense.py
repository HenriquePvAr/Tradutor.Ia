from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

from semantic_fidelity import MODALITY_TENSE_CHANGED, VERIFY, evaluate_local_fidelity


class P28ModalityTenseTests(unittest.TestCase):
    def test_prospective_obligation_cannot_become_completed_past(self):
        finding = evaluate_local_fidelity(
            "You might have to leave.", "Talvez você tenha tido que sair."
        )
        self.assertEqual(finding.status, VERIFY)
        self.assertEqual(finding.primary_reason, MODALITY_TENSE_CHANGED)

    def test_natural_modal_paraphrase_remains_clean(self):
        finding = evaluate_local_fidelity(
            "You might have to leave.", "Pode ser que você precise ir embora."
        )
        self.assertNotEqual(finding.primary_reason, MODALITY_TENSE_CHANGED)


if __name__ == "__main__":
    unittest.main()
