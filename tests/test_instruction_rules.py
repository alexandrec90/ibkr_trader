from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
TESTING_RULE = REPO_ROOT / ".claude" / "rules" / "testing.md"


def test_testing_rule_is_scoped_to_ibkr_specific_policy():
    text = TESTING_RULE.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "description: IBKR-specific testing policy" in text
    assert "Settings.assert_trading_allowed()" in text
    assert "pytest --cov=ibkr_trader" in text
    assert "intentionally overrides devkit's" in text

    # These portable requirements are owned by devkit's engineering rule. Keeping
    # another authoritative copy here would let the two policies drift.
    assert "New or changed implemented code ships with tests" not in text
    assert "which test would fail if someone reverted my change" not in text
    assert "never lower it to make a change pass" not in text
