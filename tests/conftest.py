"""Shared test fixtures.

The suite must be hermetic: a developer shell with `.env` exported (IBKR accounts, archive
S3 credentials, ...) must not change test outcomes, so every Settings-backed environment
variable is scrubbed before each test. Tests that need one set it via monkeypatch.setenv.
"""

import pytest

from ibkr_trader.config import Settings


# Uses a private MonkeyPatch rather than the `monkeypatch` fixture: requesting that fixture
# here would instantiate it before module-level autouse fixtures, reversing their teardown
# order (e.g. the connector tests' clear_settings_cache runs after monkeypatch.undo today).
@pytest.fixture(autouse=True)
def _isolate_settings_env():
    mp = pytest.MonkeyPatch()
    for field_name in Settings.model_fields:
        mp.delenv(field_name.upper(), raising=False)
    yield
    mp.undo()
