from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from run_webtoon import _chapter_slug, _clean_url, _resolve_output_folder
from session_context import SessionContextStore
from translator_nvidia import TranslatorNvidiaBatch


class RunWebtoonTests(unittest.TestCase):
    def test_chapter_slug_uses_title_and_episode(self):
        url = "https://m.webtoons.com/en/drama/lookism/ep-50/viewer?title_no=1049"
        self.assertEqual(_chapter_slug(url), "lookism_ep-50")

    def test_markdown_url_is_cleaned(self):
        wrapped = "[https://example.com/chapter/viewer](https://example.com/chapter/viewer)"
        self.assertEqual(_clean_url(wrapped), "https://example.com/chapter/viewer")

    def test_relative_output_is_kept_under_output(self):
        path = _resolve_output_folder("meu_capitulo", "https://example.com/a/b/viewer")
        self.assertEqual(path.name, "meu_capitulo")
        self.assertEqual(path.parent.name, "output")

    def test_session_context_is_saved_and_added_to_prompt(self):
        groups = [
            SimpleNamespace(classification="speech", text="JAY, ARE YOU HERE?", translation="JAY, VOCE ESTA AQUI?", detected_proper_names=["JAY"]),
            SimpleNamespace(classification="speech", text="JAY!", translation="JAY!", detected_proper_names=["JAY"]),
            SimpleNamespace(classification="speech", text="LOGAN IS WAITING", translation="LOGAN ESTA ESPERANDO", detected_proper_names=["LOGAN"]),
            SimpleNamespace(classification="narration", text="LOGAN WAS STILL WAITING", translation="LOGAN AINDA ESPERAVA", detected_proper_names=["LOGAN"]),
            SimpleNamespace(classification="speech", text="THIS IS FROM JAY", translation="ISTO E DO JAY", detected_proper_names=["JAY"]),
            SimpleNamespace(classification="narration", text="THIS CAME FROM JAY", translation="ISTO VEIO DO JAY", detected_proper_names=["JAY"]),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session_context.json"
            store = SessionContextStore(path, "https://example.com/chapter")
            data = store.prepare(groups)
            self.assertTrue(path.is_file())
            names = {item["text"].upper() for item in data["proper_names"]}
            self.assertIn("JAY", names)
            self.assertIn("LOGAN", names)
            self.assertNotIn("WAITING", names)
            self.assertNotIn("THIS", names)
            self.assertNotIn("FROM", names)

            store.record_translations(groups)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(persisted["translations_used"]), 3)

            store.prepare([])
            persisted_after_cache = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                len(persisted_after_cache["translations_used"]),
                len(persisted["translations_used"]),
            )
            self.assertEqual(
                len(persisted_after_cache["possible_characters"]),
                len(persisted["possible_characters"]),
            )

            translator = TranslatorNvidiaBatch(api_key="")
            translator.set_session_context(store)
            prompt = translator._system_prompt()
            self.assertIn("Contexto temporario", prompt)
            self.assertIn("Jay", prompt)
            self.assertTrue(translator.stats["context_enabled"])

    def test_repeated_dialogue_tokens_are_not_promoted_to_proper_names(self):
        groups = [
            SimpleNamespace(classification="speech", text="ECHO, COME BACK!", translation="ECHO, VOLTE!", detected_proper_names=[]),
            SimpleNamespace(classification="speech", text="ECHO, PLEASE!", translation="ECHO, POR FAVOR!", detected_proper_names=[]),
        ]
        with tempfile.TemporaryDirectory() as folder:
            store = SessionContextStore(Path(folder) / "session_context.json", "https://example.com/chapter")
            data = store.prepare(groups)
        self.assertEqual(data["proper_names"], [])


if __name__ == "__main__":
    unittest.main()
