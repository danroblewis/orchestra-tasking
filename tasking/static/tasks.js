// The task system's shared page code: the ticket and gate-run drawers, priorities, re-engagement.
// Loaded after Orchestra's /static/app.js. Its names do not clash with the agents' agents.js
// (transcripts, the Supervisor chat): a page may load both, and a ticket's sessions open in the
// agents' transcript drawer when it is there.
const TASKS_API = "/api/plugins/tasks";
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
  if (t.state === "REJECTED" && t.feedback) alerts += `<div class="alert rejected"><h4>Rejected by the ${esc(t.rejected_by || "reviewer")} · round ${t.review_rounds}</h4>${md(t.feedback)}</div>`;
  if (live && live.state === "OPEN" && !live.ready) alerts += `<div class="alert waiting"><h4>Waiting</h4>${esc(live.why_waiting)}</div>`;
  const stateColor = color(t.state);
  openDrawer(`${alerts}<h3><span class="pill" style="--c:${stateColor};background:${stateColor}">${t.state.replace(/_/g, " ")}</span><span style="font-family:var(--mono)">${esc(t.id)}</span> ${esc(t.title)}</h3>
    <div class="kv"><div>milestone</div><div>${esc(t.milestone_id)}</div><div>priority</div><div>${priorityText(t, live)}</div><div>depends on</div><div>${t.depends_on.join(", ") || "—"}</div>
    <div>branch</div><div class="num">${esc(t.branch || "—")}</div><div>reviewed commit</div><div class="num">${esc((t.reviewed_commit || "").slice(0, 10) || "—")}</div><div>worker turns</div><div>${turnsText(t)}</div>
    <div>sessions</div><div>${t.worker_session ? `<a href="#" onclick="(window.openTranscript ? openTranscript('${t.worker_session}') : 0);return false">${t.worker_session}</a>` : ""} ${t.reviewer_session ? `<a href="#" onclick="(window.openTranscript ? openTranscript('${t.reviewer_session}') : 0);return false">${t.reviewer_session}</a>` : ""}</div>
    ${live && live.state === "OPEN" && live.ready ? `<div>waiting</div><div>${esc(live.why_waiting)}</div>` : ""}</div>
    ${t.objective ? `<h2>Objective</h2>${md(t.objective)}` : ""}
    <h2>Description</h2>${md(t.description) || '<div class="empty">none</div>'}<h2>Acceptance</h2>${md(t.acceptance) || '<div class="empty">none</div>'}
    ${t.verification ? `<h2>Verification</h2>${md(t.verification)}` : ""}
    ${t.state !== "REJECTED" && t.feedback ? `<h2>${t.rejected_by ? "Last feedback from the " + esc(t.rejected_by) : "Note for the worker"}</h2>${md(t.feedback)}` : ""}
    <h2>Notes</h2>${t.notes.slice().reverse().map((n) => `<div class="note"><b>${esc(n.author)} · ${esc(n.kind)} · ${esc(n.created_at)}</b>${md(n.text)}</div>`).join("") || '<div class="empty">none</div>'}`);
}


async function openGateRun(id) {
  const r = await (await fetch(`${TASKS_API}/gate/runs/${id}`)).json();
  openDrawer(`<h3>Gate run ${r.id} <span class="${r.status === "landed" ? "ok" : "bad"}">${r.status}</span></h3>
    <div class="kv"><div>batch</div><div>${r.batch.join(", ")}</div><div>merged</div><div>${(r.merged || []).join(", ") || "—"}</div><div>rejected</div><div>${(r.rejected || []).join(", ") || "—"}</div><div>summary</div><div>${esc(r.summary)}</div></div>
    ${(r.stages || []).map((s) => `<h2>${s.stage}:${s.name} <span class="${s.status === "passed" ? "ok" : "bad"}">${s.status}</span></h2><pre>${highlightLog(s.tail || "")}</pre>`).join("")}`);
}


function priorityText(t, live) {
  const pi = live && live.priority_info; if (!pi) return String(t.priority);
  const bits = [];
  if (pi.inherited_from) bits.push(`inherited from ${pi.inherited_from}`);
  if (pi.age_levels) bits.push(`+${pi.age_levels} for age`);
  return pi.effective === pi.own ? `${pi.own}` : `${pi.effective} <span class="muted">(own ${pi.own}; ${bits.join(", ")})</span>`;
}

// One description of what a session is doing right now, from its recorded phase.
// Returns {cls, text, title}. cls: working | waiting | resident | down | archived.

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// #ticket=<id> and #gate=<id> open their drawers (the agents' agents.js handles #session=)
function tasksFromHash() {
  const m = location.hash.match(/^#(ticket|gate)=([^&]+)/);
  if (m) ({ ticket: openTicket, gate: openGateRun })[m[1]](decodeURIComponent(m[2]));
}
window.addEventListener("hashchange", tasksFromHash);
