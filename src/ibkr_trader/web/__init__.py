"""Local web UI — models, order recommendations and paper decisions in one place.

- [views.py](views.py) — pure DB/filesystem-in, JSON-out functions, one per page. No
  FastAPI import; unit-tested against in-memory SQLite (tests/test_web_views.py).
- [app.py](app.py) — the FastAPI routes over them plus the built ``frontend/dist``. Needs
  the ``[ui]`` extra; served by ``ibkr-trader ui`` on 127.0.0.1 until its terminal closes.

The React app lives in ``frontend/`` (Vite, devkit's ``[frontend]`` tier).
"""
