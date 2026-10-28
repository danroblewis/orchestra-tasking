"""The merge gate's test reports (SPEC.md section 5, "Test reports"). Each run keeps a directory,
``<state>/reports/<run>/``, where its stage commands may write their own reports (``{reports}``
in a command), a sibling ``<run>.rerun<n>/`` for each rerun of a failed spec, and ``gate.xml``,
the run's stages as JUnit: one test case per spec run (``classname="merge_gate.<stage>"``), a
``<failure>`` when it failed, Surefire's ``<flakyFailure>`` for each failed attempt of a spec
that then passed on a rerun, ``<rerunFailure>`` for each rerun of one that never passed. The
instrument ``tasks.tests`` (tasking/web.py) reads them all."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

KEEP = 100            # report directories kept, newest first; older ones are removed after each run


def write_report(where: Path, run: dict) -> Path | None:
    """``gate.xml`` for a finished run in ``where``; None when the run ran no stage (a conflict)."""
    stages = run.get("stages") or []
    if not stages:
        return None
    suite = ET.Element("testsuite", name="merge_gate", tests=str(len(stages)),
                       failures=str(sum(s.get("status") == "failed" for s in stages)),
                       time=f"{sum(float(s.get('duration') or 0) for s in stages):.1f}", timestamp=str(run.get("started_at") or ""))
    ET.SubElement(ET.SubElement(suite, "properties"), "property", name="gate_run", value=str(run.get("id")))
    for s in stages:
        case = ET.SubElement(suite, "testcase", classname=f"merge_gate.{s.get('stage', '')}", name=str(s.get("name", "")),
                             time=f"{float(s.get('duration') or 0):.1f}")
        failed = s.get("status") == "failed"
        if failed:
            ET.SubElement(case, "failure", message=f"exit {s.get('returncode')}").text = s.get("tail") or ""
        for a in s.get("failed_attempts") or []:
            ET.SubElement(case, "flakyFailure", message=f"exit {a.get('returncode')}").text = a.get("tail") or ""
        if failed:
            for _ in range(int(s.get("reruns") or 0)):
                ET.SubElement(case, "rerunFailure", message="failed again on a rerun")
    where.mkdir(parents=True, exist_ok=True)
    path = where / "gate.xml"
    root = ET.Element("testsuites", name="merge_gate")
    root.append(suite)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def prune_reports(root: Path, keep: int = KEEP) -> None:
    """Only the newest ``keep`` report directories stay."""
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[keep:]:
        shutil.rmtree(d, ignore_errors=True)
