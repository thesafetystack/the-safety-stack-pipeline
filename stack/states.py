"""Row states (§2.5, D2, D5, D13): the four ``documents`` states, the blocks and digest
agreements each requires (S8/S9's data), the forward-only rule with its two exceptions (X19),
and the ``sources`` states. Pure data and two functions; the checks (T1.4) and ``gather`` (M2)
import it.
"""

from __future__ import annotations

from stack.schemas.vocab import DOCUMENT_STATES

STATES: tuple[str, ...] = DOCUMENT_STATES  # identified, triaged, acquired, retired

#: Block paths that must be non-NULL in each state (S8). Cumulative: ``acquired`` is cumulative
#: over ``triaged`` (D5). ``retired`` freezes the other blocks; ``identity.merged_into`` is set on
#: a merge and NULL on a split, so it is not required.
REQUIRED_BLOCKS: dict[str, tuple[str, ...]] = {
    "identified": ("identity", "metadata", "inband"),
    "triaged": ("identity", "metadata", "inband", "annotations.triage"),
    "acquired": ("identity", "metadata", "inband", "annotations.triage", "content"),
    "retired": ("identity", "metadata", "inband"),
}

#: Digest agreements per state (S9): ``(left path, right path)`` must be equal; plus
#: ``annotations.triage.basis_kind = 'fields'`` on ``triaged`` and ``acquired``.
DIGEST_AGREEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "identified": (),
    "triaged": (("annotations.triage.basis_digest", "metadata.fields_digest"),),
    "acquired": (
        ("annotations.triage.basis_digest", "metadata.fields_digest"),
        ("content.members_digest", "identity.members_digest"),
    ),
    "retired": (),
}
TRIAGE_BASIS_KIND = "fields"

#: Forward order of the live states; ``retired`` is outside the order (a tombstone).
_RANK: dict[str, int] = {"identified": 0, "triaged": 1, "acquired": 2}

def is_forward(a: str, b: str, *, digest_changed: bool = False, separate: bool = False) -> bool:
    """Whether a row may go from state ``a`` to state ``b`` (X19). Staying put is allowed.
    Forward by the owning phase; ``→ retired`` from any live state (merge or split);
    ``→ identified`` from ``triaged``/``acquired`` only when ``gather`` changed
    ``members_digest`` or ``fields_digest`` (``digest_changed``); ``retired → identified`` only on
    ``separate``. Everything else is monotone."""
    for s in (a, b):
        if s not in REQUIRED_BLOCKS:
            raise KeyError(f"unknown state {s!r}; states: {STATES}")
    if a == b:
        return True
    if a == "retired":
        return b == "identified" and separate
    if b == "retired":
        return True
    if b == "identified":
        return digest_changed
    return _RANK[b] > _RANK[a]
