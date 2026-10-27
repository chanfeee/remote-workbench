"""Read-only localhost audit viewer; does not start SSH or an agent."""
import argparse
import errno
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import re
import uuid

from remote_workbench import audit
from remote_workbench.presentation import summarize_record, summarize_output, parse_unified_diff


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Remote Workbench · 操作记录</title>
<style>
:root{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202b3a;background:#f4f6f9;--panel:#fff;--line:#e1e6ed;--muted:#687587;--accent:#315fd4;--accent-soft:#edf2ff;--code:#f7f8fa;--green:#17713c;--green-soft:#edf8f0;--red:#a62f27;--red-soft:#fff0ee;--amber:#835400;--amber-soft:#fff6df;--shadow:0 1px 2px rgba(19,32,52,.05)}
[hidden]{display:none!important}*{box-sizing:border-box}body{margin:0;min-height:100vh}button,input{font:inherit}button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
header{height:64px;padding:0 22px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:2}h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.01em}.brand-mark{width:9px;height:9px;background:var(--accent);border-radius:3px;transform:rotate(45deg)}header p{margin:0;color:var(--muted);font-size:13px}.live{margin-left:auto;white-space:nowrap;color:var(--muted);font-size:13px;display:flex;gap:7px;align-items:center}.live input{accent-color:var(--accent)}
main{display:grid;grid-template-columns:minmax(250px,310px) minmax(0,1fr);height:calc(100vh - 64px);min-height:500px}.sidebar{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-height:0}.sidehead{padding:16px 15px 12px;border-bottom:1px solid var(--line)}.sidehead-top{display:flex;align-items:center;justify-content:space-between}.sidehead h2{font-size:14px;margin:0;font-weight:700}.filter{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px;margin-top:12px;cursor:pointer}.filter input{accent-color:var(--accent)}.records{overflow:auto;padding:8px}.record{width:100%;display:block;border:1px solid transparent;border-radius:9px;padding:10px 11px;background:transparent;text-align:left;color:inherit;cursor:pointer;margin:2px 0}.record:hover{background:#f6f8fb}.record[aria-current=true]{background:var(--accent-soft);border-color:#dce6ff}.record-title{font-weight:650;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px}.record-meta{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;overflow:hidden;margin-top:2px}.record-meta span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.record-time{margin-left:auto;flex-shrink:0}.status{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;border-radius:99px;padding:2px 9px;background:#eef1f5;color:#596779;white-space:nowrap}.status::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}.status-running{background:var(--amber-soft);color:var(--amber)}.status-failed,.status-cancelled{background:var(--red-soft);color:var(--red)}.status-timeout,.status-warning{background:var(--amber-soft);color:var(--amber)}.status-success{background:var(--green-soft);color:var(--green)}.subtle{color:var(--muted)}
.content{overflow:auto;padding:28px 34px;min-width:0}.empty{height:100%;display:grid;place-items:center;color:var(--muted);text-align:center;padding:30px}.empty-inner{max-width:340px}.empty-icon{font-size:32px;color:#a8b4c3;margin-bottom:8px}.empty strong{display:block;color:#354255;margin-bottom:5px}.operation{max-width:1040px;margin:0 auto}.ophead{display:flex;gap:14px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}.ophead>div{min-width:0;max-width:100%}.ophead>div:first-child{flex:1 1 240px}.ophead h2{font-size:24px;line-height:1.25;margin:0 0 7px;font-weight:730;letter-spacing:-.015em;overflow-wrap:anywhere}.op-context{color:var(--muted);font-size:13px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.op-context .separator{color:#b5bdc8}.op-actions{display:flex;gap:8px;align-items:center}.button{border:1px solid var(--line);border-radius:7px;background:var(--panel);padding:7px 11px;cursor:pointer;color:inherit;font-size:13px;box-shadow:var(--shadow)}.button:hover{border-color:#b9c8e7}.button:disabled{opacity:.45;cursor:default}.button-secondary{box-shadow:none}
.summary-card{margin:18px 0 20px;padding:14px 16px;border:1px solid #d8eadc;border-radius:10px;background:var(--green-soft);color:var(--green)}.summary-card .summary-title{font-weight:700;font-size:16px}.summary-card .summary-detail{font-size:13px;margin-top:2px;opacity:.85;overflow-wrap:anywhere;white-space:pre-wrap}.summary-card.is-failed{border-color:#f2d5d1;background:var(--red-soft);color:var(--red)}.summary-card.is-neutral{border-color:var(--line);background:var(--panel);color:#344258}.summary-files{display:grid;gap:5px;margin-top:10px}.summary-file{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:13px}.summary-file-path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;color:inherit}.summary-file-count{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px}
.section{margin:21px 0}.section-head{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:8px}.section-title{font-size:14px;font-weight:700;margin:0;color:#364357}.section-note{font-size:12px;color:var(--muted)}.target-path{display:block;min-width:0;max-width:100%;margin:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 13px;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;box-shadow:var(--shadow)}.command,.output,.wrapper{margin:0;border:1px solid var(--line);border-radius:9px;background:var(--code);padding:13px 15px;font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow:auto;overflow-wrap:anywhere;max-height:34vh;tab-size:4}.command{border-left:3px solid var(--accent)}details{margin-top:11px}details summary{cursor:pointer;color:var(--muted);font-size:12px;user-select:none}.tech-grid{display:grid;grid-template-columns:1fr;gap:8px;margin-top:9px}.tech-item{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 12px}.tech-label{font-size:11px;font-weight:700;color:var(--muted);margin-bottom:5px}.tech-value{margin:0;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:24vh;overflow:auto}.children{display:flex;gap:7px;flex-wrap:wrap}.child{border:1px solid var(--line);background:var(--panel);border-radius:7px;padding:6px 9px;cursor:pointer;color:var(--accent);font-size:12px}.child:hover{border-color:var(--accent)}
.toolbar{display:flex;align-items:center;gap:5px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:8px}.stream{border:1px solid transparent;background:transparent;border-radius:7px;padding:6px 11px;cursor:pointer;color:var(--muted);font-size:13px}.stream[aria-pressed=true]{background:var(--accent-soft);color:#244da8;font-weight:650}.output-wrap{position:relative}.output{min-height:160px;max-height:58vh;white-space:pre;overflow:auto;overflow-wrap:normal;color:#263447}.empty-output{color:var(--muted)}.diff-row{display:grid;grid-template-columns:3.4em 3.4em minmax(0,1fr);min-width:max-content;margin:0 -15px;padding:0 15px}.diff-row-code{white-space:pre}.diff-gutter{user-select:none;text-align:right;color:#9aa5b3;padding-right:10px;border-right:1px solid var(--line);margin-right:10px;opacity:.8}.diff-add{color:#146c36;background:#e9f6ec}.diff-del{color:#a72c24;background:#fff0ee}.diff-meta{color:#315d9a;font-weight:650;background:#eef3fa;display:block;padding:2px 0}.diff-file{color:#214c91;font-weight:700;background:#eaf0fa;display:block;padding:7px 0;margin:3px 0}.message{color:var(--red);font-size:12px;min-height:18px}.actions{display:flex;gap:9px;align-items:center;margin-top:9px}.actions small{color:var(--muted);margin-left:auto;font-size:12px}.raw-details{margin-top:12px}.raw-details summary{font-size:12px}.raw-output{max-height:24vh;margin-top:7px}.toast{color:var(--red);font-size:12px;min-height:18px}
@media(max-width:800px){main{grid-template-columns:minmax(215px,270px) minmax(0,1fr)}.content{padding:22px 20px}.ophead h2{font-size:21px}}
@media(max-width:650px){header{height:58px;padding:0 14px;gap:9px}header p{display:none}.live{margin-left:auto}main{display:flex;flex-direction:column;height:auto;min-height:calc(100vh - 58px)}.sidebar{max-height:35vh;min-height:130px;border-right:0;border-bottom:1px solid var(--line)}.sidehead{padding:11px 13px}.filter{margin-top:8px}.records{padding:5px 8px}.content{overflow:visible;padding:18px 14px}.empty{min-height:300px}.ophead h2{font-size:20px}.summary-card{margin:14px 0}.output{max-height:54vh}}
@media(prefers-color-scheme:dark){:root{color:#e6edf5;background:#111820;--panel:#18212c;--line:#2d3a49;--muted:#a0adbc;--accent:#8dadff;--accent-soft:#243654;--code:#131b24;--green:#a8e4b5;--green-soft:#1b3424;--red:#ffaaa2;--red-soft:#3b2528;--amber:#ffdb8c;--amber-soft:#3b321b;--shadow:none}.record:hover{background:#202c39}.record[aria-current=true]{border-color:#38527e}.summary-card{border-color:#31543a}.summary-card.is-failed{border-color:#643b3a}.summary-card.is-neutral{border-color:var(--line);color:#d4deeb}.status{color:#c0ccda}.status-timeout,.status-warning{background:#4c3a17;color:#ffda8a}.section-title{color:#d4deeb}.target-path,.tech-item{background:#151e28}.stream[aria-pressed=true]{color:#d0deff}.output{color:#dce6f4}.diff-add{color:#a5e8b5;background:#183523}.diff-del{color:#ffaaa8;background:#492626}.diff-meta{color:#b6cefa;background:#202d40}.diff-file{color:#c3d6fb;background:#1e2b3e}.diff-gutter{border-color:#344253}}
</style></head><body>
<header><span class="brand-mark" aria-hidden="true"></span><h1>Remote Workbench 操作记录</h1><p>命令、输出和代码改动保存在本机，可直接查看完整记录。</p>
<label class="live"><input id="live" type="checkbox" checked> 自动刷新</label></header>
<main><aside class="sidebar"><div class="sidehead"><div class="sidehead-top"><h2>最近操作</h2><button class="button button-secondary" id="refresh" type="button">刷新</button></div><label class="filter"><input id="follow-latest" type="checkbox" checked disabled> 跟随最新操作</label><label class="filter"><input id="show-technical" type="checkbox"> 显示内部执行步骤</label><div class="subtle" id="refresh-status" role="status" style="font-size:12px;margin-top:10px">正在开启自动刷新…</div></div><div class="records" id="records" aria-live="polite"></div></aside>
<section class="content" id="content"><div class="empty" id="empty"><div class="empty-inner"><div class="empty-icon" aria-hidden="true">⌘</div><strong>选择一项操作</strong><span>这里会显示实际命令、运行结果和代码改动。</span></div></div>
<article class="operation" id="operation" hidden><div class="ophead"><div><h2 id="heading">操作详情</h2><div class="op-context" id="context-line"></div></div><div class="op-actions"><span class="status" id="state-pill"></span></div></div>
<div class="summary-card" id="summary-card" hidden><div class="summary-title" id="summary-title"></div><div class="summary-detail" id="summary-detail"></div><div class="summary-files" id="summary-files"></div></div>
<div class="section" id="action-section" hidden><div class="section-head"><h3 class="section-title" id="action-label">实际执行的命令</h3><span class="section-note" id="action-note"></span></div><pre class="command" id="command"></pre><pre class="target-path" id="target-path" hidden></pre></div>
<details id="wrapper-details"><summary>技术详情：参数、远端路径与执行记录</summary><div class="tech-grid"><div class="tech-item"><div class="tech-label">操作编号</div><pre class="tech-value" id="opid"></pre></div><div class="tech-item" id="host-detail-item"><div class="tech-label">服务器</div><pre class="tech-value" id="host-detail"></pre></div><div class="tech-item" id="path-detail-item"><div class="tech-label">工作目录</div><pre class="tech-value" id="path-detail"></pre></div><div class="tech-item" id="command-detail-item" hidden><div class="tech-label">原始命令 / 操作说明</div><pre class="tech-value" id="command-detail"></pre></div><div class="tech-item" id="arguments-item" hidden><div class="tech-label">工具参数</div><pre class="tech-value" id="arguments"></pre></div><div class="tech-item" id="wrapped-item" hidden><div class="tech-label">SSH / shell 包装命令</div><pre class="tech-value" id="wrapper"></pre></div><div class="tech-item" id="child-section" hidden><div class="tech-label">内部执行步骤</div><div class="children" id="children"></div></div><div class="tech-item" id="extra-stream-item" hidden><div class="tech-label">启动信息与其他数据流</div><div class="children" id="extra-streams"></div></div></div></details>
<div class="section"><div class="section-head"><h3 class="section-title" id="output-heading">运行输出</h3><span class="section-note" id="output-note"></span></div><div class="toolbar" id="streams" role="tablist" aria-label="操作输出"></div>
<div class="summary-card" id="output-summary-card" hidden><div class="summary-title" id="output-summary-title"></div><div class="summary-detail" id="output-summary-detail"></div><div class="summary-files" id="output-summary-files"></div></div>
<div class="output-wrap"><pre class="output" id="output" aria-live="polite"></pre></div>
<div class="actions"><button class="button" id="next" type="button">读取下一段</button><small id="output-meta"></small></div><div class="message" id="message" role="status"></div>
<details class="raw-details" id="raw-details"><summary>查看原始完整输出</summary><div class="subtle" style="font-size:12px;margin:6px 0">原始文本会按需分段读取，滚动区域保留完整换行和空格。</div><pre class="output raw-output" id="raw-output"></pre><div class="actions"><button class="button" id="raw-next" type="button">读取下一段</button><small id="raw-meta"></small></div></details></div></article></section></main>
<script>
(() => {
  const VIEWER_ID = null;
  const STREAMS = ['stdout','stderr','diff','stdin','startup_stdout','startup_stderr'];
  const LABELS = {stdout:'输出',stderr:'运行日志',diff:'代码改动',stdin:'输入',startup_stdout:'启动信息',startup_stderr:'启动诊断'};
  const TOOL_TITLES = {ssh_run:'运行命令',ssh_run_background:'启动后台任务',ssh_job_status:'查看后台任务',ssh_read_file:'读取文件',ssh_write_file:'修改文件',ssh_patch_file:'修改文件',ssh_append_file:'追加文件内容',ssh_insert_lines:'插入文件内容',ssh_replace_lines:'替换文件行',ssh_list_dir:'查看目录',ssh_file_info:'查看文件信息',ssh_mkdir:'创建目录',ssh_move:'移动文件',ssh_delete:'删除文件',ssh_download:'下载文件',ssh_grep:'搜索代码',ssh_find_files:'查找文件',ssh_git_status:'查看版本状态',ssh_git_diff:'查看代码改动',ssh_git_log:'查看提交记录',ssh_apply_patch:'应用代码补丁',ssh_check_connection:'检查连接'};
  const el = id => document.getElementById(id);
  let selected = '', activeStream = 'stdout', cursor = 0, rawCursor = 0, selectedRecord = null, polling = false, timer = null, diffState = {old:null,next:null,oldRemaining:0,nextRemaining:0,inHunk:false,pending:''};
  let latestRecordId = '', streamPinned = false, outputPlaceholder = false, ready = false;
  const buttons = new Map();
  async function get(url) {
    let requestPrefix = '';
    if (VIEWER_ID !== null) {
      const instancePrefix = '/v/' + VIEWER_ID;
      if (location.pathname !== instancePrefix && !location.pathname.startsWith(instancePrefix + '/'))
        throw Error('此操作记录页面已失效或来自其他项目，请重新打开当前项目提供的链接。');
      const workspacePrefix = location.pathname.match(new RegExp('^' + instancePrefix + '/w/[a-f0-9]{32}(?=/)'));
      requestPrefix = instancePrefix + (workspacePrefix ? workspacePrefix[0].slice(instancePrefix.length) : '');
    } else {
      const workspacePrefix = location.pathname.match(/^\/w\/[a-f0-9]{32}(?=\/)/);
      requestPrefix = workspacePrefix ? workspacePrefix[0] : '';
    }
    const response = await fetch(requestPrefix + url, {cache:'no-store'});
    if (VIEWER_ID !== null && (!response.headers || response.headers.get('X-Remote-Workbench-Viewer') !== VIEWER_ID))
      throw Error('此操作记录页面已失效或来自其他项目，请重新打开当前项目提供的链接。');
    let value;
    try { value = await response.json(); } catch (_) { throw Error('HTTP ' + response.status); }
    if (!response.ok) throw Error(value.error || ('HTTP ' + response.status));
    return value;
  }
  function setUrl(id, replace=false) {
    const url = new URL(location.href);
    if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }
  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function clean(value) { return value === undefined || value === null ? '' : String(value); }
  function pathName(value) {
    const path = clean(value).replace(/[\\/]+$/,'');
    if (!path) return '';
    const parts = path.split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length-1] : path;
  }
  function operationTitle(record) {
    const view = record.view || {};
    return view.title || TOOL_TITLES[record.tool] || (record.kind === 'command' ? '远端命令' : clean(record.tool).replace(/^ssh_/,'').replace(/_/g,' ')) || '操作记录';
  }
  function stateInfo(record) {
    const view = record.view || {};
    const status = clean(record.status).toLowerCase();
    const code = record.exit_code;
    if (view.state_label) {
      const state = clean(view.state).toLowerCase();
      const cls = ['failed','error'].includes(state) ? 'status-failed' : ['running','starting'].includes(state) ? 'status-running' : ['timeout','timed_out'].includes(state) ? 'status-timeout' : ['success','passed','checked','finished','complete','completed','ok'].includes(state) ? 'status-success' : 'status-neutral';
      return {label:view.state_label,cls:cls};
    }
    if (['starting','running'].includes(status)) return {label:'进行中',cls:'status-running'};
    if (status === 'timeout') return {label:'已超时',cls:'status-timeout'};
    if (status === 'cancelled') return {label:'已取消',cls:'status-cancelled'};
    if (status === 'failed' || (Number.isFinite(Number(code)) && Number(code) !== 0)) return {label:'执行失败',cls:'status-failed'};
    if (status === 'finished' || status === 'complete' || status === 'completed') return {label:'已完成',cls:'status-success'};
    return {label:status || '结果未知',cls:''};
  }
  function formatTime(value) {
    if (value === undefined || value === null || value === '') return '';
    const date = new Date(Number(value) * 1000);
    if (!Number.isFinite(date.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
  }
  function workspaceName(record) {
    const view = record.view || {};
    const workspace = view.workspace || view.workspace_path || record.cwd || record.actual_cwd || '';
    return view.workspace && view.workspace !== view.workspace_path ? String(view.workspace) : pathName(workspace) || '';
  }
  function updateSelectionStyle() {
    for (const [id, button] of buttons) button.setAttribute('aria-current', String(id === selected));
  }
  function rowTitle(row) { return operationTitle(row); }
  async function refreshRecords() {
    const rows = await get('/api/records');
    const showTechnical = el('show-technical').checked;
    const visible = rows.filter(row => showTechnical || !row.parent_id);
    latestRecordId = visible.length ? visible[0].id : '';
    const keep = new Set();
    const container = el('records');
    const selectedButton = buttons.get(selected);
    const selectedOffset = selectedButton ? selectedButton.offsetTop - container.scrollTop : null;
    const empty = container.querySelector('[data-empty]');
    if (empty) empty.remove();
    for (const row of visible) {
      keep.add(row.id);
      let button = buttons.get(row.id);
      if (!button) {
        button = make('button','record'); button.type = 'button'; button.dataset.id = row.id;
        button.addEventListener('click', () => selectManualRecord(row.id));
        buttons.set(row.id, button);
      }
      button.replaceChildren();
      const title = make('span','record-title',rowTitle(row));
      const meta = make('span','record-meta');
      const outcome = stateInfo(row);
      const state = make('span','status ' + outcome.cls,outcome.label);
      const project = workspaceName(row);
      const projectNode = make('span','',project || clean(row.host) || '远端工作区');
      const time = make('span','record-time',formatTime(row.created_at));
      meta.append(state,projectNode,time);
      button.append(title,meta); container.append(button);
    }
    for (const [id, button] of buttons) if (!keep.has(id)) { button.remove(); buttons.delete(id); }
    updateSelectionStyle();
    if (!visible.length) {
      const note = make('div','record-meta',rows.length ? '当前仅有内部执行步骤' : '暂无操作记录'); note.dataset.empty = 'true'; container.append(note);
    } else if (selectedButton && buttons.has(selected)) {
      container.scrollTop = selectedButton.offsetTop - selectedOffset;
    }
    return visible;
  }
  function setStatePill(record) {
    const info = stateInfo(record), node = el('state-pill');
    node.className = 'status ' + info.cls; node.textContent = info.label;
  }
  function setContext(record) {
    const context = el('context-line'), view = record.view || {};
    context.replaceChildren();
    const host = view.host || record.host;
    const workspace = workspaceName(record);
    const elapsed = view.elapsed && view.elapsed !== '—' ? view.elapsed : (record.elapsed_ms != null ? Number(record.elapsed_ms) + ' 毫秒' : '');
    if (workspace) context.append(make('span','',workspace));
    if (workspace && host) context.append(make('span','separator','·'));
    if (host) context.append(make('span','',host));
    if (elapsed) { if (workspace || host) context.append(make('span','separator','·')); context.append(make('span','',elapsed)); }
    const when = formatTime(record.created_at);
    if (when) { if (workspace || host || elapsed) context.append(make('span','separator','·')); context.append(make('span','',when)); }
    if (view.sync_label) { context.append(make('span','separator','·')); context.append(make('span','',view.sync_label)); }
  }
  function showSummary(target, summary) {
    const prefix = target === 'output' ? 'output-' : '';
    const card = el(prefix + 'summary-card');
    const titleNode = el(prefix + 'summary-title'), detailNode = el(prefix + 'summary-detail'), filesNode = el(prefix + 'summary-files');
    filesNode.replaceChildren();
    if (!summary || !summary.title) { card.hidden = true; return; }
    card.hidden = false;
    titleNode.textContent = summary.title;
    detailNode.textContent = summary.detail || '';
    const failed = summary.state === 'failed';
    const verifiedPass = summary.kind === 'tests' && summary.state === 'success' && summary.complete === true;
    card.classList.toggle('is-failed', failed);
    card.classList.toggle('is-neutral', !failed && !verifiedPass);
    if (!summary.detail && summary.kind === 'diff') {
      const added = Number(summary.added || 0), deleted = Number(summary.deleted || 0);
      const files = Number((summary.files || []).length || 0);
      titleNode.textContent = summary.complete ? files + ' 个文件有改动' : files + ' 个文件的改动片段';
      detailNode.textContent = '新增 ' + added + ' 行 · 删除 ' + deleted + ' 行' + (summary.complete ? '' : ' · 统计尚未完成');
    }
    (summary.files || []).forEach(file => {
      const row = make('div','summary-file');
      row.append(make('span','summary-file-path',file.path || '文件'));
      const added = Number(file.added || 0), deleted = Number(file.deleted || 0);
      if (added || deleted) row.append(make('span','summary-file-count','+' + added + '  −' + deleted));
      filesNode.append(row);
    });
  }
  function renderSummaryFromRecord(record) {
    const view = record.view || {};
    const summary = view.summary || null;
    showSummary('summary',summary);
  }
  function preferredStream(record) {
    const sizes = record.stream_bytes || record.output_bytes || {};
    if (Number(sizes.diff || 0) > 0 || record.arguments && record.arguments.mode === 'patch') return 'diff';
    if (Number(sizes.stdout || 0) > 0) return 'stdout';
    return Number(sizes.stderr || 0) > 0 ? 'stderr' : 'stdout';
  }
  function renderStreams(record) {
    const root = el('streams'); root.replaceChildren();
    const counts = record.stream_bytes || record.output_bytes || {};
    const gitDiffPatch = record.arguments && record.arguments.mode === 'patch';
    const available = ['stdout','stderr','diff'].filter(name => name !== 'stdout' || !gitDiffPatch);
    const extras = ['stdin','startup_stdout','startup_stderr'].filter(name => Number(counts[name] || 0) > 0);
    const extraRoot = el('extra-streams'); extraRoot.replaceChildren();
    el('extra-stream-item').hidden = extras.length === 0;
    for (const name of extras) {
      const button = make('button','child',LABELS[name]); button.type = 'button';
      button.setAttribute('aria-pressed',String(activeStream === name));
      button.addEventListener('click',()=>{streamPinned=true;activeStream=name;renderStreams(selectedRecord);loadOutput(true);if(el('raw-details').open)loadRaw(true);});
      extraRoot.append(button);
    }
    if (!available.includes(activeStream) && !extras.includes(activeStream)) activeStream = preferredStream(record);
    for (const name of available) {
      const button = make('button','stream',LABELS[name] || name); button.type = 'button';
      button.setAttribute('role','tab'); button.setAttribute('aria-selected',String(activeStream === name));
      button.setAttribute('aria-pressed', String(activeStream === name));
      button.addEventListener('click', () => { streamPinned = true; activeStream = name; renderStreams(selectedRecord); loadOutput(true); if (el('raw-details').open) loadRaw(true); });
      root.append(button);
    }
    el('output-heading').textContent = activeStream === 'diff' ? '代码改动' : activeStream === 'stderr' ? '运行日志' : activeStream === 'stdin' ? '输入内容' : '运行输出';
    el('output-note').textContent = '';
  }
  function resetDiff() { diffState = {old:null,next:null,oldRemaining:0,nextRemaining:0,inHunk:false,pending:''}; }
  function appendDiffRow(parent, line) {
    if (line.startsWith('diff --git ')) {
      diffState.inHunk = false;
      const file = make('span','diff-file',line); parent.append(file); return;
    }
    if (line.startsWith('@@ ')) {
      const match = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
      if (match) {
        diffState.old = Number(match[1]); diffState.next = Number(match[3]);
        diffState.oldRemaining = Number(match[2] || 1); diffState.nextRemaining = Number(match[4] || 1);
        diffState.inHunk = true;
      }
      const meta = make('span','diff-meta',line); parent.append(meta); return;
    }
    if (line.startsWith('+++') || line.startsWith('---')) {
      if (!diffState.inHunk) { parent.append(make('span','diff-meta',line)); return; }
    }
    if (line.startsWith('index ') || line.startsWith('new file mode') || line.startsWith('deleted file mode') || line.startsWith('\\ No newline')) {
      parent.append(make('span','diff-meta',line)); return;
    }
    if (diffState.inHunk && (line.startsWith('+') || line.startsWith('-') || line.startsWith(' '))) {
      const added = line.startsWith('+'), deleted = line.startsWith('-');
      const row = make('span','diff-row ' + (added ? 'diff-add' : deleted ? 'diff-del' : ''));
      const oldNum = added || diffState.old === null ? '' : String(diffState.old);
      const newNum = deleted || diffState.next === null ? '' : String(diffState.next);
      row.append(make('span','diff-gutter',oldNum),make('span','diff-gutter',newNum),make('span','diff-row-code',line));
      if (added) {
        if (diffState.next !== null) diffState.next += 1;
        diffState.nextRemaining -= 1;
      } else if (deleted) {
        if (diffState.old !== null) diffState.old += 1;
        diffState.oldRemaining -= 1;
      } else {
        if (diffState.old !== null) diffState.old += 1;
        if (diffState.next !== null) diffState.next += 1;
        diffState.oldRemaining -= 1; diffState.nextRemaining -= 1;
      }
      if (diffState.oldRemaining <= 0 && diffState.nextRemaining <= 0) diffState.inHunk = false;
      parent.append(row); return;
    }
    parent.append(make('span','diff-meta',line));
  }
  function renderDiff(content, append, hasMore=false) {
    const out = el('output');
    if (!append) { out.replaceChildren(); resetDiff(); }
    const combined = (append ? diffState.pending : '') + content;
    diffState.pending = '';
    const lines = combined.match(/[^\n]*\n|[^\n]+$/g) || [];
    if (hasMore && lines.length && !lines[lines.length-1].endsWith('\n')) diffState.pending = lines.pop();
    for (const line of lines) appendDiffRow(out,line);
    if (!out.childNodes.length) out.classList.add('empty-output'); else out.classList.remove('empty-output');
  }
  function renderContent(content, append=false, hasMore=false) {
    if (outputPlaceholder) { append = false; outputPlaceholder = false; }
    if (activeStream === 'diff') renderDiff(content, append, hasMore);
    else {
      const out = el('output');
      if (!append) out.textContent = '';
      out.append(document.createTextNode(content));
      out.classList.toggle('empty-output', !out.textContent);
    }
  }
  function outputSourceStream(stream) {
    const gitDiff = selectedRecord && (['git_diff','ssh_git_diff'].includes(selectedRecord.tool) || selectedRecord.arguments && selectedRecord.arguments.mode === 'patch');
    return stream === 'diff' && gitDiff ? 'stdout' : stream;
  }
  function emptyOutputText(stream) {
    return stream === 'diff' ? '无改动' : '（当前没有这类输出）';
  }
  async function loadOutput(reset, append=false) {
    if (!selected) return true;
    if (reset) { cursor = 0; renderContent('', false); showSummary('output',null); }
    const requestId = selected, requestStream = activeStream, sourceStream = outputSourceStream(activeStream), requestCursor = cursor;
    const sourceId = selectedRecord && selectedRecord.output_source_id || requestId;
    el('message').textContent = '';
    try {
      const value = await get('/api/output?id='+encodeURIComponent(sourceId)+'&stream='+encodeURIComponent(sourceStream)+'&cursor='+requestCursor);
      if (selected !== requestId || activeStream !== requestStream || cursor !== requestCursor || (selectedRecord && selectedRecord.output_source_id || selected) !== sourceId) return true;
      const workerError = value.summary && value.summary.kind === 'worker_error';
      renderContent(workerError ? value.summary.detail : value.content, workerError ? false : append || requestCursor > 0, value.truncated);
      cursor = value.next_cursor;
      if (value.summary) showSummary('output',value.summary);
      el('next').disabled = !value.truncated;
      el('output-meta').textContent = value.total_bytes.toLocaleString() + ' 字节' + (value.truncated ? ' · 尚有后续内容' : ' · 已显示完整内容');
      if (!value.total_bytes && !el('output').textContent) { el('output').textContent = emptyOutputText(requestStream); outputPlaceholder = true; }
      return true;
    } catch (error) {
      if (selected !== requestId || activeStream !== requestStream) return true;
      const missingOutput = /not found|no such file or directory/i.test(error.message);
      if (missingOutput) { el('output').textContent = emptyOutputText(requestStream); outputPlaceholder = true; }
      el('next').disabled = true; el('output-meta').textContent = '';
      if (!missingOutput) el('message').textContent = error.message;
      return missingOutput;
    }
  }
  async function loadRaw(reset=false) {
    if (!selected) return;
    if (reset) { rawCursor = 0; el('raw-output').textContent = ''; }
    const requestId = selected, requestStream = activeStream, requestCursor = rawCursor, stream = outputSourceStream(activeStream);
    const sourceId = selectedRecord && selectedRecord.output_source_id || requestId;
    try {
      const value = await get('/api/output?id='+encodeURIComponent(sourceId)+'&stream='+encodeURIComponent(stream)+'&cursor='+requestCursor);
      if (selected !== requestId || activeStream !== requestStream || rawCursor !== requestCursor || (selectedRecord && selectedRecord.output_source_id || selected) !== sourceId) return;
      if (!requestCursor) el('raw-output').textContent = '';
      el('raw-output').append(document.createTextNode(value.content)); rawCursor = value.next_cursor;
      el('raw-next').disabled = !value.truncated;
      el('raw-meta').textContent = value.total_bytes.toLocaleString() + ' 字节' + (value.truncated ? ' · 尚有后续内容' : ' · 已读完');
    } catch (error) {
      if (selected !== requestId) return;
      el('raw-output').textContent = '（当前没有原始输出）'; el('raw-next').disabled = true; el('raw-meta').textContent = '';
    }
  }
  function renderAction(record) {
    const view = record.view || {}, args = record.arguments || {};
    const tool = record.tool || '';
    const command = args.command || (['ssh_run','ssh_run_background'].includes(tool) ? view.command || record.command || '' : '');
    const path = args.path || args.file || args.source || args.destination || '';
    const commandTool = ['ssh_run','ssh_run_background'].includes(tool) || Boolean(args.command);
    el('action-section').hidden = !commandTool && !path;
    el('command').hidden = !commandTool;
    el('target-path').hidden = !path;
    if (commandTool) {
      el('action-label').textContent = tool === 'ssh_run_background' ? '启动命令' : '实际执行的命令';
      el('action-note').textContent = '在远端工作目录中运行';
      el('command').textContent = command || '（命令记录不可用）';
    }
    if (path) {
      if (!commandTool) el('action-label').textContent = ['ssh_git_diff','ssh_git_status','ssh_git_log'].includes(tool) ? '涉及路径' : '操作对象';
      el('target-path').textContent = path;
    }
  }
  function renderRecordDetails(record) {
    el('heading').textContent = operationTitle(record);
    setStatePill(record); setContext(record); renderSummaryFromRecord(record); renderAction(record);
    el('arguments-item').hidden = !record.arguments;
    el('arguments').textContent = record.arguments ? JSON.stringify(record.arguments,null,2) : '';
    const commandDetail = (record.view && record.view.command) || record.command || (record.arguments && record.arguments.command) || '';
    el('command-detail-item').hidden = !commandDetail;
    el('command-detail').textContent = commandDetail;
    const fullPath = (record.view && record.view.workspace_path) || record.actual_cwd || record.cwd || '';
    el('path-detail-item').hidden = !fullPath;
    el('path-detail').textContent = fullPath;
    el('host-detail-item').hidden = !(record.view && record.view.host || record.host);
    el('host-detail').textContent = (record.view && record.view.host) || record.host || '';
    el('wrapper').textContent = record.wrapped_command || '（无包装命令记录）';
    el('wrapped-item').hidden = !record.wrapped_command;
    const childIds = record.child_commands || [];
    el('children').replaceChildren();
    el('child-section').hidden = !record.parent_id && !childIds.length;
    if (record.parent_id) {
      const parent = make('button','child','查看上层操作'); parent.type='button'; parent.addEventListener('click',()=>selectManualRecord(record.parent_id)); el('children').append(parent);
    }
    childIds.forEach((child,index)=>{const button=make('button','child','内部步骤 '+(index+1));button.type='button';button.addEventListener('click',()=>selectManualRecord(child));el('children').append(button);});
  }
  function selectManualRecord(id) {
    el('follow-latest').checked = id === latestRecordId;
    return selectRecord(id);
  }
  async function selectRecord(id, options={}) {
    selected = id; cursor = 0; rawCursor = 0; selectedRecord = null; streamPinned = false; outputPlaceholder = false;
    if (!options.fromHistory) setUrl(id, Boolean(options.replace));
    updateSelectionStyle(); el('message').textContent = ''; el('output').replaceChildren(); el('raw-output').replaceChildren();
    el('empty').hidden = true; el('operation').hidden = false;
    el('heading').textContent = '正在读取操作记录…'; el('opid').textContent = id;
    el('command').textContent = ''; el('command-detail').textContent = ''; el('wrapper').textContent = ''; el('arguments').textContent = '';
    el('action-section').hidden = true; el('command').hidden = false; el('target-path').hidden = true;
    el('wrapped-item').hidden = true; el('arguments-item').hidden = true; el('command-detail-item').hidden = true;
    el('child-section').hidden = true; el('children').replaceChildren();
    el('extra-stream-item').hidden = true; el('extra-streams').replaceChildren();
    el('summary-card').hidden = true; el('output-summary-card').hidden = true;
    el('raw-next').disabled = true;
    try {
      const record = await get('/api/record?id='+encodeURIComponent(id));
      if (selected !== id) return true;
      selectedRecord = record;
      renderRecordDetails(record);
      activeStream = preferredStream(record);
      renderStreams(record); const ok = await loadOutput(true);
      if (el('raw-details').open) await loadRaw(true);
      return ok;
    } catch (error) {
      if (selected !== id) return true;
      el('heading').textContent = '无法读取操作记录'; el('message').textContent = error.message;
      el('operation').hidden = false;
      return false;
    }
  }
  async function refreshSelectedRecord() {
    const requestId = selected;
    const record = await get('/api/record?id='+encodeURIComponent(requestId));
    if (selected !== requestId) return true;
    const previousSource = selectedRecord && selectedRecord.output_source_id || requestId, previousStream = activeStream;
    selectedRecord = record;
    if (!streamPinned) activeStream = preferredStream(record);
    const sizes = record.stream_bytes || record.output_bytes || {};
    const reset = previousSource !== (record.output_source_id || requestId) || previousStream !== activeStream || Number(sizes[outputSourceStream(activeStream)] || 0) < cursor;
    renderRecordDetails(record); renderStreams(record);
    const ok = await loadOutput(reset, !reset);
    if (el('raw-details').open) await loadRaw(reset);
    return ok;
  }
  function refreshStatus(error) {
    if (error) { el('refresh-status').textContent = '刷新失败：' + error.message; return; }
    const time = new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date());
    const mode = el('live').checked ? (el('follow-latest').checked ? '跟随最新' : '更新当前记录') : '已刷新';
    el('refresh-status').textContent = mode + ' · 最近更新 ' + time;
  }
  async function poll() {
    if (polling) return; polling = true;
    try {
      const rows = await refreshRecords();
      if (el('live').checked && el('follow-latest').checked && rows.length && selected !== rows[0].id) {
        if (!await selectRecord(rows[0].id, {replace:true})) throw Error(el('message').textContent || '无法读取操作记录');
        el('records').scrollTop = 0;
      } else if (selected && !await refreshSelectedRecord()) throw Error(el('message').textContent || '无法读取操作输出');
      refreshStatus();
    } catch (error) { el('message').textContent = error.message; refreshStatus(error); }
    finally { polling = false; }
  }
  el('refresh').addEventListener('click',poll);
  el('show-technical').addEventListener('change',()=>refreshRecords().catch(error=>el('message').textContent=error.message));
  el('next').addEventListener('click',()=>loadOutput(false,true));
  el('raw-next').addEventListener('click',()=>loadRaw(false));
  el('raw-details').addEventListener('toggle',event=>{if(event.target.open) loadRaw(true);});
  function syncPolling() {
    if (timer) clearInterval(timer); timer = null;
    el('follow-latest').disabled = !el('live').checked;
    if (!ready) return;
    if (el('live').checked) { poll(); timer=setInterval(poll,2000); }
    else el('refresh-status').textContent = '自动刷新已关闭';
  }
  el('live').addEventListener('change',syncPolling);
  el('follow-latest').addEventListener('change',()=>{if(el('follow-latest').checked)el('records').scrollTop=0;if(el('live').checked)poll();});
  addEventListener('pageshow',syncPolling);
  addEventListener('pagehide',()=>{if(timer)clearInterval(timer);timer=null;});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && el('live').checked)poll();});
  addEventListener('popstate',()=>{
    const id = new URL(location.href).searchParams.get('id');
    el('follow-latest').checked = false;
    if (id) selectRecord(id,{fromHistory:true});
    else { selected=''; selectedRecord=null; setUrl('',true); el('operation').hidden=true; el('empty').hidden=false; updateSelectionStyle(); }
  });
  refreshRecords().then(async()=>{
    const id = new URL(location.href).searchParams.get('id');
    if (id) await selectRecord(id,{fromHistory:true});
  }).catch(error=>{el('message').textContent=error.message;refreshStatus(error);}).finally(()=>{ready=true;syncPolling();});
})();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in expected or (origin and origin not in {"http://" + host for host in expected}):
            self._send_json({"error": "Forbidden"}, 403)
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        value = lambda key, default="": query.get(key, [default])[0]
        try:
            if url.path == "/":
                viewer_id = getattr(self, "viewer_instance_id", None)
                page = HTML.replace(
                    "const VIEWER_ID = null;",
                    f"const VIEWER_ID = {json.dumps(viewer_id)};",
                    1,
                )
                self._send(page.encode(), "text/html; charset=utf-8", 200)
                return
            if url.path == "/api/records":
                result = self._recent_views(self._integer(value("limit", "100"), "limit"))
            elif url.path == "/api/record":
                record_id = value("id")
                record, source_id = self._record_view(record_id)
                source_dir = audit.directory(source_id)
                record["output_source_id"] = source_id
                record["stream_bytes"] = {
                    name: (source_dir / name).stat().st_size
                    for name in audit.STREAMS
                    if (source_dir / name).is_file()
                }
                self._send_json(record, 200)
                return
            elif url.path == "/api/output":
                record_id = value("id")
                record_path = audit.directory(record_id) / "record.json"
                if not record_path.is_file():
                    raise FileNotFoundError("Operation not found")
                limit = self._integer(value("limit", "16384"), "limit")
                result = audit.read_output(record_id, value("stream", "stdout"),
                                           self._integer(value("cursor", "0"), "cursor"), limit)
                summary = self._output_summary(record_id, value("stream", "stdout"))
                if summary is not None:
                    result["summary"] = summary
            else:
                self._send_json({"error": "Route not found"}, 404)
                return
            self._send_json(result, 200)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc) or "Output stream not found"}, 404)
        except (ValueError, OSError) as exc:
            self._send_json({"error": str(exc)}, 400)

    @staticmethod
    def _integer(value: str, name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc

    @staticmethod
    def _record_view(record_id: str) -> tuple[dict, str]:
        record_path = audit.directory(record_id) / "record.json"
        if not record_path.is_file():
            raise FileNotFoundError("Operation not found")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        source_id = record_id
        source_record = None
        child_id = record.get("output_id")
        listed_children = record.get("child_commands", [])
        if (isinstance(child_id, str) and child_id != record_id
                and isinstance(listed_children, list) and child_id in listed_children):
            try:
                child_dir = audit.directory(child_id)
                child_path = child_dir / "record.json"
                if child_path.is_file():
                    child = json.loads(child_path.read_text(encoding="utf-8"))
                    if child.get("parent_id") == record_id:
                        source_id = child_id
                        source_record = child
                        if child.get("kind") == "job":
                            for key in ("status", "execution_state", "running", "remote_task", "sync_state"):
                                record[key] = child.get(key)
                        for key in ("actual_cwd", "exit_code", "diff_kind", "check_only", "patch_result", "wrapped_command"):
                            if key not in record and key in child:
                                record[key] = child[key]
            except (OSError, ValueError, TypeError):
                # A stale or malformed child reference must never escape the
                # audit directory or prevent the parent record from loading.
                pass
        record["view"] = summarize_record(record, source_record)
        return record, source_id

    @classmethod
    def _recent_views(cls, limit: int) -> list[dict]:
        rows = audit.recent(limit)
        for row in rows:
            try:
                record, _ = cls._record_view(row["id"])
                row["view"] = {key: value for key, value in record["view"].items() if key != "command"}
            except (OSError, ValueError, TypeError):
                row["view"] = summarize_record(row)
                row["view"].pop("command", None)
        return rows

    @staticmethod
    def _output_summary(record_id: str, stream: str):
        """Derive small UI-only summaries from completed, bounded local logs.

        Never parse a preview as though it were the complete execution result.
        Large outputs remain paged, without scanning their full contents here.
        These HTTP fields are not added to any MCP tool result.
        """
        directory = audit.directory(record_id)
        record = json.loads((directory / "record.json").read_text(encoding="utf-8"))
        path = directory / stream
        if (record.get("status") not in {"finished", "failed"}
                or record.get("exit_code") is None or path.stat().st_size > 262144):
            return None
        if stream == "diff" and not record.get("diff_kind"):
            # SSH has finished before remote_client writes the derived diff.
            # Its final metadata flag is written only after the diff is closed.
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if stream == 'stdout' and record.get('worker_op'):
            # Older records already contain the parsed worker_op. Present their
            # structured failures without changing the saved protocol bytes.
            markers = list(re.finditer(r'\x1eREMOTE_WORKBENCH_RESULT_[a-f0-9]{32}:\x1f', text))
            if markers:
                try:
                    result, _ = json.JSONDecoder().raw_decode(text[markers[-1].end():])
                except ValueError:
                    result = None
                if isinstance(result, dict) and isinstance(result.get('error'), str):
                    message = result.get('message') or result['error']
                    if not isinstance(message, str):
                        message = result['error']
                    if isinstance(result.get('path'), str):
                        message += '\n' + result['path']
                    return {'kind': 'worker_error', 'state': 'failed', 'complete': True,
                            'title': '远端操作失败', 'detail': message}
        if stream == "diff":
            return parse_unified_diff(text, complete=True)
        if stream in {"stdout", "stderr"}:
            return summarize_output(text, record, stream, complete=True) or parse_unified_diff(text, complete=True)
        return None

    def _send_json(self, value, status):
        self._send(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def _send(self, body, mime, status):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")
        viewer_id = getattr(self, "viewer_instance_id", None)
        if viewer_id is not None:
            self.send_header("X-Remote-Workbench-Viewer", viewer_id)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def handler_for_directory(path):
    """Create an HTTP handler permanently scoped to one session's records."""
    class ScopedHandler(Handler):
        def do_GET(self):
            with audit.scoped_root(path):
                super().do_GET()

    return ScopedHandler


def handler_for_workspaces(path):
    """One viewer with explicit, isolated /w/<workspace UUID>/ routes."""
    import re
    base = Path(path)

    class WorkspaceHandler(Handler):
        def do_GET(self):
            match = re.match(r"^/w/([a-f0-9]{32})(/.*)$", self.path)
            if match is None or not (base / match[1]).is_dir():
                self._send_json({"error": "Use the operation_log link for a known workspace"}, 404)
                return
            self.path = match[2]
            with audit.scoped_root(base / match[1]):
                super().do_GET()

    return WorkspaceHandler


def handler_for_instance(base_handler, instance_id):
    """Require a per-server URL prefix so stale links cannot reach a new viewer."""
    if not isinstance(instance_id, str) or re.fullmatch(r"[a-f0-9]{32}", instance_id) is None:
        raise ValueError("viewer instance ID must be 32 lowercase hexadecimal characters")

    prefix = f"/v/{instance_id}"

    class InstanceHandler(base_handler):
        viewer_instance_id = instance_id

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == prefix:
                scoped_path = "/"
            elif url.path.startswith(prefix + "/"):
                scoped_path = url.path[len(prefix):]
            else:
                self._send_json({"error": "This viewer link is no longer valid"}, 404)
                return
            self.path = scoped_path + ("?" + url.query if url.query else "")
            super().do_GET()

    return InstanceHandler


def main():
    parser = argparse.ArgumentParser(description="Remote Workbench local audit viewer")
    parser.add_argument("--port", type=int, default=0,
                        help="Local viewer port (default 0: OS-assigned); falls back if occupied")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    instance_id = uuid.uuid4().hex
    handler = handler_for_instance(Handler, instance_id)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    print(f"操作记录：http://127.0.0.1:{server.server_port}/v/{instance_id}/ 目录：{audit.root()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
