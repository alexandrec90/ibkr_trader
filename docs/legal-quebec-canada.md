# Legal & Regulatory Considerations — Québec / Canada

> ⚠️ **This is engineering research, not legal or tax advice.** Written 2026-07-04. Confirm
> anything consequential with a Québec securities lawyer and/or tax professional (CPA familiar
> with Revenu Québec + CRA treatment of trading income).

## Who regulates what

- **AMF (Autorité des marchés financiers)** — Québec's securities regulator. Registration
  (dealer/adviser) is only triggered if you trade or advise **for others** or hold yourself out
  as doing so. Trading **your own account with your own money**, even algorithmically, does not
  by itself require registration.
- **CIRO** (Canadian Investment Regulatory Organization) regulates dealers. Québec residents
  hold IBKR accounts with **Interactive Brokers Canada Inc.** (CIRO member, CIPF protection).
- The US FINRA "Pattern Day Trader" USD 25k rule applies to accounts held at the **US** broker
  entity; IBKR **Canada** margin accounts follow Canadian margin rules instead. **[verify your
  account's carrying entity in Client Portal before assuming either way]**

## Lines not to cross (turns a personal bot into a regulated/illegal activity)

- **Trading for others / pooling money / selling signals** → adviser/dealer registration
  territory (AMF). Keep this strictly personal.
- **Market manipulation**: strategies that *post and cancel to move price* (spoofing/layering),
  wash trades, or trading on the basis of *spreading* misinformation are offences. A bot that
  merely *reads* WallStreetBets sentiment is fine; a bot that *posts* to move sentiment is not.
- **Insider information**: if any data source ever includes material non-public information,
  trading on it is illegal. Public news/social feeds are fine.

## Tax (the part most likely to bite)

- CRA/Revenu Québec distinguish **capital gains** (50% inclusion) from **business income**
  (100% taxable). Frequent, short-holding-period, automated trading with significant time
  invested looks like **business income**. Expect it; keep clean records (the `orders`/
  `executions` tables + IBKR Flex reports are the audit trail).
- **Never run this in a TFSA/RRSP/FHSA**: carrying on a day-trading business inside a TFSA is
  taxed as business income and draws audits. Registered accounts also generally can't hold the
  needed margin/short positions. Use a non-registered (margin) account.
- Currency: gains on US-listed trades must be reported in CAD; IBKR reports help. Québec files
  both CRA and Revenu Québec returns.
- If it becomes a real business, incorporation may matter someday — talk to a professional
  first.

## Data & privacy (Québec Law 25 / PIPEDA)

- Québec's **Law 25** (privacy) is the strictest in Canada. Social-media ingestion stores
  content written by identifiable people. Even for personal use, design defensively:
  - Store **hashed author identifiers**, not usernames (see `social_posts.author_hash`).
  - Keep only what the models need (title/body/score/timestamps); don't build people profiles.
  - Don't republish collected content.
- Respect **API terms of service**: NewsAPI free tier is non-commercial; Reddit's API terms
  restrict bulk redistribution and using content for model training has specific terms
  **[verify current Reddit Data API terms]**; Google Trends via `pytrends` is unofficial
  scraping (can break / be blocked at any time). IBKR **market data may not be redistributed**
  — it is licensed to your username for your use.
- If the service is hosted outside Canada, personal information collected under Québec rules is
  crossing borders — for personal-use data at this scale it's low-risk, but hosting in a
  Canadian region (e.g. AWS ca-central-1 in Montréal, GCP northamerica-northeast1) is the
  path of least resistance and lowest latency to TSX anyway.

## Practical checklist

- [ ] Confirm IBKR account is with IB Canada and understand its margin rules.
- [ ] Non-registered account only; never registered accounts.
- [ ] Keep every order/execution in Postgres + archive IBKR Flex statements (tax records).
- [ ] Track data-source ToS for each connector in `docs/data-sources.md` before going beyond
      free-tier personal use.
- [ ] Re-read this file before ever enabling `ENVIRONMENT=live`.
