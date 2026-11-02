"""Rendering an order plan: terminal lines and the CSV the owner works through."""

import csv
import json
from datetime import date, datetime

from ibkr_trader.accounts import AccountType
from ibkr_trader.advisory.planner import AccountPlan, AccountState, OrderTicket, PlanConfig
from ibkr_trader.advisory.report import (
    account_line,
    format_plans,
    plan_notes,
    plan_payload,
    write_csv,
    write_json,
)
from ibkr_trader.advisory.service import PlanResult
from ibkr_trader.advisory.targets import StrategyTargets, TargetWeight

TODAY = date(2026, 10, 18)
STATE = AccountState(
    number="12345282",
    kind=AccountType.TFSA,
    label="TFSA",
    equity_cad=20_000.0,
    usdcad=1.4,
    available={"CAD": 4_200.0, "USD": 0.0},
)
TICKET = OrderTicket(
    "TFSA ...282", "Buy", "XIC.TO", 40, 40.95, "CAD", date(2026, 10, 25), "new toward 50%"
)


def _plan(orders: list[OrderTicket], notes: list[str] | None = None) -> AccountPlan:
    return AccountPlan(STATE, orders, notes or [], {"CAD": 2_562.0, "USD": 0.0})


def test_account_line_masks_the_number_and_hides_empty_currencies():
    line = account_line(STATE)
    assert line.startswith("TFSA ...282  equity CAD 20,000")
    assert "CAD 4,200.00" in line and "USD" not in line
    assert "12345282" not in line


def test_format_plans_says_advisory_and_lists_tickets_then_notes():
    lines = format_plans(
        [_plan([TICKET], ["SPY: wants USD 5,000"]), _plan([])], "title", ["stale snapshot"]
    )
    text = "\n".join(lines)
    assert lines[0] == "title"
    assert "ADVISORY ONLY" in lines[1]
    assert "  ! stale snapshot" in lines
    assert "XIC.TO" in text and "40.95" in text and "until 2026-10-25" in text
    assert "  - SPY: wants USD 5,000" in lines
    assert "  (no orders)" in lines
    assert "cash left after every buy fills: CAD 2,562.00" in text


def test_plan_notes_warn_about_a_stale_empty_or_incomplete_snapshot():
    fresh = StrategyTargets("core", TODAY, [TargetWeight("XIC", "CAD", 1.0, True, None)])
    stale = StrategyTargets("core", date(2026, 8, 1), [], ("9",))
    assert plan_notes(fresh, TODAY) == []
    notes = plan_notes(stale, TODAY)
    assert any("78 days old" in n for n in notes)
    assert any("1 snapshot name(s)" in n for n in notes)
    assert any("holds nothing" in n for n in notes)


def test_write_csv_has_one_row_per_ticket_with_order_entry_fields(tmp_path):
    path = tmp_path / "reports" / "plan.csv"
    assert write_csv([_plan([TICKET, TICKET]), _plan([])], path) == 2
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 2
    assert rows[0]["limit_price"] == "40.95"
    assert rows[0]["order_type"] == "Limit"
    assert rows[0]["time_in_force"] == "GoodTillDate"
    assert rows[0]["notional"] == "1638.00"


RESULT = PlanResult("core", date(2026, 10, 1), TODAY, [_plan([TICKET]), _plan([])], ["stale"])


def test_plan_payload_masks_accounts_and_carries_tickets_notes_and_options():
    payload = plan_payload(RESULT, PlanConfig(max_new_names=1), generated_at=datetime(2026, 1, 1))

    assert payload["title"] == RESULT.title
    assert payload["snapshot_asof"] == "2026-10-01"
    assert payload["notes"] == ["stale"]
    assert payload["config"]["max_new_names"] == 1
    first = payload["accounts"][0]
    assert first["account"] == "TFSA ...282" and first["kind"] == "tfsa"
    assert first["orders"][0]["notional"] == 1638.0
    assert first["orders"][0]["good_until"] == "2026-10-25"
    assert payload["accounts"][1]["orders"] == []
    assert "12345282" not in json.dumps(payload)


def test_write_json_writes_what_it_returns(tmp_path):
    path = tmp_path / "reports" / "plan.json"
    payload = write_json(RESULT, path, PlanConfig())
    assert json.loads(path.read_text(encoding="utf-8")) == payload
