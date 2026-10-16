"""Readable branch summaries and conservative idle compression regressions."""

import ast
import json

from test_debug_timeline_overview import STATIC, run_view


def test_upper_branch_labels_distinguish_problem_events_from_merged_activity():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      function summary(phases,extra=[]) {
        const events=Array.from({length:phases},(_,index)=>({seq:index+1,kind:'phase.changed',elapsed_ms:index*.01,session_id:'root',phase:index%2?'model_wait':'tools'}));
        extra.forEach((fields,index)=>events.push({seq:events.length+1,elapsed_ms:5+index*.01,session_id:'root',...fields}));
        const lane=V.overview(V.timeline({agents:[{id:'root'}],trace:{timeline:events}}),900).lanes[0];
        assert.equal(lane.height,224);
        assert.equal(lane.lineY,lane.top+108);
        assert.equal(lane.branches.length,1);
        assert.deepEqual(lane.branches[0].events.map(e=>e.seq),events.map(e=>e.seq),'Summaries must retain every member for inspection');
        return lane.branches[0];
      }
      const failed=summary(96,[{kind:'tool.completed',tool_call_id:'bad',tool_name:'run_command',state:'error'}]);
      assert.equal(failed.errors,1);
      assert.match(failed.label,/异常事件 1/,'A cell with 97 events contains one error event');
      assert.match(failed.label,/含 97 个事件/);
      assert.doesNotMatch(failed.label,/执行异常.*97\s*项|异常事件 97/);
      const rejected=summary(80,[{kind:'tool.completed',tool_call_id:'denied',tool_name:'run_command',state:'rejected'}]);
      assert.match(rejected.label,/拒绝事件 1/);
      assert.match(rejected.label,/含 81 个事件/);
      const phases=summary(97);
      assert.match(phases.label,/阶段切换 97 次/,'Normal activity is counted as phase transitions');
      assert.doesNotMatch(phases.label,/97\s*项/);
      const mixed=summary(60,[{kind:'model.final'},{kind:'model.final'},{kind:'plan.updated',stage_index:1,state:'completed'}]);
      assert.match(mixed.label,/含 63 个事件/,'A merged event count is not a count of outcomes');
      assert.doesNotMatch(mixed.label,/完成.*63\s*项|回复.*63\s*次/);
      const terminal=summary(60,[{kind:'tool.completed',tool_call_id:'bad',state:'error'},{kind:'turn.stopped',reason:'finished',state:'completed'}]);
      assert.match(terminal.label,/异常事件 1/,'A completed turn must still disclose its actual error event count');
      assert.match(terminal.label,/含 62 个事件/);
    """)


def test_arrow_branches_have_readable_labels_and_bounded_nonoverlapping_cells():
    run_view(r"""
      const assert=require('node:assert/strict'), V=AgentDebugView, events=[];
      const put=(kind,time,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id:'root',...fields});
      put('turn.started',0);
      for(let i=0;i<300;i++) {
        put('phase.changed',i*10+1,{phase:i%2?'model_wait':'recovery'});
        put('tool.started',i*10+2,{tool_call_id:`c-${i}`,tool_name:i%2?'command':'rag_search'});
        put('tool.completed',i*10+3,{tool_call_id:`c-${i}`,tool_name:i%2?'command':'rag_search',state:i===12?'error':'completed'});
      }
      put('plan.updated',3005,{stage_index:2,state:'completed',title:'PRIVATE_TASK'});
      put('turn.stopped',3010,{reason:'checkpoint',state:'checkpoint'});
      const view=V.timeline({agents:[{id:'root',status:'idle'}],trace:{timeline:events}}), lane=V.overview(view,900).lanes[0];
      const sparse=V.overview(V.timeline({agents:[{id:'root'}],trace:{timeline:events.slice(0,4)}}),900).lanes[0];
      assert.equal(lane.height,sparse.height,'Event count must not change lane height');
      assert.ok(lane.branches.length>0&&lane.tools.length>0);
      for(const regions of [lane.branches,lane.tools]) {
        assert.ok(regions.length<15,'Labels must share bounded horizontal cells');
        regions.forEach((region,index)=>{
          assert.ok(region.label&&region.label.length>1);
          assert.ok(region.labelWidth>=88,'An arrow needs room for a meaningful label');
          assert.ok(region.labelX>=140&&region.labelX+region.labelWidth<=890);
          if(index)assert.ok(regions[index-1].labelX+regions[index-1].labelWidth<=region.labelX,'Branch labels collide');
          assert.ok(region.events.length&&region.event);
        });
      }
      assert.equal(lane.tools.flatMap(r=>r.groups.flatMap(g=>g.calls)).length,300);
      assert.ok(lane.tools.some(r=>/命令|检索/.test(r.label)));
      assert.ok(lane.branches.some(r=>/暂停/.test(r.label)),'Stop reason remains readable in a dense summary');
      assert.ok(lane.branches.some(r=>r.errors),'Failure remains prominent');
      assert.doesNotMatch(JSON.stringify(lane.branches.map(r=>r.label)),/PRIVATE_TASK/);
      const expected=view.upper.map(e=>e.seq).sort((a,b)=>a-b);
      const actual=lane.branches.flatMap(r=>r.events).filter(e=>view.upper.some(u=>u.seq===e.seq)).map(e=>e.seq).sort((a,b)=>a-b);
      assert.deepEqual(actual,expected,'All upper branch events remain inspectable');
    """)


def test_idle_compression_preserves_elapsed_time_and_does_not_compress_active_waits():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const e=(seq,kind,time,fields={})=>({seq,kind,elapsed_ms:time,session_id:'root',...fields});
      const first=e(1,'turn.started',0), stopped=e(2,'turn.stopped',1000,{reason:'finished'}), resumed=e(3,'turn.started',3601000);
      const view=V.timeline({agents:[{id:'root',busy:true}],trace:{timeline:[first,stopped,resumed,e(4,'phase.changed',3602000,{phase:'model_wait'})],now_elapsed_ms:3660000}});
      const plain=V.timeScale(view,900,false), compact=V.timeScale(view,900,true);
      assert.equal(plain.gaps.length,0);
      assert.equal(compact.gaps.length,1);
      assert.equal(compact.gaps[0].duration,3600000);
      assert.ok(compact.position(3601000)-compact.position(1000)<100);
      for(const time of [0,1000,100000,3601000,3660000])assert.ok(Math.abs(compact.timeAt(compact.position(time))-time)<.001,'Elapsed-time mapping must be reversible');
      assert.deepEqual(view.events.map(V.eventTime),[0,1000,3601000,3602000]);
      const waiting=V.timeline({agents:[{id:'root',busy:true}],trace:{timeline:[first,e(2,'phase.changed',10,{phase:'model_wait'}),e(3,'context.sample',3600000)],now_elapsed_ms:3660000}});
      assert.equal(V.timeScale(waiting,900,true).gaps.length,0,'Long model silence is active time');
      const parallel=V.timeline({agents:[{id:'root'},{id:'child',parent_id:'root',busy:true}],trace:{timeline:[first,e(2,'turn.started',1,{session_id:'child'}),stopped,resumed],now_elapsed_ms:3660000}});
      assert.equal(V.timeScale(parallel,900,true).gaps.length,0,'Another active Agent prevents idle compression');
      const unknown=V.timeline({agents:[{id:'root'},{id:'child',parent_id:'root',busy:true}],trace:{timeline:[first,stopped,resumed],now_elapsed_ms:3660000}});
      assert.equal(V.timeScale(unknown,900,true).gaps.length,0,'A busy Agent with a missing start cannot be assumed idle');
    """)


def test_spine_state_distinguishes_activity_pause_failure_and_completion():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const frame=(reason,state,busy=false)=>({agents:[{id:'a',status:busy?'running':'idle',busy}],trace:{now_elapsed_ms:1000,timeline:[
        {seq:1,kind:'turn.started',elapsed_ms:0,session_id:'a'},
        ...(reason?[{seq:2,kind:'turn.stopped',elapsed_ms:100,session_id:'a',reason,state}]:[])
      ]}});
      const lane=(reason,state,busy)=>V.overview(V.timeline(frame(reason,state,busy)),900).lanes[0];
      assert.equal(lane('',null,true).spineState,'running');
      assert.equal(lane('checkpoint','checkpoint').spineState,'paused');
      assert.equal(lane('error','error').spineState,'error');
      assert.equal(lane('model_connection','error').spineState,'error');
      assert.match(lane('model_connection','error').stateLabel,/连接中断/);
      assert.equal(lane('finished','completed').spineState,'completed');
      assert.match(lane('checkpoint','checkpoint').stateLabel,/暂停/);
      assert.match(lane('error','error').stateLabel,/失败/);
    """)


def test_nearly_all_idle_submillisecond_work_preserves_continuity_and_plot_bounds():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const e=(seq,kind,time)=>({seq,kind,elapsed_ms:time,session_id:'root',reason:kind==='turn.stopped'?'finished':undefined});
      for(const stop of [0,.2,2]) {
        const view=V.timeline({agents:[{id:'root',busy:true}],trace:{timeline:[e(1,'turn.started',0),e(2,'turn.stopped',stop),e(3,'turn.started',3600000)],now_elapsed_ms:3600000}});
        const scale=V.timeScale(view,900,true);
        assert.ok(Math.abs(scale.position(view.domain[0])-140)<.001);
        assert.ok(Math.abs(scale.position(view.domain[1])-870)<.001,'Latest activity must reach the right plot bound');
        for(const time of [0,stop,1,10000,3599999,3600000])assert.ok(Math.abs(scale.timeAt(scale.position(time))-time)<.001,'Sub-ms fragments must remain reversible');
        if(scale.gaps.length)assert.ok(Math.abs(scale.position(stop)-scale.gaps[0].left)<.001,'No discontinuity at a folded gap');
      }
    """)


def test_motion_is_scoped_to_running_spines_and_stops_for_pause_and_reduced_motion():
    css = (STATIC / "debug.css").read_text()
    source = (STATIC / "debug.js").read_text()
    assert "agent-flow" in source and "agent-cursor" in source
    assert '"data-motion"' in source
    assert "paused || document.hidden" in source
    assert '[data-motion="paused"]' in css
    assert "prefers-reduced-motion: reduce" in css
    assert "animation: none" in css


def test_first_agents_use_distinct_color_families_and_keep_colors_when_reordered():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const ids=['root','first-child','second-child','third-child','fourth-child','fifth-child'],colors=ids.map(V.agentColor);
      assert.equal(new Set(colors).size,ids.length);
      const hues=colors.map(color=>Number(color.match(/hsl\((\d+(?:\.\d+)?)/)[1]));
      for(let a=0;a<hues.length;a++)for(let b=a+1;b<hues.length;b++) {
        const delta=Math.abs(hues[a]-hues[b]);
        assert.ok(Math.min(delta,360-delta)>=35,'The first few Agents need visibly different color families');
      }
      assert.deepEqual(ids.slice().reverse().map(V.agentColor),colors.slice().reverse(),'A different render order must not change assigned colors');
    """)


def test_all_registered_public_tools_have_readable_action_labels():
    package = STATIC.parent.parent
    names = set()

    def registration(call):
        return isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "register_tool"

    def name_for(call, default, bindings=None):
        for keyword in call.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value
            if keyword.arg == "name" and isinstance(keyword.value, ast.Name) and bindings:
                return bindings.get(keyword.value.id, default)
        return default

    for path in sorted((package / "tools").glob("*.py")):
        tree = ast.parse(path.read_text())
        functions = {item.name for item in ast.walk(tree) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for item in ast.walk(tree):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in item.decorator_list:
                    if registration(decorator):
                        names.add(name_for(decorator, item.name))
            elif isinstance(item, ast.Call) and registration(item.func) and item.args:
                callback = item.args[0]
                default = callback.id if isinstance(callback, ast.Name) and callback.id in functions else None
                name = name_for(item.func, default)
                if isinstance(name, str):
                    names.add(name)
            elif isinstance(item, ast.For) and isinstance(item.target, ast.Tuple) and isinstance(item.iter, (ast.Tuple, ast.List)):
                for row in item.iter.elts:
                    if not isinstance(row, (ast.Tuple, ast.List)):
                        continue
                    bindings = {target.id: value.id if isinstance(value, ast.Name) else value.value for target, value in zip(item.target.elts, row.elts) if isinstance(target, ast.Name) and isinstance(value, (ast.Name, ast.Constant))}
                    for call in ast.walk(item):
                        if isinstance(call, ast.Call) and registration(call.func) and call.args:
                            callback = call.args[0]
                            default = bindings.get(callback.id) if isinstance(callback, ast.Name) else None
                            name = name_for(call.func, default, bindings)
                            if isinstance(name, str):
                                names.add(name)
    assert {"create_file", "create_session", "plan_bind", "inspect_environment", "web_search"} <= names
    assert len(names) >= 38
    run_view("const publicNames=" + json.dumps(sorted(names)) + r""";
      const assert=require('node:assert/strict'),V=AgentDebugView;
      for(const name of publicNames)assert.match(V.toolLabel(name),/[\u3400-\u9fff]/,`${name} needs a readable action label`);
      const events=[];
      for(const [index,name] of publicNames.entries())events.push({seq:index+1,kind:'tool.completed',elapsed_ms:index,session_id:'a',tool_call_id:name,tool_name:name,state:'completed'});
      const regions=V.overview(V.timeline({agents:[{id:'a'}],trace:{timeline:events}}),900).lanes[0].tools;
      assert.deepEqual(regions.flatMap(r=>r.toolNames).sort(),publicNames,'Original tool names remain available for hover and inspection');
    """)


def test_every_filtered_event_is_available_without_a_payload_request():
    source = (STATIC / "debug.js").read_text()
    html = (STATIC / "debug.html").read_text()
    assert 'id="timeline-all-events"' in html
    assert 'id="compress-idle"' in html
    assert '$("timeline-all-events").addEventListener("click"' in source
    assert 'type: "all-events"' in source


def test_rendered_arrows_motion_and_all_event_inspector_follow_filters_without_fetching():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Metadata must render as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const e=(seq,kind,time,session_id,fields={})=>({seq,kind,elapsed_ms:time,session_id,...fields});
      const frame={runtime:{active_agents:1,active_model_calls:1,active_tools:[]},agents:[{id:'root',busy:true,status:'running'},{id:'child',parent_id:'root',busy:false,status:'idle'}],trace:{capacity:10000,dropped:0,generation:'one',cursor:6,active:[],events:[],timeline:[
        e(1,'turn.started',0,'root'),e(2,'phase.changed',10,'root',{phase:'model_wait'}),
        e(3,'model.completed',20,'root'),e(4,'agent.created',30,'child',{parent_session_id:'root'}),
        e(5,'tool.completed',40,'child',{tool_call_id:'one',tool_name:'command',state:'error',command:'PRIVATE_COMMAND'}),
        e(6,'turn.stopped',50,'child',{reason:'error',state:'error'})
      ],now_elapsed_ms:100}};
      const requests=[],setTimeout=()=>{};const fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0],descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
        const find=css=>descendants(scene()).filter(node=>(node.className||node.attributes.class||'').split(' ').includes(css));
        assert.equal(find('agent-flow').length,1,'Only the busy Agent animates');
        assert.equal(find('agent-cursor').length,1);
        assert.equal(find('flow-branch-up').length,0,'Compact child behavior starts folded');
        const child=find('lane-label').find(button=>button.className.includes('child'));
        await child.click();
        assert.equal(find('lane-label').find(button=>button.className.includes('child')).attributes['aria-expanded'],'true');
        assert.ok(find('behavior-arrow-up').length>0&&find('behavior-arrow-down').length>0);
        assert.ok(find('flow-branch-up').length>0&&find('flow-branch-down').length>0);
        assert.ok(find('flow-branch-up').every(button=>button.children.some(child=>child.className==='flow-title')));
        assert.ok(find('flow-branch-down').every(button=>button.children.some(child=>child.className==='flow-detail')));
        assert.equal(find('behavior-label').length,0);
        assert.equal(find('phase-region').length,0);
        assert.ok(find('tool-batch').every(button=>button.title.includes('command')),'Hover retains original tool names');
        await nodes.pause.click();
        assert.equal(nodes['timeline-content'].attributes['data-motion'],'paused');
        await nodes['timeline-all-events'].click();
        assert.equal(nodes['selection-page'].textContent,'1–6 / 6','Unplotted model completion remains accessible');
        assert.equal(nodes['event-selection-list'].children.length,6);
        assert.equal(requests.length,1,'Opening the full metadata inspector is local');
        nodes['agent-filter'].value='child';
        await Promise.all(nodes['agent-filter'].handlers.change.map(handler=>handler()));
        assert.equal(nodes['selection-page'].textContent,'1–3 / 3');
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).session_id,'child');
        nodes['errors-only'].checked=true;
        await Promise.all(nodes['errors-only'].handlers.change.map(handler=>handler()));
        assert.equal(nodes['selection-page'].textContent,'1–2 / 2');
        assert.equal(JSON.parse(nodes['event-detail-meta'].textContent).state,'error');
        assert.doesNotMatch(nodes['event-detail-meta'].textContent,/PRIVATE_COMMAND/);
        nodes['agent-filter'].value='root';
        await Promise.all(nodes['agent-filter'].handlers.change.map(handler=>handler()));
        assert.equal(nodes['event-selection'].hidden,true,'Empty error filters clear the previously selected Agent');
        assert.equal(nodes['event-detail-load'].disabled,true);
        assert.equal(requests.length,1);
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)
