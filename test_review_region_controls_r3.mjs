// R3 frontend contracts: dirty protection and pointer-to-natural-image geometry.
// Runs the real helpers extracted from static/tradutor_ui.js; no DOM/network needed.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(root, 'static', 'tradutor_ui.js'), 'utf8');
function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found`);
  const bodyStart = source.indexOf(') {', start) + 2;
  let depth = 0;
  for (let i = bodyStart; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces for ${name}`);
}
const reviewEditorDirty = new Function(`${extractFunction('reviewEditorDirty')};return reviewEditorDirty;`)();
const reviewTextDirty = new Function(`${extractFunction('reviewTextDirty')};return reviewTextDirty;`)();
const reviewBoxFromDrag = new Function(`${extractFunction('reviewBoxFromDrag')};return reviewBoxFromDrag;`)();
const reviewPointerToImage = new Function('$', `${extractFunction('reviewPointerToImage')};return reviewPointerToImage;`);
const guardUnsavedReviewEdits = new Function('appState','selectedReviewArticleDirty','showToast',
  `${extractFunction('guardUnsavedReviewEdits')};return guardUnsavedReviewEdits;`);

const pass = [];
function test(name, fn) { fn(); pass.push(name); }
const control = (mode, type, effectiveMode = 'auto', effectiveType = 'speech') => ({
  dataset: { effectiveMode, effectiveType },
  querySelector: selector => selector === '[data-region-mode]' ? { value: mode }
    : selector === '[data-region-type]' ? { value: type } : null,
});
const article = c => ({querySelector: selector => selector === '[data-region-controls]' ? c : null});

test('mode override is dirty until saved', () => {
  const a = article(control('ignore', 'speech'));
  assert.equal(reviewEditorDirty(a), true);
  assert.equal(reviewTextDirty(a), false);
});
test('region type override is dirty until saved', () => {
  assert.equal(reviewEditorDirty(article(control('auto', 'narration'))), true);
});
test('restored effective mode and type are clean', () => {
  assert.equal(reviewEditorDirty(article(control('translate', 'thought', 'translate', 'thought'))), false);
});
test('unsaved region controls block navigation', () => {
  let shown = false;
  const guard = guardUnsavedReviewEdits({reviewDraw:null}, () => true, () => { shown = true; });
  assert.equal(guard(), false);
  assert.equal(shown, true);
});
test('active drawing blocks navigation', () => {
  const guard = guardUnsavedReviewEdits({reviewDraw:{mode:'add'}}, () => false, () => {});
  assert.equal(guard(), false);
});
test('drag coordinates clamp at natural-image bounds', () => {
  assert.deepEqual(reviewBoxFromDrag({x: 95, y: 110}, {x: 110, y: 130}, 100, 120), [95, 110, 5, 10]);
});
test('reverse drag normalizes origin and extents', () => {
  assert.deepEqual(reviewBoxFromDrag({x: 60, y: 50}, {x: 10, y: 20}, 100, 100), [10, 20, 50, 30]);
});
test('letterbox clicks map to natural coordinates; bars are rejected', () => {
  const image = {naturalWidth: 100, naturalHeight: 100};
  const frame = {getBoundingClientRect: () => ({left: 10, top: 20, width: 200, height: 100})};
  const lookup = selector => selector === '#reviewPreviewImage' ? image : frame;
  const mapper = reviewPointerToImage(lookup);
  assert.deepEqual(mapper(70, 45), {x: 10, y: 25, scale: 1, offsetX: 50, offsetY: 0});
  assert.equal(mapper(20, 45), null);
  assert.deepEqual(mapper(220, 130, {clampOutside:true}), {
    x:100, y:100, scale:1, offsetX:50, offsetY:0,
  });
});

console.log(`ok - ${pass.length} passed`);
