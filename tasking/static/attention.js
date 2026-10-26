// What waits on the human, on every page of Orchestra's web UI (a plugin script: Orchestra loads it
// and knows nothing of it). Blocked tickets and Supervisor messages not yet read become:
// - a "Supervisor" button in the navigation, with a count when something waits;
// - a banner under the navigation that says what waits, with a way to answer;
// - "(n)" in the tab's title.
// The button opens the Supervisor's chat (tasks.js) with the list of what waits above it. The
// Supervisor is the human's one way in: blocked work is answered by telling it what to do.
(function () {
  const API = "/api/plugins/tasks";
  const POLL_MS = 5000;
  const load = (src) => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = bad; document.head.appendChild(s); });

  // what the chat drawer needs: tasks.js (transcripts, the chat), and markdown rendering
  async function ready() {
    if (!window.marked) await load("https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js").catch(() => {});
    if (!window.DOMPurify) await load("https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js").catch(() => {});
    if (typeof window.openTranscript !== "function") await load("/plugins/tasks/static/tasks.js");
  }

  const css = document.createElement("style");
  css.textContent = `
    .att-btn { display:inline-flex; align-items:center; gap:6px; margin-left:14px; padding:3px 10px; border-radius:999px;
      border:1px solid var(--border2, #2a3142); background:transparent; color:var(--text, #e6e8ee); font:inherit; font-size:12px; cursor:pointer }
    .att-btn:hover { border-color:#6ea8fe }
    .att-btn .att-n { min-width:18px; height:18px; padding:0 5px; border-radius:9px; background:#f59e0b; color:#1a1205;
      font-weight:700; font-size:11px; display:none; align-items:center; justify-content:center }
    .att-btn.on { border-color:#f59e0b; box-shadow:0 0 0 0 rgba(245,158,11,.6); animation:att-pulse 2s infinite }
    .att-btn.on .att-n { display:inline-flex }
    .att-btn .att-dot { width:7px; height:7px; border-radius:50%; background:#6ea8fe; display:none }
    .att-btn.working .att-dot { display:inline-block; animation:att-blink 1s infinite alternate }
    @keyframes att-pulse { 0% { box-shadow:0 0 0 0 rgba(245,158,11,.55) } 70% { box-shadow:0 0 0 8px rgba(245,158,11,0) } 100% { box-shadow:0 0 0 0 rgba(245,158,11,0) } }
    @keyframes att-blink { from { opacity:.25 } to { opacity:1 } }
    .att-bar { display:none; align-items:center; gap:12px; padding:8px 16px; background:linear-gradient(90deg, rgba(245,158,11,.16), rgba(245,158,11,.05));
      border-bottom:1px solid rgba(245,158,11,.45); color:var(--text, #e6e8ee); font-size:13px }
    .att-bar.on { display:flex }
    .att-bar b { color:#fbbf24 }
    .att-bar .att-what { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    .att-bar button { flex:none; padding:4px 12px; border-radius:6px; border:1px solid #f59e0b; background:#f59e0b; color:#1a1205; font-weight:600; cursor:pointer }
    .att-bar .att-x { background:transparent; border-color:transparent; color:var(--muted, #8a90a3); font-weight:400; padding:4px 6px }
    .att-panel { border:1px solid rgba(245,158,11,.5); background:rgba(245,158,11,.07); border-radius:8px; padding:12px 14px; margin:4px 0 14px }
    .att-panel h4 { margin:0 0 8px; color:#fbbf24; font-size:12px; letter-spacing:.06em; text-transform:uppercase }
    .att-item { display:grid; grid-template-columns:1fr auto; gap:4px 12px; padding:8px 0; border-top:1px solid rgba(245,158,11,.2) }
    .att-item:first-of-type { border-top:none }
    .att-item .att-t { font-weight:600 } .att-item .att-t code { color:#fbbf24 }
    .att-item .att-r { grid-column:1; color:var(--muted, #8a90a3); font-size:12px; line-height:1.45; max-height:5.8em; overflow:hidden }
    .att-item .att-acts { grid-column:2; grid-row:1 / span 2; display:flex; flex-direction:column; gap:6px; align-items:stretch }
    .att-item button { padding:3px 10px; border-radius:6px; border:1px solid var(--border2, #2a3142); background:var(--panel2, #151a24); color:var(--text, #e6e8ee); cursor:pointer; font-size:12px }
    .att-item button.primary { border-color:#f59e0b; color:#fbbf24 }
    @media (max-width:640px) { .att-bar .att-what { white-space:normal } .att-item { grid-template-columns:1fr } .att-item .att-acts { grid-column:1; grid-row:auto; flex-direction:row } }`;
  document.head.appendChild(css);

  let A = { blocked: [], message: null, count: 0, working: false };
  let dismissed = "";                       // the banner's content when the viewer closed it
  const baseTitle = document.title;
  const sig = () => JSON.stringify([A.blocked.map((b) => b.id), A.message && A.message.at]);
  const ago_ = (t) => (typeof ago === "function" && t ? ago(Date.now() / 1000 - t) : "");
  const since = (iso) => { const t = Date.parse(iso || ""); return isFinite(t) ? ago_(t / 1000) : ""; };
  const e_ = (s) => (typeof esc === "function" ? esc(s) : String(s ?? ""));

  // the navigation button
  const btn = document.createElement("button");
  btn.className = "att-btn"; btn.type = "button";
  btn.innerHTML = '<span>💬 Supervisor</span><span class="att-dot" title="the Supervisor is working"></span><span class="att-n"></span>';
  btn.onclick = () => openSupervisor();
  const nav = document.getElementById("nav");
  if (nav) nav.insertBefore(btn, document.getElementById("proj"));

  // the banner, right under the navigation
  const bar = document.createElement("div");
  bar.className = "att-bar";
  if (nav) nav.insertAdjacentElement("afterend", bar);

  function render() {
    btn.classList.toggle("on", A.count > 0);
    btn.classList.toggle("working", !!A.working);
    btn.querySelector(".att-n").textContent = A.count || "";
    btn.title = A.count ? `${A.count} thing${A.count === 1 ? "" : "s"} waiting on you` : "Talk to the Supervisor";
    document.title = (A.count ? `(${A.count}) ` : "") + baseTitle;
    const parts = [];
    if (A.blocked.length) parts.push(`<b>${A.blocked.length} ticket${A.blocked.length === 1 ? " is" : "s are"} blocked</b> waiting for a decision (${A.blocked.slice(0, 3).map((b) => e_(b.id)).join(", ")}${A.blocked.length > 3 ? ", …" : ""})`);
    if (A.message) parts.push(`<b>the Supervisor wrote to you</b> ${ago_(A.message.at)} ago`);
    const show = parts.length && sig() !== dismissed;
    bar.classList.toggle("on", !!show);
    if (show) bar.innerHTML = `<span>⚠</span><span class="att-what">${parts.join(" · ")}</span>
      <button onclick="window.TaskAttention.open()">Review &amp; answer</button><button class="att-x" title="hide until something changes" onclick="window.TaskAttention.dismiss()">✕</button>`;
    const panel = document.getElementById("att-panel");
    if (panel) panel.outerHTML = panelHtml();
  }

  function panelHtml() {
    if (!A.count) return '<div id="att-panel"></div>';
    const items = A.blocked.map((b) => `<div class="att-item">
        <div class="att-t"><code>${e_(b.id)}</code> ${e_(b.title)} <span class="muted" style="font-weight:400">· blocked ${since(b.since)}</span></div>
        <div class="att-r">${e_(b.reason) || "(no reason recorded)"}</div>
        <div class="att-acts"><button class="primary" onclick="window.TaskAttention.answer('${e_(b.id)}')">Answer</button>
          <button onclick="openTicket('${e_(b.id)}')">Ticket</button></div></div>`);
    if (A.message) items.unshift(`<div class="att-item">
        <div class="att-t">The Supervisor wrote to you <span class="muted" style="font-weight:400">· ${ago_(A.message.at)} ago</span></div>
        <div class="att-r">${e_(A.message.text.slice(0, 400))}${A.message.text.length > 400 ? "…" : ""}</div>
        <div class="att-acts"><button class="primary" onclick="document.getElementById('chat-input')?.focus()">Reply</button></div></div>`);
    return `<div id="att-panel" class="att-panel"><h4>Waiting on you</h4>${items.join("")}
      <div class="muted" style="font-size:11px;margin-top:6px">Answer by telling the Supervisor what to do; it unblocks, changes or cancels the ticket.</div></div>`;
  }

  async function refresh() {
    try {
      const d = await (await fetch(API + "/attention")).json();
      if (d.ok) { A = d; render(); }
    } catch (e) { /* the task system is down: keep the last state */ }
  }

  async function markSeen() {
    try { await fetch(API + "/attention/seen", { method: "POST" }); } catch (e) {}
  }

  // the Supervisor's chat, with what waits listed above the message box
  async function openSupervisor(prefill) {
    await ready();
    await openTranscript("supervisor");
    const head = document.getElementById("transcript-head");
    if (head && !document.getElementById("att-panel")) head.insertAdjacentHTML("afterend", panelHtml());
    if (prefill) {
      const ta = document.getElementById("chat-input");
      if (ta && !ta.disabled) { ta.value = prefill; ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    }
    // reading the chat reads the message; blocked tickets stay until they are unblocked
    if (A.message) { await markSeen(); A.message = null; A.count = A.blocked.length; render(); }
  }

  // after the human writes, the Supervisor's reply arrives in the open chat: it is read
  const wrapSend = () => {
    if (typeof window.sendToSupervisor !== "function" || window.sendToSupervisor.__att) return;
    const orig = window.sendToSupervisor;
    window.sendToSupervisor = async function (ev) { const r = await orig(ev); await markSeen(); refresh(); return r; };
    window.sendToSupervisor.__att = true;
  };

  window.TaskAttention = {
    open: () => openSupervisor(),
    dismiss: () => { dismissed = sig(); render(); },
    answer: (id) => {
      const b = A.blocked.find((x) => x.id === id);
      openSupervisor(`About ${id}${b ? ` ("${b.title}")` : ""}, which is blocked: `);
    },
  };

  refresh();
  setInterval(() => { wrapSend(); refresh(); }, POLL_MS);
  setTimeout(wrapSend, 500);
})();
