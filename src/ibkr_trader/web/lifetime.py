"""Tie the UI server's life to the process that launched it.

`ibkr-trader ui` is meant to end with its terminal, and from a shell it does. From the
devkit workspace task it did not: the dispatcher's `log-wrap.py` starts its child with
CREATE_NO_WINDOW, so nothing below it is attached to the terminal's console, closing the
terminal kills only the console-attached processes, and Windows never kills a child with
its parent. Measured 2026-10-26: with `log-wrap.py` killed, `ui-task.py`, `uv` and the
server all lived on, still answering on their port -- the same leftover-process shape as
the Streamlit server that outlived a VS Code crash (TODO.md, 2026-07-19).

So the server watches the launcher's PID and asks itself to stop when that process is
gone. Stdlib only: a process handle on Windows (held from the server's start, so a PID
reused later cannot be mistaken for the launcher), a signal-0 probe elsewhere.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

POLL_SECONDS = 1.0

# Windows: the right to wait on a process, and "wait forever".
_SYNCHRONIZE = 0x0010_0000
_INFINITE = 0xFFFF_FFFF


def _state_from_stat(stat: str) -> str:
    """The one-letter state field of a ``/proc/<pid>/stat`` line.

    The command name is parenthesised and may itself hold spaces or ``)``, so the state is
    the first field after the *last* ``)``.
    """
    return stat.rpartition(")")[2].split()[0]


def _alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        pass
    # An exited process its parent has not reaped yet is a zombie: signal 0 still finds it,
    # but it has ended. Linux exposes the state in /proc; without /proc (macOS) the signal
    # probe is all there is, and a launcher's zombie lasts only until its parent reaps it.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            return _state_from_stat(fh.read()) not in ("Z", "X")
    except OSError:
        return True


def wait_for_exit(pid: int, *, poll: float = POLL_SECONDS) -> None:
    """Block until process ``pid`` has ended; return at once if it already has."""
    # Inline rather than a helper: mypy narrows on `sys.platform`, so the Linux CI run
    # never type-checks WinDLL, which only Windows' ctypes has.
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:  # already gone (or never existed): nothing to wait for
            return
        try:
            kernel32.WaitForSingleObject(handle, _INFINITE)
        finally:
            kernel32.CloseHandle(handle)
        return
    while _alive_posix(pid):
        time.sleep(poll)


def watch_process(pid: int, on_exit: Callable[[], None]) -> threading.Thread:
    """Call ``on_exit`` from a daemon thread once ``pid`` ends. Returns the started thread."""

    def run() -> None:
        wait_for_exit(pid)
        on_exit()

    thread = threading.Thread(target=run, name=f"watch-pid-{pid}", daemon=True)
    thread.start()
    return thread
