"""Local results dashboard (Streamlit) — visualize persisted backtest_runs and launch new runs.

Split in two so the core package never needs the UI dependencies:

- [data.py](data.py) — pure DB-in/frame-out helpers + the run launcher. No streamlit import;
  unit-tested against in-memory SQLite (tests/test_dashboard_data.py).
- [app.py](app.py) — the Streamlit page. Imports streamlit/plotly, so it only runs under the
  ``[dashboard]`` extra (``uv sync --extra dashboard``); launched via ``ibkr-trader dashboard``.
"""
