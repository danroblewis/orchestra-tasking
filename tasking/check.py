"""What the task system says against a merged configuration (Orchestra's ``project.config.raw``):
a list of problems, empty when its sections read cleanly (or are absent). It is the plugin's
``Plugin.check``, which Orchestra's ``config_edit`` runs on every change, so a change that makes it
say something new is refused with what it said; the operator's guide (``guide.md``) names it.

It says what is wrong whatever order settings are made in: an unknown key, a value of the wrong
kind, an entry without its name or command, a placeholder a command cannot be given (the gate
fills commands with ``str.format``, and a run whose command cannot be filled is a gate error that
pauses the pipeline), and every problem ``recurring.check`` finds. Settings that are valid but do
nothing together (``[[gate.acceptance]]`` without ``[acceptance] start``) are the guide's to
explain, not refused here: a configuration is often written one setting at a time."""

from __future__ import annotations

import string
from typing import Any

from orchestra.config import ConfigError, parse_duration

from . import recurring
from .config import Config

SECTIONS: dict[str, set[str]] = {
    "project": {"base_branch", "ticket_prefix", "work", "push"},
    "tasks": {"workers", "reviewers", "tick", "return_timeout", "review_rounds_before_escalation", "aging_days_per_level",
              "reuse_worktrees", "worktree_pool", "worktree_clean_ignored", "recurring"},
    "build": {"command", "artifact", "timeout"},
    "acceptance": {"start", "ready", "ready_timeout"},
    "gate": {"timeout", "mode", "rerun_failures", "test_reports", "external", "stacking", "stack_depth", "unit", "acceptance"},
    "gate.test_reports": {"enabled", "kinds", "window", "confirm"},
    "gate.external": {"repository", "failing_labels", "passing_labels", "line_comments", "conversation_comments",
                      "failing_checks", "settle"},
    "staging": {"start", "port", "health", "health_timeout", "stop", "tunnel"},
    "staging.tunnel": {"name", "hostname"},
}
ENTRIES = {"gate.unit": {"name", "command", "timeout"}, "gate.acceptance": {"name", "command", "timeout"},
           "probes": {"name", "command", "every", "timeout"}}
TARGET = {"artifact", "port", "url", "python"}
PLACEHOLDERS = {"build.command": {"reports"}, "gate.unit": {"reports"}, "gate.acceptance": TARGET | {"reports"},
                "acceptance.start": TARGET, "acceptance.ready": TARGET, "staging.start": {"artifact", "port", "python"},
                "staging.stop": {"port", "pid"}}
CHOICES = {"project.work": ("tasks", "code"), "gate.mode": ("land", "external")}
DURATIONS = ("tasks.tick", "tasks.return_timeout", "build.timeout", "acceptance.ready_timeout", "gate.timeout",
             "gate.external.settle", "staging.health_timeout")


def _at(raw: dict, dotted: str) -> Any:
    v: Any = raw
    for part in dotted.split("."):
        if not isinstance(v, dict):
            return None
        v = v.get(part)
    return v


def _placeholders(where: str, text: Any, allowed: set[str]) -> list[str]:
    if not isinstance(text, str):
        return [f"{where} must be text"]
    try:
        names = [n for _, n, _, _ in string.Formatter().parse(text) if n is not None]
    except ValueError as e:
        return [f"{where}: {e}; write {{{{ and }}}} for a literal brace"]
    shown = ", ".join("{" + p + "}" for p in sorted(allowed))
    return [f"{where}: {{{n}}} is not a placeholder here ({shown}); write {{{{ and }}}} for a literal brace"
            for n in names if n not in allowed]


def _sections(raw: dict) -> list[str]:
    out = []
    for name, keys in SECTIONS.items():
        table = _at(raw, name)
        if table is None:
            continue
        if not isinstance(table, dict):
            out.append(f"[{name}] must be a table")
            continue
        out += [f"[{name}] {k}: unknown key; the keys are {', '.join(sorted(keys))}" for k in sorted(set(table) - keys)]
    for key, allowed in CHOICES.items():
        v = _at(raw, key)
        if v is not None and v not in allowed:
            section, k = key.rsplit(".", 1)
            out.append(f"[{section}] {k} = {v!r}: {' or '.join(repr(a) for a in allowed)}")
    for key in DURATIONS:
        try:
            parse_duration(_at(raw, key), 0.0)
        except ConfigError as e:
            section, k = key.rsplit(".", 1)
            out.append(f"[{section}] {k}: {e}")
    return out


def _entries(raw: dict) -> list[str]:
    out = []
    for name, keys in ENTRIES.items():
        items = _at(raw, name)
        if items is None:
            continue
        if not isinstance(items, list):
            out.append(f"write each as [[{name}]] (an array of tables)")
            continue
        seen: set[str] = set()
        for i, d in enumerate(items):
            label = f"[[{name}]] #{i + 1}"
            if not isinstance(d, dict):
                out.append(f"{label} must be a table")
                continue
            out += [f"{label} {k}: unknown key; the keys are {', '.join(sorted(keys))}" for k in sorted(set(d) - keys)]
            if not isinstance(d.get("name"), str) or not d["name"].strip():
                out.append(f"{label}: name is required")
            elif d["name"] in seen:
                out.append(f"{label}: another entry is already named {d['name']!r}")
            else:
                seen.add(d["name"])
            if not isinstance(d.get("command"), str) or not d["command"].strip():
                out.append(f"{label}: command is required")
            elif name in PLACEHOLDERS:
                out += _placeholders(f"{label} command", d["command"], PLACEHOLDERS[name])
            for k in ("timeout", "every"):
                try:
                    parse_duration(d.get(k), 0.0)
                except ConfigError as e:
                    out.append(f"{label} {k}: {e}")
    return out


def check(raw: dict) -> list[str]:
    """Every problem the task system finds in ``raw``; [] when its sections read cleanly."""
    out = _sections(raw) + _entries(raw)
    for key, allowed in PLACEHOLDERS.items():
        if "." in key and not key.startswith("gate.") and _at(raw, key) is not None:
            section, k = key.rsplit(".", 1)
            out += _placeholders(f"[{section}] {k}", _at(raw, key), allowed)
    if not out:                       # what the reader itself refuses, once the shapes are right
        try:
            Config.from_raw(raw)
        except Exception as e:  # noqa: BLE001 - whatever the reader raises is a problem to report, never a crash
            out.append(f"{type(e).__name__}: {e}" if not isinstance(e, ConfigError) else str(e))
    if isinstance(raw.get("tasks") or {}, dict):
        for label, reasons in recurring.check(raw).problems.items():
            out += [f"[[tasks.recurring]] {label}: {r}" for r in reasons]
    return out
