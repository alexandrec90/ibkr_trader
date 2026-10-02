from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
TESTING_RULE = REPO_ROOT / ".claude" / "rules" / "testing.md"


def test_testing_rule_is_scoped_to_ibkr_specific_policy():
    text = TESTING_RULE.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "description: IBKR-specific testing policy" in text
    assert "Settings.assert_trading_allowed()" in text
    assert "pytest --cov=ibkr_trader" in text


def test_testing_rule_does_not_order_a_whole_suite_run():
    """It once overrode devkit's session-scope rule with "run the full gate", a bare
    `pytest` that never collects `scripts/hooks/tests/`: every session that obeyed ran
    the suite (ledger d607d6b8), and PR #74 was called green on it while CI was red."""
    text = TESTING_RULE.read_text(encoding="utf-8")

    assert "intentionally overrides devkit's" not in text
    assert "Run the full gate" not in text
    assert ".claude/rules/session-scope.md" in text
    assert "scripts/hooks/tests/" in text

    # These portable requirements are owned by devkit's engineering rule. Keeping
    # another authoritative copy here would let the two policies drift.
    assert "New or changed implemented code ships with tests" not in text
    assert "which test would fail if someone reverted my change" not in text
    assert "never lower it to make a change pass" not in text
