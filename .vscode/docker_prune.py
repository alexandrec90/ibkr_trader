#!/usr/bin/env python3
"""Reclaim disk from Docker — aggressively, but never at the cost of the database.

Two phases:

  Phase A/B (default, non-disruptive): prune unused images/build-cache/stopped containers
  and purge the pip download cache + stale temp dirs. Leaves running containers alone, so the
  dev DB (and its `postgres:16` image) stay up and are NOT re-pulled next time.

  Phase C (`--compact`, disruptive, Windows/WSL2 only): the space freed above lives *inside*
  Docker Desktop's WSL2 VHDX and is not returned to Windows until the VHDX is compacted. This
  quits Docker Desktop (so it can't re-mount and re-lock the disk mid-compact — the usual
  reason naive `wsl --shutdown` compaction fails), runs `wsl --shutdown`, and compacts the
  VHDX with diskpart. It stops Docker, so run it when you're done for the session, then
  `docker compose up -d db` to resume.

HARD SAFETY RULE (CLAUDE.md: Postgres is the single source of truth): this script NEVER runs
`docker volume prune` or passes `--volumes`. Named volumes — including `ibkr_trader_pgdata`,
which holds every ingested bar/fundamental — are always preserved. `image prune -a` only
removes images not backing any container, so keeping the DB up protects `postgres:16`.

Usage:
    python .vscode/docker_prune.py            # safe reclaim (DB stays up)
    python .vscode/docker_prune.py --compact  # also return space to Windows (stops Docker)
    python .vscode/docker_prune.py --compact --yes   # no confirmation prompt
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: Named volumes that must survive every prune, no matter what. Belt-and-suspenders: we never
#: prune volumes at all, but this is asserted post-prune so a regression fails loudly.
PROTECTED_VOLUMES = ("ibkr_trader_pgdata",)


def run(argv: list[str], *, check: bool = False) -> tuple[int, str]:
    """Run a command, capturing combined output. Returns (exit_code, text)."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {proc.returncode}\n{out}")
    return proc.returncode, out.strip()


def free_gb(path: str = "C:\\") -> float:
    if platform.system() != "Windows":
        path = "/"
    return shutil.disk_usage(path).free / 1e9


def step(label: str, argv: list[str]) -> None:
    print(f"\n{label}\n  $ {' '.join(argv)}")
    code, out = run(argv)
    tail = "\n".join(f"  {line}" for line in out.splitlines()[-6:])
    if tail:
        print(tail)
    if code != 0:
        print(f"  [WARN] exited {code} (continuing)")


def assert_volumes_survived() -> None:
    _, out = run(["docker", "volume", "ls", "--format", "{{.Name}}"])
    present = set(out.splitlines())
    missing = [v for v in PROTECTED_VOLUMES if v not in present]
    if missing:
        print(f"\n[FATAL] protected volume(s) missing after prune: {missing}")
        sys.exit(2)
    print(f"  protected volumes intact: {', '.join(PROTECTED_VOLUMES)}")


def phase_ab() -> None:
    print("=== Phase A: Docker prune (volume-safe; DB left running) ===")
    # No --volumes anywhere. system prune -f: dangling images, stopped containers, networks,
    # build cache. image prune -a: every image not backing a container (running DB protects
    # postgres:16). builder prune -a: all build cache.
    step("Pruning unused Docker objects (no volumes)", ["docker", "system", "prune", "-f"])
    step("Pruning all unused images", ["docker", "image", "prune", "-a", "-f"])
    step("Pruning all build cache", ["docker", "builder", "prune", "-a", "-f"])
    assert_volumes_survived()

    print("\n=== Phase B: host-side pip reclaim ===")
    # Prefer the repo venv's pip so its cache is the one purged.
    venv_py = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    step("Purging pip download cache", [py, "-m", "pip", "cache", "purge"])
    tmp = Path(tempfile.gettempdir())
    removed = 0
    for pattern in ("pip-unpack-*", "pip-uninstall-*", "pip-metadata-*", "pip-ephem-wheel-cache-*"):
        for p in tmp.glob(pattern):
            try:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
            except OSError:
                pass
    print(f"  removed {removed} stale pip temp dir(s)")


def _find_vhdx() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Docker" / "wsl"
    for candidate in (base / "disk" / "docker_data.vhdx", base / "data" / "ext4.vhdx"):
        if candidate.exists():
            return candidate
    matches = list(base.rglob("*.vhdx")) if base.exists() else []
    return matches[0] if matches else None


def _stop_docker_desktop() -> None:
    cli = Path(os.environ.get("ProgramFiles", "")) / "Docker" / "Docker" / "DockerCli.exe"
    if cli.exists():
        step("Quitting Docker Desktop (releases the VHDX)", [str(cli), "-Stop"])
    else:
        # Fall back to killing the app process so it can't re-mount the disk.
        step("Quitting Docker Desktop", ["taskkill", "/IM", "Docker Desktop.exe", "/F"])


def phase_c_compact() -> None:
    if platform.system() != "Windows":
        print("\n[skip] --compact is Windows/WSL2-only.")
        return
    vhdx = _find_vhdx()
    if vhdx is None:
        print("\n[skip] no Docker WSL VHDX found.")
        return

    print(f"\n=== Phase C: compact VHDX -> return space to Windows ===\n  target: {vhdx}")
    _stop_docker_desktop()
    step("Shutting down WSL", ["wsl", "--shutdown"])

    # diskpart is present on all Windows editions (Optimize-VHD needs the Hyper-V module).
    script = (
        f'select vdisk file="{vhdx}"\n'
        "attach vdisk readonly\n"
        "compact vdisk\n"
        "detach vdisk\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(script)
        script_path = fh.name
    try:
        code, out = run(["diskpart", "/s", script_path])
        for line in out.splitlines()[-10:]:
            print(f"  {line}")
        if code != 0:
            print(
                f"  [WARN] diskpart exited {code}. Compaction needs an *elevated* shell — "
                "re-run this from an Administrator terminal."
            )
    finally:
        os.unlink(script_path)
    print("\n  Docker Desktop is stopped. Restart it, then: docker compose up -d db")


def main() -> int:
    parser = argparse.ArgumentParser(description="Volume-safe Docker disk reclaim.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Also compact the WSL VHDX to return space to Windows (stops Docker; Windows only).",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the --compact confirmation.")
    args = parser.parse_args()

    before = free_gb()
    print(f"Free space before: {before:.2f} GB")

    phase_ab()

    if args.compact:
        if not args.yes:
            reply = input("\n--compact stops Docker (DB goes down). Continue? [y/N] ").strip().lower()
            if reply != "y":
                print("Skipping compaction.")
                args.compact = False
        if args.compact:
            phase_c_compact()

    after = free_gb()
    print(f"\nFree space after: {after:.2f} GB  (reclaimed to Windows: {after - before:+.2f} GB)")
    if not args.compact:
        print(
            "Note: Docker-side space was freed *inside* the WSL VHDX. Re-run with --compact "
            "to return it to Windows."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
