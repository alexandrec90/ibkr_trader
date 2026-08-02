#!/usr/bin/env python3
"""Reclaim the maximum possible disk from Docker, then return it to Windows.

Default flow (aggressive — stops Docker briefly, then restores it):

  1. `docker compose down`            stop the stack (detaches volumes; see safety rule below)
  2. `docker system prune -af`        remove stopped containers, unused networks, ALL unused
     `docker builder prune -af`       images, and ALL build cache
  3. purge pip download cache + stale pip temp dirs
  4. quit Docker Desktop, `wsl --shutdown`, diskpart-compact the WSL2 VHDX so the freed space
     is returned to Windows (not just freed inside the virtual disk)
  5. restart Docker Desktop and `docker compose up -d db` so the dev DB is back when it finishes

HARD SAFETY RULE (CLAUDE.md: Postgres is the single source of truth): this NEVER runs
`docker volume prune` or passes `--volumes`. Step 1's `compose down` detaches the named
volume `ibkr_trader_pgdata`, which is exactly why `--volumes` would then delete it — so we
never use it, and we assert the volume still exists after pruning. All ingested bars/
fundamentals live in that volume and survive every run (the container + `postgres:16` image
are recreated/re-pulled by step 5).

Windows/WSL2 only for the compaction step. `diskpart compact vdisk` needs an ELEVATED shell;
if this runs unelevated, quitting Docker Desktop still triggers its own reclaim, but for the
guaranteed full compaction run it from an Administrator terminal.

Usage:
    python scripts/docker-prune.py                 # full aggressive reclaim + restore
    python scripts/docker-prune.py --no-compact    # prune only; skip VHDX compaction/restart
    python scripts/docker-prune.py --no-restart     # compact but leave Docker stopped
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: Named volumes that must survive every prune, no matter what. We never prune volumes at all;
#: this is asserted post-prune so any regression fails loudly instead of silently losing data.
PROTECTED_VOLUMES = ("ibkr_trader_pgdata",)


def run(argv: list[str]) -> tuple[int, str]:
    """Run a command, capturing combined output. Returns (exit_code, text)."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def free_gb() -> float:
    return shutil.disk_usage("C:\\" if platform.system() == "Windows" else "/").free / 1e9


def step(label: str, argv: list[str]) -> int:
    print(f"\n{label}\n  $ {' '.join(argv)}")
    code, out = run(argv)
    for line in out.splitlines()[-8:]:
        print(f"  {line}")
    if code != 0:
        print(f"  [WARN] exited {code} (continuing)")
    return code


def assert_volumes_survived() -> None:
    _, out = run(["docker", "volume", "ls", "--format", "{{.Name}}"])
    missing = [v for v in PROTECTED_VOLUMES if v not in set(out.splitlines())]
    if missing:
        print(f"\n[FATAL] protected volume(s) missing after prune: {missing}")
        sys.exit(2)
    print(f"  protected volumes intact: {', '.join(PROTECTED_VOLUMES)}")


def prune() -> None:
    print("=== Prune (max reclaim; volumes always preserved) ===")
    step("Stopping the stack", ["docker", "compose", "down"])
    # No --volumes anywhere: stopped containers, unused networks, ALL unused images, ALL cache.
    step("Pruning containers/networks/images", ["docker", "system", "prune", "-af"])
    step("Pruning all build cache", ["docker", "builder", "prune", "-af"])
    assert_volumes_survived()


def pip_reclaim() -> None:
    print("\n=== Host-side pip reclaim ===")
    venv_py = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    step("Purging pip download cache", [py, "-m", "pip", "cache", "purge"])
    tmp = Path(tempfile.gettempdir())
    removed = 0
    for pattern in ("pip-unpack-*", "pip-uninstall-*", "pip-metadata-*", "pip-ephem-wheel-cache-*"):
        for p in tmp.glob(pattern):
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    print(f"  removed {removed} stale pip temp dir(s)")


def _find_vhdx() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "wsl"
    for candidate in (base / "disk" / "docker_data.vhdx", base / "data" / "ext4.vhdx"):
        if candidate.exists():
            return candidate
    matches = list(base.rglob("*.vhdx")) if base.exists() else []
    return matches[0] if matches else None


def _docker_cli() -> Path:
    return Path(os.environ.get("ProgramFiles", "")) / "Docker" / "Docker" / "DockerCli.exe"


def _stop_docker_desktop() -> None:
    # DockerCli.exe has no portable "stop" flag across versions (older builds print usage and
    # exit non-zero), so kill the GUI + backend processes directly. This must happen BEFORE
    # `wsl --shutdown`, or Docker Desktop immediately re-mounts and re-locks the VHDX — the
    # reason a naive shutdown-then-compact fails with "file in use".
    for image in ("Docker Desktop.exe", "com.docker.backend.exe", "com.docker.build.exe"):
        step(f"Quitting {image}", ["taskkill", "/IM", image, "/F"])


def compact_vhdx() -> None:
    vhdx = _find_vhdx()
    if vhdx is None:
        print("\n[skip] no Docker WSL VHDX found.")
        return
    print(f"\n=== Compact VHDX -> return space to Windows ===\n  target: {vhdx}")
    _stop_docker_desktop()
    step("Shutting down WSL", ["wsl", "--shutdown"])
    time.sleep(5)  # let the backend + WSL VM fully release the disk before diskpart attaches it

    # diskpart is present on every Windows edition (Optimize-VHD needs the Hyper-V module).
    script = f'select vdisk file="{vhdx}"\nattach vdisk readonly\ncompact vdisk\ndetach vdisk\n'
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(script)
        script_path = fh.name
    try:
        code = step("Compacting VHDX (diskpart)", ["diskpart", "/s", script_path])
        if code != 0:
            print(
                "  [WARN] diskpart failed — run this from an Administrator terminal for the "
                "guaranteed compaction (Docker Desktop's own shutdown reclaim still applied)."
            )
    finally:
        os.unlink(script_path)


def restart_docker_and_db() -> None:
    print("\n=== Restart Docker + bring DB back up ===")
    cli = _docker_cli()
    if cli.exists():
        run([str(cli), "-SwitchLinuxEngine"])  # ensures the app is launched
    dd = Path(os.environ.get("ProgramFiles", "")) / "Docker" / "Docker" / "Docker Desktop.exe"
    if dd.exists():
        subprocess.Popen([str(dd)], close_fds=True)
    print("  waiting for the Docker daemon...")
    for _ in range(60):
        if run(["docker", "info"])[0] == 0:
            print("  daemon up.")
            break
        time.sleep(3)
    else:
        print(
            "  [WARN] daemon didn't come up in time; start Docker Desktop and run "
            "`docker compose up -d db` yourself."
        )
        return
    repo = Path(__file__).resolve().parents[1]
    step(
        "Starting the dev DB",
        ["docker", "compose", "-f", str(repo / "docker-compose.yml"), "up", "-d", "db"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Max-reclaim Docker disk cleanup (volume-safe).")
    parser.add_argument(
        "--no-compact", action="store_true", help="Prune only; skip VHDX compaction."
    )
    parser.add_argument(
        "--no-restart", action="store_true", help="Compact but leave Docker stopped."
    )
    args = parser.parse_args()

    before = free_gb()
    print(f"Free space before: {before:.2f} GB")

    prune()
    pip_reclaim()

    if not args.no_compact and platform.system() == "Windows":
        compact_vhdx()
        if not args.no_restart:
            restart_docker_and_db()
    elif not args.no_compact:
        print("\n[skip] VHDX compaction is Windows/WSL2-only.")

    after = free_gb()
    print(f"\nFree space after: {after:.2f} GB  (reclaimed to Windows: {after - before:+.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
