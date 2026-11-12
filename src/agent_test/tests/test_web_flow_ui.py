"""Node 执行浏览器共用图校验；不是实际浏览器或真实模型测评。"""
from pathlib import Path
from html.parser import HTMLParser
import shutil
import subprocess


def run_flow_js(tmp_path, body):
    node = shutil.which('node')
    assert node, '必须通过 Linux/nvm PATH 解析 Node，不能静默跳过'
    asset = Path(__file__).resolve().parents[2] / 'agent_frontend' / 'web' / 'static' / 'flow.js'
    script = tmp_path / 'flow-behavior.cjs'
    script.write_text("const assert = require('node:assert/strict');\n"
                      "const api = require(process.argv[2]);\n" + body, encoding='utf-8')
    result = subprocess.run([node, str(script), str(asset)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def run_flow_state_js(tmp_path, body):
    # 受控状态机 harness：仅屏蔽 DOM 绘制，执行实际异步请求及 SSE 合并代码。
    run_flow_js(tmp_path, '''
const fs=require('node:fs'), vm=require('node:vm');
let source=fs.readFileSync(process.argv[2],'utf8');
assert.ok(source.includes('function render() {'));
assert.ok(source.includes('window.FlowUI = {'));
source=source.replace('function render() {','function render() { if(window.renderFields) window.renderFields(); return;');
source=source.replace('window.FlowUI = {', `window.flowStateHarness={
  setup(b,f){bridge=b;active=f.id;drafts.set(f.id,f)},
  perform,snapshot,inputField,refreshRun,refreshList,setInteraction(fn){cancelNodeDrag=fn},select(id){selected=id},setPicker(fn){pickDirectory=fn},
  draft:()=>drafts.get(active),rev:()=>revision,summary:()=>summaries,projects:()=>projects,markProject:()=>projectDirty.add(active),switchTo(f){active=f.id;drafts.set(f.id,f)}
}; window.FlowUI = {`);
global.window={};global.document={getElementById:()=>null};
vm.runInThisContext(source);
const harness=window.flowStateHarness;
const flow={id:'a'.repeat(32),project_id:'project-1',title:'受控流程',nodes:[],edges:[],run:null,revision:1};
''' + body)


def test_contract_draft_keeps_invalid_input_and_clears(tmp_path):
    run_flow_js(tmp_path, r'''
const {flowContractDraft, flowContractInvalid, flowDefinition} = api;
const node = {id:'one', handoff_contract:flowContractDraft('{"fields":')};
const flow = {id:'a', nodes:[node], edges:[]};
assert.equal(node.handoff_contract, '{"fields":');
assert.equal(flowContractInvalid(flow), true);
assert.equal(flowContractDraft('{"fields":{"answer":{"type":"bad"}}}'), '{"fields":{"answer":{"type":"bad"}}}');
assert.equal(flowContractDraft('{"fields":{"answer":{"type":"integer","enum":[true]}}}'), '{"fields":{"answer":{"type":"integer","enum":[true]}}}');
for (const spec of [
  {type:'number',enum:[1.5]},
  {type:'integer',enum:[9007199254740992]},
  {type:'integer',enum:[-9007199254740992]},
  {type:'string',enum:['\ud800']},
  {type:'string',enum:['\udc00']},
]) {
  const text = JSON.stringify({fields:{answer:spec}});
  assert.equal(flowContractDraft(text), text);
}
for (const spec of [
  {type:'number'}, {type:'integer',enum:[-9007199254740991,9007199254740991]},
  {type:'string',enum:['😀']},
]) {
  const text = JSON.stringify({fields:{answer:spec}});
  assert.deepEqual(flowContractDraft(text), JSON.parse(text));
}
node.handoff_contract = flowContractDraft('{"fields":{"answer":{"type":"string"}}}');
assert.equal(flowContractInvalid(flow), false);
assert.deepEqual(flowDefinition(flow).nodes[0].handoff_contract, {fields:{answer:{type:'string'}}});
node.handoff_contract = flowContractDraft('  ');
assert.equal(node.handoff_contract, null);
''')


def test_custom_tool_removal_after_invalid_draft_clears_stale_selection(tmp_path):
    run_flow_js(tmp_path, r'''
const {flowSetCustomTools} = api;
const node = {custom_tools: [], tool_names: ['read_file', 'removed_tool']};
const catalog = {tools: [{name:'read_file'}]};
flowSetCustomTools(node, '[{', catalog);
assert.equal(node.custom_tools, '[{');
assert.deepEqual(node.tool_names, ['read_file', 'removed_tool']);
flowSetCustomTools(node, '[]', catalog);
assert.deepEqual(node.tool_names, ['read_file']);
node.tool_names = null;
flowSetCustomTools(node, '[]', catalog);
assert.equal(node.tool_names, null);
''')


def test_designer_dock_keeps_canvas_after_generation_and_ignores_late_reply(tmp_path):
    # 受控 DOM 与异步接口，仅验证 UI 状态；不代表浏览器布局或模型执行。
    run_flow_js(tmp_path, r'''
const fs=require('node:fs'),vm=require('node:vm');
let source=fs.readFileSync(process.argv[2],'utf8');
source=source.replace('function render() {', 'function render() { window.rendered.push({active, designerVisible}); return;');
source=source.replace('window.FlowUI = {', `window.harness={
 setup(b,f){bridge=b;active=f.id;drafts.set(f.id,f);designerVisible=true;designerDefaultAvailable=true},
 drawDesigner,open,openCreated,hide,state:()=>({active,designerVisible,history:designerHistory}),
}; window.FlowUI = {`);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.listeners={};this.classList={add(){},remove(){}};this.dataset={};this.value='';}
 append(...items){for(const item of items){item.parent=this;this.children.push(item)}}
 replaceWith(item){const i=this.parent.children.indexOf(this);this.parent.children[i]=item;item.parent=this.parent}
 replaceChildren(...items){this.children=[];this.append(...items)}
 addEventListener(name,fn){this.listeners[name]=fn}
 setAttribute(){} click(){return this.listeners.click?.({})}
 querySelector(selector){if(selector==='.flow-designer-panel')return this.children.find(x=>x.className==='flow-designer-panel')||null;return null}
}
const layout=new Element('div'),root=new Element('section');root.querySelector=s=>s==='.flow-layout'?layout:null;
const list=new Element('div');const elements={'flow-view':root,'flow-list':list};
global.document={createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text}),getElementById:id=>elements[id]||null,body:new Element('body')};
global.window={rendered:[]};global.requestAnimationFrame=()=>{};
vm.runInThisContext(source);
const h=window.harness, original={id:'old',title:'旧草稿',nodes:[],edges:[]}, created={id:'new',title:'生成副本',nodes:[],edges:[]};
let resolveChat, imports=0, chatId, cancelOptions;
const bridge={enter(){},models:()=>[],toast(){},api(path,options){
 if(path==='/api/flow-designer/chat'){chatId=options.body.request_id;return new Promise(resolve=>{resolveChat=resolve});}
 if(path==='/api/flow-designer/cancel'){cancelOptions=options;return Promise.resolve({cancelled:true});}
 if(path==='/api/flows/import'){imports++;return Promise.resolve(created)}
 if(path==='/api/flows')return Promise.resolve({flows:[],flows_revision:0});
 return Promise.resolve(original);
}};
function find(node,text){if(node.textContent===text)return node;for(const child of node.children||[]){const found=find(child,text);if(found)return found;}return null}
function findTag(node,tag){if(node.tag===tag)return node;for(const child of node.children||[]){const found=findTag(child,tag);if(found)return found;}return null}
function send(){h.drawDesigner();const panel=layout.querySelector('.flow-designer-panel'),input=findTag(panel,'textarea');input.value='设计';input.listeners.input();return find(panel,'发送').click()}
(async()=>{
 h.setup(bridge,original);
 let pending=send();resolveChat({reply:'已设计',template:{format:'agent-flow'}});await pending;
 assert.equal(imports,1);assert.equal(h.state().active,'new');assert.equal(h.state().designerVisible,true);
 assert.equal(h.state().history.at(-1).display,'已设计');
 pending=send();h.open('old');resolveChat({reply:'迟到',template:{format:'agent-flow'}});await pending;
 assert.equal(imports,1,'切换后迟到的回复不得导入');assert.equal(h.state().active,'old');assert.equal(h.state().designerVisible,false);
 assert.equal(cancelOptions.body.request_id,chatId);assert.equal(cancelOptions.keepalive,true);assert.equal(cancelOptions.signal,undefined,'取消请求不能使用已中止的 signal');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_designer_context_prioritizes_latest_complete_template_and_warns_on_overflow(tmp_path):
    run_flow_js(tmp_path, r'''
const {flowDesignerReply, flowDesignerTrim} = api;
const template={format:'agent-flow',nodes:[{prompt:'x'.repeat(9000)}]};
const entry=flowDesignerReply('r'.repeat(9000),template);
assert.deepEqual(JSON.parse(entry.content),template,'组合超限时完整模板优先');
assert.equal(entry.display,'r'.repeat(9000));assert.equal(entry.warning,'');
const oversized=flowDesignerReply('说明',{nodes:[{prompt:'x'.repeat(17000)}]});
assert.ok(oversized.warning.includes('下一轮'));assert.equal(oversized.display,'说明');
assert.ok(!flowDesignerTrim([entry,oversized,{role:'user',content:'继续'}]).includes(entry),
  '最新模板过大时不能把旧图误当最新图送入下一轮');
const history=[{role:'assistant',content:'older',template:true},entry,
  ...Array.from({length:50},(_,i)=>({role:'user',content:'q'.repeat(1800)+i}))];
const trimmed=flowDesignerTrim(history);
assert.ok(trimmed.includes(entry),'保留最新完整模板');
assert.equal(trimmed.at(-1),history.at(-1),'保留最新用户消息');
assert.ok(trimmed.length<=40 && trimmed.reduce((sum,item)=>sum+item.content.length,0)<=64000);
''')


def test_flow_entered_during_connect_is_not_replaced_by_initial_session(tmp_path):
    run_flow_js(tmp_path, r'''
const fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(path.dirname(process.argv[2]),'app.js'),'utf8');
const definition=source.match(/  async function connect\(\) \{[\s\S]*?\n  \}/)[0];
let release, selected=0, welcome=0, active=false;
const state={ready:false,connecting:false,retryCount:0,sessions:new Map([['session',{}]])};
const window={FlowUI:{isActive:()=>active,refreshModels(){}}};
const $=()=>({open:false});
const connect=new Function('state','window','$','jsonApi','connectionStatus','renderArchiveLink','showError','applySnapshot','sessionStorage','selectSession','showWelcome','startEvents','refreshTasks','loadModels','fillSelect','renderConversationStatus','renderConnectionActions','clearTimeout','clearInterval','setInterval', 'return ('+definition+')')(
 state,window,$,url=>url==='/api/meta'?new Promise(r=>{release=r}):Promise.resolve({sessions:[]}),()=>{},()=>{},()=>{},()=>{},
 {getItem:()=>null},()=>selected++,()=>welcome++,()=>{},()=>{},()=>Promise.resolve(),()=>{},()=>{},()=>{},()=>{},()=>{},()=>0);
(async()=>{const pending=connect();active=true;release({models:[]});await pending;assert.equal(selected+welcome,0,'启动响应不得关闭已打开的 Flow');})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_model_labels_refresh_in_place_preserving_selection_and_input(tmp_path):
    run_flow_js(tmp_path, r'''
const fs=require('node:fs'),vm=require('node:vm');let source=fs.readFileSync(process.argv[2],'utf8');
source=source.replace('window.FlowUI = {','window.harness={setup(b){bridge=b},refreshModels}; window.FlowUI = {');
const select={dataset:{flowModel:'designer'},value:'runtime-1',replaceChildren(...options){this.options=options}};
const draft={value:'未发送内容'},root={querySelectorAll:()=>[select]};
global.window={};global.document={getElementById:()=>root,createElement:()=>({})};vm.runInThisContext(source);
window.harness.setup({models:()=>[{value:'runtime-1',label:'友好名称 · 模型'}]});
window.harness.refreshModels();
assert.equal(select.value,'runtime-1');assert.equal(select.options[1].textContent,'友好名称 · 模型');
assert.equal(draft.value,'未发送内容','更新模型选项不能重绘输入框');
''')


def test_flow_graph_connections_allow_diamonds_and_reject_cycles(tmp_path, monkeypatch, sandbox_env):
    node = shutil.which('node')
    assert node, '必须通过 Linux/nvm PATH 解析 Node，不能静默跳过'
    asset = Path(__file__).resolve().parents[2] / 'agent_frontend' / 'web' / 'static' / 'flow.js'
    script = tmp_path / 'flow-graph.cjs'
    script.write_text('''
const assert = require('node:assert/strict');
const {flowCanConnect} = require(process.argv[2]);
const nodes = ['a','b','c','d'].map(id => ({id}));
const edges = [{source:'a',target:'b'}, {source:'a',target:'c'}, {source:'b',target:'d'}];
const before = JSON.stringify({nodes,edges});
assert.equal(flowCanConnect(nodes, edges, 'c', 'd'), true, '分支可汇合');
assert.equal(flowCanConnect(nodes, edges, 'b', 'b'), false, '拒绝自环');
assert.equal(flowCanConnect(nodes, edges, 'a', 'b'), false, '拒绝重复边');
assert.equal(flowCanConnect(nodes, edges, 'd', 'a'), false, '拒绝传递环');
assert.equal(flowCanConnect(nodes, edges, 'absent', 'a'), false, '源节点必须存在');
assert.equal(flowCanConnect(nodes, edges, 'a', 'absent'), false, '目标节点必须存在');
assert.equal(flowCanConnect(nodes, edges, 'd', 'c'), true, '允许尚未构成环的反向布局边');
assert.equal(JSON.stringify({nodes,edges}), before, '预览校验不能改写草稿');
''', encoding='utf-8')
    result = subprocess.run([node, str(script), str(asset)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_flow_project_preview_normalizes_equivalent_paths_and_rejects_illegal_paths(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, '''
const {flowProjectInfo} = api;
for (const path of ['/project', '/project/', '/project/.', '/project//./']) {
  const flow = {workspace_path:path, nodes:[{id:'a'}, {id:'b',workspace_path:'/project/./'}], edges:[{source:'a',target:'b'}]};
  assert.deepEqual(flowProjectInfo(flow,'a'), {path:'/project',source:'流程默认'});
  assert.deepEqual(flowProjectInfo(flow,'b'), {path:'/project',source:'节点指定'});
}
for (const path of ['relative/project', '~/project', '//project', '///project', '/project/../project', '/project/..']) {
  for (const nodeLevel of [false,true]) {
    const flow = {workspace_path:nodeLevel ? '/valid' : path, nodes:[{id:'a',workspace_path:nodeLevel ? path : null}],edges:[]};
    assert.ok(flowProjectInfo(flow,'a').error, `必须拒绝已知非法路径 ${path}`);
  }
}
''')


def test_flow_project_preview_inheritance_conflict_cycles_and_missing_roots(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, '''
const {flowProjectInfo} = api;
const flow = {workspace_path:'/project', nodes:['a','b','c','d'].map(id=>({id})), edges:[
  {source:'a',target:'b'}, {source:'a',target:'c'}, {source:'b',target:'d'}, {source:'c',target:'d'}]};
const before = JSON.stringify(flow);
assert.deepEqual(flowProjectInfo(flow,'d'), {path:'/project',source:'继承上游'});
assert.equal(JSON.stringify(flow),before,'预览不能写回草稿');
flow.nodes[0].workspace_path='/another';
assert.deepEqual(flowProjectInfo(flow,'d'), {path:'/another',source:'继承上游'});
flow.nodes[2].workspace_path='/conflict';
assert.ok(flowProjectInfo(flow,'d').error.includes('冲突'));
flow.nodes[2].workspace_path=null;
flow.edges.push({source:'d',target:'a'});
assert.ok(flowProjectInfo(flow,'d').error.includes('循环'));
assert.ok(flowProjectInfo({nodes:[{id:'a'}],edges:[]},'a').error);
assert.ok(flowProjectInfo({nodes:[],edges:[]},'absent').error);
assert.ok(flowProjectInfo({nodes:[{id:'a'}],edges:[{source:'absent',target:'a'}]},'a').error);
''')


def test_flow_project_preview_resolves_shared_ancestors_once(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, '''
const {flowProjectInfo} = api;
let reads = 0;
const nodes = [], edges = [];
for (let level=0;level<24;level++) {
  for (const side of ['a','b']) {
    const id=`${level}${side}`;
    nodes.push({id, get workspace_path(){reads++;return null;}});
    if (level) for (const parent of ['a','b']) edges.push({source:`${level-1}${parent}`,target:id});
  }
}
assert.deepEqual(flowProjectInfo({workspace_path:'/project',nodes,edges},'23a'), {path:'/project',source:'继承上游'});
assert.ok(reads<=nodes.length,`共享祖先应缓存，每节点读取一次；实际 ${reads}`);
''')


def test_flow_canvas_bounds_include_negative_and_large_coordinates_without_mutation(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, '''
const {flowCanvasBounds} = api;
assert.deepEqual(flowCanvasBounds([]), {minX:0,minY:0,width:600,height:400});
const nodes=[{id:'negative',x:-6000,y:-4500},{id:'large',x:7200,y:9500}];
const before=JSON.stringify(nodes), box=flowCanvasBounds(nodes);
assert.ok(box.minX<0 && box.minY<0);
for(const node of nodes) {
  assert.ok(node.x-80>=box.minX && node.y-80>=box.minY,'左上方留白');
  assert.ok(node.x+180+80<=box.minX+box.width,'右方节点与留白完整可见');
  assert.ok(node.y+80+80<=box.minY+box.height,'下方节点与留白完整可见');
}
assert.equal(JSON.stringify(nodes),before,'自动适配不得篡改持久化坐标');
''')


def test_flow_pending_run_catches_final_sse_instead_of_staying_queued(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, '''
(async()=>{
  let release, detailGets=0;
  const summaries={flows:[{id:flow.id,status:'completed'}],flows_revision:9};
  harness.setup({
    api(path,options){
      if(options) return new Promise(resolve=>{release=resolve});
      if(path==='/api/flows') return Promise.resolve(summaries);
      detailGets++;return Promise.resolve({...flow,run:{status:'completed'}});
    },toast(message){throw new Error(message)}
  },flow);
  const work=harness.perform('run');
  harness.snapshot(summaries);
  release({...flow,run:{status:'queued'}});
  await work;
  for(let i=0;i<4;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.ok(detailGets>=1,'pending期间收到最终SSE后必须补读详情');
  assert.equal(harness.draft().run.status,'completed','最终状态不能被较早queued响应覆盖');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_flow_snapshot_accepts_new_service_revision_after_restart(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, '''
const oldId=flow.id,newId='b'.repeat(32);
harness.snapshot({flows:[{id:oldId,status:'completed'}],flows_revision:9,flows_epoch:'old-instance'});
harness.snapshot({flows:[{id:newId,status:'idle'}],flows_revision:8,flows_epoch:'old-instance'});
assert.equal(harness.rev(),9,'同实例乱序帧不能回滚revision');
assert.deepEqual(harness.summary().map(row=>row.id),[oldId]);
harness.snapshot({flows:[{id:newId,status:'idle'}],flows_revision:0,flows_epoch:'new-instance'}, {authoritative:true});
assert.equal(harness.rev(),0,'服务重启计数归零后应恢复同步');
assert.deepEqual(harness.summary().map(row=>row.id),[newId],'重开服务不能永久停留旧Flow列表');
harness.snapshot({flows:[{id:oldId,status:'completed'}],flows_revision:99,flows_epoch:'old-instance'});
assert.equal(harness.rev(),0,'旧实例迟到的高revision帧也不能回滚新服务');
assert.deepEqual(harness.summary().map(row=>row.id),[newId]);
''')


def test_flow_definition_request_excludes_large_run_and_preserves_independent_draft(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, '''
const {flowDefinition}=api;
const flow={id:'a'.repeat(32),title:'流程',workspace_path:'/project',handoff:'背景',
  execution_environment:'sandbox',permission_policy:'readonly',model_profile:'saved-model',revision:7,
  nodes:[{id:'a',title:'A',prompt:'指令',x:-900,y:4000,handoff:'节点背景',handoff_file:'brief.txt',workspace_path:null,model_profile:null}],
  edges:[],run:{status:'completed',nodes:{a:{output_handoff:'X'.repeat(1100000)}}}};
const before=JSON.stringify(flow),expected=structuredClone(flow);delete expected.run;
const request=flowDefinition(flow);
assert.deepEqual(request,expected,'保存和运行必须保留完整定义及revision');
assert.ok(JSON.stringify(request).length<2000,'大交接运行详情不应进入保存或运行请求');
request.nodes[0].prompt='修改请求';request.edges.push({source:'a',target:'b'});
assert.equal(JSON.stringify(flow),before,'请求必须深拷贝，不能改动草稿');
''')


def test_flow_save_and_run_requests_actually_omit_run_details(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, '''
(async()=>{
  for(const kind of ['save','run']) {
    let sent;
    const original={...flow,run:{status:'completed',nodes:{a:{output_handoff:'X'.repeat(1100000)}}}};
    harness.setup({api(path,options){
      if(options){sent=options.body;return Promise.resolve({...flow,run:{status:'completed'}})}
      return Promise.resolve({flows:[],flows_revision:0});
    },toast(message){throw new Error(message)}},original);
    await harness.perform(kind);
    const definition=kind==='run' ? sent.definition : sent;
    assert.equal(Object.hasOwn(definition,'run'),false,`${kind}实际请求必须剔除run`);
    assert.equal(definition.revision,flow.revision);
    assert.ok(JSON.stringify(sent).length<2000,'请求不能因已有交接正文超过1MiB');
  }
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_flow_directory_picker_updates_current_draft_after_async_refresh(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, '''
class ElementDouble {
  constructor(){this.children=[];this.listeners={};this.dataset={};}
  append(...items){this.children.push(...items);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(name,value){this[name]=value;}
  click(){this.listeners.click();}
}
document.createElement=()=>new ElementDouble();
(async()=>{
  for(const nodeLevel of [false,true]) {
    const original={...flow,id:(nodeLevel?'c':'a').repeat(32),workspace_path:'/old',nodes:[{id:'n',workspace_path:'/old'}]};
    harness.setup({api:()=>Promise.resolve(structuredClone(original))},original);
    harness.select(nodeLevel ? 'n' : null);
    let picker;
    harness.setPicker(options=>{picker=options});
    const parent=new ElementDouble();
    const target=nodeLevel ? original.nodes[0] : original;
    const input=harness.inputField(parent,'目录',target,'workspace_path',{browse:true,nullable:true,node:nodeLevel});
    parent.children[0].children[1].click();
    await harness.refreshRun(original.id);
    assert.notEqual(harness.draft(),original,'SSE详情刷新确实替换了草稿对象');
    assert.equal(picker.valid(),true,'有效选择应持续绑定相同Flow与节点');
    input.value='/new';picker.apply('/new');
    const current=nodeLevel ? harness.draft().nodes[0] : harness.draft();
    assert.equal(current.workspace_path,'/new','目录选择应写入当前草稿对象');
    assert.equal(target.workspace_path,'/old','不能将选择写入已被替换的旧对象');
    harness.setup({}, {...flow,id:'b'.repeat(32)});
    assert.equal(picker.valid(),false,'切换Flow后旧目录选择不得写入新目标');
  }
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_flow_old_instance_late_http_list_and_detail_do_not_override_restart(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, '''
(async()=>{
  const old={flows:[{id:flow.id,status:'queued'}],flows_revision:9,flows_epoch:'old-instance'};
  const fresh={flows:[{id:flow.id,status:'completed'}],flows_revision:0,flows_epoch:'new-instance'};
  harness.snapshot(old);
  let releaseList,releaseDetail,detailGets=0;
  harness.setup({api(path){
    if(path==='/api/flows') return new Promise(resolve=>{releaseList=resolve});
    detailGets++;
    if(detailGets===1) return new Promise(resolve=>{releaseDetail=resolve});
    return Promise.resolve({...flow,run:{status:'completed'}});
  },toast(message){throw new Error(message)}},flow);
  const lateDetail=harness.refreshRun(flow.id),lateList=harness.refreshList();
  harness.snapshot(fresh,{authoritative:true});
  for(let i=0;i<3;i++) await new Promise(resolve=>setImmediate(resolve));
  releaseDetail({...flow,run:{status:'queued'}});releaseList(old);
  await Promise.all([lateDetail,lateList]);
  assert.equal(harness.rev(),0,'旧HTTP列表不能覆盖新实例revision');
  assert.equal(harness.summary()[0].status,'completed','旧HTTP列表不能覆盖新实例概要');
  assert.equal(harness.draft().run.status,'completed','旧HTTP详情不能覆盖新实例运行状态');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


FORM_DOM = '''
class FormElementDouble {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.dataset={};this.isConnected=true;}
  append(...items){this.children.push(...items);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(name,value){this[name]=value;}
  closest(){return ['input','textarea','select'].includes(this.tag)?this:null;}
  replaceChildren(){
    const detach=element=>{element.isConnected=false;element.children.forEach(detach)};
    this.children.forEach(detach);this.children=[];
  }
  focus(){
    const previous=document.activeElement;
    if(previous && previous!==this) previous.listeners.change?.();
    document.activeElement=this.isConnected?this:null;
  }
  fill(value){assert.ok(this.isConnected,'下一个字段不能在blur/change期间被销毁');this.value=value;this.listeners.input();}
}
const view={querySelectorAll:()=>[],querySelector:()=>null};
document.getElementById=id=>id==='flow-view'?view:null;
document.createElement=tag=>new FormElementDouble(tag);
const form=new FormElementDouble();let nameField,promptField,draws=0;
window.renderFields=()=>{
  draws++;form.replaceChildren();
  const node=harness.draft().nodes[0];
  nameField=harness.inputField(form,'名称',node,'title',{node:true});
  promptField=harness.inputField(form,'节点指令',node,'prompt',{node:true,multiline:true});
};
'''


def test_flow_consecutive_name_and_prompt_input_keeps_field_focus_and_saved_content(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, FORM_DOM + '''
(async()=>{
  let sent;
  const original={...flow,nodes:[{id:'n',title:'旧名称',prompt:''}]};
  harness.setup({api(path,options){
    if(options){sent=options.body;return Promise.resolve(structuredClone(harness.draft()))}
    if(path==='/api/flows')return Promise.resolve({flows:[],flows_revision:0});
    return Promise.resolve(structuredClone(harness.draft()));
  },toast(message){throw new Error(message)}},original);
  harness.select('n');window.renderFields();
  nameField.focus();nameField.fill('实现节点');
  const nextField=promptField;
  nextField.focus();nextField.fill('读取项目并实现功能');
  assert.equal(document.activeElement,nextField,'连续输入不能丢失节点指令焦点');
  assert.equal(promptField,nextField,'名称change不能重建正在进入的字段');
  assert.equal(harness.draft().nodes[0].prompt,'读取项目并实现功能');
  await harness.perform('save');
  assert.equal(sent.nodes[0].title,'实现节点');
  assert.equal(sent.nodes[0].prompt,'读取项目并实现功能','实际保存payload必须保留非空指令');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_flow_clean_focused_field_survives_background_detail_refresh(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, FORM_DOM + '''
(async()=>{
  const original={...flow,nodes:[{id:'n',title:'节点',prompt:''}]};
  harness.setup({api:()=>Promise.resolve(structuredClone(original))},original);
  harness.select('n');window.renderFields();
  const input=promptField;input.focus();
  await harness.refreshRun(flow.id);
  assert.equal(harness.draft(),original,'聚焦但尚未输入时后台刷新不能替换字段绑定的草稿');
  assert.equal(document.activeElement,input);
  assert.equal(input.isConnected,true);
  assert.equal(draws,1,'背景详情不能销毁当前聚焦表单');
  input.fill('后台刷新后的新指令');
  assert.equal(harness.draft().nodes[0].prompt,'后台刷新后的新指令');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_new_project_agent_and_flow_submit_selected_permission_or_default(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, r'''
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[2]),'app.js'),'utf8');
const definition=source.match(/  async function createSession\(event\) \{[\s\S]*?\n  \}/)[0];
const controls=new Map();
const $=id=>{if(!controls.has(id))controls.set(id,{value:'',disabled:false,close(){}});return controls.get(id)};
let sent;
const create=new Function('$','jsonApi','state','selectSession','renderRuntime','focusInput','showError','window','return ('+definition+')')(
  $,async(url,options)=>{sent={url,body:options.body};return {id:'created',flow_id:'instance'}}, {sessions:new Map()},()=>{},()=>{},()=>{},
  (id,error)=>{if(error)throw error},{FlowUI:{open(){}}});
(async()=>{
  for(const mode of ['agent','flow'])for(const policy of ['readonly','']){
    $('new-mode').value=mode;$('new-flow-policy').value=policy;$('new-flow-template').value='template';
    await create({preventDefault(){}});
    assert.equal(sent.url,mode==='agent'?'/api/sessions':'/api/projects');
    if(mode==='flow') assert.equal(sent.body.flow_id,'template');
    assert.equal(sent.body.permission_policy,policy||null,`${mode}必须提交用户所选权限或null默认`);
  }
})().catch(error=>{console.error(error);process.exitCode=1});
''')


def test_new_project_permission_control_is_visible_in_classic_agent_mode(tmp_path, monkeypatch, sandbox_env):
    class Controls(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.parents = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if attributes.get('id') == 'new-flow-policy':
                self.parents = self.stack[:] + [attributes]
            if tag not in {'input', 'meta', 'link', 'br', 'hr', 'img'}:
                self.stack.append(attributes)

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

    asset = Path(__file__).resolve().parents[2] / 'agent_frontend' / 'web' / 'static' / 'index.html'
    parser = Controls()
    parser.feed(asset.read_text(encoding='utf-8'))
    assert parser.parents is not None
    assert not any('hidden' in attributes for attributes in parser.parents), '默认Agent模式下权限控件不能藏在Flow专属区域'


def test_new_project_mode_switch_updates_labels_and_preserves_selected_policy(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, r'''
const fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[2]),'app.js'),'utf8');
const definition=source.match(/  function updateNewMode\(\) \{[\s\S]*?\n  \}/)[0];
const controls=new Map();
const $=id=>{if(!controls.has(id))controls.set(id,{value:'',hidden:false,textContent:''});return controls.get(id)};
const update=new Function('$','setText','return ('+definition+')')($,(id,value)=>{$(id).textContent=value});
$('new-flow-policy').value='readonly';
for(const mode of ['flow','agent','flow']){
  $('new-mode').value=mode;update();
  const label=mode==='flow'?'Flow':'Agent';
  assert.ok($('new-dialog-title').textContent.includes(label),'对话框标题必须反映实际创建模式');
  assert.ok($('new-submit').textContent.includes(label),'提交按钮必须反映实际创建模式');
  assert.equal($('new-flow-options').hidden,mode!=='flow','仅Flow补充背景随模式切换');
  assert.equal($('new-flow-policy').value,'readonly','切换模式不能抹去已选择权限');
  assert.equal($('new-flow-policy').hidden,false);
}
''')


def test_template_cannot_run_from_flow_ui(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, """
let calls=[];
delete flow.project_id;
harness.setup({api:async (...args)=>{calls.push(args);return flow},toast:()=>{}},flow);
(async()=>{await harness.perform("run");assert.equal(calls.length,0);})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_project_configuration_is_saved_before_running(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, """
let calls=[];
flow.project={id:'project-1',revision:4,permission_policy:'readonly',execution_environment:'sandbox',model_profile:null,handoff:'背景'};
harness.setup({api:async (path,options)=>{calls.push({path,options});if(path.startsWith('/api/projects/'))return {...flow.project,revision:5};if(options)return flow;return {flows:[],flows_revision:0}},toast:()=>{}},flow);
harness.markProject();
(async()=>{await harness.perform('run');assert.equal(calls[0].path,'/api/projects/project-1');assert.equal(calls[0].options.body.revision,4);assert.equal(calls[0].options.body.permission_policy,'readonly');assert.ok(calls[1].path.endsWith('/run'));assert.equal(Object.hasOwn(calls[1].options.body.definition,'project'),false)})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_old_snapshot_cannot_replace_projects(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, """
harness.setup({api:async()=>{},toast:()=>{}},flow);
harness.snapshot({flows:[],projects:[{id:'new'}],flows_revision:5,flows_epoch:'epoch'}, {authoritative:true});
harness.snapshot({flows:[],projects:[{id:'old'}],flows_revision:4,flows_epoch:'epoch'});
assert.equal(harness.projects()[0].id,'new');
harness.snapshot({flows:[],projects:[{id:'foreign'}],flows_revision:8,flows_epoch:'other'});
assert.equal(harness.projects()[0].id,'new');
""")


def test_created_instance_summary_is_not_a_template(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, """
const fs=require('node:fs'),vm=require('node:vm');
let source=fs.readFileSync(process.argv[2],'utf8');
source=source.replace('open(f.id, { preserveDesigner }); refreshList();','');
source=source.replace('window.FlowUI = {','window.harness={openCreated,summary:()=>summaries}; window.FlowUI = {');
global.window={};global.document={getElementById:()=>null};vm.runInThisContext(source);
window.harness.openCreated({id:'instance',title:'项目',project_id:'project'});
assert.equal(window.harness.summary()[0].project_id,'project');
""")


def test_project_save_response_does_not_overwrite_switched_flow(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, """
let release, calls=[];
flow.project={id:'project-1',revision:4,permission_policy:'readonly',execution_environment:'sandbox',model_profile:null,handoff:'A'};
harness.setup({api:(path,options)=>{calls.push({path,options});if(path.startsWith('/api/projects/'))return new Promise(resolve=>{release=resolve});if(options)return Promise.resolve({...flow});return Promise.resolve({flows:[],flows_revision:0})},toast:()=>{}},flow);
harness.markProject();
(async()=>{const pending=harness.perform('run');const other={id:'b',project_id:'project-2',project:{id:'project-2',revision:8},nodes:[],edges:[],revision:1};harness.switchTo(other);release({...flow.project,revision:5});await pending;assert.equal(harness.draft().project.id,'project-2');assert.equal(harness.draft().project.revision,8);assert.ok(calls.some(call=>call.path.endsWith('/run')&&call.path.includes(flow.id)))})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_directory_patch_preserves_unsaved_project_settings(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, """
const draft={revision:3,handoff:'新背景',permission_policy:'readonly',model_profile:'new',execution_environment:'local'};
const response={revision:4,workspace_path:'/new',handoff:'旧背景',permission_policy:'smart',model_profile:'old',execution_environment:'sandbox'};
const merged=api.flowMergeProject(draft,response,true);
assert.equal(merged.revision,4);assert.equal(merged.workspace_path,'/new');assert.equal(merged.handoff,'新背景');
assert.equal(merged.permission_policy,'readonly');assert.equal(merged.model_profile,'new');assert.equal(merged.execution_environment,'local');
assert.equal(api.flowMergeProject(draft,response,false).handoff,'旧背景');
""")


def test_flow_heading_identifies_the_project(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, """
assert.equal(api.flowDisplayTitle({title:'模板',project_id:'p',project:{title:'项目 A'}}),'项目 A');
assert.equal(api.flowDisplayTitle({title:'模板',project_id:null}),'模板');
""")


def test_flow_edges_fill_canvas_and_drag_preview_is_live():
    static = Path(__file__).resolve().parents[2] / "agent_frontend" / "web" / "static"
    css = (static / "flow.css").read_text(encoding="utf-8")
    js = (static / "flow.js").read_text(encoding="utf-8")
    assert "width: 100%; height: 100%" in css.split(".flow-edges {")[1].split("}")[0]
    assert "flow-edge-preview" in js
    assert "closest(\".input-port\")" in js
    assert "pointercancel" in js


def test_flow_drag_expands_stage_at_scaled_coordinates(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, """
const stage={style:{}}, canvas={style:{}};
api.flowExpandDragCanvas(stage,canvas,{minX:-80,minY:-20,width:600,height:400},{x:900,y:700},0.5);
assert.equal(stage.style.width, \"620px\");
assert.equal(stage.style.height, \"440px\");
assert.equal(canvas.style.width, \"1240px\");
assert.equal(canvas.style.height, \"880px\");
""")


def test_flow_single_node_positive_movement_keeps_origin(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, """
const start=api.flowCanvasBounds([{x:80,y:80}]);
const moved=api.flowCanvasBounds([{x:820,y:420}]);
assert.equal(start.minX,0); assert.equal(moved.minX,0);
assert.equal(start.minY,0); assert.equal(moved.minY,0);
const saved=JSON.parse(JSON.stringify({nodes:[{x:820,y:420}]}));
assert.deepEqual(api.flowCanvasBounds(saved.nodes),moved);
""")


def test_flow_escape_cancels_connection_before_pointerup(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, """
let pending=true, prevented=false, edges=0;
const cancel=()=>{pending=false};
assert.equal(api.flowCancelConnectionOnEscape({key:\"Escape\",preventDefault(){prevented=true}},cancel),true);
if(pending) edges++;
assert.equal(prevented,true); assert.equal(edges,0);
assert.equal(api.flowCancelConnectionOnEscape({key:\"Enter\"},cancel),false);
""")


def test_flow_refresh_preserves_inflight_node_drag(tmp_path, monkeypatch, sandbox_env):
    run_flow_state_js(tmp_path, """
let release, renders=0; window.renderFields=()=>renders++;
harness.setup({api:()=>new Promise(resolve=>release=resolve),toast:()=>{}},flow);
(async()=>{const pending=harness.refreshRun(flow.id);harness.setInteraction(()=>{});release({...flow,title:\"server\",run:null});await pending;assert.equal(harness.draft(),flow);assert.equal(renders,0)})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_node_drag_cancel_redraws_connected_edges(tmp_path, monkeypatch, sandbox_env):
    run_flow_js(tmp_path, r"""
const fs=require("node:fs"); const src=fs.readFileSync(process.argv[2],"utf8").replace(/\r\n/g,"\n");
const start=src.indexOf("      card.addEventListener(\"pointerdown\", (event) => {"+String.fromCharCode(10)+"        if (locked()");
const end=src.indexOf(String.fromCharCode(10)+"      });",start); assert.ok(start>=0 && end>start);
const handler=src.slice(start,end)+String.fromCharCode(10)+"      });";
const setup=new Function("ctx","with(ctx){"+handler+"}");
for (const kind of ["pointercancel","blur"]) {
 const events={},node={id:"a",x:0,y:0},flow={id:"flow",nodes:[node],edges:[{source:"a",target:"b"}]};
 let onDown,renders=0; const card={style:{},addEventListener(name,fn){onDown=fn}};
 const ctx={card,portOut:{},window:{addEventListener(n,fn){events[n]=fn},removeEventListener(n){delete events[n]}},
  locked:()=>false,cancelConnection:null,cancelNodeDrag:null,viewport:{scrollLeft:0,scrollTop:0},
  node,f:flow,active:flow.id,drafts:new Map([[flow.id,flow]]),scale:1,bounds:{minX:0,minY:0,width:600,height:400},
  stage:{style:{}},canvas:{style:{}},flowExpandDragCanvas:api.flowExpandDragCanvas,mark(){},render(){renders++},
  selected:null,selectedEdge:null,inspectorModes:new Map(),selectionVersion:0};
 setup(ctx); onDown({button:0,target:card,clientX:0,clientY:0,pointerId:1,preventDefault(){}});
 events.pointermove({pointerId:1,clientX:50,clientY:0}); assert.equal(node.x,50);
 events[kind](kind==="blur"?{}:{pointerId:1});
 assert.equal(renders,1); assert.equal(events.pointerup,undefined);
}
""")
