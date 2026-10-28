"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    ready: false, connecting: false, deletedIds: new Set(),
    meta: { policies: [], models: [], tools: [], limits: {} }, sessions: new Map(),
    activeId: null, openTabs: [], drafts: new Map(), history: new Map(),
    runtime: {}, tasks: [], commands: [], tasksRevision: 0, sidebarView: "sessions", filePath: "",
    monitorTab: "agents", monitorOpen: true, connected: false, connectionState: "connecting",
    sending: new Set(), loading: new Set(), pendingHistory: new Set(), snapshotVersion: 0, streamAbort: null, streamGeneration: 0,
    retryTimer: null, retryCount: 0, taskTimer: null, clockTimer: null,
    paletteItems: [], paletteIndex: 0, slashItems: [], slashIndex: 0,
    quickId: null, quickKind: null, quickSaving: false, settingsId: null, memoryId: null, filesGeneration: 0,
    eventState: null, eventFrame: null, renderSignatures: {},
    maintenance: null, maintenanceLoading: false, maintenanceGeneration: 0, cleanupPending: false,
    planOwner: null, planView: null, planLoading: false, planGeneration: 0,
    selectionGeneration: 0, planHydrationKey: null,
    layout: { left: 246, right: 310, split: 52 }, modelLibrary: [], defaultModel: null, editingModel: null, modelSaving: false,
  };
  const policyLabels = {
    read_only: "只读", readonly: "只读", read: "只读", ask: "逐次确认", confirm: "逐次确认",
    standard: "智能申请", safe: "智能申请", balanced: "智能申请", trusted: "智能申请", smart: "智能申请", auto: "智能申请",
    full: "完全访问", full_access: "完全访问", unrestricted: "完全访问", bypass: "完全访问",
  };
  const phaseLabels = {
    idle: "就绪", ready: "就绪", thinking: "正在思考", running: "运行中", working: "工作中",
    model: "正在生成", model_call: "正在生成", streaming: "正在生成", tools: "执行工具",
    tool: "执行工具", tool_call: "执行工具", waiting: "等待确认", awaiting_confirmation: "等待确认",
    waiting_confirmation: "等待确认", completed: "已完成", done: "已完成", stopped: "已停止",
    cancelled: "已取消", canceling: "正在停止", cancelling: "正在停止", error: "出现错误", failed: "失败",
    queued: "排队中", pending: "待执行", created: "已创建", interrupted: "已中断", canceled: "已取消",
  };

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }
  function setText(id, value) {
    const node = $(id), text = value == null ? "" : String(value);
    if (node.textContent !== text) node.textContent = text;
  }
  function showError(id, error) {
    const node = $(id);
    node.textContent = error ? errorText(error) : "";
    node.hidden = !error;
  }
  function errorText(error) {
    if (typeof error === "string") return error;
    if (error && typeof error.message === "string") return error.message;
    return "操作未完成，请重试。";
  }
  function detailText(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((entry) => typeof entry === "string" ? entry : [entry.loc?.join("."), entry.msg || entry.message].filter(Boolean).join(": ")).join("\n");
    if (detail && typeof detail === "object") return detail.message || detail.error || JSON.stringify(detail);
    return "";
  }
  function toast(message, kind = "success") {
    const node = element("div", "toast" + (kind === "error" ? " error" : ""));
    const icon = element("span", "toast-icon", kind === "error" ? "!" : "✓");
    const body = element("span", "toast-message", String(message).slice(0, 1500));
    const close = element("button", "", "×");
    close.type = "button"; close.setAttribute("aria-label", "关闭提示");
    close.addEventListener("click", () => node.remove());
    node.append(icon, body, close); $("toasts").append(node);
    setTimeout(() => node.remove(), kind === "error" ? 10000 : 4500);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined && typeof options.body !== "string") {
      headers.set("Content-Type", "application/json");
      options = { ...options, body: JSON.stringify(options.body) };
    }
    let response;
    try {
      response = await fetch(path, { ...options, headers, credentials: "omit", cache: "no-store" });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error("无法连接本地服务。请确认启动终端仍在运行。");
    }
    if (!response.ok) {
      let detail = "";
      try { const body = await response.json(); detail = detailText(body.detail ?? body.error ?? body); }
      catch { detail = response.statusText; }
      const error = new Error(detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return response;
  }
  async function jsonApi(path, options) { return (await api(path, options)).json(); }
  function sessionPath(id, suffix = "") { return `/api/sessions/${encodeURIComponent(id)}${suffix}`; }
  function selectedSession() { return state.sessions.get(state.activeId); }
  function phaseLabel(session) {
    if (session?.confirmation) return "等待确认";
    const phase = typeof session?.phase === "string" && session.phase.trim() ? session.phase : session?.status || "idle";
    return phaseLabels[phase] || String(phase).replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 120);
  }
  function isError(session) { return ["error", "failed"].includes(session?.status) || ["error", "failed"].includes(session?.phase); }
  function statusClass(session) { return session?.confirmation ? "waiting" : isError(session) ? "error" : session?.busy ? "busy" : ""; }
  function normalizeOptions(items, kind) {
    if (!Array.isArray(items)) return [];
    return items.map((item) => {
      const model = kind === "model" ? state.modelLibrary.find((saved) => saved.id === (typeof item === "string" ? item : item.id)) : null;
      const modelLabel = model ? [model.name || model.id, model.model].filter(Boolean).join(" · ") : null;
      if (typeof item === "string") return { value: item, label: kind === "policy" ? policyLabels[item] || item : modelLabel || item, description: "" };
      const value = item.id ?? item.name ?? item.value ?? item.key ?? item.profile ?? item.policy;
      return { value: String(value ?? ""), label: modelLabel || item.label || item.title || item.display_name || (kind === "model" ? item.name : null) || (kind === "policy" ? policyLabels[value] : null) || String(value ?? ""), description: item.description || item.help || "" };
    }).filter((item) => item.value);
  }
  function optionLabel(items, value, kind) {
    if (!value) return kind === "policy" ? "默认权限" : state.defaultModel ? `默认 · ${state.defaultModel}` : "默认模型";
    return normalizeOptions(items, kind).find((item) => item.value === value)?.label || (kind === "policy" ? policyLabels[value] : null) || value;
  }
  function workspaceLabel(session) { return session?.workspace_custom && session.workspace_path ? session.workspace_path : session?.parent_id ? (session.workspace_mode === "isolated" ? "历史独立工作区" : "父项目工作区") : "项目工作区"; }
  function plainContent(content) {
    if (content == null) return "";
    if (typeof content === "string") return content;
    if (Array.isArray(content)) return content.map((part) => typeof part === "string" ? part : part.text ?? part.content ?? (part.type === "image_url" ? "[图片]" : JSON.stringify(part))).join("\n");
    return JSON.stringify(content, null, 2);
  }
  function formatBytes(size) {
    if (typeof size !== "number") return "";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
  function elapsed(value) {
    if (!value) return "—";
    let start = typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value);
    if (!Number.isFinite(start)) return "—";
    const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
    return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
  }
  function focusInput() { if (!$('conversation').hidden) $("message-input").focus({ preventScroll: true }); }
  function nearBottom() { const node = $("message-scroll"); return node.scrollHeight - node.scrollTop - node.clientHeight < 95; }
  function scrollBottom(force = false) {
    if (force || nearBottom()) requestAnimationFrame(() => { $("message-scroll").scrollTop = $("message-scroll").scrollHeight; updateJumpButton(); });
  }
  function updateJumpButton() { $("jump-bottom").hidden = nearBottom() || !state.activeId; }

  const LAYOUT_STORAGE_KEY = "agent-workspace-layout-v1";
  function clampLayout(value, width) {
    const finite = (key, fallback) => typeof value?.[key] === "number" && Number.isFinite(value[key]) ? value[key] : fallback;
    // Reserve the activity rail, both drag handles and a readable conversation.
    const available = Math.max(440, width - 52 - 12 - 420);
    const left = Math.round(Math.max(190, Math.min(360, available - 250, finite("left", 246))));
    const right = Math.round(Math.max(250, Math.min(460, available - left, finite("right", 310))));
    const split = Math.max(24, Math.min(78, finite("split", 52)));
    return { left, right, split };
  }
  function persistLayout() {
    try { localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(state.layout)); } catch { /* Storage may be disabled. */ }
  }
  function applyLayout() {
    // Keep preferred dimensions through a temporary smaller window.
    const actual = clampLayout(state.layout, $("workspace").clientWidth);
    const root = $("workspace");
    root.style.setProperty("--sidebar-width", `${actual.left}px`);
    root.style.setProperty("--inspector-width", `${actual.right}px`);
    root.style.setProperty("--inspector-split", `${actual.split}%`);
    const available = Math.max(440, root.clientWidth - 52 - 12 - 420);
    [["sidebar-resizer", actual.left, Math.min(360, available - actual.right)], ["inspector-resizer", actual.right, Math.min(460, available - actual.left)], ["inspector-splitter", actual.split, 78]].forEach(([id, value, maximum]) => {
      $(id).setAttribute("aria-valuenow", String(Math.round(value)));
      $(id).setAttribute("aria-valuemax", String(Math.round(maximum)));
    });
  }
  function initLayout() {
    try { state.layout = clampLayout(JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY)), Math.max(1400, $("workspace").clientWidth)); } catch { /* Use default dimensions. */ }
    applyLayout();
    [["sidebar-resizer", "left"], ["inspector-resizer", "right"], ["inspector-splitter", "split"]].forEach(([id, dimension]) => {
      const handle = $(id); let drag = null;
      const update = (amount) => {
        state.layout = clampLayout({ ...state.layout, [dimension]: amount }, $("workspace").clientWidth);
        applyLayout();
      };
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        const actual = clampLayout(state.layout, $("workspace").clientWidth);
        drag = { start: dimension === "split" ? event.clientY : event.clientX, size: actual[dimension], pointer: event.pointerId };
        handle.setPointerCapture(event.pointerId); document.body.classList.add("resizing");
      });
      handle.addEventListener("pointermove", (event) => {
        if (!drag || event.pointerId !== drag.pointer) return;
        const distance = (dimension === "split" ? event.clientY : event.clientX) - drag.start;
        update(drag.size + (dimension === "right" ? -distance : dimension === "split" ? distance / Math.max(1, $("inspector").clientHeight) * 100 : distance));
      });
      const finish = () => { if (drag) { drag = null; document.body.classList.remove("resizing"); persistLayout(); } };
      handle.addEventListener("pointerup", finish); handle.addEventListener("pointercancel", finish); handle.addEventListener("lostpointercapture", finish);
      handle.addEventListener("keydown", (event) => {
        const backward = dimension === "split" ? "ArrowUp" : "ArrowLeft", forward = dimension === "split" ? "ArrowDown" : "ArrowRight";
        if (![backward, forward, "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const step = (dimension === "split" ? 2 : 12) * (event.shiftKey ? 3 : 1);
        const actual = clampLayout(state.layout, $("workspace").clientWidth);
        update(event.key === "Home" ? Number(handle.getAttribute("aria-valuemin")) : event.key === "End" ? Number(handle.getAttribute("aria-valuemax")) : actual[dimension] + (event.key === forward ? 1 : -1) * (dimension === "right" ? -step : step));
        persistLayout();
      });
    });
    window.addEventListener("resize", applyLayout);
  }
  function activityPhase(session, tools, connection) {
    if (connection !== "connected") return "connection";
    if (session?.confirmation) return "waiting";
    if (!session?.busy) {
      if (["cancelled", "canceled"].includes(session?.status)) return "cancelled";
      if (session?.status === "stopped") return "stopped";
      if (["error", "failed"].includes(session?.status)) return "error";
    }
    const phase = String(session?.phase || "").toLowerCase();
    if (/context|compact|summary|上下文|压缩|摘要/.test(phase)) return "context";
    if (tools.some((tool) => /search|rag|browse|搜索|检索/.test(String(tool.name))) || /search|rag|搜索|检索/.test(phase)) return "search";
    if (tools.length || /tool|工具/.test(phase)) return "tools";
    if (session?.busy) return "thinking";
    if (["error", "failed"].includes(session?.status)) return "error";
    return "idle";
  }
  function taskDistribution(plan, tasks) {
    const statuses = { active: 0, waiting: 0, completed: 0 };
    const count = (status, amount) => {
      if (!Number.isFinite(amount) || amount <= 0) return;
      const bucket = ["completed", "done", "failed", "cancelled", "canceled", "error", "stopped"].includes(status) ? "completed" : ["pending", "queued", "waiting", "blocked", "interrupted"].includes(status) ? "waiting" : "active";
      statuses[bucket] += amount;
    };
    // A bound child task represents a plan step, so choose one source of counts.
    if (plan && !["none", "archived"].includes(plan.status)) Object.entries(plan.counts || {}).forEach(([status, amount]) => count(status, amount));
    else tasks.forEach((task) => count(task.status || task.state || "pending", 1));
    return statuses;
  }
  function projectPlanSummary(plan) {
    if (!plan || typeof plan !== "object" || !Number.isInteger(plan.revision) || plan.revision < 0) return null;
    const counts = {};
    ["pending", "in_progress", "waiting", "blocked", "interrupted", "completed", "failed", "cancelled"].forEach((status) => {
      const count = plan.counts?.[status]; if (Number.isInteger(count) && count >= 0) counts[status] = count;
    });
    return { plan_id: typeof plan.plan_id === "string" ? plan.plan_id : null, revision: plan.revision,
      status: typeof plan.status === "string" ? plan.status : "none", verification: plan.verification === "verified" ? "verified" : "unverified",
      counts, ready: Array.isArray(plan.ready) ? plan.ready.filter((key) => typeof key === "string") : [],
      execution_signature: typeof plan.execution_signature === "string" ? plan.execution_signature : "" };
  }
  function mergePlanSummary(owner, plan, captured) {
    const session = state.sessions.get(owner), summary = projectPlanSummary(plan);
    if (!session || !summary) return false;
    const current = session.plan;
    // A newer SSE plan or runtime signature takes precedence over an older GET.
    if (current && (current.revision > summary.revision ||
        (current !== captured && (!captured || current.plan_id !== captured.plan_id ||
          (current.revision === summary.revision && current.execution_signature !== captured.execution_signature))))) return false;
    state.sessions.set(owner, { ...session, plan: summary });
    return true;
  }
  async function hydrateSelectedPlan() {
    const session = selectedSession();
    if (!state.meta.task_plans_enabled || !session || session.plan) return;
    const owner = session.id, generation = state.selectionGeneration, captured = session.plan;
    const key = `${generation}:${owner}:missing`;
    if (state.planHydrationKey === key) return;
    state.planHydrationKey = key;
    try {
      const plan = await jsonApi(`/api/sessions/${encodeURIComponent(owner)}/plan?limit=1`);
      if (generation !== state.selectionGeneration || owner !== state.activeId) return;
      if (mergePlanSummary(owner, plan, captured)) renderConversationStatus();
    } catch { /* Plan details are optional; chat and live events remain available. */ }
  }
  function renderInspector() {
    const session = selectedSession(), tools = (Array.isArray(state.runtime.active_tools) ? state.runtime.active_tools : []).filter((tool) => tool.session_id === session?.id);
    const phase = activityPhase(session, tools, state.connectionState);
    const labels = { idle: "准备就绪", thinking: "正在思考", tools: "正在执行工具", search: "正在搜索", connection: "正在连接", context: "正在整理上下文", waiting: "等待你的确认", error: "运行遇到问题", cancelled: "已取消", stopped: "已停止" };
    $("agent-visual").dataset.phase = phase;
    setText("activity-label", state.connectionState === "offline" ? "连接中断，正在重试" : labels[phase]);
    setText("activity-session-name", session?.title || "等待任务");
    setText("activity-description", phase === "cancelled" ? "本轮任务已取消。发送消息可继续这个会话。" : phase === "stopped" ? "本轮运行已停止。发送消息可继续这个会话。" : tools.length ? tools.map((tool) => tool.name || "工具").join(" · ") : session?.busy ? phaseLabel(session) : session?.confirmation ? "在对话中确认操作后继续。" : "发起对话后，运行状态会在这里更新。");
    document.querySelectorAll("[data-activity]").forEach((item) => item.classList.toggle("active", item.dataset.activity === phase));
    const context = session?.context_status || {}, capacityKnown = Number.isFinite(context.capacity_utilization);
    const ratio = capacityKnown ? context.capacity_utilization : context.utilization;
    const utilization = Number.isFinite(ratio) ? Math.max(0, Math.min(100, Math.round(ratio * 100))) : null;
    setText("context-percent", utilization == null ? "—" : `${utilization}%`);
    $("context-meter-fill").style.width = `${utilization || 0}%`;
    $("context-meter").setAttribute("aria-valuetext", utilization == null ? "首次模型调用后更新上下文占用" : `${capacityKnown ? "输入估算占声明容量" : "工作预算占用"} ${utilization}%`);
    setText("context-capacity-note", Number.isFinite(context.sent_tokens) && Number.isFinite(context.input_limit_tokens)
      ? `输入约 ${context.sent_tokens.toLocaleString()} / ${(context.context_capacity_tokens || context.input_limit_tokens).toLocaleString()} tokens · 工作预算 ${Math.round((context.utilization || 0) * 100)}%（含预留）`
      : "容量尚未声明；当前显示本地工作预算");
    if (utilization == null) $("context-meter").removeAttribute("aria-valuenow"); else $("context-meter").setAttribute("aria-valuenow", String(utilization));
    $("context-meter").title = $("context-progress").textContent;
    const plan = session?.plan && !["none", "archived"].includes(session.plan.status) ? session.plan : null, counts = plan?.counts || {}, total = Object.values(counts).filter(Number.isFinite).reduce((sum, count) => sum + Math.max(0, count), 0), completed = Math.max(0, counts.completed || 0);
    const percent = total ? Math.min(100, Math.round(completed / total * 100)) : 0;
    $("plan-meter-fill").style.width = `${percent}%`; $("plan-meter").setAttribute("aria-valuenow", String(percent));
    $("plan-meter").setAttribute("aria-valuetext", `${completed}/${total} 步完成，${plan?.verification === "verified" ? "已验证" : "仍待验收"}`);
    setText("plan-inspector-hint", plan ? `${total} 个步骤 · ${plan.verification === "verified" ? "验收已验证" : "完成后仍需验收"}` : session ? "可让 Agent 为复杂任务创建计划。" : "选择会话后查看计划。");
    const statuses = taskDistribution(plan, [...state.tasks, ...state.commands]);
    const signature = JSON.stringify(statuses);
    if (state.renderSignatures.distribution !== signature) {
      state.renderSignatures.distribution = signature;
      const fragment = document.createDocumentFragment();
      [["active", "执行中"], ["waiting", "待执行"], ["completed", "已结束"]].forEach(([key, label]) => { const card = element("div", `distribution-item ${key}`); card.append(element("strong", "", statuses[key]), element("span", "", label)); fragment.append(card); });
      $("task-distribution").replaceChildren(fragment);
    }
  }

  async function connect() {
    if (state.ready || state.connecting) return;
    state.connecting = true; clearTimeout(state.retryTimer);
    connectionStatus(state.retryCount ? "offline" : "connecting");
    try {
      const meta = await jsonApi("/api/meta");
      const snapshot = await jsonApi("/api/sessions");
      state.meta = meta; state.ready = true; state.retryCount = 0;
      renderArchiveLink();
      showError("connection-error", null); applySnapshot(snapshot);
      const remembered = sessionStorage.getItem("agent-workspace-session");
      const initial = state.sessions.has(remembered) ? remembered : state.sessions.keys().next().value;
      if (initial) selectSession(initial); else showWelcome();
      startEvents(); refreshTasks(true);
      // Model labels are supplementary; a library failure must not delay chat.
      loadModels().then(() => { if (state.activeId) renderConversationStatus(); if ($("settings-dialog").open) fillSelect("settings-model", state.meta.models, $("settings-model").value, "model"); }).catch(() => {});
      clearInterval(state.taskTimer); clearInterval(state.clockTimer);
      state.taskTimer = setInterval(() => {
        if (state.ready && !document.hidden) refreshTasks(true);
        if ($("plan-dialog").open && state.ready) refreshPlan(true);
      }, 8000);
      state.clockTimer = setInterval(() => { if (state.monitorOpen && state.monitorTab === "tools") renderMonitor(); }, 1000);
    } catch (error) {
      connectionStatus("offline"); showError("connection-error", error);
      const delay = Math.min(10000, 800 * 2 ** Math.min(state.retryCount++, 4));
      state.retryTimer = setTimeout(connect, delay);
    } finally { state.connecting = false; renderConnectionActions(); }
  }
  function reconnect() {
    clearTimeout(state.retryTimer);
    if (state.ready) { startEvents(); refreshSessions(); }
    else connect();
  }
  function renderConnectionActions() {
    ["new-session", "new-session-footer", "welcome-new", "activity-maintenance"].forEach((id) => { $(id).disabled = !state.ready; });
    $("reconnect-service").hidden = state.connectionState !== "offline";
  }
  function connectionStatus(kind) {
    state.connectionState = kind; state.connected = kind === "connected";
    $("connection-dot").className = "connection-dot " + (kind === "connected" ? "connected" : kind === "offline" ? "offline" : "");
    setText("connection-label", kind === "connected" ? "已连接" : kind === "offline" ? "连接中断 · 重试中" : "正在连接");
    renderComposer(); renderConnectionActions(); renderInspector();
  }
  function scheduleSnapshot(snapshot, flowAuthoritative = false) {
    state.eventState = { snapshot, flowAuthoritative: flowAuthoritative || Boolean(state.eventState?.flowAuthoritative) };
    if (!state.eventFrame) state.eventFrame = requestAnimationFrame(() => {
      state.eventFrame = null; const latest = state.eventState; state.eventState = null;
      if (latest && state.ready) applySnapshot(latest.snapshot, { flowAuthoritative: latest.flowAuthoritative });
    });
  }
  async function startEvents() {
    if (!state.ready) return;
    const generation = ++state.streamGeneration;
    state.streamAbort?.abort();
    if (state.eventFrame) cancelAnimationFrame(state.eventFrame);
    state.eventFrame = null; state.eventState = null;
    const controller = new AbortController(); state.streamAbort = controller;
    let watchdog;
    const armWatchdog = () => { clearTimeout(watchdog); watchdog = setTimeout(() => controller.abort(), 45000); };
    let connectedAt = 0;
    connectionStatus(state.retryCount ? "offline" : "connecting");
    try {
      const response = await api("/api/events", { signal: controller.signal, headers: { Accept: "text/event-stream" } });
      if (!response.body) throw new Error("当前浏览器无法读取实时事件流。");
      connectionStatus("connected"); connectedAt = Date.now(); armWatchdog();
      if (state.activeId) loadConversation(state.activeId, { quiet: true });
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = "", eventName = "message", dataLines = [], firstSnapshot = true;
      const dispatch = () => {
        if (dataLines.length && ["state", "message"].includes(eventName)) {
          try { scheduleSnapshot(JSON.parse(dataLines.join("\n")), firstSnapshot); firstSnapshot = false; }
          catch { /* Ignore malformed frames; a later state snapshot repairs the view. */ }
        }
        eventName = "message"; dataLines = [];
      };
      while (state.ready && generation === state.streamGeneration) {
        const { value, done } = await reader.read();
        if (generation !== state.streamGeneration) return;
        if (done) throw new Error("实时连接已关闭。");
        armWatchdog(); buffer += decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline).replace(/\r$/, ""); buffer = buffer.slice(newline + 1);
          if (!line) dispatch();
          else if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
        }
      }
    } catch (error) {
      if (!state.ready || generation !== state.streamGeneration) return;
      connectionStatus("offline");
      if (connectedAt && Date.now() - connectedAt > 5000) state.retryCount = 0;
      const delay = Math.min(10000, 800 * 2 ** Math.min(state.retryCount++, 4));
      state.retryTimer = setTimeout(() => { if (state.ready) startEvents(); }, delay);
    } finally { clearTimeout(watchdog); }
  }
  function applySnapshot(snapshot, { flowAuthoritative = false } = {}) {
    if (!snapshot || !Array.isArray(snapshot.sessions)) return;
    window.FlowUI?.snapshot(snapshot, { authoritative: flowAuthoritative });
    state.snapshotVersion++;
    const oldActive = selectedSession();
    const incoming = new Map(snapshot.sessions.filter((session) => !state.deletedIds.has(session.id)).map((session) => {
      const previous = state.sessions.get(session.id), merged = { ...previous, ...session };
      // A snapshot queued before plan hydration may still contain a lazy null.
      if (previous?.plan && (!session.plan || session.plan.revision < previous.plan.revision)) merged.plan = previous.plan;
      return [session.id, merged];
    }));
    for (const id of state.sessions.keys()) if (!incoming.has(id)) {
      state.history.delete(id); state.drafts.delete(id); state.pendingHistory.delete(id); state.sending.delete(id);
    }
    state.sessions = incoming; state.runtime = snapshot.runtime || state.runtime;
    state.openTabs = state.openTabs.filter((id) => incoming.has(id));
    if (state.activeId && !incoming.has(state.activeId)) {
      const fallback = state.openTabs.at(-1) || incoming.keys().next().value;
      state.activeId = null;
      if (fallback) selectSession(fallback); else showWelcome();
    }
    renderSessions(); renderTabs(); renderRuntime();
    const session = selectedSession();
    if (session) {
      renderConversationStatus(); renderLiveMessage();
      if (oldActive?.id === session.id && oldActive.busy && !session.busy) loadConversation(session.id, { quiet: true });
      else if (oldActive?.id === session.id && session.busy && (oldActive.phase !== session.phase || (oldActive.partial && !session.partial))) loadConversation(session.id, { quiet: true });
      if (session.total_messages !== undefined && state.history.has(session.id) && session.total_messages > state.history.get(session.id).total) loadConversation(session.id, { quiet: true });
    }
    if (state.monitorOpen) renderMonitor();
    if ($("maintenance-dialog").open) renderMaintenance();
    if ($("plan-dialog").open) {
      const owner = state.sessions.get(state.planOwner);
      if (!owner) { $("plan-dialog").close(); state.planOwner = null; }
      else if (owner.plan?.revision !== state.planView?.revision ||
               owner.plan?.execution_signature !== state.planView?.execution_signature) refreshPlan(true);
    }
  }
  async function refreshSessions() {
    try { applySnapshot(await jsonApi("/api/sessions")); }
    catch (error) { toast(errorText(error), "error"); }
  }
  function renderSessions() {
    const query = $("session-search").value.trim().toLocaleLowerCase();
    const sessions = [...state.sessions.values()].filter((session) => !query || `${session.title || "未命名会话"} ${session.id}`.toLocaleLowerCase().includes(query));
    const signature = JSON.stringify(sessions.map((s) => [s.id, s.title, s.status, s.phase, s.busy, Boolean(s.confirmation), s.unread, s.parent_id, state.activeId === s.id]));
    if (state.renderSignatures.sessions !== signature) {
      state.renderSignatures.sessions = signature;
      const fragment = document.createDocumentFragment();
      sessions.forEach((session) => {
        const row = element("button", "session-item" + (session.id === state.activeId ? " selected" : ""));
        row.type = "button"; row.setAttribute("aria-current", session.id === state.activeId ? "true" : "false");
        const icon = element("span", "session-item-icon", session.parent_id ? "↳" : "◇");
        const body = element("div", "session-item-body"), title = element("div", "session-item-title");
        title.append(element("span", "", session.title || "未命名会话"));
        if (session.unread && session.id !== state.activeId) title.append(element("span", "unread-badge", typeof session.unread === "number" ? session.unread : "新"));
        const subtitle = element("div", "session-item-subtitle");
        subtitle.append(element("span", "small-status-dot " + statusClass(session)), element("span", "", phaseLabel(session)), element("span", "session-scope-caption", "·"), element("span", "session-scope-caption", session.parent_id ? "子 Agent" : workspaceLabel(session)));
        row.title = `${session.title || "未命名会话"} · ${phaseLabel(session)} · ${session.parent_id ? "子 Agent" : workspaceLabel(session)}`;
        body.append(title, subtitle); row.append(icon, body);
        row.addEventListener("click", () => selectSession(session.id)); fragment.append(row);
      });
      $("session-list").replaceChildren(fragment);
    }
    $("sessions-empty").hidden = sessions.length > 0;
    $("sessions-empty").textContent = query ? "没有匹配的会话。" : "还没有会话。点击 + 开始第一次对话。";
    setText("session-count", `${state.sessions.size} 个会话`);
  }
  function renderTabs() {
    const signature = JSON.stringify(state.openTabs.map((id) => { const s = state.sessions.get(id); return [id, s?.title, s?.busy, state.activeId === id]; }));
    if (state.renderSignatures.tabs === signature) return;
    state.renderSignatures.tabs = signature;
    const fragment = document.createDocumentFragment();
    state.openTabs.forEach((id) => {
      const session = state.sessions.get(id); if (!session) return;
      const tab = element("div", "session-tab"); tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(id === state.activeId)); tab.tabIndex = id === state.activeId ? 0 : -1;
      if (session.busy) tab.append(element("span", "tab-dot"));
      const label = element("span", "tab-label", session.title || "未命名会话"); label.title = session.title || "未命名会话";
      const close = element("button", "tab-close", "×"); close.type = "button"; close.setAttribute("aria-label", `关闭标签：${session.title || "会话"}`);
      close.addEventListener("click", (event) => { event.stopPropagation(); closeTab(id); });
      tab.append(label, close); tab.addEventListener("click", () => selectSession(id));
      tab.addEventListener("keydown", (event) => {
        if (event.target !== tab) return;
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectSession(id); }
        if (["ArrowRight", "ArrowLeft"].includes(event.key)) {
          event.preventDefault(); const index = state.openTabs.indexOf(id), direction = event.key === "ArrowRight" ? 1 : -1;
          selectSession(state.openTabs[(index + direction + state.openTabs.length) % state.openTabs.length]);
        }
      }); fragment.append(tab);
    });
    $("session-tabs").replaceChildren(fragment);
  }
  function closeTab(id) {
    const index = state.openTabs.indexOf(id); state.openTabs = state.openTabs.filter((value) => value !== id);
    if (id === state.activeId) {
      saveDraft(); state.activeId = null;
      const next = state.openTabs[Math.max(0, index - 1)];
      if (next) selectSession(next); else showWelcome();
    }
    renderTabs();
  }
  function saveDraft() { if (state.activeId) state.drafts.set(state.activeId, $("message-input").value); }
  function showWelcome() {
    window.FlowUI?.hide();
    if ($("quick-dialog").open) $("quick-dialog").close();
    state.activeId = null; renderArchiveLink(); state.selectionGeneration++; state.planHydrationKey = null; $("welcome").hidden = false; $("conversation").hidden = true;
    sessionStorage.removeItem("agent-workspace-session"); renderSessions(); renderTabs(); renderRuntime();
    $("jump-bottom").hidden = true;
  }
  function selectSession(id) {
    if (!state.sessions.has(id)) return;
    window.FlowUI?.hide();
    if (id === state.activeId) { $("sidebar").classList.remove("mobile-open"); return; }
    if ($("quick-dialog").open) $("quick-dialog").close();
    saveDraft(); state.activeId = id; state.selectionGeneration++; state.planHydrationKey = null;
    if (!state.openTabs.includes(id)) state.openTabs.push(id);
    sessionStorage.setItem("agent-workspace-session", id);
    $("welcome").hidden = true; $("conversation").hidden = false;
    $("message-input").value = state.drafts.get(id) || ""; resizeInput(); showError("composer-error", null);
    $("slash-menu").hidden = true; $("sidebar").classList.remove("mobile-open");
    state.filePath = ""; state.filesGeneration++;
    renderSessions(); renderTabs(); renderConversationStatus();
    const history = state.history.get(id);
    if (history) renderMessages(history, true);
    else { $("messages").replaceChildren(); $("conversation-start").hidden = true; $("load-history").hidden = true; }
    renderLiveMessage(); scrollBottom(true); loadConversation(id);
    hydrateSelectedPlan();
    if (state.sidebarView === "files") loadFiles("");
  }
  async function loadConversation(id, { before = null, quiet = false } = {}) {
    if (state.loading.has(id)) { if (before === null) state.pendingHistory.add(id); return; }
    state.loading.add(id);
    const version = state.snapshotVersion, initialPlan = state.sessions.get(id)?.plan;
    if (id === state.activeId && !quiet) $("history-loading").hidden = false;
    const oldHeight = $("message-scroll").scrollHeight, oldTop = $("message-scroll").scrollTop;
    const atBottom = nearBottom();
    try {
      const query = new URLSearchParams({ limit: "60" }); if (before !== null) query.set("before", String(before));
      const data = await jsonApi(sessionPath(id, `?${query}`));
      if (!state.sessions.has(id)) return;
      const current = state.sessions.get(id), mergedSession = { ...current, ...data };
      if (current.plan && (!data.plan || current.plan.revision > data.plan.revision ||
          (current.plan !== initialPlan && current.plan.revision === data.plan.revision))) mergedSession.plan = current.plan;
      if (version !== state.snapshotVersion) {
        for (const key of ["busy", "phase", "status", "confirmation", "partial", "partial_display", "partial_reasoning", "title", "permission_policy", "model_profile", "workspace_mode", "workspace_path", "workspace_custom", "workspace_can_change", "workspace_error", "execution_environment", "parent_id", "unread", "total_messages", "context_status", "plan", "error"]) {
          if (key in current) mergedSession[key] = current[key];
        }
      }
      state.sessions.set(id, mergedSession);
      const messages = Array.isArray(data.messages) ? data.messages : [];
      const offset = Number.isInteger(data.next_before) ? data.next_before : Math.max(0, (before ?? data.total_messages ?? messages.length) - messages.length);
      const records = messages.map((message, index) => ({ message, index: offset + index }));
      const existing = state.history.get(id);
      let history;
      if (before !== null && existing) {
        const merged = new Map([...records, ...existing.records].map((record) => [record.index, record]));
        history = { ...existing, records: [...merged.values()].sort((a, b) => a.index - b.index), nextBefore: data.next_before, total: data.total_messages ?? existing.total };
      } else {
        const earlier = existing?.records.filter((record) => record.index < offset) || [];
        history = { records: [...earlier, ...records], nextBefore: earlier.length ? existing.nextBefore : data.next_before, total: data.total_messages ?? messages.length, nodes: existing?.nodes || new Map() };
      }
      state.history.set(id, history);
      if (id === state.activeId) {
        renderMessages(history); renderConversationStatus(); renderLiveMessage();
        if (before !== null) requestAnimationFrame(() => { $("message-scroll").scrollTop = oldTop + $("message-scroll").scrollHeight - oldHeight; });
        else if (atBottom || !existing) scrollBottom(true);
      }
      renderSessions(); renderTabs();
    } catch (error) {
      if (id === state.activeId) showError("session-error", error);
      else if (!quiet) toast(errorText(error), "error");
    } finally {
      state.loading.delete(id);
      if (id === state.activeId) { $("history-loading").hidden = true; $("load-history").disabled = false; updateJumpButton(); }
      if (state.pendingHistory.delete(id) && state.ready && state.sessions.has(id)) loadConversation(id, { quiet: true });
    }
  }

  function appendInline(parent, source) {
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
    let cursor = 0, match;
    while ((match = pattern.exec(source))) {
      parent.append(document.createTextNode(source.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith("`")) parent.append(element("code", "", token.slice(1, -1)));
      else if (token.startsWith("**")) parent.append(element("strong", "", token.slice(2, -2)));
      else {
        const split = token.lastIndexOf("]("), label = token.slice(1, split), href = token.slice(split + 2, -1);
        try {
          const url = new URL(href);
          if (["http:", "https:", "mailto:"].includes(url.protocol)) {
            const link = element("a", "", label); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; parent.append(link);
          } else parent.append(document.createTextNode(token));
        } catch { parent.append(document.createTextNode(token)); }
      }
      cursor = pattern.lastIndex;
    }
    parent.append(document.createTextNode(source.slice(cursor)));
  }
  function markdown(parent, content) {
    const lines = content.split("\n"); let paragraph = [], code = null, language = "", list = null;
    const flushParagraph = () => { if (paragraph.length) { const p = element("p"); appendInline(p, paragraph.join("\n")); parent.append(p); paragraph = []; } };
    const flushList = () => { list = null; };
    const flushCode = () => {
      const block = element("div", "code-block"), header = element("div", "code-block-header"), copy = element("button", "", "复制");
      copy.type = "button"; const value = code.join("\n");
      copy.addEventListener("click", () => copyText(value)); header.append(element("span", "", language || "代码"), copy);
      const pre = element("pre"), node = element("code", "", value); pre.append(node); block.append(header, pre); parent.append(block); code = null;
    };
    for (const line of lines) {
      const fence = line.match(/^\s*```(.*)$/);
      if (fence) { flushParagraph(); flushList(); if (code !== null) flushCode(); else { code = []; language = fence[1].trim(); } continue; }
      if (code !== null) { code.push(line); continue; }
      if (!line.trim()) { flushParagraph(); flushList(); continue; }
      const heading = line.match(/^(#{1,4})\s+(.+)$/), bullet = line.match(/^\s*([-*+]\s+|\d+\.\s+)(.+)$/), quote = line.match(/^>\s?(.*)$/);
      if (heading) { flushParagraph(); flushList(); const h = element(`h${Math.min(4, heading[1].length + 1)}`); appendInline(h, heading[2]); parent.append(h); }
      else if (bullet) {
        flushParagraph(); const tag = /^\d/.test(bullet[1]) ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== tag) { list = element(tag); parent.append(list); }
        const li = element("li"); appendInline(li, bullet[2]); list.append(li);
      } else if (quote) { flushParagraph(); flushList(); const block = element("blockquote"); appendInline(block, quote[1]); parent.append(block); }
      else { flushList(); paragraph.push(line); }
    }
    flushParagraph(); if (code !== null) flushCode();
  }
  function toolDetails(name, content, label = "参数", open = false) {
    const block = element("details", "tool-call-block"); block.open = open;
    const summary = element("summary"); summary.append(element("span", "", name), element("span", "tool-call-label", label));
    const pre = element("pre", "tool-result-content", plainContent(content)); block.append(summary, pre); return block;
  }
  function displayContent(message) {
    return plainContent(message.display_content ?? message.content);
  }
  function hasNarrative(message) {
    return ["user", "assistant"].includes(message.role || "assistant") && Boolean(displayContent(message).trim());
  }
  function appendToolCalls(body, message) {
    (message.tool_calls || message.toolcalls || []).forEach((call) => {
      const fn = call.function || call, name = fn.name || call.name || "工具调用";
      let args = fn.arguments ?? call.arguments ?? {};
      if (typeof args === "string") { try { args = JSON.stringify(JSON.parse(args), null, 2); } catch { /* Preserve original argument text. */ } }
      body.append(toolDetails(name, args, "调用参数"));
    });
  }
  function executionRecord(message, toolNames) {
    const body = element("div", "execution-record"), content = plainContent(message.content);
    const reasoning = plainContent(message.reasoning ?? message.reasoning_content);
    if (message.role === "tool") body.append(toolDetails(message.name || toolNames.get(message.tool_call_id) || "工具输出", content || "（无输出）", "查看结果"));
    else if (content) body.append(toolDetails(message.role === "system" ? "系统上下文" : "原始消息", content, "查看记录"));
    if (reasoning) body.append(toolDetails("思考记录", reasoning, "查看原始记录"));
    appendToolCalls(body, message);
    return body;
  }
  function buildMessage(message, toolNames) {
    const role = message.role || "assistant";
    const node = element("article", `message ${role}-message`);
    const avatar = element("div", "message-avatar " + (role === "assistant" ? "assistant-avatar" : role === "user" ? "user-avatar" : role === "tool" ? "tool-avatar" : "system-avatar"), role === "assistant" ? "A" : role === "user" ? "你" : role === "tool" ? "⌘" : "·");
    avatar.setAttribute("aria-hidden", "true"); const body = element("div", "message-body"), meta = element("div", "message-meta");
    meta.append(element("span", "", role === "assistant" ? "Agent" : role === "user" ? "你" : role === "tool" ? "工具结果" : role === "system" ? "系统上下文" : role));
    const timestamp = message.created_at || message.timestamp;
    if (timestamp) { const date = new Date(typeof timestamp === "number" && timestamp < 1e12 ? timestamp * 1000 : timestamp); if (!Number.isNaN(date.getTime())) { const time = element("time", "", date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })); time.dateTime = date.toISOString(); meta.append(time); } }
    body.append(meta);
    const reasoning = plainContent(message.reasoning ?? message.reasoning_content);
    if (reasoning) { const detail = element("details", "reasoning-block"); detail.append(element("summary", "", "查看思考记录"), element("div", "reasoning-content", reasoning)); body.append(detail); }
    const content = displayContent(message);
    if (role === "tool") {
      const name = message.name || toolNames.get(message.tool_call_id) || "工具输出";
      body.append(toolDetails(name, content || "（无输出）", "查看结果"));
    } else if (role === "system") {
      const detail = element("details", "reasoning-block"); detail.append(element("summary", "", "查看上下文"), element("div", "reasoning-content", content)); body.append(detail);
    } else if (content) {
      const text = element("div", "message-content");
      if (role === "assistant") markdown(text, content); else text.textContent = content;
      body.append(text);
    }
    if (message.display_content !== undefined && content !== plainContent(message.content)) body.append(toolDetails("原始消息", message.content, "查看记录"));
    appendToolCalls(body, message);
    node.append(avatar, body); return node;
  }
  function renderMessages(history) {
    if (!history.nodes) history.nodes = new Map();
    const toolNames = new Map(); history.records.forEach(({ message }) => (message.tool_calls || message.toolcalls || []).forEach((call) => { toolNames.set(call.id, call.function?.name || call.name); }));
    const fragment = document.createDocumentFragment(), currentKeys = new Set();
    let execution = [];
    const flushExecution = () => {
      if (!execution.length) return;
      const key = `execution:${execution[0].index}`, signature = JSON.stringify(execution), cached = history.nodes.get(key);
      currentKeys.add(key);
      if (cached?.signature === signature) { fragment.append(cached.node); execution = []; return; }
      const detail = element("details", "execution-block"), summary = element("summary", "", `查看执行记录 · ${execution.length} 条`);
      detail.open = cached?.node.open || false;
      const records = element("div", "execution-records");
      execution.forEach(({ message }) => records.append(executionRecord(message, toolNames)));
      detail.append(summary, records); history.nodes.set(key, { node: detail, signature }); fragment.append(detail); execution = [];
    };
    history.records.forEach(({ message, index }) => {
      if (!hasNarrative(message)) { execution.push({ message, index }); return; }
      flushExecution();
      const key = message.id || `${index}:${message.role}`, signature = JSON.stringify(message);
      currentKeys.add(key); const cached = history.nodes.get(key);
      let node;
      if (cached?.signature === signature) node = cached.node;
      else { node = buildMessage(message, toolNames); history.nodes.set(key, { node, signature }); }
      fragment.append(node);
    });
    flushExecution();
    for (const key of history.nodes.keys()) if (!currentKeys.has(key)) history.nodes.delete(key);
    $("messages").replaceChildren(fragment);
    $("load-history").hidden = history.nextBefore == null || history.nextBefore <= 0;
    const session = selectedSession();
    $("conversation-start").hidden = history.records.length > 0 || history.total > 0 || history.nextBefore > 0 || session?.total_messages > 0 || Boolean(session?.busy);
  }
  function renderConversationStatus() {
    const session = selectedSession(); if (!session) return;
    setText("conversation-title", session.title || "未命名会话");
    setText("session-state", phaseLabel(session)); $("session-state").className = "state-pill " + statusClass(session);
    showError("session-error", session.error || null);
    setText("status-workspace", workspaceLabel(session));
    $("status-workspace").title = session.workspace_path || "默认会话工作区";
    setText("status-permission", optionLabel(state.meta.policies, session.permission_policy, "policy"));
    setText("status-model", optionLabel(state.meta.models, session.model_profile, "model"));
    setText("composer-model-label", optionLabel(state.meta.models, session.model_profile, "model"));
    setText("composer-permission-label", optionLabel(state.meta.policies, session.permission_policy, "policy"));
    renderArchiveLink();
    setText("file-workspace-label", workspaceLabel(session));
    renderSessionProgress(session);
    renderInspector();
    const confirmation = session.confirmation;
    $("confirmation-card").hidden = !confirmation;
    if (confirmation) setText("confirmation-prompt", plainContent(confirmation.prompt));
    renderComposer();
  }
  function renderSessionProgress(session) {
    const signature = JSON.stringify([session.id, session.context_status, session.plan, state.meta.task_plans_enabled]);
    if (state.renderSignatures.progress === signature) return;
    state.renderSignatures.progress = signature;
    const context = session.context_status || {}, items = [];
    if (Number.isFinite(context.context_capacity_tokens)) items.push(`声明容量 ${context.context_capacity_tokens.toLocaleString()} tokens（本地配置）`);
    if (Number.isFinite(context.sent_tokens)) items.push(`输入估算 ${context.sent_tokens.toLocaleString()} tokens`);
    if (Number.isFinite(context.schema_tokens)) items.push(`工具描述 ${context.schema_tokens.toLocaleString()} tokens`);
    if (Number.isFinite(context.provider_input_tokens)) items.push(`上次供应商输入 ${context.provider_input_tokens.toLocaleString()} tokens`);
    if (Number.isFinite(context.input_limit_tokens)) items.push(`工作上限 ${context.input_limit_tokens.toLocaleString()}，输出预留 ${context.response_reserve_tokens?.toLocaleString() || "0"}`);
    if (Number.isFinite(context.utilization)) items.push(`上下文预算 ${(context.utilization * 100).toFixed(0)}%`);
    else if (Number.isFinite(context.sent_tokens)) items.push(`上下文约 ${context.sent_tokens.toLocaleString()} tokens`);
    if (Number.isFinite(context.archived_chunks)) items.push(`已归档 ${context.archived_chunks} 块`);
    const supervision = { evaluated: "已检查", already_compacted: "相同历史已压缩，无需重复", cooldown: "新增历史尚少，无需重复整理", hard_budget: "额度不足，直接整理", not_due: "未到检查间隔", below_threshold: "占用未达阈值，无需检查", disabled: "未启用", insufficient_history: "历史不足", unavailable: "不可用", unknown: "状态未知" };
    if (context.structured) items.push(`监督：${supervision[context.supervisor] || (context.supervisor_enabled ? "等待检查" : "未启用")}`);
    if (context.summary_kind === "extractive_fallback" || context.summary === "failed_or_over_budget") items.push("摘要：已降级");
    if (context.compact_reason === "high_utilization_and_variance") items.push(`${context.compacted === true ? "本次压缩" : "压缩判定"}：高占用且价值差异过大`);
    else if (context.compact_reason === "context_insufficient") items.push(`${context.compacted === true ? "本次压缩" : "压缩判定"}：上下文额度不足`);
    else if (context.compact_reason === "budget_risk") items.push("压缩判定：预算风险");
    else if (context.compact_reason === "value_variance") items.push("压缩判定：价值差异");
    setText("context-progress", items.join(" · ") || "上下文指标：首次模型调用后更新");
    const plan = session.plan && !["none", "archived"].includes(session.plan.status) ? session.plan : null, counts = plan?.counts || {}, total = Object.values(counts).filter(Number.isFinite).reduce((sum, count) => sum + Math.max(0, count), 0);
    const labels = { completed: "已完成", failed: "失败", cancelled: "已取消", in_progress: "进行中", pending: "待执行", waiting: "等待执行", blocked: "受阻", interrupted: "已中断" };
    setText("plan-progress", plan ? `计划 ${counts.completed || 0}/${total} 步 · ${labels[plan.status] || "待执行"} · 验收${plan.verification === "verified" ? "已验证" : "未验证"}` : "查看任务计划");
    $("plan-progress").hidden = !state.meta.task_plans_enabled && !plan;
  }
  function renderLiveMessage() {
    const session = selectedSession(); if (!session) return;
    const bottom = nearBottom();
    const partial = plainContent(session.partial_display ?? session.partial), reasoning = plainContent(session.partial_reasoning);
    const visible = Boolean(session.busy && !session.confirmation);
    const changed = $("live-message").hidden === visible || $("live-content").textContent !== partial || $("live-reasoning").textContent !== reasoning;
    $("live-message").hidden = !visible;
    setText("live-content", partial); setText("live-reasoning", reasoning); setText("live-phase", phaseLabel(session));
    $("live-reasoning-block").hidden = !reasoning;
    if (visible) $("conversation-start").hidden = true;
    if (changed && bottom) scrollBottom(true);
  }
  function renderComposer() {
    const session = selectedSession(); if (!session) return;
    const pending = state.sending.has(session.id), busy = Boolean(session.busy);
    $("send-message").disabled = pending || busy || !$("message-input").value.trim() || !state.ready;
    $("stop-agent").hidden = !busy; $("send-message").hidden = busy;
    const optionsDisabled = pending || busy || !state.ready || state.quickSaving;
    ["composer-model", "composer-permission", "status-model", "status-permission"].forEach((id) => { $(id).disabled = optionsDisabled; });
    const status = state.connectionState === "offline" ? "连接已断开，正在重试…" : session.confirmation ? "确认操作后，Agent 将继续" : pending ? "正在发送…" : busy ? phaseLabel(session) : "准备就绪";
    setText("composer-status", busy ? `${status} · 可编辑草稿，任务完成后再发送` : status);
    if ($("quick-dialog").open) renderQuickOptions();
  }
  function resizeInput() { const input = $("message-input"); input.style.height = "auto"; input.style.height = `${Math.min(190, Math.max(69, input.scrollHeight))}px`; }
  async function sendMessage(event) {
    event?.preventDefault(); const session = selectedSession(), submittedDraft = $("message-input").value, text = submittedDraft.trim();
    if (!session || !text || session.busy || state.sending.has(session.id)) return;
    const slash = slashCommands.find((command) => text === command.slash);
    if (slash) { $("message-input").value = ""; state.drafts.set(session.id, ""); resizeInput(); $("slash-menu").hidden = true; renderComposer(); slash.run(); return; }
    state.drafts.set(session.id, submittedDraft);
    state.sending.add(session.id); showError("composer-error", null); renderComposer();
    try {
      await jsonApi(sessionPath(session.id, "/messages"), { method: "POST", body: { text } });
      const currentDraft = state.activeId === session.id ? $("message-input").value : state.drafts.get(session.id);
      if (currentDraft === submittedDraft) {
        state.drafts.set(session.id, "");
        if (state.activeId === session.id) { $("message-input").value = ""; resizeInput(); $("slash-menu").hidden = true; }
      }
      const latest = state.sessions.get(session.id);
      if (latest) { latest.busy = true; latest.phase = "thinking"; latest.partial = ""; latest.partial_reasoning = ""; }
      if (state.activeId === session.id) { renderConversationStatus(); renderLiveMessage(); }
      await loadConversation(session.id, { quiet: true }); scrollBottom(state.activeId === session.id);
    } catch (error) {
      if (state.activeId === session.id) showError("composer-error", error); else toast(errorText(error), "error");
    } finally { state.sending.delete(session.id); renderComposer(); }
  }
  async function stopSession(id = state.activeId) {
    if (!id) return;
    try { await jsonApi(sessionPath(id, "/stop"), { method: "POST", body: {} }); toast("已请求停止 Agent。"); await refreshSessions(); }
    catch (error) { toast(errorText(error), "error"); }
  }
  async function answerConfirmation(allowed) {
    const session = selectedSession(), confirmation = session?.confirmation; if (!confirmation) return;
    const id = session.id, confirmationId = confirmation.id;
    $("confirmation-allow").disabled = true; $("confirmation-deny").disabled = true;
    try {
      await jsonApi(sessionPath(id, "/confirmation"), { method: "POST", body: { confirmation_id: confirmationId, allowed } });
      const current = state.sessions.get(id); if (current?.confirmation?.id === confirmationId) current.confirmation = null;
      if (state.activeId === id) renderConversationStatus();
      await refreshSessions();
    } catch (error) { toast(errorText(error), "error"); }
    finally { $("confirmation-allow").disabled = false; $("confirmation-deny").disabled = false; }
  }

  function updateNewMode() {
    const flow = $("new-mode").value === "flow";
    $("new-flow-options").hidden = !flow;
    setText("new-dialog-title", flow ? "新建 Flow 项目" : "新建 Agent 项目");
    setText("new-submit", flow ? "创建 Flow 项目" : "创建 Agent 项目");
    setText("new-project-hint", flow ? "创建后也可在画布或检查器中选择目录；运行前需要已有项目。" : "创建后也可在会话设置中浏览选择目录。子 Agent 自动共享父项目文件，继承工具与权限，保留独立上下文。");
  }
  function openNew() { $("new-mode").value = "agent"; updateNewMode(); $("new-flow-handoff").value = ""; $("new-flow-policy").value = ""; $("new-title").value = ""; $("new-project-path").value = ""; $("new-execution-environment").value = ""; showError("new-error", null); $("new-dialog").showModal(); $("new-title").focus(); }
  async function createSession(event) {
    event.preventDefault(); $("new-submit").disabled = true; showError("new-error", null);
    try {
      if ($("new-mode").value === "flow") {
        const flow = await jsonApi("/api/flows", { method: "POST", body: { title: $("new-title").value.trim() || "新建Flow", workspace_path: $("new-project-path").value.trim() || null, execution_environment: $("new-execution-environment").value || null, permission_policy: $("new-flow-policy").value || null, handoff: $("new-flow-handoff").value } });
        $("new-dialog").close(); window.FlowUI.openCreated(flow); return;
      }
      const session = await jsonApi("/api/sessions", { method: "POST", body: { title: $("new-title").value.trim() || "新会话", workspace_path: $("new-project-path").value.trim() || null, execution_environment: $("new-execution-environment").value || null, permission_policy: $("new-flow-policy").value || null } });
      state.sessions.set(session.id, session); $("new-dialog").close(); selectSession(session.id); renderRuntime(); focusInput();
    } catch (error) { showError("new-error", error); }
    finally { $("new-submit").disabled = false; }
  }
  async function renameSession() {
    const session = selectedSession(); if (!session) return;
    const title = window.prompt("新的会话名称（最多 120 字）", session.title || ""); if (title == null || !title.trim() || title.trim() === session.title) return;
    if (title.trim().length > 120) { toast("会话名称最多 120 字。", "error"); return; }
    try {
      const updated = await jsonApi(sessionPath(session.id), { method: "PATCH", body: { title: title.trim() } });
      state.sessions.set(session.id, { ...session, ...updated }); renderSessions(); renderTabs(); renderConversationStatus();
    } catch (error) { toast(errorText(error), "error"); }
  }
  async function deleteSession() {
    const session = selectedSession(); if (!session) return;
    const ids = descendantIds(session.id);
    if (ids.some((id) => state.sessions.get(id)?.busy || state.sending.has(id)) || (state.runtime.active_tools || []).some((tool) => ids.includes(tool.session_id))) { toast("此会话或子 Agent 正在运行。请先停止并等待任务及工具结束，再删除。", "error"); return; }
    if (ids.some((id) => state.sessions.get(id)?.confirmation)) { toast("此会话或子 Agent 有待确认操作，请先处理后再删除。", "error"); return; }
    if (!window.confirm(`删除会话「${session.title || "未命名会话"}」及其 ${ids.length - 1} 个子会话？\n\n共 ${ids.length} 个会话将从工作台中移除并归档。\n工作区文件会保留。\n请确认删除这些会话。`)) return;
    $("delete-session").disabled = true;
    try {
      const result = await jsonApi(sessionPath(session.id), { method: "DELETE", body: { confirm: true } });
      const deleted = Array.isArray(result.deleted_ids) ? result.deleted_ids : ids;
      removeDeletedSessions(deleted); toast(`已删除并归档 ${deleted.length} 个会话。`); await refreshSessions(); refreshTasks(true);
    } catch (error) { toast(errorText(error), "error"); }
    finally { $("delete-session").disabled = false; }
  }
  function descendantIds(id) {
    const ids = new Set([id]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const session of state.sessions.values()) if (ids.has(session.parent_id) && !ids.has(session.id)) { ids.add(session.id); changed = true; }
    }
    return [...ids];
  }
  function removeDeletedSessions(ids) {
    const deleted = new Set(ids), activeDeleted = deleted.has(state.activeId);
    for (const id of deleted) {
      state.deletedIds.add(id); state.sessions.delete(id); state.history.delete(id); state.drafts.delete(id);
      state.loading.delete(id); state.pendingHistory.delete(id); state.sending.delete(id);
    }
    state.snapshotVersion++; state.openTabs = state.openTabs.filter((id) => !deleted.has(id));
    if (deleted.has(sessionStorage.getItem("agent-workspace-session"))) sessionStorage.removeItem("agent-workspace-session");
    for (const [key, dialog] of [["settingsId", "settings-dialog"], ["memoryId", "memory-dialog"]]) if (deleted.has(state[key])) { state[key] = null; if ($(dialog).open) $(dialog).close(); }
    const belongsToDeleted = (task) => [task.session_id, task.owner, task.owner_session_id, task.parent_session_id, task.child_id].some((id) => deleted.has(id));
    state.tasks = state.tasks.filter((task) => !belongsToDeleted(task)); state.commands = state.commands.filter((task) => !belongsToDeleted(task)); state.tasksRevision++;
    if (Array.isArray(state.runtime.active_tools)) state.runtime.active_tools = state.runtime.active_tools.filter((tool) => !deleted.has(tool.session_id));
    if (activeDeleted) {
      state.activeId = null; $("message-input").value = ""; $("messages").replaceChildren(); $("live-message").hidden = true;
      const fallback = state.openTabs.at(-1) || state.sessions.keys().next().value;
      if (fallback) selectSession(fallback); else showWelcome();
    }
    renderSessions(); renderTabs(); renderRuntime(); if (state.monitorOpen) renderMonitor();
  }
  function maintenanceBusy() {
    return Boolean(state.maintenance?.busy || state.sending.size || [...state.sessions.values()].some((session) => session.busy || session.confirmation) || state.runtime.active_tools?.length);
  }
  function cleanupSelection() {
    return { cache: $("cleanup-cache").checked, logs: $("cleanup-logs").checked, sessions: $("cleanup-sessions").checked };
  }
  function cleanupSummary(selection = cleanupSelection()) {
    const data = state.maintenance || {}, parts = [];
    if (selection.cache) parts.push(`${data.cache?.files || 0} 个缓存文件（${formatBytes(data.cache?.bytes || 0)}）`);
    if (selection.logs) parts.push(`${data.logs?.files || 0} 个日志文件（${formatBytes(data.logs?.bytes || 0)}）`);
    if (selection.sessions) parts.push(`${data.sessions?.count || 0} 个会话记录（含子会话）`);
    return parts.join("、");
  }
  function renderMaintenance() {
    const data = state.maintenance, loading = state.maintenanceLoading, busy = maintenanceBusy(), pending = state.cleanupPending;
    for (const name of ["cache", "logs", "sessions"]) {
      const input = $("cleanup-" + name), unavailable = Boolean(data?.[name]?.error);
      input.disabled = unavailable || loading || pending;
      if (unavailable) input.checked = false;
      input.title = data?.[name]?.error || "";
    }
    setText("cleanup-cache-count", data ? `${data.cache?.files || 0} 个文件 · ${formatBytes(data.cache?.bytes || 0)}` : loading ? "正在读取…" : "未读取");
    setText("cleanup-logs-count", data ? `${data.logs?.files || 0} 个文件 · ${formatBytes(data.logs?.bytes || 0)}` : loading ? "正在读取…" : "未读取");
    setText("cleanup-sessions-count", data ? `${data.sessions?.count || 0} 个会话` : loading ? "正在读取…" : "未读取");
    for (const name of ["cache", "logs", "sessions"]) {
      if (data?.[name]?.error) setText("cleanup-" + name + "-count", "不可清理：" + data[name].error);
    }
    const selected = cleanupSelection(), summary = cleanupSummary(selected);
    const count = (selected.cache ? data?.cache?.files || 0 : 0) + (selected.logs ? data?.logs?.files || 0 : 0) + (selected.sessions ? data?.sessions?.count || 0 : 0);
    $("cleanup-options").disabled = !data || loading || pending;
    $("cleanup-busy").hidden = !busy;
    $("cleanup-confirm").disabled = !data || loading || pending || busy || !count;
    if (busy || loading || !count) $("cleanup-confirm").checked = false;
    setText("cleanup-preview", pending ? "正在清理选中的数据…" : loading ? "正在读取可清理的数据…" : !data ? "读取数量后可选择清理范围。" : summary ? `将清理：${summary}。` : "请选择至少一项清理范围。");
    setText("cleanup-confirm-label", summary ? `确认清理 ${summary}；缓存和旧日志不可恢复，会话移入回收区。` : "确认清理选中的数据；缓存和旧日志不可恢复，会话移入回收区。");
    $("cleanup-submit").disabled = !data || loading || pending || busy || !count || !$("cleanup-confirm").checked;
    setText("cleanup-submit", pending ? "正在清理…" : "确认清理");
    $("refresh-maintenance").disabled = loading || pending;
    $("maintenance-dialog").querySelectorAll(".dialog-close").forEach((button) => { button.disabled = pending; });
  }
  async function openMaintenance() {
    if (!state.ready) return;
    for (const id of ["cleanup-cache", "cleanup-logs", "cleanup-sessions", "cleanup-confirm"]) $(id).checked = false;
    state.maintenance = null; showError("maintenance-error", null);
    $("maintenance-dialog").showModal(); await refreshMaintenance();
  }
  async function refreshMaintenance() {
    if (state.cleanupPending) return;
    const generation = ++state.maintenanceGeneration;
    state.maintenanceLoading = true; $("cleanup-confirm").checked = false; showError("maintenance-error", null); renderMaintenance();
    try {
      const data = await jsonApi("/api/maintenance");
      if (generation === state.maintenanceGeneration) state.maintenance = data;
    } catch (error) { if (generation === state.maintenanceGeneration) { state.maintenance = null; showError("maintenance-error", error); } }
    finally { if (generation === state.maintenanceGeneration) { state.maintenanceLoading = false; renderMaintenance(); } }
  }
  async function cleanupData(event) {
    event.preventDefault(); renderMaintenance();
    if ($("cleanup-submit").disabled) return;
    const selection = cleanupSelection();
    state.cleanupPending = true; showError("maintenance-error", null); renderMaintenance();
    try {
      const result = await jsonApi("/api/maintenance/cleanup", { method: "POST", body: { ...selection, confirm: true } });
      if (selection.sessions) removeDeletedSessions(result.sessions?.deleted_ids || []);
      $("maintenance-dialog").close(); toast("所选数据已清理。"); await refreshSessions(); refreshTasks(true);
    } catch (error) {
      state.maintenance = null; $("cleanup-confirm").checked = false;
      showError("maintenance-error", error);
    } finally { state.cleanupPending = false; renderMaintenance(); }
  }
  function fillSelect(id, items, value, kind) {
    const node = $(id); node.replaceChildren();
    if (kind !== "policy") {
      const defaultOption = element("option", "", state.defaultModel ? `默认 · ${state.defaultModel}` : "继承默认模型"); defaultOption.value = ""; node.append(defaultOption);
    }
    const options = normalizeOptions(items, kind);
    if (value && !options.some((item) => item.value === value)) options.push({ value, label: optionLabel(items, value, kind) });
    options.forEach((item) => { const option = element("option", "", item.label); option.value = item.value; node.append(option); });
    node.value = value || "";
  }
  function renderArchiveLink() {
    const debugLink = $("debug-link");
    debugLink.hidden = !state.meta.debug_enabled || !state.activeId;
    debugLink.href = state.activeId ? `/debug?${new URLSearchParams({ session: state.activeId })}` : "/debug";
    const link = $("open-context-archive");
    link.hidden = !state.meta.debug_enabled || !state.activeId;
    link.href = state.activeId ? `/debug?${new URLSearchParams({ session: state.activeId, context: "1" })}` : "/debug";
  }
  function renderQuickOptions() {
    const session = state.sessions.get(state.quickId), kind = state.quickKind;
    if (!session || !["model", "policy"].includes(kind)) return;
    const disabled = Boolean(session.busy || state.sending.has(session.id) || state.quickSaving);
    const options = [...(kind === "model" ? [{ value: "", label: optionLabel([], null, kind), description: "继承服务默认配置" }] : []), ...normalizeOptions(kind === "model" ? state.meta.models : state.meta.policies, kind)];
    const selected = kind === "model" ? session.model_profile : session.permission_policy;
    const signature = JSON.stringify([session.id, kind, selected, disabled, options]);
    if (state.renderSignatures.quick === signature) return;
    state.renderSignatures.quick = signature;
    setText("quick-hint", session.busy ? "任务运行中；完成后可以修改。" : state.quickSaving ? "正在保存…" : "选择后立即用于此会话。以下选项由本地服务提供。");
    const choices = options.map((option) => {
      const button = element("button", "quick-option"); button.type = "button"; button.disabled = disabled;
      button.setAttribute("aria-pressed", String((selected || "") === option.value));
      const text = element("span", "quick-option-text"); text.append(element("strong", "", option.label));
      if (option.description) text.append(element("small", "", option.description));
      button.append(text, element("span", "quick-option-check", (selected || "") === option.value ? "✓" : ""));
      button.addEventListener("click", () => saveQuickSetting(option.value)); return button;
    });
    $("quick-options").replaceChildren(...choices);
  }
  function openQuickPicker(kind) {
    const session = selectedSession();
    if (!session || session.busy || state.sending.has(session.id) || state.quickSaving || !state.ready) return;
    state.quickId = session.id; state.quickKind = kind; state.renderSignatures.quick = null;
    showError("quick-error", null); setText("quick-title", kind === "model" ? "选择模型" : "操作权限");
    renderQuickOptions(); $("quick-dialog").showModal();
  }
  async function saveQuickSetting(value) {
    const id = state.quickId, kind = state.quickKind, session = state.sessions.get(id);
    if (!session || session.busy || state.sending?.has(id) || state.quickSaving || !["model", "policy"].includes(kind)) return;
    state.quickSaving = true; showError("quick-error", null); renderQuickOptions();
    try {
      const field = kind === "model" ? "model_profile" : "permission_policy";
      const updated = await jsonApi(sessionPath(id), { method: "PATCH", body: { [field]: value || null } });
      if (state.sessions.has(id)) state.sessions.set(id, { ...state.sessions.get(id), [field]: updated[field] ?? (value || null) });
      if (state.quickId === id && state.quickKind === kind && $("quick-dialog").open) $("quick-dialog").close();
      renderSessions(); renderTabs();
    } catch (error) {
      if (state.quickId === id && state.quickKind === kind && $("quick-dialog").open) showError("quick-error", error);
    } finally {
      state.quickSaving = false; renderQuickOptions();
      if (state.activeId) renderConversationStatus();
    }
  }
  function handleWorkspaceShortcut(event) {
    if (event.isComposing || event.keyCode === 229 || event.altKey || !(event.ctrlKey || event.metaKey) || !state.ready || state.cleanupPending) return;
    const key = event.key.toLowerCase();
    if (key === "o" && event.shiftKey) {
      event.preventDefault(); document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); openNew();
    } else if (key === "l" && !event.shiftKey) {
      event.preventDefault(); document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); focusInput();
    } else if (["k", "p"].includes(key)) {
      event.preventDefault(); if (!$("command-dialog").open) { document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); openCommand(); }
    }
  }
  function policyDescription() {
    const option = normalizeOptions(state.meta.policies, "policy").find((item) => item.value === $("settings-policy").value);
    const descriptions = { readonly: "允许读取与安全查询；阻止写入及其他有副作用的操作。", smart: "已验证的低风险操作可自动批准；高风险或未知操作需要申请。旧会话的保守确认规则继续有效。", full_access: "工具调用自动批准，遵循所选执行环境及路径、审计检查；删除会话仍需确认。" };
    setText("settings-policy-description", option?.description || descriptions[$("settings-policy").value] || "使用服务的默认权限策略；需要确认的操作会在对话中显示。");
  }
  async function openSettings() {
    const session = selectedSession(); if (!session) { toast("请先选择或创建会话。"); return; }
    state.settingsId = session.id; showError("settings-error", null);
    $("settings-title").value = session.title || ""; fillSelect("settings-policy", state.meta.policies, session.permission_policy, "policy"); fillSelect("settings-model", state.meta.models, session.model_profile, "model");
    $("settings-project-path").value = session.workspace_custom ? session.workspace_path || "" : "";
    $("settings-execution-environment").value = session.execution_environment || "";
    const workspaceLocked = session.workspace_can_change === false || Boolean(session.parent_id) || session.busy;
    ["settings-project-path", "browse-workspace", "reset-workspace"].forEach((id) => { $(id).disabled = workspaceLocked; });
    $("settings-execution-environment").disabled = workspaceLocked;
    $("workspace-picker").hidden = true; state.workspaceBrowseGeneration = (state.workspaceBrowseGeneration || 0) + 1;
    setText("settings-project-path-hint", session.workspace_error || (session.parent_id ? "工作区由父 Agent 的会话设置管理。" : workspaceLocked ? "请先停止此项目的 Agent 和后台任务，再切换目录。" : `当前目录：${session.workspace_path || "默认会话目录"}。可输入已有 Linux 绝对路径或浏览选择。`));
    policyDescription();
    setText("settings-busy-hint", session.busy ? "运行中的设置修改可能被服务拒绝。" : "");
    $("settings-dialog").showModal();
  }
  async function browseWorkspace(path) {
    const owner = state.settingsId, generation = state.workspaceBrowseGeneration = (state.workspaceBrowseGeneration || 0) + 1;
    $("workspace-picker").hidden = false; $("workspace-select").disabled = true; $("workspace-parent").disabled = true;
    showError("workspace-picker-error", null); $("workspace-directories").replaceChildren(element("p", "field-hint", "正在读取目录…"));
    try {
      const data = await jsonApi("/api/workspaces/directories" + (path ? `?path=${encodeURIComponent(path)}` : ""));
      if (generation !== state.workspaceBrowseGeneration || state.settingsId !== owner || !$("settings-dialog").open) return;
      state.workspaceBrowsePath = data.path; state.workspaceBrowseParent = data.parent_path;
      setText("workspace-current", data.path); $("workspace-parent").disabled = !data.parent_path;
      $("workspace-select").disabled = !data.selectable;
      showError("workspace-picker-error", data.error || null);
      const fragment = document.createDocumentFragment();
      (data.directories || []).forEach((entry) => {
        const button = element("button", "workspace-directory", entry.name); button.type = "button"; button.title = entry.path;
        button.addEventListener("click", () => browseWorkspace(entry.path)); fragment.append(button);
      });
      if (!data.directories?.length) fragment.append(element("p", "field-hint", "没有可浏览的子目录。"));
      if (data.truncated) fragment.append(element("p", "field-hint", "目录较多，仅显示部分；也可直接输入完整路径。"));
      $("workspace-directories").replaceChildren(fragment);
    } catch (error) {
      if (generation === state.workspaceBrowseGeneration && state.settingsId === owner) {
        $("workspace-directories").replaceChildren(); showError("workspace-picker-error", error);
      }
    }
  }
  function resetModelForm() {
    state.editingModel = null; $("model-form").reset();
    $("model-api-key").value = "";
    setText("model-form-title", "添加模型"); setText("model-key-hint", "本地服务无需密钥时可留空；编辑时留空保留现有密钥。");
    showError("models-error", null);
  }
  async function loadModels() {
    const data = await jsonApi("/api/models");
    state.defaultModel = typeof data.default_model === "string" ? data.default_model : null;
    // The public library intentionally contains no API key values.
    state.modelLibrary = (data.models || []).map((item) => ({ id: item.id, name: item.name, model: item.model, base_url: item.base_url, context_tokens: item.context_tokens, response_reserve_tokens: item.response_reserve_tokens, has_api_key: Boolean(item.has_api_key), editable: !item.read_only && item.editable !== false && !item.builtin && !item.built_in, source: item.source }));
    renderModels();
  }
  async function openModels() {
    resetModelForm(); $("models-dialog").showModal();
    $("model-list").replaceChildren(element("p", "field-hint", "正在读取模型…"));
    try { await loadModels(); } catch (error) { showError("models-error", error); }
  }
  function renderModels() {
    const fragment = document.createDocumentFragment();
    state.modelLibrary.forEach((model) => {
      const row = element("article", "model-entry"), info = element("div", "model-entry-info"), actions = element("div", "model-entry-actions");
      info.append(element("strong", "", model.name || model.id), element("span", "", model.model), element("small", "", model.has_api_key ? "已配置密钥" : "未配置密钥"));
      const select = element("button", "text-button", "选择"); select.type = "button";
      select.addEventListener("click", () => {
        // Saving the surrounding session form applies the selection.
        fillSelect("settings-model", state.meta.models, model.id, "model");
        $("models-dialog").close(); toast("已选择模型，保存会话设置后生效。");
      }); actions.append(select);
      if (model.editable) {
        const edit = element("button", "text-button", "编辑"); edit.type = "button";
        edit.addEventListener("click", () => { resetModelForm(); state.editingModel = model.id; $("model-name").value = model.name || ""; $("model-id").value = model.model || ""; $("model-base-url").value = model.base_url || ""; $("model-context-tokens").value = model.context_tokens ?? ""; $("model-response-reserve").value = model.response_reserve_tokens ?? ""; setText("model-form-title", "编辑模型"); setText("model-key-hint", "留空保留现有密钥；修改服务地址须重新输入对应密钥。"); $("model-name").focus(); });
        const remove = element("button", "text-button danger-hover", "删除"); remove.type = "button"; remove.addEventListener("click", () => deleteModel(model, remove));
        actions.append(edit, remove);
      }
      row.append(info, actions); fragment.append(row);
    });
    if (!state.modelLibrary.length) fragment.append(element("p", "field-hint", "还没有保存的模型。添加连接后即可选择。"));
    $("model-list").replaceChildren(fragment);
  }
  async function refreshModelOptions(selected) {
    const meta = await jsonApi("/api/meta"); state.meta = meta;
    fillSelect("settings-model", state.meta.models, selected ?? $("settings-model").value, "model");
    if (state.activeId) renderConversationStatus();
  }
  async function saveModel(event) {
    event.preventDefault(); if (state.modelSaving) return;
    state.modelSaving = true; $("model-submit").disabled = true; $("model-reset").disabled = true; showError("models-error", null);
    try {
      const id = state.editingModel;
      const body = { name: $("model-name").value.trim(), model: $("model-id").value.trim(), base_url: $("model-base-url").value.trim(), api_key: $("model-api-key").value,
        context_tokens: $("model-context-tokens").value ? Number($("model-context-tokens").value) : null,
        response_reserve_tokens: $("model-response-reserve").value ? Number($("model-response-reserve").value) : null };
      const saved = await jsonApi(id ? `/api/models/${encodeURIComponent(id)}` : "/api/models", { method: id ? "PUT" : "POST", body });
      $("model-api-key").value = "";
      await loadModels(); await refreshModelOptions(saved.id); resetModelForm(); toast("模型已保存，可在会话中选择。");
    } catch (error) { showError("models-error", error); }
    finally { $("model-api-key").value = ""; state.modelSaving = false; $("model-submit").disabled = false; $("model-reset").disabled = false; }
  }
  async function deleteModel(model, button) {
    if (!window.confirm(`删除模型「${model.name || model.id}」？`)) return;
    button.disabled = true; showError("models-error", null);
    try { await jsonApi(`/api/models/${encodeURIComponent(model.id)}`, { method: "DELETE", body: {} }); await loadModels(); await refreshModelOptions(); if (state.editingModel === model.id) resetModelForm(); toast("模型已删除。"); }
    catch (error) { showError("models-error", error); button.disabled = false; }
  }
  async function saveSettings(event) {
    event.preventDefault(); const id = state.settingsId; if (!id) return;
    $("settings-submit").disabled = true; showError("settings-error", null);
    try {
      const body = { title: $("settings-title").value.trim() || "未命名会话", model_profile: $("settings-model").value || null };
      // 修改标题/模型不应隐式升级旧会话的保守确认规则。
      if ($("settings-policy").value !== state.sessions.get(state.settingsId)?.permission_policy) body.permission_policy = $("settings-policy").value || null;
      const current = state.sessions.get(id), workspace = $("settings-project-path").value.trim();
      if (!$("settings-execution-environment").disabled && $("settings-execution-environment").value !== (current?.execution_environment || "")) body.execution_environment = $("settings-execution-environment").value || null;
      if (!$("settings-project-path").disabled && workspace !== (current?.workspace_custom ? current.workspace_path || "" : "")) body.workspace_path = workspace || null;
      const updated = await jsonApi(sessionPath(id), { method: "PATCH", body });
      state.sessions.set(id, { ...state.sessions.get(id), ...updated }); $("settings-dialog").close(); renderSessions(); renderTabs();
      if (state.activeId === id) { renderConversationStatus(); if (state.sidebarView === "files") loadFiles(""); }
      toast("会话设置已保存。");
    } catch (error) { showError("settings-error", error); }
    finally { $("settings-submit").disabled = false; }
  }
  async function openMemory() {
    const session = selectedSession(); if (!session) return;
    state.memoryId = session.id; $("memory-content").value = ""; $("memory-content").disabled = true; $("memory-submit").disabled = true; showError("memory-error", null); $("memory-dialog").showModal();
    try {
      const data = await jsonApi(sessionPath(session.id, "/memory"));
      if (state.memoryId === session.id) { $("memory-content").value = data.content || ""; $("memory-content").disabled = false; $("memory-submit").disabled = false; }
    } catch (error) { showError("memory-error", error); }
  }
  async function saveMemory(event) {
    event.preventDefault(); const id = state.memoryId; if (!id) return;
    if (!window.confirm("保存将替换此会话的全部记忆内容。确认保存修改？")) return;
    $("memory-submit").disabled = true; showError("memory-error", null);
    try {
      const data = await jsonApi(sessionPath(id, "/memory"), { method: "PUT", body: { content: $("memory-content").value, confirm: true } });
      $("memory-content").value = data.content || ""; $("memory-dialog").close(); toast("会话记忆已保存。");
    } catch (error) { showError("memory-error", error); }
    finally { $("memory-submit").disabled = false; }
  }
  async function exportSession(format) {
    const session = selectedSession(); if (!session) return;
    try {
      const response = await api(sessionPath(session.id, `/export?format=${encodeURIComponent(format)}`));
      const blob = await response.blob(), url = URL.createObjectURL(blob), link = element("a");
      link.href = url; link.download = `${(session.title || "session").replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").slice(0, 100)}.${format === "json" ? "json" : "md"}`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      $("export-dialog").close(); toast("会话已导出。");
    } catch (error) { toast(errorText(error), "error"); }
  }

  function switchSidebar(view) {
    state.sidebarView = view; $("sessions-view").hidden = view !== "sessions"; $("files-view").hidden = view !== "files";
    document.querySelectorAll("[data-view]").forEach((button) => { button.classList.toggle("active", button.dataset.view === view); button.setAttribute("aria-pressed", String(button.dataset.view === view)); });
    if (window.innerWidth <= 760) $("sidebar").classList.add("mobile-open");
    if (view === "files") loadFiles(state.filePath);
  }
  async function loadFiles(path = "") {
    const id = state.activeId, generation = ++state.filesGeneration; showError("files-error", null);
    if (!id) { $("file-list").replaceChildren(element("div", "monitor-empty", "选择一个会话以查看工作区文件。")); return; }
    $("file-list").replaceChildren(element("div", "monitor-empty", "正在读取文件…"));
    try {
      const data = await jsonApi(sessionPath(id, `/files?${new URLSearchParams({ path })}`));
      if (generation !== state.filesGeneration || id !== state.activeId) return;
      state.filePath = data.path || ""; setText("file-path", state.filePath ? `/${state.filePath}` : "/"); $("file-up").disabled = !state.filePath;
      const entries = [...(data.entries || [])].sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name, "zh-CN"));
      const fragment = document.createDocumentFragment();
      entries.forEach((entry) => {
        const button = element("button", "file-row"); button.type = "button"; button.title = entry.path;
        button.append(element("span", "", entry.is_dir ? "▱" : "▤"), element("span", "", entry.name), element("span", "", entry.is_dir ? "›" : formatBytes(entry.size)));
        button.addEventListener("click", () => entry.is_dir ? loadFiles(entry.path) : previewFile(entry.path)); fragment.append(button);
      });
      if (!entries.length) fragment.append(element("div", "monitor-empty", "此目录为空。"));
      $("file-list").replaceChildren(fragment);
    } catch (error) {
      if (generation === state.filesGeneration && id === state.activeId) { showError("files-error", error); $("file-list").replaceChildren(); }
    }
  }
  async function previewFile(path) {
    const id = state.activeId; if (!id) return;
    setText("preview-title", path.split("/").at(-1) || "文件预览"); setText("preview-path", path); setText("preview-content", "正在读取文件…"); $("preview-truncated").hidden = true; $("copy-file").disabled = true; $("file-dialog").showModal();
    try {
      const data = await jsonApi(sessionPath(id, `/file?${new URLSearchParams({ path })}`));
      setText("preview-path", data.path || path); setText("preview-content", data.content || "（空文件）"); $("preview-truncated").hidden = !data.truncated; $("copy-file").disabled = false;
    } catch (error) { setText("preview-content", errorText(error)); toast(errorText(error), "error"); }
  }
  async function copyText(value) {
    try { await navigator.clipboard.writeText(value); toast("内容已复制。"); }
    catch { toast("浏览器未允许复制，请选中文本后复制。", "error"); }
  }

  function renderRuntime() {
    const active = state.runtime.active_agents ?? [...state.sessions.values()].filter((s) => s.busy).length;
    const activeCount = Array.isArray(active) ? active.length : Number(active) || 0;
    const tools = Array.isArray(state.runtime.active_tools) ? state.runtime.active_tools : [];
    setText("status-agents", `${activeCount} Agent 运行中`); setText("agents-count", state.sessions.size); setText("tools-count", tools.length); setText("tasks-count", state.tasks.length + state.commands.length);
    $("activity-badge").hidden = !activeCount; setText("activity-badge", activeCount);
    const calls = state.runtime.active_model_calls ?? 0;
    setText("runtime-summary", `${activeCount} Agent · ${Array.isArray(calls) ? calls.length : calls} 模型调用 · ${tools.length} 工具`);
    if (!state.activeId) { setText("status-permission", "默认权限"); setText("status-model", "默认模型"); }
    renderInspector();
  }
  function toggleMonitor(force) {
    const mobile = window.matchMedia("(max-width: 979px)").matches;
    const next = force ?? (mobile ? !$("inspector").classList.contains("mobile-open") : !state.monitorOpen);
    if (mobile) $("inspector").classList.toggle("mobile-open", next);
    state.monitorOpen = next; $("monitor").hidden = !state.monitorOpen; $("activity-monitor").classList.toggle("active", state.monitorOpen);
    if (state.monitorOpen) { renderMonitor(); refreshTasks(true); }
    applyLayout();
  }
  function monitorTable(headers, widths) {
    const table = element("table", "monitor-table"), head = element("thead"), row = element("tr"), body = element("tbody");
    table.dataset.kind = state.monitorTab;
    headers.forEach((header, index) => { const cell = element("th", "", header); if (widths[index]) cell.style.width = widths[index]; row.append(cell); });
    head.append(row); table.append(head, body); return { table, body };
  }
  function monitorCell(row, content, className) { const cell = element("td", className); if (content instanceof Node) cell.append(content); else cell.textContent = String(content ?? "—"); row.append(cell); return cell; }
  function renderMonitor() {
    const container = $("monitor-content"), fragment = document.createDocumentFragment();
    const signature = JSON.stringify([state.monitorTab, state.monitorTab === "agents" ? [...state.sessions.values()].map((s) => [s.id, s.title, s.phase, s.status, s.busy, Boolean(s.confirmation), s.workspace_mode, s.parent_id]) : state.monitorTab === "tools" ? (state.runtime.active_tools || []).map((t) => [t.session_id, t.name, t.tool_call_id, t.concurrency, elapsed(t.started_at)]) : state.tasksRevision]);
    if (state.renderSignatures.monitor === signature) return;
    state.renderSignatures.monitor = signature;
    if (state.monitorTab === "agents") {
      if (!state.sessions.size) { container.replaceChildren(element("div", "monitor-empty", "创建会话后，Agent 及其子任务会显示在这里。")); return; }
      const { table, body } = monitorTable(["AGENT / 会话", "状态", "工作区", "父 Agent"], ["40%", "20%", "20%", "20%"]);
      const sessions = [...state.sessions.values()], ordered = [], visited = new Set();
      const visit = (session, depth) => { if (visited.has(session.id)) return; visited.add(session.id); ordered.push([session, depth]); sessions.filter((child) => child.parent_id === session.id).forEach((child) => visit(child, Math.min(5, depth + 1))); };
      sessions.filter((s) => !s.parent_id || !state.sessions.has(s.parent_id)).forEach((s) => visit(s, 0)); sessions.forEach((s) => visit(s, 0));
      ordered.forEach(([session, depth]) => {
        const row = element("tr"), button = element("button", "monitor-name"); button.type = "button"; button.style.paddingLeft = `${depth * 13}px`;
        button.append(element("span", "small-status-dot " + statusClass(session)), element("span", "", `${depth ? "↳ " : ""}${session.title || "未命名会话"}`)); button.addEventListener("click", () => selectSession(session.id));
        monitorCell(row, button); monitorCell(row, phaseLabel(session)); monitorCell(row, workspaceLabel(session), "monitor-muted"); monitorCell(row, state.sessions.get(session.parent_id)?.title || session.parent_id || "—", "monitor-muted"); body.append(row);
      }); fragment.append(table);
    } else if (state.monitorTab === "tools") {
      const tools = Array.isArray(state.runtime.active_tools) ? state.runtime.active_tools : [];
      if (!tools.length) { container.replaceChildren(element("div", "monitor-empty", "当前没有工具运行。工具执行时将在此显示。")); return; }
      const { table, body } = monitorTable(["工具", "所属会话", "并发方式", "已运行"], ["30%", "35%", "20%", "15%"]);
      tools.forEach((tool) => {
        const row = element("tr"), name = element("span", "", tool.name || "工具"); name.title = tool.tool_call_id || "";
        monitorCell(row, name); monitorCell(row, state.sessions.get(tool.session_id)?.title || tool.session_id, "monitor-muted"); monitorCell(row, typeof tool.concurrency === "object" ? JSON.stringify(tool.concurrency) : tool.concurrency || "—", "monitor-muted"); monitorCell(row, elapsed(tool.started_at), "monitor-muted"); body.append(row);
      }); fragment.append(table);
    } else {
      const tasks = [...state.tasks.map((task) => ({ ...task, type: "Agent 任务" })), ...state.commands.map((command) => ({ ...command, type: "命令" }))];
      if (!tasks.length) { container.replaceChildren(element("div", "monitor-empty", "当前没有后台任务或命令。")); return; }
      const { table, body } = monitorTable(["任务 / 命令", "类型", "状态", "所属会话", ""], ["36%", "14%", "16%", "25%", "9%"]);
      tasks.forEach((task) => {
        const row = element("tr"), status = task.status || task.state || "pending", id = task.id || task.task_id || task.job_id;
        const name = task.title || task.name || task.description || task.task || task.prompt || task.command || id || "任务";
        let displayName = plainContent(name);
        if (task.child_id && state.sessions.has(task.child_id)) { displayName = element("button", "monitor-name", plainContent(name)); displayName.type = "button"; displayName.addEventListener("click", () => selectSession(task.child_id)); }
        const cell = monitorCell(row, displayName); cell.title = plainContent(name);
        monitorCell(row, task.type, "monitor-muted"); monitorCell(row, phaseLabels[status] || status);
        const owner = task.session_id || task.owner || task.owner_session_id || task.parent_session_id;
        monitorCell(row, state.sessions.get(owner)?.title || owner || "—", "monitor-muted");
        if (task.type === "Agent 任务" && id && !["completed", "done", "failed", "error", "cancelled", "canceled", "stopped"].includes(status)) {
          const button = element("button", "task-cancel", "取消"); button.type = "button"; button.addEventListener("click", () => cancelTask(id, button)); monitorCell(row, button);
        } else monitorCell(row, ""); body.append(row);
      }); fragment.append(table);
    }
    container.replaceChildren(fragment);
  }
  async function refreshTasks(quiet = false) {
    if (!state.ready) return;
    try { const data = await jsonApi("/api/tasks"); state.tasks = Array.isArray(data.tasks) ? data.tasks : []; state.commands = Array.isArray(data.commands) ? data.commands : []; state.tasksRevision++; renderRuntime(); if (state.monitorOpen && state.monitorTab === "tasks") renderMonitor(); }
    catch (error) { if (!quiet) toast(errorText(error), "error"); }
  }
  async function cancelTask(id, button) {
    button.disabled = true;
    try { await jsonApi(`/api/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} }); toast("已请求取消任务。"); await refreshTasks(); }
    catch (error) { button.disabled = false; toast(errorText(error), "error"); }
  }

  async function openPlan() {
    if (!state.activeId) return;
    state.planOwner = state.activeId; state.planView = null; state.planGeneration++;
    $("plan-board").replaceChildren(); $("plan-evidence").replaceChildren();
    showError("plan-error", null);
    setText("plan-goal", ""); setText("plan-summary", "正在读取…");
    $("plan-dialog").showModal(); await refreshPlan();
  }
  async function refreshPlan(quiet = false) {
    const owner = state.planOwner;
    if (!owner || state.planLoading) return;
    const generation = state.planGeneration, captured = state.sessions.get(owner)?.plan; state.planLoading = true;
    try {
      const plan = await jsonApi(`/api/sessions/${encodeURIComponent(owner)}/plan?limit=256`);
      if (generation !== state.planGeneration || state.planOwner !== owner || !$("plan-dialog").open) return;
      if (mergePlanSummary(owner, plan, captured) && state.activeId === owner) renderConversationStatus();
      state.planView = plan; showError("plan-error", null);
      const runtime = state.sessions.get(owner);
      setText("plan-summary", `${runtime?.title || owner} · 运行：${runtime?.status || "unknown"} · 计划：${plan.status} · revision ${plan.revision} · 验收：${plan.verification}`);
      setText("plan-goal", plan.goal || "尚未创建计划。复杂任务可让 Agent 使用 plan_create 记录步骤。");
      const board = document.createDocumentFragment();
      const groups = [["待执行", ["pending"]], ["执行与等待", ["in_progress", "waiting"]], ["阻塞与中断", ["blocked", "interrupted"]], ["结束（仍待验收）", ["completed", "failed", "cancelled"]]];
      groups.forEach(([label, statuses]) => {
        const column = element("section", "plan-column"); column.append(element("h3", "", label));
        plan.tasks.filter((step) => statuses.includes(step.status)).forEach((step) => {
          const card = element("article", "plan-card"); card.append(element("strong", "", `${step.key} · ${step.status}`), element("p", "", step.title));
          card.append(element("p", "field-hint", `验收条件：${step.acceptance}`), element("p", "field-hint", `依赖：${step.depends_on.join(", ") || "无"}`));
          if (step.block_reason) card.append(element("p", "", `阻塞：${step.block_reason}`));
          if (step.needs_review) card.append(element("p", "", "前台已空闲，步骤尚未汇报结束，需要检查"));
          if (step.execution_ref) { const ref = step.execution_ref; card.append(element("p", "field-hint", `${ref.kind}/${ref.id.slice(0, 8)} · ${ref.runtime_status} · ${ref.drained ? "已排空" : "未排空"}${ref.ready_for_review ? " · 待检查结果" : ""}`)); }
          if (step.result) card.append(element("p", "", `自报结果：${step.result}`));
          if (step.evidence_refs.length) card.append(element("p", "field-hint", `证据：${step.evidence_refs.join(", ")}`));
          column.append(card);
        }); board.append(column);
      }); $("plan-board").replaceChildren(board);
      const evidence = document.createDocumentFragment();
      plan.available_evidence.forEach((item) => { const row = element("details", "plan-card"); row.append(element("summary", "", `${item.name} · ${item.source} · ${item.id}`), element("pre", "", item.summary)); evidence.append(row); });
      $("plan-evidence").replaceChildren(evidence);
    } catch (error) {
      if (generation === state.planGeneration) { setText("plan-summary", "计划读取失败"); showError("plan-error", error); }
    } finally {
      state.planLoading = false;
      if (generation !== state.planGeneration && $("plan-dialog").open) queueMicrotask(() => refreshPlan(true));
    }
  }

  const commands = [
    { label: "新建项目", keywords: "new create session flow", icon: "＋", run: openNew },
    { label: "查看全部会话", keywords: "sessions list", icon: "◇", run: () => switchSidebar("sessions") },
    { label: "工作区文件", keywords: "files explorer", icon: "▤", run: () => switchSidebar("files") },
    { label: "会话设置", keywords: "settings policy model workspace permissions", icon: "⚙", requireSession: true, run: openSettings },
    { label: "查看与编辑会话记忆", keywords: "memory", icon: "◈", requireSession: true, run: openMemory },
    { label: "查看任务计划与步骤证据", keywords: "plan steps 计划 步骤", icon: "☷", requireSession: true, run: openPlan },
    { label: "重命名当前会话", keywords: "rename", icon: "✎", requireSession: true, run: renameSession },
    { label: "导出为 Markdown", keywords: "export markdown md download", icon: "↧", requireSession: true, run: () => exportSession("md") },
    { label: "导出为 JSON", keywords: "export json download", icon: "↧", requireSession: true, run: () => exportSession("json") },
    { label: "停止当前 Agent", keywords: "stop cancel", icon: "■", requireSession: true, run: () => stopSession() },
    { label: "打开 Agent 与任务监控", keywords: "monitor agents tasks tools", icon: "◉", run: () => toggleMonitor(true) },
    { label: "刷新工作台", keywords: "refresh reload", icon: "↻", run: refreshSessions },
    { label: "清理数据…", keywords: "cleanup clean cache logs sessions 清理 缓存 日志", icon: "⌫", run: openMaintenance },
    { label: "删除当前会话…", keywords: "delete remove archive", icon: "×", requireSession: true, run: deleteSession },
  ];
  const slashCommands = [
    { slash: "/new", label: "新建项目", run: openNew }, { slash: "/stop", label: "停止当前 Agent", run: () => stopSession() },
    { slash: "/settings", label: "模型、权限与工具设置", run: openSettings }, { slash: "/memory", label: "查看与编辑会话记忆", run: openMemory },
    { slash: "/export", label: "导出当前会话", run: () => $("export-dialog").showModal() },
    { slash: "/files", label: "浏览工作区文件", run: () => switchSidebar("files") },
    { slash: "/sessions", label: "查看全部会话", run: () => switchSidebar("sessions") },
    { slash: "/tasks", label: "查看后台任务", run: () => { state.monitorTab = "tasks"; activateMonitorTab("tasks"); toggleMonitor(true); } },
    { slash: "/plan", label: "只读查看任务计划与证据", run: openPlan },
    { slash: "/cleanup", label: "清理缓存、日志或会话记录", run: openMaintenance },
  ];
  function openCommand() {
    if (!state.ready) return;
    $("command-input").value = ""; state.paletteIndex = 0; renderCommands(); $("command-dialog").showModal(); $("command-input").focus();
  }
  function renderCommands() {
    const query = $("command-input").value.trim().toLocaleLowerCase();
    const items = commands.filter((command) => (!command.requireSession || state.activeId) && (!query || `${command.label} ${command.keywords}`.toLocaleLowerCase().includes(query))).map((command) => ({ ...command, detail: "命令" }));
    [...state.sessions.values()].filter((session) => !query || `${session.title || "未命名会话"} ${session.id}`.toLocaleLowerCase().includes(query)).forEach((session) => items.push({ label: session.title || "未命名会话", detail: phaseLabel(session), icon: "◇", run: () => selectSession(session.id) }));
    state.paletteItems = items; state.paletteIndex = Math.max(0, Math.min(state.paletteIndex, items.length - 1));
    const fragment = document.createDocumentFragment();
    items.forEach((item, index) => {
      const button = element("button", "command-result" + (index === state.paletteIndex ? " selected" : "")); button.type = "button"; button.setAttribute("role", "option"); button.setAttribute("aria-selected", String(index === state.paletteIndex));
      button.append(element("span", "", item.icon), element("span", "command-result-label", item.label), element("span", "command-result-detail", item.detail));
      button.addEventListener("click", () => runCommand(index)); fragment.append(button);
    });
    if (!items.length) fragment.append(element("div", "monitor-empty", "没有匹配的命令或会话。"));
    $("command-results").replaceChildren(fragment);
    $("command-results").querySelector(".selected")?.scrollIntoView({ block: "nearest" });
  }
  function runCommand(index) { const item = state.paletteItems[index]; if (!item) return; $("command-dialog").close(); item.run(); }
  function renderSlash() {
    const input = $("message-input").value;
    if (!/^\/[a-z]*$/i.test(input)) { $("slash-menu").hidden = true; return; }
    state.slashItems = slashCommands.filter((command) => command.slash.startsWith(input.toLowerCase()));
    state.slashIndex = Math.max(0, Math.min(state.slashIndex, state.slashItems.length - 1));
    const fragment = document.createDocumentFragment();
    state.slashItems.forEach((command, index) => {
      const button = element("button", "slash-option" + (state.slashIndex === index ? " selected" : "")); button.type = "button"; button.setAttribute("role", "option"); button.setAttribute("aria-selected", String(state.slashIndex === index));
      button.append(element("strong", "", command.slash), element("span", "", command.label)); button.addEventListener("mousedown", (event) => event.preventDefault()); button.addEventListener("click", () => completeSlash(index)); fragment.append(button);
    });
    $("slash-menu").replaceChildren(fragment); $("slash-menu").hidden = !state.slashItems.length;
  }
  function completeSlash(index) {
    const item = state.slashItems[index]; if (!item) return;
    $("message-input").value = item.slash; saveDraft(); $("slash-menu").hidden = true; renderComposer(); focusInput();
  }
  function activateMonitorTab(tab) {
    state.monitorTab = tab; document.querySelectorAll("[data-monitor]").forEach((button) => { button.classList.toggle("active", button.dataset.monitor === tab); button.setAttribute("aria-selected", String(button.dataset.monitor === tab)); }); renderMonitor();
  }

  ["new-session", "new-session-footer", "welcome-new"].forEach((id) => $(id).addEventListener("click", openNew));
  $("reconnect-service").addEventListener("click", reconnect);
  $("activity-maintenance").addEventListener("click", openMaintenance);
  $("maintenance-form").addEventListener("submit", cleanupData);
  $("refresh-maintenance").addEventListener("click", refreshMaintenance);
  ["cleanup-cache", "cleanup-logs", "cleanup-sessions"].forEach((id) => $(id).addEventListener("change", () => { $("cleanup-confirm").checked = false; renderMaintenance(); }));
  $("cleanup-confirm").addEventListener("change", renderMaintenance);
  $("maintenance-dialog").addEventListener("cancel", (event) => { if (state.cleanupPending) event.preventDefault(); });
  $("new-form").addEventListener("submit", createSession);
  $("new-mode").addEventListener("change", updateNewMode);
  $("new-browse").addEventListener("click", () => window.FlowUI.pickDirectory({ input: $("new-project-path"), valid: () => $("new-dialog").open }));
  window.FlowUI.init({ api: jsonApi, models: () => state.meta.models, selectSession: async (id) => { if (!state.sessions.has(id)) await refreshSessions(); if (state.sessions.has(id)) selectSession(id); else toast("节点会话暂不可用。", "error"); }, enter: () => { saveDraft(); state.activeId = null; state.selectionGeneration++; $("welcome").hidden = true; $("conversation").hidden = true; $("sidebar").classList.remove("mobile-open"); renderSessions(); renderTabs(); }, toast });
  $("refresh-sessions").addEventListener("click", refreshSessions); $("session-search").addEventListener("input", renderSessions);
  $("composer-form").addEventListener("submit", sendMessage); $("stop-agent").addEventListener("click", () => stopSession());
  $("message-input").addEventListener("input", () => { saveDraft(); resizeInput(); renderComposer(); state.slashIndex = 0; renderSlash(); });
  $("message-input").addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (!$("slash-menu").hidden) {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); state.slashIndex = (state.slashIndex + (event.key === "ArrowDown" ? 1 : -1) + state.slashItems.length) % state.slashItems.length; renderSlash(); return; }
      if (event.key === "Tab") { event.preventDefault(); completeSlash(state.slashIndex); return; }
      if (event.key === "Escape") { event.preventDefault(); $("slash-menu").hidden = true; return; }
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); const item = state.slashItems[state.slashIndex]; if (item) { $("message-input").value = ""; saveDraft(); resizeInput(); $("slash-menu").hidden = true; renderComposer(); item.run(); } return; }
    }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); }
  });
  $("message-scroll").addEventListener("scroll", updateJumpButton, { passive: true }); $("jump-bottom").addEventListener("click", () => scrollBottom(true));
  $("load-history").addEventListener("click", () => { const history = state.history.get(state.activeId); if (history?.nextBefore != null) { $("load-history").disabled = true; loadConversation(state.activeId, { before: history.nextBefore }); } });
  $("confirmation-allow").addEventListener("click", () => answerConfirmation(true)); $("confirmation-deny").addEventListener("click", () => answerConfirmation(false));
  ["open-settings", "activity-settings"].forEach((id) => $(id).addEventListener("click", openSettings));
  ["composer-model", "status-model"].forEach((id) => $(id).addEventListener("click", () => openQuickPicker("model")));
  ["composer-permission", "status-permission"].forEach((id) => $(id).addEventListener("click", () => openQuickPicker("policy")));
  $("quick-settings").addEventListener("click", () => { $("quick-dialog").close(); openSettings(); });
  $("settings-form").addEventListener("submit", saveSettings); $("settings-policy").addEventListener("change", policyDescription);
  $("browse-workspace").addEventListener("click", () => browseWorkspace($("settings-project-path").value.trim() || state.sessions.get(state.settingsId)?.workspace_path));
  $("workspace-parent").addEventListener("click", () => browseWorkspace(state.workspaceBrowseParent));
  $("workspace-select").addEventListener("click", () => { $("settings-project-path").value = state.workspaceBrowsePath; $("workspace-picker").hidden = true; state.workspaceBrowseGeneration++; });
  $("reset-workspace").addEventListener("click", () => { $("settings-project-path").value = ""; $("workspace-picker").hidden = true; state.workspaceBrowseGeneration++; });
  $("manage-models").addEventListener("click", openModels); $("model-form").addEventListener("submit", saveModel); $("model-reset").addEventListener("click", resetModelForm);
  $("models-dialog").addEventListener("close", () => { $("model-api-key").value = ""; });
  $("open-memory").addEventListener("click", openMemory); $("memory-form").addEventListener("submit", saveMemory);
  $("refresh-plan").addEventListener("click", () => refreshPlan());
  $("plan-progress").addEventListener("click", openPlan);
  $("rename-session").addEventListener("click", renameSession); $("delete-session").addEventListener("click", deleteSession);
  $("export-session").addEventListener("click", () => $("export-dialog").showModal()); document.querySelectorAll("[data-export]").forEach((button) => button.addEventListener("click", () => exportSession(button.dataset.export)));
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchSidebar(button.dataset.view)));
  $("mobile-sidebar").addEventListener("click", () => $("sidebar").classList.toggle("mobile-open"));
  $("refresh-files").addEventListener("click", () => loadFiles(state.filePath)); $("file-up").addEventListener("click", () => loadFiles(state.filePath.split("/").slice(0, -1).join("/")));
  $("copy-file").addEventListener("click", () => copyText($("preview-content").textContent));
  ["activity-monitor", "status-monitor"].forEach((id) => $(id).addEventListener("click", () => toggleMonitor())); $("close-monitor").addEventListener("click", () => toggleMonitor(false));
  $("inspector-close").addEventListener("click", () => { $("inspector").classList.remove("mobile-open"); });
  document.querySelectorAll("[data-monitor]").forEach((button) => button.addEventListener("click", () => activateMonitorTab(button.dataset.monitor))); $("refresh-tasks").addEventListener("click", () => refreshTasks());
  $("open-command").addEventListener("click", openCommand);
  $("command-input").addEventListener("input", () => { state.paletteIndex = 0; renderCommands(); });
  $("command-input").addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    if (["ArrowDown", "ArrowUp"].includes(event.key) && state.paletteItems.length) { event.preventDefault(); state.paletteIndex = (state.paletteIndex + (event.key === "ArrowDown" ? 1 : -1) + state.paletteItems.length) % state.paletteItems.length; renderCommands(); }
    else if (event.key === "Enter") { event.preventDefault(); runCommand(state.paletteIndex); }
  });
  document.addEventListener("keydown", handleWorkspaceShortcut);
  document.querySelectorAll(".dialog-close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target === dialog && !(dialog.id === "maintenance-dialog" && state.cleanupPending)) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  });
  document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => { $("message-input").value = button.dataset.prompt; saveDraft(); resizeInput(); renderComposer(); focusInput(); }));
  window.addEventListener("online", () => { if (!state.connected) reconnect(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.ready) { refreshSessions(); if (state.monitorOpen) refreshTasks(true); } });
  if (/Mac|iPhone|iPad/.test(navigator.platform)) { setText("command-shortcut", "⌘ K"); setText("welcome-command-shortcut", "⌘ K"); }
  initLayout(); $("activity-monitor").classList.toggle("active", state.monitorOpen); renderInspector(); renderMonitor(); connect();
})();
