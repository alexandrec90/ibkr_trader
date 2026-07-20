"""Local results view — render persisted backtest_runs as a static HTML report.

Split in two so the core package never needs the plotting dependency:

- [data.py](data.py) — pure DB-in/frame-out helpers + the run launcher. No plotly import;
  unit-tested against in-memory SQLite (tests/test_dashboard_data.py).
- [report.py](report.py) — builds the self-contained HTML (leaderboard + Plotly equity/drawdown
  charts). Imports plotly, so it only runs under the ``[report]`` extra
  (``uv sync --extra report``); rendered via ``ibkr-trader report``. No server, nothing resident.
"""
