// Exercise the reused Lite renderers and the existing read-only host bridge.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../remote_workbench/operation_card.html'),'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const tick = ()=>new Promise(resolve=>setImmediate(resolve));
function boot(initial) {
  const elements=new Map(),messages=[],timers=new Map(),events={};
  const el=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false,handlers:{},addEventListener(name,fn){this.handlers[name]=fn;}});return elements.get(id);};
  const parent={postMessage:message=>messages.push(message)};
  let serial=0;
  const context={Map,Set,JSON,Number,Array,Object,Boolean,String,Error,Promise,
    document:{getElementById:el,documentElement:{scrollHeight:300}},
    window:{parent,openai:initial?{toolResponseMetadata:initial}:undefined,addEventListener:(name,fn)=>{events[name]=fn;}},
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id)};
  vm.runInNewContext(source,context);
  const deliver=message=>events.message({source:parent,data:{jsonrpc:'2.0',...message}});
  return {el,messages,events,parent,deliver};
}
const wrap=steps=>({_meta:{'remote-workbench/operation':{steps}}});
const hostile='<img src=x onerror=alert(1)>';
const command={tool:'exec_command',host:'server',cwd:'/run',command:hostile,state:'finished',exit_code:0,elapsed_ms:1250,
  operation_id:'a'.repeat(32),output_id:'b'.repeat(32),streams:{stdout:{content:hostile,next_cursor:10,truncated:true}}};
(async()=>{
  const app=boot();
  const init=app.messages.find(x=>x.method==='ui/initialize');assert(init);
  app.deliver({id:init.id,result:{}});await tick();
  assert(app.messages.some(x=>x.method==='ui/notifications/initialized'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([command])});
  const activity=app.el('activity');
  assert.equal(activity.hidden,false);assert.equal(app.el('review').hidden,true);
  assert(activity.innerHTML.includes('远程活动'));
  assert(activity.innerHTML.includes('exit 0 · 1.25s'));
  assert(activity.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!activity.innerHTML.includes('<img'));
  for(const text of ['读取 0','修改 0','没有文件读取','完整命令','history','operation ','本次操作快照','CLI'])assert(!activity.innerHTML.includes(text),text);
  assert(!activity.innerHTML.includes(command.operation_id));
  const button={dataset:{step:'0',stream:'stdout'},disabled:false,hidden:false};
  const click=activity.handlers.click({target:{closest:()=>button}});await tick();
  const call=app.messages.find(x=>x.method==='tools/call');
  assert.equal(call.params.name,'read_output');assert.equal(call.params.arguments.cursor,10);
  app.deliver({id:call.id,result:{content:[{type:'text',text:JSON.stringify({content:'next',next_cursor:14,truncated:false})}]}});
  await click;
  assert.equal(app.el('rwb-output-0-stdout').textContent,hostile+'next');assert(button.hidden);
  assert(app.messages.every(x=>!['ui/message','ui/update-model-context'].includes(x.method)));
  assert(app.messages.filter(x=>x.method==='tools/call').every(x=>x.params.name==='read_output'));

  const diff='--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-old\n+'+hostile+'\n';
  const patch={tool:'apply_patch',host:'server',cwd:'/run',exit_code:0,state:'success',check_only:true,operation_id:'c'.repeat(32),
    diff_summary:{complete:true,added:1,deleted:1,files:[{path:'sample.py',added:1,deleted:1}]},
    streams:{diff:{content:diff,next_cursor:90,truncated:false}}};
  app.deliver({method:'ui/notifications/tool-result',params:wrap([patch])});
  const review=app.el('review');
  assert(!review.hidden&&activity.hidden);
  assert(review.innerHTML.includes('预览 · 1 个远程文件'));
  assert(review.innerHTML.includes('class="file-change"'));
  assert(review.innerHTML.includes('class="line added">+&lt;img'));
  assert(review.innerHTML.includes('class="line deleted">-old'));
  assert(review.innerHTML.includes('class="add">+1'));
  for(const text of ['history','operation ','本次操作快照','进程结果','完整历史','CLI'])assert(!review.innerHTML.includes(text),text);
  const previous=review.innerHTML;
  app.events.message({source:{},data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',params:wrap([])}});
  assert.equal(review.innerHTML,previous);

  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,exit_code:7,streams:{stderr:{content:'problem',next_cursor:7,truncated:false}}}])});
  assert(activity.innerHTML.includes('1 项失败')&&activity.innerHTML.includes('exit 7'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,exit_code:null,streams:{}}])});
  assert(activity.innerHTML.includes('状态未知')&&!activity.innerHTML.includes('1 项失败'));

  for(const [state,label] of [['remote_state_unknown','状态未知'],['unknown','状态未知'],
      ['cancellation_requested','取消待确认'],['cancelled','已取消'],['running','运行中'],['failed','1 项失败']]){
    app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,state,exit_code:0,streams:{},truncated:false}])});
    assert(activity.innerHTML.includes(label),state);
    assert(!activity.innerHTML.includes('已完成'),state);
    assert(!activity.innerHTML.includes('>✓<'),state);
    assert(activity.innerHTML.includes('exit 0'),state);
  }
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,cleanup_state:'unconfirmed',streams:{}}])});
  assert(activity.innerHTML.includes('状态未知')&&!activity.innerHTML.includes('已完成'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,state:'remote_state_unknown',timed_out:true,streams:{}}])});
  assert(activity.innerHTML.includes('已超时 · 状态未知')&&!activity.innerHTML.includes('已完成'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...patch,state:'remote_state_unknown',check_only:false}])});
  assert(review.innerHTML.includes('状态未知')&&review.innerHTML.includes('class="file-change"'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...patch,state:'remote_state_unknown',error:'apply_failed'}])});
  assert(review.innerHTML.includes('状态未知')&&review.innerHTML.includes('apply_failed'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,state:'failed',executed:false,exit_code:null,error:'compute_review_required',streams:{}}])});
  assert(activity.innerHTML.includes('未执行')&&!activity.innerHTML.includes('1 项失败'));
  app.deliver({method:'ui/notifications/tool-cancelled',params:{}});
  assert(app.el('error').textContent.includes('远端状态待确认'));

  const partial={...patch,check_only:false,diff_summary:{...patch.diff_summary,complete:false},streams:{diff:{content:diff,next_cursor:90,truncated:true}}};
  app.deliver({method:'ui/notifications/tool-result',params:wrap([partial])});
  assert(review.innerHTML.includes('（片段）')&&review.innerHTML.includes('data-stream="diff"'));
  // Metadata bootstrap can show a saved card with no additional model request.
  const saved=boot(wrap([command])._meta);
  assert(saved.el('activity').innerHTML.includes('远程活动'));
  assert(!saved.messages.some(x=>x.method==='tools/call'));
  // Optional actual MCP wire metadata from the Python integration test.
  if(process.argv[2]){
    const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
    const actual=boot(fixture.meta);
    const markup=actual.el(fixture.tool==='apply_patch'?'review':'activity').innerHTML;
    assert(markup.includes(fixture.label),markup);
    assert(!markup.includes('已完成'));
    assert(!markup.includes('>✓<'));
    if(fixture.tool==='exec_command')assert(markup.includes(`exit ${fixture.exit_code ?? '–'}`));
    assert(!actual.messages.some(x=>x.method==='tools/call'));
  }
  console.log('Lite frontend: command/diff views, compact text, safe rendering, paging, bootstrap and status passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
