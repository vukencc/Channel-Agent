/* 运行视图只读取当前进程的快照，不修改 Flow 草稿。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = String(text); return node; };
  let bridge, active = false, rows = [], selected = null, timer = null, pending = null, generation = 0;
  let detailPane, detailSnapshot;
  let refreshMessage = "正在更新运行状态…";
  const view = () => $("working-view");
  const value = (v) => v == null || v === "" ? "—" : typeof v === "object" ? JSON.stringify(v, null, 2) : String(v);
  const status = (v) => ({queued: "排队中", running: "运行中", cancelling: "正在停止", completed: "已完成", blocked: "已阻断", cancelled: "已取消", failed: "失败", error: "错误", pending: "等待中", confirming: "等待确认", stopping: "正在停止", checkpoint: "阶段已保存", interrupted: "已中断"})[v] || value(v);
  const structure = (runs) => JSON.stringify(runs.map((r) => [r.run_id, (r.nodes || []).map((n) => n.id)]));
  function field(container, label, content) {
    const group = el("div", "working-field"); group.append(el("dt", "", label), el("dd", "", value(content))); container.append(group);
  }
  function updateNode(button, tip, node) {
    button.replaceChildren(el("strong", "", node.title || node.id), el("span", "working-status", status(node.status)));
    tip.replaceChildren(el("strong", "", node.title || node.id), el("div", "", `子 Agent：${node.active_subagent_count ?? 0}/${node.subagent_count ?? 0} 活跃`), el("div", "", `任务：${value(node.task_summary)}`), el("div", "", `模型：${value(node.model)}`));
  }
  function positionTip(button, tip) {
    const rect = button.getBoundingClientRect(), width = Math.min(280, window.innerWidth - 20);
    tip.style.width = `${width}px`;
    const height = Math.min(tip.scrollHeight, window.innerHeight - 20);
    tip.style.left = `${Math.max(10, Math.min(rect.left, window.innerWidth - width - 10))}px`;
    tip.style.top = `${Math.max(10, Math.min(rect.bottom + height <= window.innerHeight - 10 ? rect.bottom : rect.top - height, window.innerHeight - height - 10))}px`;
  }
  function details() {
    const pane = view().querySelector(".working-detail"); if (!pane) return;
    const run = rows.find((r) => r.run_id === selected?.run), node = run?.nodes?.find((n) => n.id === selected?.node);
    const snapshot = JSON.stringify([run?.run_id, run?.title, node]);
    if (pane === detailPane && snapshot === detailSnapshot) return;
    detailPane = pane; detailSnapshot = snapshot;
    const scroll = pane.scrollTop; pane.replaceChildren(el("h2", "", "节点详情"));
    if (!node) { pane.append(el("p", "working-muted", "选择节点查看只读详情。")); return; }
    pane.append(el("h3", "", node.title || node.id));
    const list = el("dl", "working-fields");
    const validation = node.handoff_validation;
    const handoff = validation?.status === "passed" ? "格式校验通过，业务结果需另行核验" : validation?.message;
    [["Flow", run.title], ["运行 ID", run.run_id], ["状态", status(node.status)], ["阶段", node.phase], ["任务", node.task], ["任务概览", node.task_summary], ["模型", node.model], ["模型预设", node.model_profile], ["思考模式", node.thinking_mode], ["推理强度", node.reasoning_effort], ["开始时间", node.started_at], ["会话 ID", node.session_id], ["模型调用", node.model_calls], ["工具调用", node.tool_calls], ["交接验收", handoff]].forEach(([label, content]) => field(list, label, content));
    pane.append(list, el("h3", "", `子 Agent (${node.active_subagent_count ?? 0}/${node.subagent_count ?? 0} 活跃)`));
    if ((node.subagent_count ?? 0) > (node.subagents || []).length) {
      pane.append(el("p", "working-muted", `显示 ${(node.subagents || []).length} / ${node.subagent_count} 个子 Agent，其余未显示。`));
    }
    for (const sub of node.subagents || []) {
      const card = el("section", "working-subagent"), subfields = el("dl", "working-fields");
      card.append(el("strong", "", sub.title || sub.id));
      [["状态", status(sub.status)], ["阶段", sub.phase], ["任务概览", sub.task_summary], ["任务", sub.task], ["模型", sub.model]].forEach(([label, content]) => field(subfields, label, content));
      card.append(subfields); pane.append(card);
    }
    pane.scrollTop = scroll;
  }
  function render() {
    if (!active) return;
    const root = view(), header = el("header", "working-header"), layout = el("div", "working-layout"), list = el("div", "working-runs"), pane = el("aside", "working-detail");
    const notice = el("p", "working-refresh-notice", refreshMessage); notice.setAttribute("role", "status"); notice.hidden = !refreshMessage;
    header.append(el("div", "", "WORKING"), el("h1", "", "运行中的 Flow"), el("p", "working-muted", "查看当前运行的节点与子 Agent。"), notice);
    if (!rows.length) list.append(el("p", "working-empty", refreshMessage ? "等待运行状态更新。" : "当前没有运行中的 Flow。"));
    for (const run of rows) {
      const section = el("section", "working-run"), heading = el("div", "working-run-heading");
      section.dataset.run = run.run_id;
      heading.append(el("h2", "", run.title || run.flow_id), el("span", "working-status", status(run.status))); section.append(heading);
      const nodes = el("div", "working-nodes");
      for (const node of run.nodes || []) {
        const wrap = el("div", "working-node-wrap"), button = el("button", "working-node"); button.type = "button";
        button.setAttribute("aria-pressed", String(selected?.run === run.run_id && selected?.node === node.id));
        button.dataset.run = run.run_id; button.dataset.node = node.id;
        const tip = el("div", "working-tip"); tip.setAttribute("role", "tooltip"); tip.id = `working-tip-${list.querySelectorAll(".working-tip").length}-${nodes.children.length}`; button.setAttribute("aria-describedby", tip.id);
        updateNode(button, tip, node);
        wrap.addEventListener("pointerenter", () => positionTip(button, tip));
        button.addEventListener("focus", () => positionTip(button, tip));
        button.addEventListener("click", () => { selected = { run: run.run_id, node: node.id }; root.querySelectorAll(".working-node").forEach((n) => n.setAttribute("aria-pressed", String(n === button))); details(); });
        wrap.append(button, tip); nodes.append(wrap);
      }
      section.append(nodes); list.append(section);
    }
    layout.append(list, pane); root.replaceChildren(header, layout); details();
  }
  async function refresh() {
    if (!active || pending) return;
    const controller = new AbortController(), ticket = generation;
    pending = controller; const deadline = setTimeout(() => controller.abort(), 10000);
    try {
      const result = await bridge.api("/api/working", { signal: controller.signal }); if (!active || ticket !== generation) return;
      refreshMessage = "";
      const next = result.runs || [], changed = structure(rows) !== structure(next);
      const focused = document.activeElement?.closest?.(".working-node"), focusedId = focused && { run: focused.dataset.run, node: focused.dataset.node };
      const hadFocus = view().contains(document.activeElement), scroll = view().querySelector(".working-runs")?.scrollTop || 0;
      rows = next; $("working-count").textContent = rows.length;
      if (selected && !rows.some((r) => r.run_id === selected.run && (r.nodes || []).some((n) => n.id === selected.node))) selected = null;
      if (changed || !view().querySelector(".working-layout")) {
        render(); const list = view().querySelector(".working-runs"); if (list) list.scrollTop = scroll;
        if (hadFocus) {
          const target = focusedId && [...view().querySelectorAll(".working-node")].find((n) => n.dataset.run === focusedId.run && n.dataset.node === focusedId.node);
          (target || view()).focus({ preventScroll: true });
        }
      } else {
        view().querySelectorAll(".working-run").forEach((section) => {
          const run = rows.find((r) => r.run_id === section.dataset.run);
          if (run) { section.querySelector("h2").textContent = run.title || run.flow_id; section.querySelector(".working-run-heading .working-status").textContent = status(run.status); }
        });
        view().querySelectorAll(".working-node").forEach((button) => {
          const node = rows.find((r) => r.run_id === button.dataset.run)?.nodes?.find((n) => n.id === button.dataset.node);
          if (node) { const tip = button.nextElementSibling; updateNode(button, tip, node); if (button.parentElement.matches(":hover") || document.activeElement === button) positionTip(button, tip); }
        });
        details();
      }
      const notice = view().querySelector(".working-refresh-notice"); if (notice) { notice.textContent = ""; notice.hidden = true; }
      const empty = view().querySelector(".working-empty"); if (empty) empty.textContent = "当前没有运行中的 Flow。";
    } catch (error) {
      if (active && ticket === generation) {
        refreshMessage = rows.length ? "无法更新运行状态，当前显示的是上次成功获取的数据。" : "无法更新运行状态，请稍后重试。";
        const notice = view().querySelector(".working-refresh-notice"); if (notice) { notice.textContent = refreshMessage; notice.hidden = false; }
        const empty = view().querySelector(".working-empty"); if (empty) empty.textContent = "运行状态暂不可用。";
      }
    } finally {
      clearTimeout(deadline);
      if (pending === controller) { pending = null; if (active && ticket === generation) timer = setTimeout(refresh, 2000); }
    }
  }
  function hide() { active = false; generation++; clearTimeout(timer); if (pending) { pending.abort(); pending = null; } rows = []; selected = null; detailPane = null; detailSnapshot = null; refreshMessage = "正在更新运行状态…"; document.body.classList.remove("working-active"); view().hidden = true; $("working-open").classList.remove("active"); }
  function init(options) {
    bridge = options;
    view().tabIndex = -1;
    $("working-open").addEventListener("click", () => { if (active) return; bridge.enter(); active = true; document.body.classList.add("working-active"); view().hidden = false; $("working-open").classList.add("active"); render(); refresh(); });
    const reposition = () => {
      if (!active) return;
      view().querySelectorAll(".working-node-wrap").forEach((wrap) => {
        const button = wrap.querySelector(".working-node");
        if (wrap.matches(":hover") || document.activeElement === button) positionTip(button, wrap.querySelector(".working-tip"));
      });
    };
    window.addEventListener("resize", reposition); document.addEventListener("scroll", reposition, true);
  }
  function snapshot(snapshot) {
    if (Array.isArray(snapshot.flows)) $("working-count").textContent = snapshot.flows.filter((f) => ["queued", "running", "cancelling"].includes(f.status)).length;
  }
  window.WorkingUI = { init, hide, snapshot, isActive: () => active };
})();
