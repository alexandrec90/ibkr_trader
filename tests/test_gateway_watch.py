from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ibkr_trader import gateway_watch
from ibkr_trader.gateway_watch import GatewayDown, GatewayWatch, check_accounts, send_ntfy


class _Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


class _Gateway:
    """A probe whose answer the test sets: a list of accounts, or an exception to raise."""

    def __init__(self) -> None:
        self.answer: list[str] | BaseException = ["DU1234567"]

    def __call__(self) -> list[str]:
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _watch(**kwargs):
    gateway = _Gateway()
    sent: list[tuple[str, str]] = []
    clock = _Clock()
    watch = GatewayWatch(
        gateway,
        lambda title, message: sent.append((title, message)) or True,
        now=clock,
        **kwargs,
    )
    return watch, gateway, sent, clock


def _run(watch):
    """One scheduled run: the job re-raises GatewayDown so job_health records it."""
    try:
        return watch()
    except GatewayDown:
        return None


# --- GatewayWatch --------------------------------------------------------------------------


def test_a_logged_in_paper_gateway_is_healthy_and_silent():
    watch, _, sent, _ = _watch()
    assert watch() == "logged in (1 account(s))"
    assert sent == []


def test_a_failed_probe_raises_so_job_health_records_it():
    watch, gateway, _, _ = _watch()
    gateway.answer = GatewayDown("no API handshake")
    with pytest.raises(GatewayDown):
        watch()


def test_one_failure_does_not_alert_so_the_nightly_restart_stays_quiet():
    watch, gateway, sent, _ = _watch(alert_after=2)
    gateway.answer = GatewayDown("no API handshake")
    _run(watch)
    assert sent == []


def test_alerts_once_the_failures_in_a_row_reach_the_threshold():
    watch, gateway, sent, clock = _watch(alert_after=2)
    gateway.answer = GatewayDown("no API handshake with ib-gateway:4004: TimeoutError")
    _run(watch)
    clock.advance(minutes=5)
    _run(watch)
    assert len(sent) == 1
    title, message = sent[0]
    assert title == "IB Gateway needs you"
    assert "~5 min" in message
    assert "IBKR Mobile" in message
    assert "TimeoutError" in message


def test_a_success_between_failures_resets_the_count():
    watch, gateway, sent, _ = _watch(alert_after=2)
    gateway.answer = GatewayDown("down")
    _run(watch)
    gateway.answer = ["DU1"]
    _run(watch)
    gateway.answer = GatewayDown("down")
    _run(watch)
    assert sent == []


def test_does_not_repeat_the_alert_every_run_while_still_down():
    watch, gateway, sent, clock = _watch(alert_after=1, repeat_hours=2)
    gateway.answer = GatewayDown("down")
    for _ in range(10):
        _run(watch)
        clock.advance(minutes=5)
    assert len(sent) == 1


def test_repeats_the_alert_after_the_repeat_window():
    watch, gateway, sent, clock = _watch(alert_after=1, repeat_hours=2)
    gateway.answer = GatewayDown("down")
    _run(watch)
    clock.advance(hours=2)
    _run(watch)
    assert len(sent) == 2
    assert "~120 min" in sent[1][1]


def test_sends_one_recovery_notice_after_an_alert():
    watch, gateway, sent, _ = _watch(alert_after=1)
    gateway.answer = GatewayDown("down")
    _run(watch)
    gateway.answer = ["DU1"]
    _run(watch)
    _run(watch)
    assert [title for title, _ in sent] == ["IB Gateway needs you", "IB Gateway is back"]


def test_no_recovery_notice_when_no_alert_was_sent():
    watch, gateway, sent, _ = _watch(alert_after=2)
    gateway.answer = GatewayDown("down")
    _run(watch)
    gateway.answer = ["DU1"]
    _run(watch)
    assert sent == []


def test_alert_after_below_one_still_alerts_on_the_first_failure():
    watch, gateway, sent, _ = _watch(alert_after=0)
    gateway.answer = GatewayDown("down")
    _run(watch)
    assert len(sent) == 1


def test_a_live_account_while_paper_alerts_without_naming_the_account():
    watch, gateway, sent, _ = _watch(alert_after=1)
    gateway.answer = ["U7654321"]
    with pytest.raises(GatewayDown, match="NON-paper"):
        watch()
    assert len(sent) == 1
    assert "U7654321" not in sent[0][1]


def test_a_probe_error_that_is_not_gateway_down_propagates_untouched():
    """Only GatewayDown is the watch's business; anything else is a bug to surface as-is."""
    watch, gateway, sent, _ = _watch(alert_after=1)
    gateway.answer = ValueError("bug")
    with pytest.raises(ValueError):
        watch()
    assert sent == []


# --- check_accounts ------------------------------------------------------------------------


def test_check_accounts_accepts_paper_accounts():
    check_accounts(["DU1", "du2"], "paper")


def test_check_accounts_refuses_an_empty_account_list():
    with pytest.raises(GatewayDown, match="no managed accounts"):
        check_accounts([], "paper")


def test_check_accounts_refuses_a_mixed_login_while_paper():
    with pytest.raises(GatewayDown, match="NON-paper"):
        check_accounts(["DU1", "U2"], "paper")


def test_check_accounts_leaves_account_prefixes_alone_outside_paper():
    check_accounts(["U2"], "live")


# --- probe_gateway -------------------------------------------------------------------------


def test_probe_returns_the_managed_accounts(monkeypatch):
    calls = []

    async def fake(host, port, client_id, timeout):
        calls.append((host, port, client_id, timeout))
        return ["DU1"]

    monkeypatch.setattr(gateway_watch, "_managed_accounts", fake)
    assert gateway_watch.probe_gateway("ib-gateway", 4004, 99, timeout=3) == ["DU1"]
    assert calls == [("ib-gateway", 4004, 99, 3)]


def test_probe_turns_any_connection_error_into_gateway_down(monkeypatch):
    async def fake(host, port, client_id, timeout):
        raise TimeoutError()

    monkeypatch.setattr(gateway_watch, "_managed_accounts", fake)
    with pytest.raises(GatewayDown, match="ib-gateway:4004: TimeoutError"):
        gateway_watch.probe_gateway("ib-gateway", 4004, 99)


def test_probe_works_from_a_worker_thread_like_the_scheduler(monkeypatch):
    """APScheduler runs jobs on pool threads, which have no event loop of their own."""
    import threading

    async def fake(host, port, client_id, timeout):
        return ["DU1"]

    monkeypatch.setattr(gateway_watch, "_managed_accounts", fake)
    result = []
    thread = threading.Thread(target=lambda: result.append(gateway_watch.probe_gateway("h", 1, 99)))
    thread.start()
    thread.join()
    assert result == [["DU1"]]


def test_managed_accounts_connects_read_only_and_disconnects(monkeypatch):
    import asyncio

    import ib_async

    seen = {}

    class FakeIB:
        async def connectAsync(self, host, port, clientId, timeout, readonly, fetchFields):
            seen.update(host=host, port=port, client_id=clientId, readonly=readonly)

        def managedAccounts(self):
            return ["DU1"]

        def disconnect(self):
            seen["disconnected"] = True

    monkeypatch.setattr(ib_async, "IB", FakeIB)
    accounts = asyncio.run(gateway_watch._managed_accounts("h", 4004, 99, 1))
    assert accounts == ["DU1"]
    assert seen == {
        "host": "h",
        "port": 4004,
        "client_id": 99,
        "readonly": True,
        "disconnected": True,
    }


# --- send_ntfy -----------------------------------------------------------------------------


def test_send_ntfy_posts_title_and_body_to_the_topic():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return SimpleNamespace(close=lambda: None)

    assert send_ntfy("https://ntfy.sh/", "topic-x", "Title", "Body", opener=opener)
    request, timeout = requests[0]
    assert request.full_url == "https://ntfy.sh/topic-x"
    assert request.get_method() == "POST"
    assert request.data == b"Body"
    assert request.get_header("Title") == "Title"
    assert request.get_header("Priority") == "high"
    assert timeout == 10


def test_send_ntfy_without_a_topic_sends_nothing():
    def opener(request, timeout):
        raise AssertionError("must not send")

    assert send_ntfy("https://ntfy.sh", "", "Title", "Body", opener=opener) is False


def test_send_ntfy_swallows_a_network_error():
    def opener(request, timeout):
        raise OSError("offline")

    assert send_ntfy("https://ntfy.sh", "t", "Title", "Body", opener=opener) is False


# --- watch_from_settings -------------------------------------------------------------------


def test_watch_from_settings_wires_the_probe_and_notifier(monkeypatch):
    from ibkr_trader.config import Settings

    settings = Settings(
        _env_file=None,
        ibkr_host="ib-gateway",
        ibkr_port=4004,
        gateway_check_client_id=77,
        gateway_alert_after_failures=3,
        gateway_alert_repeat_hours=1.5,
        ntfy_server="https://example.test",
        ntfy_topic="t",
    )
    probes, posts = [], []
    monkeypatch.setattr(
        gateway_watch, "probe_gateway", lambda *args: probes.append(args) or ["DU1"]
    )
    monkeypatch.setattr(gateway_watch, "send_ntfy", lambda *args: posts.append(args) or True)

    watch = gateway_watch.watch_from_settings(settings)
    watch()
    watch.notify("T", "M")

    assert probes == [("ib-gateway", 4004, 77)]
    assert posts == [("https://example.test", "t", "T", "M")]
    assert watch.alert_after == 3
    assert watch.repeat == timedelta(hours=1.5)
    assert watch.environment == "paper"


def test_default_check_client_id_differs_from_the_trading_one():
    """Same client ID twice and IBKR drops one of the two connections."""
    from ibkr_trader.config import Settings

    settings = Settings(_env_file=None)
    assert settings.gateway_check_client_id != settings.ibkr_client_id
