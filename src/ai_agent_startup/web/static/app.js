"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    ready: false, connecting: false, deletedIds: new Set(),
    meta: { policies: [], models: [], tools: [], limits: {} }, sessions: new Map(),
    activeId: null, openTabs: [], drafts: new Map(), history: new Map(),
    runtime: {}, tasks: [], commands: [], tasksRevision: 0, sidebarView: "sessions", filePath: "",
    monitorTab: "agents", monitorOpen: false, connected: false, connectionState: "connecting",
    sending: new Set(), loading: new Set(), pendingHistory: new Set(), snapshotVersion: 0, streamAbort: null, streamGeneration: 0,
    retryTimer: null, retryCount: 0, taskTimer: null, clockTimer: null,
    paletteItems: [], paletteIndex: 0, slashItems: [], slashIndex: 0,
    settingsId: null, memoryId: null, settingsAllTools: true, filesGeneration: 0,
    eventState: null, eventFrame: null, renderSignatures: {},
    maintenance: null, maintenanceLoading: false, maintenanceGeneration: 0, cleanupPending: false,
    planOwner: null, planView: null, planLoading: false, planGeneration: 0,
  };
  const policyLabels = {
    read_only: "只读", readonly: "只读", read: "只读", ask: "逐次确认", confirm: "逐次确认",
    standard: "标准", safe: "安全", balanced: "标准", trusted: "信任", smart: "智能", auto: "自动",
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
    const phase = session?.phase || session?.status || "idle";
    return phaseLabels[phase] || (session?.busy ? "运行中" : phase);
  }
  function isError(session) { return ["error", "failed"].includes(session?.status) || ["error", "failed"].includes(session?.phase); }
  function statusClass(session) { return session?.confirmation ? "waiting" : isError(session) ? "error" : session?.busy ? "busy" : ""; }
  function normalizeOptions(items, kind) {
    if (!Array.isArray(items)) return [];
    return items.map((item) => {
      if (typeof item === "string") return { value: item, label: kind === "policy" ? policyLabels[item] || item : item, description: "" };
      const value = item.id ?? item.name ?? item.value ?? item.key ?? item.profile ?? item.policy;
      return { value: String(value ?? ""), label: item.label || item.title || item.display_name || (kind === "policy" ? policyLabels[value] : null) || String(value ?? ""), description: item.description || item.help || "" };
    }).filter((item) => item.value);
  }
  function optionLabel(items, value, kind) {
    if (!value) return kind === "policy" ? "默认权限" : "默认模型";
    return normalizeOptions(items, kind).find((item) => item.value === value)?.label || (kind === "policy" ? policyLabels[value] : null) || value;
  }
  function workspaceLabel(session) { return session?.parent_id ? (session.workspace_mode === "isolated" ? "历史独立工作区" : "父项目工作区") : "项目工作区"; }
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

  async function connect() {
    if (state.ready || state.connecting) return;
    state.connecting = true; clearTimeout(state.retryTimer);
    connectionStatus(state.retryCount ? "offline" : "connecting");
    try {
      const meta = await jsonApi("/api/meta");
      const snapshot = await jsonApi("/api/sessions");
      state.meta = meta; state.ready = true; state.retryCount = 0;
      showError("connection-error", null); applySnapshot(snapshot);
      const remembered = sessionStorage.getItem("agent-workspace-session");
      const initial = state.sessions.has(remembered) ? remembered : state.sessions.keys().next().value;
      if (initial) selectSession(initial); else showWelcome();
      startEvents(); refreshTasks(true);
      clearInterval(state.taskTimer); clearInterval(state.clockTimer);
      state.taskTimer = setInterval(() => {
        if (state.monitorOpen && state.ready) refreshTasks(true);
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
    renderComposer(); renderConnectionActions();
  }
  function scheduleSnapshot(snapshot) {
    state.eventState = snapshot;
    if (!state.eventFrame) state.eventFrame = requestAnimationFrame(() => {
      state.eventFrame = null; const latest = state.eventState; state.eventState = null;
      if (latest && state.ready) applySnapshot(latest);
    });
  }
  async function startEvents() {
    if (!state.ready) return;
    const generation = ++state.streamGeneration;
    state.streamAbort?.abort();
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
      let buffer = "", eventName = "message", dataLines = [];
      const dispatch = () => {
        if (dataLines.length && ["state", "message"].includes(eventName)) {
          try { scheduleSnapshot(JSON.parse(dataLines.join("\n"))); }
          catch { /* Ignore malformed frames; a later state snapshot repairs the view. */ }
        }
        eventName = "message"; dataLines = [];
      };
      while (state.ready && generation === state.streamGeneration) {
        const { value, done } = await reader.read();
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
  function applySnapshot(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.sessions)) return;
    state.snapshotVersion++;
    const oldActive = selectedSession();
    const incoming = new Map(snapshot.sessions.filter((session) => !state.deletedIds.has(session.id)).map((session) => [session.id, { ...state.sessions.get(session.id), ...session }]));
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
        subtitle.append(element("span", "small-status-dot " + statusClass(session)), element("span", "", phaseLabel(session)), element("span", "", "·"), element("span", "", session.parent_id ? "子 Agent" : workspaceLabel(session)));
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
    state.activeId = null; $("welcome").hidden = false; $("conversation").hidden = true;
    sessionStorage.removeItem("agent-workspace-session"); renderSessions(); renderTabs(); renderRuntime();
    $("jump-bottom").hidden = true;
  }
  function selectSession(id) {
    if (!state.sessions.has(id)) return;
    if (id === state.activeId) { $("sidebar").classList.remove("mobile-open"); return; }
    saveDraft(); state.activeId = id;
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
    if (state.sidebarView === "files") loadFiles("");
  }
  async function loadConversation(id, { before = null, quiet = false } = {}) {
    if (state.loading.has(id)) { if (before === null) state.pendingHistory.add(id); return; }
    state.loading.add(id);
    const version = state.snapshotVersion;
    if (id === state.activeId && !quiet) $("history-loading").hidden = false;
    const oldHeight = $("message-scroll").scrollHeight, oldTop = $("message-scroll").scrollTop;
    const atBottom = nearBottom();
    try {
      const query = new URLSearchParams({ limit: "60" }); if (before !== null) query.set("before", String(before));
      const data = await jsonApi(sessionPath(id, `?${query}`));
      if (!state.sessions.has(id)) return;
      const current = state.sessions.get(id), mergedSession = { ...current, ...data };
      if (version !== state.snapshotVersion) {
        for (const key of ["busy", "phase", "status", "confirmation", "partial", "partial_display", "partial_reasoning", "title", "permission_policy", "model_profile", "tool_names", "workspace_mode", "parent_id", "unread", "total_messages"]) {
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
    $("conversation-start").hidden = history.records.some(({ message }) => hasNarrative(message)) || Boolean(selectedSession()?.busy);
  }
  function renderConversationStatus() {
    const session = selectedSession(); if (!session) return;
    setText("conversation-title", session.title || "未命名会话");
    setText("session-state", phaseLabel(session)); $("session-state").className = "state-pill " + statusClass(session);
    showError("session-error", session.error || null);
    setText("status-workspace", workspaceLabel(session));
    setText("status-permission", optionLabel(state.meta.policies, session.permission_policy, "policy"));
    setText("status-model", optionLabel(state.meta.models, session.model_profile, "model"));
    setText("composer-model-label", optionLabel(state.meta.models, session.model_profile, "model"));
    setText("file-workspace-label", workspaceLabel(session));
    const confirmation = session.confirmation;
    $("confirmation-card").hidden = !confirmation;
    if (confirmation) setText("confirmation-prompt", plainContent(confirmation.prompt));
    renderComposer();
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
    setText("composer-status", state.connectionState === "offline" ? "连接已断开，正在重试…" : session.confirmation ? "确认操作后，Agent 将继续" : pending ? "正在发送…" : busy ? phaseLabel(session) : "准备就绪");
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

  function openNew() { $("new-title").value = ""; showError("new-error", null); $("new-dialog").showModal(); $("new-title").focus(); }
  async function createSession(event) {
    event.preventDefault(); $("new-submit").disabled = true; showError("new-error", null);
    try {
      const session = await jsonApi("/api/sessions", { method: "POST", body: { title: $("new-title").value.trim() || "新会话" } });
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
    const defaultOption = element("option", "", kind === "policy" ? "继承默认权限" : "继承默认模型"); defaultOption.value = ""; node.append(defaultOption);
    const options = normalizeOptions(items, kind);
    if (value && !options.some((item) => item.value === value)) options.push({ value, label: optionLabel(items, value, kind) });
    options.forEach((item) => { const option = element("option", "", item.label); option.value = item.value; node.append(option); });
    node.value = value || "";
  }
  function policyDescription() {
    const option = normalizeOptions(state.meta.policies, "policy").find((item) => item.value === $("settings-policy").value);
    const descriptions = { readonly: "允许读取与安全查询；阻止写入及其他有副作用的操作。", standard: "读取操作可直接执行；写入、命令等操作遵循确认规则。", trusted: "仅匹配显式预先确认规则时自动批准，其余操作仍需确认。", smart: "已验证的低风险操作可自动批准；其余操作需要确认。", full_access: "工具调用自动批准，保留沙箱、路径、配额及审计检查；删除会话仍需确认。" };
    setText("settings-policy-description", option?.description || descriptions[$("settings-policy").value] || "使用服务的默认权限策略；需要确认的操作会在对话中显示。");
  }
  async function openSettings() {
    const session = selectedSession(); if (!session) { toast("请先选择或创建会话。"); return; }
    state.settingsId = session.id; showError("settings-error", null);
    $("settings-title").value = session.title || ""; fillSelect("settings-policy", state.meta.policies, session.permission_policy, "policy"); fillSelect("settings-model", state.meta.models, session.model_profile, "model");
    policyDescription();
    setText("settings-busy-hint", session.busy ? "运行中的设置修改可能被服务拒绝。" : "");
    state.settingsAllTools = session.tool_names == null;
    const selected = new Set(session.tool_names || []), fragment = document.createDocumentFragment();
    normalizeOptions(state.meta.tools, "tool").forEach((tool) => {
      const label = element("label"), input = element("input"); input.type = "checkbox"; input.value = tool.value; input.checked = state.settingsAllTools || selected.has(tool.value);
      input.addEventListener("change", () => { state.settingsAllTools = false; }); label.title = tool.description || tool.label; label.append(input, element("span", "", tool.label)); fragment.append(label);
    });
    $("settings-tools").replaceChildren(fragment); $("settings-dialog").showModal();
  }
  async function saveSettings(event) {
    event.preventDefault(); const id = state.settingsId; if (!id) return;
    $("settings-submit").disabled = true; showError("settings-error", null);
    const tools = [...$("settings-tools").querySelectorAll("input:checked")].map((input) => input.value);
    try {
      const body = { title: $("settings-title").value.trim() || "未命名会话", permission_policy: $("settings-policy").value || null, model_profile: $("settings-model").value || null, tool_names: state.settingsAllTools ? null : tools };
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
  }
  function toggleMonitor(force) {
    state.monitorOpen = force ?? !state.monitorOpen; $("monitor").hidden = !state.monitorOpen; $("activity-monitor").classList.toggle("active", state.monitorOpen);
    if (state.monitorOpen) { renderMonitor(); refreshTasks(true); }
  }
  function monitorTable(headers, widths) {
    const table = element("table", "monitor-table"), head = element("thead"), row = element("tr"), body = element("tbody");
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
    const generation = state.planGeneration; state.planLoading = true;
    try {
      const plan = await jsonApi(`/api/sessions/${encodeURIComponent(owner)}/plan?limit=256`);
      if (generation !== state.planGeneration || state.planOwner !== owner || !$("plan-dialog").open) return;
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
    { label: "新建会话", keywords: "new create session", icon: "＋", run: openNew },
    { label: "查看全部会话", keywords: "sessions list", icon: "◇", run: () => switchSidebar("sessions") },
    { label: "工作区文件", keywords: "files explorer", icon: "▤", run: () => switchSidebar("files") },
    { label: "会话设置", keywords: "settings policy model tools permissions", icon: "⚙", requireSession: true, run: openSettings },
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
    { slash: "/new", label: "新建会话", run: openNew }, { slash: "/stop", label: "停止当前 Agent", run: () => stopSession() },
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
  ["open-settings", "activity-settings", "composer-model", "status-permission", "status-model"].forEach((id) => $(id).addEventListener("click", openSettings));
  $("settings-form").addEventListener("submit", saveSettings); $("settings-policy").addEventListener("change", policyDescription);
  $("tools-all").addEventListener("click", () => { state.settingsAllTools = true; $("settings-tools").querySelectorAll("input").forEach((input) => { input.checked = true; }); });
  $("tools-none").addEventListener("click", () => { state.settingsAllTools = false; $("settings-tools").querySelectorAll("input").forEach((input) => { input.checked = false; }); });
  $("open-memory").addEventListener("click", openMemory); $("memory-form").addEventListener("submit", saveMemory);
  $("refresh-plan").addEventListener("click", () => refreshPlan());
  $("rename-session").addEventListener("click", renameSession); $("delete-session").addEventListener("click", deleteSession);
  $("export-session").addEventListener("click", () => $("export-dialog").showModal()); document.querySelectorAll("[data-export]").forEach((button) => button.addEventListener("click", () => exportSession(button.dataset.export)));
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchSidebar(button.dataset.view)));
  $("mobile-sidebar").addEventListener("click", () => $("sidebar").classList.toggle("mobile-open"));
  $("refresh-files").addEventListener("click", () => loadFiles(state.filePath)); $("file-up").addEventListener("click", () => loadFiles(state.filePath.split("/").slice(0, -1).join("/")));
  $("copy-file").addEventListener("click", () => copyText($("preview-content").textContent));
  ["activity-monitor", "status-monitor"].forEach((id) => $(id).addEventListener("click", () => toggleMonitor())); $("close-monitor").addEventListener("click", () => toggleMonitor(false));
  document.querySelectorAll("[data-monitor]").forEach((button) => button.addEventListener("click", () => activateMonitorTab(button.dataset.monitor))); $("refresh-tasks").addEventListener("click", () => refreshTasks());
  $("open-command").addEventListener("click", openCommand);
  $("command-input").addEventListener("input", () => { state.paletteIndex = 0; renderCommands(); });
  $("command-input").addEventListener("keydown", (event) => {
    if (event.isComposing) return;
    if (["ArrowDown", "ArrowUp"].includes(event.key) && state.paletteItems.length) { event.preventDefault(); state.paletteIndex = (state.paletteIndex + (event.key === "ArrowDown" ? 1 : -1) + state.paletteItems.length) % state.paletteItems.length; renderCommands(); }
    else if (event.key === "Enter") { event.preventDefault(); runCommand(state.paletteIndex); }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && ["k", "p"].includes(event.key.toLowerCase()) && state.ready && !state.cleanupPending) {
      event.preventDefault(); if (!$("command-dialog").open) { document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close()); openCommand(); }
    }
  });
  document.querySelectorAll(".dialog-close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target === dialog && !(dialog.id === "maintenance-dialog" && state.cleanupPending)) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  });
  document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => { $("message-input").value = button.dataset.prompt; saveDraft(); resizeInput(); renderComposer(); focusInput(); }));
  window.addEventListener("online", () => { if (!state.connected) reconnect(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.ready) { refreshSessions(); if (state.monitorOpen) refreshTasks(true); } });
  if (/Mac|iPhone|iPad/.test(navigator.platform)) { setText("command-shortcut", "⌘ K"); setText("welcome-command-shortcut", "⌘ K"); }
  connect();
})();
