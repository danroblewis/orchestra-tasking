"""The task system reads its configuration only through Orchestra's objects (its AGENTS.md rule 10):
never by opening orchestra.toml, so a section set only in the owner's private overlay reaches it."""

import ast
import builtins
import inspect
import subprocess
import sys
from pathlib import Path

from orchestra.config import load_project as load_orchestra_project

import tasking
from tasking.config import load_project
from tasking.events import Events
from tasking.scheduler import Scheduler
from tasking.store import LocalStore
from tasking.web import plugin

PACKAGE = Path(tasking.__file__).parent
NAMES = {"orchestra.toml", "orchestra.local.toml"}
FILE_IO = {"read_text", "read_bytes", "write_text", "write_bytes", "open", "unlink", "replace", "rename"}


def _opens_the_config(tree: ast.AST) -> list[str]:
    """Where code builds a path to the configuration file, or opens one by name."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and (
                isinstance(node.right, ast.Constant) and node.right.value in NAMES
                or isinstance(node.right, ast.Name) and node.right.id in ("CONFIG_FILE", "OVERLAY_FILE")):
            out.append(f"line {node.lineno}: a path to {ast.unparse(node.right)}")
        elif isinstance(node, ast.Call) and any(isinstance(a, ast.Constant) and a.value in NAMES for a in node.args):
            out.append(f"line {node.lineno}: {ast.unparse(node.func)}({ast.unparse(node.args[0])})")
        elif (isinstance(node, ast.Attribute) and node.attr in FILE_IO and isinstance(node.value, ast.Attribute)
              and node.value.attr in ("config_file", "overlay_file")):
            out.append(f"line {node.lineno}: {ast.unparse(node)}")              # naming the file (to print it) is fine
        elif isinstance(node, ast.Call) and ast.unparse(node.func) == "open" and node.args and any(
                isinstance(n, ast.Attribute) and n.attr in ("config_file", "overlay_file") for n in ast.walk(node.args[0])):
            out.append(f"line {node.lineno}: open({ast.unparse(node.args[0])})")
        elif isinstance(node, ast.ImportFrom) and node.module == "orchestra.config" and any(
                a.name in ("CONFIG_FILE", "OVERLAY_FILE") for a in node.names):
            out.append(f"line {node.lineno}: imports the file's name")
    return out


def test_no_module_of_the_task_system_opens_orchestra_toml():
    found = {str(f.relative_to(PACKAGE)): bad for f in sorted(PACKAGE.rglob("*.py"))
             if (bad := _opens_the_config(ast.parse(f.read_text(encoding="utf-8"))))}
    assert found == {}, f"read and change configuration through Orchestra's objects (project.config, config_edit): {found}"


def test_loading_and_reloading_reads_no_config_file_from_the_task_system(project, monkeypatch):
    """At run time too: every read of orchestra.toml or the overlay comes from Orchestra's code."""
    readers = []
    real_open, real_read = builtins.open, Path.read_text

    def caller() -> str:
        f = inspect.currentframe().f_back.f_back
        return f.f_globals.get("__name__", "")

    def watch_open(file, *a, **k):
        if Path(str(file)).name in NAMES:
            readers.append(caller())
        return real_open(file, *a, **k)

    def watch_read(self, *a, **k):
        if self.name in NAMES:
            readers.append(caller())
        return real_read(self, *a, **k)

    monkeypatch.setattr(builtins, "open", watch_open)
    monkeypatch.setattr(Path, "read_text", watch_read)
    p = load_project(project.repo_root)
    p.reload_config()
    plugin(load_orchestra_project(project.repo_root))
    assert readers and all(not r.startswith("tasking") for r in readers), readers


def test_a_section_set_only_in_the_overlay_reaches_the_scheduler(project):
    overlay = project.orchestra.overlay_file
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text('[tasks]\nworkers = 5\n\n[project]\nticket_prefix = "MINE"\n')
    p = load_project(project.repo_root)
    assert (p.config.workers, p.config.ticket_prefix) == (5, "MINE")
    store = LocalStore(project, Events.for_project(project, source="task_scheduler"))
    s = Scheduler(project, store, Events.for_project(project, source="task_scheduler"))
    s.tick()                                                         # the scheduler reloads on every tick
    assert project.config.workers == 5 and store.capacity()["max_workers"] == 5
    overlay.write_text('[tasks]\nworkers = 3\n')
    s.tick()
    assert project.config.workers == 3 and project.config.ticket_prefix == "T"


def test_a_project_set_up_for_tasks_only_in_the_overlay_runs_the_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text('[events]\nurl = "file"\n')
    o = load_orchestra_project(repo)
    o.config_edit.set("tasks.workers", 1, target="overlay")
    assert [x.argv() for x in plugin(load_orchestra_project(repo)).processes] == [
        [sys.executable, "-m", "orchestra", "tasks", "run", "--wait"]]
