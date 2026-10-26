// The Tasks panel on Orchestra's Dashboard (Orchestra.panels; loaded by tasks-boot.js after
// tasks.js). One card per ticket in flight (not waiting to start, not finished), in the ticket's
// state colour, with its id, title, worker-turns badge and state, and a lane that says which
// machine instance holds it now and since when (the scheduler offers it: worker/1 takes it, then
// reviewer/1, then a worker again after a rejection), that it is offered and to whom, or what it
// waits for. Above the cards, the queue by state and the operator's capacity and pause controls.
// A card opens the ticket.
(function () {
  if (!window.Orchestra || !Orchestra.panels) return;
  const ORDER = ["OPEN", "IN_PROGRESS", "READY_FOR_REVIEW", "IN_REVIEW", "REJECTED", "BLOCKED", "APPROVED", "MERGING", "DONE", "CANCELLED"];

  function turns(t) {
    if (!t.worker_turns || t.worker_turns < 2) return "";
    const other = t.worker_turns - 1 - (t.reviewer_rejections || 0) - (t.gate_rejections || 0);
    const bits = [`↺${t.worker_turns}`];
    if (t.reviewer_rejections) bits.push(`✗${t.reviewer_rejections}`);
    if (t.gate_rejections) bits.push(`⛔${t.gate_rejections}`);
    if (other > 0) bits.push(`⟳${other}`);
    return `<span class="turns" title="worker turns: ${t.worker_turns} (${turnsWhy(t).join(", ")}); ✗ reviewer rejections, ⛔ gate rejections, ⟳ crashes, timeouts or operator stops">${bits.join(" ")}</span>`;
  }

  Orchestra.panels.register({ id: "tasks", title: "Tasks", size: { w: 12, h: 9 }, mount(body, ui) {
    let D = null;
    const ago = (ts) => { const s = Math.max(0, Math.round(D.now - ts)); return s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`; };
    window.TasksPanel = { ...taskControlActions(() => D, refresh), ticket: openTicket };

    function render() {
      if (!D) return;
      const says = (t) => {   // one line: a blocker's first paragraph, without its markdown backticks (the drawer has it whole)
        if (t.assignment) return t.assignment.instance ? `${t.assignment.instance} · since ${ago(t.assignment.since)}` : `offered to a ${t.assignment.role} ${ago(t.assignment.since)} ago`;
        return t.state === "BLOCKED" ? (t.blocked_reason || t.waiting || "").split(/\n\s*\n/)[0].replace(/`/g, "") : t.waiting || ""; };
      const q = ORDER.filter((s) => D.counts[s]).map((s) => `<span style="white-space:nowrap"><span class="pill" style="--c:${color(s)}">${esc(s.replace(/_/g, " "))}</span> <b class="num">${D.counts[s]}</b></span>`).join(" ");
      const cards = D.tickets.map((t) => `<div class="work" style="--c:${color(t.state)}" onclick="TasksPanel.ticket('${esc(t.id)}')" title="Open the ticket">
          <div class="top"><span><span class="id">${esc(t.id)}</span> <span class="title">${esc(t.title)}</span></span>
            <span class="right">${turns(t)}<span class="pill">${esc(t.state.replace(/_/g, " "))}</span></span></div>
          <div class="lane"><span class="msg">${esc(says(t))}</span></div></div>`).join("");
      const sum = taskSummary(D), open = sum.start;
      ui.setSub(sum.text + (D.scheduler ? "" : " · the scheduler is not running"));
      body.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin-bottom:10px">
          <span style="flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:6px">${q || '<span class="muted">no tickets yet</span>'}</span>
          <span style="display:flex;gap:10px;align-items:center;font-size:11.5px;color:var(--text2)">${taskControls(D, "TasksPanel")}</span></div>
        ${cards || `<div class="empty">No ticket is in flight.${open ? ` ${open} wait${open === 1 ? "s" : ""} to start: the next ready one is offered to a worker when a slot is free.` : ""}</div>`}`;
    }
    async function refresh() {
      try {
        const d = await (await fetch(TASKS_API + "/board")).json();
        if (d.ok) { D = d; render(); }
      }
      catch (e) { body.innerHTML = '<div class="empty">The task system is not answering.</div>'; }
    }
    refresh(); setInterval(refresh, 3000);
  } });
})();
