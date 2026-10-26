// The Tickets page: the graph of milestones and tickets, laid out by dagre and drawn as SVG,
// panned and zoomed by hand. Loaded after tasks.js (TASKS_API, openTicket, tasksFromHash).
let STATE = null, fitted = false;
const view = { x: 20, y: 20, k: 1 };
const DONE = new Set(["DONE", "CANCELLED"]);
const ACTIVE = new Set(["IN_PROGRESS", "IN_REVIEW", "MERGING"]);
const TW = 200, TH = 46, MW = 180, MH = 46;
const opened = new Set(JSON.parse(sessionStorage.getItem("graph.open") || "[]"));

async function refresh() {
  try {
    STATE = await (await fetch(TASKS_API + "/state")).json();
    $("conn").textContent = STATE.scheduler && STATE.scheduler.alive ? "live" : "scheduler not running";
    render();
  }
  catch (e) { if ($("conn")) $("conn").textContent = "disconnected"; }
}

// The model. Milestones are stadium nodes chained in order. A ticket with no drawn predecessor
// hangs off its milestone by a dotted edge; others hang off their dependencies. Done tickets are
// hidden unless "show done tickets" is ticked or their milestone is opened; a dependency chain
// that ran through hidden ones becomes one dashed edge from the nearest live ancestor, "via N done".
function buildModel(s) {
  const q = $("filter").value.trim().toLowerCase();
  const showDone = $("showdone").checked;
  const byId = Object.fromEntries(s.tickets.map((t) => [t.id, t]));
  const deps = (t) => (t.depends_on || []).filter((d) => byId[d]);
  const msOrder = [...s.milestones].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id, undefined, { numeric: true }));
  // Scope: every ticket when done ones are shown; else the live tickets in open milestones plus
  // their prerequisite chains, and the done tickets of opened milestones.
  const scope = new Set();
  const addChain = (id) => { if (scope.has(id)) return; scope.add(id); deps(byId[id]).forEach(addChain); };
  for (const t of s.tickets) if (showDone || !DONE.has(t.state) || opened.has(t.milestone_id)) addChain(t.id);
  if (q) for (const id of [...scope]) { const t = byId[id]; if (![t.id, t.title, t.state, t.milestone_id].join(" ").toLowerCase().includes(q)) scope.delete(id); }
  // The done tickets reached only as prerequisites are collapsed into "via N done" edges, unless their milestone is opened.
  const collapsed = new Set();
  if (!showDone) for (const id of scope) if (DONE.has(byId[id].state) && !opened.has(byId[id].milestone_id)) collapsed.add(id);
  const memo = {};
  const effPreds = (id, stack = new Set()) => {   // {live ancestor: collapsed nodes in between}
    if (memo[id]) return memo[id];
    const out = {};
    for (const dp of deps(byId[id])) {
      if (!scope.has(dp) || stack.has(dp)) continue;
      if (collapsed.has(dp)) { const s2 = new Set(stack); s2.add(id); for (const [anc, via] of Object.entries(effPreds(dp, s2))) if (!(anc in out) || via + 1 < out[anc]) out[anc] = via + 1; }
      else out[dp] = 0;
    }
    return (memo[id] = out);
  };
  const nodes = [], edges = [];
  const msStats = {};
  for (const m of msOrder) {
    const all = s.tickets.filter((t) => t.milestone_id === m.id), done = all.filter((t) => DONE.has(t.state)).length;
    const cls = !all.length ? "msnext" : done === all.length ? "msdone" : "";
    msStats[m.id] = { all: all.length, done };
    nodes.push({ id: `ms:${m.id}`, kind: "ms", m, w: MW, h: MH, cls, open: opened.has(m.id), stats: msStats[m.id] });
  }
  for (let i = 1; i < msOrder.length; i++) edges.push({ from: `ms:${msOrder[i - 1].id}`, to: `ms:${msOrder[i].id}`, cls: "chain" });
  for (const id of scope) {
    if (collapsed.has(id)) continue;
    const t = byId[id];
    nodes.push({ id, kind: "ticket", t, w: TW, h: TH });
    const preds = effPreds(id);
    for (const [dp, via] of Object.entries(preds)) edges.push({ from: dp, to: id, cls: via ? "via" : "dep", label: via ? `via ${via} done` : "" });
    // The milestone acts as a dependency only for tickets that have no drawn predecessor;
    // everything else hangs off its dependencies and needs no extra link.
    if (!Object.keys(preds).length && s.milestones.some((m) => m.id === t.milestone_id)) edges.push({ from: `ms:${t.milestone_id}`, to: id, cls: "member" });
  }
  return { nodes, edges };
}

// The layout: dagre, left to right.
function layout(model) {
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setGraph({ rankdir: "LR", nodesep: 34, ranksep: 70, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  model.nodes.forEach((n) => g.setNode(n.id, { width: n.w, height: n.h }));
  model.edges.forEach((e, i) => g.setEdge(e.from, e.to, { weight: e.cls === "chain" ? 4 : e.cls === "member" ? 1 : 2, minlen: 1 }, String(i)));
  dagre.layout(g);
  model.nodes.forEach((n) => { const p = g.node(n.id); n.x = p.x; n.y = p.y; });
  model.edges.forEach((e, i) => { e.points = g.edge(e.from, e.to, String(i)).points; });
  const gr = g.graph(); return { w: gr.width, h: gr.height };
}

function pathFor(points) {   // smooth curve through dagre's points
  if (!points || points.length < 2) return "";
  if (points.length === 2) return `M${points[0].x},${points[0].y} L${points[1].x},${points[1].y}`;
  let d = `M${points[0].x},${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) { const p = points[i], n = points[i + 1]; const mx = (p.x + n.x) / 2, my = (p.y + n.y) / 2; d += ` Q${p.x},${p.y} ${mx},${my}`; }
  const l = points[points.length - 1]; d += ` L${l.x},${l.y}`; return d;
}

// The drawing.
let bounds = { w: 800, h: 600 }, liveBox = null;
function render() {
  const s = STATE; if (!s) return;
  $("legend").innerHTML = Object.entries(s.counts).filter(([, v]) => v).map(([k, v]) => `<span class="badge" style="background:${color(k)}">${esc(k.replace(/_/g, " "))} ${v}</span>`).join(" ");
  $("chips").innerHTML = [...opened].map((m) => `<span class="ms-chip" onclick="toggleMs('${m}')">▼ ${esc(m)} · open</span>`).join("");
  if (!s.tickets.length) { $("nodes").innerHTML = `<text x="20" y="40" fill="#8a90a3">No tickets yet. A planner creates them (tasking api create_ticket …).</text>`; $("edges").innerHTML = ""; return; }
  const model = buildModel(s);
  bounds = layout(model);
  // The frame worth looking at: everything except done milestones (and the chain behind them).
  const live = model.nodes.filter((n) => n.kind === "ticket" || n.cls !== "msdone");
  const pick = live.length ? live : model.nodes;
  liveBox = { x0: Math.min(...pick.map((n) => n.x - n.w / 2)) - 20, y0: Math.min(...pick.map((n) => n.y - n.h / 2)) - 20,
              x1: Math.max(...pick.map((n) => n.x + n.w / 2)) + 20, y1: Math.max(...pick.map((n) => n.y + n.h / 2)) + 20 };
  const nodeById = Object.fromEntries(model.nodes.map((n) => [n.id, n]));
  $("edges").innerHTML = model.edges.map((e) => {
    const intoActive = e.cls === "dep" && nodeById[e.to] && nodeById[e.to].kind === "ticket" && ACTIVE.has(nodeById[e.to].t.state);
    const mid = e.points[Math.floor(e.points.length / 2)];
    return `<path class="edge ${e.cls}${intoActive ? " into-active" : ""}" d="${pathFor(e.points)}"/>` +
      (e.label ? `<text class="edge-label" x="${mid.x.toFixed(1)}" y="${(mid.y - 5).toFixed(1)}" text-anchor="middle">${esc(e.label)}</text>` : "");
  }).join("");
  $("nodes").innerHTML = model.nodes.map((n) => {
    const tx = (n.x - n.w / 2).toFixed(1), ty = (n.y - n.h / 2).toFixed(1);
    if (n.kind === "ms") {
      const sub = !n.stats.all ? "planned" : `${n.stats.done}/${n.stats.all} done` + (n.m.status === "closed" ? " · closed" : "");
      return `<g class="ms ${n.cls} ${n.open ? "msopen" : ""}" transform="translate(${tx},${ty})" onclick="toggleMs('${n.m.id}')"><rect width="${n.w}" height="${n.h}"/>
        <text x="${n.w / 2}" y="19" style="font-weight:700">${n.open ? "▼ " : ""}${esc(n.m.id)} · ${esc(n.m.name.slice(0, 18))}</text><text class="sub" x="${n.w / 2}" y="35">${esc(sub)}${n.open ? " · open" : ""}</text><title>${esc(n.m.name)}: click to ${n.open ? "close" : "open"}</title></g>`;
    }
    const t = n.t, active = ACTIVE.has(t.state), queued = t.state === "APPROVED" || t.state === "READY_FOR_REVIEW", done = DONE.has(t.state), waiting = t.state === "OPEN" && !t.ready;
    const label = t.state === "OPEN" ? (t.ready ? "READY" : (t.waiting_on && t.waiting_on.length ? "WAITS " + t.waiting_on.join(",") : "CLOSED")) : t.state.replace(/_/g, " ");
    const cls = ["node", active ? "active" : "", queued ? "queued" : "", done ? "done" : "", waiting ? "waiting" : ""].join(" ");
    return `<g class="${cls}" style="--c:${color(t.state)}" transform="translate(${tx},${ty})" onclick="openTicket('${t.id}')">
      <rect class="card-bg" width="${n.w}" height="${n.h}"/><rect class="bar" width="6" height="${n.h}" fill="${color(t.state)}"/>
      <text x="14" y="17" style="font-weight:700;fill:#e6e8ee">${done ? "✓ " : ""}${esc(t.id)}</text>
      <text class="pill" x="${n.w - 8}" y="17" text-anchor="end" fill="${color(t.state)}">${esc(label)}</text>
      <text class="title" x="14" y="34" fill="#c9d1d9">${esc(t.title.slice(0, 30))}${t.title.length > 30 ? "…" : ""}</text>
      <title>${esc(t.why_waiting || t.state)}: ${esc(t.title)}</title></g>`;
  }).join("");
  if (!fitted) { fit(); fitted = true; }
}

// Opening milestones, panning and zooming.
function toggleMs(id) { if (opened.has(id)) opened.delete(id); else opened.add(id); sessionStorage.setItem("graph.open", JSON.stringify([...opened])); render(); }
function collapseAll() { opened.clear(); sessionStorage.setItem("graph.open", "[]"); render(); }
function applyView() { $("view").setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`); }
function fit() {
  // Frame the live subset: starts at the first open milestone, so finished history sits off to the left.
  const st = $("stage").getBoundingClientRect();
  const b = liveBox || { x0: 0, y0: 0, x1: bounds.w, y1: bounds.h };
  const w = Math.max(1, b.x1 - b.x0), h = Math.max(1, b.y1 - b.y0);
  view.k = Math.min(1.4, Math.max(0.12, Math.min((st.width - 40) / w, (st.height - 40) / h)));
  view.x = 20 - b.x0 * view.k + Math.max(0, ((st.width - 40) - w * view.k) / 2);
  view.y = 20 - b.y0 * view.k + Math.max(0, ((st.height - 40) - h * view.k) / 2); applyView();
}
(function wire() {
  const stage = $("stage"); let drag = null, moved = false;
  stage.addEventListener("mousedown", (e) => { if (e.button !== 0) return; e.preventDefault(); moved = false; drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; stage.classList.add("dragging"); });
  window.addEventListener("mousemove", (e) => { if (!drag) return; if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) moved = true; view.x = drag.vx + (e.clientX - drag.x); view.y = drag.vy + (e.clientY - drag.y); applyView(); });
  window.addEventListener("mouseup", () => { drag = null; stage.classList.remove("dragging"); });
  stage.addEventListener("click", (e) => { if (moved) { e.stopPropagation(); e.preventDefault(); } }, true);
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const scale = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1, dx = e.deltaX * scale, dy = e.deltaY * scale;
    if (e.ctrlKey || e.metaKey) {
      const r = stage.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      const factor = Math.exp(Math.max(-40, Math.min(40, -dy)) * 0.006), k = Math.min(3, Math.max(0.1, view.k * factor));
      view.x = mx - (mx - view.x) * (k / view.k); view.y = my - (my - view.y) * (k / view.k); view.k = k;
    } else { view.x -= dx; view.y -= dy; }
    applyView();
  }, { passive: false });
  let touch = null;
  const dist = (ts) => Math.hypot(ts[0].clientX - ts[1].clientX, ts[0].clientY - ts[1].clientY);
  const mid = (ts) => [(ts[0].clientX + ts[1].clientX) / 2, (ts[0].clientY + ts[1].clientY) / 2];
  stage.addEventListener("touchstart", (e) => {
    if (e.touches.length === 1) touch = { x: e.touches[0].clientX, y: e.touches[0].clientY, vx: view.x, vy: view.y, moved: false };
    else if (e.touches.length === 2) touch = { d: dist(e.touches), k: view.k, m: mid(e.touches), vx: view.x, vy: view.y, moved: true };
  }, { passive: true });
  stage.addEventListener("touchmove", (e) => {
    if (!touch) return; e.preventDefault();
    if (e.touches.length === 1 && touch.d === undefined) { const dx = e.touches[0].clientX - touch.x, dy = e.touches[0].clientY - touch.y; if (Math.abs(dx) + Math.abs(dy) > 4) touch.moved = true; view.x = touch.vx + dx; view.y = touch.vy + dy; applyView(); }
    else if (e.touches.length === 2 && touch.d !== undefined) { const r = stage.getBoundingClientRect(), m = mid(e.touches), mx = m[0] - r.left, my = m[1] - r.top; const k = Math.min(3, Math.max(0.1, touch.k * dist(e.touches) / touch.d)); view.x = mx - (mx - touch.vx) * (k / touch.k) + (m[0] - touch.m[0]); view.y = my - (my - touch.vy) * (k / touch.k) + (m[1] - touch.m[1]); view.k = k; applyView(); }
  }, { passive: false });
  stage.addEventListener("touchend", (e) => { if (touch && touch.moved) moved = true; if (!e.touches.length) touch = null; }, { passive: true });
})();
$("filter").addEventListener("input", render);
$("showdone").addEventListener("change", render);
refresh(); setInterval(refresh, 2500);
tasksFromHash();
