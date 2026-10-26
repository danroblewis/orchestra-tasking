// The task system's machines on Orchestra's pages (a plugin script; Orchestra.machineCards in
// Orchestra's app.js). Orchestra knows them as sources on the bus; here each gets the card the
// task system can draw: an agent with what each instance is doing now and on which ticket, the
// scheduler with the queue it works from. A click opens the agent's session transcript; "machine
// details" opens Orchestra's own view of the machine (events, metrics, log).
(function () {
  if (!window.Orchestra || !Orchestra.machineCards) return;
  const API = "/api/plugins/tasks";
  let D = null, last = "";
  const e_ = (s) => esc(s);
  const since = (t) => (t ? ago(Date.now() / 1000 - t) : "");
  const STATE_C = { OPEN: "#94a3b8", IN_PROGRESS: "#60a5fa", READY_FOR_REVIEW: "#c084fc", IN_REVIEW: "#a78bfa", REJECTED: "#f87171",
    APPROVED: "#34d399", MERGING: "#2dd4bf", DONE: "#22c55e", BLOCKED: "#f59e0b", CANCELLED: "#64748b" };
  const pill = (st) => `<span class="tkc-pill" style="--c:${STATE_C[st] || "#94a3b8"}">${e_(st.replace(/_/g, " ").toLowerCase())}</span>`;

  const css = document.createElement("style");
  css.textContent = `
    .tkc-h { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:0 0 8px; font-size:14px }
    .tkc-h .tkc-kind { font-size:10.5px; padding:1px 7px; border-radius:999px; border:1px solid #7c6cf0; color:#c4b5fd }
    .tkc-h .tkc-kind.prog { border-color:#475569; color:#cbd5e1 }
    .tkc-h .tkc-more { margin-left:auto; font-size:11px; color:var(--muted); text-decoration:underline dotted; cursor:pointer }
    .tkc-sub { font-size:11.5px; color:var(--muted); margin:-4px 0 8px }
    .tkc-inst { display:grid; grid-template-columns:auto 1fr; gap:3px 10px; padding:7px 0; border-top:1px solid var(--border, #222a38); align-items:baseline }
    .tkc-inst .tkc-id { font-family:var(--mono); font-size:11.5px; color:#c4b5fd }
    .tkc-inst .tkc-tk { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12.5px }
    .tkc-inst .tkc-tk code { color:var(--text); font-weight:600; cursor:pointer }
    .tkc-inst .tkc-txt { grid-column:2; font-size:11.5px; color:var(--muted); overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical }
    .tkc-pill { display:inline-block; font-size:10.5px; padding:0 7px; border-radius:999px; background:color-mix(in oklab, var(--c) 22%, transparent); color:var(--c); border:1px solid color-mix(in oklab, var(--c) 55%, transparent); margin:0 4px }
    .tkc-idle { font-size:12px; color:var(--muted); padding:4px 0 }
    .tkc-q { display:flex; flex-wrap:wrap; gap:6px; margin:4px 0 6px }
    .tkc-q span { font-size:12px } .tkc-q b { font-size:13px }
    .tkc-talk { margin-top:6px; font-size:12px; padding:3px 10px; border-radius:6px; border:1px solid #6ea8fe; background:transparent; color:#93c5fd; cursor:pointer }`;
  document.head.appendChild(css);

  async function tasksJs() {
    if (typeof window.openTranscript === "function") return;
    await new Promise((ok, bad) => { const s = document.createElement("script"); s.src = "/plugins/tasks/static/tasks.js"; s.onload = ok; s.onerror = bad; document.head.appendChild(s); });
  }
  window.TaskCards = {
    transcript: async (sid) => { await tasksJs(); openTranscript(sid); },
    ticket: async (id) => { await tasksJs(); openTicket(id); },
  };

  const role = (m) => (m.about && m.about.role) || m.id;
  const sessionsOf = (m) => (D ? D.sessions.filter((s) => s.role === role(m)) : []);
  const running = (m) => sessionsOf(m).filter((s) => s.status === "running");
  const header = (m, kind) => {
    const a = m.about || {}, agent = kind === "agent";
    return `<div class="tkc-h"><span class="dot ${m.active ? "" : "off"}" style="--rc:${typeof mcolor === "function" ? mcolor(m.id) : "#94a3b8"}"></span><b>${e_(m.id)}</b>
      <span class="tkc-kind ${agent ? "" : "prog"}">${agent ? "AI agent" : "program"}</span>
      ${m.mode ? `<span class="tag">${e_(m.mode)}</span>` : ""}
      <span class="tkc-more" title="Orchestra's view: events, metrics, log" onclick="event.stopPropagation();Orchestra.machineCards.openDefault('${e_(m.id)}')">machine details</span></div>
      ${agent ? `<div class="tkc-sub">${e_(a.harness || (D && D.harness) || "")}${(a.model || (D && D.model)) ? " · " + e_(a.model || D.model) : ""}${m.last_seen ? ` · last event ${since(m.last_seen)} ago` : ""}</div>` : ""}`;
  };
  const instRow = (s) => `<div class="tkc-inst">
      <span class="tkc-id">${e_(s.instance || s.session)}</span>
      <span class="tkc-tk">${s.ticket ? `<code onclick="event.stopPropagation();TaskCards.ticket('${e_(s.ticket.id)}')">${e_(s.ticket.id)}</code>${pill(s.ticket.state)}${e_(s.ticket.title)}` : `<span class="muted">${s.status === "running" ? "working" : "waiting"}</span>`}
        <span class="muted"> · ${s.status === "running" ? (s.phase === "tool" && s.tool ? `running ${e_(s.tool)}` : s.phase || "working") + ` ${since(s.phase_since || s.started_at)}` : `idle ${since(s.settled_at)}`}</span></span>
      ${s.last_text ? `<span class="tkc-txt">${e_(s.last_text)}</span>` : ""}</div>`;

  function agentCard(m, compact) {
    const run = running(m), all = sessionsOf(m);
    const resident = m.mode === "continuous" || m.mode === "scheduled";
    let body;
    if (m.unseen) body = `<div class="tkc-idle">Declared, not run yet. ${e_((m.declared || {}).description || "")}</div>`;
    else if (resident) body = all.slice(0, 1).map(instRow).join("") || `<div class="tkc-idle">not started</div>`;
    else body = run.map(instRow).join("") || `<div class="tkc-idle">No ticket right now${all.length ? ` · ${all.length} ticket session${all.length === 1 ? "" : "s"} so far` : ""}.</div>`;
    const talk = m.id === "supervisor" && !compact ? `<button class="tkc-talk" onclick="event.stopPropagation();TaskAttention ? TaskAttention.open() : TaskCards.transcript('supervisor')">💬 Talk to the Supervisor</button>` : "";
    return header(m, "agent") + body + talk;
  }
  function schedulerCard(m) {
    if (!D) return header(m, "program") + `<div class="tkc-idle">reading…</div>`;
    const order = ["OPEN", "IN_PROGRESS", "READY_FOR_REVIEW", "IN_REVIEW", "REJECTED", "BLOCKED", "APPROVED", "DONE", "CANCELLED"];
    const q = order.filter((s) => D.tickets[s]).map((s) => `<span>${pill(s)}<b>${D.tickets[s]}</b></span>`).join("") || '<span class="muted">no tickets</span>';
    const cap = D.capacity || {};
    return header(m, "program") + `<div class="tkc-q">${q}</div>
      <div class="tkc-sub" style="margin:0">${cap.max_workers ?? "?"} worker${cap.max_workers === 1 ? "" : "s"} · ${cap.max_reviewers ?? "?"} reviewer${cap.max_reviewers === 1 ? "" : "s"}${D.pickup_paused ? ' · <b style="color:#f59e0b">new work paused</b>' : ""}${D.loop.alive ? "" : ' · <b style="color:#f87171">not running</b>'}</div>`;
  }

  Orchestra.machineCards.register("tasks", {
    card(m, { compact } = {}) {
      const kind = (m.about && m.about.kind) || (m.declared && m.declared.kind);
      if (m.id === "task_scheduler") return schedulerCard(m);
      if (kind === "agent") return agentCard(m, compact);
      return null;
    },
    open(m) {
      const kind = (m.about && m.about.kind) || (m.declared && m.declared.kind);
      if (kind !== "agent" || m.unseen) return false;
      const s = running(m)[0] || sessionsOf(m).sort((a, b) => (b.started_at || 0) - (a.started_at || 0))[0];
      if (!s) return false;
      TaskCards.transcript(s.session);
      return true;
    },
  });

  async function refresh() {
    try {
      const d = await (await fetch(API + "/agents")).json();
      if (!d.ok) return;
      const sig = JSON.stringify([d.sessions, d.tickets, d.pickup_paused, d.loop.alive]);
      D = d;
      if (sig !== last) { last = sig; Orchestra.machineCards.changed(); }
    } catch (e) {}
  }
  refresh(); setInterval(refresh, 4000);
})();
