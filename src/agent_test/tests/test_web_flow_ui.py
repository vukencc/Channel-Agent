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
  perform,snapshot,inputField,refreshRun,refreshList,select(id){selected=id},setPicker(fn){pickDirectory=fn},
  draft:()=>drafts.get(active),rev:()=>revision,summary:()=>summaries
}; window.FlowUI = {`);
global.window={};global.document={getElementById:()=>null};
vm.runInThisContext(source);
const harness=window.flowStateHarness;
const flow={id:'a'.repeat(32),title:'受控流程',nodes:[],edges:[],run:null,revision:1};
''' + body)


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
  $,async(url,options)=>{sent={url,body:options.body};return {id:'created'}}, {sessions:new Map()},()=>{},()=>{},()=>{},
  (id,error)=>{if(error)throw error},{FlowUI:{openCreated(){}}});
(async()=>{
  for(const mode of ['agent','flow'])for(const policy of ['readonly','']){
    $('new-mode').value=mode;$('new-flow-policy').value=policy;
    await create({preventDefault(){}});
    assert.equal(sent.url,mode==='agent'?'/api/sessions':'/api/flows');
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
