"""The demo's worker (demo/machines/shell_worker.py): it runs a ticket's commands in the ticket's
own folder with ORCHESTRA_REPO naming the repository, says where it ran, and on a rejection it
cannot act on reports BLOCKED with the reviewer's words in the reason; a release stops it."""

import time

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
