"""Pandera data-quality gates for price and supervised-feature frames.

Provider rows are deliberately accepted by ingestion and validated only when downstream
research code reads them.  Validation is lazy so callers receive one report containing every
violation in the frame instead of fixing failures one at a time.
"""

from datetime import UTC, date
from typing import Any

import numpy as np
import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from ibkr_trader.signals.features import FEATURE_SET_VERSION

# Numeric model inputs emitted by signals.features for feature-set v2.  The dataset builder
# materializes every column, using NaN when a feature is unavailable for an observation.
NUMERIC_FEATURE_COLUMNS = (
    "dividend_growth_3y",
    "dividend_yield_ttm",
    "downside_deviation_252d",
    "excess_return_12m",
    "excess_return_3m",
    "history_days",
    "log_market_cap",
    "max_drawdown_252d",
    "momentum_12_1",
    "pct_off_52w_high",
    "return_12m",
    "return_1m",
    "return_3m",
    "return_6m",
    "volatility",
    "volatility_60d",
    "volume_zscore_60d",
)


#: Relative tolerance for close-vs-high/low: float-adjustment noise only, never real gaps.
PRICE_REL_TOL = 1e-9


def _ts_is_utc(frame: pd.DataFrame) -> bool:
    dtype = frame["ts"].dtype
    return isinstance(dtype, pd.DatetimeTZDtype) and str(dtype.tz) == "UTC"


def _ts_not_future(frame: pd.DataFrame) -> pd.Series:
    if not _ts_is_utc(frame):
        return pd.Series(False, index=frame.index)
    return frame["ts"] <= pd.Timestamp.now(tz=UTC)


def _ts_monotonic_per_instrument(frame: pd.DataFrame) -> bool:
    if not _ts_is_utc(frame):
        return False
    return bool(
        frame.groupby("instrument_id", sort=False)["ts"]
        .apply(lambda values: values.is_monotonic_increasing)
        .all()
    )


price_frame_schema = pa.DataFrameSchema(
    {
        "instrument_id": pa.Column(pa.Int64, nullable=False),
        # Pandas 3 may use microsecond resolution, so timezone and datetime-ness are checked
        # explicitly rather than hard-coding datetime64[ns, UTC].
        "ts": pa.Column(None, nullable=False),
        "open": pa.Column(float, nullable=False, coerce=True),
        "high": pa.Column(float, nullable=False, coerce=True),
        "low": pa.Column(float, nullable=False, coerce=True),
        "close": pa.Column(
            float,
            pa.Check(lambda values: values > 0, name="close_positive"),
            nullable=False,
            coerce=True,
        ),
        "volume": pa.Column(
            float,
            pa.Check(lambda values: values >= 0, name="volume_non_negative"),
            nullable=True,
            coerce=True,
        ),
    },
    checks=[
        pa.Check(_ts_is_utc, name="ts_utc"),
        pa.Check(
            lambda frame: ~frame.duplicated(["instrument_id", "ts"], keep=False),
            name="ts_unique_per_instrument",
        ),
        pa.Check(_ts_monotonic_per_instrument, name="ts_monotonic_per_instrument"),
        pa.Check(_ts_not_future, name="ts_not_future"),
        pa.Check(lambda frame: frame["high"] >= frame["low"], name="high_gte_low"),
        # Adjusted bars (yfinance auto_adjust) scale OHLC by a float multiplier, so close can
        # land outside [low, high] by machine epsilon (~2e-16 relative in the stored data).
        # Tolerate that noise only — anything larger is a provider bug the ingest layer must
        # normalize instead (see yahoo_fx's high/low clamping).
        pa.Check(
            lambda frame: (
                (frame["high"] * (1 + PRICE_REL_TOL) >= frame["close"])
                & (frame["close"] >= frame["low"] * (1 - PRICE_REL_TOL))
            ),
            name="close_within_high_low",
        ),
    ],
    strict=True,
    name="price_frame",
)


def _is_date(values: pd.Series) -> pd.Series:
    return values.map(lambda value: isinstance(value, date))


def _is_string_or_null(values: pd.Series) -> pd.Series:
    return values.isna() | values.map(lambda value: isinstance(value, str))


def _finite_or_null(values: pd.Series) -> pd.Series:
    return values.isna() | np.isfinite(values)


_feature_columns: dict[str, Any] = {
    "instrument_id": pa.Column(pa.Int64, nullable=False),
    "symbol": pa.Column(None, pa.Check(_is_string_or_null, name="symbol_string"), nullable=False),
    "date": pa.Column(object, pa.Check(_is_date, name="date_values"), nullable=False),
    "fwd_excess_return": pa.Column(
        float,
        pa.Check(_finite_or_null, name="finite_values"),
        nullable=False,
    ),
    "label": pa.Column(
        float,
        checks=[
            pa.Check(_finite_or_null, name="finite_values"),
            pa.Check.in_range(0.0, 1.0, name="label_in_range"),
        ],
        nullable=False,
    ),
    "sector": pa.Column(
        None,
        pa.Check(_is_string_or_null, name="sector_string_or_null"),
        nullable=True,
    ),
}
_feature_columns.update(
    {
        column: pa.Column(
            float,
            pa.Check(_finite_or_null, name="finite_values"),
            nullable=True,
        )
        for column in NUMERIC_FEATURE_COLUMNS
    }
)

feature_frame_schema = pa.DataFrameSchema(
    _feature_columns,
    strict=True,
    name=f"feature_frame_v{FEATURE_SET_VERSION}",
)


def validate_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate a canonical OHLCV frame and collect all failures in one exception."""
    return price_frame_schema.validate(frame, lazy=True)


def validate_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate the current version's assembled supervised-training frame lazily."""
    return feature_frame_schema.validate(frame, lazy=True)


def format_schema_errors(error: SchemaErrors) -> list[str]:
    """Render Pandera failure cases as compact, stable, row-oriented report lines."""
    lines: list[str] = []
    cases = error.failure_cases.drop_duplicates(subset=["column", "check", "failure_case", "index"])
    for case in cases.itertuples(index=False):
        row = "-" if pd.isna(case.index) else str(case.index)
        column = "frame" if pd.isna(case.column) else str(case.column)
        lines.append(f"row {row}: {column} failed {case.check} (value={case.failure_case!r})")
    return lines
