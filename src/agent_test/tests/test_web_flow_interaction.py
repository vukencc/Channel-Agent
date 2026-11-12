"""受控 DOM 验证设计请求清空；布局仍需实际浏览器检查。"""
import os
from pathlib import Path
import shutil
import subprocess


def test_designer_clear_cancels_and_discards_late_reply(tmp_path):
    node = shutil.which("node")
    assert node
    asset = Path(os.environ.get("FLOW_TEST_ASSET", Path(__file__).resolve().parents[2] / "agent_frontend/web/static/flow.js"))
    script = tmp_path / "designer.cjs"
    script.write_text(r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let source=fs.readFileSync(process.argv[2],'utf8');
source=source.replace('function render() {', 'function render() { return;');
source=source.replace('window.FlowUI = {', `window.harness={setup(b){bridge=b;active='source';drafts.set(active,{id:active,title:'草稿',nodes:[],edges:[]});designerVisible=true;designerDefaultAvailable=true},drawDesigner,openDesigner,refreshModels,state:()=>({history:designerHistory,draft:designerDraft,model:designerModel,active})}; window.FlowUI = {`);
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.listeners={};this.classList={add(){},remove(){}};this.dataset={};this.value='';}
 append(...items){for(const item of items){item.parent=this;this.children.push(item)}}
 replaceWith(item){const i=this.parent.children.indexOf(this);this.parent.children[i]=item;item.parent=this.parent}
 replaceChildren(...items){this.children=[];this.append(...items)}
 addEventListener(name,fn){this.listeners[name]=fn}
 setAttribute(){} click(){return this.listeners.click?.({})}
 querySelector(selector){return selector==='.flow-designer-panel'?this.children.find(x=>x.className==='flow-designer-panel')||null:null}
}
const layout=new Element('div'),root=new Element('section');root.querySelector=s=>s==='.flow-layout'?layout:null;root.querySelectorAll=()=>[];
global.document={createElement:tag=>new Element(tag),getElementById:id=>id==='flow-view'?root:null,body:new Element('body')};
global.window={};vm.runInThisContext(source);
let resolveChat, cancelled=0, request, registry=[{id:'env',read_only:true,has_api_key:false},{id:'local',read_only:false,has_api_key:false}];
const bridge={models:()=>registry,toast(){},api(path,options){if(path.endsWith('/cancel')){cancelled++;return Promise.resolve({})}if(path==='/api/models')return Promise.resolve({default_model_available:false,models:registry});request=options.body;return new Promise(resolve=>resolveChat=resolve)}};
function find(node,text){if(node.textContent===text)return node;for(const child of node.children||[]){const result=find(child,text);if(result)return result;}return null}
(async()=>{
 const h=window.harness;h.setup(bridge);h.drawDesigner();let panel=layout.querySelector('.flow-designer-panel');
 let input=panel.children.find(x=>x.tag==='textarea');input.value='旧需求';input.listeners.input();
 const pending=find(panel,'发送').click();panel=layout.querySelector('.flow-designer-panel');
 assert.ok(find(panel,'生成中…'),'请求期间显示生成状态');
 const clear=find(panel,'清空');assert.ok(clear,'清空按钮可用');clear.click();
 resolveChat({reply:'迟到回复',template:{format:'agent-flow'}});await pending;
 assert.equal(cancelled,1);assert.deepEqual(h.state().history,[]);assert.equal(h.state().draft,'');
 panel=layout.querySelector('.flow-designer-panel');find(panel,'关闭').click();h.openDesigner();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(h.state().model,'local','无环境默认时显式选择可用运行时模型');
 panel=layout.querySelector('.flow-designer-panel');input=panel.children.find(x=>x.tag==='textarea');input.value='新需求';input.listeners.input();
 const hiddenPending=find(panel,'发送').click();assert.equal(request.model_profile,'local');
 registry=[];h.refreshModels();assert.equal(h.state().model,'');assert.equal(request.model_profile,'local','已发送请求保持原模型');
 panel=layout.querySelector('.flow-designer-panel');find(panel,'关闭').click();resolveChat({reply:'完成'});await hiddenPending;
 assert.equal(cancelled,1,'隐藏不取消请求');assert.equal(h.state().history.at(-1).display,'完成');assert.equal(h.state().active,'source');
 h.openDesigner();await new Promise(resolve=>setImmediate(resolve));assert.ok(find(layout,'助手：完成'));
 panel=layout.querySelector('.flow-designer-panel');input=panel.children.find(x=>x.tag==='textarea');input.value='无模型';input.listeners.input();
 const noModelSend=find(panel,'发送');assert.ok(noModelSend.disabled,'无可用模型时禁止发送');assert.ok(find(panel,'请先在模型管理中配置可用模型。'));
 const prior=request;await noModelSend.click();assert.equal(request,prior,'无模型不得发起请求');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding="utf-8")
    result = subprocess.run([node, str(script), str(asset)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_working_node_context_opens_and_closes_details(tmp_path):
    node = shutil.which("node")
    assert node
    asset = Path(os.environ.get("WORKING_TEST_ASSET", Path(__file__).resolve().parents[2] / "agent_frontend/web/static/working.js"))
    script = tmp_path / "working.cjs"
    script.write_text(r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.listeners={};this.dataset={};this.style={};this.className='';this.hidden=false;this.classList={add(){},remove(){}};}
 append(...items){for(const item of items){item.parent=this;this.children.push(item)}}
 replaceChildren(...items){this.children=[];this.append(...items)}
 addEventListener(name,fn){this.listeners[name]=fn}
 setAttribute(name,value){this[name]=value}
 contains(){return false}
 querySelectorAll(selector){let result=[];for(const child of this.children){if(selector[0]==='.' && child.className.split(' ').includes(selector.slice(1)))result.push(child);result.push(...child.querySelectorAll(selector))}return result}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null}
}
const view=new Element('section'),open=new Element('button'),count=new Element('span');
global.document={getElementById:id=>({'working-view':view,'working-open':open,'working-count':count})[id],createElement:tag=>new Element(tag),body:new Element('body'),addEventListener(){},activeElement:null};
global.window={addEventListener(){}};global.setTimeout=()=>1;global.clearTimeout=()=>{};
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
window.WorkingUI.init({enter(){},api:async()=>({runs:[{run_id:'r',title:'流程',nodes:[{id:'a',title:'分析',status:'running',subagents:[{id:'s',title:'子任务',status:'failed'}]}]}]})});
(async()=>{
 open.listeners.click();await new Promise(resolve=>setImmediate(resolve));
 const node=view.querySelector('.working-node'),pane=view.querySelector('.working-detail');
 assert.ok(pane.hidden,'初始详情关闭');node.listeners.click();assert.ok(pane.hidden,'左键仅选择');
 let prevented=false;node.listeners.contextmenu({preventDefault(){prevented=true}});
 assert.ok(prevented);assert.equal(pane.hidden,false,'右键打开详情');
 function find(node,text){if(node.textContent===text)return node;for(const child of node.children){const result=find(child,text);if(result)return result;}return null}
 assert.ok(find(pane,'子任务'));find(pane,'关闭').listeners.click();assert.ok(pane.hidden,'关闭详情');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding="utf-8")
    result = subprocess.run([node, str(script), str(asset)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_flow_hover_and_registry_refresh(tmp_path):
    node = shutil.which("node")
    assert node
    asset = Path(__file__).resolve().parents[2] / "agent_frontend/web/static/flow.js"
    script = tmp_path / "catalog.cjs"
    script.write_text(r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let source=fs.readFileSync(process.argv[2],'utf8');
source=source.replace('window.FlowUI = {', `window.harness={setup(b){bridge=b;designerModels=[{id:'gone',name:'旧模型'}];designerModel='keep'},modelChoices,refreshModels}; window.FlowUI = {`);
class Select {
 constructor(value,scope){this.value=value;this.dataset={flowModel:scope};this.children=[]}
 replaceChildren(...items){this.children=items}
 append(item){this.children.push(item)}
}
const inspector=new Select('keep','inspector'),designer=new Select('keep','designer'),missing=new Select('gone','inspector');
const root={querySelectorAll:()=>[inspector,designer,missing]};
global.document={getElementById:()=>root,createElement:()=>({})};global.window={};vm.runInThisContext(source);
const tip=flowNodeTip({prompt:'任务',model_profile:'keep'}, {status:'running'}, [['keep','可读模型']]);
assert.ok(tip.includes('运行中'));assert.ok(tip.includes('可读模型'));assert.ok(!tip.includes('子 Agent'));
assert.ok(flowNodeTip({}, {subagent_count:2}, []).includes('子 Agent：2'));
const h=window.harness;h.setup({models:()=>[{value:'keep',label:'可读模型'}]});h.refreshModels();
assert.deepEqual(h.modelChoices().map(([id])=>id),['keep']);
for(const select of [inspector,designer]){assert.equal(select.value,'keep');assert.ok(!select.children.some(o=>o.value==='gone'))}
assert.equal(missing.value,'gone');assert.ok(missing.children.find(o=>o.value==='gone').disabled,'保留草稿引用但不可再选');
''', encoding="utf-8")
    result = subprocess.run([node, str(script), str(asset)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
