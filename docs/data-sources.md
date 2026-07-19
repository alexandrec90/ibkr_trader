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
| Yahoo fundamentals (yfinance) | Dividends (decades), share counts (~2015+), sector/industry/name (current only), income/balance/cashflow statements (**only ~4-5 annual / ~5-7 quarterly** periods → snapshot forward), earnings report dates (back to ~2001). ETFs return dividends only. | Same unofficial scraper + shared ≥2 s throttle as prices | — | `ingestion/market/yahoo_fundamentals.py` |
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
- Headline/post sentiment model setup and provenance are documented in
  [sentiment.md](sentiment.md).
- Canadian angle: TSX tickers (e.g. `SHOP.TO` style suffixes) differ per provider — each
  connector maps to/from the canonical `instruments.symbol` + `exchange`.

## Delisted-inclusive daily bars (survivorship research, 2026-07-10)

Prices are the vendor's published personal/non-commercial rates before tax and may change.
No subscription was purchased during this research.

| Source | Delisted + daily-bar coverage | TSX | Personal-use price / licence | Decision |
|---|---|---|---|---|
| [Norgate Data](https://norgatedata.com/stockmarketpackages.php) | Platinum includes delisted securities and historical index constituents back to 1990 for both markets. Adjusted and unadjusted daily data are available through its Windows Python integration or export. | Explicit Canadian package: TSX, TSX-V, CSE and Neo; delisted Canadian securities are included at Platinum. | US Platinum: USD 630/year; Canadian Platinum: CAD 630/year (or 6-month terms). Single-user subscription; do not redistribute, and confirm the licence agreement before purchase. | **Best coverage fit.** The only candidate here that explicitly promises complete survivorship-oriented US and Canadian packages. |
| [Sharadar / Nasdaq Data Link](https://data.nasdaq.com/databases/SEP) | SEP contains active and delisted US equity EOD prices; Nasdaq delivers it as a premium tables product. | No: Core US Equities is US-listed coverage, so it cannot fix the Canadian half. | Current price and non-professional terms are shown after Data Link login rather than on the public product page; obtain a quote. | Good API-first US supplement, but reject as the sole source because it has no TSX universe. |
| [EODHD](https://eodhd.com/financial-apis/delisted-stock-companies-data) | EOD data is advertised for pre-2018 delistings; post-2018 delistings also have other corporate data. Its exchange-symbol endpoint can return inactive tickers and the EOD service covers worldwide exchanges. | Toronto is in worldwide coverage, but the public material does not quantify completeness of delisted TSX history; validate a known-failure sample before relying on it. | [EOD All World](https://eodhd.com/pricing): USD 19.99/month or 199/year, explicitly personal use, with delisted data listed as a feature; commercial use needs another licence. | Cheapest credible pilot, but its TSX-delisting completeness is not documented strongly enough to be the final source without a trial audit. |
| [Financial Modeling Prep](https://site.financialmodelingprep.com/developer/docs/stable/delisted-companies) | Has a delisted-company directory and historical EOD endpoint, but FMP states historical prices exist only for *select US companies* and symbols may disappear when reused. | Current Canadian EOD coverage starts at Premium, but FMP does not promise delisted-inclusive TSX history. | [Personal plans](https://site.financialmodelingprep.com/pricing-plans): Premium USD 59/month and Ultimate USD 149/month, billed annually; individual use, no display/redistribution without a separate agreement. | Reject for this fix: neither US nor TSX delisted completeness is promised. |

**Recommendation:** if the owner accepts the cost, buy no data until completing Norgate's
three-week trial, then choose the US Platinum + Canadian Platinum packages after verifying a
small list of bankruptcies, acquisitions, symbol changes and TSX delistings against known final
trade dates. Norgate is the recommended production source because its published coverage
directly matches both required markets and the survivorship use case. EODHD is the fallback
pilot when cost dominates, but it must pass the same TSX completeness audit first.

### Ingestion readiness

The existing schema can already represent a dead security as an `Instrument` with `PriceBar`
rows that simply end on its last trading date; neither table requires the symbol to remain
active. The missing lifecycle field is `Instrument.end_of_life_date`, and the selected vendor
may also require a stable provider identifier plus symbol-history mapping to prevent ticker
reuse from merging different companies. Those additions are tracked in `TODO.md` and wait for
the chosen source's contract and identifiers; no speculative migration is added here.

## Registration links

- https://newsapi.org/register
- https://finnhub.io/register
- https://www.alphavantage.co/support/#api-key
- https://site.financialmodelingprep.com/developer/docs
- https://www.reddit.com/prefs/apps (create a "script" app)
