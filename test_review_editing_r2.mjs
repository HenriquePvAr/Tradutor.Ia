// R2 — dirty-detection for review editors (frontend). Executes the REAL
// reviewEditorDirty from static/tradutor_ui.js (extracted by balanced-brace scan).
// This is the guard that prevents silently discarding unsaved source/translation edits.
// Run with: node test_review_editing_r2.mjs
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
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') { depth -= 1; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces for ${name}`);
}
// eslint-disable-next-line no-new-func
const reviewEditorDirty = new Function(`${extractFunction('reviewEditorDirty')}; return reviewEditorDirty;`)();

function fakeField(value, effective) { return { value, dataset: { effective } }; }
function fakeArticle(sourceField, targetField) {
  const map = {};
  if (sourceField !== undefined) map['[data-review-source]'] = sourceField;
  if (targetField !== undefined) map['[data-review-translation]'] = targetField;
  return { querySelector: (sel) => map[sel] || null };
}

const failures = [];
let passed = 0;
function test(name, fn) { try { fn(); passed += 1; } catch (e) { failures.push({ name, message: String(e.stack || e) }); } }

test('clean editors are not dirty', () => {
  const a = fakeArticle(fakeField('1 AM HERE', '1 AM HERE'), fakeField('1 ESTOU AQUI', '1 ESTOU AQUI'));
  assert.equal(Boolean(reviewEditorDirty(a)), false);
});
test('edited source is dirty', () => {
  const a = fakeArticle(fakeField('I AM HERE', '1 AM HERE'), fakeField('1 ESTOU AQUI', '1 ESTOU AQUI'));
  assert.equal(Boolean(reviewEditorDirty(a)), true);
});
test('edited target is dirty', () => {
  const a = fakeArticle(fakeField('1 AM HERE', '1 AM HERE'), fakeField('EU ESTOU AQUI', '1 ESTOU AQUI'));
  assert.equal(Boolean(reviewEditorDirty(a)), true);
});
test('null article is not dirty', () => {
  assert.equal(Boolean(reviewEditorDirty(null)), false);
});
test('missing editors are not dirty', () => {
  assert.equal(Boolean(reviewEditorDirty(fakeArticle())), false);
});

if (failures.length) {
  for (const f of failures) console.error(`FAIL ${f.name}\n${f.message}`);
  console.error(`\n${failures.length} failed, ${passed} passed`);
  process.exit(1);
}
console.log(`ok - ${passed} passed`);
