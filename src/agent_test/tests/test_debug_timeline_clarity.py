"""Storyboarding regressions for a readable multi-agent timeline."""

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


COLLABORATION = r"""
  const assert=require('node:assert/strict'), V=AgentDebugView;
  const events=[];
  const add=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
  add('turn.started',0,'root');
  add('phase.changed',40,'root',{phase:'planning'});
  add('agent.created',200,'alpha',{parent_session_id:'root'});
  add('agent.started',210,'alpha');
  add('agent.created',250,'beta',{parent_session_id:'root'});
  add('agent.started',260,'beta');
  add('agent.created',300,'gamma',{parent_session_id:'root'});
  add('agent.started',310,'gamma');
  add('phase.changed',340,'root',{phase:'model_wait'});
  add('tool.started',350,'beta',{tool_call_id:'call-beta',tool_name:'command'});
  add('tool.completed',450,'beta',{tool_call_id:'call-beta',tool_name:'command',state:'completed'});
  add('agent.completed',600,'alpha',{state:'completed'});
  add('agent.completed',700,'gamma',{state:'completed'});
  add('model.final',850,'root');
  const snapshot={agents:[
    {id:'root',status:'running',busy:true},
    {id:'alpha',parent_id:'root',status:'completed',busy:false},
    {id:'beta',parent_id:'root',status:'running',busy:true},
    {id:'gamma',parent_id:'root',status:'completed',busy:false}],
    trace:{timeline:events,active:[],events:[],now_elapsed_ms:1000}};
  const view=V.timeline(snapshot), scale=V.timeScale(view,900), story=V.storyboard(view,900,scale);
"""


def test_collaboration_keeps_every_overlapping_child_on_a_separate_horizontal_branch():
    run_view(COLLABORATION + r"""
      assert.equal(story.roots.length,1);
      assert.equal(story.roots[0].id,'root');
      assert.equal(story.roots[0].height,224);
      assert.equal(story.collaborations.length,1);
      const group=story.collaborations[0];
      assert.equal(group.parentId,'root');
      assert.deepEqual(group.children.map(child=>child.id),['alpha','beta','gamma']);
      assert.deepEqual(group.children.map(child=>child.height),[224,224,224]);
      assert.ok(group.children.every(child=>child.expanded),'Every child shows its behavior immediately');
      assert.equal(group.branches.length,3,'Every child keeps an inspectable branch');
      assert.deepEqual(group.branches.map(branch=>branch.childId),['alpha','beta','gamma']);
      assert.equal(new Set(group.branches.map(branch=>branch.y)).size,3,'Siblings cannot share a horizontal track');
      for(const branch of group.branches) {
        const child=group.children.find(item=>item.id===branch.childId);
        assert.equal(branch.y,child.lineY);
        assert.ok(Math.abs(branch.startX-scale.position(child.start))<.001);
        assert.ok(Math.abs(branch.endX-scale.position(child.end))<.001);
        assert.ok(group.forkBus.x<branch.startX,'The fork bus remains outside child work');
        assert.ok(!group.joinBus || group.joinBus.x>branch.endX,'The join bus remains outside child work');
      }
      assert.deepEqual(group.branches.map(branch=>branch.closed),[true,false,true],
        'An active child must not acquire a fabricated join');
      assert.ok(group.forkBus.fromY<group.forkBus.toY);
      assert.ok(group.joinBus&&group.joinBus.fromY<group.joinBus.toY);
    """)


def test_collaboration_keeps_all_behavior_visible_and_reserves_bus_columns():
    run_view(COLLABORATION + r"""
      const expanded=V.storyboard(view,900,scale,['beta']);
      const root=expanded.roots[0], group=expanded.collaborations[0];
      assert.equal(group.children.find(child=>child.id==='beta').height,224);
      assert.deepEqual(group.children.filter(child=>child.id!=='beta').map(child=>child.height),[224,224]);
      assert.equal(expanded.height,story.height,'Selecting a child cannot collapse any behavior');
      assert.deepEqual(root.events.map(event=>event.seq),view.lanes.find(lane=>lane.id==='root').events.map(event=>event.seq),
        'Root execution remains available around the collaboration');
      assert.ok(root.flowDown.length>0&&root.flowUp.length>0);
      for(const lane of [root,group.children.find(child=>child.id==='beta')]) {
        for(const region of [...lane.flowUp,...lane.flowDown]) {
          const left=region.labelX, right=left+region.labelWidth;
          for(const bus of [group.forkBus,group.joinBus].filter(Boolean))
            assert.ok(right<=bus.x-12 || left>=bus.x+12,
              `Flow label on ${lane.id} covers the collaboration bus`);
        }
      }
    """)


def test_incident_summary_deduplicates_tool_span_and_separates_rejections():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const e=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'root',...fields});
      const events=[e(1,'turn.started',0),
        e(2,'tool.started',100,{tool_call_id:'failed-call',tool_name:'command'}),
        e(3,'tool.completed',200,{tool_call_id:'failed-call',tool_name:'command',state:'error',span_id:7}),
        e(4,'span.completed',200,{tool_call_id:'failed-call',module:'tool',state:'error',span_id:7}),
        e(5,'tool.completed',250,{tool_call_id:'denied-call',tool_name:'command',state:'rejected'}),
        e(6,'phase.changed',300,{phase:'recovery',state:'error'}),
        e(7,'context.compacted',400)];
      const view=V.timeline({agents:[{id:'root'}],trace:{timeline:events,active:[],events:[]}});
      const story=V.storyboard(view,900);
      assert.equal(story.summary.agents,1);
      assert.equal(story.summary.tool_errors,1,'The tool and its span represent one failure');
      assert.equal(story.summary.rejections,1);
      assert.equal(story.summary.other_errors,1);
      assert.equal(story.summary.compactions,1);
      assert.deepEqual(story.incidents.map(item=>item.type),['tool_error','rejected','error']);
      assert.equal(story.incidents[0].event.seq,3);
      assert.ok(story.incidents[0].events.some(event=>event.seq===4),
        'The duplicate span stays inspectable within the incident');
      assert.equal(story.incidents[1].event.seq,5);
      assert.equal(story.incidents[2].event.seq,6);
    """)


def test_reused_tool_call_id_in_separate_turns_keeps_two_failures():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const e=(seq,kind,time,turn_id,fields={})=>({seq,kind,elapsed_ms:time,session_id:'root',turn_id,...fields});
      const events=[
        e(1,'turn.started',0,'first'),
        e(2,'tool.started',10,'first',{tool_call_id:'reused',tool_name:'command'}),
        e(3,'tool.completed',20,'first',{tool_call_id:'reused',tool_name:'command',state:'error'}),
        e(4,'turn.stopped',30,'first',{reason:'finished',state:'completed'}),
        e(5,'turn.started',100,'second'),
        e(6,'tool.started',110,'second',{tool_call_id:'reused',tool_name:'command'}),
        e(7,'tool.completed',120,'second',{tool_call_id:'reused',tool_name:'command',state:'error'})];
      const view=V.timeline({agents:[{id:'root'}],trace:{timeline:events,active:[],events:[]}});
      const story=V.storyboard(view,900);
      assert.equal(view.groups.length,2);
      assert.deepEqual(view.groups.map(group=>group.calls.map(call=>[call.start,call.end])),[[[10,20]],[[110,120]]]);
      assert.equal(story.summary.tool_errors,2,'Reused call IDs in different turns are separate failures');
      assert.deepEqual(story.incidents.map(item=>item.event.seq),[3,7]);
      assert.deepEqual(story.incidents.map(item=>item.events.map(event=>event.seq)),[[3],[7]],
        'Each raw failure remains separately inspectable');
    """)


def test_outer_join_bus_stays_clear_of_active_grandchild_work():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const e=(seq,kind,time,session_id,fields={})=>({seq,kind,elapsed_ms:time,session_id,...fields});
      const events=[
        e(1,'turn.started',0,'root'),
        e(2,'agent.created',100,'a',{parent_session_id:'root'}),
        e(3,'agent.started',110,'a'),
        e(4,'agent.created',120,'grand',{parent_session_id:'a'}),
        e(5,'agent.started',130,'grand'),
        e(6,'agent.created',200,'b',{parent_session_id:'root'}),
        e(7,'agent.started',210,'b'),
        e(8,'agent.completed',400,'a',{state:'completed'}),
        e(9,'agent.completed',500,'b',{state:'completed'})];
      const snapshot={agents:[
        {id:'root',status:'running',busy:true},
        {id:'a',parent_id:'root',status:'completed',busy:false},
        {id:'grand',parent_id:'a',status:'running',busy:true},
        {id:'b',parent_id:'root',status:'completed',busy:false}],
        trace:{timeline:events,active:[],events:[],now_elapsed_ms:1000}};
      const view=V.timeline(snapshot),scale=V.timeScale(view,900),story=V.storyboard(view,900,scale);
      const outer=story.collaborations.find(group=>group.parentId==='root');
      const inner=story.collaborations.find(group=>group.parentId==='a');
      assert.ok(outer&&inner&&outer.joinBus);
      const grand=story.lanes.find(lane=>lane.id==='grand');
      assert.equal(grand.closed,false);
      assert.equal(inner.joinBus,null,'The active grandchild has no invented join');
      assert.ok(outer.forkBus.x<scale.position(grand.start),
        'The outer fork bus remains before all descendant work');
      assert.ok(outer.joinBus.x>scale.position(grand.end),
        'The outer join bus cannot cut through active grandchild work');
      assert.ok(Math.abs(outer.branches.find(branch=>branch.childId==='a').endX-scale.position(400))<.001,
        'Moving the bus must not move the completed child endpoint');
      assert.ok(Math.abs(inner.branches[0].endX-scale.position(1000))<.001);
    """)


def test_default_render_hides_global_message_edges_but_keeps_message_inspector():
    script = r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div'){this.tag=tag;this.textContent='';this.className='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must be rendered as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='class')this.className=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,session_id,fields={})=>({seq,kind,elapsed_ms:time,session_id,...fields});
      const events=[e(1,'turn.started',0,'root'),e(2,'agent.created',100,'child',{parent_session_id:'root'}),
        e(3,'message.sent',150,'root',{related_session_id:'child',message_seq:7}),
        e(4,'message.read',220,'child',{related_session_id:'root',message_seq:7}),
        e(5,'message.sent',250,'child',{related_session_id:'root',message_seq:8})];
      const frame={runtime:{active_agents:2,active_model_calls:0,active_tools:[]},agents:[{id:'root',status:'running',busy:true},{id:'child',parent_id:'root',status:'running',busy:true}],agents_omitted:0,
        trace:{active:[],events:[],capacity:1000,dropped:0,generation:'one',cursor:5,timeline:events,now_elapsed_ms:400}};
      let requests=0;const setTimeout=()=>0,fetch=async()=>{requests++;return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=nodes['timeline-lanes'].children[0];
        const all=node=>[node,...node.children.flatMap(all)],rendered=all(scene);
        assert.equal(rendered.filter(node=>node.attributes.class==='message-edge').length,0,
          'The whole scene must not fill with crossing message curves');
        const entry=rendered.find(node=>node.className.split(' ').includes('message-summary'));
        assert.ok(entry,'A message inspector remains reachable');
        await entry.click();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.equal(nodes['selection-page'].textContent,'1–3 / 3',
          'Sent, read, and pending messages remain available');
        assert.equal(nodes['communication-routes'].children.length,2);
        assert.match(nodes['communication-routes'].children[0].children.at(-1).textContent,/已读/);
        assert.match(nodes['communication-routes'].children[1].children.at(-1).textContent,/待接收/);
        const rows=nodes['event-selection-list'].children;
        assert.equal(rows.length,3);
        assert.match(rows.at(-1).children[1].textContent,/发送 #8/,
          'The unmatched send remains a send rather than a fabricated read');
        await rows.at(-1).click();
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).kind,'message.sent');
        assert.equal(requests,1,'Inspecting already loaded communication stays local');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """
    run_view(script)
