// Contract for the reader's state model.
//
// Every decision the reader makes -- page bounds, zoom steps, fit scale, which async
// result is still wanted -- lives in `createReaderState` and is exercised here with
// no DOM, no timers and no network.
//
// Run: node test_chapter_reader.mjs

import assert from 'node:assert/strict';
import {
  createReaderState, MIN_ZOOM, MAX_ZOOM, ZOOM_STEPS,
  CLOSED, LOADING, READY, ERROR,
} from './static/chapter_reader.js';

const failures = [];
let passed = 0;

function test(name, fn) {
  try { fn(); passed += 1; }
  catch (err) { failures.push({name, message: String((err && err.message) || err)}); }
}

const doc = (pages, extra = {}) => ({
  title: 'Capítulo', mode: 'images', page_count: pages.length, pages, ...extra,
});
const uniform = (count, width = 800, height = 1600) =>
  Array.from({length: count}, () => ({width, height}));

// A reader already open on a document, sized to a viewport.
function ready(pages = uniform(10), extra = {}) {
  const reader = createReaderState();
  const token = reader.open('job-a');
  reader.accept(token, doc(pages, extra));
  reader.setViewport(1000, 800);
  return reader;
}

/* ---------------------------------------------------------------- lifecycle */

test('a fresh reader is closed and shows nothing', () => {
  const reader = createReaderState();
  assert.equal(reader.state.status, CLOSED);
  assert.equal(reader.state.pageCount, 0);
});

test('opening moves to loading and only the matching result becomes ready', () => {
  const reader = createReaderState();
  const token = reader.open('job-a');
  assert.equal(reader.state.status, LOADING);
  assert.equal(reader.accept(token, doc(uniform(3))), true);
  assert.equal(reader.state.status, READY);
  assert.equal(reader.state.pageCount, 3);
});

test('a failure for the current load is reported, a stale one is ignored', () => {
  const reader = createReaderState();
  const stale = reader.open('job-a');
  const current = reader.open('job-b');
  assert.equal(reader.fail(stale, 'artifact_unavailable'), false);
  assert.equal(reader.state.status, LOADING);
  assert.equal(reader.fail(current, 'artifact_unavailable'), true);
  assert.equal(reader.state.status, ERROR);
  assert.equal(reader.state.error, 'artifact_unavailable');
});

test('opening B while A is still loading discards A entirely', () => {
  const reader = createReaderState();
  const a = reader.open('job-a');
  const b = reader.open('job-b');
  // A finishes late, carrying pages that belong to a chapter the user left.
  assert.equal(reader.accept(a, doc(uniform(99))), false);
  assert.equal(reader.state.pageCount, 0);
  assert.equal(reader.accept(b, doc(uniform(4))), true);
  assert.equal(reader.state.pageCount, 4);
  assert.equal(reader.state.jobId, 'job-b');
});

test('closing releases the document and invalidates results in flight', () => {
  const reader = createReaderState();
  const token = reader.open('job-a');
  reader.close();
  assert.equal(reader.state.status, CLOSED);
  assert.equal(reader.accept(token, doc(uniform(5))), false);
  assert.equal(reader.state.pages.length, 0);
});

/* ------------------------------------------------------------------- pages */

test('pages are 1-based and start at the first page', () => {
  const reader = ready(uniform(84));
  assert.equal(reader.state.page, 1);
  assert.equal(reader.currentPage().width, 800);
});

test('next and previous stop at the ends instead of wrapping', () => {
  const reader = ready(uniform(3));
  assert.equal(reader.canPrevious(), false);
  assert.equal(reader.next(), 2);
  assert.equal(reader.next(), 3);
  assert.equal(reader.canNext(), false);
  assert.equal(reader.next(), 3);
  assert.equal(reader.previous(), 2);
  reader.first();
  assert.equal(reader.previous(), 1);
  assert.equal(reader.last(), 3);
});

test('a typed page jump survives garbage without crashing', () => {
  const reader = ready(uniform(10));
  assert.equal(reader.goto('7'), 7);
  assert.equal(reader.goto(0), 1);
  assert.equal(reader.goto(-4), 1);
  assert.equal(reader.goto(11), 10);
  assert.equal(reader.goto('abc'), 10);
  assert.equal(reader.goto(''), 10);
  assert.equal(reader.goto(null), 10);
  assert.equal(reader.goto(undefined), 10);
});

/* -------------------------------------------------------------------- zoom */

test('zoom walks fixed steps and never leaves its bounds', () => {
  const reader = ready();
  reader.resetZoom();
  assert.equal(reader.scale(), 1);
  assert.equal(reader.zoomIn(), 1.25);
  assert.equal(reader.zoomOut(), 1);
  for (let i = 0; i < 30; i += 1) reader.zoomIn();
  assert.equal(reader.scale(), MAX_ZOOM);
  for (let i = 0; i < 40; i += 1) reader.zoomOut();
  assert.equal(reader.scale(), MIN_ZOOM);
  // Repeated stepping must land on the published steps, not on drifted floats.
  assert.ok(ZOOM_STEPS.includes(reader.scale()));
});

test('zoom refuses zero, negative and non-numeric scales', () => {
  const reader = ready();
  assert.equal(reader.setZoom(0), MIN_ZOOM);
  assert.equal(reader.setZoom(-3), MIN_ZOOM);
  assert.equal(reader.setZoom(1e9), MAX_ZOOM);
  assert.equal(reader.setZoom('huge'), 1);
});

/* --------------------------------------------------------------- fit modes */

test('fit width uses the available width of the current page', () => {
  const reader = ready(uniform(3, 2000, 4000));
  assert.equal(reader.fitWidth(), 0.5);
  assert.equal(reader.state.fitMode, 'width');
});

test('fit page fits the whole page inside the viewport', () => {
  const reader = ready(uniform(3, 2000, 4000));
  // 1000/2000 = 0.5 wide, 800/4000 = 0.2 tall; the page has to fit both.
  assert.equal(reader.fitPage(), 0.25);
});

test('a fit mode recomputes per page, so mixed page sizes each fit', () => {
  const reader = ready([{width: 2000, height: 4000}, {width: 500, height: 400}]);
  reader.fitWidth();
  assert.equal(reader.scale(), 0.5);
  reader.next();
  assert.equal(reader.scale(), 2);
});

test('resizing follows a fit mode and leaves a manual zoom alone', () => {
  const reader = ready(uniform(3, 1000, 2000));
  reader.fitWidth();
  assert.equal(reader.scale(), 1);
  reader.setViewport(500, 800);
  assert.equal(reader.scale(), 0.5);
  reader.setZoom(2);
  reader.setViewport(2000, 900);
  assert.equal(reader.scale(), 2);
  assert.equal(reader.state.fitMode, 'none');
});

test('a document opens fit to width, the mode manga pages are read in', () => {
  const reader = ready(uniform(3, 800, 6000));
  assert.equal(reader.state.fitMode, 'width');
});

/* -------------------------------------------------------- render staleness */

test('the render token changes with the page and with the scale', () => {
  const reader = ready(uniform(80));
  const start = reader.renderToken();
  reader.goto(60);
  const moved = reader.renderToken();
  assert.notEqual(start, moved);
  reader.setZoom(2);
  assert.notEqual(moved, reader.renderToken());
  // Coming back to the same page and scale is the same render again.
  reader.goto(60);
  assert.equal(reader.renderToken(), reader.renderToken());
});

test('a render started before a document swap can never match after it', () => {
  const reader = ready(uniform(20));
  const before = reader.renderToken();
  const token = reader.open('job-b');
  reader.accept(token, doc(uniform(20)));
  reader.setViewport(1000, 800);
  assert.notEqual(reader.renderToken(), before);
});

/* ------------------------------------------------------------------ review */

test('a review_required chapter is readable and merely flagged', () => {
  const reader = ready(uniform(6), {review_required: true});
  assert.equal(reader.state.status, READY);
  assert.equal(reader.state.pageCount, 6);
  assert.equal(reader.state.reviewRequired, true);
});

test('a document the parser could not page falls back to the browser viewer', () => {
  const reader = createReaderState();
  const token = reader.open('job-a');
  reader.accept(token, {title: 'X', mode: 'embed', page_count: 0, pages: []});
  assert.equal(reader.state.mode, 'embed');
  assert.equal(reader.state.status, READY);
});

if (failures.length) {
  failures.forEach(f => console.error(`FAIL ${f.name}: ${f.message}`));
  console.error(`${failures.length} failed, ${passed} passed`);
  process.exit(1);
}
console.log(`${passed} passed`);
