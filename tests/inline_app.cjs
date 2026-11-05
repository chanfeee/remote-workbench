// A host bridge simulation: no browser, SSH or model calls.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
class Element {
  constructor(tag) {this.tag = tag; this.children = []; this.handlers = {};this.textContent = '';}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren() {this.children = [];}
  addEventListener(name, fn) {this.handlers[name] = fn;}
}
const el = {cards:new Element('div'),error:new Element('p')};
const messages = [], handlers = {}, timers = new Map();
const parent = {postMessage:msg => messages.push(msg)};
let timerId = 0;
const context = {console,Map,JSON,Number,Object,Error,String,Promise,
  document:{getElementById:id=>el[id],createElement:tag=>new Element(tag),documentElement:{scrollHeight:300}},
  window:{parent,addEventListener:(event,fn)=>{handlers[event]=fn;}},
  setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id)};
const html = fs.readFileSync(path.join(__dirname,'../remote_workbench/operation_card.html'),'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInNewContext(source,context);
const deliver = data=>handlers.message({source:parent,data:{jsonrpc:'2.0',...data}});
const tick = ()=>new Promise(resolve=>setImmediate(resolve));
const find = (root, cls) => {
  if ((root.className || '').split(' ').includes(cls)) return root;
  for (const child of root.children) {const match=find(child,cls);if(match)return match;}
};
const count = root => 1 + root.children.reduce((n,child)=>n+count(child),0);
(async () => {
  const init = messages[0]; assert.equal(init.method,'ui/initialize');
  deliver({id:init.id,result:{}});await tick();
  assert(messages.some(x=>x.method==='ui/notifications/initialized'));
  const hostile = '<img src=x onerror=alert(1)>';
  const payload = {_meta:{'remote-workbench/operation':{steps:[{tool:'exec_command',host:'server',cwd:'/run',command:hostile,
    state:'finished',exit_code:0,operation_id:'a'.repeat(32),output_id:'b'.repeat(32),
    streams:{stdout:{content:hostile,next_cursor:10,truncated:true}}}]}}};
  deliver({method:'ui/notifications/tool-result',params:payload});
  assert.equal(find(el.cards,'primary').textContent,hostile);
  const details = find(el.cards,'stream'),pre=details.children[1],more=details.children[2];
  const waiting = more.handlers.click();await tick();
  const call = messages.find(x=>x.method==='tools/call');
  assert.equal(call.params.name,'read_output');assert.equal(call.params.arguments.cursor,10);
  deliver({id:call.id,result:{content:[{type:'text',text:JSON.stringify({content:'next',next_cursor:14,truncated:false})}]}});
  await waiting;assert.equal(pre.textContent,hostile+'next');assert(more.hidden);
  assert(messages.every(x=>!['ui/message','ui/update-model-context'].includes(x.method)));
  assert(messages.filter(x=>x.method==='tools/call').every(x=>x.params.name==='read_output'));
  deliver({method:'ui/notifications/tool-result',params:{_meta:{'remote-workbench/operation':{steps:[{
    tool:'apply_patch',host:'server',cwd:'/run',exit_code:0,state:'success',check_only:true,
    diff_summary:{complete:true,files:[{path:'sample.py',added:1,deleted:1}]},
    streams:{diff:{content:'--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-old\n+'+hostile+'\n',next_cursor:90,truncated:false}}
  }]}}}});
  assert.equal(find(el.cards,'file-path').textContent,'sample.py');
  assert.equal(find(el.cards,'add').textContent,'+1');
  assert.equal(find(el.cards,'added').textContent,'+'+hostile);
  assert.equal(find(el.cards,'deleted').textContent,'-old');
  assert.equal(find(el.cards,'count').textContent,'执行 0 条命令');
  assert.equal(find(el.cards,'section').children[0].textContent,'补丁预览 ');
  const before=count(el.cards);
  // Messages from a different frame cannot replace this chat's operation.
  handlers.message({source:{},data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{}}});
  assert.equal(count(el.cards),before);
  // Opening saved cards can bootstrap from host metadata without a new model turn.
  const bootCards=new Element('div'),bootMessages=[];
  vm.runInNewContext(source,{...context,window:{parent:{postMessage:x=>bootMessages.push(x)},openai:{toolResponseMetadata:payload._meta},addEventListener:()=>{}},document:{...context.document,getElementById:id=>id==='cards'?bootCards:new Element('div')}});
  assert.equal(find(bootCards,'primary').textContent,hostile);
  console.log('Inline bridge: initialization, rendering, bounded paging and parent isolation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
