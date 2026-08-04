"""Per-job outcome tracking for `serve`, persisted to a parseable artifact.

``scheduler._guard`` deliberately swallows a job's exception so that one failing source cannot
kill the long-running process. The cost is that nothing outside the log stream knows a job has
stopped working: APScheduler still logs "executed successfully" for a run whose body raised. In
practice that hid a dead ingestion pipeline for six days — the database had been stopped, every
job was failing on connect, and the only evidence was a traceback buried between two lines
claiming success.

This module is the missing signal. Every guarded run records its outcome here, and the registry
is flushed to ``logs/scheduler-health.json`` after each one, per the failure-artifact rule in
``.agents/rules/engineering.md``: a parseable file, overwritten in place, carrying everything
needed to diagnose. ``ibkr-trader health`` reads it and is the answer to "is ingestion actually
pulling data".

Recording must never be able to break ingestion, so a filesystem error while writing is logged
and dropped; the in-memory registry stays correct regardless of whether the artifact lands.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default artifact location, relative to the working directory (``/app`` in the container).
#: ``logs/`` is gitignored and created on demand.
DEFAULT_ARTIFACT = "logs/scheduler-health.json"

#: A job is "stale" once it has gone this many times its own interval without a success. Two
#: and a half intervals means a single missed run is not yet an alarm, but two are.
STALE_INTERVAL_FACTOR = 2.5

_lock = threading.Lock()
_registry: dict[str, dict[str, Any]] = {}


def _blank(job: str) -> dict[str, Any]:
    return {
        "job": job,
        "interval_seconds": None,
        "runs": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "last_run": None,
        "last_success": None,
        "last_failure": None,
        # Distinct from last_success on purpose: a poll that returns 0 has *succeeded* but has
        # not ingested anything. Finnhub answered 200 OK for two weeks while storing zero new
        # articles — by every other measure that job was healthy.
        "last_wrote": None,
        "last_result": None,
        "last_error": None,
        "last_traceback": None,
    }


def _rows_written(result: object) -> int:
    """How many rows a job's return value claims to have written (0 when it says nothing).

    Polls return an int; the scoring and pruning jobs return a ``{table: count}`` dict.
    """
    if isinstance(result, bool):  # bool is an int subclass; a flag is not a row count
        return 0
    if isinstance(result, int | float):
        return int(result) if result > 0 else 0
    if isinstance(result, dict):
        total = 0
        for value in result.values():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            if value > 0:
                total += int(value)
        return total
    return 0


def _entry(job: str) -> dict[str, Any]:
    return _registry.setdefault(job, _blank(job))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_schedule(job: str, interval_seconds: float) -> None:
    """Declare a job's cadence, so staleness can be judged against what it promised."""
    with _lock:
        _entry(job)["interval_seconds"] = interval_seconds


def record_success(job: str, result: object = None) -> None:
    """Note a run that completed. ``result`` is stringified — it is a summary, not a payload."""
    with _lock:
        entry = _entry(job)
        stamp = _now()
        entry["runs"] += 1
        entry["consecutive_failures"] = 0
        entry["last_run"] = stamp
        entry["last_success"] = stamp
        entry["last_result"] = None if result is None else str(result)
        if _rows_written(result):
            entry["last_wrote"] = stamp
        entry["last_error"] = None
        entry["last_traceback"] = None


def record_failure(job: str, exc: BaseException) -> int:
    """Note a failed run and return how many times in a row this job has now failed."""
    with _lock:
        entry = _entry(job)
        stamp = _now()
        entry["runs"] += 1
        entry["failures"] += 1
        entry["consecutive_failures"] += 1
        entry["last_run"] = stamp
        entry["last_failure"] = stamp
        entry["last_error"] = f"{type(exc).__name__}: {exc}"
        entry["last_traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        return int(entry["consecutive_failures"])


def snapshot() -> dict[str, Any]:
    """The full registry, shaped as it is written to disk."""
    with _lock:
        return {
            "written_at": _now(),
            "jobs": {job: dict(entry) for job, entry in _registry.items()},
        }


def reset() -> None:
    """Drop all recorded state (tests, and a fresh `serve` process)."""
    with _lock:
        _registry.clear()


def write_artifact(path: str | os.PathLike[str] = DEFAULT_ARTIFACT) -> bool:
    """Write the registry to ``path`` atomically. Returns whether it landed.

    Never raises: a scheduler that cannot write its health file must still ingest. The
    temp-file-then-replace dance keeps a reader from ever seeing a half-written artifact.
    An empty path disables the artifact (the in-memory registry still records everything).
    """
    if not str(path):
        return False
    target = Path(path)
    payload = json.dumps(snapshot(), indent=2, sort_keys=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=target.name,
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        os.replace(temp_name, target)
    except OSError:
        logger.warning("could not write the scheduler health artifact to %s", target, exc_info=True)
        return False
    return True


def seed_from_artifact(path: str | os.PathLike[str] = DEFAULT_ARTIFACT) -> bool:
    """Restore a previous process's registry, so a restart does not erase job history.

    Without this, every `serve` restart resets all counters and the next health check reports
    "never-run" for every job until each has fired — which for a daily job means a full day of
    a red check that means nothing. A restart is not evidence that anything ingested, so the
    recorded facts (including an open failure streak) carry over unchanged.

    Unknown keys in the artifact are dropped rather than merged: an artifact written by an
    older build must not inject a shape the current code does not expect.
    """
    try:
        loaded = load_artifact(path)
    except (OSError, ValueError):
        return False
    jobs = loaded.get("jobs")
    if not isinstance(jobs, dict):
        return False
    with _lock:
        for name, stored in jobs.items():
            if not isinstance(stored, dict):
                continue
            entry = _blank(str(name))
            entry.update({key: value for key, value in stored.items() if key in entry})
            entry["job"] = str(name)
            _registry[str(name)] = entry
    return True


def load_artifact(path: str | os.PathLike[str] = DEFAULT_ARTIFACT) -> dict[str, Any]:
    """Read an artifact back. Raises ``OSError``/``ValueError`` for the caller to report."""
    with open(path, encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(loaded).__name__}")
    return loaded


def status_for(entry: dict[str, Any], *, now: datetime | None = None) -> str:
    """Classify one job's entry: ``ok``, ``failing``, ``stale`` or ``never-run``.

    ``failing`` outranks ``stale`` because a job that is erroring right now is the more
    actionable fact — staleness is usually its consequence.
    """
    if entry.get("consecutive_failures"):
        return "failing"
    last_success = entry.get("last_success")
    if not last_success:
        return "never-run"
    interval = entry.get("interval_seconds")
    if not interval:
        return "ok"
    moment = now or datetime.now(UTC)
    age = (moment - datetime.fromisoformat(last_success)).total_seconds()
    return "stale" if age > interval * STALE_INTERVAL_FACTOR else "ok"
