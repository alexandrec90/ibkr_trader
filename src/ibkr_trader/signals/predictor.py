"""Model interface + registry. Keep it boring: features in, signed score out, persisted to
`predictions` with the feature snapshot for auditability.

Models self-register via ``@register`` so the CLI and backtester can resolve one from just a
name — the string stored in ``predictions.model_name`` / ``backtest_runs.strategy``. This lets
long-term and short-term models coexist and be ranked head-to-head (see backtest.compare).

Two contracts keep the registry boring:
- Registration is an import side-effect: a Predictor is only resolvable once its module has been
  imported. Keep concrete predictors in this module (or import them here) until that grows
  unwieldy, then switch to a proper entry-point/plugin scan.
- Registered predictors must be constructible with no arguments — a model that needs a trained
  artifact resolves its own path (e.g. by name+version) in ``__init__``/``load`` — so callers can
  build one from just its name.
"""

import abc
import json
import logging
from pathlib import Path
from typing import Any

from ibkr_trader.signals.features import FEATURE_SET_VERSION

logger = logging.getLogger(__name__)


class Predictor(abc.ABC):
    name: str = "override-me"
    version: str = "0"

    @abc.abstractmethod
    def predict(self, features: dict) -> float:
        """Return a signed score (+ long / - short / 0 flat) for one instrument+horizon."""


REGISTRY: dict[str, type[Predictor]] = {}


def register(cls: type[Predictor]) -> type[Predictor]:
    """Class decorator: index a Predictor under its ``name`` so ``get_predictor`` can find it."""
    key = cls.name
    if key in REGISTRY and REGISTRY[key] is not cls:
        raise ValueError(f"duplicate predictor name {key!r}: {REGISTRY[key]!r} vs {cls!r}")
    REGISTRY[key] = cls
    return cls


def available() -> list[str]:
    """Registered predictor names, sorted — for CLI help and error messages."""
    return sorted(REGISTRY)


def get_predictor(name: str) -> Predictor:
    """Instantiate a registered predictor by name, or raise KeyError listing the known names."""
    try:
        cls = REGISTRY[name]
    except KeyError:
        known = ", ".join(available()) or "(none registered)"
        raise KeyError(f"unknown predictor {name!r}; available: {known}") from None
    return cls()


@register
class MomentumBaseline(Predictor):
    """Trivial baseline so backtests have something honest to compare against."""

    name = "momentum_baseline"
    version = "1"

    def predict(self, features: dict) -> float:
        # TODO(skeleton): sign of trailing 20d return, once build_daily_features() lands.
        raise NotImplementedError


def _require_ml() -> None:
    """lightgbm lives behind the ``[ml]`` extra — the registry must import without it."""
    try:
        import lightgbm  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "the 'ml_lt' predictor needs the ML extra — install with: pip install -e .[ml]"
        ) from exc


def _require_ridge_ml() -> None:
    """scikit-learn/joblib live behind ``[ml]``; registry import remains lightweight."""
    try:
        import joblib  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "the 'ml_lt_ridge' predictor needs the ML extra — install with: pip install -e .[ml]"
        ) from exc


@register
class MlLongTerm(Predictor):
    """Trained long-term model (ML-03): LightGBM over feature set v1, artifact-backed.

    ``__init__`` resolves the newest artifact under ``Settings.ml_lt_model_dir`` (the
    ``latest`` marker written by ``ibkr-trader train run``) and reads its metadata;
    the booster itself loads lazily on first ``predict``. ``version`` is the artifact
    version (e.g. ``"v1"``), so backtest runs pin exactly which trained model scored them.

    ``predict`` returns the model's predicted cross-sectional rank (label ∈ [0, 1])
    centered to a signed score: ``predicted - 0.5`` — above-median names score positive.
    If the artifact was trained on a different ``FEATURE_SET_VERSION`` than the code
    computes, the model refuses to score (every prediction is 0.0) rather than feed
    mismatched features to the booster.
    """

    name = "ml_lt"
    version = "0"  # replaced by the artifact version on construction

    def __init__(self, model_dir: str | Path | None = None):
        _require_ml()
        if model_dir is None:
            from ibkr_trader.config import get_settings

            model_dir = get_settings().ml_lt_model_dir
        root = Path(model_dir)
        marker = root / "latest"
        if not marker.exists():
            raise FileNotFoundError(
                f"no trained ml_lt artifact under {root} — run `ibkr-trader train run` first"
            )
        artifact_version = marker.read_text().strip()
        self._artifact_dir = root / artifact_version
        metadata_path = self._artifact_dir / "metadata.json"
        if not metadata_path.exists():
            raise FileNotFoundError(
                f"latest marker points at {artifact_version!r} but {metadata_path} is gone"
            )
        metadata = json.loads(metadata_path.read_text())
        self.version = str(metadata.get("version", artifact_version))
        self._model_file: str = metadata.get("model_file", "model.txt")
        self._feature_columns: list[str] = list(metadata["feature_columns"])
        self._categories: dict[str, list[str]] = {
            column: list(spec["categories"])
            for column, spec in metadata.get("categorical", {}).items()
        }
        self._booster: Any = None  # lightgbm.Booster, loaded lazily on first predict
        artifact_fsv = str(metadata.get("feature_set_version"))
        self._version_mismatch = artifact_fsv != FEATURE_SET_VERSION
        if self._version_mismatch:
            logger.error(
                "ml_lt artifact %s was trained on feature set v%s but the code computes v%s — "
                "refusing to score (all predictions 0.0); retrain with `ibkr-trader train run`",
                self.version,
                artifact_fsv,
                FEATURE_SET_VERSION,
            )

    def predict(self, features: dict) -> float:
        if self._version_mismatch:
            return 0.0
        import pandas as pd

        nan = float("nan")
        x = pd.DataFrame([{c: features.get(c, nan) for c in self._feature_columns}])
        # fixed category lists from training metadata — LightGBM stores categoricals by code,
        # so predict-time frames must encode with the exact lists the model was fit with
        for column, cats in self._categories.items():
            if column in x.columns:
                x[column] = pd.Categorical(x[column], categories=cats)
        if self._booster is None:
            import lightgbm

            self._booster = lightgbm.Booster(model_file=str(self._artifact_dir / self._model_file))
        return float(self._booster.predict(x)[0]) - 0.5


@register
class MlLtRidge(Predictor):
    """Artifact-backed numeric ridge strategy saved alongside ``ml_lt`` LightGBM."""

    name = "ml_lt_ridge"
    version = "0"

    def __init__(self, model_dir: str | Path | None = None):
        _require_ridge_ml()
        if model_dir is None:
            from ibkr_trader.config import get_settings

            model_dir = get_settings().ml_lt_model_dir
        root = Path(model_dir)
        marker = root / "latest"
        if not marker.exists():
            raise FileNotFoundError(
                f"no trained ml_lt_ridge artifact under {root} — run `ibkr-trader train run` first"
            )
        artifact_version = marker.read_text().strip()
        self._artifact_dir = root / artifact_version
        metadata_path = self._artifact_dir / "metadata.json"
        if not metadata_path.exists():
            raise FileNotFoundError(
                f"latest marker points at {artifact_version!r} but {metadata_path} is gone"
            )
        metadata = json.loads(metadata_path.read_text())
        ridge = metadata.get("ridge")
        if not ridge:
            raise FileNotFoundError(
                f"artifact {artifact_version!r} has no ridge model — "
                "retrain with `ibkr-trader train run`"
            )
        self.version = str(metadata.get("version", artifact_version))
        self._model_file = str(ridge.get("model_file", "ridge.joblib"))
        self._feature_columns = list(ridge["numeric_feature_columns"])
        self._sklearn_version = str(ridge["sklearn_version"])
        self._model: Any = None
        artifact_fsv = str(metadata.get("feature_set_version"))
        self._version_mismatch = artifact_fsv != FEATURE_SET_VERSION
        if self._version_mismatch:
            logger.error(
                "ml_lt_ridge artifact %s was trained on feature set v%s but the code computes "
                "v%s — refusing to score (all predictions 0.0); retrain with "
                "`ibkr-trader train run`",
                self.version,
                artifact_fsv,
                FEATURE_SET_VERSION,
            )
        else:
            from ibkr_trader.signals.train import assert_sklearn_compatible

            assert_sklearn_compatible(self._sklearn_version)

    def predict(self, features: dict) -> float:
        if self._version_mismatch:
            return 0.0
        import pandas as pd

        nan = float("nan")
        x = pd.DataFrame([{column: features.get(column, nan) for column in self._feature_columns}])
        if self._model is None:
            from ibkr_trader.signals.train import RidgeModel

            self._model = RidgeModel.load(
                self._artifact_dir / self._model_file,
                trained_sklearn_version=self._sklearn_version,
            )
        return float(self._model.predict(x)[0]) - 0.5
