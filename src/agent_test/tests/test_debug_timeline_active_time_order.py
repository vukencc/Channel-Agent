"""跨轮空闲与上下事件的共同顺序回归；仅执行视图计算。"""

import re

from test_debug_timeline_overview import STATIC, run_view


def test_idle_gap_survives_tail_metadata_and_starts_after_actual_cleanup():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const events=[],put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'root',...fields});
      put('turn.started',0,{turn_id:'first'});
      put('span.started',10,{span_id:1,module:'agent.run'});
      put('turn.stopped',1000,{turn_id:'first',reason:'finished'});
      put('span.started',1100,{span_id:2,module:'persistence.save'});
      put('context.sample',1200,{metrics:{context_utilization:.4}});
      put('span.completed',1500,{span_id:2,module:'persistence.save'});
      put('span.completed',2000,{span_id:1,module:'agent.run'});
      put('turn.started',3602000,{turn_id:'second'});
      put('phase.changed',3603000,{phase:'model_wait'});
      const view=V.timeline({agents:[{id:'root',busy:true}],trace:{timeline:events,now_elapsed_ms:3604000}});
      const scale=V.timeScale(view,900,true);
      assert.deepEqual(scale.gaps.map(gap=>[gap.start,gap.end,gap.duration]),[[2000,3602000,3600000]],'Trailing bookkeeping must not retain a whole idle hour');
      assert.ok(scale.position(3602000)-scale.position(2000)<=72);
      assert.ok(scale.position(2000)-scale.position(0)>200,'Actual work keeps visible width');
      for(const time of [0,1000,1200,2000,2000000,3602000,3604000])
        assert.ok(Math.abs(scale.timeAt(scale.position(time))-time)<.001,'The true clock remains reversible');
      assert.equal(scale.realDuration,3604000);
      assert.deepEqual(view.events,events,'Compression cannot rewrite timestamps or raw events');
    """)


def test_idle_gap_keeps_delegated_agent_lifecycle_and_late_tool_work_active():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const events=[],put=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
      put('turn.started',0,'root',{turn_id:'first'});
      put('agent.started',100,'child',{task_id:'task'});
      put('turn.started',110,'child',{turn_id:'child-turn'});
      put('turn.stopped',1000,'root',{turn_id:'first',reason:'finished'});
      put('turn.stopped',1500,'child',{turn_id:'child-turn',reason:'finished'});
      put('agent.completed',2000,'child',{task_id:'task'});
      put('tool.started',3000,'child',{tool_call_id:'cleanup',tool_name:'read_file'});
      put('tool.completed',5000,'child',{tool_call_id:'cleanup',tool_name:'read_file',state:'completed'});
      put('turn.started',3605000,'root',{turn_id:'second'});
      const view=V.timeline({agents:[{id:'root',busy:true},{id:'child',parent_id:'root',busy:false}],trace:{timeline:events,now_elapsed_ms:3606000}});
      const gaps=V.timeScale(view,900,true).gaps;
      assert.deepEqual(gaps.map(gap=>[gap.start,gap.end]),[[5000,3605000]],'Only the final globally idle suffix is folded after cleanup');
      const unfinished={...view,scaleEvents:events.filter(event=>event.kind!=='agent.completed')};
      assert.equal(V.timeScale(unfinished,900,true).gaps.length,0,'A stopped child turn does not prove its delegated task finished');
    """)


def test_original_side_summaries_keep_all_events_and_anchor_nearby_real_time():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      for(const dense of [false,true]) {
        const events=[],put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'root',...fields});
        put('turn.started',0);
        for(let index=0;index<(dense?300:2);index++) {
          put('phase.changed',10+index*10,{phase:'context_prepare'});
          put('phase.changed',12+index*10,{phase:'model_wait'});
          put('model.final',14+index*10);
        }
        put('turn.stopped',dense?3020:40,{reason:'finished'});
        const view=V.timeline({agents:[{id:'root'}],trace:{timeline:events}}),scale=V.timeScale(view,900),lane=V.overview(view,900,scale).lanes[0];
        const expected=events.filter(event=>V.flowSide(event));
        const story=V.storyboard(view,900,scale).lanes[0];
        assert.equal(story.height,224,'The original fixed-height lane remains');
        for(const side of ['up','down']) {
          const values=story[side==='up'?'flowUp':'flowDown'];
          assert.deepEqual(values.flatMap(region=>region.events).sort((a,b)=>a.seq-b.seq),expected.filter(event=>V.flowSide(event)===side),'Every original event remains available in its side inspector');
          assert.ok(values.length<=5,'Original side summaries stay bounded at overview width');
          values.forEach((region,index)=>{
            assert.ok(region.labelX>=140&&region.labelX+region.labelWidth<=870);
            if(index)assert.ok(values[index-1].labelX+values[index-1].labelWidth<=region.labelX,'Same-side labels cannot collide');
            assert.ok(region.events.includes(region.anchorEvent),'The marker must belong to its original side summary');
            assert.equal(region.anchorX,scale.position(V.eventTime(region.anchorEvent)),'Arrow stems retain a real event timestamp');
            const center=region.labelX+region.labelWidth/2;
            assert.ok(Math.abs(region.anchorX-center)<=Math.min(...region.events.map(event=>Math.abs(scale.position(V.eventTime(event))-center)))+.001,'Keep the original caption association without horizontal callout lines');
            assert.ok(region.events.every(event=>V.flowSide(event)===side));
          });
        }
      }
    """)


def test_equal_timestamp_side_summaries_keep_recorded_seq_without_fictional_time():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const events=[{seq:7,kind:'phase.changed',elapsed_ms:10,session_id:'root',phase:'context_prepare'},
        {seq:8,kind:'phase.changed',elapsed_ms:10,session_id:'root',phase:'model_wait'},
        {seq:9,kind:'model.final',elapsed_ms:10,session_id:'root'}];
      const view=V.timeline({agents:[{id:'root'}],trace:{timeline:events}}),lane=V.storyboard(view,900).lanes[0];
      assert.deepEqual([...lane.flowUp,...lane.flowDown].flatMap(region=>region.events.map(event=>event.seq)).sort((a,b)=>a-b),[7,8,9]);
      assert.ok([...lane.flowUp,...lane.flowDown].every(region=>region.anchorX===140),'Tied clocks cannot be spread into fictional time');
      assert.deepEqual(view.events.map(event=>event.seq),[7,8,9],'Recorded sequence remains readable in the source events');
    """)


def test_render_keeps_short_vertical_arrows_two_line_labels_and_actual_event_times():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div'){this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Metadata must render as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'root',...fields});
      const frame={runtime:{active_agents:0,active_model_calls:0,active_tools:[]},agents:[{id:'root',busy:false,status:'completed'}],agents_omitted:0,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor:5,timeline:[
        e(1,'turn.started',0),e(2,'phase.changed',10,{phase:'context_prepare'}),e(3,'phase.changed',20,{phase:'model_wait'}),
        e(4,'model.final',30),e(5,'turn.stopped',40,{state:'completed',reason:'finished'})]}};
      const requests=[],setTimeout=()=>{};const fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=nodes['timeline-lanes'].children[0],descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
        const rendered=descendants(scene),find=css=>rendered.filter(node=>(node.className||node.attributes.class||'').split(' ').includes(css));
        assert.equal(find('flow-order').length,0,'The original overview has no numbered order summary');
        const paths=[...find('behavior-arrow-down'),...find('behavior-arrow-up')];
        const regions=[...AgentDebugView.storyboard(AgentDebugView.timeline(frame),900).lanes[0].flowDown,...AgentDebugView.storyboard(AgentDebugView.timeline(frame),900).lanes[0].flowUp];
        assert.equal(paths.length,regions.length);
        assert.ok(paths.every(node=>/^M[\d.]+ [\d.]+V[\d.]+$/.test(node.attributes.d)),'Arrows remain short vertical stems without a horizontal segment');
        const starts=paths.map(node=>Number(node.attributes.d.match(/^M([\d.]+)/)[1])).sort((a,b)=>a-b);
        assert.deepEqual(starts,regions.map(region=>region.anchorX).sort((a,b)=>a-b),'Rendered stems use an actual event timestamp on each side');
        const labels=find('flow-branch');
        assert.equal(labels.length,regions.length);
        assert.ok(labels.every(node=>node.children.length===2&&node.children[0].className==='flow-title'&&node.children[1].className==='flow-detail'),'Original two-line labels stay intact');
        const upper=labels.find(node=>node.className.includes('flow-branch-up'));
        await upper.click();
        assert.match(nodes['selection-summary'].textContent,/状态分流/);
        assert.deepEqual(nodes['event-selection-list'].children.map(node=>node.children[0].textContent),['0.01 s','0.03 s','0.04 s']);
        assert.equal(requests.length,1,'Reading the ordered metadata list requires no detail fetch');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_original_flow_label_height_and_line_count():
    css = (STATIC / "debug.css").read_text()
    branch = re.search(r"\.timeline-event\.flow-branch, \.tool-batch\.flow-branch\s*\{([^}]+)\}", css)
    assert branch is not None
    assert re.search(r"\bheight:\s*42px\s*;", branch.group(1))
    assert ".flow-branch .flow-order" not in css
