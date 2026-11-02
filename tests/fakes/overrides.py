"""Test helper: add override entries to a store's ``data/overrides/main/`` series through the real
``overrides.add`` run (:func:`stack.overrides.run_add`), as ``stack overrides add`` does."""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from stack.overrides import rows_of
from stack.runs import RunRecord
from stack.store.chain import EmptyChainError, read_view, resolve
from tests.synth.records import sdir

#: The series directory the test stores keep their overrides in (D377).
OVERRIDES_DIR = sdir("overrides")

_N = itertools.count()


def chain_ids(root: Path) -> set[str]:
    """The override ids already in ``root``'s chain."""
    try:
        return {o.override_id for o in rows_of(read_view(resolve(root, OVERRIDES_DIR)))}
    except EmptyChainError:
        return set()


def add_overrides(root: Path, target: str, entries: Sequence[Mapping[str, Any]], *,
                  only_new: bool = False) -> RunRecord | None:
    """One ``overrides.add`` run of ``entries`` (today's YAML entry format) for ``target``; with
    ``only_new`` the entries whose id the chain already has are left out (a test that re-states a
    whole file), and nothing runs when none is left. Asserts the run ended ``ok``."""
    from stack.overrides import run_add

    if only_new:
        have = chain_ids(root)
        entries = [e for e in entries if e["override_id"] not in have]
        if not entries:
            return None
    d = root.parent / f"{root.name}-overrides-in"
    d.mkdir(exist_ok=True)
    path = d / f"{next(_N):04d}.yaml"
    path.write_text(yaml.safe_dump({target: [dict(e) for e in entries]}, sort_keys=False))
    rec = run_add(root, path, OVERRIDES_DIR)
    assert rec.status == "ok", rec.error
    return rec


def overrides_input(root: Path) -> list[str]:
    """``[OVERRIDES_DIR]`` (an ``--in``) when the store has overrides, else ``[]``."""
    return [OVERRIDES_DIR] if chain_ids(root) else []
