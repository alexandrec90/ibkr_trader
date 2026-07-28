# Plan TOOLS-01 — FinBERT sentiment scoring (replaces VADER as the default scorer)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[signals/features.py](../../../src/ibkr_trader/signals/features.py)
(`score_sentiment`, ~line 335) ·
[signals/sentiment.py](../../../src/ibkr_trader/signals/sentiment.py) (`score_pending`) ·
[maintenance.py](../../../src/ibkr_trader/maintenance.py) (`prune_scored_raw`) ·
[db/models.py](../../../src/ibkr_trader/db/models.py) (`NewsArticle`, `SocialPost`) ·
[tests/test_sentiment.py](../../../tests/test_sentiment.py)

## Context

Sentiment today is VADER (`score_sentiment`, lazy singleton in `features.py`): a
general-purpose lexicon, weak on finance headlines ("cuts guidance" vs "cuts costs").
FinBERT (`ProsusAI/finbert` via `transformers`) is finance-tuned and free on CPU at our
volume (headlines + reddit titles, hundreds/day). The docstring on `score_sentiment`
already promises this revisit. This is the single most likely prediction-quality win.

Facts that shape the design:

- Scoring a row unlocks `prune_scored_raw` dropping its `raw` blob — but the scored **text**
  survives pruning (`news_articles.title`/`summary`, `social_posts.title`/`body`), so old
  rows can be re-scored with a better model at any time.
- There is currently **no record of which model produced a score** — `sentiment` is just a
  Float. A model switch without provenance would silently mix incomparable scales.
- `score_pending` runs inside the `serve` scheduler and must stay DB-only at runtime. The
  HF model download (~440 MB, one-time) must therefore happen out-of-band, never inside a
  scoring call triggered by the scheduler.

## Decisions already made (don't relitigate)

- FinBERT fills the **same** `sentiment` column, mapped to [-1, 1] as `p_positive − p_negative`.
  Downstream features/consumers do not change.
- VADER stays as the fallback when the extra isn't installed; the scorer is selected by a new
  `Settings` field (e.g. `sentiment_model: Literal["vader", "finbert"] = "vader"`, flipped to
  `finbert` in `.env` once validated).
- Heavy deps live in a new optional extra; the core package must import without it
  (same guarded-import pattern as `[ml]` / `[archive]` — see `train.py:_require_ml`).

## Deliverables

1. **Extra**: `sentiment = ["transformers>=4.40", "torch>=2.2"]` in `pyproject.toml`
   (CPU torch; check `uv` needs an index hint for the CPU wheel — if it gets awkward, pin
   whatever resolves cleanly and note it). `uv lock` + `uv sync --extra sentiment`.
2. **Schema**: `sentiment_model: str | None` (String(32)) on `news_articles` and
   `social_posts` — Alembic autogenerate, review, upgrade. Backfill existing scored rows to
   `'vader'` in the same migration (`WHERE sentiment IS NOT NULL`).
3. **Scorer**: a `FinbertScorer` (module suggestion: `signals/finbert.py`) with lazy model
   load from local HF cache only (`local_files_only=True` at runtime), batch inference
   (pipeline batching, truncation to 512 tokens), empty text → 0.0. Guarded import +
   actionable error naming `uv sync --extra sentiment`.
4. **Wiring**: `score_pending` picks the scorer from `Settings`, stamps `sentiment_model`
   on every row it scores. Keep the batch-drain structure as-is.
5. **CLI**:
   - `ibkr-trader sentiment download` — fetches the model into the HF cache (the only
     network-touching step, explicit and owner-invoked).
   - `ibkr-trader sentiment rescore --model finbert [--limit N]` — re-scores rows whose
     `sentiment_model != 'finbert'` (or NULL) from surviving text, batched, idempotent.
6. **Docs**: short note in `docs/reference/data-sources.md` or a new `docs/sentiment.md`: model choice,
   scale mapping, how to download/rescore, the provenance column.

## Testing (mandatory, same commit)

- Unit-test the scorer with a **faked** transformers pipeline (monkeypatch) — assert the
  [-1,1] mapping, empty-text 0.0, batching, and the guarded-import error. No model download
  in CI; a real-model smoke test may exist but must be `importorskip` + skipped-by-default.
- `score_pending`: existing tests keep passing with VADER default; new tests assert
  `sentiment_model` stamping and settings-driven scorer selection.
- Rescore CLI via `CliRunner`: idempotence (second run scores 0 rows), `--limit` respected.
- Migration backfill: scored rows get `'vader'`, unscored stay NULL.
- If the extras-gated tests need `[sentiment]` in CI, update the CI install line — verify CI
  actually collects them (testing.md rule on extras).

## Out of scope

- Symbol-level aggregation of sentiment into `features` (separate plan; today's consumers
  are unchanged).
- Scoring full article bodies or fetching richer text — headline-grade text only.
- Any GPU/quantization work.

## Done when

- [x] Extra installs cleanly via `uv sync --extra sentiment`; core imports without it
- [x] Migration applied; existing scores stamped `'vader'`
- [ ] `sentiment download` / `sentiment rescore` work end-to-end on the dev DB
- [x] Serve-path `score_pending` uses the configured scorer, stamps provenance, stays DB-only
- [x] Full gate green: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
- [ ] Backlog rescored with FinBERT (owner flips `.env` after eyeballing a sample)

Implementation note (2026-07-18): both CLI paths are covered hermetically, including rescore
idempotence and `--limit`. The end-to-end item remains open because downloading model weights,
reviewing real scores, and re-scoring the owner's backlog are intentionally owner-invoked.
