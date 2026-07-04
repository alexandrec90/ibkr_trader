# Running IB Gateway Headless in Docker (cloud deployment)

> Researched 2026-07-04. This is the load-bearing operational piece for a cloud-hosted service.

## The problem

The TWS API talks to a *running, logged-in* TWS/IB Gateway. IB Gateway is a GUI Java app with
no official headless mode, it logs itself out daily, and logins require 2FA. Community tooling
solves most of this:

- **IBC** (IbcAlpha) — automates the login dialog, restarts, daily-restart handling.
- **Xvfb + x11vnc** — virtual framebuffer so the GUI runs without a display; VNC for debugging.
- Prebuilt images that bundle all of it.

## Recommended image: `gnzsnz/ib-gateway-docker`

https://github.com/gnzsnz/ib-gateway-docker (image: `ghcr.io/gnzsnz/ib-gateway`)

- Env-var config: `TWS_USERID`, `TWS_PASSWORD`, `TRADING_MODE` (`paper`/`live`/`both`),
  `READ_ONLY_API`, `AUTO_RESTART_TIME`, `TWOFA_TIMEOUT_ACTION`, `VNC_SERVER_PASSWORD`,
  `TIME_ZONE`. **[verify current variable names against the image README before first run]**
- The Gateway binds its API to localhost inside the container; the image uses **socat** to
  forward it — container ports **4003 (live)** and **4004 (paper)**.
- Alternatives: `extrange/ibkr-docker` (noVNC in-browser), `hartza-capital/docker-ib-gateway`.

## 2FA and the weekly re-auth reality

- With IBKR Mobile (IB Key) 2FA, someone must approve the login push. `TWOFA_TIMEOUT_ACTION=restart`
  makes IBC retry the login until you approve it.
- **Security tokens are invalidated every Sunday ~1:00 am ET** → roughly **once a week a human
  must complete 2FA**, even with auto-restart. Design consequence: the service must tolerate
  a dead broker connection (queue/skip trading, alert you) rather than crash.
- `AUTO_RESTART_TIME` (e.g. `11:59 PM`) handles the *daily* restart without re-auth for the
  rest of the week.
- Do **not** disable 2FA to make automation easier; use the "second factor only on new
  devices"-type settings cautiously and read IBKR's current security docs. **[verify options]**

## Cloud/security notes

- Never expose ports 4001–4004 or VNC publicly. Bind them to an internal network only
  (compose network / localhost). Anyone reaching that socket can trade as you.
- Keep credentials in env/secret manager, not in the image or repo. `.env` is gitignored.
- IBKR may flag logins from datacenter IPs in unfamiliar regions; keep the region stable
  (e.g. a Montréal/us-east region) **[verify — anecdotal]**.
- Paper first: run the whole stack in `TRADING_MODE=paper` until execution + risk code is
  proven. The compose file in this repo defaults to paper.

## Local (Windows) alternative for development

For development on this machine you can skip Docker for the gateway: install IB Gateway
natively, log in manually (paper), enable API on port 4002, and point the app at
`IBKR_HOST=host.docker.internal` (from containers) or `127.0.0.1` (native runs).

## Sources

- https://github.com/gnzsnz/ib-gateway-docker
- https://github.com/IbcAlpha/IBC
- https://github.com/extrange/ibkr-docker
- https://github.com/hartza-capital/docker-ib-gateway
