// The task system's machine on Orchestra's pages (Orchestra.machineCards in Orchestra's app.js;
// loaded by tasks-boot.js after tasks.js), inside Orchestra's machine-card frame: task_scheduler is
// the task queue, the tickets by state, with the capacity and pause controls. The tickets
// themselves are the Tasks panel's (tasks-panel.js); the machines that work them are not the task
// system's and get their own cards.
(function () {
  if (!window.Orchestra || !Orchestra.machineCards) return;
  let D = null, last = "";
  const ORDER = ["OPEN", "IN_PROGRESS", "READY_FOR_REVIEW", "IN_REVIEW", "REJECTED", "BLOCKED", "APPROVED", "MERGING", "DONE", "CANCELLED"];
  const more = (m, compact) => (compact ? "" : `<div class="tkc-more" onclick="event.stopPropagation();Orchestra.machineCards.openDefault('${esc(m.id)}')">machine details</div>`);

  const css = document.createElement("style");
  css.textContent = `
    .tkc-more { text-align:right; font-size:10.5px; color:var(--muted); text-decoration:underline dotted; cursor:pointer; margin:6px 2px 0 }
    .tkc-q { display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 6px }
    .tkc-q .pill { cursor:default } .tkc-q b { font-family:var(--mono); font-size:12px; margin-left:4px }
    .tkc-ctl { display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:11.5px; color:var(--text2); margin:0 0 8px }
    .tkc-sub { font-weight:400; font-size:12px; color:var(--muted); margin-left:6px }`;
  document.head.appendChild(css);

  // The title as Orchestra's own card draws it: an activity dot and the name in an h3.
  const title = (m) => `<h3><span class="dot ${m.active ? "" : "off"}" style="--rc:${typeof mcolor === "function" ? mcolor(m.id) : "#22c55e"}"></span>${esc(m.id)}`;

  function schedulerCard(m, compact) {
    if (!D) return `${title(m)}</h3><div class="empty">reading…</div>`;
    const q = ORDER.filter((s) => D.counts[s]).map((s) => `<span><span class="pill" style="--c:${color(s)}">${esc(s.replace(/_/g, " "))}</span><b>${D.counts[s]}</b></span>`).join("")
      || '<span class="muted">no tickets yet</span>';
    return `${title(m)}<span class="tkc-sub">· the task queue</span>${D.scheduler ? "" : '<span class="tkc-sub" style="color:#f87171">· not running</span>'}</h3>
      <div class="tkc-ctl">${taskControls(D, "TaskCards")}</div>
      <div class="tkc-q">${q}</div>${more(m, compact)}`;
  }

  window.TaskCards = taskControlActions(() => D, () => refresh(true));

  Orchestra.machineCards.register("tasks", {
    rank: () => 20,
    summary(m) {
      if (m.id !== "task_scheduler" || !D) return null;
      return taskSummary(D, { finished: false }).text;
    },
    card(m, { compact } = {}) { return m.id === "task_scheduler" ? schedulerCard(m, compact) : null; },
    open() { return false; },
  });

  async function refresh(force) {
    try {
      const d = await (await fetch(TASKS_API + "/board")).json();
      if (!d.ok) return;
      const sig = JSON.stringify([d.counts, d.capacity, d.pickup_paused, d.scheduler, d.tickets.map((t) => t.assignment && t.assignment.instance)]);
      D = d;
      if (force || sig !== last) { last = sig; Orchestra.machineCards.changed(); }
    } catch (e) {}
  }
  refresh(true); setInterval(refresh, 3000);
})();
