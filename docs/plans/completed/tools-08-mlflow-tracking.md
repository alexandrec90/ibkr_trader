# Plan TOOLS-08 — Local MLflow training-run comparison

Promoted from [TOOLS-07](tools-07-deferred-bench.md) on 2026-07-19 after Ridge became a
second first-class model family alongside LightGBM.

## Decisions

- MLflow is optional in a `[tracking]` extra and enabled explicitly with `--track-mlflow`.
- Tracking uses an explicit local `file:` URI. No remote or persistent tracking server is
  configured by the application. MLflow's required `MLFLOW_ALLOW_FILE_STORE=true` maintenance-
  mode opt-in is set by the tracking path.
- `models/ml_lt/<version>/metadata.json` remains the source of truth. MLflow receives that
  exact file plus scalar params and metrics derived from it for comparison; model artifacts
  are not duplicated.
- vectorbt and skfolio remain deferred because their TOOLS-07 triggers have not fired.

## Acceptance checklist

- [x] `train run --track-mlflow` writes a run to a caller-selected local directory
- [x] Missing `[tracking]` extra fails with a clear `uv sync` hint
- [x] Exact authoritative `metadata.json` is logged; comparison fields are derived from it
- [x] Default training behavior is unchanged when tracking is not requested
- [x] Full pytest / Ruff / mypy gate green (420 tests; coverage 90.59%)
