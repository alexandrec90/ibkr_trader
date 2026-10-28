"""The UI server's launcher watch (web/lifetime.py), against real processes.

The bug this exists for: under the devkit task, killing `log-wrap.py` left the server
running. So the tests use real child processes and the real OS wait, not a fake clock.
"""

import os
import subprocess
import sys
import threading

from ibkr_trader.web.lifetime import _state_from_stat, wait_for_exit, watch_process


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_state_from_stat_reads_the_field_after_the_command_name():
    assert _state_from_stat("4242 (python) S 1 4242 4242 0 -1") == "S"
    assert _state_from_stat("4242 (python) Z 1 4242 4242 0 -1\n") == "Z"


def test_state_from_stat_survives_a_command_name_with_spaces_and_parens():
    assert _state_from_stat("7 (a b) c) Z 1 7 7") == "Z"


def test_watch_fires_when_the_launcher_is_killed():
    """Killed but not yet reaped -- `launcher.wait()` runs only in `finally` -- so on Linux
    the launcher is a zombie here, which signal 0 still finds. It has ended all the same."""
    launcher = _sleeper()
    fired = threading.Event()
    try:
        watch_process(launcher.pid, fired.set)
        assert not fired.wait(0.5)  # alive: the server keeps serving
        launcher.kill()
        assert fired.wait(10)  # gone: the server is asked to stop
    finally:
        launcher.kill()
        launcher.wait()


def test_a_launcher_already_gone_returns_at_once():
    launcher = subprocess.Popen([sys.executable, "-c", "pass"])
    launcher.wait()
    done = threading.Event()

    watch_process(launcher.pid, done.set)

    assert done.wait(10)


def test_wait_blocks_while_the_process_lives():
    """The current process outlives this test, so the wait must not return."""
    thread = threading.Thread(target=wait_for_exit, args=(os.getpid(),), daemon=True)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive()


def test_watch_returns_a_started_daemon_thread():
    launcher = _sleeper()
    try:
        thread = watch_process(launcher.pid, lambda: None)
        assert thread.daemon and thread.is_alive()  # never holds the server's exit
    finally:
        launcher.kill()
        launcher.wait()
