"""No orphans: a turn's process that outlived its loop is stopped when the next loop starts."""

import subprocess
import time

import psutil

from tasking.loop import Loop


def test_an_orphaned_turn_is_stopped():
    p = subprocess.Popen(["sleep", "60"], start_new_session=True)       # as a harness starts a turn
    Loop._kill_orphan(p.pid, "worker-T-1")
    p.wait(timeout=5)
    assert p.returncode is not None


def test_a_pid_that_is_gone_is_left_alone():
    p = subprocess.Popen(["true"], start_new_session=True)
    p.wait()
    time.sleep(0.1)
    Loop._kill_orphan(p.pid, "worker-T-1")                              # no error
    assert not psutil.pid_exists(p.pid) or psutil.Process(p.pid).status() == psutil.STATUS_ZOMBIE
