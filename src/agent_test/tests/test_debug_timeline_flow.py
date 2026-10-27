"""细时间线的事件分流与密集布局回归，不调用模型。"""

from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "static"


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


def test_flow_direction_keeps_user_waits_above_and_execution_below():
    run_view(r"""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const event=(kind,fields={})=>({kind,...fields});
      for(const phase of ['context_prepare','compaction','confirmation','awaiting_input','waiting_input'])
        assert.equal(V.flowSide(event('phase.changed',{phase})),'up',phase);
      for(const phase of ['model','model_wait','continuation','tools','planning'])
        assert.equal(V.flowSide(event('phase.changed',{phase})),'down',phase);
      for(const kind of ['context.compacted','model.final'])
        assert.equal(V.flowSide(event(kind)),'up',kind);
      assert.equal(V.flowSide(event('turn.stopped',{reason:'finished'})),'up');
      for(const kind of ['turn.started','tools.batch','plan.updated','tool.started','tool.completed'])
        assert.equal(V.flowSide(event(kind)),'down',kind);
      assert.equal(V.flowSide(event('context.sample',{metrics:{context_utilization:.5}})),null);
      assert.equal(V.flowSide(event('message.sent',{related_session_id:'child'})),null);
      for(const item of [event('phase.changed',{phase:'model_wait'}),event('phase.changed',{phase:'confirmation'}),
        event('plan.updated'),event('tool.completed',{tool_name:'command'}),event('model.final')]) {
        const label=V.flowLabel(item);
        assert.equal(typeof label,'string');
        assert.ok(label.length>0&&label.length<=20,`Unusable short label: ${label}`);
      }
    """)


def test_dense_flow_regions_preserve_every_event_without_same_side_overlap():
    run_view(r"""
      const assert=require('node:assert/strict'), V=AgentDebugView, events=[];
      const put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'parent',...fields});
      put('turn.started',0);
      for(let index=0;index<250;index++) {
        const time=index*20+1;
        put('phase.changed',time,{phase:index%2?'context_prepare':'model_wait'});
        put('model.final',time+2);
        put('plan.updated',time+3,{stage_index:index});
        put('tool.started',time+4,{batch_id:`b-${index}`,tool_call_id:`c-${index}`,tool_name:'command'});
        put('tool.completed',time+6,{batch_id:`b-${index}`,tool_call_id:`c-${index}`,tool_name:'command',state:index===100?'error':'completed'});
      }
      put('turn.stopped',5001,{reason:'finished',state:'completed'});
      const view=V.timeline({agents:[{id:'parent'}],trace:{timeline:events,active:[],events:[]}});
      const lane=V.overview(view,900).lanes[0];
      assert.equal(lane.height,224);
      assert.equal(lane.lineY,lane.top+108);
      assert.ok(lane.flowUp.length>0&&lane.flowDown.length>0);
      const expected=side=>events.filter(e=>V.flowSide(e)===side).map(e=>e.seq).sort((a,b)=>a-b);
      for(const [side,regions] of [['up',lane.flowUp],['down',lane.flowDown]]) {
        assert.deepEqual(regions.flatMap(r=>r.events.map(e=>e.seq)).sort((a,b)=>a-b),expected(side),`${side} events must remain inspectable`);
        assert.ok(regions.length<=5,`${side} has too many labels for the available width`);
        regions.forEach((region,index)=>{
          assert.ok(region.event&&region.events.includes(region.event));
          assert.equal(typeof region.label,'string'); assert.ok(region.label.length>0&&region.label.length<=30);
          assert.equal(typeof region.detail,'string');
          assert.ok(Number.isFinite(region.anchorX));
          assert.ok(region.labelX>=140&&region.labelWidth>0&&region.labelX+region.labelWidth<=900);
          if(index)assert.ok(regions[index-1].labelX+regions[index-1].labelWidth<=region.labelX,`${side} labels overlap`);
        });
      }
      assert.ok(lane.branches.length&&lane.tools.length&&lane.phases.length,'Legacy detail data stays available');
    """)


def test_flow_layout_is_fixed_and_keeps_parent_child_messages():
    run_view(r"""
      const assert=require('node:assert/strict'), V=AgentDebugView;
      const e=(seq,kind,time,session_id,fields={})=>({seq,kind,elapsed_ms:time,session_id,...fields});
      const events=[e(1,'turn.started',0,'parent'),e(2,'agent.created',10,'child',{parent_session_id:'parent'}),
        e(3,'message.sent',20,'parent',{related_session_id:'child',message_seq:7}),
        e(4,'phase.changed',25,'child',{phase:'context_prepare'}),
        e(5,'message.read',30,'child',{related_session_id:'parent',message_seq:7}),
        e(6,'tool.started',40,'child',{tool_call_id:'call',tool_name:'command'}),
        e(7,'tool.completed',50,'child',{tool_call_id:'call',tool_name:'command',state:'completed'})];
      const snapshot={agents:[{id:'parent'},{id:'child',parent_id:'parent'}],trace:{timeline:events,active:[],events:[]}};
      const overview=V.overview(V.timeline(snapshot),900), dense=V.overview(V.timeline({agents:[{id:'parent'}],trace:{timeline:[
        ...events.filter(e=>e.session_id==='parent'),...Array.from({length:300},(_,i)=>e(8+i,'model.final',100+i,'parent'))]}}),900);
      assert.equal(overview.lanes.length,2);
      assert.ok(overview.lanes.every(lane=>lane.height===224&&lane.lineY===lane.top+108));
      assert.equal(dense.lanes[0].height,overview.lanes[0].height,'Density must not grow lane height');
      assert.equal(overview.lanes[1].parentId,'parent');
      assert.equal(overview.communications.length,1);
      assert.deepEqual([overview.communications[0].sent.seq,overview.communications[0].read.seq],[3,5]);
    """)


def test_render_uses_clickable_unboxed_flow_and_hairline():
    script = r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.className='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='class')this.className=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'parent',...fields});
      const frame={runtime:{active_agents:1,active_model_calls:0,active_tools:[]},agents:[{id:'parent'}],agents_omitted:0,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor:5,timeline:[
        e(1,'turn.started',0),e(2,'phase.changed',10,{phase:'context_prepare'}),
        e(3,'phase.changed',20,{phase:'model_wait'}),e(4,'model.final',30),e(5,'turn.stopped',40,{reason:'finished',state:'completed'})]}};
      let requests=0;const setTimeout=()=>0;const fetch=async()=>{requests++;return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=nodes['timeline-lanes'].children[0];
        const all=(node)=>[node,...node.children.flatMap(all)];
        const rendered=all(scene), matching=part=>rendered.filter(node=>node.className.includes(part));
        const up=matching('flow-branch-up'),down=matching('flow-branch-down');
        assert.ok(up.length&&down.length,'Events split above and below the spine');
        assert.ok(up.every(node=>node.tag==='button')&&down.every(node=>node.tag==='button'));
        assert.ok(rendered.some(node=>node.className==='flow-title'));
        assert.ok(rendered.some(node=>node.className==='flow-detail'));
        assert.equal(matching('phase-region').length,0);
        assert.equal(matching('behavior-label').length,0);
        assert.equal(matching('activity-trend').length,0);
        for(const line of [...matching('agent-track'),...matching('agent-lifetime')])
          assert.ok(Number(line.attributes['stroke-width'])<=1.2,'Main line must stay thin');
        assert.ok(matching('behavior-arrow-up').length&&matching('behavior-arrow-down').length);
        await up[0].click();
        assert.equal(nodes['event-selection'].hidden,false,'Clicking a flow node opens its event detail list');
        assert.equal(requests,1,'Inspection uses the already loaded events');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """
    run_view(script)


def test_child_tracks_fork_join_and_messages_follow_actual_direction():
    script = r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.className='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='class')this.className=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const events=[],e=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
      e('turn.started',0,'root');
      e('agent.created',100,'child-one',{parent_session_id:'root'});
      e('agent.started',110,'child-one');
      e('message.sent',120,'root',{related_session_id:'child-one',message_seq:1});
      e('message.read',130,'child-one',{related_session_id:'root',message_seq:1});
      e('agent.created',140,'child-two',{parent_session_id:'root'});
      e('agent.started',150,'child-two');
      e('message.sent',160,'child-two',{related_session_id:'root',message_seq:2});
      e('message.read',170,'root',{related_session_id:'child-two',message_seq:2});
      e('agent.completed',300,'child-one',{state:'completed'});
      const frame={runtime:{active_agents:2,active_model_calls:0,active_tools:[]},agents:[{id:'root',status:'running',busy:true},{id:'child-one',parent_id:'root',status:'completed',busy:false},{id:'child-two',parent_id:'root',status:'running',busy:true}],agents_omitted:0,trace:{active:[],events:[],capacity:1000,dropped:0,generation:'one',cursor:events.length,timeline:events,now_elapsed_ms:400}};
      let requests=0;const setTimeout=()=>0,fetch=async()=>{requests++;return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=nodes['timeline-lanes'].children[0],all=node=>[node,...node.children.flatMap(all)],rendered=all(scene);
        const matching=part=>rendered.filter(node=>node.className.split(' ').includes(part));
        const view=AgentDebugView.timeline(frame),story=AgentDebugView.storyboard(view,900),x=AgentDebugView.timeScale(view,900).position;
        assert.equal(story.lanes.length,3);
        assert.deepEqual(story.lanes.map(lane=>lane.parentId),[null,'root','root']);
        assert.deepEqual(story.lanes.map(lane=>lane.height),[224,224,224]);
        assert.equal(matching('agent-fork-bus').length,1);
        assert.equal(matching('agent-join-bus').length,1);
        assert.equal(matching('agent-branch').length,2,'Each child has an individual fork');
        assert.equal(matching('agent-join').length,1,'Only the completed child rejoins its parent');
        assert.equal(matching('agent-track').length,1,'The parent alone owns the full guide line');
        const lifetimes=matching('agent-lifetime');
        assert.equal(lifetimes.length,3);
        for(const [id,start,end] of [['child-one',100,300],['child-two',140,400]]) {
          const lane=story.lanes.find(item=>item.id===id);
          const line=lifetimes.find(item=>Number(item.attributes.y1)===lane.lineY);
          assert.ok(line,`Missing lifetime for ${id}`);
          assert.ok(Math.abs(Number(line.attributes.x1)-x(start))<.001,`${id} starts at its fork`);
          assert.ok(Math.abs(Number(line.attributes.x2)-x(end))<.001,`${id} ends at its own endpoint`);
        }
        assert.equal(matching('message-edge').length,0,'Global message curves do not cross sibling tracks');
        assert.deepEqual(view.communications.map(edge=>[edge.from,edge.to]),[['root','child-one'],['child-two','root']]);
        const summary=matching('message-summary')[0];
        assert.ok(summary,'Parent communication remains locally inspectable');
        await summary.click();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.equal(nodes['selection-page'].textContent,'1–4 / 4');
        assert.deepEqual(nodes['communication-routes'].children.map(row=>row.title),
          ['root → child-one · 消息 #1','child-two → root · 消息 #2'],
          'The local routes preserve both message directions');
        assert.equal(requests,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """
    run_view(script)
