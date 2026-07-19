"""Launch the TigerVNC viewer against the ib-gateway container without a password prompt.

The gateway container already holds VNC_SERVER_PASSWORD in its environment, so it generates
the obfuscated VNC auth file itself (x11vnc -storepasswd) and we copy that out; the plaintext
password is never read on the host. If any of that fails (container down, no password set),
fall back to launching the viewer normally, which prompts like before.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VNC_HOST = "127.0.0.1::5900"
CONTAINER_AUTH = "/tmp/.task-vncauth"
COMPOSE = ["docker", "compose", "--profile", "ibkr"]


def fetch_auth_file() -> Path | None:
    """Have the container write its VNC auth file and copy it to a host temp file."""
    store = (
        '[ -n "$VNC_SERVER_PASSWORD" ] && '
        f'x11vnc -storepasswd "$VNC_SERVER_PASSWORD" {CONTAINER_AUTH} >/dev/null 2>&1'
    )
    fd, name = tempfile.mkstemp(prefix="ibgw-vncauth-")
    os.close(fd)
    host_auth = Path(name)
    try:
        subprocess.run(
            [*COMPOSE, "exec", "-T", "ib-gateway", "sh", "-c", store],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [*COMPOSE, "cp", f"ib-gateway:{CONTAINER_AUTH}", str(host_auth)],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [*COMPOSE, "exec", "-T", "ib-gateway", "rm", "-f", CONTAINER_AUTH],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            timeout=30,
        )
        if host_auth.stat().st_size == 0:
            raise RuntimeError("container produced an empty auth file")
    except Exception as exc:  # noqa: BLE001 - any failure just means the viewer prompts
        host_auth.unlink(missing_ok=True)
        print(f"VNC auth file unavailable ({exc}); the viewer will prompt.", file=sys.stderr)
        return None
    return host_auth


def main() -> int:
    viewer = sys.argv[1] if len(sys.argv) > 1 else r"C:\Program Files\TigerVNC\vncviewer.exe"
    auth = fetch_auth_file()
    cmd = [viewer, "-SecurityTypes", "VncAuth"]
    if auth is not None:
        cmd += ["-PasswordFile", str(auth)]
    cmd.append(VNC_HOST)
    try:
        return subprocess.run(cmd, check=False).returncode
    finally:
        if auth is not None:
            auth.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
