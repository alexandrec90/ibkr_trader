"""The local UI's HTTP surface: JSON over ``views`` plus the built frontend.

Needs the ``[ui]`` extra (fastapi + uvicorn). Started by ``ibkr-trader ui`` on 127.0.0.1
and stopped with its terminal, like the MLflow viewer: nothing stays resident, which is
the lesson of the Streamlit dashboard this replaces (TODO.md, 2026-07-19).

Everything is a GET except ``POST /api/plan``, which redeems the Questrade token to build
an advisory plan. Nothing here transmits an order to any broker. Two guards keep another
web page in the same browser from driving or reading it: the Host header must name the
loopback (no DNS rebinding), and the POST must be ``application/json``, which a
cross-origin page can only send after a CORS preflight this app never grants.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ibkr_trader.web import views

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
DB_DOWN = "cannot reach the database; start it with `docker compose up -d db`"

SessionFactory = Callable[[], AbstractContextManager[Session]]


class PlanRequest(BaseModel):
    """The `questrade plan` options, with the CLI's defaults and bounds."""

    strategy: str = Field(min_length=1)
    min_order: float = Field(1000.0, ge=0)
    max_position: float = Field(0.20, ge=0.01, le=1.0)
    max_new: int = Field(3, ge=0)
    exits: bool = False


def run_plan(request: PlanRequest, csv_path: Path) -> dict[str, Any]:
    """Build the plan, write its CSV and JSON beside each other, and return the JSON.

    Raises ``advisory.service.PlanError`` with the owner-facing reason.
    """
    from ibkr_trader.advisory.planner import PlanConfig
    from ibkr_trader.advisory.report import write_csv, write_json
    from ibkr_trader.advisory.service import make_plan

    config = PlanConfig(
        min_order_cad=request.min_order,
        max_position_pct=request.max_position,
        max_new_names=request.max_new,
        exits=request.exits,
    )
    result = make_plan(request.strategy, config, datetime.now(tz=UTC).date())
    write_csv(result.plans, csv_path)
    return write_json(result, csv_path.with_suffix(".json"), config)


def _placeholder() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>ibkr-trader UI</title><body style='font-family:system-ui'>"
        "<h1>The frontend is not built</h1><p>Run <code>npm --prefix frontend ci</code> and "
        "<code>npm --prefix frontend run build</code>, or "
        "<code>python scripts/ui-task.py</code>, which does both. The JSON API is live under "
        "<code>/api/</code>.</p></body>",
        status_code=503,
    )


def _utc_today() -> date:
    return datetime.now(tz=UTC).date()


@dataclass(frozen=True)
class AppConfig:
    """Everything the app reads or calls, so tests inject in-memory replacements."""

    session_factory: SessionFactory | None = None  # None: the real Postgres session
    models_dir: Path = Path("models")
    reports_dir: Path = Path("reports")
    static_dir: Path | None = FRONTEND_DIST
    plan_runner: Callable[[PlanRequest, Path], dict[str, Any]] = run_plan
    today: Callable[[], date] = _utc_today
    allowed_hosts: tuple[str, ...] = LOOPBACK_HOSTS

    @property
    def plan_csv(self) -> Path:
        return self.reports_dir / "questrade-plan.csv"

    def read(self, fn: Callable[[Session], Any]) -> Any:
        """Run ``fn`` in one short session."""
        factory = self.session_factory
        if factory is None:
            from ibkr_trader.db.session import get_session

            factory = get_session
        with factory() as session:
            return fn(session)


def _page_routes(cfg: AppConfig) -> APIRouter:
    """The read-only JSON behind the Models and Paper trading pages."""
    router = APIRouter(prefix="/api")

    @router.get("/runs")
    def runs() -> list[dict[str, Any]]:
        return cfg.read(views.backtest_runs)

    @router.get("/curves")
    def curves(ids: str = Query("", description="comma-separated backtest run ids")) -> Any:
        try:
            run_ids = [int(part) for part in ids.split(",") if part.strip()]
            return cfg.read(lambda s: views.backtest_curves(s, run_ids))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/model")
    def model() -> dict[str, Any] | None:
        return views.trained_model(cfg.models_dir)

    @router.get("/shadow")
    def shadow() -> dict[str, Any]:
        return cfg.read(
            lambda s: {
                "books": views.shadow_books(s),
                "performance": views.shadow_performance(s, asof=cfg.today()),
            }
        )

    @router.get("/paper")
    def paper() -> dict[str, Any]:
        return cfg.read(views.paper_orders)

    return router


def _plan_routes(cfg: AppConfig) -> APIRouter:
    """The Order recommendations page: the last plan, and the one action that builds one."""
    router = APIRouter(prefix="/api")

    @router.get("/plan")
    def latest_plan() -> dict[str, Any]:
        try:
            plan = views.latest_plan(cfg.plan_csv.with_suffix(".json"))
        except ValueError as exc:
            raise HTTPException(500, str(exc)) from exc
        return {"plan": plan, "strategies": cfg.read(views.snapshot_strategies)}

    @router.post("/plan")
    async def new_plan(request: Request) -> dict[str, Any]:
        from ibkr_trader.advisory.service import PlanError

        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send the plan options as application/json")
        try:
            body = PlanRequest.model_validate(await request.json())
        except ValueError as exc:  # malformed JSON, or pydantic's ValidationError
            raise HTTPException(422, str(exc)) from exc
        try:
            return {"plan": await run_in_threadpool(cfg.plan_runner, body, cfg.plan_csv)}
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router


def _report_routes(cfg: AppConfig) -> APIRouter:
    """The generated HTML reports under ``reports/``, and nothing outside it."""
    router = APIRouter()

    @router.get("/api/reports")
    def reports() -> list[dict[str, Any]]:
        return views.report_files(cfg.reports_dir)

    @router.get("/reports/{path:path}")
    def report(path: str) -> FileResponse:
        root = cfg.reports_dir.resolve()
        target = (root / path).resolve()
        if target.suffix != ".html" or not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "no such report")
        return FileResponse(target)

    return router


async def _db_down(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": DB_DOWN}, status_code=503)


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    """Build the app: host guard, the three routers, then the built frontend at ``/``."""
    cfg = cfg or AppConfig()
    app = FastAPI(title="ibkr-trader", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.allowed_hosts))
    app.add_exception_handler(OperationalError, _db_down)
    for router in (_page_routes(cfg), _plan_routes(cfg), _report_routes(cfg)):
        app.include_router(router)
    if cfg.static_dir is not None and (cfg.static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="frontend")
    else:
        app.add_api_route("/", _placeholder, include_in_schema=False)
    return app
