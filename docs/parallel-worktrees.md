# Parallel worktrees: two agents, one machine, minimal disk

Setup for running two coding agents on separate branches at the same time without them
conflicting — and without duplicating the heavy parts of the stack (Docker images, the
ingested Postgres data, the uv package cache, git history, IB Gateway).

## What is shared automatically

- **Git objects** — `git worktree` gives each branch its own checkout but one shared
  `.git` object store. A checkout of this repo is ~1 MB of source.
- **Python packages** — uv installs from a global cache using hardlinks, so each
  worktree's `uv sync` produces its own `.venv` that points at the same files on disk.
  Isolation without duplication.
- **Docker image layers** — `postgres:16` and the ib-gateway image exist once in the
  Docker layer store regardless of how many containers reference them.

## What is shared as a single running instance

### One Postgres container, one database per agent

Never run a second Postgres container. `docker-compose.yml` pins `name: ibkr_trader`
so `docker compose up -d db` from *any* worktree resolves to the same project and the
same `pgdata` volume — without the pin, Compose derives the project name from the
directory, and a worktree named `ibkr_trader-a` would silently create a second
Postgres container/volume and collide on host port 5433.

Isolation between agents happens one level down, as logical databases in the shared
server:

```bash
docker compose up -d db     # once, from any checkout
docker compose exec db psql -U trader -d ibkr_trader -c 'CREATE DATABASE ibkr_trader_a'
docker compose exec db psql -U trader -d ibkr_trader -c 'CREATE DATABASE ibkr_trader_b'
```

The main `ibkr_trader` database keeps the ingested dataset once. Agent databases start
empty (a few MB) and get their schema from `alembic upgrade head`. Tests use in-memory
SQLite anyway (see `.claude/rules/testing.md`), so a real Postgres is mostly needed to
validate migrations — an empty schema-migrated database covers that. If an agent needs
the real dataset, point it at the main database with a read-only role rather than
copying the data.

### One IB Gateway

IBKR allows only one active session per username, so a second gateway container with
the same paper login would steal the session from the first. Both worktrees connect to
the shared `127.0.0.1:4004`; concurrent API connections are isolated by giving each
agent a distinct `IBKR_CLIENT_ID` in its `.env`.

## Per-worktree setup

```bash
cd ~/ibkr_trader
git worktree add ../ibkr_trader-a agent-a   # creates branch agent-a
git worktree add ../ibkr_trader-b agent-b

cd ../ibkr_trader-a
uv sync                                     # hardlinked from the shared cache, ~free
cp ../ibkr_trader/.env .env                 # then edit, see below
uv run alembic upgrade head
# repeat for ../ibkr_trader-b
```

Per-worktree `.env` overrides (everything else can stay identical):

| Variable         | worktree a                                | worktree b                                |
| ---------------- | ----------------------------------------- | ----------------------------------------- |
| `DATABASE_URL`   | `...@127.0.0.1:5433/ibkr_trader_a`        | `...@127.0.0.1:5433/ibkr_trader_b`        |
| `IBKR_CLIENT_ID` | `2`                                       | `3`                                       |

Each agent then works natively — `uv run pytest`, `uv run ibkr-trader ...` — against
its own database. This is the usual dev loop, just twice.

## What NOT to do per worktree

- **Don't run the `app` profile per branch.** A per-branch app image is the one
  genuinely expensive duplication. The scheduler container is a deployment concern,
  not a dev-loop one; run at most one, from whichever checkout is authoritative.
- **Don't start a second gateway or a second Postgres.** See above.

## Marginal disk cost per extra agent

Checkout (~1 MB) + hardlinked `.venv` (~0 real data) + empty migrated database
(~10 MB). Everything heavy exists exactly once.

## Known coordination hazard: Alembic heads

Isolated databases do not protect against migration conflicts. If both agents
autogenerate a revision off the same head, the branches end up with divergent Alembic
heads — git won't flag it as a textual conflict, but `alembic upgrade head` will fail
with "multiple heads" once both land. Either serialize schema changes (one agent owns
`db/models.py` at a time) or resolve at merge time by re-parenting one revision /
`alembic merge`.
