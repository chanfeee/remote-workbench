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
  let contentHeight=120,observer;
  const el=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false,handlers:{},removeAttribute(){},getBoundingClientRect(){return {height:contentHeight};},addEventListener(name,fn){this.handlers[name]=fn;}});return elements.get(id);};
  const parent={postMessage:message=>messages.push(message)};
  let serial=0;
  const context={Map,Set,JSON,Number,Array,Object,Boolean,String,Error,Promise,TextEncoder,TextDecoder,
    document:{getElementById:el,documentElement:{scrollHeight:1800}},
    ResizeObserver:class{constructor(fn){observer=fn;}observe(target){assert.equal(target,el('content'));}},
    window:{parent,openai:initial?{toolResponseMetadata:initial}:undefined,addEventListener:(name,fn)=>{events[name]=fn;}},
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id)};
  vm.runInNewContext(source,context);
  const deliver=message=>events.message({source:parent,data:{jsonrpc:'2.0',...message}});
  return {el,messages,events,parent,deliver,
    toggle(root,id,open=true){el(root).handlers.toggle({target:{dataset:{detail:id},open}});return el(id);},
    measure(height){contentHeight=height;observer([{contentRect:{height}}]);}};
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
  assert(activity.innerHTML.includes('命令 <span class="section-count">1</span>'));
  assert(!activity.innerHTML.includes('details class="section"'));
  assert(!activity.innerHTML.includes('class="counts"'));
  assert(activity.innerHTML.includes('exit 0 · 1.25s'));
  assert(activity.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!activity.innerHTML.includes('<img'));
  for(const text of ['读取 0','修改 0','没有文件读取','完整命令','history','operation ','本次操作快照','CLI'])assert(!activity.innerHTML.includes(text),text);
  assert(!activity.innerHTML.includes(command.operation_id));
  const sizes=()=>app.messages.filter(x=>x.method==='ui/notifications/size-changed');
  assert.equal(sizes().at(-1).params.height,120);
  app.measure(520);assert.equal(sizes().at(-1).params.height,520);
  const before=sizes().length;app.measure(520);assert.equal(sizes().length,before);
  // The host's 1800px viewport must not become a minimum after collapse.
  app.measure(96);assert.equal(sizes().at(-1).params.height,96);
  assert(sizes().every(x=>x.params.height<1800));
  const outputBody=app.toggle('activity','rwb-detail-0-command');
  assert(outputBody.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
  const button={dataset:{step:'0',stream:'stdout'},disabled:false,hidden:false};
  const originalStream=JSON.stringify(command.streams);
  const click=activity.handlers.click({target:{closest:()=>button}});await tick();
  const call=app.messages.find(x=>x.method==='tools/call');
  assert.equal(call.params.name,'read_output');assert.equal(call.params.arguments.cursor,10);
  app.deliver({id:call.id,result:{content:[{type:'text',text:JSON.stringify({content:'next',next_cursor:14,truncated:false})}]}});
  await click;
  assert.equal(app.el('rwb-output-0-stdout').textContent,hostile+'next');assert(button.hidden);
  app.toggle('activity','rwb-detail-0-command',false);
  assert.equal(outputBody.innerHTML,'');
  app.toggle('activity','rwb-detail-0-command');
  assert(outputBody.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;next'));
  assert(!outputBody.innerHTML.includes('继续读取'));
  assert.equal(JSON.stringify(command.streams),originalStream);
  const commandMarkup=activity.innerHTML;
  activity.innerHTML='preserved paged view';
  app.deliver({method:'ui/notifications/tool-result',params:wrap([command])});
  assert.equal(activity.innerHTML,'preserved paged view');
  activity.innerHTML=commandMarkup;
  assert(app.messages.every(x=>!['ui/message','ui/update-model-context'].includes(x.method)));
  assert(app.messages.filter(x=>x.method==='tools/call').every(x=>x.params.name==='read_output'));

  const diff='--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-old\n+'+hostile+'\n';
  const patch={tool:'apply_patch',host:'server',cwd:'/run',exit_code:0,state:'success',check_only:true,operation_id:'c'.repeat(32),
    diff_summary:{complete:true,added:1,deleted:1,files:[{path:'sample.py',added:1,deleted:1}]},
    streams:{diff:{content:diff,next_cursor:90,truncated:false}}};
  app.deliver({method:'ui/notifications/tool-result',params:wrap([patch])});
  const review=app.el('review');
  assert(!review.hidden&&activity.hidden);
  assert(review.innerHTML.includes('预览 · 1 个文件变更'));
  assert(review.innerHTML.includes('class="file-change"'));
  assert(!review.innerHTML.includes('class="line added"'));
  const diffBody=app.toggle('review','rwb-detail-0-file-0');
  assert(diffBody.innerHTML.includes('class="line added"')&&diffBody.innerHTML.includes('class="line-code">&lt;img'));
  assert(diffBody.innerHTML.includes('class="line deleted"')&&diffBody.innerHTML.includes('class="line-code">old'));
  assert(!diffBody.innerHTML.includes('--- a/sample.py'));
  assert.equal((diffBody.innerHTML.match(/class="gutter" aria-hidden="true">1</g)||[]).length,2);
  app.toggle('review','rwb-detail-0-file-0',false);
  assert.equal(diffBody.innerHTML,'');
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
  assert(review.innerHTML.includes('查看提交的补丁')&&!review.innerHTML.includes('class="file-change"'));
  const appliedPatch={...patch,check_only:false,error:'partial_apply',applied_diff:true,pending_paths:['pending.txt'],
    diff_summary:{...patch.diff_summary,files:[{path:'sample.py',previous_path:'before.py',added:1,deleted:1}]}};
  app.deliver({method:'ui/notifications/tool-result',params:wrap([appliedPatch])});
  assert(review.innerHTML.includes('待处理 pending.txt')&&review.innerHTML.includes('before.py → sample.py'));
  assert(app.toggle('review','rwb-detail-0-file-0').innerHTML.includes('class="line-code">old'));
  app.deliver({method:'ui/notifications/tool-result',params:wrap([{...command,state:'failed',executed:false,exit_code:null,error:'compute_review_required',streams:{}}])});
  assert(activity.innerHTML.includes('未执行')&&!activity.innerHTML.includes('1 项失败'));
  app.deliver({method:'ui/notifications/tool-cancelled',params:{}});
  assert(app.el('error').textContent.includes('远端状态待确认'));

  const partial={...patch,check_only:false,diff_summary:{...patch.diff_summary,complete:false},streams:{diff:{content:diff,next_cursor:90,truncated:true}}};
  app.deliver({method:'ui/notifications/tool-result',params:wrap([partial])});
  assert(review.innerHTML.includes('（片段）'));
  assert(app.toggle('review','rwb-detail-0-diff').innerHTML.includes('data-stream="diff"'));
  const largePatch={...partial,output_id:patch.operation_id,diff_summary:{
    complete:true,added:1,deleted:2,files:[
      {path:'sample.py',added:1,deleted:1},{path:'empty.txt',added:0,deleted:0},
      {path:'删除.txt',added:0,deleted:1},
    ],
  }};
  app.deliver({method:'ui/notifications/tool-result',params:wrap([largePatch])});
  assert(review.innerHTML.includes('3 个文件变更')&&!review.innerHTML.includes('（片段）'));
  assert(review.innerHTML.includes('empty.txt')&&review.innerHTML.includes('删除.txt'));
  const largeBody=app.toggle('review','rwb-detail-0-diff');
  assert(largeBody.innerHTML.includes('data-stream="diff"')&&!largeBody.innerHTML.includes('删除.txt'));
  const diffButton={dataset:{step:'0',stream:'diff'},disabled:false,hidden:false};
  const moreDiff=review.handlers.click({target:{closest:()=>diffButton}});await tick();
  const diffCall=app.messages.filter(x=>x.method==='tools/call').at(-1);
  assert.equal(diffCall.params.arguments.cursor,90);
  app.deliver({id:diffCall.id,result:{structuredContent:{content:'--- a/删除.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n',next_cursor:150,truncated:false}}});
  await moreDiff;
  assert(app.el('rwb-output-0-diff').innerHTML.includes('class="line-code">removed'));
  assert(diffButton.hidden);
  const indexed=boot();
  indexed.deliver({id:indexed.messages[0].id,result:{}});await tick();
  const later='--- a/删除.txt\n+++ /dev/null\n@@ -47 +0,0 @@\n-removed\n';
  const laterEnd=4200+Buffer.byteLength(later);
  const indexedPatch={...largePatch,diff_summary:{...largePatch.diff_summary,files:[
    {path:'sample.py',added:1,deleted:1,range:[0,4200]},
    {path:'删除.txt',added:0,deleted:1,range:[4200,laterEnd]},
  ]}};
  indexed.deliver({method:'ui/notifications/tool-result',params:wrap([indexedPatch])});
  assert(!indexed.messages.some(x=>x.method==='tools/call'));
  assert(!indexed.el('review').innerHTML.includes('查看逐行 diff'));
  indexed.toggle('review','rwb-detail-0-file-1');await tick();
  const laterCall=indexed.messages.find(x=>x.method==='tools/call');
  assert.equal(laterCall.params.arguments.cursor,4200);
  assert.equal(laterCall.params.arguments.max_bytes,Buffer.byteLength(later));
  indexed.deliver({id:laterCall.id,result:{structuredContent:{content:later,next_cursor:laterEnd,truncated:true}}});
  await tick();
  assert(indexed.el('rwb-output-0-diff-1').innerHTML.includes('aria-hidden="true">47<'));
  assert(indexed.el('rwb-output-0-diff-1').innerHTML.includes('class="line-code">removed'));
  assert(indexed.el('rwb-more-0-diff-1').hidden);
  indexed.toggle('review','rwb-detail-0-file-1',false);
  assert(indexed.toggle('review','rwb-detail-0-file-1').innerHTML.includes('class="line-code">removed'));
  assert.equal(indexed.messages.filter(x=>x.method==='tools/call').length,1);
  // Metadata bootstrap can show a saved card with no additional model request.
  const saved=boot(wrap([command])._meta);
  assert(saved.el('activity').innerHTML.includes('命令 <span class="section-count">1</span>'));
  assert(!saved.messages.some(x=>x.method==='tools/call'));
  // A history snapshot is not necessarily the unwrapped live notification.
  for(const key of ['call_tool_result','mcp_tool_result']){
    const history=boot({status:'success',[key]:wrap([command])});
    assert(!history.el('activity').hidden,key);
    assert(history.el('activity').innerHTML.includes('exit 0 · 1.25s'),key);
    assert(!history.messages.some(x=>x.method==='tools/call'),key);
  }
  // Restored globals can arrive after mount; no replay or network read is needed.
  const delayed=boot();
  delayed.events['openai:set_globals']({detail:{globals:{toolResponseMetadata:{call_tool_result:wrap([patch])}}}});
  assert(!delayed.el('review').hidden);
  assert(delayed.toggle('review','rwb-detail-0-file-0').innerHTML.includes('class="line-code">old'));
  assert(!delayed.messages.some(x=>x.method==='tools/call'));
  const restored=delayed.el('review').innerHTML;
  delayed.events['openai:set_globals']({detail:{globals:{theme:'dark'}}});
  delayed.deliver({method:'ui/notifications/tool-result',params:{content:[{type:'text',text:JSON.stringify(wrap([command]))}]}});
  assert.equal(delayed.el('review').innerHTML,restored);
  // A second instance has independent data; late hydration cannot replace its peer.
  const peer=boot({mcp_tool_result:wrap([{...command,command:'second operation'}])});
  assert(peer.el('activity').innerHTML.includes('second operation'));
  assert(!saved.el('activity').innerHTML.includes('second operation'));
  // Re-delivery of the same snapshot must not discard expanded or paged UI.
  delayed.el('review').innerHTML='preserved interaction';
  delayed.events['openai:set_globals']({detail:{globals:{toolResponseMetadata:{call_tool_result:wrap([patch])}}}});
  assert.equal(delayed.el('review').innerHTML,'preserved interaction');
  const grouped=boot({call_tool_result:wrap([{...command,exit_code:7,
    command:Array.from({length:8},(_,i)=>`${i+1}: printf step-${i+1}`).join('\n'),
    batch_steps:[...Array.from({length:6},()=>({state:'finished',exit_code:0})),
      {state:'finished',exit_code:7},{state:'not_run',exit_code:null}],
    streams:{stdout:{content:'shared batch output',next_cursor:19,truncated:false}}}])});
  const groupMarkup=grouped.el('activity').innerHTML;
  assert(groupMarkup.includes('class="section-count">8</span>'));
  assert(groupMarkup.includes('8: printf step-8'));
  assert.equal((groupMarkup.match(/data-detail=/g)||[]).length,1);
  assert(!groupMarkup.includes('shared batch output'));
  const groupDetails=grouped.toggle('activity','rwb-detail-0-command').innerHTML;
  assert(groupDetails.includes('7. 失败 · exit 7'));
  assert(groupDetails.includes('8. 未执行'));
  assert.equal((groupDetails.match(/shared batch output/g)||[]).length,1);
  const compactPayload=wrap([command,{...command,command:'second saved command'}]);
  compactPayload._meta['remote-workbench/operation'].compact=true;
  const compact=boot({call_tool_result:compactPayload});
  const compactMarkup=compact.el('activity').innerHTML;
  assert(compactMarkup.includes('<details class="activity-group"><summary>查看命令与输出</summary>'));
  assert(compactMarkup.includes('class="section-count">2</span>'));
  assert(compactMarkup.includes('second saved command')&&!compactMarkup.includes('<pre'));
  const edited={...patch,check_only:false};
  const checked=wrap([edited,{...command,streams:{},batch_steps:[
    {state:'finished',exit_code:0},{state:'finished',exit_code:7},{state:'not_run',exit_code:null}
  ],exit_code:7}]);
  checked._meta['remote-workbench/operation'].compact=true;
  const verification=boot({call_tool_result:checked});
  const verificationMarkup=verification.el('activity').innerHTML;
  assert(verification.el('review').innerHTML.includes('1 个文件变更'));
  assert(verificationMarkup.includes('验证')&&verificationMarkup.includes('1 项通过 · 1 项失败 · 未执行'));
  assert(verificationMarkup.includes('role="alert"')&&verificationMarkup.includes('第 2 项 失败'));
  assert(verificationMarkup.includes('<details class="activity-group">'));
  const unverified=boot({call_tool_result:wrap([edited])});
  assert(unverified.el('activity').innerHTML.includes('未运行'));
  const skipped=boot({call_tool_result:wrap([edited,{...command,
    test_summary:{status:'completed',executed:false,passed:0,reported_skips:2}
  }])});
  assert(skipped.el('activity').innerHTML.includes('2 个测试跳过'));
  assert(!skipped.el('activity').innerHTML.includes('1 项通过'));
  const blocked=boot({call_tool_result:wrap([{tool:'apply_patch',executed:false,
    error:'compute_review_required',message:'review <unsafe> command'}])});
  assert(blocked.el('review').innerHTML.includes('role="alert"'));
  assert(blocked.el('review').innerHTML.includes('需要确认'));
  assert(blocked.el('review').innerHTML.includes('review &lt;unsafe&gt; command'));
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
