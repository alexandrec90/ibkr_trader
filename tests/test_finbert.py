from types import SimpleNamespace

import pytest

from ibkr_trader.signals import finbert


class _Loader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def from_pretrained(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.value


def _fake_transformers(outputs):
    tokenizer = _Loader("tokenizer")
    model = _Loader("model")
    pipeline_calls = []
    inference_calls = []

    def inference(texts, **kwargs):
        inference_calls.append((texts, kwargs))
        return outputs

    def pipeline(*args, **kwargs):
        pipeline_calls.append((args, kwargs))
        return inference

    return (
        SimpleNamespace(
            AutoTokenizer=tokenizer,
            AutoModelForSequenceClassification=model,
            pipeline=pipeline,
        ),
        tokenizer,
        model,
        pipeline_calls,
        inference_calls,
    )


def test_finbert_maps_probabilities_skips_empty_and_batches(monkeypatch):
    outputs = [
        [
            {"label": "positive", "score": 0.8},
            {"label": "negative", "score": 0.1},
            {"label": "neutral", "score": 0.1},
        ],
        [
            {"label": "positive", "score": 0.05},
            {"label": "negative", "score": 0.75},
            {"label": "neutral", "score": 0.2},
        ],
    ]
    fake, tokenizer, model, pipeline_calls, inference_calls = _fake_transformers(outputs)
    monkeypatch.setattr(finbert, "_transformers", lambda: fake)

    scores = finbert.FinbertScorer().score_many(
        ["profits beat estimates", "   ", "guidance cut"], batch_size=7
    )

    assert scores == pytest.approx([0.7, 0.0, -0.7])
    assert tokenizer.calls == [((finbert.MODEL_NAME,), {"local_files_only": True})]
    assert model.calls == [((finbert.MODEL_NAME,), {"local_files_only": True})]
    assert pipeline_calls == [
        (
            ("text-classification",),
            {"model": "model", "tokenizer": "tokenizer", "device": -1},
        )
    ]
    assert inference_calls == [
        (
            ["profits beat estimates", "guidance cut"],
            {"batch_size": 7, "truncation": True, "max_length": 512, "top_k": None},
        )
    ]


def test_finbert_all_empty_does_not_load_pipeline(monkeypatch):
    monkeypatch.setattr(
        finbert,
        "_transformers",
        lambda: pytest.fail("empty input must not load transformers"),
    )
    assert finbert.FinbertScorer().score_many(["", "  "], batch_size=2) == [0.0, 0.0]


def test_finbert_guarded_import_has_actionable_extra_command(monkeypatch):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(finbert, "import_module", missing)
    with pytest.raises(RuntimeError, match="uv sync --extra sentiment"):
        finbert.FinbertScorer().score("headline")


def test_finbert_missing_cache_has_download_command(monkeypatch):
    class MissingLoader:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise OSError("not cached")

    fake = SimpleNamespace(
        AutoTokenizer=MissingLoader,
        AutoModelForSequenceClassification=MissingLoader,
    )
    monkeypatch.setattr(finbert, "_transformers", lambda: fake)
    with pytest.raises(RuntimeError, match="ibkr-trader sentiment download"):
        finbert.FinbertScorer().score("headline")


def test_download_model_allows_network_and_populates_both_assets(monkeypatch):
    fake, tokenizer, model, _, _ = _fake_transformers([])
    monkeypatch.setattr(finbert, "_transformers", lambda: fake)
    finbert.download_model()
    assert tokenizer.calls == [((finbert.MODEL_NAME,), {})]
    assert model.calls == [((finbert.MODEL_NAME,), {})]
