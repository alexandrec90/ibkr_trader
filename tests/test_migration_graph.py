"""The Alembic revisions form one chain: unique ids, known parents, a single head.

Two branches that each add a revision with the same id merge without a textual conflict --
the files have different names -- and Alembic then silently keeps one of them, so a
database upgraded on either branch never runs the other's.
"""

import ast
from collections import Counter
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _assigned(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} assigns no {name}")


def _revisions() -> dict[str, tuple[str, str | None]]:
    """file name -> (revision, down_revision)"""
    return {
        path.name: (_assigned(path, "revision"), _assigned(path, "down_revision"))
        for path in sorted(MIGRATIONS.glob("*.py"))
    }


def test_every_revision_id_is_unique():
    counts = Counter(revision for revision, _ in _revisions().values())
    assert [revision for revision, n in counts.items() if n > 1] == []


def test_every_parent_exists_and_there_is_one_root_and_one_head():
    pairs = list(_revisions().values())
    ids = {revision for revision, _ in pairs}
    parents = [down for _, down in pairs]

    assert [p for p in parents if p is not None and p not in ids] == []
    assert parents.count(None) == 1
    assert len(ids - set(parents)) == 1


def test_the_file_name_starts_with_its_revision_id():
    mismatched = [name for name, (rev, _) in _revisions().items() if not name.startswith(rev)]
    assert mismatched == []
