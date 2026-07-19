"""Data-quality contracts at the PostgreSQL-to-research boundary."""

from datetime import UTC, date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from pandera.errors import SchemaErrors

from ibkr_trader.signals.dataset import META_COLUMNS
from ibkr_trader.signals.schemas import (
    NUMERIC_FEATURE_COLUMNS,
    validate_feature_frame,
    validate_price_frame,
)


def _price_frame() -> pd.DataFrame:
    now = datetime.now(UTC) - timedelta(days=2)
    return pd.DataFrame(
        {
            "instrument_id": [1, 1],
            "ts": [now, now + timedelta(days=1)],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "volume": [100.0, np.nan],
        }
    )


def _assert_price_failure(frame: pd.DataFrame, rule: str) -> None:
    with pytest.raises(SchemaErrors) as caught:
        validate_price_frame(frame)
    assert rule in str(caught.value)


def test_clean_price_frame_passes_all_rules():
    result = validate_price_frame(_price_frame())
    assert len(result) == 2


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (lambda frame: frame.assign(close=[-1.0, 12.0]), "close_positive"),
        (lambda frame: frame.assign(high=[8.0, 13.0]), "high_gte_low"),
        (lambda frame: frame.assign(close=[12.5, 12.0]), "close_within_high_low"),
        (lambda frame: frame.assign(volume=[-1.0, np.nan]), "volume_non_negative"),
        (
            lambda frame: frame.assign(ts=[frame.loc[0, "ts"], frame.loc[0, "ts"]]),
            "ts_unique_per_instrument",
        ),
        (lambda frame: frame.iloc[::-1].reset_index(drop=True), "ts_monotonic_per_instrument"),
        (
            lambda frame: frame.assign(
                ts=[datetime.now(UTC) + timedelta(days=1), frame.loc[1, "ts"]]
            ),
            "ts_not_future",
        ),
        (
            lambda frame: frame.assign(
                ts=pd.Series(
                    [
                        datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=-5))),
                        datetime(2020, 1, 2, tzinfo=timezone(timedelta(hours=-5))),
                    ]
                )
            ),
            "ts_utc",
        ),
    ],
)
def test_price_rule_violation_is_named(mutate, rule):
    _assert_price_failure(mutate(_price_frame()), rule)


def _feature_frame() -> pd.DataFrame:
    data: dict[str, object] = {
        "instrument_id": [1],
        "symbol": ["AAA"],
        "date": [date(2025, 1, 31)],
        "fwd_excess_return": [0.1],
        "label": [0.5],
        "sector": [None],
    }
    data.update({column: [np.nan] for column in NUMERIC_FEATURE_COLUMNS})
    data["history_days"] = [252.0]
    return pd.DataFrame(data)[list(META_COLUMNS) + sorted(["sector", *NUMERIC_FEATURE_COLUMNS])]


def test_clean_feature_frame_passes_current_version_contract():
    result = validate_feature_frame(_feature_frame())
    assert set(result.columns) == set(META_COLUMNS) | {"sector", *NUMERIC_FEATURE_COLUMNS}


def test_infinite_feature_is_named_in_lazy_report():
    frame = _feature_frame()
    frame.loc[0, "history_days"] = np.inf
    with pytest.raises(SchemaErrors) as caught:
        validate_feature_frame(frame)
    assert "finite_values" in str(caught.value)
    assert "history_days" in str(caught.value)


def test_feature_frame_rejects_wrong_label_dtype():
    frame = _feature_frame()
    frame["label"] = "not-a-number"
    with pytest.raises(SchemaErrors) as caught:
        validate_feature_frame(frame)
    assert "dtype" in str(caught.value)


def test_feature_frame_requires_every_versioned_column():
    frame = _feature_frame().drop(columns="return_12m")
    with pytest.raises(SchemaErrors) as caught:
        validate_feature_frame(frame)
    assert "return_12m" in str(caught.value)


def test_close_within_high_low_tolerates_adjustment_epsilon():
    """Adjusted provider bars scale OHLC through a float multiplier; close may exceed high
    (or undercut low) by machine epsilon. That noise must pass; real gaps (the ~4% case in
    the parametrized test above) must still fail."""
    frame = _price_frame()
    frame.loc[0, "close"] = frame.loc[0, "high"] * (1 + 1e-12)
    frame.loc[1, "close"] = frame.loc[1, "low"] * (1 - 1e-12)
    result = validate_price_frame(frame)
    assert len(result) == 2
