"""DEBUG 会话入口与只读时间线请求的受控界面回归。"""

from test_debug_timeline_overview import STATIC, run_view
from test_web_workspace_layout import STATIC as WORKSPACE_STATIC


def test_workspace_debug_link_tracks_selected_session():
    source = (WORKSPACE_STATIC / "app.js").read_text()
    function = source[source.index("  function renderArchiveLink() {"):source.index("  function renderQuickOptions() {")]
    run_view("""
      const assert=require('node:assert/strict');
      const nodes={},$=id=>nodes[id]||=( {hidden:false,href:''} );
      const state={meta:{debug_enabled:true},activeId:'root/child ?'};
    """ + function + """
      renderArchiveLink();
      assert.equal(nodes['debug-link'].href,'/debug?session=root%2Fchild+%3F');
      assert.equal(nodes['debug-link'].hidden,false);
      assert.equal(nodes['open-context-archive'].href,'/debug?session=root%2Fchild+%3F&context=1');
      state.activeId='second';renderArchiveLink();
      assert.equal(nodes['debug-link'].href,'/debug?session=second');
      state.activeId=null;renderArchiveLink();
      assert.equal(nodes['debug-link'].href,'/debug');
      assert.equal(nodes['debug-link'].hidden,true);
    """)


def test_scoped_poll_rebuild_and_detail_keep_session_query():
    run_view(r"""
      const assert=require('node:assert/strict');
      class FakeNode {
        constructor(tag='div'){this.tag=tag;this.textContent='';this.children=[];this.handlers={};this.dataset={};this.attributes={};this.style={setProperty(key,value){this[key]=value;}};this.value='';this.open=false;this.hidden=false;this.disabled=false;this.checked=false;this.clientWidth=900;this.scrollLeft=0;}
        set innerHTML(value){assert.fail('Fixture metadata must render as text');}
        setAttribute(key,value){this.attributes[key]=value;if(key==='data-event-key')this.dataset.eventKey=value;}
        append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
        querySelectorAll(selector){return this.children.flatMap(child=>[...((selector==='button'&&child.tag==='button')||(selector==='[data-event-key]'&&child.dataset.eventKey)?[child]:[]),...child.querySelectorAll(selector)]);}
        addEventListener(name,handler){(this.handlers[name]||=[]).push(handler);} focus(){document.activeElement=this;}
        click(){return Promise.all((this.handlers.click||[]).map(handler=>handler({target:this})));}
      }
      const nodes={},document={hidden:false,activeElement:null,getElementById:id=>nodes[id]||=new FakeNode(),createElement:tag=>new FakeNode(tag),createElementNS:(ns,tag)=>new FakeNode(tag)},window={location:{search:'?session=root%2Fchild'}};
      const event={seq:7,kind:'model.final',elapsed_ms:20,session_id:'root/child'};
      const trace=(extra={})=>({active:[],events:[],capacity:10000,dropped:0,generation:'one',cursor:7,timeline:[event],...extra});
      const frame=(extra={})=>({runtime:{active_agents:0,active_model_calls:0,active_tools:[]},agents:[{id:'root/child',busy:false,status:'completed'}],agents_omitted:0,scope_session_id:'root/child',trace:trace(extra)});
      const requests=[],timers=[],setTimeout=callback=>timers.push(callback);
      const fetch=async(path)=>{requests.push(path);if(path.includes('/detail?'))return{ok:true,json:async()=>({content:'hello',format:'text',total_chars:5,next_offset:null})};return{ok:true,json:async()=>requests.length===3?frame({gap:true,incremental:true,timeline:[]}):frame()};};
      const tick=()=>new Promise(resolve=>setImmediate(resolve));
    """ + (STATIC / "debug.js").read_text() + r"""
      (async()=>{
        await tick();
        assert.equal(requests[0],'/api/debug?session_id=root%2Fchild','Initial scoped snapshot keeps the selected session');
        assert.equal(nodes['agent-filter'].value,'','The root scope includes its children before any optional Agent filter');
        const scene=nodes['timeline-lanes'].children[0];
        const flow=scene.querySelectorAll('button').find(button=>button.className.includes('flow-branch-up'));
        assert.ok(flow,'Selected event is present in the scoped overview');
        await flow.click();
        await nodes['event-detail-load'].click();
        assert.equal(requests[1],'/api/debug/events/7/detail?generation=one&offset=0&limit=6000&session_id=root%2Fchild');
        assert.equal(nodes['event-detail-content'].textContent,'hello');
        await timers.shift()();await tick();
        assert.equal(requests[2],'/api/debug?session_id=root%2Fchild&cursor=7&generation=one','Incremental poll retains scope');
        assert.equal(requests[3],'/api/debug?session_id=root%2Fchild','Gap rebuild retains scope and drops the stale cursor');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)
