// The task system's shared page code: the ticket drawer, priorities, re-engagement, the operator's
// controls, and its look (tasks.css). Loaded after Orchestra's /static/app.js (esc, md, color,
// openDrawer are its), by the Tickets page and by tasks-boot.js on every other page.
const TASKS_API = "/api/plugins/tasks";
if (!document.querySelector('link[href^="/plugins/tasks/static/tasks.css"]')) {
  const l = document.createElement("link"); l.rel = "stylesheet"; l.href = "/plugins/tasks/static/tasks.css"; document.head.appendChild(l);
}

function turnsText(t) {
  const n = t.worker_turns || 0;
  if (n < 2) return n === 1 ? "1 (first attempt)" : "none yet";
  const why = [];
  if (t.reviewer_rejections) why.push(`${t.reviewer_rejections} after a reviewer rejection`);
  if (t.gate_rejections) why.push(`${t.gate_rejections} after the merge gate sent it back`);
  const other = n - 1 - (t.reviewer_rejections || 0) - (t.gate_rejections || 0);
  if (other > 0) why.push(`${other} after a crash, timeout, or operator stop`);
  return `${n} · re-engaged ${n - 1}×` + (why.length ? ` (${why.join(", ")})` : "");
}

async function openTicket(id) {
  const t = await (await fetch(`${TASKS_API}/tickets/${id}`)).json();
  const live = (window.STATE && STATE.tickets || []).find((x) => x.id === id);
  // What needs attention comes first, before the title: a blocker, a rejection, or why it waits.
  let alerts = "";
  if (t.state === "BLOCKED") alerts += `<div class="alert blocked"><h4>Blocked · needs a decision</h4>${md(t.blocked_reason || "(no reason recorded)")}</div>`;
  if (t.state === "REJECTED" && t.feedback) alerts += `<div class="alert rejected"><h4>Rejected by ${esc(t.rejected_by || "the reviewer")} · round ${t.review_rounds}</h4>${md(t.feedback)}</div>`;
  if (live && live.state === "OPEN" && !live.ready) alerts += `<div class="alert waiting"><h4>Waiting</h4>${esc(live.why_waiting)}</div>`;
  const stateColor = color(t.state);
  const row = (k, v) => (v ? `<div>${k}</div><div class="num">${esc(v)}</div>` : "");
  openDrawer(`${alerts}<h3><span class="pill" style="--c:${stateColor};background:${stateColor}">${t.state.replace(/_/g, " ")}</span><span style="font-family:var(--mono)">${esc(t.id)}</span> ${esc(t.title)}</h3>
    <div class="kv"><div>milestone</div><div>${esc(t.milestone_id)}</div><div>priority</div><div>${priorityText(t, live)}</div><div>depends on</div><div>${t.depends_on.join(", ") || "—"}</div>
    ${row("held by", t.owner)}${row("branch", t.branch)}${row("reviewed commit", (t.reviewed_commit || "").slice(0, 10))}<div>worker turns</div><div>${turnsText(t)}</div>
    ${live && live.state === "OPEN" && live.ready ? `<div>waiting</div><div>${esc(live.why_waiting)}</div>` : ""}</div>
    ${t.objective ? `<h2>Objective</h2>${md(t.objective)}` : ""}
    <h2>Description</h2>${md(t.description) || '<div class="empty">none</div>'}<h2>Acceptance</h2>${md(t.acceptance) || '<div class="empty">none</div>'}
    ${t.verification ? `<h2>Verification</h2>${md(t.verification)}` : ""}
    ${t.state !== "REJECTED" && t.feedback ? `<h2>${t.rejected_by ? "Last feedback from " + esc(t.rejected_by) : "Note for the worker"}</h2>${md(t.feedback)}` : ""}
    <h2>Notes</h2>${t.notes.slice().reverse().map((n) => `<div class="note"><b>${esc(n.author)} · ${esc(n.kind)} · ${esc(n.created_at)}</b>${md(n.text)}</div>`).join("") || '<div class="empty">none</div>'}`);
}

function priorityText(t, live) {
  const p = live && live.priority_info; if (!p) return String(t.priority);
  const bits = [];
  if (p.inherited_from) bits.push(`inherited from ${p.inherited_from}`);
  if (p.age_levels) bits.push(`+${p.age_levels} for age`);
  return p.effective === p.own ? `${p.own}` : `${p.effective} <span class="muted">(own ${p.own}; ${bits.join(", ")})</span>`;
}

// The operator's controls, drawn by the Tasks panel and the scheduler's card from the same /board
// answer D: the capacity ceilings with one-more/one-fewer buttons, and the pause. ns names the
// object (taskControlActions) the buttons call.
function taskControls(D, ns) {
  const c = (D && D.capacity) || {}, paused = !!(D && D.pickup_paused);
  const cap = (kind) => {
    const step = (d) => `<button class="ctl" style="padding:0 6px;margin:0" onclick="event.stopPropagation();${ns}.capacity('${kind}',${d})" title="${d < 0 ? "one fewer" : "one more"} ${kind.slice(0, -1)}">${d < 0 ? "−" : "+"}</button>`;
    return `<span title="how many tickets may be offered to ${kind} at once">${step(-1)}<b style="margin:0 4px">${c["max_" + kind] ?? "?"}</b>${step(1)} ${kind}</span>`;
  };
  return `${cap("workers")}${cap("reviewers")}${paused ? '<span style="color:#fde68a">new workers PAUSED</span>' : ""}<button class="ctl ${paused ? "paused" : ""}" style="margin:0" onclick="event.stopPropagation();${ns}.pause()" title="${paused ? "Resume offering work to workers." : "Stop offering new work to workers. Work under way finishes; reviews go on."}">${paused ? "▶ resume" : "⏸ pause new workers"}</button>`;
}
function taskControlActions(current, refresh) {
  const post = async (body) => {
    const r = await fetch(TASKS_API + "/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) alert(await r.text());
    refresh();
  };
  return {
    capacity(kind, d) { const c = (current() || {}).capacity || {}; const b = { action: "capacity", workers: c.max_workers, reviewers: c.max_reviewers }; b[kind] = Math.max(0, (c["max_" + kind] || 0) + d); post(b); },
    pause() { post({ action: (current() || {}).pickup_paused ? "resume" : "pause" }); },
  };
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// #ticket=<id> opens the ticket's drawer.
function tasksFromHash() {
  const m = location.hash.match(/^#ticket=([^&]+)/);
  if (m) openTicket(decodeURIComponent(m[1]));
}
window.addEventListener("hashchange", tasksFromHash);
