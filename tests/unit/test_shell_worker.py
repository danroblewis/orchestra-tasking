"""The demo's worker (demo/machines/shell_worker.py): it runs a ticket's commands in the ticket's
own folder with ORCHESTRA_REPO naming the repository, says where it ran, and on a rejection it
cannot act on reports BLOCKED with the reviewer's words in the reason; a release stops it. In code
mode it commits in the worktree; with --only-commands it leaves prose tickets to another worker."""

import subprocess
import time
from pathlib import Path

from demo.machines.shell_worker import Job


class Recorder:
    """The bus as the job sees it: what it publishes."""

    def __init__(self):
        self.said = []

    def publish(self, kind, data, **ext):
        self.said.append((kind, data))


def start_job(tmp_path, brief: dict, repo: str, lease_s: float = 180.0) -> tuple[Job, Recorder]:
    workspace = tmp_path / "workspaces" / "T-1"
    workspace.mkdir(parents=True, exist_ok=True)
    events, jobs = Recorder(), {}
    offer = {"assignment": "T-1.worker.1", "ticket": "T-1", "role": "worker", "workspace": str(workspace),
             "lease_s": lease_s, "brief": brief}
    return Job(events, "worker/1", offer, jobs, {"PATH": "/usr/bin:/bin", "ORCHESTRA_REPO": repo}), events


def run_job(tmp_path, brief: dict, repo: str) -> tuple[dict, Recorder]:
    job, events = start_job(tmp_path, brief, repo)
    assert job.done.wait(30)
    return next(d["report"] for k, d in events.said if k == "task.reported"), events


def test_commands_run_in_the_tickets_folder_with_the_repository_named(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("one\ntwo\n")
    brief = {"title": "Count", "description": "```sh\npwd\n```\n\n```sh\ncd \"$ORCHESTRA_REPO\" && wc -l README.md\n```"}
    report, events = run_job(tmp_path, brief, str(repo))
    assert [k for k, _ in events.said] == ["task.accepted", "task.reported"]
    assert report["state"] == "READY_FOR_REVIEW" and "README.md" in report["summary"]
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
    assert len(events.said) == said and "task.reported" not in [k for k, _ in events.said]
    assert not (folder / "second").exists() and not (folder / "RESULT.md").exists() and not (folder / ".commands").exists()


def test_in_code_mode_the_commands_run_in_the_worktree_and_are_committed_without_a_result_file(tmp_path):
    """A branch in the offer: the workspace is a worktree; what the commands change is committed on
    it and nothing else (no RESULT.md, no record of the commands) goes into the branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "orchestra/T-1-x", str(wt), "main"], cwd=repo, check=True)
    events, jobs = Recorder(), {}
    offer = {"assignment": "T-1.worker.1", "ticket": "T-1", "role": "worker", "workspace": str(wt), "branch": "orchestra/T-1-x",
             "lease_s": 180.0, "brief": {"title": "Add a collector", "description": "```sh\necho 'x = 1' > collector.py\n```"}}
    env = {"PATH": "/usr/bin:/bin", "ORCHESTRA_REPO": str(repo), "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}          # no identity of the machine's own
    job = Job(events, "worker/1", offer, jobs, env)
    assert job.done.wait(30)
    report = next(d["report"] for k, d in events.said if k == "task.reported")
    assert report["state"] == "READY_FOR_REVIEW" and "Committed" in report["summary"] and "orchestra/T-1-x" in report["summary"]
    files = subprocess.run(["git", "show", "--name-only", "--format=%an", "HEAD"], cwd=wt, capture_output=True, text=True, check=True).stdout.split()
    assert files == ["worker/1", "collector.py"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True).stdout == ""
    assert not (wt / "RESULT.md").exists() and not (wt / ".commands").exists()


def test_with_only_commands_an_offer_without_commands_is_left_for_another_worker(tmp_path, monkeypatch):
    """--only-commands: the offer for a prose ticket is not accepted (another machine serving the
    role takes it); one with commands is."""
    from demo.machines import shell_worker

    said = []

    class Bus:
        def __init__(self):
            self.offers = [{"type": "task.assigned", "data": {"assignment": f"T-{i}.worker.1", "ticket": f"T-{i}", "role": "worker",
                                                               "workspace": str(tmp_path / f"T-{i}"), "lease_s": 180.0,
                                                               "brief": {"title": "x", "description": d}}}
                           for i, d in ((1, "Write a collector that measures disk usage."), (2, "```sh\ntrue\n```"))]
            for o in self.offers:
                Path(o["data"]["workspace"]).mkdir()

        def publish(self, kind, data, **ext):
            said.append((kind, data))

        def end(self):
            return 0

        def after(self, offset):
            if offset == 0:
                return self.offers, 1
            raise KeyboardInterrupt                      # the second read ends the loop

    monkeypatch.setattr(shell_worker, "load_project", lambda: type("P", (), {"repo_root": tmp_path})())
    monkeypatch.setattr(shell_worker.Events, "for_project", staticmethod(lambda *a, **k: Bus()))
    monkeypatch.setattr(shell_worker.time, "sleep", lambda s: None)
    try:
        shell_worker.main("1", only_commands=True)
    except KeyboardInterrupt:
        pass
    deadline = time.time() + 10
    while not any(k == "task.reported" for k, _ in said) and time.time() < deadline:
        time.sleep(0.05)
    assert [d["ticket"] for k, d in said if k == "task.accepted"] == ["T-2"]
    assert [d["report"]["state"] for k, d in said if k == "task.reported"] == ["READY_FOR_REVIEW"]
