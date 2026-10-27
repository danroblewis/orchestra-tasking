// The task system's one every-page script (Orchestra loads it on every page of its web UI,
// orchestra/plugins.py): it loads tasks.js, the shared code and look, and then the scripts that
// need it: the blocked-ticket banner and the Tasks panel. A page
// that already has tasks.js (the Tickets page) keeps its copy; nothing is loaded twice.
(function () {
  const have = (path) => [...document.scripts].some((s) => s.src && new URL(s.src).pathname === path);
  const load = (name) => new Promise((ok) => {
    const path = `/plugins/tasks/static/${name}`;
    if (have(path)) return ok();
    const s = document.createElement("script"); s.src = path; s.onload = ok; s.onerror = ok; document.body.appendChild(s);
  });
  load("tasks.js").then(() => ["attention.js", "tasks-panel.js"].forEach(load));
})();
