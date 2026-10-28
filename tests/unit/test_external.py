"""External review's settings and what the worker is told of the feedback (tasking/external.py)."""

from tasking.config import Config
from tasking.external import describe
from tasking.model import TRANSITIONS, State


def test_external_review_is_off_unless_asked_for_in_code_mode():
    assert not Config.from_toml('[project]\nwork = "code"\n').external_review
    assert not Config.from_toml('[project]\nwork = "tasks"\n[gate]\nmode = "external"\n').external_review   # no branch to hand off
    c = Config.from_toml('[project]\nwork = "code"\n[gate]\nmode = "external"\n')
    assert c.external_review and c.external.failing_labels == () and c.external.line_comments and not c.external.conversation_comments
    assert c.external.failing_checks and c.external.settle == 15.0 and c.external.repository is None


def test_the_external_table_is_read():
    c = Config.from_toml('[project]\nwork = "code"\n[gate]\nmode = "external"\n[gate.external]\nrepository = "acme/service"\n'
                         'failing_labels = ["SAIL-failed"]\npassing_labels = ["SAIL-passed"]\nline_comments = false\n'
                         'conversation_comments = true\nfailing_checks = false\nsettle = "1m"\n')
    e = c.external
    assert (e.repository, e.failing_labels, e.passing_labels) == ("acme/service", ("SAIL-failed",), ("SAIL-passed",))
    assert (e.line_comments, e.conversation_comments, e.failing_checks, e.settle) == (False, True, False, 60.0)


def test_the_feedback_is_metadata_only():
    p = {"pull": "acme/service#12", "head": "a" * 40, "reviews": [{"by": "person.alice", "state": "changes_requested", "comments": 2, "review": 9}],
         "comments": [{"by": "person.alice", "path": "x.py", "line": 3, "review": 9}, {"by": "person.alice", "path": "y.py", "line": 4, "review": 9}],
         "conversation": [{"by": "person.bob"}], "labels": [{"by": "person.sail", "label": "SAIL-failed"}],
         "checks": [{"by": "bot.ci", "check": "unit", "conclusion": "failure"}]}
    summary, feedback, meta = describe(p)
    assert summary == ("acme/service#12 at aaaaaaaaaa: person.alice requested changes with 2 line comment(s); line comments at x.py:3, y.py:4; "
                       "1 comment(s) on the conversation by person.bob; the label SAIL-failed added by person.sail; the check unit ended failure")
    assert feedback.startswith("Feedback on the pull request acme/service#12, at aaaaaaaaaa: ") and "read them there" in feedback
    assert meta["conversation"] == 1 and meta["labels"] == p["labels"] and meta["comments"] == p["comments"]


def test_a_merge_is_final_from_any_waiting_state():
    waiting = {State.OPEN, State.REJECTED, State.READY_FOR_REVIEW, State.APPROVED, State.BLOCKED, State.IN_EXTERNAL_REVIEW}
    assert {s for s, to in TRANSITIONS.items() if State.DONE in to} == waiting | {State.MERGING}
    assert TRANSITIONS[State.IN_EXTERNAL_REVIEW] == {State.REJECTED, State.DONE, State.BLOCKED, State.CANCELLED}
