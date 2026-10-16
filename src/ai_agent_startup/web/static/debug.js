"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const states = { running: "执行中", idle: "就绪", queued: "排队", confirming: "等待确认", stopping: "停止中", completed: "完成", error: "失败", timeout: "超时", cancelled: "取消", rejected: "拒绝 / 未执行", checkpoint: "阶段暂停", waiting_workspace: "等待工作区锁" };
  const activities = { reasoning: "思考流", content: "输出回复", tool_arguments: "生成工具参数", status: "接收模型状态" };
  let snapshot = null, paused = false, stopped = false, timelineWindow = null, polling = false;
  let zoom = 1, selectedEvent = null, detailAbort = null, detailRevision = 0, detailNext = null, detailBusy = false;
  let selectedRegion = null, selectionPage = 0, focusedAgent = "", timelineLabels = null, currentTimelineView = null;
  const expandedBatches = new Set(), expandedAgents = new Set();
  const eventLabels = { "turn.started": "轮次开始", "turn.stopped": "轮次停止", "model.completed": "模型请求完成", "model.final": "最终回复", "tools.batch": "工具批次", "tool.started": "工具开始", "tool.completed": "工具完成", "phase.changed": "阶段", "plan.updated": "计划更新", "agent.created": "创建子 Agent", "agent.started": "Agent 开始", "agent.completed": "Agent 完成", "message.sent": "发送消息", "message.read": "读取消息", "context.sample": "上下文采样", "context.compacted": "上下文压缩", "span.started": "模块开始", "span.completed": "模块完成" };
  const phaseLabels = { model_wait: "等待模型", context_prepare: "准备上下文", compaction: "压缩上下文", tools: "工具执行", continuation: "继续执行", recovery: "连接恢复", confirmation: "等待确认", stopping: "停止中", planning: "计划", complete: "完成" };
  const sourceLabels = { provider: "provider · 实际报告", estimated: "estimated · 估算", unknown: "未知" };
  function node(tag, text, className = "") { const value = document.createElement(tag); value.textContent = text; value.className = className; return value; }
  const short = (id) => id ? String(id).slice(0, 8) : "—";
  const duration = (ms) => `${((ms || 0) / 1000).toFixed(2)} s`;
  const elapsedLabel = (ms) => ms >= 3600000 ? `${(ms / 3600000).toFixed(1)} h` : ms >= 60000 ? `${(ms / 60000).toFixed(1)} min` : `${(ms / 1000).toFixed(1)} s`;
  function debugNavigation(search) {
    const query = new URLSearchParams(search), sessionId = query.get("session") || "";
    return { sessionId, openContext: Boolean(sessionId && query.get("context") === "1") };
  }
  const navigation = debugNavigation(window.location.search);
  let navigationApplied = false;
  const archive = { sessionId: null, generation: 0, listGeneration: 0, rawGeneration: 0, directoryOffset: 0, nextDirectoryOffset: null, chunk: null, nextRawOffset: null, listAbort: null, rawAbort: null, busyList: false, busyRaw: false };
  function archiveCurrent(sessionId, generation) {
    return $("context-archive").open && archive.sessionId === sessionId && archive.generation === generation;
  }
  async function archiveRequest(path, signal) {
    const response = await fetch(path, { cache: "no-store", credentials: "omit", signal });
    if (!response.ok) {
      let detail = "";
      try { const body = await response.json(); if (typeof body.detail === "string") detail = body.detail; } catch {}
      throw new Error(detail || (response.status === 404 ? "上下文归档不可用 (404)，可能尚无归档或 DEBUG 未启用。" : `归档读取失败 (${response.status})`));
    }
    return response.json();
  }
  function renderArchiveControls() {
    $("archive-prev").disabled = archive.busyList || archive.directoryOffset <= 0;
    $("archive-next").disabled = archive.busyList || archive.nextDirectoryOffset == null;
    $("archive-refresh").disabled = archive.busyList;
    $("archive-chunks").querySelectorAll("button").forEach((button) => { button.disabled = archive.busyList; });
    $("archive-read").disabled = archive.busyList || archive.busyRaw || !archive.chunk;
    $("archive-read").textContent = archive.busyRaw ? "正在读取…" : "读取原文";
    $("archive-raw-next").hidden = archive.nextRawOffset == null;
    $("archive-raw-next").disabled = archive.busyRaw || archive.nextRawOffset == null;
  }
  function resetArchive() {
    archive.generation++; archive.listGeneration++; archive.rawGeneration++;
    archive.listAbort?.abort(); archive.rawAbort?.abort();
    archive.sessionId = null; archive.chunk = null; archive.nextRawOffset = null;
    archive.directoryOffset = 0; archive.nextDirectoryOffset = null; archive.busyList = false; archive.busyRaw = false;
    ["archive-summary", "archive-kind", "archive-revision", "archive-page", "archive-raw", "archive-raw-page", "archive-chunk-meta", "archive-chunk-summary", "archive-error"].forEach((id) => { $(id).textContent = ""; });
    $("archive-paths").replaceChildren(); $("archive-chunks").replaceChildren(); $("archive-error").hidden = true;
    $("archive-chunk-title").textContent = "选择归档块查看详情"; renderArchiveControls();
  }
  function openArchive(sessionId) {
    if (!sessionId) return;
    resetArchive(); archive.sessionId = sessionId;
    $("archive-agent").textContent = `Agent ${sessionId} · 只读`;
    if (!$("context-archive").open) $("context-archive").showModal();
    loadArchiveDirectory(0);
  }
  function archiveTime(value) {
    if (!value) return "—";
    if (typeof value !== "object") return String(value);
    return `历史消息 ${value.start ?? "—"}–${value.end ?? "—"} · ${value.created_at || "—"}`;
  }
  function selectArchiveChunk(chunk) {
    archive.rawGeneration++; archive.rawAbort?.abort(); archive.busyRaw = false;
    archive.chunk = chunk; archive.nextRawOffset = null;
    $("archive-raw").textContent = ""; $("archive-raw-page").textContent = "";
    $("archive-error").textContent = ""; $("archive-error").hidden = true;
    $("archive-chunk-title").textContent = chunk ? `归档块 ${chunk.ID}` : "选择归档块查看详情";
    $("archive-chunk-meta").textContent = chunk ? `Time: ${archiveTime(chunk.Time)} · summary_kind: ${chunk.summary_kind || "—"}\npath: ${chunk.path || "—"}` : "";
    $("archive-chunk-summary").textContent = chunk?.Summary || "";
    $("archive-chunks").querySelectorAll?.("[data-chunk-id]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.chunkId === chunk?.ID)));
    renderArchiveControls();
  }
  function renderArchiveDirectory(data) {
    $("archive-format").textContent = String(data.format || "json").toUpperCase();
    $("archive-revision").textContent = String(data.revision ?? "—");
    $("archive-kind").textContent = data.summary_kind || "—";
    $("archive-summary").textContent = data.session_summary || "尚无会话摘要。";
    const paths = [];
    ["manifest", "chunks", "journal"].forEach((key) => { paths.push(node("dt", key), node("dd", data.paths?.[key] || "—")); });
    $("archive-paths").replaceChildren(...paths);
    const chunks = (data.chunks || []).map((chunk) => {
      const button = node("button", "", "archive-chunk"); button.type = "button";
      button.dataset.chunkId = chunk.ID; button.setAttribute("aria-pressed", "false");
      button.append(node("strong", `ID: ${chunk.ID}`), node("small", `Time: ${archiveTime(chunk.Time)} · summary_kind: ${chunk.summary_kind || "—"}`), node("span", chunk.Summary || "暂无摘要"), node("small", `path: ${chunk.path || "—"}`));
      button.addEventListener("click", () => selectArchiveChunk(chunk)); return button;
    });
    $("archive-chunks").replaceChildren(...(chunks.length ? chunks : [node("p", "尚无归档块。", "notice")]));
    $("archive-page").textContent = chunks.length ? `第 ${archive.directoryOffset + 1}–${archive.directoryOffset + chunks.length} 块 · 共 ${data.total_chunks ?? chunks.length} 块` : `共 ${data.total_chunks ?? 0} 块`;
  }
  async function loadArchiveDirectory(offset = 0) {
    const sessionId = archive.sessionId, generation = archive.generation;
    if (!sessionId || !$("context-archive").open) return;
    archive.listAbort?.abort(); archive.listAbort = new AbortController();
    const requestGeneration = ++archive.listGeneration;
    selectArchiveChunk(null); archive.busyList = true; renderArchiveControls();
    $("archive-error").textContent = ""; $("archive-error").hidden = true; $("archive-page").textContent = "正在读取目录…";
    try {
      const query = new URLSearchParams({ offset: String(offset), limit: "20" });
      const data = await archiveRequest(`/api/debug/sessions/${encodeURIComponent(sessionId)}/context?${query}`, archive.listAbort.signal);
      if (!archiveCurrent(sessionId, generation) || requestGeneration !== archive.listGeneration) return;
      archive.directoryOffset = offset; archive.nextDirectoryOffset = data.next_offset ?? null;
      renderArchiveDirectory(data);
    } catch (error) {
      if (!archiveCurrent(sessionId, generation) || requestGeneration !== archive.listGeneration || error.name === "AbortError") return;
      $("archive-error").textContent = error.message || "归档目录读取失败，请重试。"; $("archive-error").hidden = false;
      $("archive-page").textContent = "目录读取未完成";
    } finally {
      if (archiveCurrent(sessionId, generation) && requestGeneration === archive.listGeneration) { archive.busyList = false; renderArchiveControls(); }
    }
  }
  async function loadArchiveRaw(offset = 0) {
    const sessionId = archive.sessionId, generation = archive.generation, chunkId = archive.chunk?.ID;
    if (!sessionId || !chunkId || archive.busyRaw || !$("context-archive").open) return;
    archive.rawAbort?.abort(); archive.rawAbort = new AbortController();
    const requestGeneration = ++archive.rawGeneration;
    archive.busyRaw = true; renderArchiveControls(); $("archive-error").textContent = ""; $("archive-error").hidden = true;
    try {
      const query = new URLSearchParams({ offset: String(offset), limit: "6000" });
      const data = await archiveRequest(`/api/debug/sessions/${encodeURIComponent(sessionId)}/context/${encodeURIComponent(chunkId)}?${query}`, archive.rawAbort.signal);
      if (!archiveCurrent(sessionId, generation) || requestGeneration !== archive.rawGeneration || archive.chunk?.ID !== chunkId) return;
      $("archive-raw").textContent = data.RawHistory || "";
      const count = typeof data.RawHistory === "string" ? Array.from(data.RawHistory).length : 0;
      $("archive-raw-page").textContent = `字符 ${count ? offset + 1 : offset}–${offset + count} / ${data.raw_chars ?? count}`;
      archive.nextRawOffset = data.next_offset ?? null;
    } catch (error) {
      if (!archiveCurrent(sessionId, generation) || requestGeneration !== archive.rawGeneration || error.name === "AbortError") return;
      $("archive-error").textContent = error.message || "归档原文读取失败，请重试。"; $("archive-error").hidden = false;
    } finally {
      if (archiveCurrent(sessionId, generation) && requestGeneration === archive.rawGeneration) { archive.busyRaw = false; renderArchiveControls(); }
    }
  }
  function eventLabel(event) {
    if (event.kind === "phase.changed") return phaseLabels[event.phase] || "阶段变化";
    if (event.kind === "plan.updated" && Number.isSafeInteger(event.stage_index) && event.stage_index >= 0) {
      const planStates = { planned: "待执行", pending: "待执行", queued: "排队", running: "执行中", completed: "完成", cancelled: "取消", error: "失败", rejected: "拒绝", timeout: "超时", checkpoint: "阶段暂停" };
      return `步骤 ${event.stage_index} · ${planStates[event.state] || "状态未知"}`;
    }
    if (event.kind === "tool.started" || event.kind === "tool.completed") return event.tool_name || "工具";
    return eventLabels[event.kind] || event.module || "事件";
  }
  function eventMetadata(event) {
    const safe = {};
    // Only known metadata fields appear before the explicit detail request.
    ["seq", "kind", "ts", "elapsed_ms", "session_id", "parent_session_id", "related_session_id", "turn_id", "batch_id", "tool_call_id", "task_id", "message_seq", "message_index", "stage_index", "span_id", "parent_span_id", "state", "reason", "phase", "tool_name", "module", "duration_ms", "error_type", "metrics"].forEach((key) => { if (event[key] != null) safe[key] = event[key]; });
    return safe;
  }
  function clearEventDetail(message = "选择时间线事件或工具批次查看元数据。") {
    detailRevision++; detailAbort?.abort(); selectedEvent = null; detailNext = null; detailBusy = false;
    ["event-detail-meta", "event-detail-content", "event-detail-page", "event-detail-error"].forEach((id) => { $(id).textContent = ""; });
    $("event-detail-summary").textContent = message; $("event-detail-error").hidden = true;
    $("event-detail-load").disabled = true; $("event-detail-next").hidden = true;
  }
  function selectEvent(event, group = null) {
    clearEventDetail(); selectedEvent = { event, generation: timelineWindow?.generation, groupId: group?.id || null };
    $("event-detail-summary").textContent = group ? `工具批次 · ${group.calls.length} 个调用 · 异常 ${group.errors} · ${duration(group.end - group.start)}` : `${eventLabel(event)} · Agent ${short(event.session_id)} · ${states[event.state] || event.state || "元数据"}`;
    $("event-detail-meta").textContent = JSON.stringify(group ? { batch_id: group.batch_id, session_id: group.session_id, start_ms: group.start, end_ms: group.end, calls: group.calls.map((call) => eventMetadata(call.event)) } : eventMetadata(event), null, 2);
    $("event-detail-load").disabled = !Number.isSafeInteger(event.seq) || !selectedEvent.generation;
  }
  function regionRows(region) {
    if (region.type === "tools") return region.groups.flatMap((group) => group.calls.map((call) => ({ event: call.event, label: `${call.tool_name} · ${states[call.state] || call.state} · ${duration(call.end - call.start)}` })));
    if (region.type === "flowDown") {
      const sequences = new Set(region.events.map((event) => event.seq));
      const calls = region.groups.flatMap((group) => group.calls.filter((call) => sequences.has(call.event.seq) || sequences.has(call.startEvent?.seq)).map((call) => ({ event: call.event, group, label: `${call.tool_name} · ${states[call.state] || call.state} · ${duration(call.end - call.start)}` })));
      const stages = region.events.filter((event) => !["tool.started", "tool.completed"].includes(event.kind)).map((event) => ({ event, label: `${AgentDebugView.flowLabel(event)} · ${eventLabel(event)}` }));
      return [...calls, ...stages].sort((a, b) => a.event.seq - b.event.seq);
    }
    if (region.type === "messages") return region.edges.flatMap((edge) => [{ event: edge.sent, label: `发送 #${edge.sent.message_seq} · ${short(edge.from)} → ${short(edge.to)} · ${edge.read ? "已读" : "待接收"}` }, ...(edge.read ? [{ event: edge.read, label: `已读 #${edge.sent.message_seq} · ${short(edge.to)}` }] : [])]);
    return region.events.map((event) => ({ event, label: `${eventLabel(event)}${event.state ? " · " + (states[event.state] || event.state) : ""}` }));
  }
  function deliveryLabel(edge) {
    if (edge.read) return "已读取";
    const recipient = currentTimelineView?.lanes.find((lane) => lane.id === edge.to);
    const terminal = recipient?.events.filter((event) => ["turn.stopped", "agent.completed"].includes(event.kind)).at(-1);
    return terminal && !recipient.busy && AgentDebugView.eventTime(edge.sent) > AgentDebugView.eventTime(terminal) ? "未读 · 发送于收件 Agent 结束后" : "待接收";
  }
  function renderSelection() {
    const rows = selectedRegion ? regionRows(selectedRegion) : [], pageSize = 20;
    selectionPage = Math.max(0, Math.min(selectionPage, Math.max(0, Math.ceil(rows.length / pageSize) - 1)));
    const offset = selectionPage * pageSize;
    const visible = new Set(rows.slice(offset, offset + pageSize).map(({ event }) => event.seq));
    const routes = selectedRegion?.type === "messages" ? selectedRegion.edges.filter((edge) => visible.has(edge.sent.seq) || visible.has(edge.read?.seq)) : [];
    $("communication-routes").hidden = !routes.length;
    $("communication-routes").replaceChildren(...routes.map((edge) => {
      const row = node("div", "", "communication-route");
      row.append(node("strong", short(edge.from), "communication-source"), node("span", "→", "communication-arrow"), node("strong", short(edge.to), "communication-target"), node("small", `${duration(AgentDebugView.eventTime(edge.sent) - (selectedRegion.origin || 0))} 发送 · ${deliveryLabel(edge)}${edge.read ? " · " + duration(AgentDebugView.eventTime(edge.read) - (selectedRegion.origin || 0)) + " 读取" : ""}`));
      row.title = `${edge.from} → ${edge.to} · 消息 #${edge.sent.message_seq}`; return row;
    }));
    $("event-selection").hidden = !selectedRegion;
    $("selection-summary").textContent = selectedRegion ? `${selectedRegion.onlyIncidents ? `异常 / 拒绝 ${selectedRegion.incidentCount} 项（去重） · 原始事件` : selectedRegion.type === "tools" ? "工具调用" : selectedRegion.type === "flowUp" ? "状态分流" : selectedRegion.type === "flowDown" ? "执行过程" : selectedRegion.type === "messages" ? "通信" : selectedRegion.type === "all-events" ? "当前筛选的全部事件" : "阶段 / 结果"} · ${rows.length} 项 · ${selectedRegion.type === "all-events" ? "保留窗口" : "所选时间段"}` : "";
    $("selection-page").textContent = rows.length ? `${offset + 1}–${Math.min(rows.length, offset + pageSize)} / ${rows.length}` : "";
    $("selection-prev").disabled = offset === 0; $("selection-next").disabled = offset + pageSize >= rows.length;
    $("event-selection-list").replaceChildren(...rows.slice(offset, offset + pageSize).map(({ event, label, group }) => {
      const button = node("button", "", `selection-row ${AgentDebugView.isError(event) ? "event-error" : ""}`); button.type = "button";
      button.setAttribute("aria-pressed", String(selectedEvent?.event.seq === event.seq));
      button.append(node("span", `${((AgentDebugView.eventTime(event) - (selectedRegion.origin || 0)) / 1000).toFixed(2)} s`, "selection-time"), node("span", label));
      button.addEventListener("click", () => { selectEvent(event, group); renderSelection(); }); return button;
    }));
  }
  function selectRegion(region, origin) {
    selectedRegion = { ...region, origin }; selectionPage = 0;
    selectEvent(region.event || region.sent, ["tools", "flowDown"].includes(region.type) && region.groups.length === 1 ? region.groups[0] : null);
    renderSelection();
    queueMicrotask(() => $("event-detail").scrollIntoView?.({ block: "nearest", behavior: "auto" }));
  }
  function refreshSelection(overview, origin) {
    if (!selectedRegion?.id) return;
    let current;
    if (selectedRegion.type === "all-events") {
      const events = currentTimelineView.events.filter((event) => (!currentTimelineView.errorsOnly && !selectedRegion.onlyIncidents || AgentDebugView.isError(event)) && (!selectedRegion.onlyIncidents || !selectedRegion.session_id || event.session_id === selectedRegion.session_id));
      if (events.length) {
        const retained = events.find((event) => event.seq === selectedEvent?.event.seq);
        current = { ...selectedRegion, events, event: retained || events[0], ...(selectedRegion.onlyIncidents ? { incidentCount: overview.incidents.filter((incident) => !selectedRegion.session_id || incident.session_id === selectedRegion.session_id).length } : {}) };
        if (!retained) selectEvent(current.event);
      }
    } else if (selectedRegion.type === "messages") {
      const anchor = selectedRegion.edges[0].sent.seq;
      if (selectedRegion.allMessages) { const edges = overview.communications.flatMap((edge) => edge.edges); if (edges.length) current = { ...selectedRegion, edges, sent: edges[0].sent }; }
      else if (selectedRegion.laneId) {
        const edges = overview.communications.filter((edge) => edge.from === selectedRegion.laneId || edge.to === selectedRegion.laneId).flatMap((edge) => edge.edges);
        if (edges.length) current = { ...selectedRegion, edges, sent: edges[0].sent };
      } else current = overview.communications.find((edge) => edge.edges.some((item) => item.sent.seq === anchor));
      if (current) current = { ...current, type: "messages", event: current.sent };
    } else {
      const regions = overview.lanes.find((lane) => lane.id === selectedRegion.session_id)?.[selectedRegion.type] || [];
      const anchor = selectedRegion.type === "tools" ? selectedRegion.groups[0].id : selectedRegion.events[0].seq;
      current = regions.find((region) => selectedRegion.type === "tools" ? region.groups.some((group) => group.id === anchor) : region.events.some((event) => event.seq === anchor));
    }
    if (!current) { selectedRegion = null; expandedBatches.clear(); clearEventDetail("所选区间已不在当前筛选中，请重新选择。"); renderSelection(); return; }
    if (expandedBatches.delete(selectedRegion.id)) expandedBatches.add(current.id);
    selectedRegion = { ...current, origin };
    if (["tools", "flowDown"].includes(current.type) && selectedEvent) {
      const old = selectedEvent.event;
      const group = current.groups.find((item) => item.id === selectedEvent.groupId);
      const call = current.groups.flatMap((group) => group.calls).find((item) => item.event.seq === old.seq || item.startEvent?.seq === old.seq);
      if (call && call.event.seq !== old.seq) selectEvent(call.event, group);
      if (group) {
        // 同批次的其他调用也会更新；保留已显式读取的正文与分页状态。
        $("event-detail-summary").textContent = `工具批次 · ${group.calls.length} 个调用 · 异常 ${group.errors} · ${duration(group.end - group.start)}`;
        $("event-detail-meta").textContent = JSON.stringify({ batch_id: group.batch_id, session_id: group.session_id, start_ms: group.start, end_ms: group.end, calls: group.calls.map((item) => eventMetadata(item.event)) }, null, 2);
      }
    }
    renderSelection();
  }
  async function loadEventDetail(offset = 0) {
    if (!selectedEvent || detailBusy || !Number.isSafeInteger(selectedEvent.event.seq) || !selectedEvent.generation) return;
    const selected = selectedEvent, revision = ++detailRevision; detailAbort?.abort(); detailAbort = new AbortController(); detailBusy = true;
    $("event-detail-load").disabled = true; $("event-detail-next").disabled = true; $("event-detail-error").hidden = true;
    try {
      const query = new URLSearchParams({ generation: selected.generation, offset: String(offset), limit: "6000" });
      const response = await fetch(`/api/debug/events/${selected.event.seq}/detail?${query}`, { cache: "no-store", credentials: "omit", signal: detailAbort.signal });
      if (!response.ok) throw new Error(response.status === 404 || response.status === 409 ? "事件已过期或服务已重启，请选择当前窗口中的事件。" : `详情读取失败 (${response.status})`);
      const data = await response.json();
      if (revision !== detailRevision || selected !== selectedEvent || selected.generation !== timelineWindow?.generation) return;
      const content = typeof data.content === "string" ? data.content : JSON.stringify(data.content ?? "", null, 2);
      $("event-detail-content").textContent = content;
      const count = Array.from(content).length;
      $("event-detail-page").textContent = `${data.format || "text"} · 字符 ${count ? offset + 1 : offset}–${offset + count} / ${data.total_chars ?? count}`;
      detailNext = data.next_offset ?? null; $("event-detail-next").hidden = detailNext == null;
    } catch (error) {
      if (revision !== detailRevision || error.name === "AbortError") return;
      $("event-detail-error").textContent = error.message || "详情读取失败，请重试。"; $("event-detail-error").hidden = false;
    } finally {
      if (revision === detailRevision) { detailBusy = false; $("event-detail-load").disabled = false; $("event-detail-next").disabled = false; }
    }
  }
  function svgNode(tag, attrs = {}, text = "") {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value))); element.textContent = text; return element;
  }
  function svgAction(element, event, label) {
    element.setAttribute("tabindex", "0"); element.setAttribute("role", "button"); element.setAttribute("aria-label", label);
    element.setAttribute("data-event-key", `svg-${event.seq}-${label}`);
    element.addEventListener("click", () => selectEvent(event));
    element.addEventListener("keydown", (key) => { if (key.key === "Enter" || key.key === " ") { key.preventDefault(); selectEvent(event); } });
  }
  function chart(view, width, x, kind) {
    const data = AgentDebugView.metrics(view, $("token-mode").value || "request"), height = 138, top = 16, bottom = 106;
    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": kind === "context" ? "上下文百分比，按采样时间" : "Token input 和 output 用量，provider 实际报告，estimated 估算" });
    const points = kind === "context" ? data.context : data.tokens;
    const max = kind === "context" ? Math.max(100, ...points.map((point) => point.value || 0)) : Math.max(1, ...points.flatMap((point) => [point.input || 0, point.output || 0]));
    const y = (value) => bottom - (value / max) * (bottom - top);
    [0, .5, 1].forEach((ratio) => {
      const yy = y(max * ratio); svg.append(svgNode("line", { x1: 138, x2: width - 20, y1: yy, y2: yy, class: "chart-grid" }), svgNode("text", { x: 125, y: yy + 4, "text-anchor": "end", class: "axis-label" }, `${Math.round(max * ratio).toLocaleString()}${kind === "context" ? "%" : ""}`));
    });
    if (!points.length) svg.append(svgNode("text", { x: 150, y: 60, class: "axis-label" }, kind === "context" ? "尚无上下文采样" : "尚无用量；未知值不会绘为 0"));
    const sessions = [...new Set(points.map((point) => point.session_id))];
    sessions.forEach((session) => {
      const samples = points.filter((point) => point.session_id === session), color = AgentDebugView.agentColor(session);
      (kind === "context" ? ["value"] : ["input", "output"]).forEach((field) => {
        let previous = null;
        samples.forEach((point) => {
          const value = point[field], source = kind === "context" ? "context" : point.source;
          if (value == null) { previous = null; return; }
          if (previous && source === previous.source) svg.append(svgNode("line", { x1: x(previous.point.time), y1: y(previous.point[field]), x2: x(point.time), y2: y(value), stroke: color, "stroke-width": 1.7, "stroke-dasharray": field === "output" ? "5 4" : source === "estimated" ? "2 3" : "", class: "metric-line" }));
          const cx = x(point.time), cy = y(value), mark = source === "estimated" ? svgNode("path", { d: `M${cx},${cy - 4}l4,4 -4,4 -4,-4Z`, stroke: color, fill: "#1b202a" }) : svgNode("circle", { cx, cy, r: 3.5, fill: color });
          const label = `Agent ${short(session)} · ${field} ${Math.round(value).toLocaleString()}${kind === "context" ? "%" : " tokens"} · ${point.reason || sourceLabels[source] || "采样"}`;
          mark.append(svgNode("title", {}, label)); svgAction(mark, point.event, label); svg.append(mark); previous = { point, source };
          if (kind === "context" && /compact/.test(point.reason)) svg.append(svgNode("text", { x: Math.min(width - 48, cx + 5), y: cy - 7, class: "axis-label" }, /before/.test(point.reason) ? "压缩前" : /after/.test(point.reason) ? "压缩后" : "压缩"));
        });
      });
    });
    return svg;
  }
  function pinTimelineLabels() {
    if (timelineLabels) timelineLabels.style.transform = `translateX(${$("timeline-scroll").scrollLeft}px)`;
  }
  function renderTimeline(selection) {
    if (!document.createElementNS) return;
    const scroll = $("timeline-scroll"), focusKey = document.activeElement?.dataset?.eventKey, oldScroll = scroll.scrollLeft;
    const minimumWidth = window.matchMedia?.("(max-width: 780px)").matches ? 820 : 0;
    const view = AgentDebugView.timeline(snapshot, selection, $("errors-only").checked), width = Math.round(Math.max(minimumWidth, scroll.clientWidth || 820) * zoom);
    currentTimelineView = view;
    const scale = AgentDebugView.timeScale(view, width, $("compress-idle").checked), board = AgentDebugView.storyboard(view, width, scale, expandedAgents);
    const eventSeqs = new Set((snapshot.trace.timeline || []).map((event) => event.seq));
    if (selectedRegion && regionRows(selectedRegion).some(({ event }) => !eventSeqs.has(event.seq))) { selectedRegion = null; clearEventDetail("所选区间已移出保留窗口，请重新选择。"); renderSelection(); }
    refreshSelection(board, view.domain[0]);
    const [min, max] = view.domain, plotWidth = width - 170, x = scale.position;
    $("timeline-content").style.width = `${width}px`;
    const axis = node("div", "", "axis-track"); axis.style.width = `${width}px`;
    const ticks = Math.min(24, Math.ceil(6 * zoom));
    for (let index = 0; index <= ticks; index++) { const position = 140 + plotWidth * index / ticks, tick = node("span", elapsedLabel(scale.timeAt(position) - min), `axis-tick${index === ticks ? " axis-end" : ""}`); tick.style.left = `${position}px`; axis.append(tick); }
    scale.gaps.forEach((gap) => { const label = node("span", `⋯ ${elapsedLabel(gap.duration)} 空闲`, "idle-gap-label"); label.style.left = `${gap.left}px`; label.style.width = `${gap.right - gap.left}px`; label.style.top = "30px"; axis.append(label); });
    $("timeline-axis").replaceChildren(axis);
    const layout = new Map(); let extra = 0;
    board.lanes.forEach((lane) => { const spans = $("show-module-spans").checked ? view.spans.filter((span) => span.session_id === lane.id) : [], added = Math.min(5, spans.length) * 21; layout.set(lane.id, { ...lane, originalTop: lane.top, top: lane.top + extra, lineY: lane.lineY + extra, height: lane.height + added, spans }); extra += added; });
    const height = board.height + extra, scene = node("div", "", "timeline-scene"); scene.style.height = `${height}px`; scene.setAttribute("data-motion", paused || document.hidden ? "paused" : "live");
    timelineLabels = node("div", "", "timeline-labels"); scene.append(timelineLabels);
    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "timeline-connections", role: "group", "aria-label": "主流程与共享分流汇流母线" }), defs = svgNode("defs");
    board.lanes.forEach((lane, index) => { const marker = svgNode("marker", { id: `message-arrow-${index}`, markerWidth: 7, markerHeight: 7, refX: 6, refY: 3.5, orient: "auto", markerUnits: "userSpaceOnUse" }); marker.append(svgNode("path", { d: "M1 1L6 3.5L1 6", fill: "none", stroke: lane.color, "stroke-width": 1 })); defs.append(marker); }); svg.append(defs); scene.append(svg);
    function button(text, xx, yy, css, key, color, click, buttonWidth = null) {
      const value = node("button", text, css); value.type = "button"; value.style.left = `${xx}px`; value.style.top = `${yy}px`; if (buttonWidth != null) value.style.width = `${buttonWidth}px`; value.style.setProperty("--agent-color", color); value.dataset.eventKey = key; value.addEventListener("click", click); scene.append(value); return value;
    }
    function inspectErrors(laneId = "", event = null) {
      const events = view.events.filter((item) => AgentDebugView.isError(item) && (!laneId || item.session_id === laneId));
      if (events.length) selectRegion({ id: `incidents-${laneId || "all"}`, type: "all-events", onlyIncidents: true, incidentCount: board.incidents.filter((incident) => !laneId || incident.session_id === laneId).length, session_id: laneId, events, event: event || events[0] }, min);
    }
    function inspectMessages(laneId = "") {
      const edges = view.communications.filter((edge) => !laneId || edge.from === laneId || edge.to === laneId);
      if (edges.length) { selectRegion({ id: `messages-${laneId || "all"}`, type: "messages", laneId, allMessages: !laneId, edges, event: edges[0].sent, sent: edges[0].sent }, min); renderTimeline(selection); }
    }
    const totals = board.summary, summary = [node("span", `${totals.agents} Agent · ${board.collaborations.length} 组协作`, "story-total"), node("span", `Compact ${totals.compactions}`, "story-total")];
    const anomaly = node("button", `工具错误 ${totals.tool_errors} · 拒绝 ${totals.rejections}${totals.other_errors ? ` · 其它异常 ${totals.other_errors}` : ""}`, `story-incidents${board.incidents.length ? " has-incidents" : ""}`); anomaly.type = "button"; anomaly.disabled = !board.incidents.length; anomaly.addEventListener("click", () => inspectErrors()); summary.push(anomaly);
    const messages = node("button", `通信 ${view.communications.length} →`, "story-messages"); messages.type = "button"; messages.disabled = !view.communications.length; messages.addEventListener("click", () => inspectMessages()); summary.push(messages); $("timeline-summary").replaceChildren(...summary);
    board.collaborations.forEach((group) => {
      const parent = layout.get(group.parentId), members = group.children.map((lane) => layout.get(lane.id)), closed = members.filter((lane) => lane.closed), lastY = Math.max(...members.map((lane) => lane.lineY));
      const top = group.top + board.lanes.filter((lane) => lane.top < group.top).reduce((sum, lane) => sum + Math.min(5, layout.get(lane.id).spans.length) * 21, 0);
      svg.append(svgNode("path", { d: `M${group.left} ${parent.lineY}V${lastY}`, fill: "none", stroke: parent.color, "stroke-width": 1, class: "agent-fork-bus" }));
      if (closed.length) svg.append(svgNode("path", { d: `M${group.right} ${Math.max(...closed.map((lane) => lane.lineY))}V${parent.lineY}`, fill: "none", stroke: parent.color, "stroke-width": 1, class: "agent-join-bus", "marker-end": `url(#message-arrow-${board.lanes.findIndex((lane) => lane.id === parent.id)})` }));
      const heading = node("div", `协作分流 · ${members.length} 子 Agent · ${closed.length} 已结束 · ${members.filter((lane) => lane.active).length} 执行中`, "collaboration-heading"); heading.style.left = `${group.left + 16}px`; heading.style.top = `${top + 8}px`; heading.style.maxWidth = `${Math.max(40, group.right - group.left - 28)}px`; scene.append(heading);
      members.forEach((lane) => {
        const startX = x(lane.start ?? min), endX = x(lane.end), arrow = `url(#message-arrow-${board.lanes.findIndex((item) => item.id === lane.id)})`;
        const fork = svgNode("path", { d: `M${group.left} ${lane.lineY}H${startX}`, fill: "none", stroke: lane.color, "stroke-width": 1, class: "agent-branch", "marker-end": arrow }); fork.append(svgNode("title", {}, `分流连接 · ${short(parent.id)} → ${short(lane.id)} · 开始 ${elapsedLabel((lane.start ?? min) - min)}；连接段非耗时`)); svg.append(fork);
        if (lane.closed) { const join = svgNode("path", { d: `M${endX} ${lane.lineY}H${group.right}`, fill: "none", stroke: lane.color, "stroke-width": 1, class: `agent-join spine-${lane.spineState}`, "marker-end": arrow }); join.append(svgNode("title", {}, `汇流连接 · ${short(lane.id)} → ${short(parent.id)} · 结束 ${elapsedLabel(lane.end - min)}；连接段非耗时`)); svg.append(join); }
      });
    });
    board.lanes.forEach((lane) => {
      const item = layout.get(lane.id), color = lane.color, parent = layout.get(lane.parentId), start = lane.start ?? min, end = x(lane.end), arrow = `url(#message-arrow-${board.lanes.indexOf(lane)})`;
      if (!parent) svg.append(svgNode("line", { x1: 140, x2: width - 30, y1: item.lineY, y2: item.lineY, stroke: color, "stroke-width": 1, class: "agent-track", "marker-end": arrow }));
      svg.append(svgNode("line", { x1: x(start), x2: end, y1: item.lineY, y2: item.lineY, stroke: color, "stroke-width": 1, class: `agent-lifetime${parent ? " agent-child-lifetime" : ""} spine-${item.spineState}` }));
      const motionOffset = `-${(performance.now() % 1800).toFixed(1)}ms`;
      if (lane.active) { const flow = svgNode("line", { x1: Math.max(x(start), end - 56), x2: end, y1: item.lineY, y2: item.lineY, stroke: color, "stroke-width": 1, "stroke-dasharray": "2 10", class: "agent-flow" }); flow.style.animationDelay = motionOffset; svg.append(flow); }
      const cursor = svgNode("circle", { cx: end, cy: item.lineY, r: lane.active ? 2.5 : 2, fill: color, class: lane.active ? "agent-cursor" : "agent-end" });
      if (lane.active) cursor.style.animationDelay = motionOffset;
      svg.append(svgNode("circle", { cx: x(start), cy: item.lineY, r: 2, fill: color, class: "agent-start" }), cursor);
      const label = node("button", "", `lane-label${parent ? " child" : ""}`); label.type = "button"; label.style.top = `${item.top + (lane.expanded ? 78 : 5)}px`; label.style.color = color;
      label.append(node("strong", `${parent ? "↳ 子" : "主"} Agent ${short(lane.id)}`), node("small", `${item.stateLabel}${parent ? lane.expanded ? " · 点击收起" : " · 点击展开行为" : " · MAIN"}`));
      label.dataset.eventKey = `lane-${lane.id}`; label.title = `${lane.id} · ${parent ? "展开/收起子 Agent 的完整过程" : "根 Agent 主流程"}`;
      label.setAttribute("aria-expanded", String(lane.expanded)); label.setAttribute("aria-label", `${parent ? "子" : "主"} Agent ${lane.id}${parent ? "，展开或收起行为" : "主流程"}`);
      label.addEventListener("click", () => { if (parent) { if (expandedAgents.has(lane.id)) expandedAgents.delete(lane.id); else expandedAgents.add(lane.id); } focusedAgent = lane.id; renderTimeline(selection); }); timelineLabels.append(label);
      const related = view.communications.filter((edge) => edge.from === lane.id || edge.to === lane.id), summary = node("div", "", "lane-summary"); summary.style.top = `${item.top + (lane.expanded ? 128 : 50)}px`;
      const count = lane.tools.reduce((sum, region) => sum + region.groups.reduce((n, group) => n + group.calls.length, 0), 0); summary.append(node("span", `${count} 次工具`));
      if (related.length) { const value = node("button", `↔ ${related.length}`, "message-summary"); value.type = "button"; value.setAttribute("aria-label", `Agent ${lane.id} 的 ${related.length} 条通信`); value.addEventListener("click", () => inspectMessages(lane.id)); summary.append(value); }
      if (lane.incidents.length) { const value = node("button", `! ${lane.incidents.length}`, "incident-summary"); value.type = "button"; value.setAttribute("aria-label", `Agent ${lane.id} 的异常与拒绝`); value.addEventListener("click", () => inspectErrors(lane.id)); summary.append(value); } timelineLabels.append(summary);
      lane.incidents.forEach((incident) => { const dot = svgNode("circle", { cx: x(AgentDebugView.eventTime(incident.event)), cy: item.lineY, r: 3.5, fill: incident.type === "rejected" ? "#dfb674" : "#ec9292", class: "incident-marker", tabindex: 0, role: "button", "aria-label": `${incident.type === "rejected" ? "拒绝" : "异常"} · Agent ${short(lane.id)}` }); dot.append(svgNode("title", {}, `${incident.event.tool_name || incident.event.module || "事件"} · ${incident.event.state} · ${elapsedLabel(AgentDebugView.eventTime(incident.event) - min)}`)); const click = () => inspectErrors(lane.id, incident.event); dot.addEventListener("click", click); dot.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) click(); }); svg.append(dot); });
      if (lane.expanded) {
        ["up", "down"].forEach((side) => lane[side === "up" ? "flowUp" : "flowDown"].forEach((region) => {
          const up = side === "up", center = region.anchorX, tip = item.top + (up ? 66 : 156);
          svg.append(svgNode("path", { d: `M${center} ${item.lineY}V${tip}`, fill: "none", stroke: color, "stroke-width": 1, "marker-end": arrow, class: `behavior-arrow behavior-arrow-${side}` }));
          const value = button("", region.labelX, item.top + (up ? 20 : 168), `${!up && region.toolNames.length ? "tool-batch" : "timeline-event"} flow-branch flow-branch-${side}${region.errors ? " event-error" : ""}`, region.id, color, () => { selectRegion(region, min); renderTimeline(selection); }, region.labelWidth);
          value.append(node("span", region.label, "flow-title"), node("small", region.detail, "flow-detail")); value.title = `${region.label} · ${region.detail} · ${region.toolNames.join(" / ")} · ${duration(region.start - min)}–${duration(region.end - min)} · 点击查看真实事件`;
          value.setAttribute("aria-pressed", String(selectedRegion?.id === region.id)); value.setAttribute("aria-expanded", String(selectedRegion?.id === region.id)); value.setAttribute("aria-label", value.title); value.setAttribute("aria-controls", "event-selection");
        }));
      }
      item.spans.slice(0, 5).forEach((span, index) => { const value = button(span.label, x(span.start), item.top + lane.height + index * 21, "module-interval", span.id, color, () => selectEvent(span.event), Math.max(12, x(span.end) - x(span.start))); value.title = `${span.label} · ${duration(span.end - span.start)}`; });
    });
    if (!view.lanes.length) scene.append(node("p", "当前筛选下暂无时间线事件。", "timeline-empty notice"));
    $("timeline-lanes").replaceChildren(scene); $("context-chart").replaceChildren(chart(view, width, x, "context")); $("token-chart").replaceChildren(chart(view, width, x, "tokens"));
    $("token-chart-title").replaceChildren(node("span", "Token 用量"), node("small", $("token-mode").value === "cumulative" ? "每 Agent · 当前窗口已知累计 input / output" : "每 Agent · 按请求 input / output"));
    $("timeline-status").textContent = `${view.events.length} 事件 · 主线为整体过程，子支线仅显示实际执行区间 · 母线只表示分流 / 汇流，不代表耗时 · 点击子 Agent 展开行为，通信在下方逐条查看 · 真实经过 ${elapsedLabel(max - min)}${scale.compressed ? ` · 已折叠 ${scale.gaps.length} 段空闲` : ""}`;
    scroll.scrollLeft = $("follow-latest").checked ? Math.max(0, width - scroll.clientWidth) : oldScroll; pinTimelineLabels();
    if (focusKey) $("timeline-content").querySelectorAll("[data-event-key]").forEach((element) => { if (element.dataset.eventKey === focusKey) element.focus({ preventScroll: true }); });
  }
  function render() {
    if (!snapshot) return;
    const selection = $("agent-filter").value || (!navigationApplied ? navigation.sessionId : ""), filtered = AgentDebugView.rows(snapshot, selection, $("errors-only").checked);
    $("agents-count").textContent = snapshot.runtime.active_agents;
    $("models-count").textContent = snapshot.runtime.active_model_calls;
    $("tools-count").textContent = snapshot.runtime.active_tools.length;
    $("span-count").textContent = snapshot.trace.active.length;
    const choices = [node("option", "全部 Agent")]; choices[0].value = "";
    AgentDebugView.agents(snapshot).forEach((agent) => { const option = node("option", `${agent.parent_id ? "↳ " : ""}${short(agent.id)}`); option.value = agent.id; choices.push(option); });
    if (selection && !choices.some((item) => item.value === selection)) { const option = node("option", short(selection)); option.value = selection; choices.push(option); }
    $("agent-filter").replaceChildren(...choices); $("agent-filter").value = choices.some((item) => item.value === selection) ? selection : "";
    const cards = AgentDebugView.agents(snapshot).map((agent) => {
      const card = node("div", "", "agent" + (agent.parent_id ? " child" : ""));
      if (card.style) card.style.setProperty("--agent-color", AgentDebugView.agentColor(agent.id));
      card.append(node("strong", `${agent.parent_id ? "↳ 子 Agent" : "根 Agent"} · ${short(agent.id)}`));
      card.title = agent.id;
      card.append(node("p", `${states[agent.status] || "就绪"}${agent.confirmation ? " · 待审批" : ""}`, agent.status));
      if (agent.parent_id) card.append(node("p", `父 Agent ${short(agent.parent_id)}`));
      const activities = snapshot.trace.active.filter((row) => row.session_id === agent.id).map((row) => row.name || row.module);
      card.append(node("p", activities.length ? activities.join(" → ") : "当前无模块活动"));
      snapshot.runtime.active_tools.filter((row) => row.session_id === agent.id).forEach((row) => card.append(node("p", `${row.name} · ${states[row.state] || "执行中"}`)));
      if (typeof agent.context_status?.utilization === "number") card.append(node("p", `工作预算 ${(agent.context_status.utilization * 100).toFixed(1)}% · 监督 ${agent.context_status.supervisor || "未检查"}`));
      const archiveButton = node("button", "上下文归档", "archive-open"); archiveButton.type = "button"; archiveButton.addEventListener("click", () => openArchive(agent.id)); card.append(archiveButton);
      return card;
    });
    $("agents").replaceChildren(...(cards.length ? cards : [node("p", "尚无会话；返回工作区启动任务。", "notice")]));
    $("modules").replaceChildren(...AgentDebugView.modules(snapshot, selection).map((item) => {
      const box = node("div", "", "module"); box.append(node("b", `${item.label} · ${item.active} 活动`));
      box.append(node("small", `保留 ${item.completed} 次 · 异常 ${item.errors} · 最长 ${duration(item.max_ms)}`)); return box;
    }));
    $("active").replaceChildren(...(filtered.active.length ? filtered.active.map((row) => {
      const card = node("div", "", "span" + (row.parent_id ? " child" : ""));
      card.append(node("span", "", "pulse"), node("span", `${row.module}${row.name ? " · " + row.name : ""} · ${duration(row.duration_ms)}`, "state"));
      card.append(node("small", `Agent ${short(row.session_id)} · #${row.id}${row.parent_id ? " ← #" + row.parent_id : ""}${row.activity ? " · " + (activities[row.activity] || "") : ""}`)); return card;
    }) : [node("p", "当前无活动调用", "notice")]));
    $("events").replaceChildren(...filtered.events.map((row) => {
      const tr = node("tr", "");
      const finished = new Date(row.started_at * 1000 + row.duration_ms);
      [short(row.session_id), row.module + (row.name ? " · " + row.name : ""), (states[row.state] || row.state) + (row.error_type ? " · " + row.error_type : ""), duration(row.duration_ms), `#${row.id}${row.parent_id ? " ← #" + row.parent_id : ""}`, finished.toLocaleTimeString()].forEach((text, i) => tr.append(node("td", text, i === 2 ? row.state : ""))); return tr;
    }));
    renderTimeline(selection);
    $("retention").textContent = `本地窗口 ${snapshot.trace.timeline?.length ?? snapshot.trace.events.length} 条；服务容量 ${snapshot.trace.capacity}，已淘汰或忽略 ${snapshot.trace.dropped} 条。Agent 未展示 ${snapshot.agents_omitted} 个。${timelineWindow?.resetReason ? "窗口已重建：" + ({ gap: "保留窗口已变化", generation: "服务已重启", reset: "服务请求重置" }[timelineWindow.resetReason]) + "。" : ""}`;
    $("export").disabled = false;
    if (!navigationApplied) { navigationApplied = true; if (navigation.openContext) openArchive(navigation.sessionId); }
  }
  async function poll() {
    if (polling) return;
    polling = true;
    if (!paused && !document.hidden && !stopped) {
      try {
        const query = timelineWindow?.generation ? new URLSearchParams({ cursor: String(timelineWindow.cursor), generation: timelineWindow.generation }) : null;
        const response = await fetch(query ? `/api/debug?${query}` : "/api/debug", { cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(4000) });
        if (!response.ok) { if (response.status === 404 || response.status === 401 || response.status === 403) stopped = true; throw new Error(response.status === 404 ? "DEBUG 未启用，请设置 DEBUG=true 后重启服务" : `调试接口不可用 (${response.status})`); }
        let next = await response.json(), resetReason = next.trace.gap ? "gap" : next.trace.reset ? "reset" : timelineWindow && timelineWindow.generation !== next.trace.generation ? "generation" : "";
        if (resetReason && timelineWindow) {
          // A fresh request makes the retained window authoritative after any discontinuity.
          const full = await fetch("/api/debug", { cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(4000) });
          if (!full.ok) throw new Error(`时间线窗口重建失败 (${full.status})`);
          next = await full.json();
        }
        if (paused || document.hidden) { polling = false; if (!stopped) setTimeout(poll, 500); return; }
        if (Array.isArray(next.trace.timeline)) {
          if (resetReason) { timelineWindow = null; clearEventDetail("时间线窗口已重建，请重新选择事件。"); expandedBatches.clear(); selectedRegion = null; renderSelection(); }
          timelineWindow = AgentDebugView.mergeTimeline(timelineWindow, next.trace);
          if (resetReason) timelineWindow.resetReason = resetReason;
          next.trace.timeline = timelineWindow.timeline;
        }
        snapshot = next; render(); $("connection").textContent = "实时 · 500 ms";
      } catch (error) { $("connection").textContent = error.message || "服务连接中断，稍后重试"; }
    }
    polling = false;
    if (!stopped) setTimeout(poll, 500);
  }
  $("pause").addEventListener("click", () => { paused = !paused; $("pause").textContent = paused ? "继续刷新" : "暂停刷新"; $("pause").setAttribute("aria-pressed", String(paused)); $("timeline-content").setAttribute("data-motion", paused ? "paused" : "live"); $("active").setAttribute("data-motion", paused ? "paused" : "live"); $("connection").textContent = paused ? "画面已暂停 · Agent 继续运行" : "重新连接…"; });
  document.addEventListener?.("visibilitychange", () => { const motion = paused || document.hidden ? "paused" : "live"; $("timeline-content").setAttribute("data-motion", motion); $("active").setAttribute("data-motion", motion); });
  $("agent-filter").addEventListener("change", () => { if ($("context-archive").open) { if ($("agent-filter").value) openArchive($("agent-filter").value); else { resetArchive(); $("context-archive").close(); } } render(); }); $("errors-only").addEventListener("change", render);
  function setZoom(value) {
    const scroll = $("timeline-scroll"), center = (scroll.scrollLeft + scroll.clientWidth / 2) / zoom;
    zoom = Math.max(1, Math.min(12, value)); $("timeline-zoom").value = String(zoom); render();
    if (!$("follow-latest").checked) scroll.scrollLeft = center * zoom - scroll.clientWidth / 2;
    pinTimelineLabels();
  }
  $("timeline-zoom").addEventListener("input", () => setZoom(Number($("timeline-zoom").value)));
  $("zoom-in").addEventListener("click", () => setZoom(zoom + .5)); $("zoom-out").addEventListener("click", () => setZoom(zoom - .5));
  $("follow-latest").addEventListener("change", render);
  $("latest").addEventListener("click", () => { $("follow-latest").checked = true; render(); });
  $("token-mode").addEventListener("change", render);
  $("show-module-spans").addEventListener("change", render);
  $("show-connections").addEventListener("change", () => {
    if ($("show-connections").checked && currentTimelineView?.communications.length) {
      const edges = currentTimelineView.communications;
      selectRegion({ id: "messages-all", type: "messages", allMessages: true, edges, event: edges[0].sent, sent: edges[0].sent }, currentTimelineView.domain[0]);
    }
    render();
  });
  $("compress-idle").addEventListener("change", render);
  $("timeline-all-events").addEventListener("click", () => {
    if (!currentTimelineView) return;
    const events = currentTimelineView.events.filter((event) => !currentTimelineView.errorsOnly || AgentDebugView.isError(event));
    if (!events.length) { clearEventDetail("当前筛选没有保留事件。"); return; }
    expandedBatches.clear(); selectRegion({ id: "all-events", type: "all-events", events, event: events[0] }, currentTimelineView.domain[0]); renderTimeline($("agent-filter").value);
  });
  $("timeline-scroll").addEventListener("scroll", pinTimelineLabels, { passive: true });
  window.addEventListener?.("resize", () => { if (snapshot) renderTimeline($("agent-filter").value); });
  $("event-detail-load").addEventListener("click", () => loadEventDetail(0));
  $("event-detail-next").addEventListener("click", () => { if (detailNext != null) loadEventDetail(detailNext); });
  $("selection-prev").addEventListener("click", () => { selectionPage--; renderSelection(); });
  $("selection-next").addEventListener("click", () => { selectionPage++; renderSelection(); });
  $("selection-close").addEventListener("click", () => { selectedRegion = null; expandedBatches.clear(); clearEventDetail(); renderSelection(); render(); });
  $("timeline-scroll").addEventListener("keydown", (event) => {
    if (event.target !== $("timeline-scroll") || event.ctrlKey || event.metaKey || event.altKey) return;
    const scroll = $("timeline-scroll");
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); $("follow-latest").checked = false; scroll.scrollLeft += event.key === "ArrowLeft" ? -100 : 100; }
    else if (event.key === "Home" || event.key === "End") { event.preventDefault(); $("follow-latest").checked = event.key === "End"; scroll.scrollLeft = event.key === "Home" ? 0 : scroll.scrollWidth; }
    else if (event.key === "+" || event.key === "=") { event.preventDefault(); setZoom(zoom + .5); }
    else if (event.key === "-") { event.preventDefault(); setZoom(zoom - .5); }
  });
  let dragging = null;
  $("timeline-scroll").addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("button, a, input, select, [role=button]")) return;
    dragging = { pointer: event.pointerId, start: event.clientX, scroll: $("timeline-scroll").scrollLeft };
    $("timeline-scroll").setPointerCapture(event.pointerId);
  });
  $("timeline-scroll").addEventListener("pointermove", (event) => {
    if (!dragging || event.pointerId !== dragging.pointer) return;
    if (Math.abs(event.clientX - dragging.start) > 4) $("follow-latest").checked = false;
    $("timeline-scroll").scrollLeft = dragging.scroll - (event.clientX - dragging.start);
  });
  $("timeline-scroll").addEventListener("pointerup", () => { dragging = null; }); $("timeline-scroll").addEventListener("pointercancel", () => { dragging = null; });
  $("timeline-scroll").addEventListener("wheel", (event) => { if (Math.abs(event.deltaX) > 0 || event.shiftKey) $("follow-latest").checked = false; }, { passive: true });
  $("export").addEventListener("click", () => {
    if (!snapshot) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" }));
    const link = node("a", ""); link.href = url; link.download = `agent-debug-${Date.now()}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("archive-close").addEventListener("click", () => { resetArchive(); $("context-archive").close(); });
  $("context-archive").addEventListener("close", () => { if (!$("context-archive").open) resetArchive(); });
  $("context-archive").addEventListener("cancel", resetArchive);
  $("archive-prev").addEventListener("click", () => loadArchiveDirectory(Math.max(0, archive.directoryOffset - 20)));
  $("archive-next").addEventListener("click", () => { if (archive.nextDirectoryOffset != null) loadArchiveDirectory(archive.nextDirectoryOffset); });
  $("archive-refresh").addEventListener("click", () => loadArchiveDirectory(archive.directoryOffset));
  $("archive-read").addEventListener("click", () => loadArchiveRaw(0));
  $("archive-raw-next").addEventListener("click", () => { if (archive.nextRawOffset != null) loadArchiveRaw(archive.nextRawOffset); });
  poll();
})();
