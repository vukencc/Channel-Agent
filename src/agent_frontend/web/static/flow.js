/* Flow 编辑器只保存窗口内草稿，服务端仍负责校验和执行。 */
function flowCanConnect(nodes, edges, source, target) {
  const ids = new Set(nodes.map((node) => node.id));
  if (!ids.has(source) || !ids.has(target) || source === target || edges.some((edge) => edge.source === source && edge.target === target)) return false;
  const seen = new Set(), queue = [target];
  while (queue.length) {
    const id = queue.pop();
    if (id === source) return false;
    if (seen.has(id)) continue;
    seen.add(id); edges.filter((edge) => edge.source === id).forEach((edge) => queue.push(edge.target));
  }
  return true;
}
function flowProjectInfo(flow, nodeId) {
  const nodes = new Map((flow.nodes || []).map((node) => [node.id, node]));
  const incoming = new Map(), memo = new Map(), visiting = new Set();
  for (const edge of flow.edges || []) {
    if (!incoming.has(edge.target)) incoming.set(edge.target, []);
    incoming.get(edge.target).push(edge.source);
  }
  function normalize(path) {
    if (!path) return "";
    if (!path.startsWith("/") || path.startsWith("//") || path.split("/").includes("..")) return null;
    return "/" + path.split("/").filter((part) => part && part !== ".").join("/");
  }
  function resolve(id) {
    if (memo.has(id)) return memo.get(id);
    if (visiting.has(id) || !nodes.has(id)) return { error: "项目继承存在循环或无效节点" };
    visiting.add(id);
    const node = nodes.get(id), parents = incoming.get(id) || [], own = node.workspace_path || "";
    const candidate = own || (!parents.length ? flow.workspace_path || "" : "");
    const normalized = normalize(candidate);
    let result;
    if (normalized === null) result = { error: "项目路径不合法：请选择绝对目录，不能包含 .. 或以 // 开头。" };
    else if (!parents.length) result = normalized ? { path: normalized, source: own ? "节点指定" : "流程默认" } : { error: "尚未选择项目" };
    else {
      const inherited = parents.map(resolve), failed = inherited.find((entry) => entry.error);
      if (failed) result = failed;
      else if (new Set(inherited.map((entry) => entry.path)).size !== 1 || (own && normalized !== inherited[0].path)) result = { error: "项目冲突，请检查本节点及上游项目" };
      else result = { path: own ? normalized : inherited[0].path, source: own ? "节点指定" : "继承上游" };
    }
    visiting.delete(id); memo.set(id, result); return result;
  }
  return resolve(nodeId);
}
function flowCanvasBounds(nodes) {
  if (!nodes.length) return { minX: 0, minY: 0, width: 600, height: 400 };
  const minX = Math.min(...nodes.map((node) => node.x - 80));
  const minY = Math.min(...nodes.map((node) => node.y - 80));
  const maxX = Math.max(...nodes.map((node) => node.x + 260));
  const maxY = Math.max(...nodes.map((node) => node.y + 160));
  return { minX, minY, width: Math.max(600, maxX - minX), height: Math.max(400, maxY - minY) };
}
function flowDefinition(flow) {
  const { id, title, workspace_path, handoff, execution_environment, permission_policy, model_profile, nodes, edges, revision } = flow;
  return structuredClone({ id, title, workspace_path, handoff, execution_environment, permission_policy, model_profile, nodes, edges, revision });
}
if (typeof module !== "undefined" && module.exports) module.exports = { flowCanConnect, flowProjectInfo, flowCanvasBounds, flowDefinition };
if (typeof window !== "undefined") (() => {
  let bridge, active = null, selected = null, selectedEdge = null, generation = 0, revision = -1, epoch = null;
  let scale = 1, pickerGeneration = 0, selectionVersion = 0, summaries = [], pending = false, lastError = "", fitReset = false;
  const drafts = new Map(), dirty = new Set(), requestVersions = new Map(), inspectorModes = new Map(), refreshDeferred = new Set();
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls = "", text = "") => {
    const node = document.createElement(tag); node.className = cls; node.textContent = text; return node;
  };
  const button = (label, action, cls = "button subtle") => {
    const node = el("button", cls, label); node.type = "button"; node.addEventListener("click", action); return node;
  };
  const path = (id, suffix = "") => `/api/flows/${encodeURIComponent(id)}${suffix}`;
  const current = () => drafts.get(active);
  const running = (flow = current()) => ["queued", "running", "cancelling"].includes(flow?.run?.status);
  const locked = () => pending || running();
  const error = (message) => { lastError = message || ""; const target = $("flow-error"); if (target) { target.textContent = lastError; target.hidden = !lastError; } };
  const mark = () => { dirty.add(active); renderList(); updateHeader(); };
  function updateHeader() {
    const f = current(); if (!f || !$("flow-title")) return;
    $("flow-title").textContent = (f.title || "未命名 Flow") + (dirty.has(active) ? " *" : "");
    const targets = new Set(f.edges.map((edge) => edge.target));
    const roots = f.nodes.filter((node) => !targets.has(node.id));
    $("flow-project").textContent = f.workspace_path ? `${f.workspace_path.split("/").filter(Boolean).pop() || "/"} · ${f.workspace_path}` : roots.length && roots.every((node) => node.workspace_path) ? "未选择；起点已有节点项目，运行前由 Core 校验。" : "未选择；也可为起点单独设置项目。";
  }
  function renderList() {
    const root = $("flow-list"); if (!root) return; root.replaceChildren();
    summaries.forEach((item) => {
      const row = button((drafts.get(item.id)?.title || item.title || "未命名 Flow") + (dirty.has(item.id) ? " *" : ""), () => open(item.id), "session-item flow-list-item" + (active === item.id ? " selected" : ""));
      root.append(row);
    });
    if (!summaries.length) root.append(el("p", "field-hint flow-empty", "还没有 Flow。"));
  }
  function snapshot(data, { authoritative = false } = {}) {
    if (!Array.isArray(data.flows) || !Number.isFinite(data.flows_revision)) return;
    const incomingEpoch = typeof data.flows_epoch === "string" ? data.flows_epoch : null;
    if (epoch && !incomingEpoch) return;
    if (incomingEpoch && epoch && incomingEpoch !== epoch) {
      if (!authoritative) return;
      revision = -1;
      for (const id of requestVersions.keys()) requestVersions.set(id, requestVersions.get(id) + 1);
    }
    if (incomingEpoch && (!epoch || authoritative)) epoch = incomingEpoch;
    if (data.flows_revision < revision) return;
    const changed = data.flows_revision > revision;
    revision = data.flows_revision; summaries = data.flows; renderList();
    if (changed && active) {
      if (pending) refreshDeferred.add(active);
      else refreshRun(active);
    }
  }
  async function refreshList() {
    const requestedEpoch = epoch;
    try { const data = await bridge.api("/api/flows"); if (requestedEpoch === epoch) snapshot(data); } catch (e) { bridge.toast(e.message, "error"); }
  }
  async function refreshRun(id) {
    const version = (requestVersions.get(id) || 0) + 1; requestVersions.set(id, version);
    try {
      const f = await bridge.api(path(id));
      if (!drafts.has(id) || pending || requestVersions.get(id) !== version) return;
      const draft = drafts.get(id), wasRunning = running(draft);
      const editing = active === id && Boolean(document.activeElement?.closest(".flow-inspector-body input, .flow-inspector-body textarea, .flow-inspector-body select"));
      draft.run = f.run;
      if (!dirty.has(id) && !editing) drafts.set(id, f);
      if (active === id && (wasRunning || running(f) || (!dirty.has(id) && !editing))) render();
    } catch (e) { if (active === id) error(e.message); }
  }
  async function open(id) {
    const ticket = ++generation; active = id; selected = null; selectedEdge = null; lastError = ""; inspectorModes.set(id, "flow");
    bridge.enter(); document.body.classList.add("flow-active"); $("flow-view").hidden = false; renderList();
    if (drafts.has(id)) { render(); fitInitial(id, ticket); if (running()) refreshRun(id); return; }
    $("flow-view").replaceChildren(el("p", "flow-loading", "正在读取 Flow…"));
    const requestedEpoch = epoch;
    try {
      const f = await bridge.api(path(id));
      if (ticket !== generation || active !== id) return;
      if (requestedEpoch !== epoch) { open(id); return; }
      drafts.set(id, f); render(); fitInitial(id, ticket);
    } catch (e) { if (ticket === generation) $("flow-view").replaceChildren(el("p", "inline-error", e.message)); }
  }
  function fitInitial(id, ticket) {
    requestAnimationFrame(() => {
      if (active !== id || generation !== ticket) return;
      const view = $("flow-view").querySelector(".flow-viewport"), f = current();
      if (!view || !f) return;
      const bounds = flowCanvasBounds(f.nodes);
      if (!view.clientWidth || !view.clientHeight) return;
      scale = Math.min(1, Math.max(Number.EPSILON, Math.min(view.clientWidth / bounds.width, view.clientHeight / bounds.height)));
      fitReset = true; render();
    });
  }
  function hide() { generation++; active = null; document.body.classList.remove("flow-active"); $("flow-view").hidden = true; renderList(); }
  function openCreated(f) { drafts.set(f.id, f); summaries = [...summaries.filter((x) => x.id !== f.id), { id: f.id, title: f.title }]; open(f.id); refreshList(); }
  async function perform(kind) {
    const id = active, f = current(); if (!f || pending) return;
    if (kind === "delete" && !window.confirm("删除此 Flow？项目文件与节点会话会保留。")) return;
    const requestedEpoch = epoch;
    requestVersions.set(id, (requestVersions.get(id) || 0) + 1);
    if (kind !== "delete") refreshDeferred.add(id);
    lastError = ""; pending = true; render();
    try {
      let result;
      if (kind === "save") result = await bridge.api(path(id), { method: "PUT", body: flowDefinition(f) });
      if (kind === "run") result = await bridge.api(path(id, "/run"), { method: "POST", body: { definition: flowDefinition(f) } });
      if (kind === "cancel") result = await bridge.api(path(id, "/cancel"), { method: "POST", body: {} });
      if (kind === "delete") {
        await bridge.api(path(id), { method: "DELETE", body: { confirm: true } });
        if (requestedEpoch !== epoch) throw new Error("服务已重启，请刷新后重试。");
        drafts.delete(id); dirty.delete(id); summaries = summaries.filter((x) => x.id !== id);
        if (active === id) { hide(); $("welcome").hidden = false; }
      } else {
        if (requestedEpoch !== epoch) throw new Error("服务已重启，请核对草稿后重试。");
        drafts.set(id, result);
        if (kind === "save" || kind === "run") dirty.delete(id);
      }
      refreshList();
    } catch (e) { if (active === id) error(e.message); else bridge.toast(e.message, "error"); }
    finally {
      pending = false; if (active) render();
      const deferred = [...refreshDeferred]; refreshDeferred.clear();
      deferred.forEach((flowId) => { if (drafts.has(flowId)) refreshRun(flowId); });
    }
  }
  function inputField(parent, label, target, key, options = {}) {
    const wrap = el("div", "flow-field"), title = el("label", "", label);
    const input = document.createElement(options.select ? "select" : options.multiline ? "textarea" : "input");
    input.dataset.flowField = key; input.dataset.flowScope = options.node ? "node" : "flow";
    input.value = target[key] ?? ""; input.disabled = locked();
    if (options.select) {
      options.select.forEach(([value, text]) => { const opt = el("option", "", text); opt.value = value; input.append(opt); });
      if (target[key] && ![...input.options].some((o) => o.value === target[key])) { const opt = el("option", "", target[key]); opt.value = target[key]; input.append(opt); }
      input.value = target[key] || "";
    }
    title.append(input); wrap.append(title);
    input.addEventListener("input", () => {
      target[key] = input.value || (options.nullable ? null : ""); mark();
      if (key === "title" && options.node) {
        const card = [...$("flow-view").querySelectorAll(".flow-node")].find((item) => item.dataset.id === selected);
        if (card) { card.querySelector("strong").textContent = input.value; card.setAttribute("aria-label", input.value); }
      }
      if (key === "workspace_path") {
        updateHeader();
        const summary = $("flow-view").querySelector(".flow-project-info");
        if (summary && selected) summary.textContent = projectInfo(selected);
      }
    });
    if (options.browse) { const browse = button("选择目录", () => {
      const id = active, nodeId = selected, flowVersion = generation, nodeVersion = selectionVersion;
      pickDirectory({ input, valid: () => active === id && generation === flowVersion && (!options.node || (selected === nodeId && selectionVersion === nodeVersion)) && !locked() && (!options.node || current()?.nodes.some((node) => node.id === nodeId)), apply: (value) => { const owner = options.node ? current().nodes.find((node) => node.id === nodeId) : current(); owner[key] = value || null; mark(); render(); } });
    }); browse.disabled = locked(); wrap.append(browse); }
    parent.append(wrap); return input;
  }
  function projectInfo(id) {
    const result = flowProjectInfo(current(), id); return result.error || `${result.source}：${result.path}`;
  }
  function inspector(root) {
    const f = current(), node = f.nodes.find((n) => n.id === selected), tabs = el("div", "flow-tabs");
    const body = el("div", "flow-inspector-body"); let mode = inspectorModes.get(active) || "flow";
    const show = (next) => { mode = next; inspectorModes.set(active, next); draw(); };
    [["flow", "Flow"], ["node", "节点"], ["run", "运行"]].forEach(([key, label]) => { const b = button(label, () => show(key), "flow-tab"); b.dataset.tab = key; tabs.append(b); });
    root.append(tabs, body);
    function draw() {
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tab === mode)); body.replaceChildren();
      const models = [["", "继承默认模型"], ...(bridge.models() || []).map((m) => typeof m === "string" ? [m, m] : [m.id || m.name || m.value || m.key || m.profile, m.label || m.title || m.display_name || m.name || m.id || m.value || m.key || m.profile]).filter(([v]) => v)];
      if (mode === "run") {
        const result = node && f.run?.nodes?.[node.id];
        body.append(el("h3", "", node ? node.title : "最近运行"), el("p", "field-hint", `流程状态：${f.run?.status || "尚未运行"}`));
        if (result) {
          [["状态", "status"], ["项目目录", "workspace_path"], ["输入交接", "received_handoff"], ["输出交接", "output_handoff"], ["错误", "error"]].forEach(([label, key]) => { body.append(el("h4", "", label), el("pre", "flow-result", String(result[key] || "—"))); });
          if (result.session_id) body.append(button("打开节点会话", () => bridge.selectSession(result.session_id)));
        } else body.append(el("p", "field-hint", "选择节点后查看运行详情。"));
        return;
      }
      if (mode === "node" && !node) { body.append(el("p", "field-hint", "选择画布中的节点。")); return; }
      const target = mode === "node" ? node : f;
      inputField(body, "名称", target, "title", { node: mode === "node" });
      if (node && mode === "node") inputField(body, "节点指令", node, "prompt", { multiline: true });
      inputField(body, "项目目录", target, "workspace_path", { browse: true, nullable: true, node: mode === "node" });
      if (mode === "node") body.append(el("p", "field-hint flow-project-info", projectInfo(node.id)));
      const details = el("details", "flow-context"); details.open = Boolean(target.handoff || (mode === "node" && target.handoff_file)); details.append(el("summary", "", "可选补充背景"));
      inputField(details, mode === "node" ? "补充背景（可选）" : "项目背景（可选）", target, "handoff", { multiline: true });
      if (mode === "node") inputField(details, "已有交接文件（可选）", target, "handoff_file", { nullable: true });
      body.append(details);
      if (mode === "flow") {
        inputField(body, "执行环境", f, "execution_environment", { nullable: true, select: [["", "使用默认环境"], ["sandbox", "隔离沙箱"], ["local", "Linux 本机"]] });
        inputField(body, "权限模式", f, "permission_policy", { nullable: true, select: [["", "使用默认权限"], ["smart", "智能申请"], ["readonly", "只读"], ["full_access", "完全访问"]] });
      }
      inputField(body, "模型", target, "model_profile", { nullable: true, select: models });
      if (mode === "node") {
        const choices = f.nodes.filter((n) => n.id !== node.id);
        const select = document.createElement("select"); choices.forEach((n) => { const o = el("option", "", n.title); o.value = n.id; select.append(o); }); select.disabled = locked() || !choices.length;
        body.append(el("h4", "", "连接到节点"), select, button("添加连接", () => addEdge(node.id, select.value)));
      }
    }
    draw();
  }
  function addEdge(source, target) {
    const f = current(); if (locked()) return;
    if (!flowCanConnect(f.nodes, f.edges, source, target)) { error("连接无效、重复或会形成环。"); return; }
    f.edges.push({ source, target }); mark(); render();
  }
  function removeSelected() {
    const f = current(); if (!f || locked()) return;
    if (selected) { f.nodes = f.nodes.filter((n) => n.id !== selected); f.edges = f.edges.filter((e) => e.source !== selected && e.target !== selected); selected = null; selectionVersion++; inspectorModes.set(active, "flow"); }
    else if (selectedEdge) { f.edges = f.edges.filter((e) => e !== selectedEdge); selectedEdge = null; }
    else return;
    mark(); render();
  }
  function render() {
    const f = current(); if (!f || !active) return;
    const root = $("flow-view"), previous = root.querySelector(".flow-viewport");
    const focused = document.activeElement;
    const focusState = root.contains(focused) && focused.dataset?.flowField ? { field: focused.dataset.flowField, scope: focused.dataset.flowScope, node: selected, start: focused.selectionStart, end: focused.selectionEnd } : null;
    const scroll = previous?.dataset.flowId === active ? { x: previous.scrollLeft, y: previous.scrollTop, minX: Number(previous.dataset.minX), minY: Number(previous.dataset.minY), scale: Number(previous.dataset.scale) } : null;
    root.replaceChildren();
    const header = el("header", "flow-header"), title = el("h1"); title.id = "flow-title";
    const actions = el("div", "flow-actions");
    [["保存", () => perform("save")], [running(f) ? "停止" : "运行", () => perform(running(f) ? "cancel" : "run")], ["删除", () => perform("delete")]].forEach(([label, fn], i) => { const b = button(label, fn, i === 1 ? "button primary" : "button subtle"); b.disabled = pending || (running(f) && i !== 1); actions.append(b); });
    header.append(title, actions);
    const project = el("div", "flow-project-bar"), projectLabel = el("span"); projectLabel.id = "flow-project";
    const choose = button("选择项目", () => {
      const input = document.createElement("input"); input.value = f.workspace_path || ""; const id = active, flowVersion = generation;
      pickDirectory({ input, valid: () => active === id && generation === flowVersion && !locked(), apply: (value) => { current().workspace_path = value || null; mark(); render(); } });
    }); choose.disabled = locked(); project.append(el("strong", "", "流程默认项目"), projectLabel, choose);
    const err = el("p", "inline-error flow-error", lastError); err.id = "flow-error"; err.hidden = !lastError;
    const toolbar = el("div", "flow-toolbar");
    const bounds = flowCanvasBounds(f.nodes);
    const add = button("＋ 添加 AgentCore", () => { if (locked()) return; const id = crypto.randomUUID().replaceAll("-", ""), view = root.querySelector(".flow-viewport"); const x = bounds.minX + (view.scrollLeft + view.clientWidth / 2) / scale - 90, y = bounds.minY + (view.scrollTop + view.clientHeight / 2) / scale - 40; f.nodes.push({ id, title: "AgentCore", prompt: "", x, y, workspace_path: null, handoff: "", handoff_file: null, model_profile: null }); selected = id; selectedEdge = null; selectionVersion++; inspectorModes.set(active, "node"); mark(); render(); });
    add.disabled = locked(); toolbar.append(add, button("删除所选", removeSelected), button("−", () => { scale = Math.max(Number.EPSILON, scale / 1.25); render(); }), button("＋", () => { scale = Math.min(2, scale * 1.25); render(); }), button("适配", () => { const view = root.querySelector(".flow-viewport"); scale = Math.min(1, Math.max(Number.EPSILON, Math.min(view.clientWidth / bounds.width, view.clientHeight / bounds.height))); fitReset = true; render(); }));
    const layout = el("div", "flow-layout"), viewport = el("div", "flow-viewport"), stage = el("div", "flow-stage"), canvas = el("div", "flow-canvas");
    viewport.dataset.flowId = active; viewport.dataset.minX = bounds.minX; viewport.dataset.minY = bounds.minY; viewport.dataset.scale = scale;
    stage.style.width = `${bounds.width * scale}px`; stage.style.height = `${bounds.height * scale}px`;
    canvas.style.width = `${bounds.width}px`; canvas.style.height = `${bounds.height}px`; canvas.style.transform = `scale(${scale})`; canvas.style.transformOrigin = "top left";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("class", "flow-edges"); svg.setAttribute("width", bounds.width); svg.setAttribute("height", bounds.height);
    const defs = document.createElementNS(svg.namespaceURI, "defs"), marker = document.createElementNS(svg.namespaceURI, "marker"); marker.setAttribute("id", "flow-arrow"); marker.setAttribute("viewBox", "0 0 10 10"); marker.setAttribute("refX", "9"); marker.setAttribute("refY", "5"); marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7"); marker.setAttribute("orient", "auto"); const arrow = document.createElementNS(svg.namespaceURI, "path"); arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z"); marker.append(arrow); defs.append(marker); svg.append(defs);
    f.edges.forEach((edge) => { const a = f.nodes.find((n) => n.id === edge.source), b = f.nodes.find((n) => n.id === edge.target); if (!a || !b) return; const line = document.createElementNS(svg.namespaceURI, "path"); line.setAttribute("d", `M ${a.x - bounds.minX + 180} ${a.y - bounds.minY + 40} C ${a.x - bounds.minX + 240} ${a.y - bounds.minY + 40}, ${b.x - bounds.minX - 60} ${b.y - bounds.minY + 40}, ${b.x - bounds.minX} ${b.y - bounds.minY + 40}`); line.setAttribute("marker-end", "url(#flow-arrow)"); line.classList.add("flow-edge"); if (edge === selectedEdge) line.classList.add("selected"); line.addEventListener("click", (event) => { event.stopPropagation(); if (selected) selectionVersion++; selected = null; selectedEdge = edge; inspectorModes.set(active, "flow"); render(); }); svg.append(line); });
    canvas.append(svg);
    f.nodes.forEach((node) => {
      const card = el("div", "flow-node" + (node.id === selected ? " selected" : "")); card.style.left = `${node.x - bounds.minX}px`; card.style.top = `${node.y - bounds.minY}px`; card.tabIndex = 0; card.setAttribute("role", "button"); card.setAttribute("aria-label", node.title);
      const status = f.run?.nodes?.[node.id]?.status || "AgentCore"; card.append(el("strong", "", node.title), el("small", "", status));
      const portIn = el("span", "flow-port input-port"), portOut = el("span", "flow-port output-port"); portIn.title = "输入"; portOut.title = "拖到目标节点的输入端口"; card.append(portIn, portOut);
      card.addEventListener("click", () => { if (selected !== node.id) { inspectorModes.set(active, "node"); selectionVersion++; } selected = node.id; selectedEdge = null; render(); });
      card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); if (selected !== node.id) { inspectorModes.set(active, "node"); selectionVersion++; } selected = node.id; render(); } });
      portOut.addEventListener("pointerdown", (event) => { if (locked()) return; event.stopPropagation(); const finish = (up) => { const target = document.elementFromPoint(up.clientX, up.clientY)?.closest(".flow-node"); if (target?.dataset.id) addEdge(node.id, target.dataset.id); }; window.addEventListener("pointerup", finish, { once: true }); });
      card.dataset.id = node.id;
      card.addEventListener("pointerdown", (event) => {
        if (locked() || event.target === portOut || event.button !== 0) return;
        event.preventDefault(); const startX = event.clientX, startY = event.clientY, x = node.x, y = node.y;
        const move = (e) => { node.x = x + (e.clientX - startX) / scale; node.y = y + (e.clientY - startY) / scale; card.style.left = `${node.x - bounds.minX}px`; card.style.top = `${node.y - bounds.minY}px`; };
        const up = (e) => { window.removeEventListener("pointermove", move); if (selected !== node.id) { inspectorModes.set(active, "node"); selectionVersion++; } selected = node.id; selectedEdge = null; if (Math.abs(e.clientX - startX) + Math.abs(e.clientY - startY) > 3) mark(); render(); };
        window.addEventListener("pointermove", move); window.addEventListener("pointerup", up, { once: true });
      });
      canvas.append(card);
    });
    stage.append(canvas); viewport.append(stage); const panel = el("aside", "flow-inspector"); inspector(panel); layout.append(viewport, panel); root.append(header, project, err, toolbar, layout); updateHeader();
    if (scroll && !fitReset) { viewport.scrollLeft = ((scroll.x / scroll.scale) + scroll.minX - bounds.minX) * scale; viewport.scrollTop = ((scroll.y / scroll.scale) + scroll.minY - bounds.minY) * scale; }
    fitReset = false;
    if (focusState && (focusState.scope !== "node" || focusState.node === selected)) {
      const replacement = [...root.querySelectorAll("[data-flow-field]")].find((item) => item.dataset.flowField === focusState.field && item.dataset.flowScope === focusState.scope);
      if (replacement && !replacement.disabled) {
        replacement.focus({ preventScroll: true });
        if (focusState.start != null && typeof replacement.setSelectionRange === "function") replacement.setSelectionRange(focusState.start, focusState.end);
      }
    }
  }
  async function pickDirectory({ input, valid, apply }) {
    const ticket = ++pickerGeneration, dialog = el("dialog", "standard-dialog flow-picker"), header = el("header", "dialog-header"), title = el("h2", "", "选择 Linux 项目目录");
    const close = button("×", () => dialog.close(), "icon-button"); header.append(title, close);
    const body = el("div", "dialog-body"), location = el("input"), parent = button("上级目录", () => load(parent.dataset.path)), list = el("div", "workspace-directory-list"), problem = el("p", "inline-error"), select = button("选择当前目录", () => { if (ticket === pickerGeneration && valid()) { input.value = location.value; if (apply) apply(location.value); else input.dispatchEvent(new Event("input", { bubbles: true })); } dialog.close(); }, "button primary");
    body.append(location, button("前往", () => load(location.value)), parent, problem, list, select); dialog.append(header, body); document.body.append(dialog);
    dialog.addEventListener("close", () => { if (ticket === pickerGeneration) pickerGeneration++; dialog.remove(); });
    async function load(p) {
      const request = ++dialog.request; select.disabled = true; problem.textContent = ""; list.replaceChildren(el("p", "field-hint", "正在读取目录…"));
      try {
        const data = await bridge.api("/api/workspaces/directories" + (p ? `?path=${encodeURIComponent(p)}` : ""));
        if (!dialog.open || ticket !== pickerGeneration || request !== dialog.request) return;
        location.value = data.path; parent.dataset.path = data.parent_path || ""; parent.disabled = !data.parent_path; select.disabled = !data.selectable; problem.textContent = data.error || ""; list.replaceChildren();
        (data.directories || []).forEach((entry) => list.append(button(entry.name, () => load(entry.path), "workspace-directory")));
        if (!data.directories?.length) list.append(el("p", "field-hint", "没有可浏览的子目录。"));
        if (data.truncated) list.append(el("p", "field-hint", "目录较多，仅显示部分；可输入完整路径。"));
      } catch (e) { if (dialog.open && ticket === pickerGeneration && request === dialog.request) { list.replaceChildren(); problem.textContent = e.message; } }
    }
    dialog.request = 0; dialog.showModal(); load(input.value);
  }
  function init(options) {
    bridge = options; $("flow-refresh").addEventListener("click", refreshList);
    window.addEventListener("beforeunload", (event) => { if (dirty.size) { event.preventDefault(); event.returnValue = ""; } });
    document.addEventListener("keydown", (event) => { if (active && !event.target.closest("input,textarea,select,dialog") && ["Delete", "Backspace"].includes(event.key)) { event.preventDefault(); removeSelected(); } });
  }
  window.FlowUI = { init, snapshot, hide, openCreated, pickDirectory };
})();
