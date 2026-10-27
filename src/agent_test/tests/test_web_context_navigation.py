"""Context archives and focused workspace controls have bounded, safe navigation."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[3] / "src/agent_frontend/web/static"
DEBUG_STATIC = Path(__file__).resolve().parents[1] / "static"


def function(name, file="app.js"):
    assets = DEBUG_STATIC if file.startswith('debug') else STATIC
    source = (assets / file).read_text(encoding="utf-8")
    match = re.search(r"  (?:async )?function " + name + r"\(.*?\n  \}", source, re.S)
    assert match, f"Missing frontend function: {name}"
    return match.group()


def run_js(source):
    node = shutil.which("node") or shutil.which("nodejs") or shutil.which("node.exe")
    if not node:
        pytest.skip("Node is required for frontend behavior checks")
    result = subprocess.run([node, "-e", source], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


class IDs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.nodes = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.nodes[attrs["id"]] = (tag, attrs)


def test_archive_is_read_only_dialog_and_workspace_link_starts_hidden():
    workspace = IDs((STATIC / "index.html").read_text(encoding="utf-8"))
    assert workspace.nodes["open-context-archive"][0] == "a"
    assert "hidden" in workspace.nodes["open-context-archive"][1]
    debug = IDs((DEBUG_STATIC / "debug.html").read_text(encoding="utf-8"))
    assert debug.nodes["context-archive"][0] == "dialog"
    for name in ("archive-paths", "archive-format", "archive-revision", "archive-summary", "archive-kind", "archive-chunks", "archive-read", "archive-raw", "archive-raw-next", "archive-next", "archive-error"):
        assert name in debug.nodes
    assert not any(tag in {"textarea", "input"} for name, (tag, _) in debug.nodes.items() if name.startswith("archive-"))
    source = (DEBUG_STATIC / "debug.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source
    export = source.split('$("export").addEventListener', 1)[1].split("\n  });", 1)[0]
    assert "JSON.stringify(snapshot, null, 2)" in export
    assert "archive" not in export and "RawHistory" not in export


def test_debug_navigation_and_workspace_archive_link_encode_selected_agent():
    run_js(function("debugNavigation", "debug.js") + function("renderArchiveLink") + '''
      const assert=require('node:assert/strict'); const nodes={}; const $=id=>nodes[id]||={hidden:false};
      const state={meta:{debug_enabled:false},activeId:'agent /?<x>'};
      assert.deepEqual(debugNavigation('?session=one%2Ftwo&context=1'),{sessionId:'one/two',openContext:true});
      assert.deepEqual(debugNavigation('?context=1'),{sessionId:'',openContext:false});
      renderArchiveLink(); assert.equal($('open-context-archive').hidden,true);
      state.meta.debug_enabled=true;renderArchiveLink();assert.equal($('open-context-archive').hidden,false);
      assert.equal($('open-context-archive').href,'/debug?session=agent+%2F%3F%3Cx%3E&context=1');
      state.activeId=null;renderArchiveLink();assert.equal($('open-context-archive').hidden,true);
    ''')


ARCHIVE_STUB = '''
  const assert=require('node:assert/strict');
  const nodes={}; const $=id=>nodes[id]||={textContent:'',hidden:false,disabled:false,open:true,replaceChildren(...values){this.children=values;}};
  const archive={sessionId:'one',generation:1,listGeneration:0,rawGeneration:0,directoryOffset:0,nextDirectoryOffset:null,chunk:null,nextRawOffset:null,listAbort:null,rawAbort:null,busyList:false,busyRaw:false};
  const renderArchiveControls=()=>{}; let renders=[]; const renderArchiveDirectory=data=>renders.push(data);
  let calls=[]; const fetch=async(path,options)=>new Promise(resolve=>calls.push({path,options,resolve}));
'''


def test_archive_directory_is_bounded_and_old_session_or_closed_response_is_ignored():
    run_js(function("archiveRequest", "debug.js") + function("archiveCurrent", "debug.js") + function("archiveTime", "debug.js") + function("selectArchiveChunk", "debug.js") + function("loadArchiveDirectory", "debug.js") + ARCHIVE_STUB + r'''
      (async()=>{
        const first=loadArchiveDirectory(0);assert.equal(calls[0].path,'/api/debug/sessions/one/context?offset=0&limit=20');
        assert.ok(calls[0].options.signal);archive.sessionId='two';archive.generation++;
        calls[0].resolve({ok:true,json:async()=>({chunks:[{ID:'stale',RawHistory:'must never be fetched'}]})});await first;
        assert.equal(renders.length,0);
        const second=loadArchiveDirectory(20);assert.equal(calls[1].path,'/api/debug/sessions/two/context?offset=20&limit=20');
        calls[1].resolve({ok:true,json:async()=>({chunks:[{ID:'new'}],next_offset:40})});await second;
        assert.equal(renders.length,1);assert.equal(archive.directoryOffset,20);assert.equal(archive.nextDirectoryOffset,40);
        const third=loadArchiveDirectory(40);$('context-archive').open=false;
        calls[2].resolve({ok:false,status:404});await third;assert.equal($('archive-error').textContent,'');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_raw_history_is_explicit_bounded_and_switching_chunks_resets_offset():
    run_js(function("archiveRequest", "debug.js") + function("archiveCurrent", "debug.js") + function("archiveTime", "debug.js") + function("selectArchiveChunk", "debug.js") + function("loadArchiveRaw", "debug.js") + ARCHIVE_STUB + r'''
      (async()=>{
        selectArchiveChunk({ID:'chunk-one',Time:{start:0,end:8,created_at:'2026-10-12T00:00:00Z'},Summary:'<img src=x>',path:'chunks/one.json'});
        assert.match($('archive-chunk-meta').textContent,/历史消息 0–8.*2026-10-12T00:00:00Z/);
        assert.doesNotMatch($('archive-chunk-meta').textContent,/\[object Object\]/);
        assert.equal(calls.length,0);assert.equal(archive.nextRawOffset,null);
        const first=loadArchiveRaw(0);assert.equal(calls[0].path,'/api/debug/sessions/one/context/chunk-one?offset=0&limit=6000');
        calls[0].resolve({ok:true,json:async()=>({ID:'chunk-one',RawHistory:'<img src=x>',raw_chars:9000,next_offset:6000})});await first;
        assert.equal($('archive-raw').textContent,'<img src=x>');assert.equal(archive.nextRawOffset,6000);
        const second=loadArchiveRaw(archive.nextRawOffset);assert.match(calls[1].path,/offset=6000&limit=6000$/);
        selectArchiveChunk({ID:'chunk-two',Summary:'two',path:'chunks/two.json'});
        assert.equal(calls[1].options.signal.aborted,true);assert.equal($('archive-raw').textContent,'');assert.equal(archive.nextRawOffset,null);
        calls[1].resolve({ok:true,json:async()=>({RawHistory:'stale raw',next_offset:8000})});await second;
        assert.equal($('archive-raw').textContent,'');const third=loadArchiveRaw(0);
        assert.match(calls[2].path,/context\/chunk-two\?offset=0&limit=6000$/);
        calls[2].resolve({ok:false,status:404});await third;assert.match($('archive-error').textContent,/404|未启用/);
        assert.equal(archive.busyRaw,false);
        const unicodePage=loadArchiveRaw(0);calls[3].resolve({ok:true,json:async()=>({RawHistory:'A😀',raw_chars:2,next_offset:null})});await unicodePage;
        assert.match($('archive-raw-page').textContent,/1–2 \/ 2/);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_quick_picker_patches_only_requested_setting_and_preserves_busy_draft():
    run_js(function("saveQuickSetting") + '''
      const assert=require('node:assert/strict');const state={quickKind:'model',quickId:'one',quickSaving:false,activeId:'one',sessions:new Map([['one',{id:'one',busy:false,title:'keep',tool_names:['safe']}],['two',{id:'two'}]])};
      const nodes={};const $=id=>nodes[id]||={open:true,close(){this.open=false;}};
      const sessionPath=id=>'/api/sessions/'+id;const showError=()=>{};const renderQuickOptions=()=>{};
      const renderSessions=()=>{};const renderTabs=()=>{};let renders=0;const renderConversationStatus=()=>renders++;
      let calls=[],fail=false;const jsonApi=async(path,options)=>{calls.push({path,...options});if(fail)throw new Error('failed');return {...state.sessions.get('one'),...options.body};};
      (async()=>{
        await saveQuickSetting('fast');assert.deepEqual(calls[0],{path:'/api/sessions/one',method:'PATCH',body:{model_profile:'fast'}});
        assert.equal(state.sessions.get('one').title,'keep');assert.deepEqual(state.sessions.get('one').tool_names,['safe']);
        state.quickKind='policy';await saveQuickSetting('');assert.deepEqual(calls[1].body,{permission_policy:null});
        state.sessions.get('one').busy=true;await saveQuickSetting('full');assert.equal(calls.length,2);
        state.sessions.get('one').busy=false;state.quickKind='unexpected';await saveQuickSetting('value');assert.equal(calls.length,2);
        state.quickKind='model';fail=true;$('quick-dialog').open=true;await saveQuickSetting('other');
        assert.equal(state.quickSaving,false);assert.equal($('quick-dialog').open,true);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_workspace_shortcuts_ignore_ime_and_keep_command_palette():
    run_js(function("handleWorkspaceShortcut") + '''
      const assert=require('node:assert/strict');const state={ready:true,cleanupPending:false};let newCount=0,focused=0,commands=0,closed=0;
      const $=()=>({open:false});const document={querySelectorAll:()=>[{close(){closed++;}}]};
      const openNew=()=>newCount++;const focusInput=()=>focused++;const openCommand=()=>commands++;
      const event=(key,extra={})=>({key,ctrlKey:true,preventDefault(){this.prevented=true;},...extra});
      handleWorkspaceShortcut(event('O',{shiftKey:true}));assert.equal(newCount,1);
      handleWorkspaceShortcut(event('l',{ctrlKey:false,metaKey:true}));assert.equal(focused,1);
      handleWorkspaceShortcut(event('k'));handleWorkspaceShortcut(event('p'));assert.equal(commands,2);
      handleWorkspaceShortcut(event('o',{shiftKey:true,isComposing:true}));handleWorkspaceShortcut(event('o',{shiftKey:true,altKey:true}));assert.equal(newCount,1);
      state.cleanupPending=true;handleWorkspaceShortcut(event('l'));assert.equal(focused,1);
      state.cleanupPending=false;state.ready=false;handleWorkspaceShortcut(event('o',{shiftKey:true}));assert.equal(newCount,1);
    ''')


def test_busy_composer_keeps_editable_draft_and_disables_quick_controls():
    run_js(function("renderComposer") + function("sendMessage") + '''
      const assert=require('node:assert/strict');const session={id:'one',busy:true};const selectedSession=()=>session;
      const state={sending:new Set(),ready:true,connectionState:'connected',drafts:new Map([['one','saved']])};
      const nodes={};const $=id=>nodes[id]||={value:'draft while running',hidden:false,disabled:false,textContent:''};
      const setText=(id,text)=>$(id).textContent=text;const phaseLabel=()=> '运行中';let sent=0;const jsonApi=()=>sent++;
      renderComposer();assert.equal($('message-input').disabled,false);assert.equal($('send-message').disabled,true);
      assert.equal($('composer-model').disabled,true);assert.equal($('composer-permission').disabled,true);
      assert.match($('composer-status').textContent,/草稿|完成/);
      (async()=>{await sendMessage();assert.equal(sent,0);assert.equal($('message-input').value,'draft while running');assert.equal(state.drafts.get('one'),'saved');})();
    ''')

def test_all_script_dom_targets_exist_in_their_page():
    for file, page in (("app.js", "index.html"), ("debug.js", "debug.html")):
        assets = DEBUG_STATIC if file.startswith('debug') else STATIC
        ids = IDs((assets / page).read_text(encoding="utf-8")).nodes
        references = set(re.findall(r'\$\("([^\"]+)"\)', (assets / file).read_text(encoding="utf-8")))
        assert references <= ids.keys(), references - ids.keys()


def test_quick_setting_response_preserves_runtime_updates_and_new_selection():
    run_js(function("saveQuickSetting") + '''
      const assert=require('node:assert/strict');
      const state={quickKind:'model',quickId:'one',quickSaving:false,activeId:'one',sessions:new Map([['one',{id:'one',busy:false,title:'old',permission_policy:'ask'}],['two',{id:'two'}]])};
      const nodes={};const $=id=>nodes[id]||={open:true,close(){this.open=false;}};
      const sessionPath=id=>'/api/sessions/'+id;let errors=0;const showError=(id,error)=>{if(error)errors++;};
      const renderQuickOptions=()=>{};const renderSessions=()=>{};const renderTabs=()=>{};const renderConversationStatus=()=>{};
      let finish;const jsonApi=()=>new Promise(resolve=>finish=resolve);
      (async()=>{
        const pending=saveQuickSetting('fast');state.sessions.set('one',{id:'one',busy:true,title:'updated by SSE',permission_policy:'ask'});
        state.activeId='two';state.quickId='two';state.quickKind='policy';
        finish({id:'one',busy:false,title:'stale response',permission_policy:'full',model_profile:'fast'});await pending;
        assert.equal(state.sessions.get('one').busy,true);assert.equal(state.sessions.get('one').title,'updated by SSE');
        assert.equal(state.sessions.get('one').permission_policy,'ask');assert.equal(state.sessions.get('one').model_profile,'fast');
        assert.equal($('quick-dialog').open,true);assert.equal(state.sessions.get('two').model_profile,undefined);assert.equal(errors,0);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_debug_page_clicks_load_only_requested_raw_and_export_stays_trace_only():
    harness = r'''
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div'){this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.value='';this.open=false;this.hidden=false;this.disabled=false;}
        set innerHTML(value){assert.fail('Archive content must stay text');}
        setAttribute(name,value){this.attributes[name]=value;}
        append(...nodes){this.children.push(...nodes);}
        replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){const result=[];for(const child of this.children){if((selector==='button'&&child.tag==='button')||(selector==='[data-chunk-id]'&&child.dataset.chunkId))result.push(child);result.push(...child.querySelectorAll(selector));}return result;}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);}
        fire(name){return Promise.all((this.handlers[name]||[]).map(handler=>handler({target:this})));}
        click(){return this.fire('click');}
        showModal(){this.open=true;}
        close(){this.open=false;setImmediate(()=>this.fire('close'));}
      }
      const nodes={}; const document={hidden:false,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag)};
      const window={location:{search:'?session=one&context=1'}};
      const trace={runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'one',status:'running'},{id:'two',status:'idle'}],trace:{active:[],events:[],capacity:100,dropped:0},agents_omitted:0};
      let requests=[],pending=[],timers=[],exported;
      const fetch=async(path,options)=>{requests.push({path,options});if(path==='/api/debug?session_id=one')return{ok:true,json:async()=>trace};return new Promise(resolve=>pending.push({path,resolve,options}));};
      const setTimeout=(callback)=>{timers.push(callback);};
      URL.createObjectURL=blob=>{exported=blob;return 'blob:test';};URL.revokeObjectURL=()=>{};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    '''
    actions = r'''
      (async()=>{
        await tick();assert.equal(nodes['agent-filter'].value,'one');assert.equal(nodes['context-archive'].open,true);
        assert.deepEqual(requests.map(item=>item.path),['/api/debug?session_id=one','/api/debug/sessions/one/context?offset=0&limit=20']);
        pending[0].resolve({ok:true,json:async()=>({format:'json',revision:4,session_summary:'<script>literal summary</script>',summary_kind:'model',paths:{manifest:'manifest.json',chunks:'chunks',journal:'journal.json'},chunks:[{ID:'c1',Time:{start:0,end:8,created_at:'2026-10-12T00:00:00Z'},Summary:'<img src=x>',path:'chunks/c1.json'}],total_chunks:21,next_offset:20})});await tick();
        assert.equal(nodes['archive-summary'].textContent,'<script>literal summary</script>');assert.equal(nodes['archive-format'].textContent,'JSON');
        assert.match(nodes['archive-chunks'].children[0].children[1].textContent,/历史消息 0–8.*2026-10-12T00:00:00Z/);
        await nodes['archive-chunks'].children[0].click();assert.equal(requests.length,2);assert.equal(nodes['archive-read'].disabled,false);
        const firstRaw=nodes['archive-read'].click();assert.equal(pending[1].path,'/api/debug/sessions/one/context/c1?offset=0&limit=6000');
        pending[1].resolve({ok:true,json:async()=>({ID:'c1',RawHistory:'RAW_ARCHIVE_ONLY',raw_chars:9000,next_offset:6000})});await firstRaw;
        assert.equal(nodes['archive-raw'].textContent,'RAW_ARCHIVE_ONLY');
        await nodes['export'].click();assert.deepEqual(JSON.parse(await exported.text()),trace);assert.doesNotMatch(await exported.text(),/RAW_ARCHIVE_ONLY|RawHistory/);
        const nextRaw=nodes['archive-raw-next'].click();assert.match(pending[2].path,/offset=6000&limit=6000$/);
        await nodes['archive-close'].click();assert.equal(pending[2].options.signal.aborted,true);assert.equal(nodes['context-archive'].open,false);
        pending[2].resolve({ok:true,json:async()=>({RawHistory:'STALE',raw_chars:9000,next_offset:null})});await nextRaw;assert.equal(nodes['archive-raw'].textContent,'');
        await nodes['agents'].children[1].children.at(-1).click();assert.equal(nodes['context-archive'].open,true);assert.equal(pending[3].path,'/api/debug/sessions/two/context?offset=0&limit=20');
        nodes['agent-filter'].value='one';await nodes['agent-filter'].fire('change');assert.equal(pending[3].options.signal.aborted,true);
        pending[3].resolve({ok:true,json:async()=>({revision:999,session_summary:'wrong agent',chunks:[]})});await tick();assert.notEqual(nodes['archive-summary'].textContent,'wrong agent');
        pending[4].resolve({ok:true,json:async()=>({revision:5,chunks:[],total_chunks:0,next_offset:null})});await tick();assert.equal(nodes['archive-revision'].textContent,'5');
        assert.ok(requests.every(item=>!item.options.method||item.options.method==='GET'),'Archive actions never stop the agent');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    '''
    run_js(harness + (DEBUG_STATIC / "debug-view.js").read_text(encoding="utf-8") + (DEBUG_STATIC / "debug.js").read_text(encoding="utf-8") + actions)
