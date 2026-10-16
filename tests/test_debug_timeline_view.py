"""Pure timeline view behavior; no browser or running model is required."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/ai_agent_startup/web/static"


def run_view(source):
    node = shutil.which("node") or "/home/elaine_vuken/.nvm/versions/node/v24.21.0/bin/node"
    if not Path(node).exists():
        pytest.skip("Node is required for pure timeline tests")
    result = subprocess.run([node, "-e", (STATIC / "debug-view.js").read_text() + source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def frontend_function(name):
    source = (STATIC / "debug.js").read_text()
    match = re.search(r"  (?:async )?function " + name + r"\(.*?\n  \}", source, re.S)
    assert match, name
    return match.group()


def test_timeline_merge_is_bounded_deduplicated_and_resets_discontinuous_windows():
    run_view("""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const event=seq=>({seq,kind:'phase.changed',session_id:'a',elapsed_ms:seq});
      let state=V.mergeTimeline(null,{generation:'one',cursor:3,timeline:[event(1),event(2),event(3)]},3);
      state=V.mergeTimeline(state,{generation:'one',cursor:5,incremental:true,timeline:[event(3),event(5),event(4)]},3);
      assert.deepEqual(state.timeline.map(e=>e.seq),[3,4,5]);
      assert.equal(state.cursor,5);
      state=V.mergeTimeline(state,{generation:'one',cursor:8,gap:true,timeline:[event(8)]});
      assert.deepEqual(state.timeline.map(e=>e.seq),[8]);assert.equal(state.resetReason,'gap');
      state=V.mergeTimeline(state,{generation:'two',cursor:1,incremental:true,timeline:[event(1)]});
      assert.deepEqual(state.timeline.map(e=>e.seq),[1]);assert.equal(state.resetReason,'generation');
      state=V.mergeTimeline(state,{generation:'two',cursor:4,reset:true,timeline:[event(4)]});
      assert.deepEqual(state.timeline.map(e=>e.seq),[4]);assert.equal(state.resetReason,'reset');
    """)


def test_parallel_tools_keep_real_intervals_and_only_contiguous_batches_group():
    run_view("""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const e=(seq,kind,t,fields={})=>({seq,kind,elapsed_ms:t,session_id:'a',...fields});
      const snapshot={agents:[{id:'a'}],trace:{timeline:[
        e(1,'tool.started',10,{batch_id:'b',tool_call_id:'x',tool_name:'one'}),
        e(2,'tool.started',20,{batch_id:'b',tool_call_id:'y',tool_name:'two'}),
        e(3,'tool.completed',80,{batch_id:'b',tool_call_id:'y',state:'error'}),
        e(4,'tool.completed',100,{batch_id:'b',tool_call_id:'x',state:'completed'}),
        e(5,'model.final',110),
        e(6,'tool.started',120,{batch_id:'b',tool_call_id:'z',tool_name:'three'}),
        e(7,'tool.completed',160,{batch_id:'b',tool_call_id:'z',state:'completed'})
      ],active:[],events:[]}};
      const view=V.timeline(snapshot);assert.equal(view.groups.length,2);
      assert.deepEqual(view.groups[0].calls.map(c=>[c.start,c.end]),[[10,100],[20,80]]);
      assert.equal(view.groups[0].errors,1);assert.equal(view.groups[0].start,10);assert.equal(view.groups[0].end,100);
      assert.equal(V.timeline(snapshot,'',true).groups[0].calls.length,2);
    """)


def test_short_tool_labels_are_readable_while_duration_geometry_keeps_real_width():
    run_view("""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const first=V.toolGeometry({start:10,end:13},[0,1000],900),second=V.toolGeometry({start:11,end:17},[0,1000],900);
      assert.ok(first.labelWidth>=160);assert.ok(second.labelWidth>=160);
      assert.ok(Math.abs(first.spanWidth-3*730/1000)<1e-9);
      assert.ok(Math.abs(second.spanWidth-6*730/1000)<1e-9);
      assert.ok(first.spanWidth<first.labelWidth);assert.ok(first.startX<second.startX);
      assert.equal(first.endX-first.startX,first.spanWidth);
    """)


def test_agent_branches_and_directed_messages_require_explicit_causal_ids():
    run_view("""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const e=(seq,kind,t,session_id,fields={})=>({seq,kind,elapsed_ms:t,session_id,...fields});
      const snapshot={agents:[{id:'parent'},{id:'child',parent_id:'parent'}],trace:{active:[],events:[],timeline:[
        e(1,'turn.started',0,'parent'),e(2,'agent.created',20,'child',{parent_session_id:'parent',task_id:'task'}),
        e(3,'message.sent',25,'parent',{related_session_id:'child',message_seq:7}),
        e(4,'message.read',40,'child',{related_session_id:'parent',message_seq:7}),
        e(5,'message.sent',50,'parent',{related_session_id:'child',message_seq:8})]}};
      const view=V.timeline(snapshot);assert.equal(view.lanes[1].parentId,'parent');assert.equal(view.lanes[1].start,20);
      assert.equal(view.communications.length,2);assert.equal(view.communications[0].from,'parent');assert.equal(view.communications[0].to,'child');
      assert.equal(view.communications[0].start,25);assert.equal(view.communications[0].end,40);
      assert.equal(view.communications[1].pending,true);assert.equal(view.communications[1].read,undefined);
      assert.equal(V.agentColor('parent'),V.agentColor('parent'));
      assert.equal(V.timeline(snapshot,'child').communications.length,2);
    """)


def test_messages_match_first_valid_read_for_both_endpoints_and_reused_sequence():
    run_view("""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const e=(seq,kind,t,from,to,message_seq)=>({seq,kind,elapsed_ms:t,session_id:from,related_session_id:to,message_seq});
      const view=V.timeline({agents:[{id:'p'},{id:'a',parent_id:'p'},{id:'b',parent_id:'p'}],trace:{now_elapsed_ms:200,timeline:[
        e(1,'message.read',1,'a','p',7),
        e(2,'message.sent',10,'p','a',7),e(3,'message.sent',11,'p','b',7),
        e(4,'message.read',12,'b','p',7),e(5,'message.read',13,'a','p',7),e(6,'message.read',14,'a','p',7),
        e(7,'message.sent',20,'p','a',7),e(8,'message.read',19,'a','p',7),e(9,'message.read',25,'a','p',7),
        e(10,'message.sent',30,'a','p',7),e(11,'message.read',31,'p','a',7),
        e(12,'message.sent',40,'p','a',8),e(13,'message.read',39,'a','p',8),
        e(14,'message.sent',50,'a','b',1),e(15,'message.read',51,'b','a',1),
        e(16,'message.sent',60,'p','b',9)]}});
      assert.deepEqual(view.communications.map(edge=>[edge.sent.seq,edge.read?.seq]),[[2,5],[3,4],[7,9],[10,11],[12,undefined],[16,undefined]]);
      assert.deepEqual(view.communications.filter(edge=>edge.pending).map(edge=>edge.sent.seq),[12,16]);
      assert.ok(view.communications.every(edge=>edge.end>=edge.start));
      assert.ok(view.communications.every(edge=>!edge.read||edge.read.seq>=edge.sent.seq));
    """)


def test_ten_thousand_message_events_use_a_linear_read_index():
    # Reproduce the 5k sends + 5k reads workload without a noisy wall-clock threshold.
    # Counting kind access detects the former per-send full-array find operation.
    run_view("""
      const assert=require('node:assert/strict');let kindReads=0;const count=5000,events=[];
      const e=(seq,kind,session_id,related_session_id,message_seq)=>({seq,elapsed_ms:seq,session_id,related_session_id,message_seq,get kind(){kindReads++;return kind;}});
      for(let index=0;index<count;index++)events.push(e(index+1,'message.sent','p','a',index));
      for(let index=0;index<count;index++)events.push(e(count+index+1,'message.read','a','p',index));
      const view=AgentDebugView.timeline({agents:[{id:'p'},{id:'a',parent_id:'p'}],trace:{timeline:events}});
      assert.equal(view.communications.length,count);assert.equal(view.communications.filter(edge=>edge.pending).length,0);
      assert.ok(kindReads<30*events.length,`Expected linear metadata visits, got ${kindReads}`);
    """)


def test_metric_domains_are_shared_sources_are_explicit_and_unknown_is_a_gap():
    run_view("""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const e=(seq,t,metrics,reason)=>({seq,kind:'context.sample',elapsed_ms:t,session_id:'a',metrics,reason});
      const snapshot={trace:{active:[],events:[],timeline:[
        e(1,10,{context_utilization:.8,input_tokens:10,output_tokens:2,tokens_source:'provider'},'compact.before'),
        e(2,20,{context_utilization:.3,input_tokens:5,output_tokens:1,tokens_source:'estimated'},'compact.after'),
        e(3,30,{tokens_source:'unknown'}),e(4,40,{input_tokens:4,output_tokens:3,tokens_source:'provider'})]}};
      const view=V.timeline(snapshot), request=V.metrics(view,'request'), cumulative=V.metrics(view,'cumulative');
      assert.deepEqual(request.domain,view.domain);assert.deepEqual(cumulative.domain,view.domain);
      assert.deepEqual(request.context.map(p=>p.value),[80,30,null,null]);
      assert.deepEqual(request.tokens.map(p=>p.input),[10,5,null,4]);
      assert.deepEqual(cumulative.tokens.map(p=>p.input),[10,15,null,19]);
      assert.deepEqual(request.tokens.map(p=>p.source),['provider','estimated','unknown','provider']);
      assert.equal(request.context[0].reason,'compact.before');assert.equal(request.context[1].reason,'compact.after');
    """)


def test_legacy_spans_remain_visible_without_making_up_causal_relations():
    run_view("""
      const assert=require('node:assert/strict');const snapshot={agents:[{id:'a'}],trace:{events:[{id:1,session_id:'a',module:'model',name:'request',started_at:100,duration_ms:80,state:'completed'}],active:[]}};
      const view=AgentDebugView.timeline(snapshot);assert.equal(view.spans.length,1);assert.equal(view.spans[0].end-view.spans[0].start,80);
      assert.equal(view.groups.length,0);assert.equal(view.communications.length,0);
    """)


def test_real_parent_and_child_ids_receive_stable_distinct_colors():
    run_view("""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const parent='9e157d5e8b1d4221aa5726513c6e9931',child='2cf050bc36f94c7aa5d5608f2e5bfe0d';
      const first=V.agentColor(parent),second=V.agentColor(child);assert.notEqual(first,second);
      const hue=color=>Number(color.match(/hsl\\((\\d+(?:\\.\\d+)?)/)[1]);
      const delta=Math.abs(hue(first)-hue(second));assert.ok(Math.min(delta,360-delta)>=27);
      assert.equal(V.agentColor(parent),first);assert.equal(V.agentColor(child),second);
      const view=V.timeline({agents:[{id:parent},{id:child,parent_id:parent}],trace:{timeline:[]}});
      assert.equal(view.lanes[0].color,first);assert.equal(view.lanes[1].color,second);
    """)


def test_reused_tool_ids_are_scoped_to_turn_and_tool_phases_keep_one_batch():
    run_view("""
      const assert=require('node:assert/strict');
      const e=(seq,kind,t,turn_id,fields={})=>({seq,kind,elapsed_ms:t,session_id:'a',turn_id,batch_id:'batch',tool_call_id:'call',...fields});
      const view=AgentDebugView.timeline({trace:{now_elapsed_ms:300,active:[],events:[],timeline:[
        e(1,'tool.started',10,'first'),e(2,'phase.changed',15,'first',{phase:'tools'}),
        e(3,'tool.started',20,'first',{tool_call_id:'other'}),e(4,'tool.completed',80,'first'),
        e(5,'tool.started',120,'second'),e(6,'tool.completed',200,'second')
      ]}});
      assert.equal(view.groups.length,2);assert.equal(view.groups[0].calls.length,2);
      assert.equal(view.groups[0].calls[0].end,80);assert.equal(view.groups[0].calls[1].end,300);
      assert.equal(view.groups[1].calls[0].start,120);assert.equal(view.groups[1].calls[0].end,200);
    """)


def test_cumulative_tokens_are_independent_per_agent_and_unknown_does_not_become_zero():
    run_view("""
      const assert=require('node:assert/strict');
      const e=(seq,session_id,input_tokens,tokens_source)=>({seq,kind:'model.completed',elapsed_ms:seq,session_id,metrics:{input_tokens,tokens_source}});
      const view=AgentDebugView.timeline({trace:{timeline:[e(1,'a',10,'provider'),e(2,'b',30,'provider'),e(3,'a',5,'estimated'),e(4,'a',90,'unknown'),e(5,'a',2,'provider')]}});
      const result=AgentDebugView.metrics(view,'cumulative');assert.deepEqual(result.tokens.map(p=>p.input),[10,30,15,null,17]);
      assert.deepEqual(result.tokens.map(p=>p.output),[null,null,null,null,null]);
    """)


def test_event_metadata_is_safe_and_detail_reads_are_explicit_bounded_and_stale_safe():
    run_view("\n".join(frontend_function(name) for name in ("eventMetadata", "clearEventDetail", "selectEvent", "loadEventDetail")) + r'''
      const assert=require('node:assert/strict'),nodes={};const $=id=>nodes[id]||={textContent:'',hidden:false,disabled:false};
      let selectedEvent=null,detailNext=null,detailBusy=false,detailRevision=0,detailAbort=null,timelineWindow={generation:'one'};
      const states={},short=id=>id,duration=String,eventLabel=()=> '工具';let calls=[];
      const fetch=async(path,options)=>new Promise(resolve=>calls.push({path,options,resolve}));
      (async()=>{
        selectEvent({seq:3,kind:'tool.completed',session_id:'a',tool_name:'command',command:'SECRET_COMMAND',args:{key:'SECRET_ARGS'},output:'RAW_OUTPUT'});
        assert.equal(calls.length,0);assert.doesNotMatch($('event-detail-meta').textContent,/SECRET_COMMAND|SECRET_ARGS|RAW_OUTPUT/);
        const pending=loadEventDetail();assert.equal(calls[0].path,'/api/debug/events/3/detail?generation=one&offset=0&limit=6000');
        calls[0].resolve({ok:true,json:async()=>({content:'<script>literal</script>',format:'text',next_offset:6000,total_chars:7000})});await pending;
        assert.equal($('event-detail-content').textContent,'<script>literal</script>');assert.equal(detailNext,6000);
        const next=loadEventDetail(detailNext);assert.match(calls[1].path,/offset=6000&limit=6000$/);
        clearEventDetail();assert.equal(calls[1].options.signal.aborted,true);timelineWindow.generation='two';
        calls[1].resolve({ok:true,json:async()=>({content:'STALE'})});await next;assert.equal($('event-detail-content').textContent,'');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_plan_event_label_shows_fixed_step_and_state_without_user_title():
    run_view(frontend_function("eventLabel") + """
      const assert=require('node:assert/strict'),eventLabels={'plan.updated':'计划更新'},phaseLabels={};
      assert.equal(eventLabel({kind:'plan.updated',stage_index:1,state:'running',title:'PRIVATE_TITLE'}),'步骤 1 · 执行中');
      assert.equal(eventLabel({kind:'plan.updated',stage_index:3,state:'completed',title:'PRIVATE_TITLE'}),'步骤 3 · 完成');
      assert.equal(eventLabel({kind:'plan.updated',stage_index:2,state:'PRIVATE_STATE'}),'步骤 2 · 状态未知');
      assert.equal(eventLabel({kind:'plan.updated',state:'running'}),'计划更新');
    """)


def test_pause_stops_polling_and_resume_rebuilds_a_gap_from_an_authoritative_snapshot():
    run_view(r'''
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div'){this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.value='';this.open=false;this.hidden=false;this.disabled=false;}
        setAttribute(){} append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(){return [];} addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag)},window={location:{search:''}};
      let calls=[],timers=[],exported;const setTimeout=callback=>timers.push(callback);
      URL.createObjectURL=blob=>{exported=blob;return 'blob:test';};URL.revokeObjectURL=()=>{};
      const event=seq=>({seq,kind:'phase.changed',session_id:'a',elapsed_ms:seq});
      const frame=(cursor,timeline,extra={})=>({runtime:{active_agents:0,active_model_calls:0,active_tools:[]},agents:[],agents_omitted:0,trace:{active:[],events:[],capacity:100,dropped:0,generation:'one',cursor,timeline,...extra}});
      const frames=[frame(1,[event(1)]),frame(5,[event(5)],{gap:true,incremental:true}),frame(5,[event(4),event(5)])];
      const fetch=async(path,options)=>{calls.push({path,options});return{ok:true,json:async()=>frames.shift()};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    ''' + (STATIC / "debug.js").read_text() + r'''
      (async()=>{
        await tick();assert.equal(calls.length,1);await nodes.pause.click();timers.shift()();await tick();assert.equal(calls.length,1);
        await nodes.pause.click();timers.shift()();await tick();assert.equal(calls[1].path,'/api/debug?cursor=1&generation=one');assert.equal(calls[2].path,'/api/debug');
        await nodes.export.click();const result=JSON.parse(await exported.text());assert.deepEqual(result.trace.timeline.map(e=>e.seq),[4,5]);
        assert.match(nodes.retention.textContent,/窗口已重建/);assert.ok(calls.every(call=>!call.options.method));
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')


def test_svg_render_and_expansion_preserve_parallel_coordinates_without_fetching_details():
    run_view(r'''
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div'){this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'a',...fields});
      const frame={runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'a'},{id:'child',parent_id:'a'}],agents_omitted:0,trace:{active:[],events:[{id:77,session_id:'a',module:'persistence',name:'save',started_at:.01,duration_ms:2,state:'completed'}],capacity:100,dropped:0,generation:'one',cursor:10,timeline:[
        e(1,'turn.started',0),e(2,'agent.created',5,{session_id:'child',parent_session_id:'a'}),
        e(3,'tool.started',10,{batch_id:'b',tool_call_id:'x',tool_name:'one'}),e(4,'tool.started',20,{batch_id:'b',tool_call_id:'y',tool_name:'two'}),
        e(5,'tool.completed',80,{batch_id:'b',tool_call_id:'y',tool_name:'two',state:'error'}),e(6,'tool.completed',100,{batch_id:'b',tool_call_id:'x',tool_name:'one',state:'completed'}),
        e(7,'context.sample',110,{reason:'after_compact',metrics:{context_utilization:.3}}),e(8,'model.completed',120,{metrics:{input_tokens:20,output_tokens:5,tokens_source:'estimated'}}),
        e(9,'message.sent',125,{related_session_id:'child',message_seq:1}),e(10,'model.final',140)]}};
      let requests=[],timers=[];const setTimeout=callback=>timers.push(callback);const fetch=async(path,options)=>{requests.push({path,options});return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    ''' + (STATIC / "debug.js").read_text() + r'''
      (async()=>{
        await tick();assert.equal(requests.length,1);assert.match(nodes['timeline-status'].textContent,/10 事件/);
        assert.match(nodes['timeline-summary'].children[0].textContent,/2 Agent/);
        assert.equal(nodes['context-chart'].children[0].tag,'svg');assert.equal(nodes['token-chart'].children[0].tag,'svg');
        const scene=()=>nodes['timeline-lanes'].children[0],buttons=()=>scene().querySelectorAll('button');
        const group=buttons().find(button=>button.className.includes('tool-batch'));assert.ok(group);
        assert.match(group.children.find(child=>child.className==='flow-detail').textContent,/one 1 · two 1/,
          'The summary names the two concrete tool actions');
        assert.match(group.title,/one.*two/,'Hover reveals both original tool names');
        assert.equal(buttons().filter(button=>button.className.includes('module-interval')).length,0);
        await group.click();assert.equal(requests.length,1);assert.equal(nodes['event-detail-load'].disabled,false);
        assert.equal(buttons().filter(button=>button.className.includes('tool-call')).length,0,'Tool details do not expand the lane');
        assert.equal(parseFloat(scene().style.height),224+38+72+20);
        const rows=nodes['event-selection-list'].children;
        assert.ok(rows.some(row=>row.children[1].textContent.includes('one')));
        assert.ok(rows.some(row=>row.children[1].textContent.includes('two')));
        await rows.find(row=>row.children[1].textContent.includes('two')).click();
        assert.equal(requests.length,1);assert.match(nodes['event-detail-meta'].textContent,/tool_call_id/);
        const initial=AgentDebugView.timeline(frame),calls=initial.groups[0].calls;
        assert.deepEqual(calls.map(call=>[call.start,call.end]),[[10,100],[20,80]],'Parallel calls keep independent time intervals');
        assert.ok(AgentDebugView.toolGeometry(calls[0],initial.domain,900).spanWidth>AgentDebugView.toolGeometry(calls[1],initial.domain,900).spanWidth);
        nodes['show-module-spans'].checked=true;await Promise.all(nodes['show-module-spans'].handlers.change.map(handler=>handler()));
        assert.equal(buttons().filter(button=>button.className.includes('module-interval')).length,1);
        frame.trace.timeline.find(event=>event.seq===6).elapsed_ms=13;frame.trace.timeline.find(event=>event.seq===5).elapsed_ms=26;
        await Promise.all(nodes['show-module-spans'].handlers.change.map(handler=>handler()));
        const shorter=AgentDebugView.timeline(frame),shortCalls=shorter.groups[0].calls;
        assert.deepEqual(shortCalls.map(call=>[call.start,call.end]),[[10,13],[20,26]]);
        assert.ok(Math.abs(AgentDebugView.toolGeometry(shortCalls[0],shorter.domain,900).spanWidth-3*730/1000)<1e-9);
        assert.ok(Math.abs(AgentDebugView.toolGeometry(shortCalls[1],shorter.domain,900).spanWidth-6*730/1000)<1e-9);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')
