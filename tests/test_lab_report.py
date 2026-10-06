"""Tests for the strategy-lab HTML report (dashboard/lab_report.py).

Hermetic: a hand-built ``LabResult``, no DB, no browser. Needs the [report] extra (plotly).
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from ibkr_trader.backtest import lab

# both modules import plotly at load time: skip the file cleanly without the extra
rpt = pytest.importorskip("ibkr_trader.dashboard.lab_report")
BENCHMARK_COLOR = rpt.BENCHMARK_COLOR
SERIES_COLORS = rpt.SERIES_COLORS

STAMP = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _spec(name: str, label: str) -> lab.StrategySpec:
    return lab.StrategySpec(name, label, f"about {label}", lab.CouchPotatoAllocator, 6, 0.05)


def _curve(start: date, n: int, growth: float) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), 100_000.0 * (1 + growth) ** i) for i in range(n)]


def _result(compare: bool = True, mood_start: date | None = date(2025, 10, 8)) -> lab.LabResult:
    windows = [
        lab.Window("Since 2024", date(2024, 12, 1), 0.4),
        lab.Window("Last 1y", date(2025, 10, 5), 0.6),
    ]
    specs = [_spec(lab.REFERENCE, "Couch potato"), _spec("a", "Alpha <A&B>"), _spec("b", "Beta")]
    result = lab.LabResult(
        specs=specs,
        windows=windows,
        account="tfsa",
        compare_account="rrsp" if compare else None,
        asof=date(2026, 10, 5),
        mood_start=mood_start,
        universe_n=42,
    )
    for i, spec in enumerate(specs):
        for window in windows:
            curve = _curve(window.eval_start, 400, 0.0002 * (i + 1))
            metrics = {
                "cagr": 0.05 * (i + 1),
                "max_drawdown": -0.1,
                "sharpe": 1.0,
                "trades_per_year": 3.0 * i,
                "avg_holding_years": 5.0 - i,
                "end_value_cad": curve[-1][1],
                "tax_cad": 100.0,
            }
            result.runs.append(lab.LabRun(spec.name, window.label, "tfsa", metrics, curve))
        if compare:
            result.runs.append(
                lab.LabRun(spec.name, windows[0].label, "rrsp", {"cagr": 0.06, "tax_cad": 0.0}, [])
            )
    result.holdings = {lab.REFERENCE: [("XIC", 0.25), ("SPY", 0.45)], "a": [("RY", 0.12)], "b": []}
    return result


def test_strategy_colors_are_fixed_and_the_reference_is_muted():
    colors = rpt.strategy_colors(_result())
    assert colors == {
        lab.REFERENCE: BENCHMARK_COLOR,
        "a": SERIES_COLORS[0],
        "b": SERIES_COLORS[1],
    }


def test_verdict_chart_ranks_best_on_top():
    fig = rpt.verdict_chart(_result())
    (bar,) = fig.data
    assert list(bar.y)[-1] == "Beta"  # plotly draws bottom-up: the top bar is the best
    assert list(bar.x) == pytest.approx([0.0, 0.05, 0.10])


def test_window_cagr_chart_has_one_trace_per_strategy_labelled_with_weights():
    fig = rpt.window_cagr_chart(_result())
    assert [t.name for t in fig.data] == ["Couch potato", "Alpha <A&B>", "Beta"]
    assert list(fig.data[0].x) == ["Since 2024 (40%)", "Last 1y (60%)"]


@pytest.mark.parametrize("drawdown", [False, True])
def test_windowed_curves_show_one_window_at_a_time(drawdown):
    fig = rpt.windowed_curves(_result(), drawdown=drawdown)
    assert len(fig.data) == 6  # 3 strategies × 2 windows
    visible = [t.visible for t in fig.data]
    assert visible == [False, False, False, True, True, True]  # default: newest window
    buttons = fig.layout.updatemenus[0].buttons
    assert [b.label for b in buttons] == ["Since 2024", "Last 1y"]
    assert buttons[0].args[0]["visible"] == [True, True, True, False, False, False]
    first = fig.data[3]
    if drawdown:
        assert max(first.y) <= 0.0
    else:
        assert first.y[0] == pytest.approx(100.0)
    reference = next(t for t in fig.data if t.name == "Couch potato")
    assert reference.line.dash == "dash"


def test_windowed_curves_honours_a_known_default_window():
    fig = rpt.windowed_curves(_result(), drawdown=False, default="Since 2024")
    assert [t.visible for t in fig.data][:3] == [True, True, True]


def test_calendar_heatmap_rows_and_years():
    fig = rpt.calendar_heatmap(_result())
    (heat,) = fig.data
    assert list(heat.y) == ["Beta", "Alpha <A&B>", "Couch potato"]
    assert list(heat.x) == ["2024", "2025", "2026"]
    assert heat.zmid == 0


def test_friction_chart_two_panels():
    fig = rpt.friction_chart(_result())
    assert len(fig.data) == 2
    assert list(fig.data[0].x) == [0.0, 3.0, 6.0]
    assert list(fig.data[1].x) == [5.0, 4.0, 3.0]


def test_tables_escape_labels_and_show_accounts():
    result = _result()
    assert "Alpha &lt;A&amp;B&gt;" in rpt.scorecard_table(result)
    accounts = rpt.account_table(result)
    assert "TFSA CAGR" in accounts and "RRSP CAGR" in accounts
    assert rpt.account_table(_result(compare=False)) == ""


def test_holdings_cards_show_cash_and_empty_books():
    cards = rpt.holdings_html(_result())
    assert "45.0%" in cards
    assert "<td>cash</td><td>30.0%</td>" in cards  # the reference book is 70% invested
    assert "<td>cash</td><td>100.0%</td>" in cards  # an empty book is all cash


def test_build_lab_report_is_one_self_contained_document():
    page = rpt.build_lab_report(_result(), generated_at=STAMP)
    assert page.startswith("<!DOCTYPE html>")
    assert page.count("<script") >= 1 and "plotly" in page.lower()
    for section in ("Verdict, weighted to recent data", "Friction", "If you opened the account"):
        assert section in page
    assert "2026-10-05 12:00 UTC" in page
    assert "usable from about 2025-10-08" in page
    assert "Alpha &lt;A&amp;B&gt;" in page


def test_strategies_html_lists_every_strategy_escaped():
    listing = rpt.strategies_html(_result())
    assert listing.count("<dt>") == 3
    assert "<code>a</code>" in listing and "about Alpha &lt;A&amp;B&gt;" in listing


def test_build_lab_report_without_mood_data_says_so():
    page = rpt.build_lab_report(_result(mood_start=None), generated_at=STAMP)
    assert "No news/social mood data" in page
