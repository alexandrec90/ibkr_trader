"""Tests for the per-job health registry and its artifact.

The behaviour under test is the one whose absence hid a six-day outage: a failing job has to
leave a durable, parseable trace, and a job that has stopped producing has to be
distinguishable from one that is merely between runs.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from ibkr_trader import job_health


@pytest.fixture(autouse=True)
def _clean_registry():
    job_health.reset()
    yield
    job_health.reset()


def test_record_success_captures_result_and_clears_the_failure_streak():
    job_health.record_failure("prices", RuntimeError("boom"))
    job_health.record_success("prices", 809)

    entry = job_health.snapshot()["jobs"]["prices"]
    assert entry["consecutive_failures"] == 0
    assert entry["last_result"] == "809"
    assert entry["last_success"] is not None
    assert entry["last_error"] is None  # a success must not leave a stale error behind
    assert entry["runs"] == 2
    assert entry["failures"] == 1  # the historical count is kept


def test_record_failure_counts_the_streak_and_keeps_the_traceback():
    try:
        raise ValueError("no key")
    except ValueError as exc:
        first = job_health.record_failure("newsapi", exc)
    second = job_health.record_failure("newsapi", ValueError("still no key"))

    assert (first, second) == (1, 2)
    entry = job_health.snapshot()["jobs"]["newsapi"]
    assert entry["last_error"] == "ValueError: still no key"
    assert "ValueError" in entry["last_traceback"]


def test_record_failure_returns_attempt_number_for_backoff():
    """`_guard` indexes its retry delays with this, so the first failure must be 1, not 0."""
    assert job_health.record_failure("x", RuntimeError()) == 1


# --- "succeeded" is not "ingested" ------------------------------------------------------
# Finnhub answered 200 OK for two weeks while storing zero new articles. last_success cannot
# tell that apart from a healthy poll; last_wrote can.


def test_a_successful_run_that_wrote_rows_sets_last_wrote():
    job_health.record_success("finnhub_news", 8578)
    entry = job_health.snapshot()["jobs"]["finnhub_news"]
    assert entry["last_wrote"] == entry["last_success"]


def test_a_successful_run_that_wrote_nothing_leaves_last_wrote_alone():
    job_health.record_success("finnhub_news", 12)
    wrote_at = job_health.snapshot()["jobs"]["finnhub_news"]["last_wrote"]

    job_health.record_success("finnhub_news", 0)

    entry = job_health.snapshot()["jobs"]["finnhub_news"]
    assert entry["last_wrote"] == wrote_at  # unchanged — the empty poll ingested nothing
    assert entry["runs"] == 2  # but the run itself counted, and succeeded
    assert entry["consecutive_failures"] == 0
    assert entry["last_result"] == "0"


def test_dict_returning_jobs_count_their_rows():
    """Scoring and pruning report ``{table: count}`` rather than an int."""
    job_health.record_success("sentiment", {"news_articles": 0, "social_posts": 0})
    assert job_health.snapshot()["jobs"]["sentiment"]["last_wrote"] is None

    job_health.record_success("sentiment", {"news_articles": 82528, "social_posts": 0})
    assert job_health.snapshot()["jobs"]["sentiment"]["last_wrote"] is not None


def test_a_job_returning_nothing_at_all_is_not_counted_as_a_write():
    job_health.record_success("prune", None)
    assert job_health.snapshot()["jobs"]["prune"]["last_wrote"] is None


def test_a_boolean_result_is_not_a_row_count():
    job_health.record_success("something", True)
    assert job_health.snapshot()["jobs"]["something"]["last_wrote"] is None


def test_write_artifact_round_trips(tmp_path):
    job_health.record_schedule("prices", 86400)
    job_health.record_success("prices", 12)
    target = tmp_path / "nested" / "health.json"

    assert job_health.write_artifact(target) is True

    loaded = job_health.load_artifact(target)
    assert loaded["jobs"]["prices"]["last_result"] == "12"
    assert loaded["jobs"]["prices"]["interval_seconds"] == 86400
    assert "written_at" in loaded


def test_write_artifact_creates_the_logs_directory(tmp_path):
    target = tmp_path / "logs" / "scheduler-health.json"
    job_health.record_success("prune")

    assert job_health.write_artifact(target) is True
    assert target.exists()


def test_write_artifact_is_valid_json_for_a_parser(tmp_path):
    job_health.record_failure("reddit", RuntimeError("REDDIT_CLIENT_ID/SECRET not set"))
    target = tmp_path / "health.json"
    job_health.write_artifact(target)

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["jobs"]["reddit"]["last_error"].endswith("REDDIT_CLIENT_ID/SECRET not set")


def test_write_artifact_survives_an_unwritable_path(tmp_path):
    """Ingestion must never die because the health file could not be written."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    job_health.record_success("prices")

    assert job_health.write_artifact(blocker / "health.json") is False


def test_write_artifact_leaves_no_temp_files_behind(tmp_path):
    target = tmp_path / "health.json"
    job_health.record_success("prices")
    job_health.write_artifact(target)
    job_health.write_artifact(target)

    assert [p.name for p in tmp_path.iterdir()] == ["health.json"]


def test_write_artifact_treats_an_empty_path_as_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job_health.record_success("prices")

    assert job_health.write_artifact("") is False
    assert list(tmp_path.iterdir()) == []  # no temp-file litter from a failed replace


# --- surviving a restart ----------------------------------------------------------------


def test_seed_from_artifact_restores_history_across_a_restart(tmp_path):
    job_health.record_schedule("prices", 86400)
    job_health.record_success("prices", 6207)
    artifact = tmp_path / "health.json"
    job_health.write_artifact(artifact)
    before = job_health.snapshot()["jobs"]["prices"]

    job_health.reset()  # the restart
    assert job_health.seed_from_artifact(artifact) is True

    after = job_health.snapshot()["jobs"]["prices"]
    assert after["last_success"] == before["last_success"]
    assert after["last_wrote"] == before["last_wrote"]
    assert after["runs"] == 1


def test_seed_from_artifact_keeps_an_open_failure_streak(tmp_path):
    """Restarting does not repair whatever was failing, so the streak must survive."""
    job_health.record_failure("prices", RuntimeError("db down"))
    job_health.record_failure("prices", RuntimeError("db down"))
    artifact = tmp_path / "health.json"
    job_health.write_artifact(artifact)

    job_health.reset()
    job_health.seed_from_artifact(artifact)

    assert job_health.snapshot()["jobs"]["prices"]["consecutive_failures"] == 2


def test_seed_from_artifact_is_a_noop_without_a_previous_artifact(tmp_path):
    assert job_health.seed_from_artifact(tmp_path / "absent.json") is False
    assert job_health.snapshot()["jobs"] == {}


def test_seed_from_artifact_ignores_unknown_keys(tmp_path):
    artifact = tmp_path / "health.json"
    artifact.write_text(
        json.dumps({"jobs": {"prices": {"runs": 3, "bogus_field": "x"}}}), encoding="utf-8"
    )

    assert job_health.seed_from_artifact(artifact) is True

    entry = job_health.snapshot()["jobs"]["prices"]
    assert entry["runs"] == 3
    assert "bogus_field" not in entry


def test_seed_from_artifact_survives_a_malformed_artifact(tmp_path):
    artifact = tmp_path / "health.json"
    artifact.write_text("{not json", encoding="utf-8")

    assert job_health.seed_from_artifact(artifact) is False


def test_seed_from_artifact_survives_a_jobs_field_of_the_wrong_shape(tmp_path):
    artifact = tmp_path / "health.json"
    artifact.write_text(json.dumps({"jobs": ["prices"]}), encoding="utf-8")

    assert job_health.seed_from_artifact(artifact) is False


def test_load_artifact_rejects_a_non_object(tmp_path):
    target = tmp_path / "health.json"
    target.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError):
        job_health.load_artifact(target)


def test_status_for_reports_never_run_before_a_first_success():
    job_health.record_schedule("newsapi", 3600)
    assert job_health.status_for(job_health.snapshot()["jobs"]["newsapi"]) == "never-run"


def test_status_for_reports_failing_while_the_streak_is_open():
    job_health.record_success("reddit")
    job_health.record_failure("reddit", RuntimeError("no credentials"))
    assert job_health.status_for(job_health.snapshot()["jobs"]["reddit"]) == "failing"


def test_status_for_reports_stale_when_a_success_is_older_than_its_cadence():
    """The exact shape of the outage: last write 6 days ago on a daily job, no open failure."""
    entry = {
        "consecutive_failures": 0,
        "last_success": (datetime.now(UTC) - timedelta(days=6)).isoformat(),
        "interval_seconds": 86400,
    }
    assert job_health.status_for(entry) == "stale"


def test_status_for_tolerates_one_missed_run():
    entry = {
        "consecutive_failures": 0,
        "last_success": (datetime.now(UTC) - timedelta(hours=30)).isoformat(),
        "interval_seconds": 86400,
    }
    assert job_health.status_for(entry) == "ok"


def test_status_for_prefers_failing_over_stale():
    entry = {
        "consecutive_failures": 3,
        "last_success": (datetime.now(UTC) - timedelta(days=9)).isoformat(),
        "interval_seconds": 86400,
    }
    assert job_health.status_for(entry) == "failing"


def test_status_for_without_a_known_cadence_cannot_be_stale():
    entry = {
        "consecutive_failures": 0,
        "last_success": (datetime.now(UTC) - timedelta(days=99)).isoformat(),
        "interval_seconds": None,
    }
    assert job_health.status_for(entry) == "ok"
