"""The anchor and ``doc_id`` continuity against the ``documents`` head (§6, §2.5).

**Anchor.** The member with the earliest ``first_observed_at``, tiebreak the smaller
``source_id``, masked members included; ``doc_id`` = the anchor's ``source_id``. (Not
:mod:`stack.anchor`, which is quote→span anchoring for annotations.)

**Continuity.** Every id the head knows (its ``doc_id``s and every ``former_doc_ids`` entry) is
either a ``doc_id`` of this gather (it continues, or a ``retired`` row revives: ``retired →
identified``, §2.5, e.g. after a ``separate``) or it is (or stays) a tombstone. For a tombstone
``f`` take its head row's members ``M_f`` (the row is frozen at retirement) and the new
documents holding them:

* one document ``D`` → a **merge**: ``merged_into = D``, ``f ∈ D.former_doc_ids``. With the
  anchor rule this makes the survivor of two joined documents the one whose anchor is earliest;
  a merge of merged documents points every tombstone at the live survivor (no chains);
* several → a **split**: ``merged_into`` NULL and every part lists ``f`` in ``former_doc_ids``.

A component holding a previous ``doc_id``'s anchor keeps that id unless an earlier member joined
it (then it is a merge into the earlier one). So a split-off part of a document that continues
is a new document (its members are counted ``moved_out``: neither a merge nor a split, S25-19); ``former_doc_ids`` never lists an id that is live in the same file (it is
the site's alias map, §2.9; a live id needs no alias). A head member that no ``sources`` chain
speaks for is an input error (a chain not pinned), never a silent drop.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from workflows.gather.normalise import GatherError

Row = dict[str, Any]


def anchor_of(component: Sequence[str], order: Any) -> str:
    """The earliest member by ``order`` (``(first_observed_at, source_id)``)."""
    return min(component, key=order)


@dataclass
class Continuity:
    """The outcome of :func:`continuity`."""

    former: dict[str, list[str]] = field(default_factory=dict)       # live doc_id -> sorted ids
    merged_into: dict[str, str | None] = field(default_factory=dict)  # tombstone id -> target
    revived: set[str] = field(default_factory=set)
    merges: int = 0
    splits: int = 0
    moved_out: int = 0   # members of a continuing head document now in another document


def _members(row: Row) -> list[str]:
    return [m["source_id"] for m in (row["identity"] or {}).get("members") or []]


def continuity(docs: Mapping[str, Sequence[str]], head: Mapping[str, Row]) -> Continuity:
    """``docs``: new ``doc_id`` → member ids; ``head``: the head view's rows by ``doc_id``."""
    owner = {s: d for d, ms in docs.items() for s in ms}
    known: set[str] = set(head)
    for r in head.values():
        known.update((r["identity"] or {}).get("former_doc_ids") or [])
    out = Continuity(former={d: [] for d in docs})
    former: dict[str, set[str]] = defaultdict(set)
    for f in sorted(known):
        row = head.get(f)
        if f in docs:
            if row is not None and row["state"] == "retired":
                out.revived.add(f)
            elif row is not None:
                out.moved_out += sum(s in owner and owner[s] != f for s in _members(row))
            continue
        if row is None:
            raise GatherError(f"head lists {f} in former_doc_ids but has no tombstone row for it")
        ms = _members(row) or [f]
        lost = [s for s in ms if s not in owner]
        if lost:
            raise GatherError(f"head document {f}: member(s) {lost} have no non-miss row in any sources "
                              f"input (is a sources chain missing from the inputs, or did a compaction "
                              f"keep only a miss for them?)")
        parts = sorted({owner[s] for s in ms})
        if len(parts) == 1:
            out.merged_into[f] = parts[0]
            out.merges += row["state"] != "retired" or row["identity"]["merged_into"] != parts[0]
        else:
            out.merged_into[f] = None
            out.splits += row["state"] != "retired" or row["identity"]["merged_into"] is not None
        for d in parts:
            former[d].add(f)
    for d, fs in former.items():
        out.former[d] = sorted(fs)
    return out


__all__ = ["Continuity", "anchor_of", "continuity"]
