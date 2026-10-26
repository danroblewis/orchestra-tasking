// The task system's shared page code: ticket, transcript and gate-run drawers, the Supervisor chat,
// session phases. Loaded after Orchestra's /static/app.js (its $, esc, md, openDrawer, highlightLog).
// Lifted from Orchestra's app.js as it was before the task system was removed (5006caf^).
const API = "/api/plugins/tasks";
const kindColor = (k) => ({ supervisor: "#bc8cff", coordinator: "#58a6ff", pipeline_manager: "#f0883e", worker: "#3fb950", reviewer: "#f85149" }[k] || "#8a90a3");

// opening any drawer ends the paging of a transcript that was open in it
const orchestraOpenDrawer = openDrawer;
openDrawer = function (html) { transcriptPaging = null; orchestraOpenDrawer(html); };   // eslint-disable-line no-func-assign
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
  const t = await (await fetch(`${API}/tickets/${id}`)).json();
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
    <div>sessions</div><div>${t.worker_session ? `<a href="#" onclick="openTranscript('${t.worker_session}');return false">${t.worker_session}</a>` : ""} ${t.reviewer_session ? `<a href="#" onclick="openTranscript('${t.reviewer_session}');return false">${t.reviewer_session}</a>` : ""}</div>
    ${live && live.state === "OPEN" && live.ready ? `<div>waiting</div><div>${esc(live.why_waiting)}</div>` : ""}</div>
    ${t.objective ? `<h2>Objective</h2>${md(t.objective)}` : ""}
    <h2>Description</h2>${md(t.description) || '<div class="empty">none</div>'}<h2>Acceptance</h2>${md(t.acceptance) || '<div class="empty">none</div>'}
    ${t.verification ? `<h2>Verification</h2>${md(t.verification)}` : ""}
    ${t.state !== "REJECTED" && t.feedback ? `<h2>${t.rejected_by ? "Last feedback from the " + esc(t.rejected_by) : "Note for the worker"}</h2>${md(t.feedback)}` : ""}
    <h2>Notes</h2>${t.notes.slice().reverse().map((n) => `<div class="note"><b>${esc(n.author)} · ${esc(n.kind)} · ${esc(n.created_at)}</b>${md(n.text)}</div>`).join("") || '<div class="empty">none</div>'}`);
}

let transcriptPaging = null;   // {id, nextBefore, hasMore, loading}

async function openTranscript(id) {
  const r = await fetch(`${API}/sessions/${id}/transcript?limit=30`);
  if (!r.ok) {
    const perTicket = /^(worker|reviewer)-/.test(id);
    return openDrawer(`<h3>${esc(id)}</h3><div class="empty">${perTicket
      ? "No record of this session: it has not run yet, or it finished before transcripts were kept (" + esc(id) + ")."
      : "This session has not been started. It comes up with <code>tasking run</code>."}</div>`);
  }
  const d = await r.json(); const s = d.session;
  const perTicket = /^(worker|reviewer)-/.test(id);
  let html = `<div id="transcript-head">${transcriptHead(id, s, d)}</div>`;
  if (id === "supervisor") html += `<form id="chat" class="chat top" onsubmit="return sendToSupervisor(event)">
      <textarea id="chat-input" rows="3" placeholder="Message the Supervisor… (Enter to send, Shift+Enter for a new line)" ${s.alive ? "" : "disabled"}></textarea>
      <button type="submit" ${s.alive ? "" : "disabled"}>Send</button>
      ${s.alive ? "" : '<div class="empty">The Supervisor is reachable while <code>tasking run</code> is up.</div>'}
    </form><div id="live"></div>`;
  if (d.started_with) html += `<details class="started"><summary>The prompt this session was started with <span class="muted">(${d.started_with.length.toLocaleString()} chars; roles, team responsibilities, access)</span></summary><div class="prompt md">${md(d.started_with)}</div></details>`;
  else if (!perTicket) html += `<div class="empty">The prompt this session was started with was not recorded (started before Orchestra kept them). The agent's current prompt is on the Agents page.</div>`;
  html += `<h2>Transcript · newest first</h2><div id="transcript">`;
  // Only markers at least as new as the oldest message on this page are placed; older ones belong to older pages.
  const oldest = d.messages.length ? Math.min(...d.messages.map(tsOf)) : 0;
  const first = withMarkers(d.messages, d.markers, d.has_more ? oldest - 1 : 0);
  html += first.length ? first.map(renderMessage).join("") : `<div class="empty">${s.file_exists ? "No messages yet." : "No turns yet: Pi creates the session file on the first prompt. This agent is woken by events or, for the supervisor, by a message."}</div>`;
  html += `</div><div id="transcript-more" class="empty" style="margin-top:8px">${d.has_more ? "scroll for older messages…" : "beginning of session"}</div>`;
  openDrawer(html);
  transcriptPaging = { id, nextBefore: d.next_before, hasMore: d.has_more, loading: false, total: d.total,
                       markerTs: Math.max(0, ...(d.markers || []).map(tsOf)) };
  const ta = $("chat-input");
  if (ta) { ta.focus(); ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat").requestSubmit(); } }); }
}

function transcriptHead(id, s, d) {
  const state = s.status === "running" ? "working" : s.status === "archived" ? "finished (ticket closed)" : s.alive ? "resident, waiting for work" : "not running";
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "—");
  const perTicket = /^(worker|reviewer)-/.test(id);
  const stopBtn = perTicket && s.status === "running" && s.alive && s.ticket_id
    ? `<button class="ctl kill" style="margin-left:auto" onclick="stopAgent('${esc(s.ticket_id)}','${esc(id)}')">■ stop this agent</button>` : "";
  const agentLink = s.kind && !perTicket ? `<a href="/agents#agent=${encodeURIComponent(s.kind)}" class="muted" style="margin-left:auto;font-size:11px" title="The agent's definition, schedule, and every run it has made">↩ ${esc(s.kind)}: definition and all runs</a>` : "";
  return `<h3>${esc(id)} <span class="muted">${state}</span>${agentLink}${stopBtn}</h3>
    <div class="kv"><div>kind</div><div>${esc(s.kind || "")}</div><div>process</div><div>${s.alive ? "pid " + s.pid : "none"}</div>
    ${s.ticket_id ? `<div>ticket</div><div><a href="#" onclick="openTicket('${esc(s.ticket_id)}');return false">${esc(s.ticket_id)}</a></div>` : ""}
    <div>last started</div><div>${when(s.started_at)}${s.status === "running" ? ` <span class="muted">(turn running ${ago((Date.now() / 1000) - s.started_at)})</span>` : ""}</div>
    <div>doing now</div><div>${sessionChip(s, Date.now() / 1000)}</div>
    <div>last output</div><div>${s.last_output_at ? ago((Date.now() / 1000) - s.last_output_at) + " ago" : "—"}</div>
    <div>last settled</div><div>${when(s.settled_at)}</div>
    <div>turns</div><div>${d.turns} · ${d.total} messages <span class="muted">· live, refreshes every ${TRANSCRIPT_POLL_MS / 1000}s</span></div><div>session file</div><div class="muted" style="word-break:break-all">${esc(s.session_file || "not created yet")}</div></div>
    ${s.status === "running" && (s.partial_text || s.thinking_chars) ? `<div class="msg assistant inprogress"><div class="r">reply in progress <span class="muted">· not in the transcript until the message completes${s.thinking_chars ? ` · ${Number(s.thinking_chars).toLocaleString()} chars of thinking so far` : ""}</span></div>${s.partial_text ? md(s.partial_text) : '<span class="muted">(thinking; no reply text yet)</span>'}</div>` : ""}`;
}

const TRANSCRIPT_POLL_MS = 5000;

// The open transcript polls for what arrived since it was rendered and prepends it
// (newest first), keeping the reader's place and the older pages already loaded.
async function refreshTranscript() {
  const p = transcriptPaging;
  if (!p || p.loading || !$("overlay").classList.contains("open") || !$("transcript")) return;
  p.loading = true;
  try {
    const r = await fetch(`${API}/sessions/${p.id}/transcript?after=${p.total}`);
    if (!r.ok) return;
    const d = await r.json();
    if (d.total < p.total) { transcriptPaging = null; return openTranscript(p.id); }  // file replaced: start over
    const head = $("transcript-head"); if (head) head.innerHTML = transcriptHead(p.id, d.session, d);
    const fresh = withMarkers(d.messages, d.markers, p.markerTs);
    p.markerTs = Math.max(p.markerTs || 0, ...(d.markers || []).map(tsOf));
    if (!fresh.length) return;
    const box = $("transcript"), drawer = $("drawer");
    const empty = box.querySelector(".empty"); if (empty) empty.remove();
    const before = drawer.scrollHeight;
    box.insertAdjacentHTML("afterbegin", fresh.map(renderMessage).join(""));
    highlightCode(box);
    if (drawer.scrollTop > 0) drawer.scrollTop += drawer.scrollHeight - before;  // reading older messages: do not yank
    p.nextBefore += d.messages.length; p.total = d.total;
  } catch (e) { /* next tick tries again */ }
  finally { p.loading = false; }
}

async function loadOlderTranscript() {
  const p = transcriptPaging; if (!p || !p.hasMore || p.loading) return;
  p.loading = true;
  try {
    const d = await (await fetch(`${API}/sessions/${p.id}/transcript?before=${p.nextBefore}&limit=30`)).json();
    const box = $("transcript"); if (!box) return;
    box.insertAdjacentHTML("beforeend", d.messages.map(renderMessage).join(""));
    highlightCode(box);
    p.nextBefore = d.next_before; p.hasMore = d.has_more;
    $("transcript-more").textContent = p.hasMore ? "scroll for older messages…" : "beginning of session";
  } finally { p.loading = false; }
}
document.addEventListener("DOMContentLoaded", () => {
  $("drawer").addEventListener("scroll", () => { const el = $("drawer"); if (transcriptPaging && el.scrollTop + el.clientHeight > el.scrollHeight - 300) loadOlderTranscript(); });
  setInterval(refreshTranscript, TRANSCRIPT_POLL_MS);
});

async function sendToSupervisor(ev) {
  ev.preventDefault();
  const ta = $("chat-input"), btn = ev.target.querySelector("button"), live = $("live");
  const message = ta.value.trim();
  if (!message) return false;
  ta.value = ""; ta.disabled = true; btn.disabled = true;
  live.insertAdjacentHTML("afterbegin", `<div class="msg assistant"><div class="r">supervisor · working…</div><div id="live-body"></div></div><div class="msg user"><div class="r">you</div>${md(message)}</div>`);
  highlightCode(live); $("drawer").scrollTop = 0;
  try {
    const resp = await fetch(API + "/supervisor", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }) });
    if (!resp.ok) {
      const body = await resp.text();
      const proxy = /^\s*<!doctype html|<html/i.test(body);
      $("live-body").innerHTML = `<div class="bad">${proxy ? `The connection was closed by the proxy in front of this page (HTTP ${resp.status}). The Supervisor is still working on your message; its reply will appear in the transcript below.` : esc(body.slice(0, 300))}</div>`;
      return false;
    }
    const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl; while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1); if (!line.trim()) continue;
        const e = JSON.parse(line); const body = $("live-body");
        if (e.type === "tool_start") body.innerHTML += `<div class="tc"><div class="r">⚙ ${esc(e.name || "tool")}</div></div>`;
        else if (e.type === "assistant_end") {
          const txt = e.text || "";
          if (txt.trim()) { body.innerHTML += md(txt); highlightCode(body); }
        } else if (e.type === "done") body.previousElementSibling.textContent = e.error ? `supervisor · ${e.error}` : e.timed_out ? "supervisor · timed out" : "supervisor";
      }
    }
  } catch (err) { $("live-body").innerHTML += `<div class="bad">${esc(String(err))}</div>`; return false; }
  finally { ta.disabled = false; btn.disabled = false; ta.focus(); }
  await refreshTranscript();   // the exchange is in the session file now; show it once, there
  live.innerHTML = "";
  return false;
}

const SHELL_TOOLS = new Set(["bash", "shell", "sh", "powershell"]);

function renderToolCall(t) {
  const args = t.args && typeof t.args === "object" ? t.args : {};
  if (SHELL_TOOLS.has(t.name) && typeof args.command === "string")
    return `<div class="tc"><div class="r">⚙ ${esc(t.name)}</div><pre><code class="language-bash">${esc(args.command)}</code></pre></div>`;
  const shown = Object.keys(args).length ? JSON.stringify(args, null, 1) : (typeof t.args === "string" ? t.args : "");
  return `<div class="tc"><div class="r">⚙ ${esc(t.name)}</div>${shown ? `<pre><code class="language-json">${esc(shown).slice(0, 3000)}</code></pre>` : ""}</div>`;
}

function msgTime(m) {
  if (!m.ts) return "";
  // Pi records entry timestamps as ISO strings; some message bodies carry epoch millis.
  const t = typeof m.ts === "number" ? (m.ts > 1e12 ? m.ts : m.ts * 1000) : Date.parse(m.ts);
  if (!isFinite(t)) return "";
  return `<span class="muted num" style="margin-left:8px">${new Date(t).toLocaleTimeString()} · ${ago((Date.now() - t) / 1000)} ago</span>`;
}
const tsOf = (m) => { const t = typeof m.ts === "number" ? (m.ts > 1e12 ? m.ts : m.ts * 1000) : Date.parse(m.ts || ""); return isFinite(t) ? t : 0; };
function fmtDur(s) { return s == null ? "" : s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`; }
function renderMarker(e) {
  let text;
  if (e.type === "turn_timed_out" && e.stopped_by === "silence") text = `⏱ Orchestra stopped this turn: nothing came from the agent for ${fmtDur(e.limit)} (silence_timeout) while it was ${esc(e.doing || "working")}` +
    (e.phase_for != null ? `, ${fmtDur(e.phase_for)} into that` : "") + ". Orchestra took that as stuck. If the model was only slow, raise silence_timeout in orchestra.toml.";
  else if (e.type === "turn_timed_out") text = `⏱ Orchestra stopped this turn: the ${fmtDur(e.limit)} hard turn limit (turn_timeout in orchestra.toml) ran out while the agent was ${esc(e.doing || "working")}` +
    (e.phase_for != null ? ` (${fmtDur(e.phase_for)} into that)` : "") + (e.silent_for != null ? `, last output ${fmtDur(e.silent_for)} earlier` : "") + ". Nothing after this point was the agent's decision.";
  else if (e.type === "turn_error") text = `✖ Orchestra ended this turn: ${esc(e.error || "harness error")}`;
  else if (e.type === "turn_interrupted") text = `■ Orchestra was restarted or stopped during this turn, which cut it off. ${esc(e.reason || "")}`;
  else if (e.type === "agent_crashed") text = `✖ The agent's process exited on its own between turns (crash ${e.count || 1}); Orchestra is restarting it.${e.stderr ? ` Last stderr: <code>${esc(e.stderr.trim().split("\n").slice(-1)[0].slice(0, 160))}</code>` : ""}`;
  else if (e.type === "agent_crash_loop") text = `✖ Crash loop: ${e.count} exits in fifteen minutes. The agent is paused and the Supervisor has been asked to look. Resume it from the Agents page once fixed.`;
  else if (e.type === "agent_compacted" && e.mode === "auto") text = `⇣ Context is bounded by the harness itself (${esc(e.reason || "")}); Orchestra leaves this session alone.`;
  else if (e.type === "agent_compacted") text = `⇣ Context compacted by the harness${e.tokens_before ? `: ${Number(e.tokens_before).toLocaleString()} → ${Number(e.tokens_after || 0).toLocaleString()} tokens` : ""} (${esc(e.reason || "")}). Older messages became a summary the agent keeps; the transcript is unchanged.`;
  else if (e.type === "agent_reset") text = `⟲ Session started over by ${esc(e.by || "the operator")}${e.reason ? ` (${esc(e.reason)})` : ""}. The conversation before this point is archived as run <code>${esc(e.archived || "")}</code>; the agent keeps the project context and its team's space, not its memory of the chat.`;
  else if (e.type === "agent_killed") text = "■ Stopped by the operator.";
  else if (e.type === "session_started") text = `▶ Turn started${e.ticket ? " on " + esc(e.ticket) : ""}.`;
  else text = esc(e.type);
  return `<div class="msg marker ${e.type}"><div class="r">orchestra${msgTime({ ts: e.ts })}</div>${text}</div>`;
}
// Markers are Orchestra's events for the session; they are merged with the messages by time, newest first.
function withMarkers(messages, markers, newerThan) {
  const ms = (markers || []).filter((e) => tsOf(e) > (newerThan || 0)).map((e) => ({ ...e, _marker: true }));
  return [...messages, ...ms].sort((a, b) => tsOf(b) - tsOf(a));
}
function renderMessage(m) {
  if (m._marker) return renderMarker(m);
  if (m.role === "toolResult") {
    const shell = SHELL_TOOLS.has(m.tool_name);
    const body = shell ? `<pre class="term">${highlightLog(m.text)}</pre>` : `<pre><code>${esc(m.text)}</code></pre>`;
    return `<div class="msg toolResult ${m.is_error ? "err" : ""}"><div class="r">result · ${esc(m.tool_name || "tool")}${m.is_error ? ' <span class="bad">error</span>' : ""}${msgTime(m)}</div>${body}</div>`;
  }
  return `<div class="msg ${m.role}"><div class="r">${m.role}${msgTime(m)}</div>${md(m.text.slice(0, 12000))}${m.tool_calls.map(renderToolCall).join("")}</div>`;
}

async function openGateRun(id) {
  const r = await (await fetch(`${API}/gate/runs/${id}`)).json();
  openDrawer(`<h3>Gate run ${r.id} <span class="${r.status === "landed" ? "ok" : "bad"}">${r.status}</span></h3>
    <div class="kv"><div>batch</div><div>${r.batch.join(", ")}</div><div>merged</div><div>${(r.merged || []).join(", ") || "—"}</div><div>rejected</div><div>${(r.rejected || []).join(", ") || "—"}</div><div>summary</div><div>${esc(r.summary)}</div></div>
    ${(r.stages || []).map((s) => `<h2>${s.stage}:${s.name} <span class="${s.status === "passed" ? "ok" : "bad"}">${s.status}</span></h2><pre>${highlightLog(s.tail || "")}</pre>`).join("")}`);
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });


function openFromHash() {
  const m = location.hash.match(/^#(session|ticket|gate)=([^&]+)/);
  if (!m) return;
  ({ session: openTranscript, ticket: openTicket, gate: openGateRun })[m[1]](decodeURIComponent(m[2]));
}
window.addEventListener("hashchange", openFromHash);

async function stopAgent(ticket, sessionId) {
  if (!confirm(`Stop ${sessionId}? Its ticket ${ticket} goes back to OPEN (or back to review); the branch and session are kept.`)) return;
  const r = await fetch(API + "/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "kill", ticket }) });
  if (!r.ok) alert(await r.text()); else closeDrawer();
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
function sessionPhase(s, now) {
  if (!s) return { cls: "down", text: "down", title: "" };
  if (s.status === "archived") return { cls: "archived", text: "finished", title: "" };
  const alive = !!s.alive;
  if (s.status !== "running") return alive ? { cls: "resident", text: "resident", title: "waiting for work" } : { cls: "down", text: "down", title: "" };
  const age = (t) => ago(now - (t || now));
  const turn = `turn started ${age(s.started_at)} ago`;
  if (s.phase === "tool") return { cls: "working", text: `running ${s.tool_name || "tool"} · ${age(s.phase_since)}`, title: turn };
  if (s.phase === "generating") {
    const sinceTok = s.last_token_at ? now - s.last_token_at : 0;
    return { cls: sinceTok > 60 ? "waiting" : "working", text: `receiving tokens · last token ${ago(sinceTok)} ago`, title: turn };
  }
  // The session is mid-turn and its stream has been silent since phase_since. We report
  // how long; we do not know why.
  const wait = now - (s.phase_since || s.started_at || now);
  return { cls: wait > 60 ? "waiting" : "working", text: `no new message for ${ago(wait)}`, title: turn + " · session has not exited and nothing has streamed" };
}
function sessionChip(s, now) {
  const p = sessionPhase(s, now);
  return `<span class="chip ${p.cls}" title="${esc(p.title)}"><span class="dot"></span>${esc(p.text)}</span>`;
}

// ---- machines: AI agents and programs from the factory model (dashboard and Machines page) ----
const MACHINE_COLOR = { supervisor: "var(--r-supervisor)", coordinator: "var(--r-coordinator)", "pipeline-manager": "var(--r-pipeline)",
  worker: "var(--r-worker)", reviewer: "var(--r-reviewer)" };
const MACHINE_STATUS_CHIP = { running: "resident", working: "working", waiting: "resident", idle: "", stopped: "down", "not running": "down", observed: "" };
const MACHINE_STOPPED = ["stopped", "not running"];
const stockName = (s) => (s || "").toLowerCase().replace(/_/g, " ");
function machineFlowText(f, detail) {
  const [a, b] = f.id.split(">");
  const arrow = `<span class="fl">${esc(stockName(a))} → ${esc(stockName(b))}</span>`;
  if (f.rate == null) return `${arrow}<span class="muted">no data</span>`;
  const rate = f.form === "proportional" ? `${f.rate.toFixed(2)} × ${esc(stockName(f.src))}` : f.rate.toFixed(2);
  const more = detail ? ` <span class="muted">± ${(f.se || 0).toFixed(3)} · ${f.events} events · disp ${(f.dispersion ?? 0).toFixed(1)} · ${esc(f.form)}, ${esc(f.origin)}</span>` : "";
  return `${arrow}<span class="num" title="${esc(f.form)} · ${esc(f.origin)} from ${f.events} events · ± ${(f.se || 0).toFixed(3)}">${rate} /h${more}</span>`;
}
function machineCard(m, { detail = false, controls = "" } = {}) {
  const agent = m.kind === "agent", rc = MACHINE_COLOR[m.id] || (agent ? "#c4b5fd" : "#94a3b8");
  const eng = m.engagement.map((e) => `${e.mode.replace("_", " ")} · ${e.driver}`).join(", ") || "observed only";
  const jobs = m.jobs.map((j) => `<div class="job" onclick="event.stopPropagation();openTranscript('${esc(j.session)}')"><span class="id">${esc(j.ticket || j.session)}</span><span class="msg">${esc(j.text || "working")}</span></div>`).join("");
  const desc = detail && m.description ? `<div class="mdesc">${esc(m.description)}</div>` : "";
  const flows = m.flows.length ? `<div class="mflows">${detail ? '<div class="mfh">attributed flows: hypotheses, with the equations fitted from the history</div>' : ""}${m.flows.map((f) => `<div>${machineFlowText(f, detail)}</div>`).join("")}</div>`
    : detail ? '<div class="mflows"><div class="muted">No flows attributed yet.</div></div>' : "";
  return `<div class="machine ${detail ? "detail" : ""}" style="--rc:${rc}" title="${esc(m.description)}">
    <div class="mhead"><div class="av ${agent ? "" : "prog"}">${agent ? esc(m.name[0]) : "⚙"}</div>
      <div class="mname">${esc(m.name)}${m.bootstrap ? ' <span class="tag">bootstrap</span>' : ""}<div class="msub">${agent ? "AI agent" : "program"} · ${esc(eng)}${detail ? ` · <span class="num">${esc(m.id)}</span>` : ""}</div></div>
      <span class="chip ${MACHINE_STATUS_CHIP[m.status] ?? ""}"><span class="dot"></span>${esc(m.status)}</span></div>
    ${desc}${jobs}${flows}${controls}</div>`;
}
