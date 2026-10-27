"use strict";
// 纯视图计算，可独立做 Node 回归；不把嵌套耗时相加。
globalThis.AgentDebugView = (() => {
  const labels = { agent: "执行 Agent", model: "模型推理", tool: "工具", context: "上下文", supervisor: "监督 Agent", permission: "权限确认", persistence: "持久化", memory: "记忆", communication: "Agent 通信", plan: "任务计划", rag: "RAG 检索" };
  function subtreeIds(parents, id) {
    const children = new Map(), ids = new Set(), queue = [id];
    parents.forEach((parentId, childId) => { if (!children.has(parentId)) children.set(parentId, []); children.get(parentId).push(childId); });
    for (let index = 0; index < queue.length; index++) { const current = queue[index]; if (ids.has(current)) continue; ids.add(current); queue.push(...(children.get(current) || [])); }
    return ids;
  }
  function rows(snapshot, selected = "", errorsOnly = false) {
    const parents = new Map((snapshot.agents || []).map((agent) => [agent.id, agent.parent_id || null]));
    (snapshot.trace?.timeline || []).filter((event) => event.kind === "agent.created").forEach((event) => parents.set(event.session_id, event.parent_session_id || null));
    const selectedIds = selected ? subtreeIds(parents, selected) : null;
    const keep = (row) => !selectedIds || selectedIds.has(row.session_id);
    return {
      active: (snapshot.trace?.active || []).filter(keep).sort((a, b) => a.id - b.id),
      events: (snapshot.trace?.events || []).filter(keep).filter((row) => !errorsOnly || row.state !== "completed").slice().reverse(),
    };
  }
  function modules(snapshot, selected = "") {
    const current = rows(snapshot, selected), result = new Map();
    Object.entries(labels).forEach(([key, label]) => result.set(key, { key, label, active: 0, completed: 0, errors: 0, max_ms: 0 }));
    current.active.forEach((row) => { const item = result.get(row.module.split(".")[0]); if (item) item.active += 1; });
    current.events.forEach((row) => {
      const item = result.get(row.module.split(".")[0]);
      if (!item) return;
      item.completed += 1; if (row.state !== "completed") item.errors += 1;
      item.max_ms = Math.max(item.max_ms, row.duration_ms || 0);
    });
    return [...result.values()];
  }
  function agents(snapshot) {
    const all = snapshot.agents || [], byId = new Map(all.map((row) => [row.id, row])), result = [], seen = new Set();
    function visit(row) { if (seen.has(row.id)) return; seen.add(row.id); result.push(row); all.filter((child) => child.parent_id === row.id).forEach(visit); }
    all.filter((row) => !row.parent_id || !byId.has(row.parent_id)).forEach(visit);
    all.forEach(visit); return result;
  }
  // Keep assigned colors stable as Agents appear, rather than reindexing a small palette.
  const colorAssignments = new Map();
  const firstAgentHues = [214, 38, 300, 140, 265, 2, 184, 79];
  const upperKinds = new Set(["model.final", "turn.stopped", "phase.changed", "plan.updated"]);
  const errors = new Set(["error", "failed", "timeout", "cancelled", "rejected", "checkpoint"]);
  const isError = (event) => errors.has(event.state) || Boolean(event.error_type) || (event.kind === "turn.stopped" && event.reason && event.reason !== "finished");
  function agentColor(id) {
    const key = String(id || "unknown"); if (colorAssignments.has(key)) return colorAssignments.get(key).css;
    let hash = 0; for (const char of key) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0;
    const lightness = [72, 57, 84][Math.floor(colorAssignments.size / 12) % 3];
    let hue = hash % 360, bestHue = hue, bestDistance = -1;
    for (let attempt = 0; attempt < 36; attempt++) {
      const candidate = (hue + attempt * 137.508) % 360;
      let nearest = 360;
      colorAssignments.forEach((assigned) => {
        if (Math.abs(assigned.lightness - lightness) > 10) return;
        const distance = Math.abs(candidate - assigned.hue); nearest = Math.min(nearest, distance, 360 - distance);
      });
      if (nearest > bestDistance) { bestDistance = nearest; bestHue = candidate; }
      if (nearest >= 27) break;
    }
    hue = colorAssignments.size < firstAgentHues.length ? firstAgentHues[colorAssignments.size] : Math.round(bestHue * 10) / 10;
    const css = `hsl(${hue} 72% ${lightness}%)`;
    if (colorAssignments.size < 10000) colorAssignments.set(key, { hue, lightness, css });
    return css;
  }
  function mergeTimeline(previous, trace, limit = 10000) {
    const generationChanged = previous && previous.generation !== trace.generation;
    const resetReason = trace.gap ? "gap" : trace.reset ? "reset" : generationChanged ? "generation" : "";
    const retained = previous && trace.incremental && !resetReason ? previous.timeline : [];
    const events = new Map(retained.map((event) => [event.seq, event]));
    (trace.timeline || []).forEach((event) => { if (Number.isSafeInteger(event.seq)) events.set(event.seq, event); });
    return { generation: trace.generation, cursor: trace.cursor ?? previous?.cursor ?? 0,
      timeline: [...events.values()].sort((a, b) => a.seq - b.seq).slice(-Math.max(1, Math.min(10000, limit))), resetReason };
  }
  function eventTime(event) {
    if (Number.isFinite(event.elapsed_ms)) return event.elapsed_ms;
    if (typeof event.ts === "number") return event.ts * 1000;
    const parsed = Date.parse(event.ts); return Number.isFinite(parsed) ? parsed : null;
  }
  function toolGeometry(call, domain, width) {
    const x = (time) => 140 + ((time - domain[0]) / (domain[1] - domain[0])) * (width - 170);
    const startX = x(call.start), endX = x(call.end), labelWidth = 180;
    return { startX, endX, spanWidth: Math.max(0, endX - startX), labelWidth, labelX: Math.max(140, Math.min(startX, width - labelWidth - 10)) };
  }
  const phaseNames = { model: "模型推理", model_wait: "等待模型", context_prepare: "准备上下文", compaction: "压缩上下文", tools: "工具执行", continuation: "继续执行", recovery: "连接恢复", confirmation: "等待确认", stopping: "停止中", planning: "更新计划", complete: "完成" };
  function stopLabel(event) {
    if (event.reason === "model_connection") return "连接中断";
    if (event.reason === "context_budget") return "上下文暂停";
    if (event.reason === "cost_or_rate_budget") return "预算暂停";
    if (event.state === "checkpoint" || /checkpoint|budget|limit|pause/.test(event.reason || "")) return "阶段暂停";
    if (event.state === "cancelled" || /cancel|user_stop/.test(event.reason || "")) return "已停止";
    if (event.state === "timeout" || /timeout/.test(event.reason || "")) return "等待超时";
    if (isError(event) && event.state !== "rejected") return "执行失败";
    return event.state === "rejected" ? "未获授权" : "执行完成";
  }
  function branchLabel(event) {
    if (event.kind === "turn.stopped" || event.kind === "agent.completed") return stopLabel(event);
    if (event.kind === "phase.changed") return phaseNames[event.phase] || "阶段切换";
    if (event.kind === "model.final") return "输出回复";
    if (event.kind === "context.compacted") return "上下文已压缩";
    if (event.kind === "plan.updated") return Number.isSafeInteger(event.stage_index) ? `步骤 ${event.stage_index} · ${event.state === "completed" ? "完成" : event.state === "running" ? "执行中" : "更新"}` : "计划更新";
    return isError(event) ? "执行异常" : "关键节点";
  }
  // 分流方向由事件语义决定；等待模型仍是执行过程，不能误作等待用户。
  function flowSide(event) {
    if (event.kind === "phase.changed") {
      if (["context_prepare", "compaction", "confirmation", "waiting_input", "awaiting_input", "recovery", "stopping", "complete"].includes(event.phase)) return "up";
      if (["model", "model_wait", "think", "continuation", "loop", "tools", "planning", "plan"].includes(event.phase)) return "down";
    }
    if (["context.compacted", "model.final", "turn.stopped", "agent.completed"].includes(event.kind)) return "up";
    if (["turn.started", "tools.batch", "plan.updated", "tool.started", "tool.completed", "model.completed"].includes(event.kind)) return "down";
    return isError(event) ? "up" : null;
  }
  function flowLabel(event) {
    if (["turn.stopped", "agent.completed"].includes(event.kind)) return event.reason === "finished" || !isError(event) ? "等待输入" : stopLabel(event);
    if (event.kind === "model.final") return "输出";
    if (event.kind === "context.compacted" || event.phase === "compaction") return "Compact";
    if (event.phase === "context_prepare") return "整理上下文";
    if (event.phase === "confirmation") return "等待确认";
    if (["waiting_input", "awaiting_input", "complete"].includes(event.phase)) return "等待输入";
    if (event.phase === "recovery") return "连接恢复";
    if (event.phase === "stopping") return "停止中";
    if (event.kind === "plan.updated" || ["planning", "plan"].includes(event.phase) || /^plan_/.test(event.tool_name || "")) return "Plan";
    if (event.kind === "model.completed" || ["model", "model_wait", "think"].includes(event.phase)) return "Think";
    if (["turn.started", "tools.batch"].includes(event.kind) || ["continuation", "loop"].includes(event.phase)) return "Loop";
    if (["tool.started", "tool.completed"].includes(event.kind) || event.phase === "tools") return "工具";
    return isError(event) ? "异常" : "状态";
  }
  const toolActions = {
      append_file: "追加文件", cancel_agent_task: "停止子任务", cancel_command_job: "停止后台命令", cancel_session: "停止 Agent",
      copy: "复制文件", create_file: "新建文件", create_session: "创建子 Agent", delegate: "委派子任务", delete_file: "删除文件",
      edit_file: "编辑文件", glob: "查找文件", history_read: "读取历史", history_search: "检索历史", inspect_environment: "探测环境",
      job_logs: "读取命令日志", job_status: "查询命令状态", list_files: "浏览文件", mkdir: "创建目录", move: "移动文件",
      plan_archive: "归档计划", plan_bind: "绑定子任务", plan_create: "制定计划", plan_get: "读取计划", plan_revise: "修订计划", plan_update: "更新计划",
      prepare_project_environment: "准备环境", rag_search: "检索知识库", read_file: "读取文件", read_session_messages: "读取消息",
      request_permission: "请求授权", run_command: "执行命令", send_session_message: "发送消息", start_command_job: "启动后台命令", stat: "检查文件",
      task_status: "查询子任务", tool_debug: "检查工具", update_file: "更新文件", wait_agent_task: "等待子任务", web_search: "搜索网页",
  };
  function toolLabel(name) {
    if (Object.hasOwn(toolActions, name)) return toolActions[name];
    if (/command|shell/.test(name)) return "命令";
    if (/rag|search/.test(name)) return "检索";
    if (/read.*file|file.*read/.test(name)) return "读取文件";
    if (/write|edit|patch/.test(name)) return "编辑文件";
    if (/list|directory/.test(name)) return "浏览文件";
    if (/network|fetch|request/.test(name)) return "网络请求";
    if (/agent|delegate/.test(name)) return "协作";
    return name || "工具";
  }
  // Compress only a proven globally idle interval, bounded by a terminal and a
  // subsequent start. Silence inside a model request or tool call stays real time.
  function timeScale(view, width = 900, compress = false) {
    const [min, max] = view.domain, plotWidth = Math.max(1, width - 170), gaps = [], active = new Set();
    let idleStart = null;
    const events = (view.scaleEvents || view.events).slice().sort((a, b) => eventTime(a) - eventTime(b) || a.seq - b.seq);
    const knownStarts = new Set(events.filter((event) => ["turn.started", "agent.started"].includes(event.kind)).map((event) => event.session_id));
    const unknownBusy = (view.scaleLanes || view.lanes).some((lane) => (lane.busy ?? ["running", "confirming", "stopping", "waiting_workspace", "queued"].includes(lane.status)) && !knownStarts.has(lane.id));
    const busyIntervals = [];
    (view.scaleIntervals || [...view.groups, ...view.spans]).filter((item) => Number.isFinite(item.start) && Number.isFinite(item.end) && item.end > item.start)
      .slice().sort((a, b) => a.start - b.start).forEach((item) => {
        const previous = busyIntervals.at(-1);
        if (previous && item.start <= previous.end) previous.end = Math.max(previous.end, item.end);
        else busyIntervals.push({ start: item.start, end: item.end });
      });
    let intervalIndex = 0;
    function addIdle(start, end) {
      while (intervalIndex < busyIntervals.length && busyIntervals[intervalIndex].end <= start) intervalIndex++;
      let cursor = start;
      for (let index = intervalIndex; index < busyIntervals.length && busyIntervals[index].start < end; index++) {
        const interval = busyIntervals[index];
        if (interval.start - cursor > 60000) gaps.push({ start: cursor, end: interval.start, duration: interval.start - cursor });
        cursor = Math.max(cursor, interval.end);
      }
      if (end - cursor > 60000) gaps.push({ start: cursor, end, duration: end - cursor });
    }
    // 子任务生命周期与单次 turn 分开计数，不能把 turn 停止误作子任务完成。
    const activityKey = (event) => [event.kind.startsWith("agent.") ? "agent" : "turn", event.session_id || "unknown", event.kind.startsWith("agent.") ? event.task_id || "" : event.turn_id || ""].join(":");
    if (compress && !unknownBusy) events.forEach((event) => {
      const time = eventTime(event), id = activityKey(event);
      if (["turn.started", "agent.started"].includes(event.kind)) {
        if (!active.size && idleStart != null) addIdle(idleStart, time);
        active.add(id); idleStart = null;
      } else if (["turn.stopped", "agent.completed"].includes(event.kind)) {
        active.delete(id); if (!active.size) idleStart = time;
      }
    });
    let removed = gaps.reduce((sum, gap) => sum + gap.duration, 0);
    // With no observed work on either side, there is no meaningful duration to
    // expand. Keep the ordinary axis instead of inventing a zero-time region.
    if (max - min - removed <= 0) { gaps.length = 0; removed = 0; }
    const gapPixels = Math.min(72, plotWidth / Math.max(1, gaps.length * 3));
    const regularPixels = plotWidth - gaps.length * gapPixels, regularDuration = max - min - removed;
    const segments = []; let real = min, pixel = 140;
    gaps.forEach((gap) => {
      const length = (gap.start - real) / regularDuration * regularPixels;
      if (gap.start > real) segments.push({ start: real, end: gap.start, left: pixel, right: pixel + length });
      pixel += length; gap.left = pixel; gap.right = pixel + gapPixels;
      segments.push({ start: gap.start, end: gap.end, left: pixel, right: gap.right }); pixel = gap.right; real = gap.end;
    });
    if (real < max) segments.push({ start: real, end: max, left: pixel, right: 140 + plotWidth });
    const position = (time) => {
      const segment = segments.find((part) => time <= part.end) || segments[segments.length - 1];
      return segment.left + (time - segment.start) / (segment.end - segment.start || 1) * (segment.right - segment.left);
    };
    const timeAt = (position) => {
      const segment = segments.find((part) => position <= part.right) || segments[segments.length - 1];
      return segment.start + (position - segment.left) / (segment.right - segment.left || 1) * (segment.end - segment.start);
    };
    return { position, timeAt, gaps, realDuration: max - min, compressed: gaps.length > 0 };
  }
  function timeline(snapshot, selected = "", errorsOnly = false) {
    const trace = snapshot.trace || {}, events = (trace.timeline || []).filter((event) => eventTime(event) != null).slice().sort((a, b) => a.seq - b.seq);
    const laneMap = new Map(), allGroups = [], allSpans = [], groupsByLane = new Map();
    const ensure = (id) => { id = id || "unknown"; if (!laneMap.has(id)) laneMap.set(id, { id, parentId: null, start: null, color: agentColor(id), events: [] }); return laneMap.get(id); };
    (snapshot.agents || []).forEach((agent) => { const lane = ensure(agent.id); lane.parentId = agent.parent_id || null; lane.status = agent.status; lane.busy = agent.busy; });
    events.forEach((event) => {
      const lane = ensure(event.session_id), time = eventTime(event); lane.events.push(event);
      if (lane.start == null) lane.start = time;
      if (event.kind === "agent.created") { lane.parentId = event.parent_session_id || null; lane.start = time; lane.branch = event; if (lane.parentId) ensure(lane.parentId); }
    });
    // Agent 筛选保留所选子树的全部行为；祖先只提供结构端点，共用所属根的时间尺度。
    const parents = new Map([...laneMap.values()].map((lane) => [lane.id, lane.parentId]));
    const selectedIds = selected ? subtreeIds(parents, selected) : null;
    let scopeRoot = laneMap.get(selected); const scopeAncestors = new Set();
    while (scopeRoot?.parentId && laneMap.has(scopeRoot.parentId) && !scopeAncestors.has(scopeRoot.parentId)) { scopeAncestors.add(scopeRoot.id); scopeRoot = laneMap.get(scopeRoot.parentId); }
    const scopeIds = selected ? subtreeIds(parents, scopeRoot?.id || selected) : null;
    const keep = (item) => !selectedIds || selectedIds.has(item.session_id);
    const inScope = (item) => !scopeIds || scopeIds.has(item.session_id);
    const starts = new Map(), finished = new Set(), spanStarts = new Map();
    const callKey = (event) => `${event.session_id || "unknown"}:${event.turn_id || "unknown"}:${event.tool_call_id || event.span_id || `event-${event.seq}`}`;
    events.forEach((event) => {
      const time = eventTime(event), lane = ensure(event.session_id), key = callKey(event);
      if (event.kind === "tool.started") {
        const previous = groupsByLane.get(lane.id), batch = event.batch_id;
        const group = batch && previous?.batch_id === batch && previous.turn_id === event.turn_id ? previous : { id: `batch-${event.seq}`, batch_id: batch || null, turn_id: event.turn_id, session_id: lane.id, start: time, end: time, calls: [], events: [], errors: 0 };
        if (group !== previous) allGroups.push(group);
        groupsByLane.set(lane.id, group);
        const call = { id: key, session_id: lane.id, start: time, end: time, active: true, event, startEvent: event, tool_name: event.tool_name || "工具", state: event.state || "running" };
        group.calls.push(call); group.events.push(event); starts.set(key, { call, group });
      } else if (event.kind === "tool.completed") {
        const item = starts.get(key);
        if (item) {
          item.call.end = Math.max(item.call.start, time); item.call.active = false; item.call.event = event; item.call.state = event.state || "completed";
          item.group.events.push(event); item.group.end = Math.max(item.group.end, time); if (isError(event)) item.group.errors++;
          finished.add(key);
        } else {
          // A retained window may begin after a call started. Keep an explicit incomplete interval.
          const duration = Number.isFinite(event.duration_ms) ? event.duration_ms : 0;
          const call = { id: key, session_id: lane.id, start: time - duration, end: time, event, tool_name: event.tool_name || "工具", state: event.state || "completed", incomplete: !duration };
          allGroups.push({ id: `batch-${event.seq}`, batch_id: event.batch_id || null, session_id: lane.id, start: call.start, end: time, calls: [call], events: [event], errors: isError(event) ? 1 : 0 });
        }
      } else if (event.kind === "span.started") spanStarts.set(`${lane.id}:${event.span_id}`, event);
      else if (event.kind === "span.completed") {
        const start = spanStarts.get(`${lane.id}:${event.span_id}`);
        if (event.module !== "tool" && !event.tool_call_id) allSpans.push({ id: `span-${event.seq}`, session_id: lane.id, start: start ? eventTime(start) : time - (event.duration_ms || 0), end: time, event, label: event.module || "模块", state: event.state });
        spanStarts.delete(`${lane.id}:${event.span_id}`);
      }
      // Completion order does not break a concurrently executing batch.
      if ((upperKinds.has(event.kind) && !(event.kind === "phase.changed" && event.phase === "tools")) || event.kind === "turn.started" || event.kind === "tools.batch") {
        if (event.kind !== "tools.batch" || groupsByLane.get(lane.id)?.batch_id !== event.batch_id) groupsByLane.delete(lane.id);
      }
    });
    const times = events.filter(inScope).map(eventTime), elapsed = Math.max(trace.now_elapsed_ms || 0, times.length ? Math.max(...times) : 0);
    const hasElapsed = events.some((event) => Number.isFinite(event.elapsed_ms));
    const origin = events.find((event) => Number.isFinite(event.elapsed_ms) && event.ts != null);
    const wallOrigin = origin ? (typeof origin.ts === "number" ? origin.ts * 1000 : Date.parse(origin.ts)) - origin.elapsed_ms : null;
    const legacyTime = (row) => hasElapsed && Number.isFinite(wallOrigin) ? row.started_at * 1000 - wallOrigin : row.started_at * 1000;
    const activeByCall = new Map((trace.active || []).filter((row) => row.tool_call_id).map((row) => [callKey(row), row]));
    const activeBySpan = new Map((trace.active || []).map((row) => [`${row.session_id || "unknown"}:${row.span_id ?? row.id}`, row]));
    const knownSpanIds = new Set(events.map((event) => event.span_id).filter((id) => id != null));
    [...(trace.events || []), ...(trace.active || [])].forEach((row) => {
      const lane = ensure(row.session_id); if (knownSpanIds.has(row.id) || row.tool_call_id && starts.has(callKey(row))) return;
      const start = legacyTime(row); if (!Number.isFinite(start)) return;
      const active = (trace.active || []).includes(row), end = start + (row.duration_ms || 0);
      allSpans.push({ id: `legacy-${row.id}`, session_id: lane.id, start, end, event: row, label: row.module || "模块", state: row.state, active, legacy: true });
    });
    spanStarts.forEach((event) => {
      const row = activeBySpan.get(`${event.session_id || "unknown"}:${event.span_id}`), start = eventTime(event);
      if (event.module !== "tool" && !event.tool_call_id) allSpans.push({ id: `span-${event.seq}`, session_id: event.session_id || "unknown", start, end: Math.max(start, elapsed, row ? legacyTime(row) + (row.duration_ms || 0) : 0), event, label: event.module || "模块", state: "running", active: true });
    });
    starts.forEach(({ call, group }, key) => { if (!finished.has(key)) { const row = activeByCall.get(key) || activeBySpan.get(`${call.session_id}:${call.startEvent.span_id}`); call.end = Math.max(call.start, elapsed, row ? legacyTime(row) + (row.duration_ms || 0) : 0); group.end = Math.max(group.end, call.end); } });
    const communications = [], readIndex = new Map();
    const messageKey = (sender, recipient, sequence) => JSON.stringify([sender, recipient, sequence]);
    // Index reads once. Cursor positions only advance, including when a sequence is
    // reused after an earlier read; repeated reads cannot trigger a full-event scan.
    events.forEach((event) => {
      if (event.kind !== "message.read" || !event.related_session_id || event.message_seq == null) return;
      const key = messageKey(event.related_session_id, event.session_id, event.message_seq);
      if (!readIndex.has(key)) readIndex.set(key, { reads: [], cursor: 0 });
      readIndex.get(key).reads.push(event);
    });
    events.filter((event) => event.kind === "message.sent" && event.related_session_id && event.message_seq != null).forEach((sent) => {
      const from = ensure(sent.session_id), to = ensure(sent.related_session_id);
      if (from.parentId !== to.id && to.parentId !== from.id) return;
      const start = eventTime(sent), indexed = readIndex.get(messageKey(sent.session_id, sent.related_session_id, sent.message_seq));
      if (indexed) {
        while (indexed.cursor < indexed.reads.length && (indexed.reads[indexed.cursor].seq < sent.seq || eventTime(indexed.reads[indexed.cursor]) < start)) indexed.cursor++;
      }
      const read = indexed?.reads[indexed.cursor];
      communications.push({ id: `message-${sent.seq}`, from: from.id, to: to.id, start, end: read ? eventTime(read) : elapsed, sent, read, pending: !read, color: from.color });
    });
    const ordered = [], seen = new Set();
    function visit(lane) { if (seen.has(lane.id)) return; seen.add(lane.id); ordered.push(lane); [...laneMap.values()].filter((child) => child.parentId === lane.id).forEach(visit); }
    [...laneMap.values()].filter((lane) => !lane.parentId || !laneMap.has(lane.parentId)).forEach(visit); laneMap.forEach(visit);
    const groups = allGroups.filter(keep).filter((group) => !errorsOnly || group.errors);
    const spans = allSpans.filter(keep).filter((span) => !errorsOnly || isError(span.event));
    const upper = events.filter((event) => upperKinds.has(event.kind) && keep(event) && (!errorsOnly || isError(event)));
    const visibleEdges = communications.filter((edge) => !selectedIds || selectedIds.has(edge.from) || selectedIds.has(edge.to));
    const visibleIds = new Set(selectedIds ? [...selectedIds, ...visibleEdges.flatMap((edge) => [edge.from, edge.to])] : ordered.map((lane) => lane.id));
    let selectedLane = laneMap.get(selected); const ancestors = new Set();
    while (selectedLane?.parentId && !ancestors.has(selectedLane.parentId)) { ancestors.add(selectedLane.parentId); visibleIds.add(selectedLane.parentId); selectedLane = laneMap.get(selectedLane.parentId); }
    const lanes = ordered.filter((lane) => visibleIds.has(lane.id)).map((lane) => ({ ...lane, events: lane.events.filter(keep) }));
    const scaleEvents = events.filter(inScope), scaleLanes = ordered.filter((lane) => !scopeIds || scopeIds.has(lane.id)), scaleIntervals = [...allSpans, ...allGroups].filter(inScope);
    const allTimes = [...scaleEvents.map(eventTime), ...scaleIntervals.flatMap((item) => [item.start, item.end])].filter(Number.isFinite);
    if (hasElapsed && scaleLanes.some((lane) => lane.busy ?? ["running", "confirming", "stopping", "waiting_workspace", "queued"].includes(lane.status))) allTimes.push(elapsed);
    const min = allTimes.length ? Math.min(...allTimes) : 0, max = allTimes.length ? Math.max(...allTimes) : 1000;
    return { events: events.filter(keep), scaleEvents, scaleLanes, scaleIntervals, lanes, groups, spans, upper, communications: visibleEdges, domain: [min, Math.max(min + 1000, max)], now: elapsed, errorsOnly, legacy: !Array.isArray(trace.timeline) };
  }
  // 概览按像素聚合，不按事件数增加行高；原始事件、批次和真实区间仍可下钻。
  function overview(view, width = 900, scale = timeScale(view, width)) {
    const [min, max] = view.domain, plotWidth = Math.max(1, width - 170);
    const columns = (pixels) => Math.max(1, Math.floor(plotWidth / pixels));
    const bucket = (time, count) => Math.max(0, Math.min(count - 1, Math.floor((scale.position(time) - 140) / plotWidth * count)));
    function collect(items, type, laneId, pixels) {
      const count = columns(pixels), bins = new Map();
      items.forEach((item) => {
        const index = bucket(item.start, count);
        if (!bins.has(index)) bins.set(index, { type, session_id: laneId, bucket: index, start: item.start, end: item.end, events: [], groups: [], segments: [], errors: 0,
          windowStart: scale.timeAt(140 + index / count * plotWidth), windowEnd: scale.timeAt(140 + (index + 1) / count * plotWidth) });
        const region = bins.get(index);
        region.start = Math.min(region.start, item.start); region.end = Math.max(region.end, item.end);
        region.events.push(...item.events); region.errors += item.errors || 0;
        if (item.group) region.groups.push(item.group); else region.segments.push(item);
      });
      return [...bins.values()].map((region) => {
        region.event = region.events.find(isError) || region.events[0];
        region.id = `${type}-${laneId}-${region.events[0].seq}-${region.events[region.events.length - 1].seq}`;
        const durations = new Map();
        region.segments.forEach((segment) => { const phase = segment.events[0].phase; durations.set(phase, (durations.get(phase) || 0) + Math.max(0, segment.end - segment.start)); });
        region.phase = [...durations].sort((a, b) => b[1] - a[1])[0]?.[0];
        if (["branches", "tools", "flowUp", "flowDown"].includes(type)) {
          region.labelX = 140 + region.bucket / count * plotWidth + 4;
          region.labelWidth = Math.max(1, plotWidth / count - 8);
          region.anchorX = scale.position(region.start);
          if (type === "flowUp" || type === "flowDown") {
            const categories = new Map();
            region.events.forEach((event) => { const label = flowLabel(event); if (!categories.has(label)) categories.set(label, []); categories.get(label).push(event); });
            const priority = { "Compact": 10, "异常": 9, "执行失败": 9, "等待确认": 8, "连接恢复": 8, "等待输入": 7, "输出": 6, "整理上下文": 5, "Think": 4, "Loop": 3, "Plan": 2, "工具": 1 };
            const counts = [...categories].sort((a, b) => (priority[b[0]] || 0) - (priority[a[0]] || 0)).map(([label, events]) => {
              // 一次请求/调用可能有多个阶段事件，概览不把它们当成多次调用。
              let selected = events;
              if (label === "Think" && events.some((event) => event.kind === "model.completed")) selected = events.filter((event) => event.kind === "model.completed");
              if (label === "Loop" && events.some((event) => event.kind === "tools.batch")) selected = events.filter((event) => event.kind === "tools.batch");
              if (label === "Compact" && events.some((event) => event.kind === "context.compacted")) selected = events.filter((event) => event.kind === "context.compacted");
              if (label === "Plan" && events.some((event) => event.kind === "plan.updated")) selected = events.filter((event) => event.kind === "plan.updated");
              if (label === "工具") {
                const calls = events.filter((event) => event.tool_call_id);
                if (calls.length) return [label, new Set(calls.map((event) => event.tool_call_id)).size];
              }
              return [label, selected.length];
            });
            region.label = counts.slice(0, 2).map(([label]) => label).join(" · ");
            region.detail = counts.map(([label, count]) => `${label} ${count}`).join(" · ");
            region.toolNames = [...new Set(region.events.map((event) => event.tool_name).filter(Boolean))];
            if (region.errors) region.detail += ` · 异常事件 ${region.errors}`;
          } else if (type === "tools") {
            const calls = region.groups.flatMap((group) => group.calls), counts = new Map();
            region.toolNames = [...new Set(calls.map((call) => call.tool_name))];
            calls.forEach((call) => { const name = toolLabel(call.tool_name); counts.set(name, (counts.get(name) || 0) + 1); });
            const names = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 2).map(([name, total]) => `${name} ×${total}`);
            region.label = `${names.join(" / ")}${counts.size > 2 ? " …" : ""}${region.errors ? ` · ! ${region.errors}` : ""}`;
          } else {
            const priority = (event) => ["turn.stopped", "agent.completed"].includes(event.kind) ? 5 : isError(event) ? 4 : event.phase === "recovery" || event.phase === "confirmation" ? 3 : event.kind === "plan.updated" || event.kind === "model.final" ? 2 : 1;
            region.event = region.events.slice().sort((a, b) => priority(b) - priority(a) || b.seq - a.seq)[0];
            const rejected = region.events.filter((event) => event.state === "rejected").length;
            const problems = [region.errors > rejected ? `异常事件 ${region.errors - rejected}` : "", rejected ? `拒绝事件 ${rejected}` : ""].filter(Boolean).join(" / ");
            const summary = branchLabel(region.event), merged = region.events.length > 1 ? `含 ${region.events.length} 个事件` : "";
            if (problems) region.label = [problems, summary === "执行异常" ? "" : summary, merged].filter(Boolean).join(" · ");
            else if (region.events.every((event) => event.kind === "phase.changed")) region.label = `阶段切换 ${region.events.length} 次 · ${summary}`;
            else region.label = [summary, merged].filter(Boolean).join(" · ");
          }
        }
        return region;
      });
    }
    let height = 0;
    const laneEvents = new Map(), laneGroups = new Map(), laneUpper = new Map();
    view.events.forEach((event) => { if (!laneEvents.has(event.session_id)) laneEvents.set(event.session_id, []); laneEvents.get(event.session_id).push(event); });
    view.groups.forEach((group) => { if (!laneGroups.has(group.session_id)) laneGroups.set(group.session_id, []); laneGroups.get(group.session_id).push(group); });
    view.upper.forEach((event) => { if (!laneUpper.has(event.session_id)) laneUpper.set(event.session_id, []); laneUpper.get(event.session_id).push(event); });
    const parents = new Map(view.lanes.map((lane) => [lane.id, lane.parentId]));
    const lanes = view.lanes.map((lane) => {
      const events = laneEvents.get(lane.id) || [], upper = laneUpper.get(lane.id) || [], groups = laneGroups.get(lane.id) || [];
      const phases = upper.filter((event) => event.kind === "phase.changed");
      const active = lane.busy ?? ["running", "confirming", "stopping", "waiting_workspace", "queued"].includes(lane.status);
      const last = Math.max(min, ...(lane.events || events).map(eventTime), ...groups.map((group) => group.end), active ? view.now || min : min);
      const boundaries = events.filter((event) => ["turn.stopped", "agent.completed"].includes(event.kind)); let boundaryIndex = 0;
      const phaseItems = phases.map((event, index) => {
        while (boundaryIndex < boundaries.length && boundaries[boundaryIndex].seq < event.seq) boundaryIndex++;
        const next = index + 1 < phases.length ? eventTime(phases[index + 1]) : last;
        const end = boundaries[boundaryIndex] ? Math.min(next, eventTime(boundaries[boundaryIndex])) : next;
        return { start: eventTime(event), end: Math.max(eventTime(event), end), events: [event], errors: isError(event) ? 1 : 0 };
      });
      const compact = events.filter((event) => event.kind === "context.compacted" && (!view.errorsOnly || isError(event)));
      const markers = new Map([...upper.filter((event) => event.kind !== "phase.changed" || ["recovery", "confirmation", "stopping"].includes(event.phase)), ...compact, ...events.filter(isError)].map((event) => [event.seq, event]));
      const markerItems = [...markers.values()]
        .sort((a, b) => a.seq - b.seq).map((event) => ({ start: eventTime(event), end: eventTime(event), events: [event], errors: isError(event) ? 1 : 0 }));
      let depth = 0, parent = lane.parentId; const visited = new Set([lane.id]);
      while (parent && !visited.has(parent)) { visited.add(parent); depth++; parent = parents.get(parent); }
      const densityColumns = columns(12), activity = Array.from({ length: densityColumns }, (_, index) => ({ start: scale.timeAt(140 + index / densityColumns * plotWidth), end: scale.timeAt(140 + (index + 1) / densityColumns * plotWidth), count: 0 }));
      events.forEach((event) => activity[bucket(eventTime(event), densityColumns)].count++);
      const terminal = (lane.events || events).filter((event) => ["turn.stopped", "agent.completed"].includes(event.kind)).at(-1);
      const stateLabel = active ? "执行中" : terminal ? stopLabel(terminal) : "就绪";
      const spineState = active ? "running" : /失败|超时|中断/.test(stateLabel) ? "error" : /暂停|停止|授权/.test(stateLabel) ? "paused" : terminal ? "completed" : "idle";
      const branchEvents = [...new Map([...upper, ...compact, ...events.filter(isError)].map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq);
      const flowItems = (side) => events.filter((event) => flowSide(event) === side && (!view.errorsOnly || isError(event)))
        .map((event) => ({ start: eventTime(event), end: eventTime(event), events: [event], errors: isError(event) ? 1 : 0 }));
      const result = { ...lane, depth, end: last, active, activity, stateLabel, spineState, top: height, lineY: height + 108, height: 224,
        flowUp: collect(flowItems("up"), "flowUp", lane.id, 170), flowDown: collect(flowItems("down"), "flowDown", lane.id, 170),
        phases: collect(phaseItems, "phases", lane.id, 14), markers: collect(markerItems, "markers", lane.id, 26),
        branches: collect(branchEvents.map((event) => ({ start: eventTime(event), end: eventTime(event), events: [event], errors: isError(event) ? 1 : 0 })), "branches", lane.id, 120),
        tools: collect(groups.map((group) => ({ start: group.start, end: group.end, events: group.events, errors: group.errors, group })), "tools", lane.id, 120) };
      result.flowDown.forEach((region) => {
        const sequences = new Set(region.events.map((event) => event.seq));
        region.groups = groups.filter((group) => group.events.some((event) => sequences.has(event.seq)));
        if (region.groups.length === 1) {
          const call = region.groups[0].calls.find((item) => sequences.has(item.event.seq) || sequences.has(item.startEvent?.seq));
          if (call) region.event = sequences.has(call.event.seq) ? call.event : call.startEvent;
        }
      });
      height += result.height; return result;
    });
    const connections = new Map(), count = columns(100);
    view.communications.forEach((edge) => {
      const key = JSON.stringify([edge.from, edge.to, edge.pending, bucket(edge.start, count)]);
      if (!connections.has(key)) connections.set(key, { ...edge, edges: [] });
      const connection = connections.get(key); connection.edges.push(edge);
      connection.start = Math.min(connection.start, edge.start); connection.end = Math.max(connection.end, edge.end);
    });
    const activityMax = lanes.reduce((maximum, lane) => lane.activity.reduce((current, bin) => Math.max(current, bin.count), maximum), 1);
    return { lanes, height: Math.max(100, height), activityMax, communications: [...connections.values()], domain: view.domain };
  }
  // 总览把协作关系画成共享母线，连接段不代表执行耗时；实际工作仍按同一时间轴定位。
  function storyboard(view, width = 900, scale = timeScale(view, width)) {
    const base = overview(view, width, scale), byId = new Map();
    const incidents = [], known = new Map();
    const incidentKey = (event) => `${event.session_id}:${event.turn_id || ""}:${event.tool_call_id ? "call-" + event.tool_call_id : event.span_id != null ? "span-" + event.span_id : "event-" + event.seq}`;
    view.events.filter((event) => event.kind === "tool.completed" && isError(event)).forEach((event) => {
      const key = incidentKey(event);
      if (!known.has(key)) { const incident = { id: key, session_id: event.session_id, type: event.state === "rejected" ? "rejected" : "tool_error", event, events: [] }; known.set(key, incident); incidents.push(incident); }
      if (event.span_id != null) known.set(`${event.session_id}:${event.turn_id || ""}:span-${event.span_id}`, known.get(key));
    });
    view.events.filter(isError).forEach((event) => {
      const key = incidentKey(event);
      let incident = known.get(key) || (event.span_id != null ? known.get(`${event.session_id}:${event.turn_id || ""}:span-${event.span_id}`) : null);
      if (!incident) { incident = { id: key, session_id: event.session_id, type: event.state === "rejected" ? "rejected" : "error", event, events: [] }; known.set(key, incident); incidents.push(incident); }
      incident.events.push(event);
    });
    base.lanes.forEach((lane) => {
      const terminal = lane.events.filter((event) => ["turn.stopped", "agent.completed"].includes(event.kind)).at(-1);
      const closed = Boolean(terminal && !lane.active);
      byId.set(lane.id, { ...lane, closed, terminal, end: closed ? Math.max(lane.start ?? view.domain[0], eventTime(terminal)) : lane.end,
        expanded: true, incidents: incidents.filter((incident) => incident.session_id === lane.id) });
    });
    const children = new Map();
    byId.forEach((lane) => { if (byId.has(lane.parentId)) { if (!children.has(lane.parentId)) children.set(lane.parentId, []); children.get(lane.parentId).push(lane); } });
    function descendants(members, parentId) {
      const result = [], queue = members.slice(), seen = new Set([parentId]);
      for (let index = 0; index < queue.length; index++) {
        const lane = queue[index]; if (seen.has(lane.id)) continue;
        seen.add(lane.id); result.push(lane); queue.push(...(children.get(lane.id) || []));
      }
      return result;
    }
    const collaborations = [...children].map(([parentId, members]) => {
      members.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
      // 外层母线也必须避开所有后代的实际工作，不能因直接子级先结束而穿过后代。
      const subtree = descendants(members, parentId);
      const start = Math.min(...subtree.map((lane) => lane.start ?? view.domain[0])), end = Math.max(...subtree.map((lane) => lane.end));
      return { id: `collaboration-${parentId}`, parentId, children: members, start, end,
        left: Math.max(140, scale.position(start) - 12), right: Math.min(width - 12, scale.position(end) + 12) };
    });
    // 留出母线通道，事件标签和短竖箭头都不经过通道。
    const ports = [...new Set(collaborations.flatMap((group) => [group.left, group.right]))].sort((a, b) => a - b);
    const gaps = []; let left = 140;
    ports.forEach((port) => { if (port - 12 > left) gaps.push([left, port - 12]); left = Math.max(left, port + 12); });
    if (left < width - 30) gaps.push([left, width - 30]);
    const slots = [];
    gaps.forEach(([start, end]) => { const count = Math.floor((end - start) / 170); for (let index = 0; index < count; index++) slots.push({ left: start + (end - start) * index / count, right: start + (end - start) * (index + 1) / count }); });
    if (!slots.length && gaps.length) { const gap = gaps.slice().sort((a, b) => (b[1] - b[0]) - (a[1] - a[0]))[0]; if (gap[1] - gap[0] > 16) slots.push({ left: gap[0], right: gap[1] }); }
    function annotations(lane, side) {
      const events = lane.events.filter((event) => flowSide(event) === side && (!view.errorsOnly || isError(event))), bins = new Map();
      events.forEach((event) => {
        if (!slots.length) return;
        const x = scale.position(eventTime(event)); let index = 0;
        slots.forEach((slot, candidate) => { if (Math.abs((slot.left + slot.right) / 2 - x) < Math.abs((slots[index].left + slots[index].right) / 2 - x)) index = candidate; });
        if (!bins.has(index)) bins.set(index, []); bins.get(index).push(event);
      });
      return [...bins].sort((a, b) => a[0] - b[0]).map(([index, events]) => {
        const slot = slots[index], sequences = new Set(events.map((event) => event.seq));
        const groups = lane.tools.flatMap((region) => region.groups).filter((group) => group.events.some((event) => sequences.has(event.seq)));
        const actions = new Map(), add = (label) => actions.set(label, (actions.get(label) || 0) + 1);
        if (side === "down") {
          events.filter((event) => event.kind === "plan.updated").forEach(() => add("Plan"));
          const calls = new Map();
          events.filter((event) => ["tool.started", "tool.completed"].includes(event.kind)).forEach((event) => calls.set(`${event.turn_id || ""}:${event.tool_call_id || event.seq}`, event));
          calls.forEach((event) => add(/^plan_/.test(event.tool_name || "") ? "Plan" : toolLabel(event.tool_name)));
          if (!actions.size) events.forEach((event) => add(flowLabel(event)));
        } else events.forEach((event) => add(flowLabel(event)));
        const priority = { "Compact": 100, "等待输入": 90, "执行失败": 95, "异常": 95, "等待确认": 85, "输出": 80, "Plan": 75 };
        const ordered = [...actions].sort((a, b) => (priority[b[0]] || 0) - (priority[a[0]] || 0) || b[1] - a[1]);
        let event = events.find(isError) || events[0];
        if (side === "down" && groups.length === 1) {
          const call = groups[0].calls.find((call) => sequences.has(call.event.seq) || sequences.has(call.startEvent?.seq));
          if (call) event = sequences.has(call.event.seq) ? call.event : call.startEvent;
        }
        // 保持原标签槽，仅用槽附近的真实事件定位短竖箭头，避免首事件远离摘要。
        const midpoint = (slot.left + slot.right) / 2;
        const anchorEvent = events.reduce((nearest, current) => Math.abs(scale.position(eventTime(current)) - midpoint) < Math.abs(scale.position(eventTime(nearest)) - midpoint) ? current : nearest);
        return { type: side === "up" ? "flowUp" : "flowDown", id: `story-${side}-${lane.id}-${events[0].seq}`, session_id: lane.id,
          events, event, groups, start: Math.min(...events.map(eventTime)), end: Math.max(...events.map(eventTime)),
          windowStart: scale.timeAt(slot.left), windowEnd: scale.timeAt(slot.right), labelX: slot.left + 4, labelWidth: slot.right - slot.left - 8,
          anchorX: scale.position(eventTime(anchorEvent)), anchorEvent, label: ordered.slice(0, 2).map(([name]) => name).join(" · "),
          detail: `${events.length} 事件 · ${ordered.slice(0, 3).map(([name, count]) => `${name} ${count}`).join(" · ")}`,
          toolNames: [...new Set(events.map((event) => event.tool_name).filter(Boolean))], errors: events.filter(isError).length };
      });
    }
    let height = 0;
    const placed = new Set(), ordered = [], roots = [];
    function place(lane, root = false) {
      if (placed.has(lane.id)) return;
      placed.add(lane.id); lane.top = height; lane.height = 224; lane.lineY = height + 108; height += lane.height;
      lane.flowUp = annotations(lane, "up"); lane.flowDown = annotations(lane, "down"); ordered.push(lane); if (root) roots.push(lane);
      const group = collaborations.find((group) => group.parentId === lane.id);
      if (group) { group.top = height; height += 38; group.children.forEach((child) => place(child)); height += 20; }
    }
    byId.forEach((lane) => { if (!byId.has(lane.parentId)) place(lane, true); });
    byId.forEach((lane) => place(lane, true));
    collaborations.forEach((group) => {
      const parent = byId.get(group.parentId), completed = group.children.filter((child) => child.closed);
      group.forkBus = { x: group.left, fromY: parent.lineY, toY: Math.max(...group.children.map((child) => child.lineY)) };
      group.joinBus = completed.length ? { x: group.right, fromY: parent.lineY, toY: Math.max(...completed.map((child) => child.lineY)) } : null;
      group.branches = group.children.map((child) => ({ id: `branch-${child.id}`, childId: child.id, startX: scale.position(child.start ?? view.domain[0]), endX: scale.position(child.end), y: child.lineY, closed: child.closed }));
    });
    return { ...base, lanes: ordered, roots, collaborations, height: Math.max(100, height), incidents,
      summary: { agents: byId.size, tool_errors: incidents.filter((item) => item.type === "tool_error").length, rejections: incidents.filter((item) => item.type === "rejected").length,
        other_errors: incidents.filter((item) => item.type === "error").length, compactions: view.events.filter((event) => event.kind === "context.compacted").length } };
  }
  function metrics(view, mode = "request") {
    const context = [], tokens = [], totals = new Map();
    view.events.slice().sort((a, b) => eventTime(a) - eventTime(b)).forEach((event) => {
      const values = event.metrics || {}, time = eventTime(event);
      let utilization = values.context_utilization;
      if (!Number.isFinite(utilization) && values.context_limit_tokens > 0 && Number.isFinite(values.context_tokens)) utilization = values.context_tokens / values.context_limit_tokens;
      if (Number.isFinite(utilization) || event.kind === "context.sample") context.push({ time, value: Number.isFinite(utilization) ? utilization * 100 : null, session_id: event.session_id, reason: event.reason || "", event });
      if ("tokens_source" in values || "input_tokens" in values || "output_tokens" in values) {
        const source = ["provider", "estimated"].includes(values.tokens_source) ? values.tokens_source : "unknown";
        const input = source !== "unknown" && Number.isFinite(values.input_tokens) ? values.input_tokens : null;
        const output = source !== "unknown" && Number.isFinite(values.output_tokens) ? values.output_tokens : null;
        const total = totals.get(event.session_id) || { input: 0, output: 0 };
        if (input != null) total.input += input; if (output != null) total.output += output; totals.set(event.session_id, total);
        tokens.push({ time, input: input == null ? null : mode === "cumulative" ? total.input : input, output: output == null ? null : mode === "cumulative" ? total.output : output, source, session_id: event.session_id, event });
      }
    });
    return { context, tokens, domain: view.domain };
  }
  return { rows, modules, agents, mergeTimeline, eventTime, toolGeometry, toolLabel, flowSide, flowLabel, timeScale, timeline, overview, storyboard, metrics, agentColor, isError };
})();
