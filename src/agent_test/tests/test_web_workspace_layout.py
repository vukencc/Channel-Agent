"""Workspace layout contracts and deterministic resize/activity behavior."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[3] / 'src/agent_frontend/web/static'


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = {}
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.nodes[attrs['id']] = (tag, attrs, tuple(self.stack))
        if tag not in {'input', 'meta', 'link', 'br', 'hr'}:
            self.stack.append(attrs.get('id') or tag)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


def test_monitor_lives_in_split_right_workspace_and_keeps_existing_controls():
    dom = Elements((STATIC / 'index.html').read_text(encoding='utf-8'))
    assert 'inspector' in dom.nodes['monitor'][2]
    assert 'main' not in dom.nodes['monitor'][2]
    for identifier in ('task-distribution', 'agent-activity', 'monitor-content',
                       'refresh-tasks', 'agents-count', 'tools-count', 'tasks-count'):
        assert 'inspector' in dom.nodes[identifier][2]
    for identifier, orientation in (('sidebar-resizer', 'vertical'),
                                    ('inspector-resizer', 'vertical'),
                                    ('inspector-splitter', 'horizontal')):
        attrs = dom.nodes[identifier][1]
        assert attrs['role'] == 'separator'
        assert attrs['tabindex'] == '0'
        assert attrs['aria-orientation'] == orientation


def test_context_meter_has_accessible_percentage_description():
    dom = Elements((STATIC / 'index.html').read_text(encoding='utf-8'))
    attrs = dom.nodes['context-meter'][1]
    assert attrs['role'] == 'progressbar'
    assert attrs['aria-valuemin'] == '0'
    assert attrs['aria-valuemax'] == '100'
    assert attrs['aria-describedby'] == 'context-progress'


def test_model_credentials_are_password_only_and_storage_contains_only_sizes():
    dom = Elements((STATIC / 'index.html').read_text(encoding='utf-8'))
    attrs = dom.nodes['model-api-key'][1]
    assert attrs['type'] == 'password'
    assert attrs['autocomplete'] == 'new-password'
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    assert 'localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(state.layout))' in script
    assert script.count('localStorage.setItem(') == 1
    save = script.split('async function saveModel(event)', 1)[1].split('async function deleteModel', 1)[0]
    assert 'await jsonApi(' in save
    assert 'finally' in save and '$("model-api-key").value = ""' in save
    assert 'model-api-key' not in script.split('function persistLayout()', 1)[1].split('function ', 1)[0]


def test_motion_and_small_screen_layout_have_explicit_fallbacks():
    css = (STATIC / 'app.css').read_text(encoding='utf-8')
    assert '@media(prefers-reduced-motion:reduce)' in css
    assert 'transition:none!important' in css
    assert '.inspector.mobile-open' in css
    assert 'touch-action:none' in css


def run_js(body):
    node = shutil.which('node') or shutil.which('nodejs')
    if not node:
        pytest.skip('Node is required for frontend behavior checks')
    result = subprocess.run([node, '-e', body], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def frontend_function(name):
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    return re.search(r'  (?:async )?function ' + name + r'\(.*?\n  \}', script, re.S).group()


def test_task_distribution_counts_selected_plan_without_bound_task_duplicates():
    run_js(frontend_function('taskDistribution') + '''
      const assert = require('node:assert/strict');
      const plan = {counts:{in_progress:1, pending:8, completed:2, blocked:1, failed:1}};
      const bound = [{status:'running'}, {status:'completed'}, {status:'queued'}];
      assert.deepEqual(taskDistribution(plan, bound), {active:1, waiting:9, completed:3});
      assert.deepEqual(taskDistribution({counts:{}}, bound), {active:0, waiting:0, completed:0});
      assert.deepEqual(taskDistribution(null, bound), {active:1, waiting:1, completed:1});
      assert.deepEqual(taskDistribution({status:'none', counts:{}}, bound), {active:1, waiting:1, completed:1});
      assert.deepEqual(taskDistribution({counts:{pending:-1, in_progress:'bad'}}, []), {active:0, waiting:0, completed:0});
    ''')


def test_runtime_model_options_resolve_public_names_and_default_model():
    run_js(frontend_function('normalizeOptions') + frontend_function('optionLabel') + '''
      const assert = require('node:assert/strict'); const policyLabels={};
      const state={defaultModel:'configured-model', modelLibrary:[{id:'runtime-abc', name:'中文助手', model:'provider-model'}]};
      assert.equal(normalizeOptions(['runtime-abc'], 'model')[0].label, '中文助手 · provider-model');
      assert.equal(optionLabel(['runtime-abc'], 'runtime-abc', 'model'), '中文助手 · provider-model');
      assert.equal(optionLabel([], null, 'model'), '默认 · configured-model');
      assert.equal(normalizeOptions(['ask'], 'policy')[0].label, 'ask');
      assert.equal(normalizeOptions(['unknown'], 'model')[0].label, 'unknown');
    ''')


def test_condensed_tables_preserve_task_cancellation_and_full_wide_details():
    css = (STATIC / 'app.css').read_text(encoding='utf-8')
    assert 'container-type:inline-size' in css
    assert '@container inspector (max-width: 380px)' in css
    assert '.monitor-table[data-kind=tasks]' in css
    compact = css.split('@container inspector (max-width: 380px)', 1)[1]
    assert '.monitor-table[data-kind=tasks] th:nth-child(2)' in compact
    assert '.monitor-table[data-kind=tasks] td:nth-child(4)' in compact
    assert '.monitor-table[data-kind=tasks] td:nth-child(5){display:none' not in compact
    script = (STATIC / 'app.js').read_text(encoding='utf-8')
    assert 'table.dataset.kind = state.monitorTab' in script


def test_context_supervisor_explains_below_threshold_without_check_failure():
    run_js(frontend_function('renderSessionProgress') + '''
      const assert = require('node:assert/strict');
      const nodes={}; const $=id=>nodes[id]||={textContent:'',hidden:false};
      const setText=(id,text)=>$(id).textContent=text;
      const state={renderSignatures:{},meta:{task_plans_enabled:true}};
      renderSessionProgress({id:'test', context_status:{structured:true, utilization:.3, supervisor:'below_threshold'}});
      assert.match($('context-progress').textContent,/监督：占用未达阈值，无需检查/);
    ''')


def test_plan_hydration_ignores_older_sse_race_and_requests_only_selected_once():
    run_js(frontend_function('projectPlanSummary') + frontend_function('mergePlanSummary') + frontend_function('hydrateSelectedPlan') + '''
      const assert=require('node:assert/strict');
      const state={meta:{task_plans_enabled:true}, activeId:'one', selectionGeneration:1, planHydrationKey:null, sessions:new Map([['one',{id:'one',plan:null}],['other',{id:'other',plan:null}]])};
      const selectedSession=()=>state.sessions.get(state.activeId);
      let renderCount=0; const renderConversationStatus=()=>renderCount++;
      let finish, requests=0; const jsonApi=path=>{requests++; assert.equal(path,'/api/sessions/one/plan?limit=1'); return new Promise(resolve=>finish=resolve);};
      (async()=>{
        const pending=hydrateSelectedPlan(); await hydrateSelectedPlan(); assert.equal(requests,1);
        state.sessions.set('one',{id:'one',plan:{plan_id:'p',revision:9,counts:{completed:9},execution_signature:'new'}});
        finish({plan_id:'p',revision:8,status:'in_progress',verification:'unverified',counts:{pending:9},ready:[],execution_signature:'old',goal:'must not copy',tasks:[{}]});
        await pending; assert.equal(state.sessions.get('one').plan.revision,9); assert.equal(renderCount,0);
        state.sessions.set('one',{id:'one',plan:null}); state.selectionGeneration++; state.planHydrationKey=null;
        const second=hydrateSelectedPlan(); finish({plan_id:'p',revision:9,status:'in_progress',verification:'unverified',counts:{in_progress:1,pending:8},ready:['task-2'],execution_signature:'current',goal:'excluded',tasks:[{}],extra:'excluded'});
        await second; assert.equal(state.sessions.get('one').plan.counts.pending,8); assert.equal(renderCount,1);
        assert.deepEqual(Object.keys(state.sessions.get('one').plan).sort(),['counts','execution_signature','plan_id','ready','revision','status','verification'].sort());
        assert.equal(state.sessions.get('other').plan,null);
        const captured=state.sessions.get('one').plan;
        state.sessions.set('one',{id:'one',plan:{...captured, execution_signature:'newer-sse-runtime'}});
        assert.equal(mergePlanSummary('one',{...captured, execution_signature:'stale-runtime'},captured),false);
        assert.equal(state.sessions.get('one').plan.execution_signature,'newer-sse-runtime');
        state.sessions.set('one',{id:'one',plan:null}); state.selectionGeneration++; state.planHydrationKey=null;
        const third=hydrateSelectedPlan(); state.activeId='other'; state.selectionGeneration++;
        finish({plan_id:'p',revision:10,counts:{completed:9}}); await third;
        assert.equal(state.sessions.get('one').plan,null); assert.equal(state.sessions.get('other').plan,null);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_mobile_monitor_first_open_close_reopen_and_forced_open():
    run_js(frontend_function('toggleMonitor') + '''
      const assert=require('node:assert/strict');
      const state={monitorOpen:true};
      const nodes={}; const $=id=>nodes[id]||={hidden:false,classList:{values:new Set(),contains(key){return this.values.has(key);},toggle(key,force){const next=force??!this.contains(key);if(next)this.values.add(key);else this.values.delete(key);return next;}}};
      let mobile=true; const window={matchMedia:()=>({matches:mobile})};
      let rendered=0, refreshed=0; const renderMonitor=()=>rendered++; const refreshTasks=()=>refreshed++; const applyLayout=()=>{};
      const assertOpen=()=>{assert.equal($('inspector').classList.contains('mobile-open'),true);assert.equal(state.monitorOpen,true);assert.equal($('monitor').hidden,false);};
      toggleMonitor(); assertOpen(); assert.equal(rendered,1);
      toggleMonitor(); assert.equal($('inspector').classList.contains('mobile-open'),false);assert.equal(state.monitorOpen,false);assert.equal($('monitor').hidden,true);
      toggleMonitor(); assertOpen(); assert.equal(rendered,2);
      toggleMonitor(true); assertOpen(); assert.equal(rendered,3); assert.equal(refreshed,3);
      toggleMonitor(false); toggleMonitor(true); assertOpen(); assert.equal(rendered,4);
      mobile=false; toggleMonitor(); assert.equal(state.monitorOpen,false); toggleMonitor(); assert.equal(state.monitorOpen,true);
    ''')


def test_starter_is_hidden_for_tool_only_and_paginated_history():
    run_js(frontend_function('renderMessages') + '''
      const assert=require('node:assert/strict');
      const nodes={}; const $=id=>nodes[id]||={hidden:false,replaceChildren(){}};
      const fakeNode=()=>({append(){},open:false}); const element=fakeNode;
      const document={createDocumentFragment:fakeNode}; const executionRecord=fakeNode;
      const hasNarrative=()=>false; let session={busy:false,total_messages:61};
      const selectedSession=()=>session;
      const toolHistory={records:Array.from({length:60},(_,index)=>({index,message:{role:'tool',content:'result'}})),total:61,nextBefore:1};
      renderMessages(toolHistory); assert.equal($('conversation-start').hidden,true);
      renderMessages({records:[],total:61,nextBefore:61}); assert.equal($('conversation-start').hidden,true);
      renderMessages({records:[],total:0,nextBefore:null}); assert.equal($('conversation-start').hidden,true,'session metadata still says there is history');
      session={busy:false,total_messages:0}; renderMessages({records:[],total:0,nextBefore:null}); assert.equal($('conversation-start').hidden,false);
      session.busy=true; renderMessages({records:[],total:0,nextBefore:null}); assert.equal($('conversation-start').hidden,true);
    ''')


def test_cancelled_activity_is_distinct_from_ready_and_stale_work_phase():
    run_js(frontend_function('activityPhase') + '''
      const assert=require('node:assert/strict');
      assert.equal(activityPhase({busy:false,status:'cancelled'},[],'connected'),'cancelled');
      assert.equal(activityPhase({busy:false,status:'canceled',phase:'context_compaction'},[],'connected'),'cancelled');
      assert.equal(activityPhase({busy:false,status:'stopped'},[],'connected'),'stopped');
      assert.equal(activityPhase({busy:false,status:'idle'},[],'connected'),'idle');
      assert.equal(activityPhase({busy:true,status:'cancelled',phase:'thinking'},[],'connected'),'thinking');
    ''')


def test_resize_clamps_malformed_preferences_and_preserves_reading_width():
    run_js(frontend_function('clampLayout') + '''
      const assert = require('node:assert/strict');
      const wide = clampLayout({left: 9999, right: 9999, split: 99}, 1200);
      assert.ok(1200 - 52 - 12 - wide.left - wide.right >= 420);
      assert.ok(wide.left >= 190 && wide.right >= 250);
      assert.equal(wide.split, 78);
      const invalid = clampLayout({left: 'secret', right: NaN, split: null}, 1400);
      assert.deepEqual(invalid, {left: 246, right: 310, split: 52});
      const narrow = clampLayout({left: 350, right: 500, split: 8}, 980);
      assert.ok(980 - 52 - 12 - narrow.left - narrow.right >= 420);
      assert.equal(narrow.split, 24);
    ''')


def test_activity_classification_uses_runtime_evidence():
    run_js(frontend_function('activityPhase') + '''
      const assert = require('node:assert/strict');
      assert.equal(activityPhase({busy:true, phase:'thinking'}, [], 'connected'), 'thinking');
      assert.equal(activityPhase({busy:true}, [{name:'rag_search'}], 'connected'), 'search');
      assert.equal(activityPhase({busy:true}, [{name:'create_file'}], 'connected'), 'tools');
      assert.equal(activityPhase({busy:true, phase:'context_compaction'}, [], 'connected'), 'context');
      assert.equal(activityPhase({busy:false}, [], 'offline'), 'connection');
      assert.equal(activityPhase({busy:false}, [], 'connected'), 'idle');
    ''')


def test_model_submission_clears_password_on_success_and_error():
    run_js(frontend_function('saveModel') + '''
      const assert = require('node:assert/strict');
      const state = {modelSaving:false, editingModel:null};
      const nodes = {}; const $ = id => nodes[id] ||= {value:'', disabled:false};
      const showError = () => {}; const toast = () => {};
      const loadModels = async () => {}; const refreshModelOptions = async () => {};
      const resetModelForm = () => {};
      const localStorage = { setItem(){assert.fail('credentials must not be persisted in browser storage');} };
      let request, fail = false;
      const jsonApi = async (path, options) => {request={path, ...options}; if(fail) throw new Error('safe error'); return {id:'runtime-test'};};
      (async () => {
        $('model-name').value='Test model'; $('model-id').value='test-model';
        $('model-base-url').value='https://example.invalid/v1'; $('model-api-key').value='mock-key-only';
        await saveModel({preventDefault(){}});
        assert.equal(request.path, '/api/models'); assert.equal(request.method,'POST');
        assert.equal(request.body.api_key,'mock-key-only'); assert.equal($('model-api-key').value,'');
        assert.equal(state.modelSaving,false); assert.equal($('model-submit').disabled,false);
        state.editingModel='runtime-test'; $('model-api-key').value='mock-failed-key'; fail=true;
        await saveModel({preventDefault(){}});
        assert.equal(request.path,'/api/models/runtime-test'); assert.equal(request.method,'PUT');
        assert.equal($('model-api-key').value,''); assert.equal(state.modelSaving,false);
      })().catch(error => {console.error(error);process.exitCode=1;});
    ''')
