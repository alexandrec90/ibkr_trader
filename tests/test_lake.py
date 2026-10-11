"""Tests for `src/ibkr_trader/lake.py`, the seam that wires `data_lake` to this repo.

The shared package owns no config system and no engine: it is handed both, once, at
whichever entry point this process started from. That indirection is the whole reason a
second project can reuse the connectors, and it is invisible at every call site — a
connector asks `data_lake.runtime` for settings and gets whatever the consumer injected.

So the properties worth pinning are the ones that break silently: that the settings
handed over are *this* repo's, that the session factory is this repo's (the package must
never build an engine of its own), and that the lookup happens at call time rather than
at import time, which is what lets a test monkeypatch `get_settings` and still win.
"""

from __future__ import annotations

import data_lake
import data_lake.runtime
import pytest

from ibkr_trader import config, lake
from ibkr_trader.db import session as db_session


@pytest.fixture(autouse=True)
def _restore_lake_configuration():
    """`data_lake.configure` sets process-wide globals; leaving them set would make this
    module's tests decide whether a later one sees a configured package."""
    before_settings = data_lake.runtime._settings
    before_factory = data_lake.runtime._session_factory
    yield
    data_lake.runtime._settings = before_settings
    data_lake.runtime._session_factory = before_factory


def test_configure_lake_hands_over_this_repos_settings_and_session_factory():
    data_lake.reset()

    lake.configure_lake()

    assert data_lake.runtime.resolve_settings(None) is config.get_settings()
    # The package borrows sessions from the consumer's engine and never builds one.
    assert data_lake.runtime.resolve_session_factory(None) is db_session.get_session


def test_configure_lake_reads_get_settings_at_call_time(monkeypatch):
    """The module attributes are looked up per call, not bound at import — otherwise a
    test (or an entry point that loads config late) would be handed a stale object."""
    sentinel = config.Settings()
    monkeypatch.setattr(config, "get_settings", lambda: sentinel)
    data_lake.reset()

    lake.configure_lake()

    assert data_lake.runtime.resolve_settings(None) is sentinel


def test_configure_lake_is_idempotent():
    """Both entry points call it — the CLI root callback and `build_scheduler` — and a
    `serve` run goes through both, so the second call must be a no-op, not a reset."""
    data_lake.reset()

    lake.configure_lake()
    first = data_lake.runtime.resolve_settings(None)
    lake.configure_lake()

    assert data_lake.runtime.resolve_settings(None) is first
    assert data_lake.runtime.resolve_session_factory(None) is db_session.get_session


def test_configure_lake_passes_both_arguments_by_keyword(monkeypatch):
    """`data_lake.configure` is keyword-only, and either argument may be omitted to leave
    that default untouched. Passing one positionally, or dropping one, fails at runtime
    inside a connector rather than here."""
    recorded: dict[str, object] = {}

    def fake_configure(**kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(data_lake, "configure", fake_configure)

    lake.configure_lake()

    assert set(recorded) == {"settings", "session_factory"}
    assert recorded["settings"] is config.get_settings()
    assert recorded["session_factory"] is db_session.get_session


def test_settings_satisfies_every_member_of_the_lake_protocol():
    """`Settings` meets `LakeSettings` structurally, with no import of it in config.py, so a
    member data-lake adds (as it did `fred_api_key`) breaks only mypy until this fails too.
    Naming the gaps beats a bare `isinstance`: the nightly run then says which field to add.
    data-lake declares every member as a read-only property, which is what is listed here."""
    settings = config.Settings()
    members = {
        name
        for protocol in (data_lake.IngestionSettings, data_lake.ArchiveSettings)
        for name, value in vars(protocol).items()
        if isinstance(value, property)
    }
    missing = sorted(name for name in members if not hasattr(settings, name))

    assert "fred_api_key" in members  # the listing found the protocol's members at all

    assert missing == []
    assert isinstance(settings, data_lake.LakeSettings)


def test_an_unconfigured_package_refuses_rather_than_inventing_a_default():
    """The failure this seam exists to prevent, so the assertion above is not vacuous."""
    data_lake.reset()

    with pytest.raises(RuntimeError):
        data_lake.runtime.resolve_settings(None)
    with pytest.raises(RuntimeError):
        data_lake.runtime.resolve_session_factory(None)
