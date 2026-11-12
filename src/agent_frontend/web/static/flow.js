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
function flowProjectInfo(flow, nodeId, platform = "linux") {
  const nodes = new Map((flow.nodes || []).map((node) => [node.id, node]));
  const incoming = new Map(), memo = new Map(), visiting = new Set();
  for (const edge of flow.edges || []) {
    if (!incoming.has(edge.target)) incoming.set(edge.target, []);
    incoming.get(edge.target).push(edge.source);
  }
  function normalize(path) {
    if (!path) return "";
    if (platform === "win32") {
      if (!/^[A-Za-z]:[\\/]/.test(path) || path.split(/[\\/]/).includes("..")) return null;
      const parts = path.slice(3).split(/[\\/]/).filter((part) => part && part !== ".");
      return path[0].toUpperCase() + ":\\" + parts.join("\\");
    }
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
    if (normalized === null) result = { error: "项目路径不合法：请选择绝对目录，不能包含 .. 或网络路径。" };
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
  const minX = Math.min(0, ...nodes.map((node) => node.x - 80));
  const minY = Math.min(0, ...nodes.map((node) => node.y - 80));
  const maxX = Math.max(...nodes.map((node) => node.x + 260));
  const maxY = Math.max(...nodes.map((node) => node.y + 160));
  return { minX, minY, width: Math.max(600, maxX - minX), height: Math.max(400, maxY - minY) };
}
function flowExpandDragCanvas(stage, canvas, bounds, node, scale) {
  const width = Math.max(parseFloat(canvas.style.width) || bounds.width, node.x - bounds.minX + 260);
  const height = Math.max(parseFloat(canvas.style.height) || bounds.height, node.y - bounds.minY + 160);
  canvas.style.width = width + "px"; canvas.style.height = height + "px";
  stage.style.width = (width * scale) + "px"; stage.style.height = (height * scale) + "px";
}
function flowCancelConnectionOnEscape(event, cancel) {
  if (event.key !== "Escape" || !cancel) return false;
  event.preventDefault(); cancel(); return true;
}
function flowCustomToolsDraft(text) {
  if (!text.trim()) return [];
  try {
    const value = JSON.parse(text), object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
    if (Array.isArray(value) && new Set(value.map((row) => row?.name)).size === value.length && value.every((row) => object(row) && typeof row.name === "string" && /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(row.name) && typeof row.description === "string" && row.description.length > 0 && object(row.parameters) && row.parameters.type === "object" && typeof row.code === "string" && row.code.length > 0 && (row.network === undefined || typeof row.network === "boolean"))) return value;
  } catch (_) {}
  return text;
}
function flowSetCustomTools(node, text, catalog) {
  node.custom_tools = flowCustomToolsDraft(text);
  if (Array.isArray(node.custom_tools) && node.tool_names != null) {
    const available = new Set([...catalog.tools.map((tool) => tool.name), ...node.custom_tools.map((tool) => tool.name)]);
    node.tool_names = node.tool_names.filter((name) => available.has(name));
  }
}
function flowContractDraft(text) {
  if (!text.trim()) return null;
  try {
    const value = JSON.parse(text);
    const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
    const types = ["string", "integer", "number", "boolean", "object", "array", "null"];
    const matches = (v, type) => type === "null" ? v === null : type === "integer" ? Number.isSafeInteger(v) : typeof v === type;
    const invalidUnicode = (s) => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s);
    if (!object(value) || Object.keys(value).some((key) => key !== "fields") || !object(value.fields)) return text;
    const entries = Object.entries(value.fields);
    if (!entries.length || entries.length > 32) return text;
    for (const [key, spec] of entries) {
      if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key) || !object(spec) || Object.keys(spec).some((k) => k !== "type" && k !== "enum") || !types.includes(spec.type)) return text;
      if (spec.enum != null && (!Array.isArray(spec.enum) || !spec.enum.length || spec.enum.length > 32 || ["number", "object", "array"].includes(spec.type) || spec.enum.some((item) => !matches(item, spec.type) || (typeof item === "string" && ([...item].length > 256 || invalidUnicode(item)))))) return text;
    }
    if (JSON.stringify(value).replace(/[^\x00-\x7f]/g, "\\u0000").length > 8192) return text;
    return value;
  } catch (_) {}
  return text;
}
function flowContractInvalid(flow) {
  return (flow.nodes || []).some((node) => typeof node.handoff_contract === "string");
}
function flowCustomToolsInvalid(flow) {
  return (flow.nodes || []).some((node) => typeof node.custom_tools === "string");
}
function flowDefinition(flow) {
  const { id, title, project_id, workspace_path, handoff, execution_environment, permission_policy, model_profile, nodes, edges, revision } = flow;
  return structuredClone({ id, title, ...(project_id !== undefined ? {project_id} : {}), workspace_path, handoff, execution_environment, permission_policy, model_profile, nodes, edges, revision });
}
function flowMergeProject(draft, response, dirty) {
  const merged = { ...response };
  if (dirty) for (const key of ["handoff", "execution_environment", "permission_policy", "model_profile"]) merged[key] = draft[key];
  return merged;
}
function flowDisplayTitle(flow) { return flow.project?.title || flow.title || "未命名 Flow"; }
function flowDesignerReply(reply, template) {
  const json = template ? JSON.stringify(template) : "", combined = reply + (json ? "\n\n" + json : "");
  const complete = Boolean(json) && json.length <= 16000;
  return { role: "assistant", display: reply, template: Boolean(json), complete,
    content: (combined.length <= 16000 ? combined : complete ? json : reply.slice(0, 16000)) || " ",
    warning: json && !complete ? "模板过大，下一轮对话不会携带完整模板；请重新描述需要调整的内容。" : "" };
}
function flowDesignerTrim(history) {
  let rows = [...history], newestTemplate = [...rows].reverse().find((item) => item.template), newestUser = [...rows].reverse().find((item) => item.role === "user");
  if (newestTemplate && !newestTemplate.complete) rows = rows.filter((item) => !item.template || item === newestTemplate);
  while (rows.length > 40 || rows.reduce((sum, item) => sum + item.content.length, 0) > 64000) {
    const index = rows.findIndex((item) => item !== newestTemplate && item !== newestUser);
    if (index < 0) break;
    rows.splice(index, 1);
  }
  return rows;
}
function flowNodeTip(node, run, models) {
  const states = { queued: "排队中", running: "运行中", cancelling: "正在停止", completed: "已完成", blocked: "已阻断", cancelled: "已取消", failed: "失败", error: "错误", pending: "等待中", confirming: "等待确认", stopping: "正在停止", checkpoint: "阶段已保存", interrupted: "已中断" };
  const model = node.model_profile ? (models.find(([id]) => id === node.model_profile)?.[1] || node.model_profile) : "继承默认";
  const lines = [`任务：${String(node.prompt || "无").slice(0, 160)}`, `状态：${states[run?.status] || run?.status || "AgentCore"}`, `模型：${model}`, `工具：${(node.tool_names || []).join("、") || "继承默认"}`];
  if (run?.subagent_count != null) lines.push(`子 Agent：${run.subagent_count}`);
  return lines.join("\n");
}
if (typeof module !== "undefined" && module.exports) module.exports = { flowCanConnect, flowProjectInfo, flowCanvasBounds, flowExpandDragCanvas, flowCancelConnectionOnEscape, flowContractDraft, flowContractInvalid, flowCustomToolsDraft, flowSetCustomTools, flowDefinition, flowMergeProject, flowDisplayTitle, flowDesignerReply, flowDesignerTrim, flowNodeTip };
if (typeof window !== "undefined") (() => {
  let bridge, active = null, selected = null, selectedEdge = null, generation = 0, revision = -1, epoch = null;
  let cancelConnection = null, cancelNodeDrag = null;
  let catalog = { tools: [], required_tool_names: [], reasoning_efforts: [], thinking_modes: [] };
  let designerController = null, designerRequestId = null, designerGeneration = 0, designerHistory = [], designerVisible = false, designerModel = "", designerEffort = "", designerDraft = "", inspectorVisible = false;
  let scale = 1, pickerGeneration = 0, selectionVersion = 0, summaries = [], projects = [], pending = false, lastError = "", fitReset = false;
  let designerError = "", designerPendingImport = null;
  let designerModelTouched = false, designerModels = [], designerModelsLoading = false, designerCreated = null, modelCatalogGeneration = 0;
  let designerDefaultAvailable = null, designerRecords = [];
  const runInputs = new Map(), drafts = new Map(), dirty = new Set(), projectDirty = new Set(), requestVersions = new Map(), inspectorModes = new Map(), refreshDeferred = new Set();
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
    $("flow-title").textContent = flowDisplayTitle(f) + (dirty.has(active) ? " *" : "");
    $("flow-title").title = $("flow-title").textContent;
    $("flow-project").textContent = f.project_id ? (f.project?.workspace_path || "未选择项目目录") : "Flow 模板 · 导入项目后运行";
    $("flow-project").title = $("flow-project").textContent;
  }
  function renderList() {
    const root = $("flow-list"); if (!root) return; root.replaceChildren();
    summaries.filter((item) => !item.project_id).forEach((item) => {
      const row = button((drafts.get(item.id)?.title || item.title || "未命名 Flow") + (dirty.has(item.id) ? " *" : ""), () => open(item.id), "session-item flow-list-item" + (active === item.id ? " selected" : ""));
      root.append(row);
    });
    if (!summaries.some((item) => !item.project_id)) root.append(el("p", "field-hint flow-empty", "还没有 Flow 模板。"));
    const projectRoot = $("project-list");
    if (projectRoot) {
      projectRoot.replaceChildren();
      projects.forEach((project) => projectRoot.append(button(project.title, () => open(project.flow_id), "session-item flow-list-item" + (active === project.flow_id ? " selected" : ""))));
      if (!projects.length) projectRoot.append(el("p", "field-hint flow-empty", "还没有 Flow 项目。"));
    }
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
    revision = data.flows_revision; summaries = data.flows; if (Array.isArray(data.projects)) projects = data.projects; renderList();
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
      const interacting = active === id && Boolean(cancelConnection || cancelNodeDrag);
      draft.run = f.run; draft.pending_runs = f.pending_runs;
      if (!dirty.has(id) && !editing && !interacting) drafts.set(id, f);
      if (active === id && (wasRunning || running(f) || (!dirty.has(id) && !editing && !interacting))) render();
    } catch (e) { if (active === id) error(e.message); }
  }
  async function open(id, { preserveDesigner = false } = {}) {
    if (!preserveDesigner) { stopDesigner(); designerVisible = false; inspectorVisible = false; }
    if (cancelConnection) cancelConnection();
    if (cancelNodeDrag) cancelNodeDrag();
    const ticket = ++generation; active = id; selected = null; selectedEdge = null; lastError = ""; inspectorModes.set(id, "flow");
    bridge.enter(); document.body.classList.add("flow-active"); $("flow-view").hidden = false; renderList();
    if (drafts.has(id)) { render(); fitInitial(id, ticket); if (running()) refreshRun(id); return; }
    $("flow-view").replaceChildren(el("p", "flow-loading", "正在读取 Flow…"));
    const requestedEpoch = epoch;
    try {
      const f = await bridge.api(path(id));
      if (ticket !== generation || active !== id) return;
      if (requestedEpoch !== epoch) { open(id, { preserveDesigner }); return; }
      drafts.set(id, f); render(); fitInitial(id, ticket);
    } catch (e) { if (ticket === generation) $("flow-view").replaceChildren(el("p", "inline-error", e.message)); }
  }
  function fitInitial(id, ticket) {
    requestAnimationFrame(() => {
      if (active !== id || generation !== ticket || cancelConnection || cancelNodeDrag) return;
      const view = $("flow-view").querySelector(".flow-viewport"), f = current();
      if (!view || !f) return;
      const bounds = flowCanvasBounds(f.nodes);
      if (!view.clientWidth || !view.clientHeight) return;
      scale = Math.min(1, Math.max(Number.EPSILON, Math.min(view.clientWidth / bounds.width, view.clientHeight / bounds.height)));
      fitReset = true; render();
    });
  }
  function hide() { stopDesigner(); designerVisible = false; if (cancelConnection) cancelConnection(); if (cancelNodeDrag) cancelNodeDrag(); generation++; active = null; document.body.classList.remove("flow-active"); $("flow-view").hidden = true; renderList(); }
  function openCreated(f, { preserveDesigner = false } = {}) { drafts.set(f.id, f); summaries = [...summaries.filter((x) => x.id !== f.id), { id: f.id, title: f.title, project_id: f.project_id }]; open(f.id, { preserveDesigner }); refreshList(); }
  async function perform(kind) {
    const id = active, f = current(); if (!f || pending) return;
    if ((kind === "run" || kind === "queue") && !f.project_id) return;
    if ((kind === "save" || kind === "run") && flowContractInvalid(f)) { error("结构化交接契约无效，请修正或清空后重试。"); return; }
    if ((kind === "save" || kind === "run") && flowCustomToolsInvalid(f)) { error("动态工具定义无效，请修正或清空后重试。"); return; }
    const inputText = runInputs.get(id) || "";
    if (kind === "delete" && !window.confirm("删除此 Flow？项目文件与节点会话会保留。")) return;
    const requestedEpoch = epoch;
    requestVersions.set(id, (requestVersions.get(id) || 0) + 1);
    if (kind !== "delete") refreshDeferred.add(id);
    lastError = ""; pending = true; render();
    try {
      let result;
      if ((kind === "save" || kind === "run") && f.project_id && projectDirty.has(id)) {
        const p = f.project;
        f.project = await bridge.api(`/api/projects/${encodeURIComponent(f.project_id)}`, { method: "PATCH", body: { revision: p.revision, execution_environment: p.execution_environment, permission_policy: p.permission_policy, model_profile: p.model_profile, handoff: p.handoff } });
        projectDirty.delete(id);
      }
      if (kind === "save") result = await bridge.api(path(id), { method: "PUT", body: flowDefinition(f) });
      if (kind === "run") result = await bridge.api(path(id, "/run"), { method: "POST", body: { definition: flowDefinition(f), input_text: inputText } });
      if (kind === "queue") result = await bridge.api(path(id, "/run"), { method: "POST", body: { input_text: inputText, queue_only: true } });
      if (kind === "cancel") result = await bridge.api(path(id, "/cancel"), { method: "POST", body: {} });
      if (kind === "retry-completion") result = await bridge.api(path(id, "/retry-completion"), { method: "POST", body: {} });
      if (kind === "delete") {
        await bridge.api(f.project_id ? `/api/projects/${encodeURIComponent(f.project_id)}` : path(id), { method: "DELETE", body: { confirm: true } });
        if (requestedEpoch !== epoch) throw new Error("服务已重启，请刷新后重试。");
        drafts.delete(id); dirty.delete(id); projectDirty.delete(id); summaries = summaries.filter((x) => x.id !== id); projects = projects.filter((x) => x.flow_id !== id);
        if (active === id) { hide(); $("welcome").hidden = false; }
      } else {
        if (requestedEpoch !== epoch) throw new Error("服务已重启，请核对草稿后重试。");
        if (kind === "queue" || kind === "retry-completion") { f.run = result.run; f.pending_runs = result.pending_runs; }
        else drafts.set(id, result);
        if (kind === "run" || kind === "queue") runInputs.delete(id);
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
    input.setAttribute("aria-label", label);
    input.dataset.flowField = key; input.dataset.flowScope = options.node ? "node" : "flow";
    if (key === "model_profile") input.dataset.flowModel = "inspector";
    input.value = target[key] ?? ""; input.disabled = locked();
    if (options.select) {
      options.select.forEach(([value, text]) => { const opt = el("option", "", text); opt.value = value; input.append(opt); });
      if (target[key] && ![...input.options].some((o) => o.value === target[key])) { const opt = el("option", "", key === "model_profile" ? `不可用：${target[key]}` : target[key]); opt.value = target[key]; if (key === "model_profile") opt.disabled = true; input.append(opt); }
      input.value = target[key] || "";
    }
    title.append(input); wrap.append(title);
    input.addEventListener("input", () => {
      target[key] = input.value || (options.nullable ? null : ""); if (options.project) projectDirty.add(active); mark();
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
    const result = flowProjectInfo(current(), id, (bridge.platform?.() || "linux")); return result.error || `${result.source}：${result.path}`;
  }
  function inspector(root) {
    const f = current(), node = f.nodes.find((n) => n.id === selected), tabs = el("div", "flow-tabs");
    const body = el("div", "flow-inspector-body"); let mode = inspectorModes.get(active) || "flow";
    const show = (next) => { mode = next; inspectorModes.set(active, next); draw(); };
    [["flow", "Flow"], ["node", "节点"], ...(f.project_id ? [["run", "运行"]] : [])].forEach(([key, label]) => { const b = button(label, () => show(key), "flow-tab"); b.dataset.tab = key; tabs.append(b); });
    tabs.append(button("关闭", () => { inspectorVisible = false; render(); }, "flow-close"));
    root.append(tabs, body);
    function draw() {
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tab === mode)); body.replaceChildren();
      const models = [["", "继承默认模型"], ...modelChoices()];
      if (mode === "run") {
        const result = node && f.run?.nodes?.[node.id];
        body.append(el("h3", "", node ? node.title : "最近运行"), el("p", "field-hint", `流程状态：${f.run?.status || "尚未运行"}`));
        if (f.run?.input_text) body.append(el("h4", "", "本次补充信息"), el("pre", "flow-result", f.run.input_text));
        if (result) {
          [["状态", "status"], ["项目目录", "workspace_path"], ["输入交接", "received_handoff"], ["输出交接", "output_handoff"], ["错误", "error"]].forEach(([label, key]) => { body.append(el("h4", "", label), el("pre", "flow-result", String(result[key] || "—"))); });
          const validation = result.handoff_validation;
          if (validation || node.handoff_contract) body.append(el("h4", "", "结构化格式校验"), el("pre", "flow-result", !validation ? "尚未校验" : validation.status === "passed" ? "格式校验通过（不代表业务验收通过）" : `校验失败：${validation.message || "格式不符合契约"}`));
          if (result.session_id) body.append(button("打开节点会话", () => bridge.selectSession(result.session_id)));
        } else body.append(el("p", "field-hint", "选择节点后查看运行详情。"));
        return;
      }
      if (mode === "node" && !node) { body.append(el("p", "field-hint", "选择画布中的节点。")); return; }
      const target = mode === "node" ? node : f;
      inputField(body, "名称", target, "title", { node: mode === "node" });
      if (node && mode === "node") inputField(body, "节点指令", node, "prompt", { multiline: true });
      const details = el("details", "flow-context"); details.open = Boolean((mode === "flow" && f.project_id ? f.project?.handoff : target.handoff) || (mode === "node" && target.handoff_file)); details.append(el("summary", "", "可选补充背景"));
      inputField(details, mode === "node" ? "补充背景（可选）" : "项目背景（可选）", mode === "flow" && f.project_id ? f.project : target, "handoff", { multiline: true, project: mode === "flow" && Boolean(f.project_id) });
      body.append(details);
      if (mode === "flow" && f.project_id && f.project) {
        inputField(body, "执行环境", f.project, "execution_environment", {project: true, nullable: true, select: [["", "使用默认环境"], ["sandbox", (bridge.platform?.() || "linux") === "win32" ? "WSL 隔离沙箱" : "隔离沙箱"], ...((bridge.platform?.() || "linux") === "linux" ? [["local", "Linux 本机"]] : [])]});
        inputField(body, "权限模式", f.project, "permission_policy", {project: true, nullable: true, select: [["", "使用默认权限"], ["smart", "智能申请"], ["readonly", "只读"], ["full_access", "完全访问"]]});
      }
      inputField(body, "模型", mode === "flow" && f.project_id ? f.project : target, "model_profile", { project: mode === "flow" && Boolean(f.project_id), nullable: true, select: models });
      if (mode === "node") {
        const contractWrap = el("div", "flow-field"), contractLabel = el("label", "", "结构化交接契约（可选）"), contract = document.createElement("textarea"), contractHint = el("p", "field-hint", '留空不配置。例：{"fields":{"answer":{"type":"string"}}}；所有字段必填，不允许额外字段。number 不支持枚举；integer 枚举限安全整数，字符串须为有效 Unicode。'), contractError = el("p", "inline-error");
        contract.setAttribute("aria-label", "结构化交接契约（可选）"); contract.dataset.flowField = "handoff_contract"; contract.dataset.flowScope = "node"; contract.disabled = locked();
        contract.value = typeof node.handoff_contract === "string" ? node.handoff_contract : node.handoff_contract == null ? "" : JSON.stringify(node.handoff_contract, null, 2);
        const updateContractError = () => { contractError.hidden = typeof node.handoff_contract !== "string"; contractError.textContent = contractError.hidden ? "" : "契约无效，请修正或清空。"; };
        updateContractError(); contract.addEventListener("input", () => { node.handoff_contract = flowContractDraft(contract.value); updateContractError(); mark(); });
        contractLabel.append(contract); contractWrap.append(contractLabel, contractHint, contractError); body.append(contractWrap);
        const reuseLabel = el("label", "flow-tool-choice"), reuse = document.createElement("input"); reuse.type = "checkbox"; reuse.checked = Boolean(node.reuse_session); reuse.disabled = locked(); reuseLabel.append(reuse, document.createTextNode("复用会话")); body.append(reuseLabel);
        reuse.addEventListener("change", () => { node.reuse_session = reuse.checked; mark(); });
        inputField(body, "思考模式", node, "thinking_mode", { node: true, nullable: true, select: [["", "继承默认"], ...catalog.thinking_modes.map((v) => [v, v])] });
        inputField(body, "推理强度", node, "reasoning_effort", { node: true, nullable: true, select: [["", "继承默认"], ...catalog.reasoning_efforts.map((v) => [v, v])] });
        const tools = el("details", "flow-context flow-tool-options"), summary = el("summary", "", "节点工具"); tools.append(summary);
        const inherit = el("label", "flow-tool-choice"), toggle = document.createElement("input"); toggle.type = "checkbox"; toggle.checked = node.tool_names == null; toggle.disabled = locked(); inherit.append(toggle, document.createTextNode("继承默认工具")); tools.append(inherit);
        toggle.addEventListener("change", () => { node.tool_names = toggle.checked ? null : [...catalog.required_tool_names]; mark(); draw(); });
        catalog.tools.forEach((tool) => { const label = el("label", "flow-tool-choice"), check = document.createElement("input"); check.type = "checkbox"; const required = tool.required || catalog.required_tool_names.includes(tool.name); check.checked = required || node.tool_names == null || node.tool_names.includes(tool.name); check.disabled = locked() || required || node.tool_names == null; label.title = tool.description || ""; label.append(check, document.createTextNode(tool.name + (required ? "（基础）" : ""))); tools.append(label); check.addEventListener("change", () => { const values = new Set(node.tool_names || []); if (check.checked) values.add(tool.name); else values.delete(tool.name); node.tool_names = [...values]; mark(); }); });
        const customLabel = el("label", "flow-field", "动态工具定义（JSON 数组）"), customEdit = document.createElement("textarea"), customHint = el("p", "field-hint", "Python 代码须定义 main(arguments)，返回可序列化的 JSON。"), customError = el("p", "inline-error"), customChoices = el("div", "flow-custom-tool-choices");
        customEdit.setAttribute("aria-label", "动态工具定义（JSON 数组）"); customEdit.disabled = locked();
        customEdit.placeholder = '[{"name":"example","description":"说明","parameters":{"type":"object","properties":{}},"code":"def main(arguments):\\n    return {}","network":false}]';
        customEdit.value = typeof node.custom_tools === "string" ? node.custom_tools : JSON.stringify(node.custom_tools || [], null, 2);
        function renderCustomChoices() {
          customChoices.replaceChildren(); customError.hidden = typeof node.custom_tools !== "string"; customError.textContent = customError.hidden ? "" : "定义无效，请修正。";
          for (const tool of Array.isArray(node.custom_tools) ? node.custom_tools : []) {
            const label = el("label", "flow-tool-choice"), check = document.createElement("input"); check.type = "checkbox"; check.checked = node.tool_names == null || node.tool_names.includes(tool.name); check.disabled = locked() || node.tool_names == null; label.title = tool.description || ""; label.append(check, document.createTextNode(tool.name + "（自定义）")); customChoices.append(label);
            check.addEventListener("change", () => { const values = new Set(node.tool_names || []); if (check.checked) values.add(tool.name); else values.delete(tool.name); node.tool_names = [...values]; mark(); });
          }
        }
        customEdit.addEventListener("input", () => { flowSetCustomTools(node, customEdit.value, catalog); renderCustomChoices(); mark(); });
        customLabel.append(customEdit); tools.append(customLabel, customHint, customError, customChoices); renderCustomChoices();
        body.append(tools);
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
    if (cancelConnection) cancelConnection();
    if (cancelNodeDrag) cancelNodeDrag();
    const f = current(); if (!f || !active) return;
    const root = $("flow-view"), previous = root.querySelector(".flow-viewport");
    const focused = document.activeElement;
    const focusState = root.contains(focused) && focused.dataset?.flowField ? { field: focused.dataset.flowField, scope: focused.dataset.flowScope, node: selected, start: focused.selectionStart, end: focused.selectionEnd } : null;
    const scroll = previous?.dataset.flowId === active ? { x: previous.scrollLeft, y: previous.scrollTop, minX: Number(previous.dataset.minX), minY: Number(previous.dataset.minY), scale: Number(previous.dataset.scale) } : null;
    const controlsScroll = scroll ? root.querySelector(".flow-controls")?.scrollLeft || 0 : 0;
    root.replaceChildren();
    const header = el("header", "flow-header"), title = el("h1"); title.id = "flow-title";
    const actions = el("div", "flow-actions");
    const retryCompletion = f.run?.status === "persistence_failed";
    [["保存", () => perform("save")], ["导出", exportDraft], ...(f.project_id || running(f) ? [[running(f) ? "停止" : retryCompletion ? "重试保存" : "运行", () => perform(running(f) ? "cancel" : retryCompletion ? "retry-completion" : "run")]] : []), ["删除", () => perform("delete")]].forEach(([label, fn]) => { const isRun = ["运行", "停止", "重试保存"].includes(label); const b = button(label, fn, isRun ? "button primary" : "button subtle"); b.disabled = pending || (running(f) && !isRun); actions.append(b); });
    const more = document.createElement("details"), moreSummary = el("summary", "button subtle", "更多"), moreActions = el("div", "flow-more-actions");
    more.className = "flow-more"; more.append(moreSummary, moreActions);
    [...actions.children].filter((b) => ["导出", "删除"].includes(b.textContent)).forEach((b) => moreActions.append(b)); actions.append(more);
    more.addEventListener("toggle", () => {
      if (!more.open) return;
      const anchor = moreSummary.getBoundingClientRect(), width = moreActions.offsetWidth, height = moreActions.offsetHeight;
      moreActions.style.left = `${Math.max(4, Math.min(anchor.left, window.innerWidth - width - 4))}px`;
      moreActions.style.top = `${Math.max(4, Math.min(anchor.bottom + 4, window.innerHeight - height - 4))}px`;
    });
    moreActions.addEventListener("click", (event) => { if (event.target.closest("button")) more.open = false; });
    header.append(title, actions);
    if (retryCompletion) header.append(el("p", "inline-error", "完成状态保存失败，请检查存储后重试保存；节点不会重跑。"));
    const runInputRow = el("div", "flow-run-input");
    if (f.project_id) {
      const input = document.createElement("textarea"); input.rows = 1; input.maxLength = 16000; input.placeholder = "本次运行补充信息（可选）"; input.setAttribute("aria-label", "本次运行补充信息（可选）"); input.dataset.flowField = "run_input"; input.dataset.flowScope = "run"; input.value = runInputs.get(active) || ""; input.disabled = pending; input.addEventListener("input", () => runInputs.set(active, input.value)); runInputRow.append(input);
      if (["queued", "running"].includes(f.run?.status) && f.nodes.some((node) => node.reuse_session)) { const queue = button("排队", () => perform("queue"), "button subtle"); queue.disabled = pending; runInputRow.append(queue); }
      runInputRow.append(el("span", "field-hint", `排队：${(f.pending_runs || []).length}`));
    }
    const project = el("div", "flow-project-bar"), projectLabel = el("span"); projectLabel.id = "flow-project";
    const choose = button("选择项目", () => {
      const input = document.createElement("input"); input.value = f.project?.workspace_path || ""; const id = active, flowVersion = generation;
      pickDirectory({ input, valid: () => active === id && generation === flowVersion && !locked(), apply: async (value) => { const revision = f.project.revision; try { const p = await bridge.api(`/api/projects/${encodeURIComponent(f.project_id)}`, {method: "PATCH", body: {workspace_path: value || null, revision}}); if (drafts.get(id) !== f || f.project?.revision !== revision) return; f.project = flowMergeProject(f.project, p, projectDirty.has(id)); if (active === id) render(); } catch (e) { if (active === id) error(e.message); else bridge.toast(e.message, "error"); } } });
    }); choose.disabled = locked(); project.append(el("strong", "", f.project_id ? "项目工作区" : "模板"), projectLabel); if (f.project_id) project.append(choose);
    const err = el("p", "inline-error flow-error", lastError); err.id = "flow-error"; err.hidden = !lastError;
    const toolbar = el("div", "flow-toolbar");
    const bounds = flowCanvasBounds(f.nodes);
    if (scroll && !fitReset) {
      const minX = Math.min(bounds.minX, scroll.minX + scroll.x / scroll.scale);
      const minY = Math.min(bounds.minY, scroll.minY + scroll.y / scroll.scale);
      bounds.width += bounds.minX - minX; bounds.height += bounds.minY - minY; bounds.minX = minX; bounds.minY = minY;
    }
    const add = button("＋ 添加 AgentCore", () => { if (locked()) return; const id = crypto.randomUUID().replaceAll("-", ""), view = root.querySelector(".flow-viewport"); const x = bounds.minX + (view.scrollLeft + view.clientWidth / 2) / scale - 90, y = bounds.minY + (view.scrollTop + view.clientHeight / 2) / scale - 40; f.nodes.push({ id, title: "AgentCore", prompt: "", x, y, workspace_path: null, handoff: "", handoff_file: null, model_profile: null }); selected = id; selectedEdge = null; selectionVersion++; inspectorModes.set(active, "node"); mark(); render(); });
    add.disabled = locked(); const remove = button("删除所选", removeSelected), zoomOut = button("−", () => { scale = Math.max(Number.EPSILON, scale / 1.25); render(); }), zoomIn = button("＋", () => { scale = Math.min(2, scale * 1.25); render(); }), fit = button("适配", () => { const view = root.querySelector(".flow-viewport"); scale = Math.min(1, Math.max(Number.EPSILON, Math.min(view.clientWidth / bounds.width, view.clientHeight / bounds.height))); fitReset = true; render(); });
    toolbar.append(add, remove, zoomOut, zoomIn, fit);
    const mobileAdd = button("＋ 添加", () => add.click(), "button subtle flow-mobile-action"); mobileAdd.disabled = add.disabled; actions.append(mobileAdd);
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
      card.title = flowNodeTip(node, f.run?.nodes?.[node.id], modelChoices());
      const portIn = el("span", "flow-port input-port"), portOut = el("span", "flow-port output-port"); portIn.title = "输入"; portOut.title = "拖到目标节点的输入端口"; card.append(portIn, portOut);
      card.addEventListener("click", () => { if (selected !== node.id) { inspectorModes.set(active, "node"); selectionVersion++; } selected = node.id; selectedEdge = null; render(); });
      const showDetails = () => { if (selected !== node.id) selectionVersion++; selected = node.id; selectedEdge = null; inspectorModes.set(active, "node"); designerVisible = false; inspectorVisible = true; render(); };
      card.addEventListener("contextmenu", (event) => { event.preventDefault(); showDetails(); });
      card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) { event.preventDefault(); showDetails(); } });
      portOut.addEventListener("pointerdown", (event) => {
        if (locked() || event.button !== 0) return;
        event.preventDefault(); event.stopPropagation();
        if (cancelConnection) cancelConnection();
        if (cancelNodeDrag) cancelNodeDrag();
        const pointerId = event.pointerId, preview = document.createElementNS(svg.namespaceURI, "path");
        preview.setAttribute("class", "flow-edge-preview"); svg.append(preview);
        let clientX = event.clientX, clientY = event.clientY;
        const update = () => {
          const rect = canvas.getBoundingClientRect();
          const x = (clientX - rect.left) / scale, y = (clientY - rect.top) / scale;
          const sx = node.x - bounds.minX + 180, sy = node.y - bounds.minY + 40;
          preview.setAttribute("d", "M " + sx + " " + sy + " C " + (sx + 60) + " " + sy + ", " + (x - 60) + " " + y + ", " + x + " " + y);
        };
        const move = (e) => { if (e.pointerId !== pointerId) return; clientX = e.clientX; clientY = e.clientY; update(); };
        const cleanup = () => {
          window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", finish);
          window.removeEventListener("pointercancel", cancel); window.removeEventListener("blur", cancel);
          viewport.removeEventListener("scroll", update); preview.remove(); cancelConnection = null;
        };
        const cancel = (e) => { if (e.pointerId !== undefined && e.pointerId !== pointerId) return; cleanup(); };
        const finish = (e) => {
          if (e.pointerId !== pointerId) return;
          const port = document.elementFromPoint(e.clientX, e.clientY)?.closest(".input-port");
          const target = port?.closest(".flow-node"); cleanup();
          if (target?.dataset.id && !locked()) addEdge(node.id, target.dataset.id);
        };
        cancelConnection = cleanup; window.addEventListener("pointermove", move); window.addEventListener("pointerup", finish);
        window.addEventListener("pointercancel", cancel); window.addEventListener("blur", cancel);
        viewport.addEventListener("scroll", update); update();
      });
      card.dataset.id = node.id;
      card.addEventListener("pointerdown", (event) => {
        if (locked() || event.target === portOut || event.button !== 0) return;
        if (cancelConnection) cancelConnection();
        event.preventDefault(); const startX = event.clientX, startY = event.clientY, startScrollX = viewport.scrollLeft, startScrollY = viewport.scrollTop, x = node.x, y = node.y;
        const pointerId = event.pointerId; let changed = false;
        const cleanup = () => {
          window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
          window.removeEventListener("pointercancel", cancel); window.removeEventListener("blur", cancel); cancelNodeDrag = null;
        };
        const cancel = (e) => {
          if (e.pointerId !== undefined && e.pointerId !== pointerId) return;
          cleanup();
          if (changed && active === f.id && drafts.get(active) === f) render();
        };
        const move = (e) => {
          if (e.pointerId !== pointerId || active !== f.id || drafts.get(active) !== f || locked()) return;
          node.x = x + (e.clientX - startX + viewport.scrollLeft - startScrollX) / scale; node.y = y + (e.clientY - startY + viewport.scrollTop - startScrollY) / scale;
          if (!changed && (node.x !== x || node.y !== y)) { changed = true; mark(); }
          flowExpandDragCanvas(stage, canvas, bounds, node, scale);
          card.style.left = (node.x - bounds.minX) + "px"; card.style.top = (node.y - bounds.minY) + "px";
        };
        const up = (e) => {
          if (e.pointerId !== pointerId) return; cleanup();
          if (active !== f.id || drafts.get(active) !== f || locked()) return;
          if (selected !== node.id) { inspectorModes.set(active, "node"); selectionVersion++; } selected = node.id; selectedEdge = null; render();
        };
        if (cancelNodeDrag) cancelNodeDrag(); cancelNodeDrag = cleanup;
        window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
        window.addEventListener("pointercancel", cancel); window.addEventListener("blur", cancel);
      });
      canvas.append(card);
    });
    const controls = el("div", "flow-controls"), toggleInspector = button(designerVisible ? "显示检查器" : inspectorVisible ? "隐藏检查器" : "显示检查器", () => { if (designerVisible) { designerVisible = false; inspectorVisible = true; } else inspectorVisible = !inspectorVisible; render(); }); toolbar.append(toggleInspector); controls.append(project, toolbar);
    const mobileActions = [["删除所选", remove], ["缩小", zoomOut], ["放大", zoomIn], ["适配", fit], [toggleInspector.textContent, toggleInspector], ...(f.project_id ? [["选择项目", choose]] : [])];
    mobileActions.forEach(([label, target]) => { const item = button(label, () => target.click(), "button subtle flow-mobile-action"); item.disabled = target.disabled; moreActions.append(item); });
    stage.append(canvas); viewport.append(stage); const panel = el("aside", "flow-inspector"); panel.hidden = !inspectorVisible || designerVisible; inspector(panel); layout.append(viewport, panel); header.append(runInputRow, controls); root.append(header, err, layout); if (designerVisible) drawDesigner(); updateHeader();
    controls.scrollLeft = controlsScroll;
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
    const ticket = ++pickerGeneration, dialog = el("dialog", "standard-dialog flow-picker"), header = el("header", "dialog-header"), title = el("h2", "", "选择项目目录");
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
  async function exportDraft() {
    if (flowContractInvalid(current())) { error("结构化交接契约无效，请修正或清空后重试。"); return; }
    if (flowCustomToolsInvalid(current())) { error("动态工具定义无效，请修正或清空后重试。"); return; }
    try {
      const data = await bridge.api("/api/flows/export", { method: "POST", body: { definition: flowDefinition(current()) } });
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = "flow-template.json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) { error(e.message); }
  }
  function importDialog() {
    const dialog = el("dialog", "standard-dialog flow-import-dialog"), body = el("div", "dialog-body"), file = document.createElement("input"), inherit = document.createElement("input"), label = el("label"), problem = el("p", "inline-error");
    file.type = "file"; file.accept = ".json,application/json"; inherit.type = "checkbox"; label.append(inherit, document.createTextNode("缺失模型时显式改为继承默认模型"));
    const submit = button("导入", async () => {
      try {
        if (!file.files.length || file.files[0].size > 1048576) throw new Error("请选择不超过 1 MiB 的 JSON 文件。");
        const template = JSON.parse(await file.files[0].text()); submit.disabled = true;
        const result = await bridge.api("/api/flows/import", { method: "POST", body: { template, missing_models: inherit.checked ? "inherit" : "error" } });
        dialog.close(); openCreated(result);
      } catch (e) { problem.textContent = e instanceof SyntaxError ? "JSON 格式无效。" : e.message; submit.disabled = false; }
    }, "button primary");
    body.append(el("h2", "", "导入 Flow 模板"), file, label, problem, submit, button("取消", () => dialog.close())); dialog.append(body); document.body.append(dialog); dialog.addEventListener("close", () => dialog.remove()); dialog.showModal();
  }
  function stopDesigner() {
    designerGeneration++;
    designerPendingImport = null; designerError = "";
    if (designerRequestId) bridge.api("/api/flow-designer/cancel", { method: "POST", keepalive: true, body: { request_id: designerRequestId } }).catch(() => bridge.toast("无法确认设计请求已停止，请检查服务连接。", "error"));
    designerRequestId = null;
    if (designerController) designerController.abort(); designerController = null;
  }
  function modelChoices() {
    return [...(bridge.models() || []), ...designerModels].map((m) => typeof m === "string" ? [m, m] : [m.id || m.value || m.name || m.key || m.profile, m.label || m.title || m.display_name || m.name || m.id || m.value || m.key || m.profile]).filter(([value], index, all) => value && all.findIndex(([id]) => id === value) === index);
  }
  function refreshModels() {
    const root = $("flow-view"); if (!root || !bridge) return;
    modelCatalogGeneration++; designerModels = [];
    const available = modelChoices();
    const metadata = bridge.modelCatalog?.();
    if (metadata) { designerDefaultAvailable = metadata.default_model_available; designerRecords = metadata.models || []; }
    else designerRecords = designerRecords.filter((item) => available.some(([id]) => id === item.id));
    if (designerModel && !designerChoices().some(([id]) => id === designerModel)) designerModel = "";
    root.querySelectorAll("select[data-flow-model]").forEach((select) => {
      const value = select.value, choices = [["", select.dataset.flowModel === "designer" ? "默认模型" : "继承默认模型"], ...available];
      select.replaceChildren(...choices.map(([id, label]) => { const option = el("option", "", label); option.value = id; return option; }));
      if (value && !choices.some(([id]) => id === value) && select.dataset.flowModel === "inspector") { const missing = el("option", "", `不可用：${value}`); missing.value = value; missing.disabled = true; select.append(missing); }
      select.value = choices.some(([id]) => id === value) || select.dataset.flowModel === "inspector" ? value : "";
    });
    if (designerVisible) drawDesigner();
  }
  function designerChoices() {
    return modelChoices().filter(([id]) => designerRecords.some((item) => item.id === id && !(item.read_only && !item.has_api_key)));
  }
  function designerCanSend(value) {
    return !designerModelsLoading && (value ? designerChoices().some(([id]) => id === value) : designerDefaultAvailable === true);
  }
  function openDesigner() {
    if (designerVisible) return;
    designerVisible = true; inspectorVisible = false;
    designerModelsLoading = true; const catalogTicket = modelCatalogGeneration;
    bridge.api("/api/models").then((data) => {
      if (catalogTicket !== modelCatalogGeneration) return;
      designerModels = Array.isArray(data.models) ? data.models : [];
      const defaultAvailable = data.default_model_available ?? Boolean(data.default_model);
      designerDefaultAvailable = defaultAvailable; designerRecords = designerModels;
      const available = designerModels.find((model) => !(model.read_only && !model.has_api_key));
      if (!designerModelTouched && !defaultAvailable && available) designerModel = available.id || "";
    }).catch(() => {}).finally(() => { designerModelsLoading = false; if (designerVisible) drawDesigner(); });
    if (active && current()) { render(); return; }
    generation++; active = null; selected = null; bridge.enter(); document.body.classList.add("flow-active"); $("flow-view").hidden = false; renderList();
    const root = $("flow-view"), header = el("header", "flow-header"), layout = el("div", "flow-layout"), placeholder = el("div", "flow-designer-placeholder", "描述需要的流程，生成后将在画布中打开。");
    header.append(el("h1", "", "Flow 设计")); layout.append(placeholder); root.replaceChildren(header, layout); drawDesigner();
  }
  function drawDesigner() {
    if (!designerVisible) return;
    const root = $("flow-view"), layout = root.querySelector(".flow-layout"), old = layout?.querySelector(".flow-designer-panel"), panel = el("aside", "flow-designer-panel"), header = el("header", "flow-designer-header"), history = el("div", "flow-designer-history"), input = document.createElement("textarea"), controls = el("div", "flow-designer-controls"), models = document.createElement("select"), effort = document.createElement("select"), problem = el("p", "inline-error");
    if (!layout) return;
    const previousHistory = old?.querySelector(".flow-designer-history");
    const follow = !previousHistory || previousHistory.scrollHeight - previousHistory.clientHeight - previousHistory.scrollTop < 8;
    const previousScroll = previousHistory?.scrollTop || 0;
    problem.textContent = designerError; problem.hidden = !designerError; problem.setAttribute("role", "alert");
    header.append(el("h2", "", "对话设计 Flow"), button("清空", () => { stopDesigner(); designerHistory = []; designerDraft = ""; designerError = ""; designerPendingImport = null; designerCreated = null; drawDesigner(); }), button("关闭", () => { designerVisible = false; if (active && current()) render(); else panel.remove(); }));
    designerHistory.forEach((item) => { const message = el("article", "flow-designer-message " + item.role); message.append(el("strong", "flow-designer-role", item.role === "user" ? "你" : "助手"), el("div", "flow-designer-text", item.display)); history.append(message); if (item.warning) history.append(el("p", "field-hint", item.warning)); });
    if (!designerHistory.length) history.append(el("p", "flow-designer-empty", "描述你的目标，助手会帮你设计和调整流程。"));
    if (designerController) { const status = el("p", "flow-designer-status", "正在设计流程…"); status.setAttribute("role", "status"); history.append(status); }
    const choices = [...(designerDefaultAvailable ? [["", "默认模型"]] : []), ...designerChoices()];
    choices.forEach(([value, label]) => { const o = el("option", "", label); o.value = value; models.append(o); });
    [["", "默认强度"], ...catalog.reasoning_efforts.map((v) => [v, v])].forEach(([value, label]) => { const o = el("option", "", label); o.value = value; effort.append(o); });
    models.dataset.flowModel = "designer"; effort.dataset.flowDesignerEffort = "true"; models.setAttribute("aria-label", "设计模型"); effort.setAttribute("aria-label", "设计推理强度"); models.value = designerModel; effort.value = designerEffort; models.addEventListener("change", () => { designerModel = models.value; designerModelTouched = true; }); effort.addEventListener("change", () => { designerEffort = effort.value; });
    input.placeholder = "描述需要的流程，或继续调整上一版"; input.maxLength = 16000; input.value = designerDraft; input.addEventListener("input", () => { designerDraft = input.value; }); input.setAttribute("aria-label", "设计需求");
    const runRequest = async (retry = false) => {
      const retryImport = retry && designerPendingImport;
      const text = input.value.trim(); if ((!retry && !text) || designerController || (!retryImport && !designerCanSend(models.value))) return;
      designerError = ""; if (!retry) { designerPendingImport = null; designerHistory.push({ role: "user", display: text, content: text }); designerHistory = flowDesignerTrim(designerHistory); designerDraft = ""; }
      const ticket = ++designerGeneration, controller = new AbortController(), requestId = retryImport ? retryImport.requestId : crypto.randomUUID().replaceAll("-", ""); designerController = controller; designerRequestId = requestId; drawDesigner();
      try {
        if (!retryImport) {
          const response = await bridge.api("/api/flow-designer/chat", { method: "POST", signal: controller.signal, body: { messages: designerHistory.map(({role, content}) => ({role, content})), model_profile: models.value || null, reasoning_effort: effort.value || null, request_id: requestId } });
          if (ticket !== designerGeneration) return;
          designerHistory.push(flowDesignerReply(response.reply, response.template)); designerHistory = flowDesignerTrim(designerHistory);
          if (response.template) designerPendingImport = { template: response.template, requestId };
        }
        if (designerPendingImport && ticket === designerGeneration) {
          const created = await bridge.api("/api/flows/import", { method: "POST", signal: controller.signal, body: { template: designerPendingImport.template, missing_models: "error", request_id: requestId } });
          if (ticket === designerGeneration) { designerPendingImport = null; designerController = null; designerRequestId = null; if (designerVisible) openCreated(created, { preserveDesigner: true }); else { drafts.set(created.id, created); summaries = [...summaries.filter((x) => x.id !== created.id), { id: created.id, title: created.title, project_id: created.project_id }]; designerCreated = created; renderList(); } return; }
        }
      } catch (e) { if (e.name !== "AbortError" && ticket === designerGeneration) { designerError = String(e.message || "设计请求失败，请检查模型配置后重试。"); } }
      finally { if (ticket === designerGeneration) { designerController = null; designerRequestId = null; drawDesigner(); } }
    };
    const send = button(designerController ? "生成中…" : "发送", () => runRequest(), "button primary");
    const guidance = el("p", "field-hint", "请先在模型管理中配置可用模型。");
    const updateSend = () => { const usable = designerCanSend(models.value); send.disabled = Boolean(designerController) || !usable; guidance.hidden = designerModelsLoading || usable; };
    models.addEventListener("change", updateSend); updateSend(); const stop = button("停止", () => { stopDesigner(); drawDesigner(); }); stop.hidden = !designerController; send.hidden = Boolean(designerController);
    if (designerCreated) controls.append(button("打开生成的 Flow", () => { const created = designerCreated; designerCreated = null; openCreated(created, { preserveDesigner: true }); }));
    if (designerError && designerHistory.length) problem.append(document.createTextNode(" "), button(designerPendingImport ? "重试导入" : "重试", () => runRequest(true)));
    const composer = el("div", "flow-designer-composer"); controls.append(models, effort, send, stop); composer.append(input, controls); panel.append(header, history, problem, guidance, composer); if (old) old.replaceWith(panel); else layout.append(panel); history.scrollTop = follow ? history.scrollHeight : previousScroll;
  }
  function init(options) {
    bridge = options; $("flow-refresh").addEventListener("click", refreshList);
    $("flow-import").addEventListener("click", importDialog); $("flow-designer").addEventListener("click", openDesigner);
    bridge.api("/api/flow-tools").then((data) => {
      catalog = data;
      const effort = $("flow-view").querySelector("select[data-flow-designer-effort]");
      if (effort) { const value = effort.value; effort.replaceChildren(...[["", "默认强度"], ...catalog.reasoning_efforts.map((v) => [v, v])].map(([id, label]) => { const option = el("option", "", label); option.value = id; return option; })); effort.value = value; }
      if (active && !designerVisible && !$("flow-view").contains(document.activeElement)) render();
    }).catch((e) => bridge.toast(e.message, "error"));
    window.addEventListener("beforeunload", (event) => { if (dirty.size) { event.preventDefault(); event.returnValue = ""; } });
    document.addEventListener("keydown", (event) => { if (flowCancelConnectionOnEscape(event, cancelConnection)) return; if (active && !event.target.closest("input,textarea,select,dialog") && ["Delete", "Backspace"].includes(event.key)) { event.preventDefault(); removeSelected(); } });
    document.addEventListener("keydown", (event) => { const more = $("flow-view").querySelector(".flow-more[open]"); if (event.key === "Escape" && more) { event.preventDefault(); more.open = false; more.querySelector("summary").focus(); } });
    document.addEventListener("click", (event) => { const more = $("flow-view").querySelector(".flow-more[open]"); if (more && !more.contains(event.target)) more.open = false; });
  }
  window.FlowUI = { init, snapshot, hide, openCreated, open, pickDirectory, refreshModels, isActive: () => Boolean(active || designerVisible), templates: () => summaries.filter((item) => !item.project_id) };
})();
