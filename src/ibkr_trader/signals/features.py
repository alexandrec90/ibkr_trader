"""Feature engineering: turn raw rows (bars, news, social, trends) into model inputs.

Everything reads from Postgres and writes back to Postgres (sentiment columns,
predictions) — no network calls in this package, so feature builds are reproducible.
"""

import re

# Naive first pass: $TSLA / uppercase-word matching against the instruments table.
# Known weakness: words like "CEO", "YOLO", "DD" false-positive — filter against a
# stoplist + the instruments table, and revisit with a proper NER pass later.
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")


def extract_tickers(text: str, known_symbols: set[str]) -> list[str]:
    """Extract candidate tickers from free text, keeping only known instruments."""
    if not text:
        return []
    candidates = {match.upper() for match in CASHTAG_RE.findall(text)}
    candidates |= {word for word in re.findall(r"\b[A-Z]{2,5}\b", text)}
    return sorted(candidates & known_symbols)


def score_sentiment(text: str) -> float:
    """Sentiment in [-1, 1]. TODO: start with VADER or a small finance-tuned model;
    persist into news_articles.sentiment / social_posts.sentiment."""
    raise NotImplementedError


def build_daily_features(symbol: str) -> dict:
    """Assemble per-symbol daily features: returns, volume z-score, mention counts,
    mean sentiment, trend interest delta. TODO once ingestion produces data."""
    raise NotImplementedError
