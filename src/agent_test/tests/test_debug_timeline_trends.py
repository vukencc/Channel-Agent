"""Compact Agent trends retain precise, inspectable event membership."""

from test_debug_timeline_overview import STATIC, run_view


def test_compact_lanes_preserve_hierarchy_endpoints_and_comparable_density():
    run_view(r"""
      const assert=require('node:assert/strict'),V=AgentDebugView;
      const events=[],put=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
      put('turn.started',0,'root');
      put('phase.changed',1,'root',{phase:'model_wait'});
      put('agent.created',100,'child',{parent_session_id:'root'});
      put('phase.changed',110,'child',{phase:'tools'});
      for(let i=0;i<40;i++)put('plan.updated',120+i,'root');
      put('turn.stopped',200,'child',{reason:'finished'});
      put('agent.created',250,'grandchild',{parent_session_id:'child'});
      put('plan.updated',260,'grandchild');
      put('turn.stopped',300,'grandchild',{reason:'finished'});
      put('turn.stopped',1000,'root',{reason:'finished'});
      const view=V.timeline({agents:[{id:'root',status:'completed'},{id:'child',parent_id:'root',status:'completed'},{id:'grandchild',parent_id:'child',status:'completed'}],trace:{timeline:events,active:[],events:[],now_elapsed_ms:9000}});
      const overview=V.overview(view,900),[root,child,grandchild]=overview.lanes;
      assert.equal(overview.height,3*224,'Three Agents use fixed-height lanes');
      assert.deepEqual(overview.lanes.map(lane=>lane.depth),[0,1,2]);
      assert.equal(child.end,200,'Completed Agent line ends at its last event, not the current clock');
      assert.equal(root.end,1000);
      assert.equal(grandchild.end,300);
      assert.equal(root.activity.reduce((sum,bin)=>sum+bin.count,0),view.events.filter(event=>event.session_id==='root').length);
      assert.ok(root.activity.length<=100,'Density bins remain bounded by display width');
      assert.equal(overview.activityMax,Math.max(...overview.lanes.flatMap(lane=>lane.activity.map(bin=>bin.count))),'All Agents share one density scale');
      assert.ok(root.activity.some(bin=>bin.count>1));
      const filtered=V.timeline({agents:[{id:'root'},{id:'child',parent_id:'root'},{id:'grandchild',parent_id:'child'}],trace:{timeline:events,active:[],events:[]}},'grandchild');
      assert.deepEqual(filtered.lanes.map(lane=>lane.id),['root','child','grandchild'],'Filtering a grandchild preserves its complete parent chain');
    """)


def test_compact_overview_connectors_are_deliberate_and_events_remain_accessible():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Content must render as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''}};
      const events=[],put=(kind,time,session_id,fields={})=>events.push({seq:events.length+1,kind,elapsed_ms:time,session_id,...fields});
      put('turn.started',0,'root');put('phase.changed',1,'root',{phase:'model_wait'});
      for(let child=0;child<4;child++) {
        const id=`child-${child}`;
        put('agent.created',20+child,id,{parent_session_id:'root'});
        put('phase.changed',30+child,id,{phase:'tools'});
        put('message.sent',40+child,'root',{related_session_id:id,message_seq:child+1});
        put('message.read',50+child,id,{related_session_id:'root',message_seq:child+1});
        put('tool.started',60+child,id,{tool_call_id:`call-${child}`,tool_name:'command'});
        put('tool.completed',70+child,id,{tool_call_id:`call-${child}`,tool_name:'command',state:'completed'});
        put('turn.stopped',80+child,id,{reason:'finished'});
      }
      put('turn.stopped',1000,'root',{reason:'finished'});
      const frame={runtime:{active_agents:0,active_model_calls:0,active_tools:[]},agents:[{id:'root',status:'completed'},...Array.from({length:4},(_,index)=>({id:`child-${index}`,parent_id:'root',status:'completed'}))],agents_omitted:0,trace:{active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor:events.length,timeline:events}};
      const requests=[],setTimeout=()=>{};const fetch=async(path)=>{requests.push(path);return{ok:true,json:async()=>frame};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const scene=()=>nodes['timeline-lanes'].children[0];
        const descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
        const find=css=>descendants(scene()).filter(node=>(node.className||node.attributes.class||'').split(' ').includes(css));
        assert.equal(find('message-edge').length,0,'Default view leaves global communication curves out');
        assert.equal(find('agent-fork-bus').length,1);
        assert.equal(find('agent-join-bus').length,1);
        assert.equal(find('agent-branch').length,4,'Every child has its own horizontal branch');
        assert.equal(find('agent-join').length,4,'Every completed child has its own horizontal join');
        assert.equal(parseFloat(scene().style.height),5*224+38+20,'The root and all four children show complete behavior');
        assert.equal(find('activity-trend').length,0,'Density must not cover the main lines');
        assert.equal(find('lane-label').length,5);
        assert.ok(find('flow-branch-up').every(node=>parseFloat(node.style.width)>0),'Upper flow labels remain visible');
        assert.ok(find('flow-branch-down').every(node=>parseFloat(node.style.width)>0),'Lower flow labels remain visible');
        assert.equal(find('behavior-arrow-up').length,find('flow-branch-up').length,'Each upper summary has a visible arrow from its main spine');
        assert.equal(find('behavior-arrow-down').length,find('flow-branch-down').length,'Each lower summary has a visible arrow from its main spine');
        assert.ok(find('message-summary').length>0,'Message inspector remains discoverable');
        assert.ok(find('lane-label').every(node=>node.dataset.eventKey),'Agent labels retain keyboard focus through polling');
        await find('message-summary')[0].click();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.equal(nodes['selection-page'].textContent,'1–8 / 8','All four sent/read pairs remain inspectable');
        assert.equal(nodes['communication-routes'].children.length,4);
        assert.ok(nodes['communication-routes'].children.every(row=>row.title.startsWith('root → child-')),
          'Local routes preserve the actual outgoing direction');
        assert.equal(find('message-edge').length,0,'Message inspection stays below the timeline');
        await nodes['selection-close'].click();
        assert.equal(find('message-edge').length,0);
        await find('lane-label')[1].click();
        assert.equal(parseFloat(scene().style.height),5*224+38+20,'Selecting a child keeps every behavior lane open');
        assert.equal(find('lane-label')[1].attributes['aria-pressed'],'true','Selecting a child focuses its open behavior lane');
        assert.ok(find('flow-branch-down').length>0,'Child behavior remains visible');
        nodes['show-connections'].checked=true;
        await Promise.all(nodes['show-connections'].handlers.change.map(handler=>handler()));
        assert.equal(find('message-edge').length,0);
        assert.equal(find('agent-branch').length,4);
        nodes['show-connections'].checked=false;
        await Promise.all(nodes['show-connections'].handlers.change.map(handler=>handler()));
        assert.equal(find('message-edge').length,0);
        await find('lane-label')[1].click();
        assert.equal(parseFloat(scene().style.height),5*224+38+20,'Clicking the selected child cannot collapse its behavior');
        await find('timeline-event')[0].click();
        assert.equal(nodes['event-selection'].hidden,false);
        assert.ok(Number.isInteger(JSON.parse(nodes['event-detail-meta'].textContent).seq));
        assert.equal(requests.length,1,'Overview interactions only expose metadata until details requested');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)


def test_desktop_fits_and_agent_identity_remains_pinned_while_scrolling():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div') {this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=760;this.scrollLeft=0;}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      let mobile=false;
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:''},matchMedia:()=>({matches:mobile})};
      const frame={runtime:{active_agents:0,active_model_calls:0,active_tools:[]},agents:[{id:'root',status:'completed',context_status:{utilization:.165}}],agents_omitted:0,trace:{active:[],events:[],capacity:100,dropped:0,generation:'one',cursor:2,timeline:[{seq:1,kind:'turn.started',elapsed_ms:0,session_id:'root'},{seq:2,kind:'turn.stopped',elapsed_ms:1000,session_id:'root',reason:'finished'}]}};
      const setTimeout=()=>{},fetch=async()=>({ok:true,json:async()=>frame}),tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        const descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
        const gutter=()=>descendants(nodes['timeline-lanes']).find(node=>node.className==='timeline-labels');
        assert.equal(nodes['timeline-content'].style.width,'760px','Desktop overview must fit its available width');
        assert.ok(gutter(),'Agent identities have an independent pinned gutter');
        assert.equal(descendants(gutter()).filter(node=>node.className==='lane-label').length,1);
        assert.equal(descendants(gutter()).filter(node=>node.className==='lane-summary').length,1);
        assert.equal(gutter().style.transform,'translateX(0px)');
        nodes['timeline-scroll'].scrollLeft=240;
        await Promise.all(nodes['timeline-scroll'].handlers.scroll.map(handler=>handler()));
        assert.equal(gutter().style.transform,'translateX(240px)','Manual horizontal scrolling leaves Agent identities at the viewport edge');
        await nodes.latest.click();
        assert.equal(nodes['timeline-scroll'].scrollLeft,0);
        assert.equal(gutter().style.transform,'translateX(0px)');
        await nodes['zoom-in'].click();
        assert.equal(nodes['timeline-content'].style.width,'1140px');
        assert.equal(gutter().style.transform,'translateX(380px)','Follow-latest updates the pinned gutter after zooming');
        mobile=true;nodes['timeline-scroll'].clientWidth=342;
        await nodes['zoom-out'].click();
        assert.equal(nodes['timeline-content'].style.width,'820px','Mobile retains readable horizontal timeline space');
        assert.equal(gutter().style.transform,'translateX(478px)');
        assert.ok(descendants(nodes.agents).some(node=>node.textContent.startsWith('工作预算 16.5%')),'Agent cards name the working budget, not model capacity');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)
