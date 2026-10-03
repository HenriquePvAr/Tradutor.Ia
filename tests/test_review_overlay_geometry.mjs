// R1 — bbox overlay geometry (frontend). Executes the REAL computeReviewOverlayRect
// from static/tradutor_ui.js (extracted by balanced-brace scan, not a copy) and proves
// the object-fit: contain letterbox mapping and resize alignment.
//
// Isolated: no DOM, no network. Run with: node test_review_overlay_geometry.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(ROOT, 'static', 'tradutor_ui.js'), 'utf8');

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`function ${name} not found`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let i = bodyStart; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces for ${name}`);
}

// eslint-disable-next-line no-new-func
const computeReviewOverlayRect = new Function(`${extractFunction('computeReviewOverlayRect')}; return computeReviewOverlayRect;`)();

const failures = [];
let passed = 0;
function test(name, fn) { try { fn(); passed += 1; } catch (e) { failures.push({ name, message: String(e.stack || e) }); } }

test('plain downscale, no letterbox', () => {
  const r = computeReviewOverlayRect({ width: 100, height: 200 }, { width: 50, height: 100 }, [10, 20, 30, 40]);
  assert.deepEqual(r, { left: 5, top: 10, width: 15, height: 20 });
});

test('horizontal letterbox offsets the overlay by the centered content rect', () => {
  // natural 100x100 in a 200x100 frame: scale 1, content 100 wide centered -> offX 50.
  const r = computeReviewOverlayRect({ width: 100, height: 100 }, { width: 200, height: 100 }, [10, 10, 20, 20]);
  assert.deepEqual(r, { left: 60, top: 10, width: 20, height: 20 });
});

test('vertical letterbox offsets on Y', () => {
  const r = computeReviewOverlayRect({ width: 100, height: 100 }, { width: 100, height: 200 }, [0, 0, 50, 50]);
  assert.deepEqual(r, { left: 0, top: 50, width: 50, height: 50 });
});

test('resize keeps the overlay aligned (scales proportionally)', () => {
  const box = [0, 0, 50, 50];
  const small = computeReviewOverlayRect({ width: 100, height: 100 }, { width: 100, height: 100 }, box);
  const large = computeReviewOverlayRect({ width: 100, height: 100 }, { width: 200, height: 200 }, box);
  assert.deepEqual(small, { left: 0, top: 0, width: 50, height: 50 });
  assert.deepEqual(large, { left: 0, top: 0, width: 100, height: 100 });
});

test('unmappable inputs return null', () => {
  assert.equal(computeReviewOverlayRect({ width: 0, height: 100 }, { width: 100, height: 100 }, [0, 0, 10, 10]), null);
  assert.equal(computeReviewOverlayRect({ width: 100, height: 100 }, { width: 100, height: 100 }, null), null);
  assert.equal(computeReviewOverlayRect({ width: 100, height: 100 }, { width: 0, height: 0 }, [0, 0, 10, 10]), null);
});

if (failures.length) {
  for (const f of failures) console.error(`FAIL ${f.name}\n${f.message}`);
  console.error(`\n${failures.length} failed, ${passed} passed`);
  process.exit(1);
}
console.log(`ok - ${passed} passed`);
