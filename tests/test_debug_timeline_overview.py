"""Dense timeline overview regressions, exercised without a browser or model."""

from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "src/ai_agent_startup/web/static"


def run_view(source: str) -> None:
    node = shutil.which("node") or "/home/elaine_vuken/.nvm/versions/node/v24.21.0/bin/node"
    if not Path(node).exists():
        pytest.skip("Node is required for pure timeline tests")
    result = subprocess.run(
        [node, "-e", (STATIC / "debug-view.js").read_text() + source],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


DENSE_EVENTS = r"""
  const events=[];
  const put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'parent',...fields});
  put('turn.started',0);
  for(let i=0;i<500;i++) {
    const base=i*12+1, id=`call-${i}`;
    put('phase.changed',base,{phase:i%2?'model':'tools'});
    put('model.final',base+1);
    put('tool.started',base+2,{batch_id:`batch-${i}`,tool_call_id:id,tool_name:'command'});
    put('tool.completed',base+5,{batch_id:`batch-${i}`,tool_call_id:id,tool_name:'command',state:i===333?'error':'completed',metrics:i%100===0?{input_tokens:3,output_tokens:1,tokens_source:'provider'}:undefined});
  }
  const snapshot={agents:[{id:'parent'}],trace:{timeline:events,active:[],events:[],now_elapsed_ms:6000}};
  const view=AgentDebugView.timeline(snapshot), overview=AgentDebugView.overview(view,900);
"""


def test_dense_phase_and_tool_stream_has_bounded_overview_lane():
    run_view(DENSE_EVENTS + r"""
      const assert=require('node:assert/strict');
      assert.equal(view.groups.length,500);
      assert.equal(view.upper.length,1000);
      assert.equal(overview.lanes.length,1);
      const lane=overview.lanes[0];
      assert.equal(lane.id,'parent');
      assert.ok(Number.isFinite(lane.lineY));
      assert.equal(lane.height,224,'Overview lane height is independent of event density');
      assert.equal(lane.lineY,lane.top+108);
      assert.equal(overview.height,224);
      assert.ok(lane.phases.length<500,'Phases must be clustered at overview scale');
      assert.ok(lane.tools.length<500,'Tool batches must share overview regions');
      assert.ok(lane.phases.length>0&&lane.tools.length>0);
    """)


def test_overview_regions_keep_all_source_events_and_tool_intervals():
    run_view(DENSE_EVENTS + r"""
      const assert=require('node:assert/strict'), lane=overview.lanes[0];
      const phaseSeqs=view.upper.filter(e=>e.kind==='phase.changed').map(e=>e.seq);
      const toolSeqs=view.groups.flatMap(group=>group.events.map(e=>e.seq));
      const collected=regions=>regions.flatMap(region=>region.events.map(e=>e.seq)).sort((a,b)=>a-b);
      assert.deepEqual(collected(lane.phases),phaseSeqs);
      assert.deepEqual(collected(lane.tools),toolSeqs.sort((a,b)=>a-b));
      assert.deepEqual(lane.tools.flatMap(region=>region.groups.map(g=>g.id)),view.groups.map(g=>g.id));
      assert.ok(lane.tools.every(region=>region.event&&region.events.includes(region.event)));
      assert.ok(lane.phases.every(region=>region.event&&region.events.includes(region.event)));
      assert.ok(lane.tools.every(region=>Number.isFinite(region.start)&&Number.isFinite(region.end)&&region.end>=region.start));
      assert.equal(Math.min(...lane.tools.map(region=>region.start)),view.groups[0].start);
      assert.equal(Math.max(...lane.tools.map(region=>region.end)),view.groups.at(-1).end);
      const errorRegion=lane.tools.find(region=>region.events.some(e=>e.seq===1337));
      assert.ok(errorRegion&&errorRegion.errors>=1,'An error remains visible in its region');
    """)


def test_overview_keeps_branch_messages_and_metrics_at_original_endpoints():
    run_view(r"""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const e=(seq,kind,time,session_id,fields={})=>({seq,kind,elapsed_ms:time,session_id,...fields});
      const events=[e(1,'turn.started',0,'parent'),e(2,'agent.created',10,'child',{parent_session_id:'parent'}),
        e(3,'message.sent',20,'parent',{related_session_id:'child',message_seq:9}),
        e(4,'context.sample',25,'parent',{metrics:{context_utilization:.5}}),
        e(5,'message.read',30,'child',{related_session_id:'parent',message_seq:9}),
        e(6,'model.completed',35,'child',{metrics:{input_tokens:7,output_tokens:2,tokens_source:'provider'}})];
      const view=V.timeline({agents:[{id:'parent'},{id:'child',parent_id:'parent'}],trace:{timeline:events,active:[],events:[]}});
      const overview=V.overview(view,900), edges=overview.communications;
      assert.equal(overview.lanes.length,2);
      assert.equal(overview.lanes.find(l=>l.id==='child').parentId,'parent');
      assert.equal(overview.lanes.find(l=>l.id==='child').branch.seq,2);
      assert.equal(edges.length,1);
      assert.deepEqual([edges[0].from,edges[0].to,edges[0].start,edges[0].end],['parent','child',20,30]);
      assert.equal(edges[0].sent.seq,3);assert.equal(edges[0].read.seq,5);
      assert.deepEqual(V.metrics(view,'request').context.map(p=>p.value),[50]);
      assert.deepEqual(V.metrics(view,'request').tokens.map(p=>[p.input,p.output]),[[7,2]]);
    """)


def test_busy_phase_tail_reaches_live_clock_then_stops_at_turn_end():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const event=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'agent',...fields});
      const phase=event(2,'phase.changed',20,{phase:'confirmation'});
      const frame=(status,events,busy)=>({agents:[{id:'agent',status,busy}],trace:{timeline:events,active:[],events:[],now_elapsed_ms:5000}});
      const liveView=V.timeline(frame('confirming',[event(1,'turn.started',0),phase]));
      const live=V.overview(liveView,900);
      assert.equal(live.lanes[0].phases.length,1);
      assert.equal(live.lanes[0].phases[0].start,20);
      assert.equal(live.lanes[0].phases[0].end,5000,'A busy confirmation phase reaches the live clock');
      assert.ok(liveView.domain[1]>=5000,'The shared timeline domain must include the live phase tail');
      const busy=V.overview(V.timeline(frame('idle',[event(1,'turn.started',0),phase],true)),900);
      assert.equal(busy.lanes[0].phases[0].end,5000,'The explicit busy flag keeps the phase live');
      const stopped=V.overview(V.timeline(frame('completed',[event(1,'turn.started',0),phase,event(3,'turn.stopped',100,{reason:'finished'})])),900);
      assert.equal(stopped.lanes[0].phases[0].end,100,'A stopped phase must not extend to the later poll clock');
    """)


def test_phase_segments_stop_at_turn_boundary_before_later_turn():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const event=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'agent',...fields});
      const snapshot={agents:[{id:'agent',status:'completed'}],trace:{timeline:[
        event(1,'turn.started',0),event(2,'phase.changed',20,{phase:'tools'}),
        event(3,'turn.stopped',100,{reason:'finished'}),event(4,'turn.started',900),
        event(5,'phase.changed',1000,{phase:'model_wait'}),event(6,'turn.stopped',1100,{reason:'finished'})
      ],active:[],events:[],now_elapsed_ms:5000}};
      const phases=V.overview(V.timeline(snapshot),900).lanes[0].phases;
      assert.equal(phases.length,2);
      assert.deepEqual(phases.map(region=>[region.start,region.end]),[[20,100],[1000,1100]]);
    """)


def test_dense_default_render_expands_only_selected_tools_locally():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const events=[],put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'parent',...fields});
      put('turn.started',0);
      for(let index=0;index<500;index++) {
        const time=index*12+1;
        put('phase.changed',time,{phase:'tools'});
        put('model.final',time+1);
        put('tool.started',time+2,{batch_id:`batch-${index}`,tool_call_id:`call-${index}`,tool_name:'command'});
        put('tool.completed',time+5,{batch_id:`batch-${index}`,tool_call_id:`call-${index}`,tool_name:'command',state:'completed'});
      }
      const frame={runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'parent'}],agents_omitted:0,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor:events.length,timeline:events}};
      let requests=[],timers=[];const setTimeout=callback=>timers.push(callback);const fetch=async(path,options)=>{requests.push({path,options});return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0],buttons=()=>scene().querySelectorAll('button');
        const batches=()=>buttons().filter(button=>button.className.includes('tool-batch'));
        const calls=()=>buttons().filter(button=>button.className.includes('tool-call'));
        assert.equal(parseFloat(scene().style.height),224,'Dense events stay in one fixed-height lane');
        assert.ok(batches().length>1&&batches().length<500,'Overview must expose several aggregated tool regions');
        assert.equal(calls().length,0);
        await batches()[0].click();
        assert.equal(calls().length,0,'Tool details open below the timeline, not inside the lane');
        assert.equal(parseFloat(scene().style.height),224,'Inspection does not enlarge the lane');
        assert.equal(nodes['event-selection'].hidden,false);
        assert.ok(nodes['event-selection-list'].children.length>0&&nodes['event-selection-list'].children.length<=20);
        const open=batches().filter(button=>button.attributes['aria-expanded']==='true');
        assert.equal(open.length,1);
        const another=batches().find(button=>button.dataset.eventKey!==open[0].dataset.eventKey);
        await another.click();
        assert.equal(batches().filter(button=>button.attributes['aria-expanded']==='true').length,1);
        assert.equal(calls().length,0);
        assert.equal(parseFloat(scene().style.height),224);
        assert.equal(requests.length,1,'Expanding an overview region must not fetch event details');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_dense_region_inspector_pages_full_membership_and_resets_stale_selection():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const events=[],put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'parent',...fields});
      put('turn.started',0);
      for(let index=0;index<500;index++)put('phase.changed',1+index*.01,{phase:'tools'});
      for(let index=0;index<500;index++) {
        const time=100+index*.02;
        put('tool.started',time,{batch_id:`batch-${index}`,tool_call_id:`call-${index}`,tool_name:'command'});
        put('tool.completed',time+.01,{batch_id:`batch-${index}`,tool_call_id:`call-${index}`,tool_name:'command',state:index===333?'error':'completed'});
      }
      const frame={runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'parent'}],agents_omitted:0,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor:events.length,timeline:events}};
      let requests=[],timers=[];const setTimeout=callback=>timers.push(callback);const fetch=async(path,options)=>{requests.push({path,options});return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0],buttons=()=>scene().querySelectorAll('button');
        const rows=()=>nodes['event-selection-list'].children;
        const flows=buttons().filter(button=>button.className.includes('flow-branch-down'));
        assert.ok(flows.length>0);
        assert.equal(buttons().filter(button=>button.className.includes('phase-region')).length,0,'The phase color band is gone');
        await flows[0].click();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.equal(nodes['selection-page'].textContent,'1–20 / 1001','500 phases and 500 calls stay reachable');
        assert.equal(rows().length,20);
        await nodes['selection-next'].click();
        assert.equal(nodes['selection-page'].textContent,'21–40 / 1001');
        await rows()[0].click();
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).seq,21);
        assert.equal(requests.length,1,'Selecting a row must not fetch payload details');

        const tools=buttons().filter(button=>button.className.includes('tool-batch'));
        assert.ok(tools.length>0&&tools[0].className.includes('event-error'),'A clustered error stays visible');
        assert.equal(buttons().filter(button=>button.className.includes('tool-call')).length,0,'Calls inspect below the timeline');
        for(let page=2;page<=41;page++) {
          await nodes['selection-next'].click();
          if(page===41)assert.ok(rows()[14].className.includes('event-error'),'The failed call stays marked in the full inspector');
        }
        for(let page=42;page<=50;page++)await nodes['selection-next'].click();
        assert.equal(nodes['selection-page'].textContent,'1001–1001 / 1001');
        assert.equal(rows().length,1);
        await rows()[0].click();
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).calls[0].seq,1501);
        assert.equal(requests.length,1);
        await nodes['selection-close'].click();
        assert.equal(nodes['event-selection'].hidden,true);
        assert.equal(parseFloat(scene().style.height),224);

        await buttons().find(button=>button.className.includes('flow-branch-down')).click();
        frame.trace.timeline=[events[0]];
        await Promise.all(nodes['show-module-spans'].handlers.change.map(handler=>handler()));
        assert.equal(nodes['event-selection'].hidden,true,'A retained-window change clears stale region members');
        assert.equal(nodes['event-detail-load'].disabled,true);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_live_tool_completion_and_message_read_refresh_selected_inspector():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,session_id,fields={})=>({seq,kind,elapsed_ms:time,session_id,...fields});
      const started=e(3,'tool.started',20,'parent',{batch_id:'batch',tool_call_id:'call',tool_name:'command'});
      const sent=e(4,'message.sent',30,'parent',{related_session_id:'child',message_seq:7});
      const completed=e(5,'tool.completed',50,'parent',{batch_id:'batch',tool_call_id:'call',tool_name:'command',state:'completed'});
      const read=e(6,'message.read',70,'child',{related_session_id:'parent',message_seq:7});
      const base={runtime:{active_agents:2,active_model_calls:0,active_tools:[]},agents:[{id:'parent'},{id:'child',parent_id:'parent'}],agents_omitted:0};
      const frame=(cursor,timeline,incremental=false)=>({...base,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor,timeline,incremental,now_elapsed_ms:100}});
      const frames=[frame(4,[e(1,'turn.started',0,'parent'),e(2,'agent.created',10,'child',{parent_session_id:'parent'}),started,sent]),frame(5,[completed],true),frame(6,[read],true)];
      let requests=[],timers=[];const setTimeout=callback=>timers.push(callback);const fetch=async(path,options)=>{requests.push({path,options});return{ok:true,json:async()=>frames.shift()};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0],buttons=()=>scene().querySelectorAll('button');
        const descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
        const edges=()=>descendants(scene()).filter(node=>node.attributes.class==='message-edge');
        const summaries=()=>descendants(scene()).filter(node=>node.className==='message-summary');
        const rows=()=>nodes['event-selection-list'].children;
        await buttons().find(button=>button.className.includes('tool-batch')).click();
        assert.ok(rows().some(row=>/执行中/.test(row.children[1].textContent)),'Active call is visible in the inspector');
        assert.equal(buttons().filter(button=>button.className.includes('tool-call')).length,0);
        timers.shift()();await tick();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.ok(rows().some(row=>/完成/.test(row.children[1].textContent)),'Active call updates in the open inspector');
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).calls[0].kind,'tool.completed');
        assert.equal(buttons().filter(button=>button.className.includes('tool-call')).length,0,'Inspector leaves the lane fixed');
        assert.equal(requests.length,2,'The updated inspector must not fetch payload details');

        nodes['agent-filter'].value='parent';
        await Promise.all(nodes['agent-filter'].handlers.change.map(handler=>handler()));
        assert.equal(edges().length,0,'Parent filtering leaves the scene free of message curves');
        assert.ok(summaries().length>0,'Parent filtering retains a local communication entry');
        await summaries()[0].click();
        assert.equal(nodes['selection-page'].textContent,'1–1 / 1');
        assert.equal(nodes['communication-routes'].children.length,1);
        assert.match(nodes['communication-routes'].children[0].children.at(-1).textContent,/待接收/);
        timers.shift()();await tick();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.equal(nodes['selection-page'].textContent,'1–2 / 2');
        assert.match(nodes['communication-routes'].children[0].children.at(-1).textContent,/已读/);
        assert.match(rows()[1].children[1].textContent,/已读/,'Pending message gains its actual read event');
        await rows()[1].click();
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).seq,6);
        assert.equal(requests.length,3);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_selected_multicall_batch_refreshes_summary_after_other_call_fails():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,tool_call_id,fields={})=>({seq,kind,elapsed_ms:time,session_id:'agent',batch_id:'batch',tool_call_id,tool_name:'command',...fields});
      const frame=(cursor,timeline,incremental=false)=>({runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'agent'}],agents_omitted:0,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor,timeline,incremental,now_elapsed_ms:100}});
      const frames=[frame(3,[{seq:1,kind:'turn.started',elapsed_ms:0,session_id:'agent'},e(2,'tool.started',20,'A'),e(3,'tool.started',30,'B')]),
        frame(4,[e(4,'tool.completed',50,'A',{state:'completed'})],true),
        frame(5,[e(5,'tool.completed',70,'B',{state:'error'})],true)];
      let requests=[],timers=[];const setTimeout=callback=>timers.push(callback);
      const fetch=async(path,options)=>{requests.push({path,options});return{ok:true,json:async()=>path.startsWith('/api/debug/events/')?{content:'LOADED DETAIL',format:'text',total_chars:13}:frames.shift()};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0];
        const batch=()=>scene().querySelectorAll('button').find(button=>button.className.includes('tool-batch'));
        await batch().click();
        timers.shift()();await tick();
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).calls[0].kind,'tool.completed');
        assert.match(nodes['event-detail-summary'].textContent,/异常 0/);
        await nodes['event-detail-load'].click();await tick();
        assert.equal(nodes['event-detail-content'].textContent,'LOADED DETAIL');
        timers.shift()();await tick();
        assert.match(nodes['event-detail-summary'].textContent,/异常 1/,'The batch summary reflects a different call failing');
        const meta=JSON.parse(nodes['event-detail-meta'].textContent);
        assert.equal(meta.calls.length,2);
        assert.equal(meta.calls[0].kind,'tool.completed');
        assert.equal(meta.calls[1].kind,'tool.completed');
        assert.equal(meta.calls[1].state,'error');
        assert.equal(meta.end_ms,70);
        assert.equal(nodes['event-detail-content'].textContent,'LOADED DETAIL','Refreshing batch metadata preserves loaded detail');
        assert.equal(requests.filter(call=>call.path.startsWith('/api/debug/events/')).length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)
