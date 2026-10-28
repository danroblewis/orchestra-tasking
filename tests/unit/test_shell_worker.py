"""The program worker (tasking/machines/shell_worker.py): it runs a ticket's commands in the ticket's
own folder with ORCHESTRA_REPO naming the repository, says where it ran, and asks the reviewers
itself; on a rejection it cannot act on it reports BLOCKED with the reviewer's words in the reason;
a release stops it, and so does another worker's claim that came first. In code mode it commits in
the worktree and runs the unit command for the reviewer; with --only-commands it leaves prose
tickets to another worker."""

import subprocess
import threading
import time
import uuid
from pathlib import Path

from tasking.machines.shell_worker import Job


class Recorder:
    """The bus as the job sees it: what it publishes, read back in order (``first`` is a claim
    another worker made before the job's)."""

    def __init__(self, first: str | None = None):
        self.said, self.log = [], []
        self.first = first

    def publish(self, kind, data, **ext):
        if kind == "task.accepted" and self.first:
            self.log.append({"type": kind, "source": self.first, "id": "other", "data": {**data, "instance": self.first}})
        ev = {"type": kind, "source": "worker/1", "id": uuid.uuid4().hex, "data": data, **ext}
        self.said.append((kind, data))
        self.log.append(ev)
        return ev

    def after(self, offset):
        return self.log[offset:], len(self.log)


def outcome(said) -> dict:
    """What the job ended its turn with: a review request (as a report READY_FOR_REVIEW with what
    it handed the reviewer), or its report."""
    for kind, d in said:
        if kind == "review.requested":
            return {"state": "READY_FOR_REVIEW", **d["review"], "request": d}
        if kind == "task.reported":
            return d["report"]
    raise AssertionError(f"no end of the turn in {said}")


def start_job(tmp_path, brief: dict, repo: str, lease_s: float = 180.0, first: str | None = None) -> tuple[Job, Recorder]:
    workspace = tmp_path / "workspaces" / "T-1"
    workspace.mkdir(parents=True, exist_ok=True)
    events, jobs = Recorder(first), {}
    offer = {"assignment": "T-1.worker.1", "ticket": "T-1", "role": "worker", "workspace": str(workspace),
             "lease_s": lease_s, "brief": brief}
    return Job(events, "worker/1", offer, jobs, {"PATH": "/usr/bin:/bin", "ORCHESTRA_REPO": repo}), events


def run_job(tmp_path, brief: dict, repo: str) -> tuple[dict, Recorder]:
    job, events = start_job(tmp_path, brief, repo)
    assert job.done.wait(30)
    return outcome(events.said), events


def test_commands_run_in_the_tickets_folder_with_the_repository_named(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("one\ntwo\n")
    brief = {"title": "Count", "description": "```sh\npwd\n```\n\n```sh\ncd \"$ORCHESTRA_REPO\" && wc -l README.md\n```"}
    report, events = run_job(tmp_path, brief, str(repo))
    assert [k for k, _ in events.said] == ["task.accepted", "review.requested"]
    assert report["state"] == "READY_FOR_REVIEW" and "README.md" in report["summary"] and report["worker"] == "worker/1"
    request = report["request"]
    assert request["role"] == "reviewer" and request["ends"] == "T-1.worker.1" and request["assignment"].startswith("T-1.reviewer.")
    assert request["workspace"] == str(tmp_path / "workspaces" / "T-1") and request["review"]["commit"] is None
    result = (tmp_path / "workspaces" / "T-1" / "RESULT.md").read_text().splitlines()
    assert result[0] == "# T-1: Count" and result[2] == f"Ran in {tmp_path / 'workspaces' / 'T-1'} (ORCHESTRA_REPO={repo})."
    assert str(tmp_path / "workspaces" / "T-1") in "\n".join(result)      # pwd: the ticket's folder, not the repository
    assert "2 README.md" in "\n".join(result)


def test_a_rejection_the_same_commands_cannot_answer_is_blocked_with_the_reviewers_words(tmp_path):
    brief = {"title": "Date", "description": "```sh\ndate > today.txt\n```"}
    first, _ = run_job(tmp_path, brief, str(tmp_path))
    assert first["state"] == "READY_FOR_REVIEW"
    feedback = "- the file `yesterday.txt` is missing from the workspace\n- RESULT.md does not contain \"tomorrow\""
    again, _ = run_job(tmp_path, {**brief, "rejected_by": "reviewer", "feedback": feedback}, str(tmp_path))
    assert again["state"] == "BLOCKED" and again["summary"] == "rejected, and the same commands would give the same result"
    fact, quoted = again["blocked_reason"].split("\n\n")
    assert fact.startswith("The reviewer wants what these commands never produce") and "the file `yesterday.txt` is missing" in fact
    assert quoted == "\n".join("> " + ln for ln in feedback.splitlines())  # the whole feedback, for the planner to act on
    changed, _ = run_job(tmp_path, {**brief, "description": "```sh\ndate > today.txt\ndate > yesterday.txt\n```",
                                    "rejected_by": "reviewer", "feedback": feedback}, str(tmp_path))
    assert changed["state"] == "READY_FOR_REVIEW"                         # a planner changed the commands: it works again


def test_a_release_stops_the_commands_the_heartbeat_and_the_report(tmp_path):
    brief = {"title": "Slow", "description": "```sh\nsleep 30\n```\n\n```sh\ntouch second\n```"}
    job, events = start_job(tmp_path, brief, str(tmp_path), lease_s=0.6)    # a heartbeat every 0.1 s
    deadline = time.time() + 10
    while not any(k == "task.working" for k, _ in events.said) and time.time() < deadline:
        time.sleep(0.05)
    job.stop()                                                            # what task.released does
    assert job.done.wait(10)                                              # the sleep was killed, not waited out
    said = len(events.said)
    time.sleep(0.4)
    folder = tmp_path / "workspaces" / "T-1"
    assert len(events.said) == said and not {"task.reported", "review.requested"} & {k for k, _ in events.said}
    assert not (folder / "second").exists() and not (folder / "RESULT.md").exists() and not (folder / ".commands").exists()


def test_in_code_mode_the_commands_run_in_the_worktree_and_are_committed_without_a_result_file(tmp_path):
    """A branch in the offer: the workspace is a worktree; what the commands change is committed on
    it and nothing else (no RESULT.md, no record of the commands) goes into the branch. The unit
    command finds the worktree in ORCHESTRA_REPO, as it does in the merge gate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "orchestra/T-1-x", str(wt), "main"], cwd=repo, check=True)
    events, jobs = Recorder(), {}
    offer = {"assignment": "T-1.worker.1", "ticket": "T-1", "role": "worker", "workspace": str(wt), "branch": "orchestra/T-1-x",
             "base": "main", "unit_command": 'test -f "$ORCHESTRA_REPO/collector.py"',   # ORCHESTRA_REPO: the worktree, as in the gate
             "lease_s": 180.0, "brief": {"title": "Add a collector", "description": "```sh\necho 'x = 1' > collector.py\n```"}}
    env = {"PATH": "/usr/bin:/bin", "ORCHESTRA_REPO": str(repo), "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}          # no identity of the machine's own
    job = Job(events, "worker/1", offer, jobs, env)
    assert job.done.wait(30)
    report = outcome(events.said)
    assert report["state"] == "READY_FOR_REVIEW" and "Committed" in report["summary"] and "orchestra/T-1-x" in report["summary"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True).stdout.strip()
    assert report["commit"] == head and report["test_output"].endswith("[passed]")      # what the reviewer is handed
    files = subprocess.run(["git", "show", "--name-only", "--format=%an", "HEAD"], cwd=wt, capture_output=True, text=True, check=True).stdout.split()
    assert files == ["worker/1", "collector.py"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True).stdout == ""
    assert not (wt / "RESULT.md").exists() and not (wt / ".commands").exists()


def test_with_only_commands_an_offer_without_commands_is_left_for_another_worker(tmp_path, monkeypatch):
    """--only-commands: the offer for a prose ticket is not accepted (another machine serving the
    role takes it); one with commands is."""
    from tasking.machines import shell_worker

    said = []

    class Bus:
        def __init__(self):
            self.offers = [{"type": "task.assigned", "data": {"assignment": f"T-{i}.worker.1", "ticket": f"T-{i}", "role": "worker",
                                                               "workspace": str(tmp_path / f"T-{i}"), "lease_s": 180.0,
                                                               "brief": {"title": "x", "description": d}}}
                           for i, d in ((1, "Write a collector that measures disk usage."), (2, "```sh\ntrue\n```"))]
            for o in self.offers:
                Path(o["data"]["workspace"]).mkdir()
            self.log, self.read = [], False

        def publish(self, kind, data, **ext):
            said.append((kind, data))
            ev = {"type": kind, "source": "worker/1", "id": uuid.uuid4().hex, "data": data}
            self.log.append(ev)
            return ev

        def end(self):
            return 0

        def after(self, offset):
            if offset == 0 and not self.read:
                self.read = True
                return self.offers, 1
            if threading.current_thread() is threading.main_thread():
                raise KeyboardInterrupt                  # the main loop's second read ends it
            return [*self.offers, *self.log][offset:], len(self.offers) + len(self.log)

    monkeypatch.setattr(shell_worker, "load_project", lambda: type("P", (), {"repo_root": tmp_path})())
    monkeypatch.setattr(shell_worker.Events, "for_project", staticmethod(lambda *a, **k: Bus()))
    monkeypatch.setattr(shell_worker.time, "sleep", lambda s: None)
    try:
        shell_worker.main("1", only_commands=True)
    except KeyboardInterrupt:
        pass
    deadline = time.time() + 10
    while not any(k == "review.requested" for k, _ in said) and time.time() < deadline:
        time.sleep(0.05)
    assert [d["ticket"] for k, d in said if k == "task.accepted"] == ["T-2"]
    assert [d["ticket"] for k, d in said if k == "review.requested"] == ["T-2"]


def test_a_claim_another_worker_made_first_is_lost_and_nothing_runs(tmp_path):
    """Two workers claim one offer: the bus's order decides. The job reads its claim back, finds
    another's before it, and runs nothing, says nothing more."""
    brief = {"title": "Touch", "description": "```sh\ntouch ran\n```"}
    job, events = start_job(tmp_path, brief, str(tmp_path), first="worker/2")
    assert job.done.wait(10)
    assert [k for k, _ in events.said] == ["task.accepted"]
    assert not (tmp_path / "workspaces" / "T-1" / "ran").exists()
