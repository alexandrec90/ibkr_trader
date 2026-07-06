# Data Sources

> Free-tier limits change often — figures below are ballpark from research on 2026-07-04,
> **verify on each provider's pricing page before building anything that depends on them.**

| Source | What we pull | Free-tier ballpark | Env var(s) | Connector module |
|---|---|---|---|---|
| NewsAPI (newsapi.org) | Headlines/articles by keyword & ticker name | ~100 req/day, dev/non-commercial only, 24 h delay on free tier | `NEWSAPI_KEY` | `ingestion/news/newsapi.py` |
| Finnhub | Company news, sentiment, quotes, candles | ~60 calls/min | `FINNHUB_KEY` | `ingestion/news/finnhub_news.py`, `ingestion/market/finnhub_market.py` |
| Alpha Vantage | Daily/intraday OHLCV, fundamentals, news-sentiment endpoint | very low on free tier (~25 req/day) | `ALPHA_VANTAGE_KEY` | `ingestion/market/alpha_vantage.py` |
| Financial Modeling Prep | EOD prices, fundamentals, calendars | ~250 req/day; free tier is US-only and gates some symbols (e.g. GOOG works only as GOOGL; TSX → HTTP 402) | `FMP_KEY` | `ingestion/market/fmp.py` |
| Yahoo Finance (yfinance) | EOD OHLCV incl. TSX (`.TO`) — used for symbols FMP's free tier gates | **Unofficial scraper**, no key/quota; Yahoo temp-bans abusive IPs → connector enforces ≥2 s between requests, keep volume tiny | — | `ingestion/market/yahoo.py` |
| Reddit API (PRAW) | Posts/comments from r/wallstreetbets, r/investing, r/stocks, r/CanadianInvestor | OAuth, ~100 queries/min | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | `ingestion/social/reddit.py` |
| Google Trends (pytrends) | Search-interest time series per keyword | Unofficial/scraper — fragile, throttle hard | — | `ingestion/social/google_trends.py` |
| IBKR TWS API | Historical bars + live/delayed quotes for traded instruments | Pacing rules; see [ibkr/03](ibkr/03-market-data-and-historical.md) | `IBKR_*` | `ingestion/market/ibkr_historical.py` |
| Questrade API (candidate, not wired) | OHLC candles (max 2 000/request), quotes, account data — owner has a Questrade account, API is free for clients (OAuth refresh token via App Hub) | Per-second/hour caps on the [rate-limiting page](https://www.questrade.com/api/documentation/rate-limiting) — verify before building | — | — |

## Design rules

- Every connector implements `ingestion.base.Connector` and writes through the repository
  layer into Postgres — raw payload kept in a `raw` JSON column so re-parsing is possible.
- **Idempotent upserts** keyed on (source, external_id): pollers will re-see the same items.
- Respect rate limits centrally: `tenacity` retries with backoff + per-connector token buckets.
- Symbols/tickers are normalized to the `instruments` table; text sources store a
  `symbols` array extracted by (initially naive) matching in `signals/features.py`.
- Canadian angle: TSX tickers (e.g. `SHOP.TO` style suffixes) differ per provider — each
  connector maps to/from the canonical `instruments.symbol` + `exchange`.

## Registration links

- https://newsapi.org/register
- https://finnhub.io/register
- https://www.alphavantage.co/support/#api-key
- https://site.financialmodelingprep.com/developer/docs
- https://www.reddit.com/prefs/apps (create a "script" app)
