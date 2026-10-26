// The task system's machine on Orchestra's pages (a plugin script; Orchestra.machineCards in
// Orchestra's app.js), inside Orchestra's machine-card frame: the scheduler is the queue by state,
// with the capacity and pause controls. The tickets themselves are the Tasks panel's
// (tasks-panel.js); the machines that work them are not the task system's and get their own cards.
(function () {
  if (!window.Orchestra || !Orchestra.machineCards) return;
  const API = "/api/plugins/tasks";
  if (!document.querySelector('link[href^="/plugins/tasks/static/tasks.css"]')) {        // the task system's look
    const l = document.createElement("link"); l.rel = "stylesheet"; l.href = "/plugins/tasks/static/tasks.css"; document.head.appendChild(l);
  }
  window.__tasksJs = window.__tasksJs || new Promise((ok) => { const s = document.createElement("script"); s.src = "/plugins/tasks/static/tasks.js"; s.onload = ok; s.onerror = ok; document.head.appendChild(s); });
  let D = null, last = "";
  const e_ = (s) => esc(s);
  const ORDER = ["OPEN", "IN_PROGRESS", "READY_FOR_REVIEW", "IN_REVIEW", "REJECTED", "BLOCKED", "APPROVED", "MERGING", "DONE", "CANCELLED"];
  const more = (m, compact) => (compact ? "" : `<div class="tkc-more" onclick="event.stopPropagation();Orchestra.machineCards.openDefault('${e_(m.id)}')">machine details</div>`);

  const css = document.createElement("style");
  css.textContent = `
    .tkc-more { text-align:right; font-size:10.5px; color:var(--muted); text-decoration:underline dotted; cursor:pointer; margin:6px 2px 0 }
    .tkc-q { display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 6px }
    .tkc-q .pill { cursor:default } .tkc-q b { font-family:var(--mono); font-size:12px; margin-left:4px }`;
  document.head.appendChild(css);

  const capacity = (kind) => {
    const n = ((D && D.capacity) || {})["max_" + kind];
    const step = (d) => `<button class="ctl" style="padding:0 6px;margin:0" onclick="event.stopPropagation();TaskCards.capacity('${kind}',${d})" title="${d < 0 ? "one fewer" : "one more"} ${kind.slice(0, -1)}">${d < 0 ? "−" : "+"}</button>`;
    return `<span title="How many tickets may be offered to ${kind} at once">${step(-1)}<b style="margin:0 4px">${n ?? "?"}</b>${step(1)} ${kind}</span>`;
  };
  const pause = () => `<button class="ctl ${D && D.pickup_paused ? "paused" : ""}" style="margin:0" onclick="event.stopPropagation();TaskCards.pause()" title="${D && D.pickup_paused ? "Resume offering work to workers." : "Stop offering new work to workers. Work under way finishes; reviews go on."}">${D && D.pickup_paused ? "▶ resume" : "⏸ pause new workers"}</button>`;

  function schedulerCard(m, compact) {
    if (!D) return `<h2><span>Task queue</span></h2><div class="empty">reading…</div>`;
    const q = ORDER.filter((s) => D.counts[s]).map((s) => `<span><span class="pill" style="--c:${color(s)}">${e_(s.replace(/_/g, " "))}</span><b>${D.counts[s]}</b></span>`).join("")
      || '<span class="muted">no tickets yet</span>';
    return `<h2><span>Task queue${D.scheduler ? "" : ' <span style="color:#f87171;text-transform:none">· scheduler not running</span>'}</span>
        <span class="hctl">${capacity("workers")}${capacity("reviewers")}${D.pickup_paused ? '<span style="color:#fde68a">new workers PAUSED</span>' : ""}${pause()}</span></h2>
      <div class="tkc-q">${q}</div>${more(m, compact)}`;
  }

  async function control(body) {
    const r = await fetch(API + "/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) alert(await r.text());
    refresh(true);
  }
  window.TaskCards = {
    capacity: (kind, d) => { const c = (D && D.capacity) || {}; const body = { action: "capacity", workers: c.max_workers, reviewers: c.max_reviewers }; body[kind] = Math.max(0, (c["max_" + kind] || 0) + d); control(body); },
    pause: () => control({ action: D && D.pickup_paused ? "resume" : "pause" }),
  };

  Orchestra.machineCards.register("tasks", {
    rank: () => 20,
    summary(m) {
      if (m.id !== "task_scheduler" || !D) return null;
      return `${(D.counts.IN_PROGRESS || 0) + (D.counts.IN_REVIEW || 0)} being worked · ${D.counts.OPEN || 0} waiting`;
    },
    card(m, { compact } = {}) { return m.id === "task_scheduler" ? schedulerCard(m, compact) : null; },
    open() { return false; },
  });

  async function refresh(force) {
    try {
      const d = await (await fetch(API + "/board")).json();
      if (!d.ok) return;
      await window.__tasksJs;                                   // color()
      const sig = JSON.stringify([d.counts, d.capacity, d.pickup_paused, d.scheduler]);
      D = d;
      if (force || sig !== last) { last = sig; Orchestra.machineCards.changed(); }
    } catch (e) {}
  }
  refresh(true); setInterval(refresh, 3000);
})();
