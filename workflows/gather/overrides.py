"""The ``identity`` overrides as the operations gather applies (§6, D74; the ``overrides``
family, :mod:`stack.overrides`). Effective rows only (:func:`stack.overrides.effective`: revokes
applied, one row per key: a later merge, separate or mask of the same source set replaces an
earlier one, D337).

* ``merge`` (``key.source_ids``): human edges ``human:merge`` between the named sources, star
  from the earliest; never removed by a ``separate``; may cross channel groups (a human says so).
* ``separate`` (``key.source_ids``): every pair of the named sources loses its **key** edges
  (computed edges only, as the v4e mock: "a separate removes only direct computed edges of the
  pair"). If the pair still ends up in one component (another key, a third source, a human
  merge), the separate is **bridged**: the document is masked with
  ``mask_reason = 'review_required'`` and the separate's id is in its ``override_ids``.
* ``mask`` (``key.source_id``): the member is not live (``MEMBER.is_live = false``,
  ``MEMBER.override_id`` = the mask's id); it stays in the component and may stay the anchor.
  The document is masked (``is_masked``) when no member is live; its ``mask_reason`` is the
  anchor's mask reason. ``mask_reason`` ∈ ``{operator, review_required}``; a mask without one is
  ``operator`` (a human masked it).

An override naming a ``source_id`` that no input chain speaks for is refused (GatherError).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from stack.overrides import IDENTITY, Override, effective
from stack.schemas.vocab import MASK_REASONS
from workflows.gather.normalise import GatherError


@dataclass(frozen=True)
class Mask:
    override_id: str
    reason: str


@dataclass(frozen=True)
class IdentityOps:
    """The active identity overrides, by kind."""

    merges: tuple[tuple[str, tuple[str, ...]], ...] = ()
    separates: tuple[tuple[str, frozenset[str]], ...] = ()
    masks: dict[str, Mask] = field(default_factory=dict)
    config_digest: str | None = None

    @property
    def separated_pairs(self) -> frozenset[frozenset[str]]:
        return frozenset(frozenset(p) for _, ss in self.separates
                         for p in itertools.combinations(sorted(ss), 2))

    def source_ids(self) -> set[str]:
        return ({s for _, ss in self.merges for s in ss} | {s for _, ss in self.separates for s in ss}
                | set(self.masks))

    def bridged(self, members: Collection[str]) -> list[str]:
        """Ids of separates with two or more of their sources in ``members``."""
        m = set(members)
        return [oid for oid, ss in self.separates if len(ss & m) >= 2]

    def touching(self, members: Collection[str]) -> list[str]:
        """Ids of every active entry naming a source in ``members``, sorted."""
        m = set(members)
        ids = {oid for oid, ss in self.merges if m.intersection(ss)}
        ids |= {oid for oid, ss in self.separates if m & ss}
        ids |= {mk.override_id for s, mk in self.masks.items() if s in m}
        return sorted(ids)


def from_rows(rows: Iterable[Override]) -> IdentityOps:
    """The operations of the effective ``identity`` rows of ``rows`` (every ``overrides`` row)."""
    eff = effective(rows, IDENTITY)
    merges: list[tuple[str, tuple[str, ...]]] = []
    separates: list[tuple[str, frozenset[str]]] = []
    masks: dict[str, Mask] = {}
    for o in sorted(eff.rows, key=lambda o: o.override_id):
        value = o.value
        if value == "merge":
            merges.append((o.override_id, tuple(json.loads(o.key))))
        elif value == "separate":
            separates.append((o.override_id, frozenset(json.loads(o.key))))
        elif value == "mask":
            reason = o.payload.get("mask_reason") or "operator"
            if reason not in MASK_REASONS:
                raise GatherError(f"identity override {o.override_id}: mask_reason {reason!r} not in {MASK_REASONS}")
            masks[o.key] = Mask(o.override_id, reason)
        else:
            raise GatherError(f"identity override {o.override_id}: value {value!r} not merge/separate/mask")
    return IdentityOps(tuple(merges), tuple(separates), masks, eff.digest)


def check_known(ops: IdentityOps, known: Iterable[str]) -> None:
    unknown = sorted(ops.source_ids() - set(known))
    if unknown:
        raise GatherError(f"identity overrides name source ids no sources input speaks for: {unknown}")


__all__ = ["IdentityOps", "Mask", "check_known", "from_rows"]
