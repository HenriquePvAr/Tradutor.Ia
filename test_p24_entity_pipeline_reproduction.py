from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import unittest

from ocr_balloon import TextGroup, group_proper_name_spans, validate_translation_text


class P24EntityPipelineReproductionTests(unittest.TestCase):
    def test_declared_alias_reaches_final_validator(self):
        group = TextGroup(
            group_id="synthetic",
            text="AURORA... BUT PEOPLE CALL ME RORY.",
            detected_proper_names=["AURORA", "RORY"],
        )
        spans = group_proper_name_spans(group)
        self.assertIn("RORY", spans)
        ok, reason = validate_translation_text(
            group.text,
            "AURORA... MAS AS PESSOAS ME CHAMAM DE AMANHECER.",
            required_name_spans=spans,
        )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("proper_name_altered:"))

    def test_common_word_without_entity_authority_remains_translatable(self):
        group = TextGroup(group_id="ordinary", text="PEOPLE CALL ME SUNNY.")
        spans = group_proper_name_spans(group)
        self.assertEqual(spans, [])


if __name__ == "__main__":
    unittest.main()
