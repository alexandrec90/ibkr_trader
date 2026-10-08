"""Static HTML report for the strategy lab (``ibkr-trader backtest lab``).

Same contract as dashboard.report: one self-contained file (Plotly inlined, works offline),
written once, nothing resident. Reads a ``backtest.lab.LabResult``; no DB, no engine.

What it shows, in reading order — the verdict first, the evidence after:

1. the recency-weighted verdict (CAGR edge over the couch potato, 1y weighted heaviest);
2. CAGR per window, so a strategy that only won in 2010-2015 is visible as such;
3. growth-of-100 and drawdown curves, one window at a time (buttons switch windows);
4. calendar-year returns, newest year at the right;
5. friction: trades per year and implied average holding period (the CRA "frequency" factor);
6. the TFSA-vs-RRSP tax drag, and what each strategy would buy today.

Needs the ``[report]`` extra (plotly), imported lazily by the CLI.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ibkr_trader.backtest.lab import REFERENCE, LabResult, calendar_year_returns, recency_scores
from ibkr_trader.dashboard.report import BENCHMARK_COLOR, SERIES_COLORS

#: Diverging blue (gain) ↔ red (loss) around a neutral gray zero, for the year heatmap.
_DIVERGING = [[0.0, "#c22f2f"], [0.5, "#f0efec"], [1.0, "#1f5fae"]]
_LAYOUT = {
    "template": "plotly_white",
    "margin": {"t": 40, "r": 10, "l": 10},
    "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02},
    "font": {"family": 'system-ui, -apple-system, "Segoe UI", sans-serif'},
}


def strategy_colors(result: LabResult) -> dict[str, str]:
    """Fixed colour per strategy, by line-up order; the reference is always the muted gray."""
    colors: dict[str, str] = {}
    slot = 0
    for spec in result.specs:
        if spec.name == REFERENCE:
            colors[spec.name] = BENCHMARK_COLOR
        else:
            colors[spec.name] = SERIES_COLORS[slot % len(SERIES_COLORS)]
            slot += 1
    return colors


def _labels(result: LabResult) -> dict[str, str]:
    return {spec.name: spec.label for spec in result.specs}


def verdict_chart(result: LabResult) -> go.Figure:
    """Horizontal bars: recency-weighted CAGR edge over the reference, best on top."""
    scores = recency_scores(result)
    colors, labels = strategy_colors(result), _labels(result)
    ranked = sorted(scores.items(), key=lambda kv: kv[1])  # plotly draws bottom-up
    fig = go.Figure(
        go.Bar(
            x=[score for _, score in ranked],
            y=[labels[name] for name, _ in ranked],
            orientation="h",
            marker={"color": [colors[name] for name, _ in ranked]},
            text=[f"{score * 100:+.1f} pts/yr" for _, score in ranked],
            textposition="outside",
            hovertemplate="%{y}: %{x:+.1%} CAGR vs couch potato<extra></extra>",
        )
    )
    fig.update_layout(
        **_LAYOUT,
        height=60 + 50 * len(ranked),
        showlegend=False,
        xaxis={"tickformat": "+.0%", "title": "CAGR edge over couch potato, recency-weighted"},
    )
    return fig


def window_cagr_chart(result: LabResult) -> go.Figure:
    """Grouped bars: CAGR per window (oldest → newest) for each strategy."""
    colors = strategy_colors(result)
    fig = go.Figure()
    for spec in result.specs:
        xs, ys = [], []
        for window in result.windows:
            run = result.run(spec.name, window.label)
            if run is not None:
                xs.append(f"{window.label} ({window.weight:.0%})")
                ys.append(run.metrics.get("cagr", 0.0))
        fig.add_trace(
            go.Bar(
                x=xs,
                y=ys,
                name=spec.label,
                marker={"color": colors[spec.name]},
                hovertemplate=f"{spec.label}<br>%{{x}}: %{{y:.1%}} CAGR<extra></extra>",
            )
        )
    fig.update_layout(
        **_LAYOUT,
        barmode="group",
        bargap=0.25,
        bargroupgap=0.08,
        height=380,
        yaxis={"tickformat": ".0%", "title": "CAGR"},
        xaxis={"title": "window (weight in the verdict)"},
    )
    return fig


def _curve_traces(result: LabResult, window: str, *, drawdown: bool) -> list[go.Scatter]:
    colors = strategy_colors(result)
    traces = []
    for spec in result.specs:
        run = result.run(spec.name, window)
        if run is None or not run.equity_curve:
            continue
        days = [day for day, _ in run.equity_curve]
        values = [value for _, value in run.equity_curve]
        if drawdown:
            peak, ys = values[0], []
            for value in values:
                peak = max(peak, value)
                ys.append(value / peak - 1.0)
            hover = "%{y:.1%}"
        else:
            ys = [value / values[0] * 100 for value in values]
            hover = "%{y:.1f}"
        line = {"width": 2, "color": colors[spec.name]}
        if spec.name == REFERENCE:
            line["dash"] = "dash"
        traces.append(
            go.Scatter(
                x=days,
                y=ys,
                name=spec.label,
                mode="lines",
                line=line,
                hovertemplate=f"{spec.label}: {hover}<extra></extra>",
            )
        )
    return traces


def windowed_curves(result: LabResult, *, drawdown: bool, default: str | None = None) -> go.Figure:
    """Equity (growth of 100) or drawdown curves, one window visible at a time via buttons."""
    labels = [window.label for window in result.windows]
    default = default if default in labels else labels[-1]
    fig = go.Figure()
    owner: list[str] = []
    for label in labels:
        for trace in _curve_traces(result, label, drawdown=drawdown):
            trace.visible = label == default
            fig.add_trace(trace)
            owner.append(label)
    buttons = [
        {
            "label": label,
            "method": "update",
            "args": [{"visible": [o == label for o in owner]}],
        }
        for label in labels
    ]
    fig.update_layout(
        # buttons own the top edge, so the legend moves below the plot
        **{
            **_LAYOUT,
            "margin": {"t": 50, "r": 10, "l": 10},
            "legend": {"orientation": "h", "yanchor": "top", "y": -0.12},
        },
        height=360 if drawdown else 480,
        hovermode="x unified",
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "buttons": buttons,
                "active": labels.index(default),
                "x": 0,
                "xanchor": "left",
                "y": 1.12,
                "yanchor": "top",
                "showactive": True,
            }
        ],
        yaxis={"tickformat": ".0%", "title": "drawdown"}
        if drawdown
        else {"title": "growth of 100 (CAD, after costs & tax)"},
    )
    return fig


def calendar_heatmap(result: LabResult) -> go.Figure:
    """Strategy × calendar-year returns from the first (longest) window, newest year right."""
    window = result.windows[0].label
    labels = _labels(result)
    rows, names = [], []
    years: list[int] = []
    per_strategy = {}
    for spec in result.specs:
        run = result.run(spec.name, window)
        if run is None:
            continue
        per_strategy[spec.name] = calendar_year_returns(run.equity_curve)
        years = sorted(set(years) | set(per_strategy[spec.name]))
    for spec in reversed(result.specs):  # first strategy on top
        if spec.name in per_strategy:
            names.append(labels[spec.name])
            rows.append([per_strategy[spec.name].get(year) for year in years])
    bound = max((abs(v) for row in rows for v in row if v is not None), default=0.3)
    fig = go.Figure(
        go.Heatmap(
            z=rows,
            x=[str(year) for year in years],
            y=names,
            colorscale=_DIVERGING,
            zmid=0,
            zmin=-bound,
            zmax=bound,
            xgap=2,
            ygap=2,
            text=[["" if v is None else f"{v * 100:.0f}" for v in row] for row in rows],
            texttemplate="%{text}",
            hovertemplate="%{y} · %{x}: %{z:.1%}<extra></extra>",
            colorbar={"tickformat": ".0%", "title": "return"},
        )
    )
    fig.update_layout(**_LAYOUT, height=90 + 46 * len(rows))
    return fig


def friction_chart(result: LabResult) -> go.Figure:
    """Small multiples (first window): trades per year, and implied average holding period."""
    window = result.windows[0].label
    colors, labels = strategy_colors(result), _labels(result)
    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("trades per year (buys + sells)", "average holding period (years)"),
        horizontal_spacing=0.18,
    )
    runs = [
        (spec, run) for spec in result.specs if (run := result.run(spec.name, window)) is not None
    ]
    for col, key, fmt in ((1, "trades_per_year", "{:.0f}"), (2, "avg_holding_years", "{:.1f}")):
        fig.add_trace(
            go.Bar(
                x=[run.metrics.get(key, 0.0) for _, run in runs],
                y=[labels[spec.name] for spec, _ in runs],
                orientation="h",
                marker={"color": [colors[spec.name] for spec, _ in runs]},
                text=[fmt.format(run.metrics.get(key, 0.0)) for _, run in runs],
                textposition="outside",
                showlegend=False,
                hovertemplate="%{y}: %{x:.1f}<extra></extra>",
            ),
            row=1,
            col=col,
        )
    fig.update_yaxes(autorange="reversed")
    fig.update_yaxes(showticklabels=False, row=1, col=2)
    fig.update_layout(**_LAYOUT, height=80 + 46 * len(runs))
    return fig


def _pct(value: float | None, digits: int = 1) -> str:
    return "" if value is None else f"{value * 100:.{digits}f}%"


def scorecard_table(result: LabResult) -> str:
    """Per-window metrics table: CAGR, max drawdown, Sharpe, trades/yr, average hold."""
    labels = _labels(result)
    head = (
        "<tr><th>window</th><th>strategy</th><th>CAGR</th><th>max drawdown</th>"
        "<th>Sharpe</th><th>trades/yr</th><th>avg hold (y)</th><th>end value</th></tr>"
    )
    body = []
    for window in reversed(result.windows):  # newest first
        for spec in result.specs:
            run = result.run(spec.name, window.label)
            if run is None:
                continue
            m = run.metrics
            body.append(
                f"<tr><td>{html.escape(window.label)}</td><td>{html.escape(labels[spec.name])}"
                f"</td><td>{_pct(m.get('cagr'))}</td><td>{_pct(m.get('max_drawdown'))}</td>"
                f"<td>{m.get('sharpe', 0.0):.2f}</td><td>{m.get('trades_per_year', 0.0):.1f}</td>"
                f"<td>{m.get('avg_holding_years', 0.0):.1f}</td>"
                f"<td>${m.get('end_value_cad', 0.0):,.0f}</td></tr>"
            )
    return f'<table class="grid">{head}{"".join(body)}</table>'


def account_table(result: LabResult) -> str:
    """Same strategy, two accounts, first window: what US-dividend withholding costs."""
    if not result.compare_account:
        return ""
    window = result.windows[0].label
    labels = _labels(result)
    a, b = result.account.upper(), result.compare_account.upper()
    head = (
        f"<tr><th>strategy</th><th>{a} CAGR</th><th>{b} CAGR</th>"
        f"<th>{a} US withholding paid</th><th>{b} US withholding paid</th></tr>"
    )
    body = []
    for spec in result.specs:
        run_a = result.run(spec.name, window)
        run_b = result.run(spec.name, window, result.compare_account)
        if run_a is None or run_b is None:
            continue
        body.append(
            f"<tr><td>{html.escape(labels[spec.name])}</td><td>{_pct(run_a.metrics.get('cagr'), 2)}"
            f"</td><td>{_pct(run_b.metrics.get('cagr'), 2)}</td>"
            f"<td>${run_a.metrics.get('tax_cad', 0.0):,.0f}</td>"
            f"<td>${run_b.metrics.get('tax_cad', 0.0):,.0f}</td></tr>"
        )
    return f'<table class="grid">{head}{"".join(body)}</table>'


def holdings_html(result: LabResult) -> str:
    """One card per strategy: what it would buy if the account were opened on the as-of day."""
    labels = _labels(result)
    cards = []
    for spec in result.specs:
        rows = result.holdings.get(spec.name, [])
        items = "".join(
            f"<tr><td>{html.escape(symbol)}</td><td>{weight * 100:.1f}%</td></tr>"
            for symbol, weight in rows
        )
        cash = 1.0 - sum(weight for _, weight in rows)
        if cash > 0.005:
            items += f'<tr class="muted"><td>cash</td><td>{cash * 100:.1f}%</td></tr>'
        cards.append(
            f'<div class="card"><h3>{html.escape(labels[spec.name])}</h3>'
            f'<table class="mini">{items}</table></div>'
        )
    return f'<div class="cards">{"".join(cards)}</div>'


def strategies_html(result: LabResult) -> str:
    items = "".join(
        f"<dt>{html.escape(spec.label)} <code>{html.escape(spec.name)}</code></dt>"
        f"<dd>{html.escape(spec.description)}</dd>"
        for spec in result.specs
    )
    return f'<dl class="strategies">{items}</dl>'


def coverage_line(result: LabResult) -> str:
    """The header's coverage clause for an index universe; empty for a curated run.

    Reads like " (93% of index member-days priced; 88% since 2025-10-05)". The second figure
    is the shortest window's, because the verdict weights it most.
    """
    if result.coverage is None:
        return ""
    recent = result.windows[-1].eval_start if result.windows else None
    tail = f"; {result.coverage.ratio_since(recent):.0%} since {recent:%Y-%m-%d}" if recent else ""
    return f" ({result.coverage.ratio:.0%} of index member-days priced{tail})"


def survivorship_note(result: LabResult) -> str:
    """The caveat that fits the universe: curated survivors, or a point-in-time index."""
    if result.coverage is None:
        return (
            "The universe is a curated list of today's companies. A run that starts in 2010 "
            "already &ldquo;knows&rdquo; these companies survived and grew, which flatters "
            "every stock-picking strategy &mdash; most of all the long window. The couch potato "
            "(broad ETFs) is the least biased line on the page; the gap to it is an upper "
            "bound on skill."
        )
    unpriced = len(result.coverage.unpriced)
    pending = len(result.coverage.pending)
    backfill = (
        f" <strong>The price backfill is still running: {pending} span(s) are not priced "
        "yet, so treat these results as provisional until it finishes.</strong>"
        if pending
        else ""
    )
    return (
        "Stocks come from the S&amp;P 500 as it stood on each decision date, including "
        "companies that were later acquired or went bankrupt; a holding that stops trading is "
        "cashed out at its last price. Free price sources could not price every past member "
        f"({unpriced} membership span(s) unpriced: mostly renamed tickers and older "
        "delistings), so some bias remains &mdash; the coverage figure at the top says how "
        "much of the index each result could actually see. Canadian stocks are held only "
        f"through the ETFs: no free point-in-time TSX source exists.{backfill}"
    )


def build_lab_report(result: LabResult, *, generated_at: datetime | None = None) -> str:
    """Assemble the self-contained HTML document for one lab run."""
    generated_at = generated_at or datetime.now(UTC)
    figures = [
        ("verdict", verdict_chart(result)),
        ("windows", window_cagr_chart(result)),
        ("equity", windowed_curves(result, drawdown=False)),
        ("drawdown", windowed_curves(result, drawdown=True)),
        ("years", calendar_heatmap(result)),
        ("friction", friction_chart(result)),
    ]
    rendered = {}
    for i, (key, fig) in enumerate(figures):
        rendered[key] = fig.to_html(full_html=False, include_plotlyjs=(i == 0))
    weights = ", ".join(f"{w.label} {w.weight:.0%}" for w in result.windows)
    mood_note = (
        f"News/social mood data is usable from about {result.mood_start:%Y-%m-%d}; before "
        "that, <em>Momentum + news mood</em> is identical to <em>Recent momentum</em>, so "
        "compare the two only in the windows after that date — and treat one year of news "
        "(two semi-annual reviews) as a hypothesis, not evidence."
        if result.mood_start
        else "No news/social mood data was available for this run."
    )
    return _DOCUMENT.format(
        account=html.escape(result.account.upper()),
        asof=f"{result.asof:%Y-%m-%d}" if result.asof else "?",
        universe_n=result.universe_n,
        stamp=generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        weights=html.escape(weights),
        mood_note=mood_note,
        universe_label=html.escape(result.universe_label),
        coverage_line=coverage_line(result),
        survivorship_note=survivorship_note(result),
        strategies=strategies_html(result),
        scorecard=scorecard_table(result),
        accounts=account_table(result),
        holdings=holdings_html(result),
        **rendered,
    )


_DOCUMENT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strategy lab</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 2rem auto;
         max-width: 1100px; padding: 0 1rem; color: #1a1a1a; background: #fcfcfb; }}
  h1 {{ margin-bottom: 0.2rem; }}
  h2 {{ margin-top: 2.2rem; border-bottom: 1px solid #e5e5e5; padding-bottom: 0.3rem; }}
  h3 {{ margin: 0 0 0.4rem; font-size: 0.95rem; }}
  .caption {{ color: #52514e; font-size: 0.92rem; max-width: 75ch; }}
  .meta {{ color: #888; font-size: 0.8rem; }}
  .info {{ background: #f4f6f8; border-left: 3px solid #2a78d6; padding: 0.7rem 1rem;
          border-radius: 4px; font-size: 0.9rem; }}
  table.grid {{ border-collapse: collapse; width: 100%; font-size: 0.85rem;
               font-variant-numeric: tabular-nums; }}
  table.grid th, table.grid td {{ padding: 0.35rem 0.6rem; text-align: right;
                                  border-bottom: 1px solid #eee; }}
  table.grid th {{ background: #f4f6f8; }}
  table.grid td:nth-child(-n+2), table.grid th:nth-child(-n+2) {{ text-align: left; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
           gap: 0.8rem; }}
  .card {{ border: 1px solid #e5e5e5; border-radius: 6px; padding: 0.7rem; }}
  table.mini {{ width: 100%; font-size: 0.82rem; font-variant-numeric: tabular-nums; }}
  table.mini td:last-child {{ text-align: right; }}
  tr.muted td {{ color: #888; }}
  dl.strategies dt {{ font-weight: 600; margin-top: 0.6rem; }}
  dl.strategies dd {{ margin: 0.1rem 0 0 0; color: #52514e; font-size: 0.9rem; }}
  .scroll {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>Registered-account strategy lab</h1>
<p class="caption">Account simulated: <strong>{account}</strong> · data to {asof} ·
universe: <strong>{universe_label}</strong>, {universe_n} instruments{coverage_line} · long-only, no margin, no penny stocks (price ≥ $5, ≥ 2 years
listed, liquid), every trade costed (commission, spread, slippage, churn penalty, CAD↔USD
conversion) and non-recoverable US-dividend withholding charged. Each window is a fresh
start: "if I had opened the account then".</p>
<p class="meta">Generated {stamp}</p>

<h2>Verdict, weighted to recent data</h2>
<p class="caption">Each strategy's CAGR minus the couch potato's, averaged across windows
with weights {weights}. Positive = earned its extra complexity.</p>
{verdict}

<h2>Every window</h2>
<p class="caption">A strategy that only wins in the long window won in a world that may be
gone; look for bars that hold up in the recent windows.</p>
{windows}

<h2>Growth of 100</h2>
{equity}
<h2>Drawdowns</h2>
<p class="caption">The pain you would have sat through. Same window buttons as above.</p>
{drawdown}

<h2>Calendar-year returns</h2>
<p class="caption">From the longest window; newest year on the right (the current year is
partial).</p>
{years}

<h2>Friction</h2>
<p class="caption">CRA weighs trading frequency and holding period when deciding whether a
registered account is "carrying on a business". Fewer trades and longer holds keep a
strategy on the investing side of that line.</p>
{friction}

<h2>Scorecard</h2>
<div class="scroll">{scorecard}</div>

<h2>Account type</h2>
<p class="caption">Same strategy, longest window, two accounts: the difference is US-dividend
withholding (15% unrecoverable in a TFSA/FHSA, treaty-exempt in an RRSP/LIRA for US-listed
securities held directly).</p>
<div class="scroll">{accounts}</div>

<h2>If you opened the account today</h2>
<p class="caption">Each strategy's day-one book as of {asof} — the starting point for paper
trading, not an order list.</p>
{holdings}

<h2>The strategies</h2>
{strategies}

<h2>Read this before trusting any number above</h2>
<div class="info">
<p><strong>Survivorship bias.</strong> {survivorship_note}</p>
<p><strong>Parameters were set for turnover, not tuned for return.</strong> Review cadences and
hold buffers were chosen to keep trading to roughly two dozen trades a year or fewer.</p>
<p><strong>Mood data.</strong> {mood_note}</p>
<p><strong>Not advice.</strong> Engineering output for a paper account. Confirm the tax
treatment of each account type with a CPA before funding it.</p>
</div>
</body>
</html>
"""
