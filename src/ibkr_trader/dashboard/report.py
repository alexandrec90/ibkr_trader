"""Static HTML report generator — leaderboard + equity/drawdown charts as one self-contained
file, no server.

The whole point is to be light on a laptop: this runs for a second or two under
``ibkr-trader report``, writes ``report.html`` (Plotly inlined, works offline), and exits —
nothing stays resident. Interactive filtering is gone versus the old Streamlit page, but the
Plotly charts keep pan/zoom/hover/legend-toggle inside the static file.

All data shaping lives in [data.py](data.py); this module only builds figures and stitches the
HTML. Needs the ``[report]`` extra (plotly) — imported lazily by the CLI and importorskip'd in
tests, so ``data.py`` stays importable without it.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

import pandas as pd
import plotly.graph_objects as go
from sqlalchemy.orm import Session

from ibkr_trader.dashboard import data as dash

# Categorical series colors (validated palette, fixed assignment order — never cycled).
# Benchmarks are reference series: muted ink + dash, so they never consume a slot.
SERIES_COLORS = (
    "#2a78d6",  # blue
    "#008300",  # green
    "#e87ba4",  # magenta
    "#eda100",  # yellow
    "#1baf7a",  # aqua
    "#eb6834",  # orange
    "#4a3aa7",  # violet
    "#e34948",  # red
)
BENCHMARK_COLOR = "#8a8988"
DRAWDOWN_COLOR = "#2a78d6"

SURVIVORSHIP_NOTE = (
    "Curated-current universes are survivorship-biased: absolute returns are upper bounds. "
    "Rank strategies only on the identical universe and window."
)

#: Leaderboard columns in display order, and the format applied to each numeric one. Columns
#: absent here render with their default string form. Mirrors the old Streamlit column_config.
_LEADERBOARD_COLUMNS = (
    "run_id",
    "strategy",
    "account",
    "window",
    *dash.SUMMARY_METRICS,
    "model_version",
    "universe_n",
    "has_curve",
)
_NUMERIC_FORMATS = {
    "total_return": "{:.2f}",
    "benchmark_total_return": "{:.2f}",
    "excess_return": "{:.2f}",
    "cagr": "{:.3f}",
    "sharpe": "{:.2f}",
    "sortino": "{:.2f}",
    "max_drawdown": "{:.3f}",
    "calmar": "{:.2f}",
    "trades_per_year": "{:.1f}",
    "costs_cad": "{:.0f}",
    "tax_cad": "{:.0f}",
    "fx_cost_cad": "{:.0f}",
    "end_value_cad": "{:.0f}",
}


def equity_chart(curves: pd.DataFrame, *, rebased: bool) -> go.Figure:
    """Overlaid equity curves; strategies get palette colors, benchmarks a muted dashed line."""
    fig = go.Figure()
    strategy_series = curves.loc[curves["kind"] == "strategy", "series"].unique().tolist()
    for i, series in enumerate(strategy_series):
        block = curves[curves["series"] == series]
        fig.add_trace(
            go.Scatter(
                x=block["date"],
                y=block["equity_cad"],
                name=series,
                mode="lines",
                line={"width": 2, "color": SERIES_COLORS[i % len(SERIES_COLORS)]},
            )
        )
    for series in curves.loc[curves["kind"] == "benchmark", "series"].unique():
        block = curves[curves["series"] == series]
        fig.add_trace(
            go.Scatter(
                x=block["date"],
                y=block["equity_cad"],
                name=series,
                mode="lines",
                line={"width": 2, "color": BENCHMARK_COLOR, "dash": "dash"},
            )
        )
    fig.update_layout(
        template="plotly_white",
        yaxis_title="growth of 100" if rebased else "portfolio value (CAD)",
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
        margin={"t": 30, "r": 10},
        height=450,
    )
    return fig


def drawdown_chart(curves: pd.DataFrame) -> go.Figure:
    """Per-strategy running drawdown from equity peaks (benchmarks omitted)."""
    dd = dash.drawdown(curves[curves["kind"] == "strategy"])
    fig = go.Figure()
    strategy_series = dd["series"].unique().tolist()
    for i, series in enumerate(strategy_series):
        block = dd[dd["series"] == series]
        color = (
            SERIES_COLORS[i % len(SERIES_COLORS)] if len(strategy_series) > 1 else DRAWDOWN_COLOR
        )
        fig.add_trace(
            go.Scatter(
                x=block["date"],
                y=block["drawdown"],
                name=series,
                mode="lines",
                line={"width": 2, "color": color},
            )
        )
    fig.update_layout(
        template="plotly_white",
        yaxis_title="drawdown",
        yaxis_tickformat=".0%",
        hovermode="x unified",
        showlegend=len(strategy_series) > 1,
        margin={"t": 30, "r": 10},
        height=300,
    )
    return fig


def leaderboard_html(runs: pd.DataFrame) -> str:
    """Format the runs frame as an HTML ``<table>`` (numbers per ``_NUMERIC_FORMATS``)."""
    columns = [c for c in _LEADERBOARD_COLUMNS if c in runs.columns]
    view = runs[columns].copy()
    for col, fmt in _NUMERIC_FORMATS.items():
        if col in view.columns:
            view[col] = view[col].map(lambda v, f=fmt: "" if pd.isna(v) else f.format(v))
    return view.to_html(index=False, na_rep="", border=0, classes="leaderboard")


def build_report(
    runs: pd.DataFrame,
    curves: pd.DataFrame,
    *,
    rebased: bool,
    generated_at: datetime,
) -> str:
    """Assemble the full self-contained HTML document from an already-shaped runs/curves pair.

    ``curves`` holds only the plottable (curve-bearing) runs, already rebased if ``rebased``.
    An empty ``runs`` yields the "no runs yet" page; runs present but no curves yields the
    leaderboard plus a note in place of the charts.
    """
    if runs.empty:
        body = (
            '<p class="info">No persisted backtest runs yet — run one via '
            "<code>ibkr-trader backtest run</code>.</p>"
        )
    else:
        parts = ["<h2>Leaderboard</h2>", leaderboard_html(runs)]
        if curves.empty:
            parts.append(
                '<p class="info">None of the runs stored an equity curve (runs persisted '
                "before curve storage don't have one) — re-run them to get charts.</p>"
            )
        else:
            # First chart inlines plotly.js (~offline, self-contained); the second reuses it.
            equity = equity_chart(curves, rebased=rebased).to_html(
                full_html=False, include_plotlyjs=True
            )
            drawdown = drawdown_chart(curves).to_html(full_html=False, include_plotlyjs=False)
            parts += ["<h2>Equity curves</h2>", equity, "<h2>Drawdown</h2>", drawdown]
        body = "\n".join(parts)

    stamp = generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    n = len(runs)
    return _DOCUMENT.format(
        note=html.escape(SURVIVORSHIP_NOTE),
        stamp=stamp,
        n=n,
        runs_word="run" if n == 1 else "runs",
        body=body,
    )


def build_report_from_db(
    session: Session,
    *,
    rebased: bool | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Read the leaderboard + plottable curves from ``session`` and render the HTML report.

    ``rebased=None`` picks the sensible default: rebase to growth-of-100 when more than one
    series would overlay (so different starting capitals compare), otherwise show raw CAD.
    """
    generated_at = generated_at or datetime.now(UTC)
    runs = dash.runs_frame(session)
    if runs.empty:
        return build_report(runs, runs, rebased=False, generated_at=generated_at)

    plottable = runs[runs["has_curve"]]
    run_ids = [int(rid) for rid in plottable["run_id"]]
    curves = dash.curve_frame(session, run_ids)
    if rebased is None:
        rebased = curves["series"].nunique() > 1
    if rebased and not curves.empty:
        curves = dash.rebase(curves)
    return build_report(runs, curves, rebased=rebased, generated_at=generated_at)


_DOCUMENT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ibkr-trader backtests</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 2rem auto;
         max-width: 1100px; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ margin-bottom: 0.2rem; }}
  h2 {{ margin-top: 2rem; border-bottom: 1px solid #e5e5e5; padding-bottom: 0.3rem; }}
  .caption {{ color: #555; font-size: 0.9rem; }}
  .meta {{ color: #888; font-size: 0.8rem; }}
  .info {{ background: #f4f6f8; border-left: 3px solid #2a78d6; padding: 0.7rem 1rem;
          border-radius: 4px; }}
  table.leaderboard {{ border-collapse: collapse; width: 100%; font-size: 0.85rem;
                       font-variant-numeric: tabular-nums; }}
  table.leaderboard th, table.leaderboard td {{ padding: 0.35rem 0.6rem; text-align: right;
                                                border-bottom: 1px solid #eee; }}
  table.leaderboard th {{ text-align: right; background: #f4f6f8; position: sticky; top: 0; }}
  table.leaderboard td:nth-child(-n+4), table.leaderboard th:nth-child(-n+4) {{ text-align: left; }}
</style>
</head>
<body>
<h1>Backtest results</h1>
<p class="caption">{note}</p>
<p class="meta">Generated {stamp} · {n} {runs_word}</p>
{body}
</body>
</html>
"""
