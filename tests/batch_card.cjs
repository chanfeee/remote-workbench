const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../remote_workbench/batch_card.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function mount(openai) {
  const elements = Object.fromEntries(['title', 'status', 'details', 'output'].map(name => [name, {
    textContent: '', hidden: name === 'details', open: false,
    addEventListener(type, callback) { this[type] = callback; },
    set innerHTML(_) { assert.fail('Tool output must be literal text'); },
  }]));
  const messages = [];
  const events = new Map();
  const frames = [];
  let setVisibility;
  let reads = 0;
  const parent = {postMessage: message => messages.push(message)};
  vm.runInNewContext(source, {
    window: {parent, openai, innerWidth: 640, addEventListener: (type, fn) => events.set(type, fn)},
    document: {getElementById: id => elements[id], body: {getBoundingClientRect: () => ({height: ++reads + 50})}},
    requestAnimationFrame: callback => frames.push(callback),
    IntersectionObserver: class {
      constructor(callback) { setVisibility = callback; }
      observe() {}
      disconnect() {}
    },
  });
  return {
    ...elements, messages,
    receive: data => events.get('message')({source: parent, data: {jsonrpc: '2.0', ...data}}),
    show: () => setVisibility([{isIntersecting: true}]),
    flush: () => frames.splice(0).forEach(callback => callback()),
    reads: () => reads,
  };
}

function result(data) {
  return {content: [{type: 'text', text: JSON.stringify(data)}]};
}

const data = {
  execution_state: 'finished', batch_state: 'finished', exit_code: 0,
  steps: Array.from({length: 8}, () => ({state: 'finished', exit_code: 0})),
  stdout: '<script>literal output</script>\n',
};
const wire = result(data);
const card = mount();
assert.equal(card.messages[0].method, 'ui/initialize');
card.receive({id: card.messages[0].id, result: {}});
assert.equal(card.messages[1].method, 'ui/notifications/initialized');
card.receive({method: 'ui/notifications/tool-result', params: wire});
card.flush();
assert.equal(card.details.hidden, true);
assert.equal(card.reads(), 0);
card.show();
card.flush();
assert.equal(card.title.textContent, '批量命令 · 8 条');
assert.equal(card.status.textContent, '完成');
assert.equal(card.output.textContent, '');
card.details.open = true;
card.details.toggle();
card.flush();
assert.equal(card.output.textContent.match(/exit 0/g).length, 8);
assert.ok(card.output.textContent.includes(data.stdout));
const reads = card.reads();
card.receive({method: 'ui/notifications/tool-result', params: wire});
card.flush();
assert.equal(card.reads(), reads);
card.details.open = false;
card.details.toggle();
assert.equal(card.output.textContent, '');

const restored = mount({toolResponseMetadata: {mcp_tool_result: result({...data, execution_state: 'remote_state_unknown'})}});
restored.show();
assert.equal(restored.title.textContent, '批量命令 · 8 条');
assert.equal(restored.status.textContent, '状态待确认');
