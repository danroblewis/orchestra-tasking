"""The machines shipped in the package (tasking/machines/) are examples of a machine written with Orchestra's library alone (AGENTS.md rule 9): they import only
orchestra.config and orchestra.events, and nothing in the task system imports them."""

import ast
from pathlib import Path

import tasking

PACKAGE = Path(tasking.__file__).parent
MACHINES = sorted((PACKAGE / "machines").glob("*.py"))


def imported(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_the_machines_import_only_orchestras_config_and_events():
    assert {"shell_worker.py", "check_reviewer.py"} <= {p.name for p in MACHINES}
    for path in MACHINES:
        foreign = {n for n in imported(path) if n.split(".")[0] in ("orchestra", "tasking", "demo")}
        assert foreign <= {"orchestra.config", "orchestra.events"}, f"{path} imports {sorted(foreign)}"


def test_nothing_in_the_task_system_imports_the_machines():
    for path in PACKAGE.rglob("*.py"):
        if (PACKAGE / "machines") in path.parents:
            continue
        assert not any(n.startswith("tasking.machines") for n in imported(path)), f"{path} imports a machine"
