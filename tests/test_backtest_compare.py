from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.backtest.compare import compare_runs
from ibkr_trader.db.models import BacktestRun, Base


def _make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _add_run(session: Session, strategy: str, sharpe: float | None, *, horizon: str = "1d") -> None:
    session.add(
        BacktestRun(
            strategy=strategy,
            params={"model_version": "1", "horizon": horizon},
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 6, 30, tzinfo=UTC),
            metrics={} if sharpe is None else {"sharpe": sharpe, "max_drawdown": -0.1},
            created_at=datetime(2024, 7, 1, tzinfo=UTC),
        )
    )


def test_compare_ranks_by_sharpe_descending():
    session = _make_session()
    _add_run(session, "momentum_baseline", 0.5)
    _add_run(session, "gbm_1d", 1.5)
    _add_run(session, "gbm_20d", 1.0)
    session.commit()

    ranked = compare_runs(session)

    assert [run.strategy for run in ranked] == ["gbm_1d", "gbm_20d", "momentum_baseline"]


def test_compare_filters_by_strategy_and_limits():
    session = _make_session()
    _add_run(session, "gbm_1d", 1.5)
    _add_run(session, "gbm_1d", 0.9)
    _add_run(session, "momentum_baseline", 0.5)
    session.commit()

    ranked = compare_runs(session, strategy="gbm_1d", limit=1)

    assert len(ranked) == 1
    assert ranked[0].strategy == "gbm_1d"
    assert ranked[0].metrics["sharpe"] == 1.5


def test_compare_puts_runs_missing_the_metric_last():
    session = _make_session()
    _add_run(session, "gbm_1d", 1.5)
    _add_run(session, "broken", None)
    session.commit()

    ranked = compare_runs(session, sort_by="sharpe")

    assert ranked[-1].strategy == "broken"
