"""The Alembic revisions form one unbroken chain.

Two branches that each add a revision off the same parent merge in git without a conflict:
the files have different names. Alembic then sees one revision id twice, or two heads, and
``alembic upgrade head`` either silently skips a migration or refuses to run. These checks
fail the suite on the merge instead.
"""

from importlib import import_module
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _revisions() -> list[tuple[str, str, str | None]]:
    """(file stem, revision, down_revision) for every revision file."""
    found = []
    for path in sorted(VERSIONS.glob("*.py")):
        module = import_module(f"migrations.versions.{path.stem}")
        found.append((path.stem, module.revision, module.down_revision))
    return found


def duplicate_ids(revisions) -> dict[str, list[str]]:
    """Revision ids claimed by more than one file, with the files that claim them."""
    files: dict[str, list[str]] = {}
    for stem, revision, _ in revisions:
        files.setdefault(revision, []).append(stem)
    return {revision: stems for revision, stems in files.items() if len(stems) > 1}


def heads(revisions) -> set[str]:
    """Revisions no other revision builds on."""
    parents = {down for _, _, down in revisions if down is not None}
    return {revision for _, revision, _ in revisions} - parents


def test_the_versions_directory_is_found():
    assert _revisions()


def test_every_revision_id_is_unique():
    assert duplicate_ids(_revisions()) == {}


def test_there_is_exactly_one_head():
    assert len(heads(_revisions())) == 1


def test_every_parent_exists():
    ids = {revision for _, revision, _ in _revisions()}
    assert {down for _, _, down in _revisions() if down is not None} <= ids


def test_each_file_is_named_after_its_revision():
    assert [stem for stem, revision, _ in _revisions() if not stem.startswith(revision)] == []


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        ([("a_x", "a", None), ("b_y", "b", "a")], {}),
        ([("a_x", "a", None), ("a_y", "a", None)], {"a": ["a_x", "a_y"]}),
    ],
)
def test_duplicate_ids(revisions, expected):
    assert duplicate_ids(revisions) == expected


def test_heads_of_a_fork_are_both_tips():
    forked = [("a_x", "a", None), ("b_y", "b", "a"), ("c_z", "c", "a")]
    assert heads(forked) == {"b", "c"}
    assert heads(forked[:2]) == {"b"}
