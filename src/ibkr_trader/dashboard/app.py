"""Streamlit dashboard — leaderboard, equity/drawdown charts, and a run launcher.

Launch with ``ibkr-trader dashboard`` (which shells out to ``streamlit run`` on this file);
needs the ``[dashboard]`` extra. Reads/writes the same Postgres the CLI uses, via Settings.

Rendering only — all data shaping lives in [data.py](data.py) where it is unit-tested.
"""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ibkr_trader.dashboard import data as dash
from ibkr_trader.db.session import get_session
from ibkr_trader.signals.portfolio import available as available_allocators

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


@st.cache_data(ttl=60)
def _runs() -> pd.DataFrame:
    with get_session() as session:
        return dash.runs_frame(session)


@st.cache_data(ttl=60)
def _curves(run_ids: list[int]) -> pd.DataFrame:
    with get_session() as session:
        return dash.curve_frame(session, run_ids)


def _equity_chart(curves: pd.DataFrame, *, rebased: bool) -> go.Figure:
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


def _drawdown_chart(curves: pd.DataFrame) -> go.Figure:
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


def _run_launcher() -> None:
    st.subheader("Run a backtest")
    strategies = sorted(available_allocators())
    with st.form("run_backtest"):
        strategy = st.selectbox("strategy", strategies, index=0)
        account = st.selectbox("account", ["tfsa", "rrsp", "fhsa", "lira", "nonreg"], index=0)
        universe_file = st.text_input("universe file", value="tickers.txt")
        col_start, col_eval, col_end = st.columns(3)
        start = col_start.date_input("bar-load start (warm-up)", value=date(2008, 6, 2))
        eval_start = col_eval.date_input("first decision date", value=date(2010, 1, 4))
        end = col_end.date_input("window end", value=date.today())
        submitted = st.form_submit_button("Run (persists a backtest_runs row)")
    if submitted:
        try:
            with st.spinner(f"simulating {strategy}…"):
                result = dash.run_backtest(
                    strategy,
                    universe_file,
                    account,
                    start,
                    end,
                    eval_start=eval_start,
                )
        except ValueError as exc:
            st.error(str(exc))
            return
        st.success(
            f"run #{result.run_id}: total return "
            f"{result.metrics.get('total_return', 0) * 100:+.1f}%, "
            f"excess vs benchmark {result.metrics.get('excess_return', 0) * 100:+.1f}%"
        )
        _runs.clear()
        _curves.clear()


def main() -> None:
    st.set_page_config(page_title="ibkr-trader backtests", layout="wide")
    st.title("Backtest results")
    st.caption(SURVIVORSHIP_NOTE)

    runs = _runs()
    if runs.empty:
        st.info("No persisted backtest runs yet — run one below or via `ibkr-trader backtest run`.")
        _run_launcher()
        return

    with st.sidebar:
        st.header("Filters")
        strategies = sorted(runs["strategy"].unique())
        picked_strategies = st.multiselect("strategy", strategies, default=strategies)
        accounts = sorted(runs["account"].astype(str).unique())
        picked_accounts = st.multiselect("account", accounts, default=accounts)
        sort_by = st.selectbox("sort leaderboard by", list(dash.SUMMARY_METRICS), index=3)

    view = runs[
        runs["strategy"].isin(picked_strategies) & runs["account"].astype(str).isin(picked_accounts)
    ].sort_values(sort_by, ascending=False, na_position="last")

    st.subheader("Leaderboard")
    leaderboard_cols = [
        "run_id",
        "strategy",
        "account",
        "window",
        *dash.SUMMARY_METRICS,
        "model_version",
        "universe_n",
        "has_curve",
    ]
    st.dataframe(
        view[leaderboard_cols],
        hide_index=True,
        column_config={
            "total_return": st.column_config.NumberColumn(format="%.2f"),
            "benchmark_total_return": st.column_config.NumberColumn(format="%.2f"),
            "excess_return": st.column_config.NumberColumn(format="%.2f"),
            "cagr": st.column_config.NumberColumn(format="%.3f"),
            "sharpe": st.column_config.NumberColumn(format="%.2f"),
            "sortino": st.column_config.NumberColumn(format="%.2f"),
            "max_drawdown": st.column_config.NumberColumn(format="%.3f"),
            "calmar": st.column_config.NumberColumn(format="%.2f"),
            "costs_cad": st.column_config.NumberColumn(format="%.0f"),
            "tax_cad": st.column_config.NumberColumn(format="%.0f"),
            "fx_cost_cad": st.column_config.NumberColumn(format="%.0f"),
            "end_value_cad": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    st.subheader("Equity curves")
    plottable = view[view["has_curve"]]
    if plottable.empty:
        st.info(
            "None of the filtered runs stored an equity curve (runs persisted before curve "
            "storage landed don't have one) — re-run them to get charts."
        )
    else:
        labels = {
            dash.run_label(r.run_id, r.strategy, str(r.account), r.window): int(r.run_id)
            for r in plottable.itertuples()
        }
        default = list(labels)[: min(3, len(labels))]
        picked = st.multiselect("runs to plot", list(labels), default=default)
        rebased = st.toggle("rebase each curve to 100 at its first point", value=len(picked) > 1)
        if picked:
            curves = _curves(sorted(labels[label] for label in picked))
            if rebased:
                curves = dash.rebase(curves)
            st.plotly_chart(_equity_chart(curves, rebased=rebased), width="stretch")
            st.plotly_chart(_drawdown_chart(curves), width="stretch")

    st.divider()
    _run_launcher()


if __name__ == "__main__":  # `streamlit run` executes this file as __main__
    main()
