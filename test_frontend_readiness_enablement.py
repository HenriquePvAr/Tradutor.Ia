"""Regression coverage for recovering the Nova tradução form after a stale run."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UI = ROOT / "static" / "tradutor_ui.js"
NODE = shutil.which("node")


HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenarios = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const marker = 'function runtimeHasActiveTranslation(';
const start = source.indexOf(marker);
if (start < 0) throw new Error('runtime activity helper missing');
const brace = source.indexOf('{', start);
let depth = 0;
let end = -1;
for (let i = brace; i < source.length; i += 1) {
  if (source[i] === '{') depth += 1;
  else if (source[i] === '}') {
    depth -= 1;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) throw new Error('unterminated runtime activity helper');
const body = `${source.slice(start, end)}\nreturn runtimeHasActiveTranslation;`;
const runtimeHasActiveTranslation = new Function(
  'activeOperationStatuses', body
)(new Set(['staging', 'claiming', 'starting', 'running', 'cancelling', 'awaiting_source_review']));
const result = scenarios.map(s => runtimeHasActiveTranslation(s.runtime, s.status, s.queued));
process.stdout.write(JSON.stringify(result));
"""


class FrontendReadinessEnablementTests(unittest.TestCase):
    def run_harness(self, scenarios):
        if NODE is None:
            self.skipTest("node is required")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "runtime_activity.cjs"
            payload = Path(tmp) / "scenarios.json"
            harness.write_text(HARNESS, encoding="utf-8")
            payload.write_text(json.dumps(scenarios), encoding="utf-8")
            result = subprocess.run(
                [NODE, str(harness), str(UI), str(payload)],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_terminal_or_ready_runtime_cannot_be_locked_by_historical_latest(self):
        result = self.run_harness([
            # This is the reproduced bug: the old implementation treated the
            # historical latest queued row as a live operation.
            {"status": "failed", "runtime": {"latest": {"status": "queued"}}, "queued": None},
            {"status": "ready", "runtime": {"latest": {"status": "staging"}}, "queued": None},
            {"status": "failed", "runtime": {"active": {"status": "failed"}}, "queued": None},
        ])
        self.assertEqual(result, [False, False, False])

    def test_live_operation_locks_but_queued_work_does_not_lock_fresh_draft(self):
        result = self.run_harness([
            {"status": "running", "runtime": {"active": {"status": "running"}}, "queued": None},
            {"status": "ready", "runtime": {}, "queued": {"status": "queued"}},
            {"status": "queued", "runtime": {}, "queued": {"status": "queued"}},
        ])
        self.assertEqual(result, [True, False, False])


if __name__ == "__main__":
    unittest.main()
