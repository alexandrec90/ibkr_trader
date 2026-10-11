"""The Alembic revisions form one chain: unique ids, known parents, a single root and head.

Two branches that each add a revision off the same parent merge in git without a conflict:
the files have different names. Alembic then sees one revision id twice, and silently keeps
one of them, or two heads, and ``alembic upgrade head`` refuses to run. Either way a database
upgraded on one branch never runs the other's. These checks fail the suite on the merge
instead.
"""

import ast
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _assigned(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} assigns no {name}")


def _revisions() -> list[tuple[str, str, str | None]]:
    """(file name, revision, down_revision) for every revision file."""
    return [
        (path.name, _assigned(path, "revision"), _assigned(path, "down_revision"))
        for path in sorted(MIGRATIONS.glob("*.py"))
    ]


def duplicate_ids(revisions) -> dict[str, list[str]]:
    """Revision ids claimed by more than one file, with the files that claim them."""
    files: dict[str, list[str]] = {}
    for name, revision, _ in revisions:
        files.setdefault(revision, []).append(name)
    return {revision: names for revision, names in files.items() if len(names) > 1}


def heads(revisions) -> set[str]:
    """Revisions no other revision builds on."""
    parents = {down for _, _, down in revisions if down is not None}
    return {revision for _, revision, _ in revisions} - parents


def test_the_versions_directory_is_found():
    assert _revisions()


def test_every_revision_id_is_unique():
    assert duplicate_ids(_revisions()) == {}


def test_every_parent_exists_and_there_is_one_root_and_one_head():
    revisions = _revisions()
    ids = {revision for _, revision, _ in revisions}
    parents = [down for _, _, down in revisions]

    assert [p for p in parents if p is not None and p not in ids] == []
    assert parents.count(None) == 1
    assert len(heads(revisions)) == 1


def test_the_file_name_starts_with_its_revision_id():
    assert [name for name, revision, _ in _revisions() if not name.startswith(revision)] == []


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
