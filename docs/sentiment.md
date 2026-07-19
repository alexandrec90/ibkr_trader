# Sentiment scoring

News articles and social posts store a score in `[-1, 1]` plus the producing model in
`sentiment_model`. VADER uses its compound score. FinBERT (`ProsusAI/finbert`) uses
`p_positive - p_negative`; neutral text is therefore near zero. Empty text scores exactly
zero.

VADER is the default and requires no setup. FinBERT is optional and runtime scoring is
strictly local-cache-only, so the scheduler never downloads model files:

```powershell
uv sync --extra sentiment
uv run ibkr-trader sentiment download
uv run ibkr-trader sentiment rescore --model finbert
```

Use `--limit N` to review a small initial sample. Re-running the command is idempotent: it
only selects rows whose provenance differs from the requested model. Once results have been
validated, set `SENTIMENT_MODEL=finbert` in `.env` so newly ingested rows use FinBERT.

The migration stamps pre-existing non-NULL scores as `vader`; unscored rows remain NULL.
Titles, summaries, and post bodies survive raw-payload pruning, so historical rows remain
re-scorable.
