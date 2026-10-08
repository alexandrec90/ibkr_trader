"""Tell the owner, on their phone, when a major IPO moves a stage.

The lake's ``ipo_events`` table holds two kinds of row: public registration filings from SEC
EDGAR (an S-1/F-1 made public, an amendment, the final 424B4 prospectus, a withdrawal) and
IPO-calendar deals from Finnhub (expected, priced, withdrawn). This module decides which of
those rows are worth a push notification and sends each one once, through the same ntfy
topic the gateway login watch uses.

An event is **major** when the company is on the watchlist (``IPO_WATCHLIST`` -- names, matched
on whole words so "Anthropos Capital" never fires for "Anthropic") or when the calendar puts
the deal at ``IPO_ALERT_MIN_DEAL_USD`` or more. Amendments are left out of the default stages:
a mega-IPO files several, and the stage changes worth a buzz are filed, expected, priced and
withdrawn.

One alert per (company, stage, expected date), not per row: EDGAR and Finnhub each report the
same filing, and a company going public posts its earlier DRS beside its S-1 on the same day.
The company is its name minus legal suffixes, so "Anthropic, PBC" and "Anthropic PBC" are one.

Freshness is ``first_seen_at`` -- when the lake first saw the row -- not ``filed_at``: a DRS
made public carries its weeks-old confidential submission date. Calendar deals are judged by
their expected date instead.

Sent alerts are remembered in a small JSON ledger under ``logs/`` rather than on the instance
(unlike ``GatewayWatch``): a `serve` restart re-alerting every live IPO would be noise, where a
repeated gateway alert is the point. An alert that fails to send is not recorded, so the next
run tries it again.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ibkr_trader.gateway_watch import Notification

logger = logging.getLogger(__name__)

#: Stages a default install alerts on. 'amended' is the one left out: see the module docstring.
DEFAULT_ALERT_STAGES: tuple[str, ...] = ("filed", "expected", "priced", "withdrawn")
#: ntfy tag (an emoji shortcode) that sets IPO pushes apart from the gateway's warning sign.
ALERT_TAGS = "chart_with_upwards_trend"

#: A row first seen longer ago than this is history, not news -- it never alerts.
FILING_LOOKBACK = timedelta(days=14)
#: A calendar deal whose expected date is further in the past than this is over.
EXPECTED_GRACE = timedelta(days=1)
#: Ledger entries older than this are dropped, so the file stays small.
LEDGER_RETENTION = timedelta(days=180)

#: Trailing words dropped when deciding two names are one company.
_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc"}
    | {"lp", "plc", "pbc", "sa", "ag", "nv", "group", "holdings", "holding"}
)

_STAGE_WORDS = {
    "filed": "filed publicly",
    "amended": "amended its filing",
    "expected": "IPO scheduled",
    "priced": "IPO priced",
    "withdrawn": "IPO withdrawn",
}


@dataclass(frozen=True)
class IpoEventView:
    """The columns of one ``ipo_events`` row this module reads."""

    source: str
    external_id: str
    company_name: str
    stage: str
    symbol: str | None = None
    exchange: str | None = None
    form_type: str | None = None
    filed_at: datetime | None = None
    expected_date: date | None = None
    price_low: float | None = None
    price_high: float | None = None
    shares: int | None = None
    deal_value_usd: float | None = None
    url: str | None = None
    first_seen_at: datetime | None = None


def name_tokens(text: str) -> list[str]:
    """Lower-case alphanumeric words: 'Space-X Corp.' -> ['space', 'x', 'corp']."""
    return re.findall(r"[a-z0-9]+", text.lower())


def company_key(name: str) -> str:
    """The company a name refers to: 'OpenAI Group PBC' and 'OpenAI' are both 'openai'."""
    tokens = name_tokens(name)
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _contains_run(tokens: Sequence[str], run: Sequence[str]) -> bool:
    width = len(run)
    return width > 0 and any(
        list(tokens[i : i + width]) == list(run) for i in range(len(tokens) - width + 1)
    )


def watchlist_match(company_name: str, watchlist: Iterable[str]) -> str | None:
    """The watchlist entry naming this company, or None.

    An entry matches as a run of whole words, either as written or run together, so "SpaceX",
    "Space X" and "Space-X" all match a filer called "SpaceX" and "Open AI" matches "OpenAI
    Group PBC" -- but "Discord" does not match "Discordia Mining".
    """
    tokens = name_tokens(company_name)
    for entry in watchlist:
        words = name_tokens(entry)
        if not words:
            continue
        if _contains_run(tokens, words) or _contains_run(tokens, ["".join(words)]):
            return entry
    return None


def major_reason(event: IpoEventView, watchlist: Iterable[str], min_deal_usd: float) -> str | None:
    """Why this event is major -- a watchlist entry or the deal size -- or None when it is not."""
    entry = watchlist_match(event.company_name, watchlist)
    if entry is not None:
        return f"watchlist: {entry}"
    if min_deal_usd > 0 and event.deal_value_usd is not None:
        if event.deal_value_usd >= min_deal_usd:
            return f"deal size ${event.deal_value_usd / 1e9:.1f}B"
    return None


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)  # SQLite: naive


def is_current(event: IpoEventView, now: datetime) -> bool:
    """Whether the event is still news: a deal not yet behind us, or a row first seen lately.

    A calendar deal's expected date decides alone. Otherwise ``first_seen_at`` does, with
    ``filed_at`` only as a fallback for a row that lacks it.
    """
    if event.expected_date is not None:
        return event.expected_date >= (now - EXPECTED_GRACE).date()
    seen = event.first_seen_at or event.filed_at
    return seen is not None and _utc(seen) >= now - FILING_LOOKBACK


def alert_key(event: IpoEventView) -> str:
    """One alert per (company, stage, expected date): a re-dated deal alerts again, a re-poll,
    the same filing on a second source, or a DRS beside its S-1 does not."""
    expected = event.expected_date.isoformat() if event.expected_date else "-"
    return f"{company_key(event.company_name)}:{event.stage}:{expected}"


def _price_text(event: IpoEventView) -> str | None:
    low, high = event.price_low, event.price_high
    if low is None and high is None:
        return None
    if low is not None and high is not None and low != high:
        return f"${low:,.2f}-${high:,.2f}"
    return f"${(low if low is not None else high):,.2f}"


def format_alert(event: IpoEventView, reason: str) -> tuple[str, str]:
    """The notification's (title, body). Public data only; nothing account-shaped."""
    stage = _STAGE_WORDS.get(event.stage, event.stage)
    title = f"{event.company_name}: {stage}"
    details: list[str] = []
    if event.form_type:
        details.append(f"Form {event.form_type}")
    if event.expected_date:
        details.append(f"expected {event.expected_date.isoformat()}")
    elif event.filed_at:
        details.append(f"filed {event.filed_at.date().isoformat()}")
    listing = " ".join(
        part
        for part in (
            f"on {event.exchange}" if event.exchange else "",
            f"as {event.symbol}" if event.symbol else "",
        )
        if part
    )
    if listing:
        details.append(listing)
    price = _price_text(event)
    if price:
        details.append(price)
    if event.shares:
        details.append(f"{event.shares:,} shares")
    if event.deal_value_usd:
        details.append(f"~${event.deal_value_usd / 1e9:.2f}B")
    body = ", ".join(details) or "no deal details yet"
    return title, f"{body}. ({reason}; source: {event.source})"


def load_ledger(path: Path) -> dict[str, str]:
    """alert key -> ISO time sent. A missing or unreadable ledger is an empty one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("ipo alert ledger %s unreadable; starting empty", path)
        return {}
    sent = data.get("sent") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in sent.items()} if isinstance(sent, dict) else {}


def save_ledger(path: Path, sent: dict[str, str], now: datetime) -> None:
    """Write the ledger, dropping entries past ``LEDGER_RETENTION``."""
    floor = now - LEDGER_RETENTION
    kept = {}
    for key, when in sent.items():
        try:
            if datetime.fromisoformat(when) < floor:
                continue
        except ValueError:
            continue
        kept[key] = when
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sent": kept}, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class AlertPolicy:
    """Which events are worth a push: see the module docstring."""

    watchlist: Sequence[str]
    min_deal_usd: float
    stages: Sequence[str] = DEFAULT_ALERT_STAGES

    @classmethod
    def from_settings(cls, settings) -> AlertPolicy:
        return cls(
            watchlist=tuple(settings.ipo_watchlist),
            min_deal_usd=settings.ipo_alert_min_deal_usd,
            stages=tuple(settings.ipo_alert_stages),
        )


def run_ipo_alerts(
    events: Iterable[IpoEventView],
    *,
    notify: Callable[[Notification], bool],
    policy: AlertPolicy,
    ledger_path: Path,
    now: datetime | None = None,
) -> int:
    """Notify each major, current, not-yet-sent event once. Returns how many were sent.

    ``notify`` returns whether the push went out; only a sent alert is written to the ledger.
    """
    moment = now or datetime.now(UTC)
    sent = load_ledger(ledger_path)
    count = 0
    for event in events:
        if event.stage not in policy.stages or not is_current(event, moment):
            continue
        key = alert_key(event)
        if key in sent:
            continue
        reason = major_reason(event, policy.watchlist, policy.min_deal_usd)
        if reason is None:
            continue
        title, message = format_alert(event, reason)
        note = Notification(
            title, message, priority="default", tags=ALERT_TAGS, click=event.url or ""
        )
        if notify(note):
            sent[key] = moment.isoformat()
            count += 1
    save_ledger(ledger_path, sent, moment)
    return count


def load_events(session: Session, stages: Sequence[str], now: datetime) -> list[IpoEventView]:
    """The ``ipo_events`` rows that could still be news, oldest first.

    A superset of what ``is_current`` keeps -- the SQL only bounds the read; the Python rule
    is the one that decides, and the one the tests pin.
    """
    from ibkr_trader.db.models import IpoEvent

    recent = now - FILING_LOOKBACK
    rows = session.scalars(
        select(IpoEvent)
        .where(IpoEvent.stage.in_(list(stages)))
        .where(
            or_(
                IpoEvent.expected_date >= (now - EXPECTED_GRACE).date(),
                IpoEvent.first_seen_at >= recent,
                IpoEvent.filed_at >= recent,
            )
        )
        .order_by(IpoEvent.first_seen_at, IpoEvent.id)
    )
    return [
        IpoEventView(
            source=row.source,
            external_id=row.external_id,
            company_name=row.company_name,
            stage=row.stage,
            symbol=row.symbol or None,
            exchange=row.exchange or None,
            form_type=row.form_type,
            filed_at=row.filed_at,
            expected_date=row.expected_date,
            price_low=row.price_low,
            price_high=row.price_high,
            shares=row.shares,
            deal_value_usd=row.deal_value_usd,
            url=row.url,
            first_seen_at=row.first_seen_at,
        )
        for row in rows
    ]


#: The ``ipo_alerts`` job's recorded result without a topic, so `health` says why nothing went.
NO_TOPIC = "skipped: NTFY_TOPIC is not set"


def alert_job(
    settings, session_factory: Callable[[], AbstractContextManager[Session]] | None = None
) -> object:
    """The ``ipo_alerts`` serve job: read current events, push the major new ones."""
    if not settings.ntfy_topic:
        return NO_TOPIC
    from ibkr_trader import gateway_watch
    from ibkr_trader.db.session import get_session

    policy = AlertPolicy.from_settings(settings)
    moment = datetime.now(UTC)
    with (session_factory or get_session)() as session:
        events = load_events(session, policy.stages, moment)
    sent = run_ipo_alerts(
        events,
        notify=lambda note: gateway_watch.post_ntfy(
            settings.ntfy_server, settings.ntfy_topic, note
        ),
        policy=policy,
        ledger_path=Path(settings.ipo_alert_ledger_file),
        now=moment,
    )
    return {"candidates": len(events), "alerts_sent": sent}
