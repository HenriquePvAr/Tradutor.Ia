"""Phase 2 (precedence): a shape-only high-contrast visual_class must not flatten
a structural story role into the dramatic ``display`` bucket.

Real gap (Absolute Regression - Episode 104, e.g. page 23): a plain/serif
narration box read as black ink on art was classed ``ink_display`` and rendered
with an impact/bahnschrift ``display`` font instead of its structural
``narration_box`` font.  ``ink_display``/``high_contrast_display`` only mean
"high contrast lettering", so a known semantic text_role keeps its own font;
the colour-coded ``dramatic_red_display``/``mystic_blue_system`` intents still win.
"""
from __future__ import annotations

import _test_bootstrap  # noqa: F401

import unittest

from ocr_balloon import _resolve_font_role


class ShapeOnlyVisualClassYieldsToSemanticRole(unittest.TestCase):
    def test_narration_ink_display_keeps_narration_box(self):
        role, source = _resolve_font_role("narration", "ink_display", "tall_display")
        self.assertEqual(role, "narration_box")
        self.assertEqual(source, "semantic_role_over_shape_visual_class")

    def test_caption_high_contrast_keeps_story_caption(self):
        role, _ = _resolve_font_role("caption", "high_contrast_display", "")
        self.assertEqual(role, "story_caption")

    def test_thought_system_location_keep_their_roles(self):
        self.assertEqual(_resolve_font_role("thought", "ink_display", "")[0], "thought_dialogue")
        self.assertEqual(_resolve_font_role("system", "ink_display", "")[0], "system_text")
        self.assertEqual(_resolve_font_role("location", "ink_display", "")[0], "location_label")


class StrongIntentAndSpeechAreUnchanged(unittest.TestCase):
    def test_speech_ink_display_still_display(self):
        # Dialogue is not a structural story role here; ink display stays display.
        role, source = _resolve_font_role("speech", "ink_display", "tall_display")
        self.assertEqual(role, "display")
        self.assertEqual(source, "visual_class_mapping")

    def test_colour_coded_intent_still_wins_over_semantic_role(self):
        self.assertEqual(_resolve_font_role("narration", "dramatic_red_display", "")[0],
                         "dramatic_display")
        self.assertEqual(_resolve_font_role("narration", "mystic_blue_system", "")[0],
                         "system_text")

    def test_plain_narration_without_visual_class_is_narration_box(self):
        role, source = _resolve_font_role("narration", "generic", "")
        self.assertEqual(role, "narration_box")
        self.assertEqual(source, "text_role_mapping")

    def test_unknown_defaults_to_comic_not_generic_sans(self):
        role, source = _resolve_font_role("unknown", "generic", "")
        self.assertEqual(role, "balloon_dialogue")
        self.assertEqual(source, "generic_fallback")


if __name__ == "__main__":
    unittest.main()
