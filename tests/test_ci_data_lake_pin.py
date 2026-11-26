"""The PR gate checks data-lake out at a pinned commit; the nightly at its default branch.

``uv.lock`` records the editable ``../data-lake`` package's own metadata, its dev group
included, so any edit to data-lake's ``pyproject.toml`` stales this repo's lock. While the
PR gate checked out data-lake's default branch, each such edit (a devkit ``pytest`` floor,
a Dependabot ``ruff`` bump) turned ``uv sync --locked`` red on this repo's main with no
push here. Pinning the gate makes it test the pair the lock was resolved against; the
nightly stays on the default branch so that drift still surfaces, once, as a signal to
bump the pin and relock together.
"""

import pathlib
import re

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"

FULL_SHA = re.compile(r"[0-9a-f]{40}")
STEP = re.compile(r"^\s*- name:\s*(?P<name>.+?)\s*$")
REF = re.compile(r"^\s*ref:\s*(?P<ref>\S+)\s*$")


def data_lake_refs(text: str) -> list[str | None]:
    """The ``ref:`` of every "Checkout data-lake" step, or None where the step has none."""
    refs: list[str | None] = []
    in_step = False
    for line in text.splitlines():
        step = STEP.match(line)
        if step:
            in_step = step["name"] == "Checkout data-lake"
            if in_step:
                refs.append(None)
            continue
        ref = REF.match(line)
        if in_step and ref:
            refs[-1] = ref["ref"]
    return refs


def test_pr_gate_pins_data_lake_to_a_full_commit_sha():
    refs = data_lake_refs((WORKFLOWS / "pr-gate.yml").read_text(encoding="utf-8"))
    assert refs, "pr-gate.yml no longer checks out data-lake; update this test with it"
    unpinned = [ref for ref in refs if ref is None or not FULL_SHA.fullmatch(ref)]
    assert not unpinned, (
        f"pr-gate.yml checks out data-lake at {unpinned}, not a full commit SHA: a moving "
        "ref stales uv.lock whenever data-lake's pyproject changes. Pin the commit uv.lock "
        "was resolved against."
    )


def test_nightly_follows_data_lake_default_branch():
    refs = data_lake_refs((WORKFLOWS / "nightly.yml").read_text(encoding="utf-8"))
    assert refs, "nightly.yml no longer checks out data-lake; update this test with it"
    assert refs == [None] * len(refs), (
        "the nightly is the only run that sees data-lake's main; pinning it too would hide "
        "the drift the PR gate's pin defers to it"
    )


def test_data_lake_refs_reads_only_the_data_lake_checkout():
    text = (
        "      - name: Check out devkit\n"
        "        uses: actions/checkout@v7\n"
        "        with:\n"
        "          ref: v0.11.32\n"
        "      - name: Checkout data-lake\n"
        "        uses: actions/checkout@v7\n"
        "        with:\n"
        "          repository: alexandrec90/data-lake\n"
        "          ref: main\n"
        "      - name: Checkout data-lake\n"
        "        uses: actions/checkout@v7\n"
        "      - name: Install uv\n"
        "        with:\n"
        "          ref: ignored\n"
    )
    assert data_lake_refs(text) == ["main", None]


def test_data_lake_refs_is_empty_without_the_step():
    assert data_lake_refs("      - name: Install uv\n        run: pip install uv\n") == []


def test_full_sha_rejects_branches_tags_and_short_shas():
    assert FULL_SHA.fullmatch("430536af65d96366d18dcbd6d460047a9411f3ed")
    for bad in ("main", "v1.2.3", "430536a", "430536AF65D96366D18DCBD6D460047A9411F3ED"):
        assert not FULL_SHA.fullmatch(bad), bad
