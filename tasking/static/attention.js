// Blocked tickets, on every page of Orchestra's web UI (a plugin script: Orchestra loads it and
// knows nothing of it): a banner under the navigation that says which tickets wait for a decision,
// "(n)" in the tab's title, and the ticket a click away. A blocked ticket is answered by whoever
// plans the work; a package that shows these tickets in its own way calls
// window.TaskAttention.standAside() and this banner stays hidden.
(function () {
  const API = "/api/plugins/tasks";
  const POLL_MS = 5000;
  window.__tasksJs = window.__tasksJs || new Promise((ok) => { const s = document.createElement("script"); s.src = "/plugins/tasks/static/tasks.js"; s.onload = ok; s.onerror = ok; document.head.appendChild(s); });

  const css = document.createElement("style");
  css.textContent = `
    .att-bar { display:none; align-items:center; gap:12px; padding:8px 16px; background:linear-gradient(90deg, rgba(245,158,11,.16), rgba(245,158,11,.05));
      border-bottom:1px solid rgba(245,158,11,.45); color:var(--text, #e6e8ee); font-size:13px }
    .att-bar.on { display:flex }
    .att-bar b { color:#fbbf24 }
    .att-bar .att-what { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    .att-bar a { color:#fbbf24; cursor:pointer; font-family:var(--mono) }
    .att-bar .att-x { flex:none; background:transparent; border:none; color:var(--muted, #8a90a3); cursor:pointer; padding:4px 6px }
    @media (max-width:640px) { .att-bar .att-what { white-space:normal } }`;
  document.head.appendChild(css);

  let A = { blocked: [], count: 0 }, dismissed = "", aside = false;
  const baseTitle = document.title;
  const sig = () => JSON.stringify(A.blocked.map((b) => b.id));
  const e_ = (s) => (typeof esc === "function" ? esc(s) : String(s ?? ""));
  const nav = document.getElementById("nav");
  const bar = document.createElement("div");
  bar.className = "att-bar";
  if (nav) nav.insertAdjacentElement("afterend", bar);

  function render() {
    document.title = (A.count && !aside ? `(${A.count}) ` : "") + baseTitle;
    const show = !aside && A.blocked.length && sig() !== dismissed;
    bar.classList.toggle("on", !!show);
    if (show) bar.innerHTML = `<span>⚠</span><span class="att-what"><b>${A.blocked.length} ticket${A.blocked.length === 1 ? " is" : "s are"} blocked</b> waiting for a decision: `
      + A.blocked.slice(0, 5).map((b) => `<a title="${e_(b.reason)}" onclick="window.TaskAttention.ticket('${e_(b.id)}')">${e_(b.id)}</a>`).join(", ")
      + `${A.blocked.length > 5 ? ", …" : ""}</span><button class="att-x" title="hide until something changes" onclick="window.TaskAttention.dismiss()">✕</button>`;
  }

  window.TaskAttention = {
    ticket: (id) => window.__tasksJs.then(() => openTicket(id)),
    dismiss: () => { dismissed = sig(); render(); },
    standAside: () => { aside = true; render(); },
  };
  async function refresh() {
    try { const d = await (await fetch(API + "/attention")).json(); if (d.ok) { A = d; render(); } }
    catch (e) { /* the task system is down: keep the last state */ }
  }
  refresh();
  setInterval(refresh, POLL_MS);
})();
