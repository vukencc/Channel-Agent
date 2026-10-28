"""子 Agent 行为默认完整显示，筛选不丢失所选子树的事件。"""

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
  const assert=require('node:assert/strict'), V=AgentDebugView, events=[];
  const add=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
  add('turn.started',0,'root');
  add('agent.created',100,'finished',{parent_session_id:'root'});
  add('agent.started',110,'finished');
  add('phase.changed',120,'finished',{phase:'planning'});
  add('tool.started',130,'finished',{tool_call_id:'read',tool_name:'read_file'});
  add('tool.completed',160,'finished',{tool_call_id:'read',tool_name:'read_file',state:'completed'});
  add('model.final',180,'finished');
  add('agent.completed',200,'finished',{state:'completed'});
  add('agent.created',220,'active',{parent_session_id:'root'});
  add('agent.started',230,'active');
  add('phase.changed',240,'active',{phase:'tools'});
  add('tool.started',250,'active',{tool_call_id:'failed',tool_name:'run_command'});
  add('tool.completed',280,'active',{tool_call_id:'failed',tool_name:'run_command',state:'error'});
  add('phase.changed',290,'active',{phase:'recovery',state:'error'});
  add('span.started',300,'active',{module:'model',span_id:9});
  add('agent.created',320,'grandchild',{parent_session_id:'active'});
  add('agent.started',330,'grandchild');
  add('phase.changed',340,'grandchild',{phase:'model_wait'});
  add('tool.started',350,'grandchild',{tool_call_id:'search',tool_name:'web_search'});
  add('message.sent',360,'active',{related_session_id:'grandchild',message_seq:1});
  add('message.read',370,'grandchild',{related_session_id:'active',message_seq:1});
  const agents=[{id:'root',status:'running',busy:true},
    {id:'finished',parent_id:'root',status:'completed',busy:false},
    {id:'active',parent_id:'root',status:'running',busy:true},
    {id:'grandchild',parent_id:'active',status:'running',busy:true}];
  const snapshot={agents,trace:{timeline:events,events:[],active:[],now_elapsed_ms:3600000}};
"""


def test_every_child_shows_full_behavior_without_expansion_even_after_long_wait():
    run_view(COLLABORATION + r"""
      const view=V.timeline(snapshot), scale=V.timeScale(view,1200), story=V.storyboard(view,1200,scale);
      assert.equal(scale.compressed,false,'A running child remains on the real time axis');
      assert.equal(story.lanes.length,4);
      for(const lane of story.lanes) {
        assert.equal(lane.expanded,true,`${lane.id} must show behavior immediately`);
        assert.equal(lane.height,224);
        assert.equal(lane.lineY,lane.top+108);
        assert.deepEqual(lane.events.map(event=>event.seq),events.filter(event=>event.session_id===lane.id).map(event=>event.seq));
        const source=lane.events.filter(event=>V.flowSide(event));
        assert.deepEqual([...lane.flowUp,...lane.flowDown].flatMap(region=>region.events).map(event=>event.seq).sort((a,b)=>a-b),source.map(event=>event.seq));
      }
      const child=story.lanes.find(lane=>lane.id==='finished');
      assert.ok(child.flowDown.some(region=>region.toolNames.includes('read_file')));
      assert.ok(child.flowUp.length>0);
      assert.equal(story.lanes.find(lane=>lane.id==='active').incidents.length,2);
      assert.equal(view.spans.find(span=>span.session_id==='active').active,true);
      assert.equal(view.groups.find(group=>group.session_id==='grandchild').calls[0].active,true);
      assert.equal(story.communications.length,1);
      assert.ok(story.collaborations.find(group=>group.parentId==='root').joinBus);
      assert.equal(story.collaborations.find(group=>group.parentId==='active').joinBus,null);
      const repeated=V.storyboard(view,1200,scale,[]);
      assert.deepEqual(repeated.lanes.map(lane=>[lane.id,lane.expanded,lane.height]),story.lanes.map(lane=>[lane.id,lane.expanded,lane.height]));
    """)


def test_root_filter_keeps_child_events_tools_and_exceptions_and_excludes_other_root():
    run_view(COLLABORATION + r"""
      const ownedEvents=events.slice();
      agents.push({id:'other',status:'completed',busy:false},{id:'other-child',parent_id:'other',status:'completed',busy:false});
      add('turn.started',8000000,'other');
      add('agent.created',8000001,'other-child',{parent_session_id:'other'});
      add('tool.completed',8000002,'other-child',{tool_call_id:'foreign',tool_name:'delete_file',state:'error'});
      const view=V.timeline(snapshot,'root'), story=V.storyboard(view,1200);
      assert.deepEqual(view.events.map(event=>event.seq),ownedEvents.map(event=>event.seq));
      assert.deepEqual(story.lanes.map(lane=>lane.id),['root','finished','active','grandchild']);
      assert.deepEqual(view.groups.flatMap(group=>group.calls).map(call=>call.tool_name),['read_file','run_command','web_search']);
      assert.deepEqual(view.scaleEvents.map(event=>event.seq),ownedEvents.map(event=>event.seq));
      assert.equal(view.domain[1],3600000,'An unrelated root must not stretch this tree');
      assert.equal(story.summary.tool_errors,1);
      assert.ok(story.lanes.find(lane=>lane.id==='active').flowDown.length>0);
      assert.ok(story.lanes.find(lane=>lane.id==='grandchild').flowDown.length>0);
    """)


def test_child_filter_keeps_descendant_behavior_and_only_ancestor_structure():
    run_view(COLLABORATION + r"""
      const view=V.timeline(snapshot,'active'), story=V.storyboard(view,1200);
      assert.deepEqual(view.events.map(event=>event.seq),events.filter(event=>['active','grandchild'].includes(event.session_id)).map(event=>event.seq));
      assert.deepEqual(view.groups.map(group=>group.session_id),['active','grandchild']);
      assert.deepEqual(view.lanes.map(lane=>lane.id),['root','active','grandchild']);
      assert.deepEqual(story.lanes.find(lane=>lane.id==='root').events,[],'The ancestor is a structural endpoint, not selected behavior');
      assert.equal(story.lanes.find(lane=>lane.id==='root').flowDown.length,0);
      assert.ok(story.lanes.find(lane=>lane.id==='grandchild').flowDown.length>0);
      assert.equal(view.communications.length,1);
      assert.ok(story.lanes.every(lane=>lane.height===224&&lane.expanded));
    """)


def test_module_and_legacy_span_filters_match_the_timeline_subtree():
    run_view(COLLABORATION + r"""
      agents.push({id:'other',status:'completed',busy:false});
      snapshot.trace.active=[
        {id:101,session_id:'root',module:'model',duration_ms:5},
        {id:102,session_id:'active',module:'tool',duration_ms:8},
        {id:103,session_id:'grandchild',module:'tool',duration_ms:12},
        {id:104,session_id:'other',module:'tool',duration_ms:50}];
      snapshot.trace.events=[
        {id:201,session_id:'finished',module:'tool',state:'completed',duration_ms:10},
        {id:202,session_id:'active',module:'tool',state:'error',duration_ms:20},
        {id:203,session_id:'grandchild',module:'model',state:'completed',duration_ms:30},
        {id:204,session_id:'other',module:'tool',state:'error',duration_ms:500}];
      assert.deepEqual(V.rows(snapshot,'root').active.map(row=>row.id),[101,102,103]);
      assert.deepEqual(V.rows(snapshot,'root').events.map(row=>row.id),[203,202,201]);
      assert.deepEqual(V.rows(snapshot,'active').active.map(row=>row.id),[102,103]);
      assert.deepEqual(V.rows(snapshot,'active').events.map(row=>row.id),[203,202]);
      assert.deepEqual(V.rows(snapshot,'active',true).events.map(row=>row.id),[202]);
      const rootTools=V.modules(snapshot,'root').find(item=>item.key==='tool');
      assert.deepEqual([rootTools.active,rootTools.completed,rootTools.errors,rootTools.max_ms],[2,2,1,20]);
      const childTools=V.modules(snapshot,'active').find(item=>item.key==='tool');
      assert.deepEqual([childTools.active,childTools.completed,childTools.errors,childTools.max_ms],[2,1,1,20]);
    """)


def test_eight_dense_child_lanes_keep_every_tool_and_flow_event_in_bounded_slots():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView,events=[],agents=[{id:'root',busy:true,status:'running'}];
      const add=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
      add('turn.started',0,'root');
      for(let index=0;index<8;index++) {
        const id=`child-${index}`,completed=index%2===0;
        agents.push({id,parent_id:'root',busy:!completed,status:completed?'completed':'running'});
        add('agent.created',100+index,id,{parent_session_id:'root'});
        add('agent.started',110+index,id);
        for(let call=0;call<80;call++) {
          const tool_call_id=`${id}-${call}`,time=200+call*3;
          add('phase.changed',time,id,{phase:'tools'});
          add('tool.started',time+1,id,{tool_call_id,tool_name:'read_file'});
          add('tool.completed',time+2,id,{tool_call_id,tool_name:'read_file',state:call===30?'error':'completed'});
        }
        if(completed) add('agent.completed',500+index,id,{state:'completed'});
      }
      const snapshot={agents,trace:{timeline:events,active:[],events:[],now_elapsed_ms:3600000}};
      for(const width of [820,1200]) {
        const view=V.timeline(snapshot,'root'),scale=V.timeScale(view,width),story=V.storyboard(view,width,scale);
        assert.equal(story.lanes.length,9);
        assert.ok(story.lanes.every(lane=>lane.height===224&&lane.expanded));
        assert.equal(story.summary.tool_errors,8);
        assert.equal(story.collaborations[0].branches.filter(branch=>branch.closed).length,4);
        for(const lane of story.lanes.filter(lane=>lane.parentId)) {
          const regions=[...lane.flowUp,...lane.flowDown];
          assert.ok(regions.length<=2*Math.max(1,Math.floor((width-170)/170)),'Summary slots stay bounded by display width');
          assert.deepEqual(regions.flatMap(region=>region.events).map(event=>event.seq).sort((a,b)=>a-b),lane.events.filter(event=>V.flowSide(event)).map(event=>event.seq));
          assert.equal(new Set(lane.flowDown.flatMap(region=>region.groups).flatMap(group=>group.calls).map(call=>call.id)).size,80,'Every child tool call remains inspectable');
          for(const region of regions) {
            assert.ok(region.labelWidth>0);
            assert.ok(region.labelX>=140&&region.labelX+region.labelWidth<=width-30);
            assert.equal(region.anchorX,scale.position(V.eventTime(region.anchorEvent)),'Short arrows retain real event times');
            for(const bus of [story.collaborations[0].forkBus,story.collaborations[0].joinBus].filter(Boolean))
              assert.ok(region.labelX+region.labelWidth<=bus.x-12||region.labelX>=bus.x+12,'Labels avoid the shared buses');
          }
        }
      }
    """)
