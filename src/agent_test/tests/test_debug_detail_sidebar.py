"""局部详情只在侧栏呈现，并以真实元数据解释选中行为。"""

import pytest

from test_debug_timeline_overview import STATIC, run_view


FAKE_PAGE = r"""
  const assert=require('node:assert/strict');
  let forcedScrolls=0;
  class FakeNode {
    constructor(tag='div'){this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
    set innerHTML(value){assert.fail('Metadata must render through text nodes');}
    setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
    append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
    querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
    addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
    scrollIntoView(){forcedScrolls++;}
    click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
  }
  const nodes={},stored=new Map([['agent-debug-agents-hidden','true']]),writes=[];
  const document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)};
  const window={location:{search:''},localStorage:{getItem:key=>stored.get(key),setItem:(key,value)=>{stored.set(key,value);writes.push([key,value]);}}};
  const e=(seq,kind,session_id='root',fields={})=>({seq,kind,session_id,elapsed_ms:seq*10,turn_id:'turn',...fields});
  const events=[e(1,'turn.started'),e(2,'plan.updated','root',{stage_index:2,state:'running'}),e(3,'phase.changed','root',{phase:'tools'}),
    e(4,'tool.started','root',{tool_name:'read_file',tool_call_id:'read',batch_id:'batch'}),
    e(5,'tool.completed','root',{tool_name:'read_file',tool_call_id:'read',batch_id:'batch',state:'error'}),
    e(6,'tool.started','root',{tool_name:'wait_agent_task',tool_call_id:'wait',batch_id:'waiting'}),
    e(7,'agent.created','child',{parent_session_id:'root'}),e(8,'agent.started','child'),
    e(9,'phase.changed','child',{phase:'model_wait'}),e(10,'model.final','child')];
  const frame={runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'root'},{id:'child',parent_id:'root'}],agents_omitted:0,
    trace:{active:[],events:[],capacity:100,dropped:0,generation:'one',cursor:10,timeline:events,now_elapsed_ms:100}};
  let requests=[];const setTimeout=()=>0,fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>frame};};
  const tick=()=>new Promise(resolve=>setImmediate(resolve));
"""


def test_sidebar_open_close_and_agent_toggle_never_scroll_or_read_body():
    run_view(FAKE_PAGE + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        assert.equal(nodes['agent-sidebar'].hidden,true,'Only the prior layout preference is restored');
        assert.equal(nodes['toggle-agents'].attributes['aria-expanded'],'false');
        assert.equal(nodes['event-detail'].hidden,true);
        await nodes['toggle-agents'].click();
        assert.equal(nodes['agent-sidebar'].hidden,false);
        assert.deepEqual(writes,[['agent-debug-agents-hidden','false']]);
        const scene=()=>nodes['timeline-lanes'].children[0];
        const group=scene().querySelectorAll('button').find(button=>button.className.includes('tool-batch'));
        await group.click();await tick();
        assert.equal(nodes['event-detail'].hidden,false);
        assert.equal(nodes['debug-layout'].attributes['data-detail-open'],'true');
        assert.equal(forcedScrolls,0,'Selecting local details preserves the document scroll position');
        assert.equal(requests.length,1,'Opening metadata never requests model output or tool body');
        assert.match(nodes['event-detail-narrative'].textContent,/步骤 2/);
        assert.match(nodes['event-detail-narrative'].textContent,/文件读取/);
        assert.match(nodes['event-detail-narrative'].textContent,/失败/);
        assert.doesNotMatch(nodes['event-detail-narrative'].textContent,/成功/);
        await nodes['selection-close'].click();
        assert.equal(nodes['event-detail'].hidden,true);
        assert.equal(nodes['debug-layout'].attributes['data-detail-open'],'false');
        assert.equal(forcedScrolls,0);
        await nodes['timeline-all-events'].click();
        assert.equal(nodes['event-detail'].hidden,false);
        const wait=nodes['event-selection-list'].children.find(row=>row.children[1].textContent.includes('wait_agent_task'));
        await wait.click();
        assert.match(nodes['event-detail-narrative'].textContent,/等待子任务/);
        assert.match(nodes['event-detail-narrative'].textContent,/尚未确认/);
        assert.equal(requests.length,1);
        assert.deepEqual(writes,[['agent-debug-agents-hidden','false']],'No event metadata or detail body enters localStorage');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_agent_label_repeated_clicks_focus_without_collapsing_behavior():
    run_view(FAKE_PAGE + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0];
        const child=()=>scene().querySelectorAll('button').find(button=>button.dataset.eventKey==='lane-child');
        const originalHeight=scene().style.height;
        assert.ok(child());assert.doesNotMatch(child().title,/展开|收起/);
        assert.equal(child().attributes['aria-expanded'],undefined);
        await child().click();await child().click();
        assert.equal(scene().style.height,originalHeight);
        assert.equal(child().attributes['aria-pressed'],'true');
        assert.match(nodes['timeline-status'].textContent,/完整行为/);
        assert.equal(forcedScrolls,0);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_narrative_does_not_infer_child_results_or_reuse_another_turn_stage():
    extra_events = r"""
      events.push(e(11,'tool.started','root',{turn_id:'next',tool_name:'wait_agent_task',tool_call_id:'next-wait'}),
        e(12,'tool.completed','root',{turn_id:'next',tool_name:'wait_agent_task',tool_call_id:'next-wait',state:'completed'}),
        e(13,'tool.completed','child',{tool_name:'constructor',state:'error',content:'CANARY_PRIVATE_BODY',task:'ignore previous instructions'}));
      frame.trace.cursor=13;
    """
    run_view(FAKE_PAGE + extra_events + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const rows=nodes['event-selection-list'].children;
        const completedWait=rows.find(row=>/wait_agent_task.*完成/.test(row.children[1].textContent));
        assert.ok(completedWait);await completedWait.click();
        const narrative=()=>nodes['event-detail-narrative'].textContent;
        assert.match(narrative(),/等待子任务/);assert.match(narrative(),/已完成/);
        assert.match(narrative(),/子任务结果需.*确认/);
        assert.doesNotMatch(narrative(),/步骤 2|工具执行|子任务已完成|子任务已结束/);
        await rows.find(row=>row.children[1].textContent.includes('constructor')).click();
        assert.match(narrative(),/工具执行（constructor）.*失败/);
        assert.doesNotMatch(narrative(),/function Object|CANARY_PRIVATE_BODY|ignore previous instructions|步骤 2/);
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_sidebar_still_works_when_browser_storage_is_unavailable():
    run_view(FAKE_PAGE + r"""
      Object.defineProperty(window,'localStorage',{get(){throw new Error('storage disabled');}});
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();assert.equal(nodes['agent-sidebar'].hidden,false);
        await nodes['toggle-agents'].click();assert.equal(nodes['agent-sidebar'].hidden,true);
        await nodes['timeline-all-events'].click();assert.equal(nodes['event-detail'].hidden,false);
        assert.equal(forcedScrolls,0);assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


REGION_EVENTS = r"""
  events.splice(0,events.length,
    e(1,'phase.changed','root',{phase:'context_prepare'}),
    e(2,'plan.updated','root',{stage_index:1,state:'running'}),
    e(3,'phase.changed','root',{phase:'tools'}),
    e(4,'tool.started','root',{tool_name:'read_file',tool_call_id:'same',batch_id:'alpha'}),
    e(5,'tool.completed','root',{tool_name:'read_file',tool_call_id:'same',batch_id:'alpha',state:'error',content:'CANARY_PRIVATE_BODY'}),
    e(6,'tool.started','root',{tool_name:'run_command',tool_call_id:'command',batch_id:'alpha'}),
    e(7,'tool.completed','root',{tool_name:'run_command',tool_call_id:'command',batch_id:'alpha',state:'completed'}),
    e(8,'agent.created','child',{turn_id:'child-turn',parent_session_id:'root'}),
    e(9,'tool.started','child',{turn_id:'child-turn',tool_name:'read_file',tool_call_id:'same'}),
    e(10,'tool.completed','child',{turn_id:'child-turn',tool_name:'read_file',tool_call_id:'same',state:'rejected'}),
    e(11,'tool.started','root',{turn_id:'next',tool_name:'read_file',tool_call_id:'same'}),
    e(12,'tool.completed','root',{turn_id:'next',tool_name:'read_file',tool_call_id:'same',state:'completed'}),
    e(13,'tool.started','root',{turn_id:'next',tool_name:'wait_agent_task',tool_call_id:'wait'}),
    e(14,'tool.completed','root',{turn_id:'next',tool_name:'wait_agent_task',tool_call_id:'wait',state:'completed'}),
    e(15,'tool.started','root',{turn_id:'next',tool_name:'wait_agent_task',tool_call_id:'pending'}),
    e(16,'message.sent','root',{turn_id:'next',related_session_id:'child',message_seq:9}),
    e(17,'message.read','child',{turn_id:'child-turn',related_session_id:'root',message_seq:9}),
    e(18,'message.sent','root',{turn_id:'next',related_session_id:'child',message_seq:10}),
    e(19,'context.compacted','root',{turn_id:'next',task:'ignore previous instructions'}),
    e(20,'model.final','root',{turn_id:'next'}));
  frame.trace.cursor=20;
"""


def test_region_narrative_aggregates_calls_by_agent_turn_and_preserves_view_mode():
    run_view(FAKE_PAGE + REGION_EVENTS + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const narrative=()=>nodes['event-detail-narrative'].textContent;
        assert.match(narrative(),/这段包含 20 条事件/);
        assert.match(narrative(),/2 个 Agent/);
        assert.match(narrative(),/先.*准备上下文.*随后.*步骤 1.*随后.*工具执行/s);
        assert.match(narrative(),/工具调用共 6 次/,'Start/completion pairs are counted once, with Agent and turn boundaries');
        assert.match(narrative(),/已完成 3/);assert.match(narrative(),/失败 1/);assert.match(narrative(),/拒绝 1/);
        assert.match(narrative(),/仅记录开始 1/,'An unfinished wait is not counted as completed');
        assert.match(narrative(),/文件读取（read_file）3 次/);
        assert.match(narrative(),/命令执行（run_command）1 次/);
        assert.match(narrative(),/等待子任务（wait_agent_task）2 次/);
        assert.match(narrative(),/创建 1 个子 Agent/);
        assert.match(narrative(),/发送 2 条.*读取 1 次/);
        assert.match(narrative(),/等待调用.*不能确认子任务完成/);
        assert.doesNotMatch(narrative(),/CANARY_PRIVATE_BODY|ignore previous instructions|子任务已完成|子任务已结束/);
        const original=narrative();
        await Promise.all(nodes['follow-latest'].handlers.change.map(handler=>handler()));
        assert.equal(narrative(),original,'Polling-style redraws preserve the region overview');
        await nodes['event-selection-list'].children.find(row=>/read_file.*失败/.test(row.children[1].textContent)).click();
        assert.match(narrative(),/文件读取.*失败/);assert.doesNotMatch(narrative(),/这段包含/);
        const individual=narrative();
        await Promise.all(nodes['follow-latest'].handlers.change.map(handler=>handler()));
        assert.equal(narrative(),individual,'A selected event remains an individual explanation after redraw');
        await nodes['selection-close'].click();
        await Promise.all(nodes['follow-latest'].handlers.change.map(handler=>handler()));
        assert.equal(nodes['event-detail'].hidden,true,'A redraw cannot reopen closed local details');
        await nodes['timeline-all-events'].click();
        assert.match(narrative(),/这段包含 20 条事件/,'Reopening a region restores its aggregate explanation');
        assert.equal(requests.length,1);assert.equal(forcedScrolls,0);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_message_region_narrative_uses_actual_send_and_read_edges():
    run_view(FAKE_PAGE + REGION_EVENTS + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=nodes['timeline-lanes'].children[0];
        await scene.querySelectorAll('button').find(button=>button.className==='message-summary').click();
        const narrative=()=>nodes['event-detail-narrative'].textContent;
        assert.match(narrative(),/这段包含 3 条事件/);
        assert.match(narrative(),/发送 2 条.*读取 1 次/);
        assert.match(narrative(),/已有读取记录 1 条.*尚无读取记录 1 条/);
        assert.doesNotMatch(narrative(),/工具调用|子任务已完成/,'Unrelated tools are excluded from the selected communication region');
        const original=narrative();
        await Promise.all(nodes['follow-latest'].handlers.change.map(handler=>handler()));
        assert.equal(narrative(),original);assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_region_narrative_refreshes_new_tool_result_without_loading_body():
    page = FAKE_PAGE.replace("const setTimeout=()=>0", "const timers=[],setTimeout=callback=>timers.push(callback)")
    run_view(page + REGION_EVENTS + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        assert.match(nodes['event-detail-narrative'].textContent,/仅记录开始 1/);
        frame.trace.timeline=[...events,e(21,'tool.completed','root',{turn_id:'next',tool_name:'wait_agent_task',tool_call_id:'pending',state:'error',output:'PRIVATE_TOOL_OUTPUT'})];
        frame.trace.cursor=21;
        await timers.shift()();await tick();
        const narrative=nodes['event-detail-narrative'].textContent;
        assert.match(narrative,/这段包含 21 条事件/);
        assert.match(narrative,/工具调用共 6 次/);
        assert.match(narrative,/失败 2/);assert.doesNotMatch(narrative,/仅记录开始/);
        assert.doesNotMatch(narrative,/PRIVATE_TOOL_OUTPUT/);
        assert.equal(requests.length,2);assert.ok(requests.every(path=>!path.includes('/detail')));
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_single_event_region_keeps_individual_explanation():
    run_view(FAKE_PAGE + r"""
      events.splice(0,events.length,e(1,'tool.started','root',{tool_name:'wait_agent_task',tool_call_id:'one'}));
      frame.trace.cursor=1;
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const narrative=nodes['event-detail-narrative'].textContent;
        assert.match(narrative,/等待子任务.*已开始/s);
        assert.match(narrative,/尚未确认子任务结束/);
        assert.doesNotMatch(narrative,/这段包含|工具调用共|已完成/);
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_region_narrative_explains_lifecycle_and_module_failures_without_tools():
    run_view(FAKE_PAGE + r"""
      events.splice(0,events.length,e(1,'turn.started'),
        e(2,'model.completed','root',{state:'completed'}),
        e(3,'span.completed','root',{state:'error',module:'model'}),
        e(4,'turn.stopped','root',{state:'error',reason:'error'}));
      frame.trace.cursor=4;
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const narrative=nodes['event-detail-narrative'].textContent;
        assert.match(narrative,/这段包含 4 条事件/);
        assert.match(narrative,/任务轮次记录：开始 1 次、停止 1 次（失败 1）/);
        assert.match(narrative,/1 次模型请求结束/);
        assert.match(narrative,/模块结束记录 1 条：失败 1/);
        assert.doesNotMatch(narrative,/工具调用共|任务成功|任务已完成/);
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


@pytest.mark.parametrize("mode", ["event", "region"])
def test_budget_or_connection_initialization_failed_state_is_explained(mode):
    run_view(FAKE_PAGE + r"""
      events.splice(0,events.length,e(1,'turn.started'),
        e(2,'turn.stopped','root',{state:'failed',reason:'error'}));
      frame.trace.cursor=2;
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
    """ + (r"""
        await nodes['event-selection-list'].children.find(row=>row.children[1].textContent.includes('轮次停止')).click();
        assert.match(nodes['event-detail-narrative'].textContent,/本轮任务停止：失败/);
    """ if mode == "event" else r"""
        assert.match(nodes['event-detail-narrative'].textContent,/任务轮次记录：开始 1 次、停止 1 次（失败 1）/);
    """) + r"""
        assert.doesNotMatch(nodes['event-detail-narrative'].textContent,/结果状态未知|未记录结果状态/);
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_pending_body_read_cannot_overwrite_switched_event_or_closed_sidebar():
    page = FAKE_PAGE.replace(
        "const setTimeout=()=>0,fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>frame};};",
        r"""const detailRequests=[],setTimeout=()=>0,fetch=async(path,options)=>{
          requests.push(path);
          if(path.includes('/detail?'))return new Promise(resolve=>detailRequests.push({path,options,resolve}));
          return{ok:true,json:async()=>frame};
        };""",
    )
    run_view(page + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const rows=()=>nodes['event-selection-list'].children;
        await rows().find(row=>/read_file.*失败/.test(row.children[1].textContent)).click();
        const first=nodes['event-detail-load'].click();await tick();
        assert.equal(detailRequests.length,1);assert.match(detailRequests[0].path,/events\/5\/detail/);
        await rows().find(row=>row.children[1].textContent.includes('wait_agent_task')).click();
        assert.equal(detailRequests[0].options.signal.aborted,true,'Changing events cancels the outstanding body request');
        const selectedMeta=nodes['event-detail-meta'].textContent,selectedNarrative=nodes['event-detail-narrative'].textContent;
        detailRequests[0].resolve({ok:true,json:async()=>({content:'STALE_FIRST_BODY',next_offset:6000,total_chars:7000})});
        await first;
        assert.equal(nodes['event-detail-meta'].textContent,selectedMeta);
        assert.equal(nodes['event-detail-narrative'].textContent,selectedNarrative);
        assert.equal(nodes['event-detail-content'].textContent,'');
        assert.equal(nodes['event-detail-next'].hidden,true);assert.equal(nodes['event-detail-load'].disabled,false);
        const second=nodes['event-detail-load'].click();await tick();
        assert.equal(detailRequests.length,2);
        await nodes['selection-close'].click();
        assert.equal(detailRequests[1].options.signal.aborted,true,'Closing the sidebar cancels the outstanding body request');
        detailRequests[1].resolve({ok:true,json:async()=>({content:'STALE_CLOSED_BODY',next_offset:6000,total_chars:7000})});
        await second;
        assert.equal(nodes['event-detail'].hidden,true);
        assert.equal(nodes['event-detail-content'].textContent,'');assert.equal(nodes['event-detail-meta'].textContent,'');
        assert.equal(nodes['event-detail-next'].hidden,true);assert.equal(nodes['event-detail-load'].disabled,true);
        assert.equal(requests.length,3);assert.equal(forcedScrolls,0);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_single_scope_phase_chain_omits_repeated_identity_and_keeps_eight_stage_limit():
    run_view(FAKE_PAGE + r"""
      const phases=['model_wait','tools','model_wait','confirmation','tools','continuation','model_wait','tools','complete','stopping'];
      events.splice(0,events.length,...phases.map((phase,index)=>e(index+1,'phase.changed','root',{phase})));
      frame.trace.cursor=events.length;
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const progress=nodes['event-detail-narrative'].textContent.split('\n').find(line=>line.startsWith('阶段推进：'));
        assert.equal(progress,'阶段推进：先 等待模型；随后 工具执行；随后 等待模型；随后 等待确认；随后 工具执行；随后 继续执行；随后 等待模型；随后 工具执行；另有 2 次阶段变化可在事件列表查看。');
        assert.doesNotMatch(progress,/Agent root|轮次 turn/);
        assert.equal((progress.match(/随后/g)||[]).length,7,'The phase chain still displays at most eight stages');
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_mixed_scope_phase_chain_retains_agent_and_turn_identity():
    run_view(FAKE_PAGE + r"""
      events.splice(0,events.length,
        e(1,'phase.changed','root',{turn_id:'turn-one',phase:'model_wait'}),
        e(2,'phase.changed','root',{turn_id:'turn-two',phase:'tools'}),
        e(3,'phase.changed','child',{turn_id:'turn-one',phase:'confirmation'}));
      frame.trace.cursor=events.length;
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        const progress=nodes['event-detail-narrative'].textContent.split('\n').find(line=>line.startsWith('阶段推进：'));
        assert.match(progress,/先 Agent root · 轮次 turn-one：等待模型/);
        assert.match(progress,/随后 Agent root · 轮次 turn-two：工具执行/);
        assert.match(progress,/随后 Agent child · 轮次 turn-one：等待确认/);
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_registered_file_job_environment_and_plan_tools_have_concrete_types():
    run_view(FAKE_PAGE + r"""
      const categories=[['create_file','文件创建'],['update_file','文件更新'],['append_file','文件追加'],
        ['start_command_job','启动后台命令'],['job_status','查询后台命令状态'],['job_logs','读取后台命令日志'],['cancel_command_job','取消后台命令'],
        ['inspect_environment','检查运行环境'],['prepare_project_environment','准备项目环境'],
        ['plan_create','创建计划'],['plan_update','更新计划'],['plan_get','查看计划']];
      events.splice(0,events.length,...categories.map(([tool_name],index)=>e(index+1,'tool.completed','root',{tool_name,tool_call_id:`call-${index}`,state:'completed'})));
      frame.trace.cursor=events.length;
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();await nodes['timeline-all-events'].click();
        assert.match(nodes['event-detail-narrative'].textContent,/文件创建（create_file）1 次/);
        for(const [tool,label] of categories) {
          await nodes['event-selection-list'].children.find(row=>row.children[1].textContent.startsWith(`${tool} ·`)).click();
          assert.ok(nodes['event-detail-narrative'].textContent.includes(`${label}（${tool}）`),`${tool} should describe its actual operation`);
          assert.doesNotMatch(nodes['event-detail-narrative'].textContent,/工具执行（/);
        }
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_sidebar_layout_keeps_detail_sibling_to_timeline_and_mobile_drawer():
    html = (STATIC / "debug.html").read_text()
    css = (STATIC / "debug.css").read_text()
    assert 'id="debug-layout"' in html
    assert 'id="agent-sidebar"' in html
    assert html.index('</article>') < html.index('id="event-detail"')
    assert 'id="event-detail-narrative"' in html
    assert '发生了什么' in html
    assert '.event-detail[hidden]' in css
    assert 'position: sticky' in css
    assert 'overflow-y: auto' in css
    assert '@media (max-width: 1100px)' in css
    assert 'position: fixed' in css
