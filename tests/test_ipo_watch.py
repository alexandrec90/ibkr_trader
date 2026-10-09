import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ibkr_trader.db.models import IpoEvent
from ibkr_trader.ipo_watch import (
    ALERT_TAGS,
    NO_TOPIC,
    AlertPolicy,
    IpoEventView,
    alert_job,
    alert_key,
    company_key,
    format_alert,
    is_current,
    load_events,
    load_ledger,
    major_reason,
    name_tokens,
    run_ipo_alerts,
    save_ledger,
    watchlist_match,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
WATCHLIST = ["Anthropic", "OpenAI", "SpaceX", "Space Exploration Technologies", "Discord"]


def _filing(name="Anthropic, PBC", stage="filed", **kw):
    fields = {
        "source": "sec_edgar",
        "external_id": "0001234567-26-000001",
        "company_name": name,
        "stage": stage,
        "form_type": "S-1",
        "filed_at": NOW - timedelta(days=1),
        "url": "https://www.sec.gov/Archives/edgar/data/1/x-index.htm",
    }
    fields.update(kw)
    return IpoEventView(**fields)


def _deal(name="Discord Inc", stage="expected", **kw):
    fields = {
        "source": "finnhub",
        "external_id": "abc",
        "company_name": name,
        "stage": stage,
        "symbol": "DCRD",
        "exchange": "NYSE",
        "expected_date": date(2026, 10, 20),
        "price_low": 30.0,
        "price_high": 34.0,
        "shares": 50_000_000,
        "deal_value_usd": 1.7e9,
    }
    fields.update(kw)
    return IpoEventView(**fields)


class _Notifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, note):
        self.calls.append((note.title, note.message, note.click))
        assert note.tags == ALERT_TAGS and note.priority == "default"
        return self.ok


# --- matching ------------------------------------------------------------------------------


def test_name_tokens_splits_on_punctuation():
    assert name_tokens("Space-X Corp.") == ["space", "x", "corp"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Anthropic, PBC", "Anthropic"),
        ("OpenAI Group PBC", "OpenAI"),
        ("SPACEX", "SpaceX"),
        ("Space Exploration Technologies Corp", "Space Exploration Technologies"),
        ("Discord Inc.", "Discord"),
    ],
)
def test_watchlist_matches_whole_words(name, expected):
    assert watchlist_match(name, WATCHLIST) == expected


@pytest.mark.parametrize("name", ["Anthropos Capital Corp", "Discordia Mining Ltd", "Openaire"])
def test_watchlist_does_not_match_a_prefix(name):
    assert watchlist_match(name, WATCHLIST) is None


def test_watchlist_entry_written_apart_matches_a_name_run_together():
    assert watchlist_match("OpenAI Group PBC", ["Open AI"]) == "Open AI"
    assert watchlist_match("Space X Holdings", ["Space-X"]) == "Space-X"


def test_blank_watchlist_entry_matches_nothing():
    assert watchlist_match("Anything Corp", ["", "  ", "--"]) is None


def test_major_by_deal_size_when_not_on_the_watchlist():
    event = _deal(name="Unknown Robotics", deal_value_usd=2.5e9)
    assert major_reason(event, WATCHLIST, 1e9) == "deal size $2.5B"


def test_small_deal_off_the_watchlist_is_not_major():
    assert major_reason(_deal(name="Tiny Co", deal_value_usd=5e7), WATCHLIST, 1e9) is None


def test_deal_size_threshold_zero_disables_size_rule():
    assert major_reason(_deal(name="Unknown", deal_value_usd=9e9), WATCHLIST, 0) is None


# --- currency ------------------------------------------------------------------------------


def test_recent_filing_is_current_old_one_is_not():
    assert is_current(_filing(filed_at=NOW - timedelta(days=3)), NOW)
    assert not is_current(_filing(filed_at=NOW - timedelta(days=30)), NOW)


def test_naive_filed_at_is_read_as_utc():
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
    assert is_current(_filing(filed_at=naive), NOW)


def test_expected_date_decides_over_filed_at():
    past = _deal(expected_date=date(2026, 10, 1))
    assert not is_current(past, NOW)
    assert is_current(_deal(expected_date=date(2026, 10, 7)), NOW)  # one-day grace


def test_event_with_no_date_is_not_current():
    assert not is_current(_filing(filed_at=None), NOW)


# --- formatting ----------------------------------------------------------------------------


def test_alert_key_changes_when_a_deal_is_redated():
    assert alert_key(_deal()) != alert_key(_deal(expected_date=date(2026, 10, 27)))
    assert alert_key(_deal()) == alert_key(_deal(price_low=31.0))


def test_format_calendar_deal():
    title, body = format_alert(_deal(), "watchlist: Discord")
    assert title == "Discord Inc: IPO scheduled"
    assert "expected 2026-10-20" in body
    assert "on NYSE as DCRD" in body
    assert "$30.00-$34.00" in body
    assert "50,000,000 shares" in body
    assert "~$1.70B" in body
    assert "watchlist: Discord" in body


def test_format_filing_with_no_deal_details():
    title, body = format_alert(_filing(), "watchlist: Anthropic")
    assert title == "Anthropic, PBC: filed publicly"
    assert "Form S-1" in body and "filed 2026-10-07" in body


def test_format_single_price_and_bare_event():
    _, body = format_alert(_deal(price_low=None, price_high=21.0, exchange=None), "r")
    assert "$21.00" in body and "as DCRD" in body
    bare = IpoEventView(source="s", external_id="e", company_name="X", stage="odd")
    assert format_alert(bare, "r") == ("X: odd", "no deal details yet. (r; source: s)")


# --- ledger --------------------------------------------------------------------------------


def test_ledger_round_trip_prunes_old_entries(tmp_path):
    path = tmp_path / "logs" / "ipo-alerts.json"
    old = (NOW - timedelta(days=400)).isoformat()
    save_ledger(path, {"keep": NOW.isoformat(), "old": old, "bad": "not-a-date"}, NOW)
    assert load_ledger(path) == {"keep": NOW.isoformat()}


def test_missing_or_corrupt_ledger_is_empty(tmp_path):
    assert load_ledger(tmp_path / "absent.json") == {}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert load_ledger(corrupt) == {}
    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2]", encoding="utf-8")
    assert load_ledger(wrong_shape) == {}


# --- run_ipo_alerts ------------------------------------------------------------------------


def _run(events, tmp_path, notifier, **policy):
    return run_ipo_alerts(
        events,
        notify=notifier,
        policy=AlertPolicy(**{"watchlist": WATCHLIST, "min_deal_usd": 1e9, **policy}),
        ledger_path=tmp_path / "ipo-alerts.json",
        now=NOW,
    )


def test_alert_policy_from_settings():
    settings = SimpleNamespace(
        ipo_watchlist=["A", "B"], ipo_alert_min_deal_usd=5.0, ipo_alert_stages=["priced"]
    )
    assert AlertPolicy.from_settings(settings) == AlertPolicy(("A", "B"), 5.0, ("priced",))


def test_alerts_once_per_stage_and_passes_the_click_url(tmp_path):
    notifier = _Notifier()
    events = [_filing(), _deal()]
    assert _run(events, tmp_path, notifier) == 2
    assert notifier.calls[0][2].startswith("https://www.sec.gov/")
    assert notifier.calls[1][2] == ""
    assert _run(events, tmp_path, notifier) == 0  # the ledger remembers
    assert len(notifier.calls) == 2


def test_stage_change_alerts_again(tmp_path):
    notifier = _Notifier()
    _run([_deal()], tmp_path, notifier)
    assert _run([_deal(stage="priced")], tmp_path, notifier) == 1


def test_skips_minor_stale_and_amended_events(tmp_path):
    notifier = _Notifier()
    events = [
        _filing(name="Small Bank Corp"),
        _filing(filed_at=NOW - timedelta(days=60)),
        _filing(stage="amended", form_type="S-1/A"),
    ]
    assert _run(events, tmp_path, notifier) == 0
    assert notifier.calls == []


def test_amendments_alert_when_the_stage_is_enabled(tmp_path):
    notifier = _Notifier()
    event = _filing(stage="amended", form_type="S-1/A")
    assert _run([event], tmp_path, notifier, stages=("amended",)) == 1


def test_failed_send_is_retried_next_run(tmp_path):
    assert _run([_filing()], tmp_path, _Notifier(ok=False)) == 0
    assert json.loads((tmp_path / "ipo-alerts.json").read_text())["sent"] == {}
    assert _run([_filing()], tmp_path, _Notifier()) == 1


def test_one_alert_for_the_same_filing_on_two_sources_and_a_drs_beside_its_s1(tmp_path):
    notifier = _Notifier()
    events = [
        _filing(name="Anthropic, PBC", form_type="DRS", external_id="a1"),
        _filing(name="Anthropic, PBC", form_type="S-1", external_id="a2"),
        _filing(name="Anthropic PBC", source="finnhub", external_id="h", form_type=None),
    ]
    assert _run(events, tmp_path, notifier) == 1


def test_company_key_drops_legal_suffixes_but_never_the_whole_name():
    assert company_key("OpenAI Group PBC") == company_key("OpenAI") == "openai"
    assert company_key("Anthropic, PBC") == "anthropic"
    assert company_key("Holdings") == "holdings"


def test_a_drs_made_public_alerts_on_first_seen_not_its_old_filing_date(tmp_path):
    drs = _filing(
        form_type="DRS",
        filed_at=NOW - timedelta(days=120),  # the confidential submission date
        first_seen_at=NOW - timedelta(hours=2),  # the day it became public
    )
    assert is_current(drs, NOW)
    assert not is_current(_filing(filed_at=NOW, first_seen_at=NOW - timedelta(days=30)), NOW)
    assert _run([drs], tmp_path, _Notifier()) == 1


# --- the database side ---------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://")
    IpoEvent.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def session_scope():
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return session_scope


def _row(**kw):
    fields = {
        "source": "sec_edgar",
        "external_id": "acc-1",
        "company_name": "Anthropic, PBC",
        "stage": "filed",
        "form_type": "S-1",
        "filed_at": NOW - timedelta(days=1),
        "first_seen_at": NOW - timedelta(hours=1),
        "fetched_at": NOW,
        "url": "https://www.sec.gov/x-index.htm",
        "symbol": "",
    }
    fields.update(kw)
    return IpoEvent(**fields)


def test_load_events_bounds_the_read_and_maps_the_columns(session_factory):
    with session_factory() as session:
        session.add_all(
            [
                _row(),
                _row(
                    external_id="old",
                    first_seen_at=NOW - timedelta(days=90),
                    filed_at=NOW - timedelta(days=90),
                ),
                _row(external_id="amend", stage="amended"),
                _row(
                    source="finnhub",
                    external_id="deal",
                    stage="expected",
                    filed_at=None,
                    first_seen_at=NOW - timedelta(days=60),
                    expected_date=date(2026, 10, 20),
                    exchange="NASDAQ",
                    symbol="ANTH",
                    shares=10,
                    price_low=1.0,
                ),
            ]
        )
    with session_factory() as session:
        events = load_events(session, ["filed", "expected"], NOW)

    assert [e.external_id for e in events] == ["deal", "acc-1"]
    deal, filing = events
    assert deal.exchange == "NASDAQ" and deal.symbol == "ANTH" and deal.shares == 10
    assert filing.symbol is None  # an empty ticker reads as absent
    assert filing.first_seen_at is not None and filing.url.endswith("-index.htm")


def _job_settings(tmp_path, **kw):
    fields = {
        "ntfy_server": "https://ntfy.example",
        "ntfy_topic": "t",
        "ipo_alert_stages": ["filed", "expected", "priced", "withdrawn"],
        "ipo_watchlist": ["Anthropic"],
        "ipo_alert_min_deal_usd": 1e9,
        "ipo_alert_ledger_file": str(tmp_path / "ipo-alerts.json"),
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_alert_job_pushes_a_watched_filing_once(tmp_path, monkeypatch, session_factory):
    from ibkr_trader import gateway_watch

    posts = []
    monkeypatch.setattr(gateway_watch, "post_ntfy", lambda *args: posts.append(args) or True)
    with session_factory() as session:
        session.add(_row(first_seen_at=datetime.now(UTC)))
    settings = _job_settings(tmp_path)

    assert alert_job(settings, session_factory) == {"candidates": 1, "alerts_sent": 1}
    assert alert_job(settings, session_factory) == {"candidates": 1, "alerts_sent": 0}
    server, topic, note = posts[0]
    assert (server, topic) == ("https://ntfy.example", "t")
    assert note.title == "Anthropic, PBC: filed publicly"
    assert note.click == "https://www.sec.gov/x-index.htm"
    assert len(posts) == 1


def test_alert_job_without_a_topic_is_a_recorded_skip(tmp_path, session_factory):
    def no_db():
        raise AssertionError("must not read the database")

    assert alert_job(_job_settings(tmp_path, ntfy_topic=""), no_db) == NO_TOPIC
