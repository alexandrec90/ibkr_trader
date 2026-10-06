"""Tell the owner, on their phone, when the IB Gateway is not logged in.

The paper gateway (the compose ``ib-gateway`` service) logs itself in, but now and then IBKR
asks a human to approve that login in IBKR Mobile. When nobody does, IBC restarts and tries
again (``TWOFA_TIMEOUT_ACTION=restart``) and the gateway sits logged out -- which nothing else
in this app would notice, the same way a stopped database once went unnoticed for six days.

The ``gateway`` job in `serve` probes the API read-only every few minutes. After
``alert_after`` failures in a row it pushes one ntfy notification (https://ntfy.sh), repeats it
every ``repeat_hours`` while the gateway stays down, and sends one more when the login is back.
A failed probe also re-raises, so ``job_health`` records it and `ibkr-trader health` goes red.

Notifications travel through a public ntfy server, so they carry no account ID and no
credentials -- only "not logged in" and the error type.
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

#: How long one probe waits for the API handshake. A logged-out gateway behind the image's
#: socat forward accepts the TCP connection and then never answers, so this is the bound on it.
PROBE_TIMEOUT_SECONDS = 15.0


class GatewayDown(RuntimeError):
    """The gateway did not complete a read-only API handshake, or is on the wrong account."""


async def _managed_accounts(host: str, port: int, client_id: int, timeout: float) -> list[str]:
    from ib_async import IB
    from ib_async.ib import StartupFetch

    ib = IB()
    # readonly, and no startup fetch: the probe must never be able to touch an order, and it
    # only needs the handshake plus the account list the gateway sends with it.
    await ib.connectAsync(
        host, port, clientId=client_id, timeout=timeout, readonly=True, fetchFields=StartupFetch(0)
    )
    try:
        return list(ib.managedAccounts())
    finally:
        ib.disconnect()


def probe_gateway(
    host: str, port: int, client_id: int, timeout: float = PROBE_TIMEOUT_SECONDS
) -> list[str]:
    """Connect read-only, return the managed account IDs, disconnect. Raises ``GatewayDown``.

    ``asyncio.run`` rather than ib_async's blocking ``connect``: scheduler jobs run on worker
    threads, which have no event loop of their own.
    """
    try:
        return asyncio.run(_managed_accounts(host, port, client_id, timeout))
    except Exception as exc:
        raise GatewayDown(f"no API handshake with {host}:{port}: {type(exc).__name__}") from exc


def check_accounts(accounts: list[str], environment: str) -> None:
    """Refuse a login that is up but wrong: no accounts, or a non-paper one while paper-only.

    Paper account IDs start with ``DU`` (see ``Settings.ibkr_execution_account``). The message
    names no account ID, because it ends up in a push notification.
    """
    if not accounts:
        raise GatewayDown("gateway answered but reported no managed accounts")
    if environment == "paper" and not all(a.upper().startswith("DU") for a in accounts):
        raise GatewayDown("gateway is logged into a NON-paper account while ENVIRONMENT=paper")


def send_ntfy(
    server: str,
    topic: str,
    title: str,
    message: str,
    *,
    priority: str = "high",
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    """POST one notification to ``{server}/{topic}``. Returns whether it was sent; never raises.

    A notification that fails to send must not turn a gateway outage into a second, louder
    failure of the job itself -- it is logged, and the outage is still in ``job_health``.
    """
    if not topic:
        logger.warning("gateway alert not sent: NTFY_TOPIC is not set (%s)", title)
        return False
    try:
        request = urllib.request.Request(
            f"{server.rstrip('/')}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "warning"},
            method="POST",
        )
        response = opener(request, timeout=10)
        close = getattr(response, "close", None)
        if close is not None:
            close()
    # URLError, HTTPError and timeouts are all OSError; a malformed reply is an HTTPException;
    # a bad NTFY_SERVER URL is a ValueError. Anything else is a bug and should propagate.
    except (OSError, http.client.HTTPException, ValueError):
        logger.exception("gateway alert could not be sent to ntfy")
        return False
    return True


class GatewayWatch:
    """The stateful ``gateway`` job: probe, count failures, alert on the edges.

    State lives on the instance, so a `serve` restart forgets an alert already sent; the first
    failing probes after a restart alert again, which errs on the side of telling the owner.
    """

    def __init__(
        self,
        probe: Callable[[], list[str]],
        notify: Callable[[str, str], bool],
        *,
        environment: str = "paper",
        alert_after: int = 2,
        repeat_hours: float = 2.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.probe = probe
        self.notify = notify
        self.environment = environment
        self.alert_after = max(1, alert_after)
        self.repeat = timedelta(hours=repeat_hours)
        self.now = now
        self.consecutive_failures = 0
        self.down_since: datetime | None = None
        self.last_alert: datetime | None = None

    def __call__(self) -> str:
        try:
            accounts = self.probe()
            check_accounts(accounts, self.environment)
        except GatewayDown as exc:
            self._failed(exc)
            raise
        self._recovered()
        return f"logged in ({len(accounts)} account(s))"

    def _failed(self, exc: GatewayDown) -> None:
        moment = self.now()
        self.consecutive_failures += 1
        if self.down_since is None:
            self.down_since = moment
        if self.consecutive_failures < self.alert_after:
            return
        if self.last_alert is not None and moment - self.last_alert < self.repeat:
            return
        minutes = int((moment - self.down_since).total_seconds() // 60)
        self.notify(
            "IB Gateway needs you",
            f"Paper gateway not logged in for ~{minutes} min ({exc}). Approve the login in "
            "IBKR Mobile, or open the gateway in TigerVNC at 127.0.0.1:5900.",
        )
        self.last_alert = moment

    def _recovered(self) -> None:
        if self.last_alert is not None:
            self.notify("IB Gateway is back", "Paper gateway is logged in again.")
        self.consecutive_failures = 0
        self.down_since = None
        self.last_alert = None


def watch_from_settings(settings) -> GatewayWatch:
    """The ``GatewayWatch`` `serve` registers, wired to ``Settings``."""
    return GatewayWatch(
        probe=lambda: probe_gateway(
            settings.ibkr_host, settings.ibkr_port, settings.gateway_check_client_id
        ),
        notify=lambda title, message: send_ntfy(
            settings.ntfy_server, settings.ntfy_topic, title, message
        ),
        environment=settings.environment,
        alert_after=settings.gateway_alert_after_failures,
        repeat_hours=settings.gateway_alert_repeat_hours,
    )
