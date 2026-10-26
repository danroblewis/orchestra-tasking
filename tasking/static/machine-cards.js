// The task system's machines on Orchestra's pages (a plugin script; Orchestra.machineCards in
// Orchestra's app.js), drawn as the old Agents column drew them:
// - a resident agent (the Supervisor, the Coordinator) is a row: its letter in its role's colour,
//   what it last said, and its session chip (resident, working, waiting);
// - workers and reviewers are their tickets: a card per ticket an instance is on, in the ticket's
//   state colour, with its id, title, re-engagement badge and state, and a lane with what the
//   agent is saying now;
// - the scheduler is the queue by state, with the capacity and pause controls.
// A click opens the agent's transcript (the ticket id opens the ticket); "machine details", on the
// Machines page, opens Orchestra's own view of the machine.
(async function () {
  if (!window.Orchestra || !Orchestra.machineCards) return;
  const API = "/api/plugins/tasks";
  if (typeof window.sessionChip !== "function") {          // the transcript drawer, the chat, the session chips
    await new Promise((ok) => { const s = document.createElement("script"); s.src = "/plugins/tasks/static/tasks.js"; s.onload = ok; s.onerror = ok; document.head.appendChild(s); });
  }
  let D = null, last = "";
  const e_ = (s) => esc(s);
  const ROLE_C = { supervisor: "var(--r-supervisor)", coordinator: "var(--r-coordinator)", worker: "var(--r-worker)", reviewer: "var(--r-reviewer)" };
  const RANK = { supervisor: 10, coordinator: 11, task_scheduler: 20, worker: 30, reviewer: 31 };
  const chip = (s) => (typeof sessionChip === "function" ? sessionChip(s, D ? D.now : Date.now() / 1000) : "");
  const kindOf = (m) => (m.about && m.about.kind) || (m.declared && m.declared.kind);
  const roleOf = (m) => (m.about && m.about.role) || m.id;
  const sessionsOf = (m) => (D ? D.sessions.filter((s) => s.role === roleOf(m)) : []);
  const more = (m, compact) => (compact ? "" : `<div class="tkc-more" onclick="event.stopPropagation();Orchestra.machineCards.openDefault('${e_(m.id)}')">machine details</div>`);

  const css = document.createElement("style");
  css.textContent = `
    .claimed .tkc-more { text-align:right; font-size:10.5px; color:var(--muted); text-decoration:underline dotted; cursor:pointer; margin:-2px 2px 4px }
    .claimed h2 { margin-top:10px }
    .claimed .tkc-q { display:flex; flex-wrap:wrap; gap:6px; margin:2px 0 6px }
    .claimed .tkc-q .pill { cursor:default } .claimed .tkc-q b { font-family:var(--mono); font-size:12px; margin-left:4px }
    .claimed .agent.unseen-agent { opacity:.6; border-style:dashed }`;
  document.head.appendChild(css);

  // how many times the worker has been re-engaged on a ticket, and why (as the old Dashboard)
  function turns(t) {
    if (!t || !t.worker_turns || t.worker_turns < 2) return "";
    const other = t.worker_turns - 1 - (t.reviewer_rejections || 0) - (t.gate_rejections || 0);
    const bits = [`↺${t.worker_turns}`];
    if (t.reviewer_rejections) bits.push(`✗${t.reviewer_rejections}`);
    if (t.gate_rejections) bits.push(`⛔${t.gate_rejections}`);
    if (other > 0) bits.push(`⟳${other}`);
    const title = `worker opened ${t.worker_turns} times: ${t.reviewer_rejections || 0} after a reviewer rejection` + (other > 0 ? `, ${other} after a crash, timeout, or operator stop` : "");
    return `<span class="turns" title="${e_(title)}">${bits.join(" ")}</span>`;
  }
  const capacity = (kind) => {
    const c = (D && D.capacity) || {}, n = c["max_" + kind];
    const step = (d) => `<button class="ctl" style="padding:0 6px;margin:0" onclick="event.stopPropagation();TaskCards.capacity('${kind}',${d})" title="${d < 0 ? "one fewer" : "one more"} ${kind.slice(0, -1)}">${d < 0 ? "−" : "+"}</button>`;
    return `<span title="How many ${kind} may run at once">${step(-1)}<b style="margin:0 4px">${n ?? "?"}</b>${step(1)} ${kind}</span>`;
  };
  const pause = () => `<button class="ctl ${D && D.pickup_paused ? "paused" : ""}" style="margin:0" onclick="event.stopPropagation();TaskCards.pause()" title="${D && D.pickup_paused ? "Resume starting new workers." : "Stop starting new workers. Running turns finish; reviews and the resident agents go on."}">${D && D.pickup_paused ? "▶ resume" : "⏸ pause new workers"}</button>`;

  function residentCard(m, compact) {
    const s = sessionsOf(m).sort((a, b) => (b.started_at || 0) - (a.started_at || 0))[0];
    // the stored text is a message's tail: start it at a sentence, not mid-word
    const tail = s ? s.last_text || "" : "", cut = tail.search(/[.!?]\s+[A-Z*`(]/);
    const role = roleOf(m), say = tail.length >= 290 && cut >= 0 && cut < 160 ? tail.slice(cut + 1).trim() : tail;
    const text = m.unseen ? "declared, not run yet" : say || "waiting for work";
    return `<div class="agent ${m.unseen ? "unseen-agent" : ""}" style="--rc:${ROLE_C[role] || "#c4b5fd"}" title="${e_((m.declared || {}).description || role)}">
        <div class="av">${e_(m.id[0].toUpperCase())}</div><div class="name">${e_(m.id)}</div><div class="say">${e_(text)}</div>${s ? chip(s) : ""}</div>${more(m, compact)}`;
  }

  function ticketsCard(m, compact) {
    const role = roleOf(m), live = sessionsOf(m).filter((s) => s.status === "running");
    const plural = role === "reviewer" ? "Reviewers" : "Workers";
    const cards = live.map((s) => {
      const t = s.ticket || { id: s.session, title: "", state: "IN_PROGRESS" };
      return `<div class="work" style="--c:${color(t.state)}" onclick="event.stopPropagation();TaskCards.transcript('${e_(s.session)}')" title="Open the running agent's transcript; the ticket id opens the ticket">
        <div class="top"><span><span class="id" onclick="event.stopPropagation();TaskCards.ticket('${e_(t.id)}')" title="Open the ticket">${e_(t.id)}</span> <span class="title">${e_(t.title)}</span></span>
        <span class="right">${turns(t)}<span class="pill">${e_(t.state.replace(/_/g, " "))}</span></span></div>
        <div class="lane"><span class="who">${e_(s.instance || role)}</span><span class="msg">${e_(s.last_text || "")}</span>${chip(s)}</div></div>`;
    }).join("");
    const empty = m.unseen ? `<div class="empty">Declared, not run yet: ${e_(role)}s start when a ticket is ready${role === "reviewer" ? " for review" : ""}.</div>`
      : `<div class="empty">No ${e_(role)} is on a ticket right now.${role === "worker" ? " Ready tickets start as soon as a worker slot is free." : ""}</div>`;
    return `<h2><span>${plural} on tickets</span><span class="hctl"><span class="count">${live.length}</span></span></h2>${cards || empty}${more(m, compact)}`;
  }

  function schedulerCard(m, compact) {
    if (!D) return `<h2><span>Task queue</span></h2><div class="empty">reading…</div>`;
    const order = ["OPEN", "IN_PROGRESS", "READY_FOR_REVIEW", "IN_REVIEW", "REJECTED", "BLOCKED", "APPROVED", "DONE", "CANCELLED"];
    const q = order.filter((s) => D.tickets[s]).map((s) => `<span><span class="pill" style="--c:${color(s)}">${e_(s.replace(/_/g, " "))}</span><b>${D.tickets[s]}</b></span>`).join("")
      || '<span class="muted">no tickets yet</span>';
    return `<h2><span>Task queue${D.loop.alive ? "" : ' <span style="color:#f87171;text-transform:none">· scheduler not running</span>'}</span>
        <span class="hctl">${capacity("workers")}${capacity("reviewers")}${D.pickup_paused ? '<span style="color:#fde68a">new workers PAUSED</span>' : ""}${pause()}</span></h2>
      <div class="tkc-q">${q}</div>${more(m, compact)}`;
  }

  async function control(body) {
    const r = await fetch(API + "/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) alert(await r.text());
    refresh(true);
  }
  window.TaskCards = {
    transcript: (sid) => openTranscript(sid),
    ticket: (id) => openTicket(id),
    capacity: (kind, d) => { const c = (D && D.capacity) || {}; const body = { action: "capacity", workers: c.max_workers, reviewers: c.max_reviewers }; body[kind] = Math.max(0, (c["max_" + kind] || 0) + d); control(body); },
    pause: () => control({ action: D && D.pickup_paused ? "resume" : "pause" }),
  };

  Orchestra.machineCards.register("tasks", {
    bare: true,
    rank: (m) => RANK[roleOf(m)] ?? (kindOf(m) === "agent" ? 12 : 25),
    card(m, { compact } = {}) {
      if (m.id === "task_scheduler") return schedulerCard(m, compact);
      if (kindOf(m) !== "agent") return null;
      return m.mode === "ad hoc" ? ticketsCard(m, compact) : residentCard(m, compact);
    },
    open(m) {
      if (kindOf(m) !== "agent" || m.unseen) return false;
      const s = sessionsOf(m).filter((x) => x.status === "running")[0] || sessionsOf(m).sort((a, b) => (b.started_at || 0) - (a.started_at || 0))[0];
      if (!s) return false;
      openTranscript(s.session);
      return true;
    },
  });

  async function refresh(force) {
    try {
      const d = await (await fetch(API + "/agents")).json();
      if (!d.ok) return;
      const sig = JSON.stringify([d.sessions.map((s) => [s.session, s.status, s.phase, s.last_text, s.tool_name]), d.tickets, d.capacity, d.pickup_paused, d.loop.alive]);
      D = d;
      if (force || sig !== last) { last = sig; Orchestra.machineCards.changed(); }
    } catch (e) {}
  }
  refresh(true); setInterval(refresh, 3000);
})();
