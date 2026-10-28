"""The program reviewer (tasking/machines/check_reviewer.py): a task is checked against its
acceptance text in its folder; code by a check command run in the ticket's worktree, at the commit
asked to be reviewed, with ORCHESTRA_REPO naming the worktree. It approves for the gate, or asks
the worker that did the work for changes, with what failed."""

import subprocess
import uuid

from tasking.machines.check_reviewer import review


class Recorder:
    """The bus as the reviewer sees it: what it publishes, read back in order."""

    def __init__(self):
        self.said, self.log = [], []

    def publish(self, kind, data, **ext):
        ev = {"type": kind, "source": "reviewer/1", "id": uuid.uuid4().hex, "data": data, **ext}
        self.said.append((kind, data))
        self.log.append(ev)
        return ev

    def after(self, offset):
        return self.log[offset:], len(self.log)


def verdict(said) -> tuple[str, dict]:
    [(kind, data)] = [(k, d) for k, d in said if k in ("review.approved", "changes.requested")]
    return kind, data


def run_review(offer: dict, check: str | None = None) -> Recorder:
    events = Recorder()
    review(events, "reviewer/1", {"assignment": "T-1.reviewer.1", "ticket": "T-1", "role": "reviewer", "lease_s": 180.0,
                                  "id": "offer", **offer}, 0, check)
    return events


def test_a_task_is_approved_when_its_files_and_lines_are_there(tmp_path):
    (tmp_path / "RESULT.md").write_text("ERC: 0 errors\n")
    (tmp_path / "summary.txt").write_text("x")
    events = run_review({"workspace": str(tmp_path), "branch": None,
                         "brief": {"acceptance": '`summary.txt` exists and the result says "ERC:".\nLooks tidy.'}})
    kind, data = verdict(events.said)
    assert kind == "review.approved" and "all 2 checkable criteria hold" in data["summary"] and "Looks tidy." in data["summary"]


def test_a_task_missing_a_file_goes_back_to_its_worker(tmp_path):
    (tmp_path / "RESULT.md").write_text("done\n")
    events = run_review({"workspace": str(tmp_path), "brief": {"acceptance": "`reports/erc.json` exists"},
                         "review": {"worker": "worker/2"}})
    kind, data = verdict(events.said)
    assert kind == "changes.requested" and data["to"] == "worker/2" and data["role"] == "worker"
    assert "`reports/erc.json` is missing" in data["feedback"] and data["brief"]["rejected_by"] == "reviewer"


def worktree(tmp_path):
    """A repository and a ticket's worktree on its branch with one commit beyond main: (worktree, commit)."""
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", *ident, "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "worktree", "add", "-q", "-b", "orchestra/T-1-x", str(wt), "main"], cwd=repo, check=True)
    (wt / "board.txt").write_text("ok\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", *ident, "commit", "-q", "-m", "T-1: work"], cwd=wt, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True).stdout.strip()
    return wt, head


def code_offer(wt, head, **more) -> dict:
    return {"workspace": str(wt), "branch": "orchestra/T-1-x", "base": "main", "brief": {"title": "x"},
            "review": {"commit": head, "worker": "worker/1"}, **more}


def test_code_is_approved_when_the_unit_command_passes_in_the_worktree(tmp_path):
    """The offer's unit command runs in the worktree, and finds it in ORCHESTRA_REPO, as in the gate."""
    wt, head = worktree(tmp_path)
    events = run_review(code_offer(wt, head, unit_command='grep -q ok "$ORCHESTRA_REPO/board.txt" && echo fine'))
    kind, data = verdict(events.said)
    assert kind == "review.approved" and data["commit"] == head and "passed" in data["summary"] and "fine" in data["summary"]


def test_code_whose_check_fails_goes_back_with_the_output(tmp_path):
    """--check overrides the unit command; a failure asks the worker for changes, with the output."""
    wt, head = worktree(tmp_path)
    events = run_review(code_offer(wt, head, unit_command="true"), check="echo 'DRC: 3 errors'; exit 2")
    kind, data = verdict(events.said)
    assert kind == "changes.requested" and data["to"] == "worker/1" and data["commit"] == head
    assert data["branch"] == "orchestra/T-1-x" and "exit 2" in data["feedback"] and "DRC: 3 errors" in data["feedback"]


def test_code_not_at_the_commit_to_review_is_not_approved(tmp_path):
    wt, _ = worktree(tmp_path)
    events = run_review(code_offer(wt, "0" * 40, unit_command="true"))
    kind, data = verdict(events.said)
    assert kind == "changes.requested" and "not at 0000000000" in data["feedback"]


def test_code_with_nothing_to_run_is_approved_for_the_gate_and_says_so(tmp_path):
    wt, head = worktree(tmp_path)
    kind, data = verdict(run_review(code_offer(wt, head)).said)
    assert kind == "review.approved" and "No check command" in data["summary"]


def test_a_long_check_keeps_the_lease(tmp_path):
    wt, head = worktree(tmp_path)
    events = run_review(code_offer(wt, head, lease_s=0.6), check="sleep 0.5")
    assert any(k == "task.working" for k, _ in events.said)
    assert verdict(events.said)[0] == "review.approved"
