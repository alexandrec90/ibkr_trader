"""Local-cache-only FinBERT inference for finance headline sentiment."""

from importlib import import_module
from typing import Any

MODEL_NAME = "ProsusAI/finbert"
MAX_LENGTH = 512
INSTALL_HINT = "uv sync --extra sentiment"


def _transformers() -> Any:
    """Import the optional dependency without making it part of core package startup."""
    try:
        return import_module("transformers")
    except ImportError as exc:
        raise RuntimeError(
            f"FinBERT needs the sentiment extra — install with: {INSTALL_HINT}"
        ) from exc


def download_model() -> None:
    """Download FinBERT weights and tokenizer into the Hugging Face cache."""
    transformers = _transformers()
    transformers.AutoTokenizer.from_pretrained(MODEL_NAME)
    transformers.AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)


class FinbertScorer:
    """Batch FinBERT scorer mapping class probabilities to ``[-1, 1]``.

    The model is loaded lazily and strictly from the local Hugging Face cache. The scheduler
    can therefore never trigger a network download.
    """

    model_name = "finbert"

    def __init__(self) -> None:
        self._pipeline: Any = None

    def _load_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        transformers = _transformers()
        try:
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                MODEL_NAME, local_files_only=True
            )
            model = transformers.AutoModelForSequenceClassification.from_pretrained(
                MODEL_NAME, local_files_only=True
            )
        except OSError as exc:
            raise RuntimeError(
                "FinBERT is not present in the local Hugging Face cache; run: "
                "ibkr-trader sentiment download"
            ) from exc
        self._pipeline = transformers.pipeline(
            "text-classification", model=model, tokenizer=tokenizer, device=-1
        )
        return self._pipeline

    def score_many(self, texts: list[str], *, batch_size: int) -> list[float]:
        """Score texts in batches; blanks are neutral and never sent through the model."""
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        scores = [0.0] * len(texts)
        populated = [(index, text) for index, text in enumerate(texts) if text.strip()]
        if not populated:
            return scores

        outputs = self._load_pipeline()(
            [text for _, text in populated],
            batch_size=batch_size,
            truncation=True,
            max_length=MAX_LENGTH,
            top_k=None,
        )
        for (index, _), probabilities in zip(populated, outputs, strict=True):
            by_label = {str(item["label"]).lower(): float(item["score"]) for item in probabilities}
            if "positive" not in by_label or "negative" not in by_label:
                raise RuntimeError(f"unexpected FinBERT labels: {sorted(by_label)}")
            scores[index] = max(-1.0, min(1.0, by_label["positive"] - by_label["negative"]))
        return scores

    def score(self, text: str) -> float:
        """Score one text, primarily as a convenience for callers and smoke tests."""
        return self.score_many([text], batch_size=1)[0]
