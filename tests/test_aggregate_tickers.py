"""Tests for `scripts/aggregate-tickers.py`, which builds the backtest universe.

Two properties this script exists to hold, both stated in its own docstring and neither
visible at the call site: `tickers.txt` is written comment-free, because cli.py's
universe reader does not skip `#` lines and would resolve a comment as a symbol; and a
cross-listing that collapses two different source entries onto one bare symbol is
reported rather than silently deduped, because the engine resolves universe lines by
bare symbol and cannot tell TSX `K.TO` from US `K` apart afterwards.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load a kebab-case script by path — it is not importable under its own name."""
    path = REPO_ROOT / "scripts" / name
    module_name = f"_test_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # The script imports `ingest_fmp_tickers` as a sibling, so scripts/ has to be on
    # the path for the exec to resolve it.
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
    return module


@pytest.fixture
def aggregate() -> ModuleType:
    return load_script("aggregate-tickers.py")


def _seed(workspace: Path, fmp: str, yahoo: str) -> None:
    (workspace / "tickers-fmp.txt").write_text(fmp, encoding="utf-8")
    (workspace / "tickers-yahoo.txt").write_text(yahoo, encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("xeqt.to", "XEQT"),  # Yahoo suffix stripped, as the yahoo connector does
        ("  aapl  ", "AAPL"),  # surrounding whitespace is not part of the symbol
        ("K", "K"),  # a bare symbol is already canonical
        ("TO", "TO"),  # only the *suffix* goes, never a symbol that spells it
        ("BRK.B", "BRK.B"),  # a non-Yahoo dot is part of the symbol
    ],
)
def test_canonical_strips_only_the_yahoo_suffix(aggregate, raw, expected):
    assert aggregate.canonical(raw) == expected


def test_main_merges_both_lists_sorted_deduped_and_comment_free(aggregate, tmp_path, capsys):
    _seed(
        tmp_path,
        fmp="# an FMP comment\nMSFT\naapl\n\nAAPL\n",
        yahoo="XEQT.TO\n# a Yahoo comment\nMSFT\n",
    )

    assert aggregate.main(tmp_path) == 0

    written = (tmp_path / "tickers.txt").read_text(encoding="utf-8")
    assert written == "AAPL\nMSFT\nXEQT\n"
    # The reader on the other end does not skip them, so a comment here is a fake symbol.
    assert "#" not in written
    assert "wrote 3 symbols" in capsys.readouterr().out


def test_main_reports_a_cross_listing_collision_and_exits_non_zero(aggregate, tmp_path, capsys):
    # TSX `K.TO` (Kinross) and US `K` (Kellogg) are different instruments that collapse
    # onto the same bare symbol — the case the engine's lookup cannot disambiguate.
    _seed(tmp_path, fmp="K\nMSFT\n", yahoo="K.TO\n")

    assert aggregate.main(tmp_path) == 1

    output = capsys.readouterr().out
    assert "WARNING" in output
    assert "K: 'K' vs 'K.TO'" in output
    # The file is still written: the warning is a report, not a refusal to produce it.
    assert (tmp_path / "tickers.txt").read_text(encoding="utf-8") == "K\nMSFT\n"


def test_main_does_not_call_a_same_symbol_in_both_lists_a_collision(aggregate, tmp_path, capsys):
    """The guard fires on two *different* raw entries, not on an ordinary overlap."""
    _seed(tmp_path, fmp="MSFT\nAAPL\n", yahoo="MSFT\n")

    assert aggregate.main(tmp_path) == 0
    assert "WARNING" not in capsys.readouterr().out


def test_main_defaults_to_the_repo_root(aggregate):
    """The task invokes `main()` with no argument; the default has to be the workspace."""
    import inspect

    assert inspect.signature(aggregate.main).parameters["workspace"].default is None
    assert (REPO_ROOT / "tickers-fmp.txt").is_file()
    assert (REPO_ROOT / "tickers-yahoo.txt").is_file()
