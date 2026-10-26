// The Tasks panel on Orchestra's Dashboard (a plugin script: Orchestra.panels). One entry per
// ticket in flight, as the old Dashboard had them: a card in the ticket's state colour with its id,
// title, re-engagement badge and state, and a lane with the machine that owns it now (the
// scheduler assigns it: worker/1, then reviewer/1, then worker/1 again after a rejection) and what
// that machine is doing, or what the ticket waits for. Above them, the queue by state and the
// capacity and pause controls. What the machine is saying, and its transcript, come from the
// agents' plugin when it is installed (/api/plugins/agents/agents; orchestra-agents); without it a
// card says who holds the ticket and since when. A card opens the transcript if there is one,
// otherwise the ticket; the id always opens the ticket.
(function () {
  if (!window.Orchestra || !Orchestra.panels) return;
  const API = "/api/plugins/tasks";
  if (!document.querySelector('link[href^="/plugins/tasks/static/tasks.css"]')) {
    const l = document.createElement("link"); l.rel = "stylesheet"; l.href = "/plugins/tasks/static/tasks.css"; document.head.appendChild(l);
  }
  window.__tasksJs = window.__tasksJs || (typeof window.sessionChip === "function" ? Promise.resolve()
    : new Promise((ok) => { const s = document.createElement("script"); s.src = "/plugins/tasks/static/tasks.js"; s.onload = ok; s.onerror = ok; document.head.appendChild(s); }));
  const ORDER = ["OPEN", "IN_PROGRESS", "READY_FOR_REVIEW", "IN_REVIEW", "REJECTED", "BLOCKED", "APPROVED", "MERGING", "DONE", "CANCELLED"];

  function turns(t) {
    if (!t.worker_turns || t.worker_turns < 2) return "";
    const other = t.worker_turns - 1 - (t.reviewer_rejections || 0) - (t.gate_rejections || 0);
    const bits = [`↺${t.worker_turns}`];
    if (t.reviewer_rejections) bits.push(`✗${t.reviewer_rejections}`);
    if (t.gate_rejections) bits.push(`⛔${t.gate_rejections}`);
    if (other > 0) bits.push(`⟳${other}`);
    return `<span class="turns" title="worker opened ${t.worker_turns} times: ${t.reviewer_rejections || 0} after a reviewer rejection${other > 0 ? `, ${other} after a crash, timeout or operator stop` : ""}">${bits.join(" ")}</span>`;
  }

  Orchestra.panels.register({ id: "tasks", title: "Tasks", size: { w: 12, h: 9 }, mount(body, ui) {
    let D = null, A = {};                       // A: the agents' sessions by id, when the agents' plugin answers
    const live = (t) => (t.assignment && t.assignment.session && A[t.assignment.session]) || null;
    const ago = (ts) => { const s = Math.max(0, Math.round(D.now - ts)); return s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`; };
    const control = async (b) => {
      const r = await fetch(API + "/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
      if (!r.ok) alert(await r.text()); refresh();
    };
    window.TasksPanel = {
      capacity(kind, d) { const c = (D && D.capacity) || {}; const b = { action: "capacity", workers: c.max_workers, reviewers: c.max_reviewers }; b[kind] = Math.max(0, (c["max_" + kind] || 0) + d); control(b); },
      pause() { control({ action: D && D.pickup_paused ? "resume" : "pause" }); },
      open(t) { __tasksJs.then(() => (live(t) && window.openTranscript ? openTranscript(live(t).session) : openTicket(t.id))); },
      ticket(id) { __tasksJs.then(() => openTicket(id)); },
    };
    const cap = (kind) => { const n = ((D && D.capacity) || {})["max_" + kind];
      const step = (d) => `<button class="ctl" style="padding:0 6px;margin:0" onclick="TasksPanel.capacity('${kind}',${d})" title="${d < 0 ? "one fewer" : "one more"}">${d < 0 ? "−" : "+"}</button>`;
      return `<span title="how many ${kind} may run at once">${step(-1)}<b style="margin:0 4px">${n ?? "?"}</b>${step(1)} ${kind}</span>`; };

    function render() {
      if (!D) return;
      const chip = (s) => (s && typeof sessionChip === "function" ? sessionChip(s, D.now) : "");
      const says = (t) => { const s = live(t); if (s) return s.last_text || "";
        if (t.assignment) return t.assignment.instance ? `${t.assignment.role} since ${ago(t.assignment.since)} ago` : `offered to a ${t.assignment.role}, not taken yet`;
        return t.state === "BLOCKED" ? t.blocked_reason || t.waiting : t.waiting || ""; };
      const q = ORDER.filter((s) => D.counts[s]).map((s) => `<span style="white-space:nowrap"><span class="pill" style="--c:${color(s)}">${esc(s.replace(/_/g, " "))}</span> <b class="num">${D.counts[s]}</b></span>`).join(" ");
      const cards = D.tickets.map((t, i) => `<div class="work" style="--c:${color(t.state)}" onclick="TasksPanel.open(TasksPanel.rows[${i}])" title="${live(t) ? "Open the transcript of the agent working it" : "Open the ticket"}">
          <div class="top"><span><span class="id" onclick="event.stopPropagation();TasksPanel.ticket('${esc(t.id)}')" title="Open the ticket">${esc(t.id)}</span> <span class="title">${esc(t.title)}</span></span>
            <span class="right">${turns(t)}<span class="pill">${esc(t.state.replace(/_/g, " "))}</span></span></div>
          <div class="lane"><span class="who">${esc(t.owner || "—")}</span><span class="msg">${esc(says(t))}</span>${chip(live(t))}</div></div>`).join("");
      window.TasksPanel.rows = D.tickets;
      const open = D.counts.OPEN || 0, done = D.counts.DONE || 0;
      ui.setSub(`${D.tickets.length} in flight · ${open} waiting to start · ${done} done${D.scheduler ? "" : " · the scheduler is not running"}`);
      body.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center;margin-bottom:10px">
          <span style="flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:6px">${q || '<span class="muted">no tickets yet</span>'}</span>
          <span style="display:flex;gap:10px;align-items:center;font-size:11.5px;color:var(--text2)">${cap("workers")}${cap("reviewers")}
            <button class="ctl ${D.pickup_paused ? "paused" : ""}" style="margin:0" onclick="TasksPanel.pause()" title="${D.pickup_paused ? "Resume offering work to workers." : "Stop offering new work to workers. Work under way finishes; reviews go on."}">${D.pickup_paused ? "▶ resume" : "⏸ pause new workers"}</button></span></div>
        ${cards || `<div class="empty">No ticket is in flight.${open ? ` ${open} wait${open === 1 ? "s" : ""} to start: a worker takes the next ready one when a slot is free.` : ""}</div>`}`;
    }
    async function refresh() {
      try {
        const d = await (await fetch(API + "/board")).json();
        if (d.ok) {
          D = d;
          if (d.tickets.some((t) => t.assignment && t.assignment.session)) {
            const a = await fetch("/api/plugins/agents/agents").then((r) => (r.ok ? r.json() : null)).catch(() => null);
            A = a && a.ok ? Object.fromEntries(a.sessions.map((s) => [s.session, s])) : {};
          }
          await __tasksJs; render();
        }
      }
      catch (e) { body.innerHTML = '<div class="empty">The task system is not answering.</div>'; }
    }
    refresh(); setInterval(refresh, 3000);
  } });
})();
