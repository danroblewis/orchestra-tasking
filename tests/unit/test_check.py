"""The task system's reader for Orchestra's config_edit (tasking/check.py): what it says against a
merged configuration, and that a clean one gets nothing."""

from tasking.check import check


def test_a_clean_configuration_has_no_problems():
    assert check({}) == []
    assert check({"project": {"work": "code"}, "gate": {"unit": [{"name": "unit", "command": "pytest --junitxml={reports}/u.xml"}]}}) == []


def test_unknown_keys_and_wrong_values_are_named():
    said = check({"project": {"work": "kode", "branch": "main"}, "gate": {"mode": "merge", "timeout": "soon"}})
    assert any("[project] branch: unknown key" in s for s in said)
    assert any("[project] work = 'kode'" in s for s in said)
    assert any("[gate] mode = 'merge'" in s for s in said)
    assert any("[gate] timeout" in s for s in said)


def test_a_brace_the_gate_cannot_fill_is_refused():
    said = check({"gate": {"unit": [{"name": "u", "command": "awk '{print $1}' x"}],
                           "acceptance": [{"name": "a", "command": "curl {url}/{reports}"}]}})
    assert said == ["[[gate.unit]] #1 command: {print $1} is not a placeholder here ({reports}); write {{ and }} for a literal brace"]
    assert check({"staging": {"start": "run --port {port} {url}"}})[0].startswith("[staging] start: {url}")


def test_entries_need_a_unique_name_and_a_command():
    said = check({"gate": {"unit": [{"name": "u", "command": "a"}, {"name": "u"}]}, "probes": [{"command": "x", "every": "often"}]})
    assert "[[gate.unit]] #2: another entry is already named 'u'" in said
    assert "[[gate.unit]] #2: command is required" in said
    assert "[[probes]] #1: name is required" in said and any(s.startswith("[[probes]] #1 every") for s in said)


def test_the_readers_own_refusals_and_recurring_problems_are_reported():
    assert check({"gate": {"stacking": "graphite"}}) == ['[gate] stacking = "graphite": "git-spice" or "off"']
    said = check({"tasks": {"recurring": [{"name": "brief", "every": "day", "at": "7am", "timezone": "UTC", "title": "x", "milestone": "M1"}]}})
    assert said and said[0].startswith("[[tasks.recurring]] brief:")


def test_stacking_without_git_spice_on_path_is_a_problem_with_its_fix(monkeypatch):
    import shutil

    raw = {"project": {"work": "code"}, "gate": {"stacking": "git-spice"}}
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    (said,) = check(raw)
    assert "git-spice is not on PATH" in said and "brew install git-spice" in said and 'stacking = "off"' in said
    assert check({"project": {"work": "code"}, "gate": {"stacking": "off"}}) == []
    assert check({"project": {"work": "tasks"}, "gate": {"stacking": "git-spice"}}) == []      # stacking is code mode's
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/bin/{cmd}")
    assert check(raw) == []
