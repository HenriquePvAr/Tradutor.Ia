import _test_bootstrap  # noqa: F401
import tempfile
import unittest
from pathlib import Path

from test_chapter_terminology_ledger import _group
from session_context import SessionContextStore


class WorldMemoryPhase7CRed(unittest.TestCase):
    def _store(self, d):
        return SessionContextStore(Path(d) / 'ctx.json', 'https://example.invalid/c')

    def _binding(self, store, key='CARRIER'):
        return store.term_bindings().get(key)

    def test_a_untrusted_ocr_does_not_learn(self):
        with tempfile.TemporaryDirectory() as d:
            g=_group('CARRIER ARRIVED','MENINO CHEGOU',group_id='R1')
            g.source_recovery={'trusted':False,'reason':'material_variant_disagreement'}
            s=self._store(d); s.record_translations([g])
            self.assertIsNone(self._binding(s))

    def test_b_uncertain_ocr_not_hard_authority(self):
        with tempfile.TemporaryDirectory() as d:
            g=_group('CARRIER ARRIVED','ENTREGADOR CHEGOU',group_id='R2')
            g.source_recovery={'trusted':True,'reason':'low_confidence'}
            s=self._store(d); s.record_translations([g]); binding=self._binding(s)
            self.assertTrue(binding is None or binding.get('authority') not in {'explicit_glossary','proper_name','entity_term','established_domain_term'})

    def test_c_retry_same_region_counts_once(self):
        with tempfile.TemporaryDirectory() as d:
            s=self._store(d); g=_group('CARRIER','ENTREGADOR',group_id='R3'); s.record_translations([g,g,g]); self.assertEqual(self._binding(s).get('evidence_count'),1)

    def test_d_replay_after_reload_counts_once(self):
        with tempfile.TemporaryDirectory() as d:
            g=_group('CARRIER','ENTREGADOR',group_id='R4'); s=self._store(d); s.record_translations([g]); s2=self._store(d); s2.record_translations([g]); self.assertEqual(s2.term_bindings()['CARRIER'].get('evidence_count'),1)

    def test_e_distinct_occurrence_counts(self):
        with tempfile.TemporaryDirectory() as d:
            s=self._store(d); s.record_translations([_group('CARRIER','ENTREGADOR',group_id='R5'),_group('CARRIER','ENTREGADOR',group_id='R6')]); self.assertGreaterEqual(s.term_bindings()['CARRIER'].get('evidence_count',0),2)
