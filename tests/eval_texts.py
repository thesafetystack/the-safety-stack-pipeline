"""The frozen eval sets' whole texts and archived items for tests (D790, D795): not in the repository, looked up,
never fetched.

A test that needs a set's whole texts calls :func:`require_eval_texts` with the set directories first. When a
text is in neither the set's own ``texts/`` nor the eval-text cache (:func:`stack.evals.freeze.find_text`;
``$STACK_EVAL_TEXTS_DIR``, default ``~/.cache/stack/eval-texts``) the test is skipped with a warning naming the
cache and the fetch tool. Fetching (``tools/fetch_eval_texts.py``, network) is never part of a test.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from stack.evals.freeze import (ARCHIVE_NOTE, FETCH_TEXTS_HINT, TEXTS_CACHE_ENV, find_eval_file, find_text, items_file,
                                texts_cache_root)

#: What runs without the archive.
WITHOUT = '`pytest -m "not slow and not evalsets"` needs no eval texts'


class EvalTextsMissing(UserWarning):
    """A test was skipped because eval-set whole texts are not in the cache."""


def missing_texts(set_dir: Path) -> list[str]:
    """Item ids of ``set_dir`` with a text digest whose whole text :func:`find_text` does not find; ``["items.jsonl"]``
    when the set's items are archived (D795) and not in the cache either."""
    path = items_file(set_dir)
    if not path.is_file():
        return ["items.jsonl"]
    out = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        inp = row.get("input") if isinstance(row.get("input"), dict) else {}
        if (inp.get("full_text_digest") or row.get("text_digest")) and find_text(set_dir, row["item_id"]) is None:
            out.append(row["item_id"])
    return out


def require_eval_texts(*set_dirs: Path) -> None:
    """Skip the calling test (and warn) unless every whole text of every set in ``set_dirs`` is available."""
    gaps = {f"{d.parent.name}/{d.name}": len(m) for d in set_dirs if (m := missing_texts(d))}
    if gaps:
        msg = (f"eval-set whole texts not available ({', '.join(f'{k}: {n} missing' for k, n in gaps.items())}); "
               f"they are not in the repository: fill the eval-text cache {texts_cache_root()} "
               f"(${TEXTS_CACHE_ENV}) with `{FETCH_TEXTS_HINT}` ({ARCHIVE_NOTE}); {WITHOUT}")
        warnings.warn(msg, EvalTextsMissing, stacklevel=2)
        pytest.skip(msg)


def eval_text_file(set_dir: Path, item_id: str) -> Path:
    """``item_id``'s whole text file (the set's ``texts/`` or the cache's); skips like :func:`require_eval_texts`."""
    p = find_text(set_dir, item_id)
    if p is None:
        require_eval_texts(set_dir)
        raise AssertionError(f"{set_dir}: {item_id} has no text digest")  # require skipped otherwise
    return p


def require_eval_files(evals_root: Path, rels: list[str]) -> None:
    """Skip the calling test (and warn) unless every eval file of ``rels`` (paths under ``evals/``) is in the
    repository or the cache (archived files, D793, D795)."""
    gaps = [r for r in rels if find_eval_file(evals_root, r) is None]
    if gaps:
        msg = (f"archived eval files not available ({len(gaps)}, e.g. {gaps[0]}); fill the eval-text cache "
               f"{texts_cache_root()} (${TEXTS_CACHE_ENV}) with `{FETCH_TEXTS_HINT}` ({ARCHIVE_NOTE}); {WITHOUT}")
        warnings.warn(msg, EvalTextsMissing, stacklevel=2)
        pytest.skip(msg)
