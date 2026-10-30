"""Render an order plan for the terminal and as a CSV the owner can work through."""

import csv
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ibkr_trader.advisory.planner import AccountPlan, AccountState, PlanConfig
from ibkr_trader.advisory.targets import STALE_SNAPSHOT_DAYS, StrategyTargets

if TYPE_CHECKING:
    from ibkr_trader.advisory.service import PlanResult

CSV_FIELDS = (
    "account",
    "side",
    "symbol",
    "quantity",
    "limit_price",
    "currency",
    "notional",
    "order_type",
    "time_in_force",
    "good_until",
    "note",
)


def _money(amounts: dict[str, float]) -> str:
    shown = [f"{ccy} {value:,.2f}" for ccy, value in sorted(amounts.items()) if value >= 0.005]
    return " · ".join(shown) or "none"


def account_line(state: AccountState) -> str:
    return (
        f"{state.masked}  equity CAD {state.equity_cad:,.0f}  "
        f"available {_money(state.available)}  holdings {len(state.holdings)}"
    )


def plan_notes(targets: StrategyTargets, today: date) -> list[str]:
    """Warnings about the target book itself, shown above every account."""
    notes = []
    age = (today - targets.asof).days
    if age > STALE_SNAPSHOT_DAYS:
        notes.append(
            f"the {targets.strategy} snapshot is {age} days old; run 'Snapshot: Run Monthly' "
            "for a current book"
        )
    if targets.missing:
        notes.append(f"{len(targets.missing)} snapshot name(s) no longer in the database")
    if not targets.weights:
        notes.append(f"the {targets.strategy} snapshot holds nothing (all cash)")
    return notes


def format_plans(plans: list[AccountPlan], title: str, notes: list[str]) -> list[str]:
    """Plain-text lines: one block per account, tickets first, then why anything was skipped."""
    lines = [
        title,
        "ADVISORY ONLY: nothing was sent to Questrade. Enter each order yourself, as a "
        "Limit order, Good Till Date.",
    ]
    lines += [f"  ! {note}" for note in notes]
    for plan in plans:
        lines += ["", account_line(plan.account)]
        for o in plan.orders:
            lines.append(
                f"  {o.side:<4} {o.symbol:<10} {o.quantity:>6} @ {o.limit_price:>10,.2f} "
                f"{o.currency}  = {o.notional:>11,.2f}  until {o.good_until}  {o.note}"
            )
        if not plan.orders:
            lines.append("  (no orders)")
        lines += [f"  - {note}" for note in plan.notes]
        if plan.orders:
            lines.append(f"  cash left after every buy fills: {_money(plan.cash_left)}")
    return lines


def plan_payload(
    result: "PlanResult", config: PlanConfig, *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """The whole plan as JSON-ready data. Accounts appear masked only, as in the terminal."""
    generated_at = generated_at or datetime.now(tz=UTC)
    accounts = []
    for plan in result.plans:
        state = plan.account
        accounts.append(
            {
                "account": state.masked,
                "kind": state.kind.value,
                "equity_cad": state.equity_cad,
                "available": state.available,
                "holdings": len(state.holdings),
                "orders": [
                    {
                        "side": o.side,
                        "symbol": o.symbol,
                        "quantity": o.quantity,
                        "limit_price": o.limit_price,
                        "currency": o.currency,
                        "notional": o.notional,
                        "good_until": o.good_until.isoformat(),
                        "note": o.note,
                    }
                    for o in plan.orders
                ],
                "notes": plan.notes,
                "cash_left": plan.cash_left,
            }
        )
    return {
        "title": result.title,
        "strategy": result.strategy,
        "snapshot_asof": result.snapshot_asof.isoformat(),
        "today": result.today.isoformat(),
        "generated_at": generated_at.isoformat(),
        # Tuples as lists, so the payload equals what reading the JSON back yields.
        "config": {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(config).items()},
        "notes": result.notes,
        "accounts": accounts,
    }


def write_json(result: "PlanResult", path: Path, config: PlanConfig) -> dict[str, Any]:
    """Write :func:`plan_payload` to ``path`` (parent created) and return it."""
    payload = plan_payload(result, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_csv(plans: list[AccountPlan], path: Path) -> int:
    """Write every ticket; returns how many. The parent directory is created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [o for plan in plans for o in plan.orders]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for o in rows:
            writer.writerow(
                {
                    "account": o.account,
                    "side": o.side,
                    "symbol": o.symbol,
                    "quantity": o.quantity,
                    "limit_price": f"{o.limit_price:.4f}".rstrip("0").rstrip("."),
                    "currency": o.currency,
                    "notional": f"{o.notional:.2f}",
                    "order_type": "Limit",
                    "time_in_force": "GoodTillDate",
                    "good_until": o.good_until.isoformat(),
                    "note": o.note,
                }
            )
    return len(rows)
