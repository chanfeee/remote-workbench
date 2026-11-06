// Bounded work under many saved cards, not an App latency benchmark.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '../remote_workbench/operation_card.html'), 'utf8')
  .match(/<script>([\s\S]*?)<\/script>/)[1];

function boot(payload) {
  const elements = new Map(), events = {}, frames = new Map(), messages = [];
  const metrics = {layouts: 0, serializations: 0, htmlWrites: 0, htmlBytes: 0};
  let intersection, resize, frameId = 0;
  const element = id => {
    if (!elements.has(id)) {
      let markup = '';
      elements.set(id, {
        hidden: false, textContent: '', handlers: {},
        get innerHTML() { return markup; },
        set innerHTML(value) { markup = value; metrics.htmlWrites++; metrics.htmlBytes += value.length; },
        getBoundingClientRect() { metrics.layouts++; return {height: 96}; },
        addEventListener(name, callback) { this.handlers[name] = callback; },
        removeAttribute() {},
      });
    }
    return elements.get(id);
  };
  const parent = {postMessage: message => messages.push(message)};
  const document = {hidden: false, getElementById: element, documentElement: element('html'),
    addEventListener: (name, callback) => { events[name] = callback; }};
  const window = {parent, innerWidth: 900, openai: {toolResponseMetadata: payload},
    addEventListener: (name, callback) => { events[name] = callback; }};
  const requestAnimationFrame = callback => { frames.set(++frameId, callback); return frameId; };
  const cancelAnimationFrame = id => frames.delete(id);
  Object.assign(window, {requestAnimationFrame, cancelAnimationFrame});
  vm.runInNewContext(source, {
    window, document, Map, Set, Number, String, Array, Object, Boolean, Error, Promise,
    JSON: {parse: JSON.parse, stringify(...args) { metrics.serializations++; return JSON.stringify(...args); }},
    IntersectionObserver: class { constructor(callback) { intersection = callback; } observe() {} disconnect() {} },
    ResizeObserver: class { constructor(callback) { resize = callback; } observe() {} disconnect() {} },
    requestAnimationFrame, cancelAnimationFrame, setTimeout: () => 1, clearTimeout() {},
  });
  return {metrics, messages, elements, window, document,
    visible(value) { intersection?.([{isIntersecting: value}]); },
    frame() { const queued = [...frames.values()]; frames.clear(); queued.forEach(callback => callback()); },
    resize() { events.resize(); resize?.([{contentRect: {height: 96}}]); },
    hydrate(value) { events['openai:set_globals']({detail: {globals: {toolResponseMetadata: value}}}); },
    visibility(hidden) { document.hidden = hidden; events.visibilitychange?.(); },
  };
}

const diff = '--- a/sample.py\n+++ b/sample.py\n@@ -1,100 +1,100 @@\n' + '-old\n+new\n'.repeat(100);
const payload = index => ({'remote-workbench/operation': {steps: [{
  tool: 'apply_patch', host: 'fixture', cwd: '/work', operation_id: String(index).padStart(32, '0'),
  state: 'finished', exit_code: 0,
  diff_summary: {complete: true, added: 100, deleted: 100, files: [{path: 'sample.py', added: 100, deleted: 100}]},
  streams: {diff: {content: diff, next_cursor: diff.length, truncated: false}},
}]}});
const cards = Array.from({length: 24}, (_, index) => boot(payload(index)));
const visible = cards.slice(0, 3), hidden = cards.slice(3);
visible.forEach(card => { card.visible(true); card.frame(); });
hidden.forEach(card => card.visible(false));
const initial = cards.reduce((result, card) => {
  for (const key of Object.keys(card.metrics)) result[key] = (result[key] || 0) + card.metrics[key];
  return result;
}, {});
const before = cards.map(card => ({...card.metrics}));
for (let round = 0; round < 80; round++) {
  cards.forEach((card, index) => {
    card.resize();
    card.hydrate(payload(index));
  });
}
cards.forEach(card => card.frame());
const repeated = cards.reduce((result, card, index) => {
  for (const key of Object.keys(card.metrics)) result[key] = (result[key] || 0) + card.metrics[key] - before[index][key];
  return result;
}, {});
const report = {cards: cards.length, visible: visible.length, initial, repeated,
  initializations: cards.reduce((count, card) => count + card.messages.filter(message => message.method === 'ui/initialize').length, 0)};
if (!process.argv.includes('--report-only')) {
  hidden.forEach(card => assert.deepEqual(card.metrics, {layouts: 0, serializations: 0, htmlWrites: 0, htmlBytes: 0}));
  // Handshake eagerly so hosts can reveal a view once initialized.
  assert.equal(report.initializations, cards.length);
  assert(repeated.layouts <= visible.length, JSON.stringify(report));
  assert(repeated.serializations <= visible.length, JSON.stringify(report));
  assert.equal(repeated.htmlWrites, 0);
  visible.forEach(card => assert(!card.elements.get('review').innerHTML.includes('class="line ')));
  const restored = hidden[0];
  const newest = payload(3);
  newest['remote-workbench/operation'].steps[0].diff_summary.files[0].path = 'newest.py';
  restored.hydrate(newest);
  restored.visible(true); restored.frame();
  assert(restored.elements.get('review').innerHTML.includes('newest.py'));
  assert.equal(restored.messages.filter(message => message.method === 'ui/initialize').length, 1);
  const paused = visible[0], pausedBefore = {...paused.metrics};
  paused.visibility(true);
  for (let round = 0; round < 20; round++) paused.resize();
  paused.frame();
  assert.deepEqual(paused.metrics, pausedBefore);
  paused.visibility(false); paused.frame();
}
console.log(JSON.stringify(report));
