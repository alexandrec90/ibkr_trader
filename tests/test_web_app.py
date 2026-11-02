"""The UI's HTTP surface (web/app.py): routing, guards and failure mapping.

The pages' data is covered in test_web_views.py; this file checks what the app adds on
top. Needs the [ui] extra, which CI installs.
"""

from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ibkr_trader.advisory.service import PlanError
from ibkr_trader.db.models import BacktestRun, Base
from ibkr_trader.web.app import AppConfig, PlanRequest, create_app

TEST_HOSTS = ("testserver", "127.0.0.1", "localhost")


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


def _factory(engine):
    @contextmanager
    def open_session():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    return open_session


def _client(engine, tmp_path, **kwargs) -> TestClient:
    app = create_app(
        AppConfig(
            session_factory=_factory(engine),
            models_dir=tmp_path / "models",
            reports_dir=tmp_path / "reports",
            static_dir=kwargs.pop("static_dir", None),
            today=lambda: date(2026, 10, 26),
            allowed_hosts=TEST_HOSTS,
            **kwargs,
        )
    )
    return TestClient(app)


def test_runs_and_empty_pages_answer_on_an_empty_database(engine, tmp_path):
    client = _client(engine, tmp_path)

    assert client.get("/api/runs").json() == []
    assert client.get("/api/model").json() is None
    assert client.get("/api/paper").json() == {"orders": []}
    assert client.get("/api/plan").json() == {"plan": None, "strategies": []}
    assert client.get("/api/reports").json() == []
    shadow = client.get("/api/shadow").json()
    assert shadow["books"] == []
    assert shadow["performance"]["asof"] == "2026-10-26"


def test_curves_serve_the_selected_runs(engine, tmp_path):
    with Session(engine) as session:
        session.add(
            BacktestRun(
                id=7,
                strategy="core",
                params={"account": "tfsa", "benchmark": "XEQT"},
                start=datetime(2010, 1, 4, tzinfo=UTC),
                end=datetime(2010, 1, 5, tzinfo=UTC),
                metrics={"equity_curve": [["2010-01-04", 5.0], ["2010-01-05", 6.0]]},
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        session.commit()

    (curve,) = _client(engine, tmp_path).get("/api/curves", params={"ids": "7"}).json()

    assert curve["run_id"] == 7
    assert [p[1] for p in curve["points"]] == [100.0, 120.0]


@pytest.mark.parametrize("ids", ["abc", ",".join(str(i) for i in range(9))])
def test_bad_curve_selections_are_400s(engine, tmp_path, ids):
    response = _client(engine, tmp_path).get("/api/curves", params={"ids": ids})
    assert response.status_code == 400


def test_a_foreign_host_header_is_refused(engine, tmp_path):
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 must read nothing."""
    response = _client(engine, tmp_path).get("/api/runs", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_a_stopped_database_is_a_503_that_says_how_to_start_it(tmp_path):
    @contextmanager
    def down():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        yield

    app = create_app(
        AppConfig(
            session_factory=down, reports_dir=tmp_path, static_dir=None, allowed_hosts=TEST_HOSTS
        )
    )
    response = TestClient(app).get("/api/runs")

    assert response.status_code == 503
    assert "docker compose up -d db" in response.json()["detail"]


# --- the plan action ------------------------------------------------------------------


def test_a_plan_runs_with_the_request_and_writes_beside_the_reports(engine, tmp_path):
    calls = []

    def runner(request: PlanRequest, csv_path):
        calls.append((request, csv_path))
        return {"strategy": request.strategy, "accounts": []}

    client = _client(engine, tmp_path, plan_runner=runner)
    response = client.post("/api/plan", json={"strategy": "core", "max_new": 1})

    assert response.status_code == 200
    assert response.json() == {"plan": {"strategy": "core", "accounts": []}}
    request, csv_path = calls[0]
    assert (request.max_new, request.min_order, request.exits) == (1, 1000.0, False)
    assert csv_path == tmp_path / "reports" / "questrade-plan.csv"


def test_a_plan_post_that_is_not_json_is_refused_before_the_token_is_spent(engine, tmp_path):
    """A cross-origin form can send text/plain without a preflight; it must do nothing."""
    calls = []
    client = _client(engine, tmp_path, plan_runner=lambda r, p: calls.append(r))

    response = client.post(
        "/api/plan", content='{"strategy": "core"}', headers={"content-type": "text/plain"}
    )

    assert response.status_code == 415
    assert calls == []


@pytest.mark.parametrize(
    "body", ['{"strategy": ""}', '{"strategy": "core", "max_position": 2}', "{not json"]
)
def test_invalid_plan_options_are_422s(engine, tmp_path, body):
    client = _client(engine, tmp_path, plan_runner=lambda r, p: pytest.fail("ran"))
    response = client.post("/api/plan", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 422


def test_a_plan_failure_is_a_400_with_the_owner_facing_reason(engine, tmp_path):
    def runner(request, csv_path):
        raise PlanError("QUESTRADE_TOKEN is not set")

    response = _client(engine, tmp_path, plan_runner=runner).post(
        "/api/plan", json={"strategy": "core"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "QUESTRADE_TOKEN is not set"


def test_the_latest_plan_is_read_back_from_its_json(engine, tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "questrade-plan.json").write_text('{"strategy": "core"}')
    assert _client(engine, tmp_path).get("/api/plan").json()["plan"] == {"strategy": "core"}


def test_run_plan_writes_the_csv_and_json_and_returns_the_json(monkeypatch, tmp_path):
    from ibkr_trader.advisory import service
    from ibkr_trader.web import app as web_app

    seen = {}

    def fake_make_plan(strategy, config, today):
        seen["config"] = config
        return service.PlanResult(strategy, date(2026, 10, 1), today, [], ["stale"])

    monkeypatch.setattr(service, "make_plan", fake_make_plan)
    csv_path = tmp_path / "reports" / "questrade-plan.csv"

    payload = web_app.run_plan(PlanRequest(strategy="core", max_position=0.3), csv_path)

    assert payload["strategy"] == "core" and payload["notes"] == ["stale"]
    assert seen["config"].max_position_pct == 0.3
    assert csv_path.is_file()
    assert csv_path.with_suffix(".json").is_file()


# --- files ----------------------------------------------------------------------------


def test_reports_are_served_but_nothing_outside_them(engine, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "lab-report.html").write_text("<h1>lab</h1>")
    (reports / "questrade-plan.csv").write_text("secret,ish")
    (tmp_path / "outside.html").write_text("no")
    client = _client(engine, tmp_path)

    assert client.get("/reports/lab-report.html").text == "<h1>lab</h1>"
    assert client.get("/reports/questrade-plan.csv").status_code == 404
    assert client.get("/reports/..%2Foutside.html").status_code == 404


def test_the_built_frontend_is_served_at_the_root(engine, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")

    response = _client(engine, tmp_path, static_dir=dist).get("/")

    assert response.status_code == 200
    assert "root" in response.text


def test_an_unbuilt_frontend_says_how_to_build_it(engine, tmp_path):
    response = _client(engine, tmp_path, static_dir=tmp_path / "nowhere").get("/")
    assert response.status_code == 503
    assert "npm --prefix frontend run build" in response.text
