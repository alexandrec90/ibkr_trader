"""`scripts/e2e.py`: the contract devkit's nightly `devkit-local-e2e` job reads.

pytest itself is faked: what is asserted is the exit code and the `FAILED <id>` lines the
fix pass groups by, for each shape of junit report a run can leave -- and, through a real
`--collect-only`, that the default run deselects the tier and the tier's own run selects it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TIER_TEST = "tests/test_e2e_postgres.py"


def load_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "e2e.py"
    name = "_test_e2e_script"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


JUNIT_ROW = '<testcase classname="tests.test_x" name="test_{name}">{body}</testcase>'


def junit(*rows: str) -> str:
    return f'<testsuites><testsuite name="pytest">{"".join(rows)}</testsuite></testsuites>'


def passed(name: str = "ok") -> str:
    return JUNIT_ROW.format(name=name, body="")


def ended(tag: str, name: str, message: str) -> str:
    return JUNIT_ROW.format(name=name, body=f'<{tag} message="{message}">detail</{tag}>')


@pytest.fixture
def e2e(monkeypatch, tmp_path) -> ModuleType:
    """The script, reporting into a temp tree rather than this checkout's `logs/`."""
    script = load_script()
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "REPORT", tmp_path / "logs" / "e2e-junit.xml")
    return script


def run_e2e(script, report: str | None, code: int = 0, env=None, argv=()) -> tuple[int, list]:
    """`main` over a fake pytest that writes `report`; `(exit code, argvs it was given)`."""
    seen = []

    def runner(cmd, **kwargs):
        seen.append(cmd)
        if report is not None:
            script.REPORT.write_text(report, encoding="utf-8")
        return SimpleNamespace(returncode=code, stdout="pytest said\n", stderr="")

    return script.main(list(argv), runner=runner, env=env or {}), seen


def _collect(*extra: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "pytest", TIER_TEST, "--collect-only", "-q"]
    argv += ["-p", "no:cacheprovider", *extra]
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def test_the_tier_is_deselected_by_default_and_selected_by_its_own_run():
    """The default run must stay the hermetic suite CI runs, and the tier's `-m` must
    really replace the one in `addopts`: pytest is the only witness to both."""
    default = _collect()
    assert default.returncode == 5, default.stdout + default.stderr  # 5: nothing collected
    tier = _collect("-m", "local")
    assert tier.returncode == 0, tier.stdout + tier.stderr
    assert "test_the_chain_migrates_an_empty_postgres" in tier.stdout


def test_the_tier_installs_the_ui_extra_and_leaves_the_lock_and_venv_alone():
    """Without `ui` the tier cannot import the app; `--frozen` keeps `uv.lock` untouched,
    which the nightly job requires; `--inexact` keeps the checkout's other extras."""
    argv = load_script().pytest_argv(Path("report.xml"))
    assert argv[:9] == [
        "uv",
        "run",
        "--frozen",
        "--inexact",
        "--extra",
        "ui",
        "python",
        "-m",
        "pytest",
    ]
    assert argv[argv.index("-m", 9) + 1] == "local"
    assert "tests" in argv
    assert "--junitxml=report.xml" in argv


def test_named_tests_replace_the_tree_but_stay_in_the_tier():
    argv = load_script().pytest_argv(Path("r.xml"), ["tests/test_x.py::test_y"])
    assert "tests/test_x.py::test_y" in argv and "tests" not in argv
    assert argv[argv.index("-m", 9) + 1] == "local"


@pytest.mark.parametrize(
    ("classname", "name", "ident"),
    [
        ("tests.test_x", "test_y", "tests/test_x.py::test_y"),
        ("tests.test_x.TestZ", "test_y", "tests/test_x.py::TestZ::test_y"),
        ("", "tests.test_x", "tests.test_x"),
    ],
)
def test_ids_read_as_pytests_summary_reads_them(classname, name, ident):
    assert load_script().pytest_id(classname, name) == ident


def test_an_unreadable_report_is_no_cases(e2e, tmp_path):
    assert e2e.cases(tmp_path / "absent.xml") == []
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><oops", encoding="utf-8")
    assert e2e.cases(broken) == []


def test_cases_reads_each_outcome(e2e, tmp_path):
    report = tmp_path / "r.xml"
    rows = (passed(), ended("failure", "bad", "boom"), ended("skipped", "away", "why"))
    report.write_text(junit(*rows), encoding="utf-8")
    assert [(c.ident, c.outcome) for c in e2e.cases(report)] == [
        ("tests/test_x.py::test_ok", e2e.PASSED),
        ("tests/test_x.py::test_bad", e2e.FAILED),
        ("tests/test_x.py::test_away", e2e.SKIPPED),
    ]


def test_a_green_run_exits_zero_and_prints_no_failure(e2e, capsys):
    code, seen = run_e2e(e2e, junit(passed()))
    assert code == 0
    assert len(seen) == 1
    out = capsys.readouterr().out
    assert "FAILED" not in out
    assert "e2e: report in logs/e2e-junit.xml" in out


def test_a_failure_exits_with_pytests_code_and_names_the_test(e2e, capsys):
    code, _ = run_e2e(e2e, junit(passed(), ended("failure", "bad", "boom")), code=1)
    assert code == 1
    assert "FAILED tests/test_x.py::test_bad - boom" in capsys.readouterr().out


def test_a_fixture_error_is_printed_as_failed(e2e, capsys):
    """pytest's own line says `ERROR`, and the signature is read from `FAILED` lines, so
    a database that stopped answering would be signed by an exit code alone."""
    run_e2e(e2e, junit(ended("error", "down", "connection refused")), code=1)
    assert "FAILED tests/test_x.py::test_down - connection refused" in capsys.readouterr().out


def test_a_skip_is_listed_by_hand_and_fails_under_the_scheduler(e2e, capsys):
    report = junit(passed(), ended("skipped", "away", "no db"))
    assert run_e2e(e2e, report)[0] == 0
    assert "SKIPPED tests/test_x.py::test_away - skipped: no db" in capsys.readouterr().out

    assert run_e2e(e2e, report, env={e2e.ENV_FLAG: "1"})[0] == 1
    assert "FAILED tests/test_x.py::test_away - skipped: no db" in capsys.readouterr().out


@pytest.mark.parametrize("report", [None, junit(), junit(ended("skipped", "s", "why"))])
def test_a_run_that_executed_nothing_is_red_even_by_hand(e2e, capsys, report):
    """A green from zero tests is a skip reported as a pass."""
    assert run_e2e(e2e, report, code=0)[0] == 1
    assert "not a pass" in capsys.readouterr().out


def test_pytests_own_failure_code_survives_a_run_that_executed_nothing(e2e):
    """A usage error or an interrupted run exits 2 or 4, which says more than a bare 1."""
    assert e2e.verdict(4, [], strict=False)[0] == 4
    skipped = e2e.Case("tests/test_x.py::test_y", e2e.SKIPPED, "no db")
    assert e2e.verdict(2, [skipped], strict=True)[0] == 2


def test_a_stale_report_cannot_answer_for_this_run(e2e):
    """Left in place, last night's green would read as tonight's for a pytest that died
    before it wrote one."""
    e2e.REPORT.parent.mkdir(parents=True)
    e2e.REPORT.write_text(junit(passed()), encoding="utf-8")
    assert run_e2e(e2e, None, code=2)[0] == 2


def test_output_that_a_legacy_console_cannot_encode_still_prints(e2e, capsys):
    """A by-hand run on cp1252 must not die printing the traceback it exists to show."""
    assert e2e._printable("caf\N{LATIN SMALL LETTER E WITH ACUTE}") == "caf\\xe9"
    assert run_e2e(e2e, junit(ended("failure", "bad", "\N{RIGHTWARDS ARROW}")), code=1)[0] == 1
    assert "FAILED tests/test_x.py::test_bad - \\u2192" in capsys.readouterr().out
