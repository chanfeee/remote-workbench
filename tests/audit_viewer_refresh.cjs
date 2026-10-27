const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// A small DOM and HTTP fixture for the actual shipped script, with no DOM package dependency.
class Element {
  constructor(tag = '') {
    this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {};
    this.handlers = new Map(); this.checked = false; this.hidden = false; this.open = false;
    this.scrollTop = 0; this.offsetTop = 0; this._text = '';
    const classes = new Set();
    this.classList = {add: x => classes.add(x), remove: x => classes.delete(x),
      contains: x => classes.has(x), toggle: (x, yes) => yes ? classes.add(x) : classes.delete(x)};
  }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parent) node.remove();
      this.children.push(node); node.parent = this;
    }
  }
  replaceChildren(...nodes) {
    this._text = '';
    for (const child of this.children) child.parent = null;
    this.children = []; this.append(...nodes);
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this);
    this.parent = null;
  }
  querySelector() { return this.children.find(x => x.dataset.empty); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(handler);
  }
  async emit(type) {
    for (const handler of this.handlers.get(type) || []) await handler({target: this});
  }
}

const A = 'a'.repeat(32), B = 'b'.repeat(32), C = 'c'.repeat(32);
const viewerId = process.argv[3] || '';
const responseViewer = process.argv[4] || '';
const defaultLive = process.argv[5] === 'true';
const prefix = (viewerId ? '/v/' + viewerId : '') + '/w/' + 'd'.repeat(32);
const clone = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));

async function boot({running = false, empty = false, checked = false, output = 'first\n'} = {}) {
  const elements = new Map(), timers = new Map(), events = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const state = {rows: [], records: new Map(), streams: new Map(), summaries: new Map(), requests: [], failList: false, jsonCalls: 0};
  function add(id, text, status = 'finished') {
    const record = {id, tool: 'exec_command', status, exit_code: status === 'finished' ? 0 : null,
      created_at: state.records.size + 1, arguments: {command: 'command-' + id[0]},
      view: {title: 'operation-' + id[0]}, output_source_id: id};
    state.records.set(id, record); state.rows.unshift(id);
    if (text) state.streams.set(id + ':stdout', text);
    return record;
  }
  if (!empty) add(A, output, running ? 'running' : 'finished');
  const location = {href: 'http://127.0.0.1:8767' + prefix + '/' + (empty ? '' : '?id=' + A), pathname: prefix + '/'};
  el('operation').hidden = true;
  el('live').checked = checked; el('follow-latest').checked = true;
  let serial = 0;
  const sandbox = {console, URL, Intl, Map, Set, Date,
    document: {getElementById: el, createElement: tag => new Element(tag),
      createTextNode: text => { const n = new Element(); n.textContent = text; return n; },
      addEventListener: (name, fn) => events.set(name, fn)},
    location, history: {replaceState: (_, __, url) => {location.href = String(url);},
      pushState: (_, __, url) => {location.href = String(url);}},
    addEventListener: (name, fn) => events.set(name, fn),
    setInterval: fn => {const id = ++serial; timers.set(id, fn); return id;},
    clearInterval: id => timers.delete(id),
    fetch: async url => {
      state.requests.push(url);
      const parsed = new URL(url, location.href), q = parsed.searchParams;
      let value, status = 200;
      if (parsed.pathname.endsWith('/records')) {
        if (state.failList) {state.failList = false; status = 503; value = {error: 'temporary outage'};}
        else value = state.rows.map(id => clone(state.records.get(id)));
      } else if (parsed.pathname.endsWith('/record')) {
        if (state.failRecord) {state.failRecord = false; status = 503; value = {error: 'temporary record outage'};}
        else {
          value = clone(state.records.get(q.get('id')));
          value.stream_bytes = {};
          for (const stream of ['stdout', 'stderr', 'diff']) {
            value.stream_bytes[stream] = Buffer.byteLength(state.streams.get(value.output_source_id + ':' + stream) || '');
          }
        }
      } else {
        const key = q.get('id') + ':' + q.get('stream');
        const bytes = Buffer.from(state.streams.get(key) || '');
        const cursor = Number(q.get('cursor') || 0), next = Math.min(bytes.length, cursor + 16384);
        value = {content: bytes.subarray(cursor, next).toString(), next_cursor: next,
          total_bytes: bytes.length, truncated: next < bytes.length};
        if (state.summaries.has(key)) value.summary = clone(state.summaries.get(key));
        if (state.afterOutput) {const hook = state.afterOutput; state.afterOutput = null; hook();}
      }
      return {ok: status === 200, status,
        headers: {get: name => name.toLowerCase() === 'x-remote-workbench-viewer'
          ? (responseViewer === '-' ? null : responseViewer || viewerId || null) : null},
        json: async () => {state.jsonCalls++; return value;}};
    }};
  vm.runInNewContext(fs.readFileSync(0, 'utf8'), sandbox);
  await flush();
  return {state, add, el, location, timers, events,
    enable: async () => {el('live').checked = true; await el('live').emit('change'); await flush();},
    tick: async () => {assert.equal(timers.size, 1, 'live polling timer is active'); await [...timers.values()][0](); await flush();}};
}

async function run(name) {
  let f;
  if (name === 'follow_latest' || name === 'manual_history' || name === 'retry') {
    f = await boot(); await f.enable();
    if (name === 'retry') {
      f.state.failList = true; await f.tick();
      assert.match(f.el('refresh-status').textContent, /失败/);
    }
    if (name === 'follow_latest') f.el('records').scrollTop = 800;
    f.add(B, 'second\n'); await f.tick();
    assert.equal(f.el('output').textContent, 'second\n');
    assert.equal(new URL(f.location.href).searchParams.get('id'), B);
    if (name === 'follow_latest') assert.equal(f.el('records').scrollTop, 0, 'newest followed row is visible');
    if (name === 'manual_history') {
      await f.el('records').children.find(x => x.dataset.id === A).emit('click'); await flush();
      assert.equal(f.el('follow-latest').checked, false);
      f.el('records').scrollTop = 123;
      f.add(C, 'third\n'); await f.tick();
      assert.equal(f.el('output').textContent, 'first\n', 'reading history is not interrupted');
      assert.ok(f.el('records').children.find(x => x.dataset.id === C), 'list still updates');
      assert.equal(f.el('records').scrollTop, 123, 'history scroll position is preserved');
      f.el('follow-latest').checked = true; await f.el('follow-latest').emit('change'); await flush();
      assert.equal(f.el('output').textContent, 'third\n');
    }
    if (name === 'retry') assert.doesNotMatch(f.el('refresh-status').textContent, /失败/);
  } else if (name === 'initially_empty') {
    f = await boot({empty: true}); await f.enable(); f.add(A, 'arrived\n'); await f.tick();
    assert.equal(f.el('output').textContent, 'arrived\n');
    assert.equal(f.el('operation').hidden, false);
  } else if (name === 'final_tail') {
    f = await boot({running: true});
    f.state.afterOutput = () => {f.state.streams.set(A + ':stdout', 'first\nLAST\n'); f.state.records.get(A).status = 'finished';};
    await f.enable(); await f.tick(); await f.tick();
    assert.equal(f.el('output').textContent, 'first\nLAST\n', 'completion must not lose final bytes');
  } else if (name === 'late_stdout' || name === 'late_stderr') {
    f = await boot({running: true, output: ''}); await f.enable();
    f.state.streams.set(A + ':' + (name === 'late_stderr' ? 'stderr' : 'stdout'), 'late output\n');
    f.state.records.get(A).status = 'finished'; await f.tick();
    assert.equal(f.el('output').textContent, 'late output\n', 'empty placeholder must be replaced');
  } else if (name === 'worker_error') {
    const raw = '\x1eREMOTE_WORKBENCH_RESULT_' + A + ':\x1f{"error":"file_not_found"}\n';
    f = await boot({output: raw});
    f.state.summaries.set(A + ':stdout', {kind: 'worker_error', state: 'failed',
      title: '远端操作失败', detail: 'File does not exist; create it first.'});
    await f.enable(); await f.tick();
    assert.equal(f.el('output').textContent, 'File does not exist; create it first.');
    assert.doesNotMatch(f.el('output').textContent, /REMOTE_WORKBENCH_RESULT/);
    f.el('raw-details').open = true; await f.el('raw-details').emit('toggle'); await flush();
    assert.equal(f.el('raw-output').textContent, raw);
  } else if (name === 'raw_output') {
    f = await boot({running: true}); f.el('raw-details').open = true;
    await f.el('raw-details').emit('toggle'); await flush(); await f.enable();
    f.state.streams.set(A + ':stdout', 'first\nLAST\n'); f.state.records.get(A).status = 'finished';
    await f.tick(); assert.equal(f.el('raw-output').textContent, 'first\nLAST\n');
  } else if (name === 'new_output_source') {
    f = await boot({running: true, output: ''}); await f.enable();
    f.state.records.get(A).output_source_id = C; f.state.records.get(A).status = 'finished';
    f.state.streams.set(C + ':stdout', 'child output\n'); await f.tick();
    assert.equal(f.el('output').textContent, 'child output\n');
  } else if (name === 'default_live' || name === 'disable_default_live') {
    f = await boot({checked: defaultLive});
    assert.equal(f.el('live').checked, true, 'the shipped HTML enables polling by default');
    assert.equal(f.timers.size, 1, 'opening the page starts polling without a click');
    if (name === 'default_live') {
      f.add(B, 'automatic\n'); await f.tick();
      assert.equal(f.el('output').textContent, 'automatic\n');
    } else {
      f.el('live').checked = false; await f.el('live').emit('change');
      assert.equal(f.timers.size, 0, 'the user can stop default polling');
      await f.events.get('pageshow')();
      assert.equal(f.timers.size, 0, 'restoring a page respects the user disabling polling');
      f.state.streams.set(A + ':stdout', 'first\nmanual\n');
      await f.el('refresh').emit('click'); await flush();
      assert.equal(f.el('output').textContent, 'first\nmanual\n');
    }
  } else if (name === 'restored_checkbox') {
    f = await boot({checked: true}); f.add(B, 'restored\n'); await f.tick();
    assert.equal(f.el('output').textContent, 'restored\n');
  } else if (name === 'manual_refresh') {
    f = await boot(); f.state.streams.set(A + ':stdout', 'first\nLAST\n');
    await f.el('refresh').emit('click'); await flush();
    assert.equal(f.el('output').textContent, 'first\nLAST\n');
  } else if (name === 'pinned_stream') {
    f = await boot({running: true}); f.state.streams.set(A + ':stderr', 'log\n');
    await f.el('streams').children.find(x => x.textContent === '运行日志').emit('click'); await flush();
    await f.enable(); f.state.streams.set(A + ':stdout', 'a new result\n');
    f.state.streams.set(A + ':stderr', 'log\nLAST\n'); await f.tick();
    assert.equal(f.el('output').textContent, 'log\nLAST\n', 'manual stream selection is preserved');
  } else if (name === 'retry_record') {
    f = await boot(); await f.enable(); f.state.failRecord = true;
    f.add(B, 'recovered\n'); await f.tick();
    assert.match(f.el('refresh-status').textContent, /失败/);
    await f.tick(); assert.equal(f.el('output').textContent, 'recovered\n');
  } else if (name === 'viewer_valid') {
    f = await boot();
    assert.equal(f.el('operation').hidden, false, 'the matching viewer instance can render its record');
    assert.equal(f.el('output').textContent, 'first\n');
    assert.ok(f.state.requests.every(url => url.startsWith(prefix + '/api/')),
      'all API requests retain both viewer and workspace prefixes');
  } else if (name === 'viewer_foreign' || name === 'viewer_missing') {
    f = await boot();
    assert.equal(f.state.jsonCalls, 0, 'a foreign or missing viewer identity is rejected before parsing response data');
    assert.equal(f.el('operation').hidden, true, 'foreign records are never rendered');
    assert.match(f.el('message').textContent, /页面已失效或来自其他项目/);
    assert.ok(f.state.requests.every(url => url.startsWith(prefix + '/api/')),
      'even a stale page requests only its original viewer/workspace prefix');
  } else throw Error('Unknown case: ' + name);
  assert.ok(f.state.requests.every(url => url.startsWith(prefix + '/api/')), 'workspace scope is preserved');
}

run(process.argv[2]).catch(error => {console.error(error); process.exitCode = 1;});
