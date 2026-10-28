"""The command line is `orchestra tasks`, an entry point in ``orchestra.commands``, not a script."""

import subprocess
import sys
from importlib.metadata import entry_points

from tasking import cli


def test_registered_as_orchestra_tasks_and_no_script():
    eps = {e.name: e.value for e in entry_points(group="orchestra.commands")}
    assert eps.get("tasks") == "tasking.cli:main"
    assert not [e for e in entry_points(group="console_scripts") if e.value.startswith("tasking")]


def test_prog_comes_from_orchestra(monkeypatch, capsys):
    monkeypatch.setenv("ORCHESTRA_PROG", "orchestra tasks")
    assert cli.build_parser().prog == "orchestra tasks"
    monkeypatch.delenv("ORCHESTRA_PROG")
    assert cli.prog() == "orchestra tasks"


def test_orchestra_runs_it():
    out = subprocess.run([sys.executable, "-m", "orchestra", "tasks", "--help"], check=False, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("usage: orchestra tasks")
    listed = subprocess.run([sys.executable, "-m", "orchestra", "--help"], check=False, capture_output=True, text=True, timeout=60)
    assert "tasks" in listed.stdout and "(plugin)" in listed.stdout
