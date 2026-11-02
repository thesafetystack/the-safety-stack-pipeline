"""The basis of an annotation (§3.1 ``annotate.<wf>``, A.4 ``basis_kind``; D19, D99): what a
workflow reads from a ``corpus`` row, its digest, the member that supplied it, and the work key.

* ``basis_kind = 'text'`` (screen, affiliation): the row's current text,
  ``content.current_text_sha256``; the text itself is the ``text`` blob of that digest
  (``<store>/blobs``, bucket ``text``), read only when the workflow is asked (a rerun that
  carries the prior evidence reads no blob). The supplying member is ``content.current_source_id`` (the anchor when
  NULL). No ``content`` or no current text digest → no basis, ``skipped[no_text]``.
* ``basis_kind = 'fields'`` (doc_class; triage's questions use the same rule): the row's
  ``metadata.fields_digest``; the fields are the ``metadata`` block. The supplier is the
  document's anchor. A row with neither ``title`` nor ``abstract`` has no basis,
  ``skipped[no_basis]``.
* ``basis_kind = 'chained'`` (D235; a registry entry with ``reads``, model_roles): the current
  text plus each read's typed ``ANN`` in the row's ``annotations`` block, which the stage joined
  from its inputs (:mod:`stack.annotate.inputs`): :func:`stack.fold.chained_basis`. The text is
  still what the model reads (loaded like ``text``), and the supplier is ``text``'s. No current
  text → ``skipped[no_text]``; a read absent or not ``ok``/``empty`` → ``skipped[no_basis]``.

``work_digest = H([source_id, basis_digest, config_digest, item_key])`` (D19), ``H`` =
sha256 of the canonical JSON (:func:`stack.checks.digests.H`). An invocation is per document,
so its ``item_key`` is ``''`` at both grains; item rows of a ``<wf>`` file take their key from
the payload.

``is_basis_current`` is :func:`stack.fold.basis_current` (D99): ``basis_digest`` equals the
row's current basis, NULL when either side is NULL. ``select`` writes it and X7 recomputes it
with that one function, so neither can drift from ``fold.build``'s mapping M.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stack.checks.digests import H
from stack.fold import basis_current, chained_basis

Row = Mapping[str, Any]

#: ``skipped[<error_class>]`` of a row without a basis, per ``basis_kind`` (``chained``: a row
#: with a text but a missing or undecided read; without a text it is ``no_text``).
NO_BASIS: dict[str, str] = {"text": "no_text", "fields": "no_basis", "chained": "no_basis"}


@dataclass(frozen=True)
class Basis:
    """The current basis of one ``corpus`` row for one workflow. ``digest is None`` = no basis
    (``missing`` names the ``skipped`` error class)."""

    kind: str
    digest: str | None
    source_id: str
    license_class: str | None
    no_text: bool = False

    @property
    def missing(self) -> str | None:
        if self.digest is not None:
            return None
        return "no_text" if self.no_text else NO_BASIS[self.kind]


def _member(row: Row, source_id: str) -> Row | None:
    return next((m for m in row["identity"]["members"] or [] if m["source_id"] == source_id), None)


def current_digests(row: Row) -> tuple[str | None, str | None]:
    """``(content.current_text_sha256, metadata.fields_digest)`` of a ``corpus`` row."""
    content = row.get("content")
    return (content or {}).get("current_text_sha256"), row["metadata"]["fields_digest"]


def basis_of(row: Row, kind: str, reads: tuple[str, ...] = ()) -> Basis:
    """The current basis of ``row`` for a workflow of ``basis_kind`` ``kind`` (module docstring);
    ``reads`` = the workflow's registry ``reads`` (``chained`` only)."""
    doc = row["doc_id"]
    no_text = False
    if kind in ("text", "chained"):
        content = row.get("content") or {}
        digest = content.get("current_text_sha256")
        src = content.get("current_source_id") or row["identity"]["anchor_source_id"] or doc
        if kind == "chained":
            if not reads:
                raise ValueError("basis_kind 'chained' needs the workflow's reads")
            no_text = digest is None
            digest = chained_basis(digest, row.get("annotations"), reads)
    elif kind == "fields":
        md = row["metadata"]
        digest = md["fields_digest"] if (md.get("title") or md.get("abstract")) else None
        src = row["identity"]["anchor_source_id"] or doc
    else:
        raise ValueError(f"basis_kind {kind!r} not in {sorted(NO_BASIS)}")
    m = _member(row, src)
    return Basis(kind, digest, src, m["license_class"] if m is not None else None, no_text)


def live_members(row: Row) -> list[str]:
    """Sorted ``source_id`` of the row's live members (an override's ``scope.members``)."""
    return sorted(m["source_id"] for m in row["identity"]["members"] or [] if m["is_live"])


def work_digest(source_id: str, basis_digest: str, config_digest: str, item_key: str = "") -> str:
    """The work key (D19): ``H([source_id, basis_digest, config_digest, item_key])``."""
    return H([source_id, basis_digest, config_digest, item_key])


def is_basis_current(basis_kind: str, basis_digest: str | None, row: Row, workflow: str = "?") -> bool | None:
    """``is_basis_current`` of ``workflow``'s annotation with this basis against ``row`` (D99; a
    ``chained`` basis reads the row's joined ``annotations`` and the workflow's registry reads)."""
    text, fields = current_digests(row)
    return basis_current({"basis_kind": basis_kind, "basis_digest": basis_digest,
                          "workflow": workflow}, text, fields, row.get("annotations"))


__all__ = ["NO_BASIS", "Basis", "basis_of", "current_digests", "is_basis_current", "live_members",
           "work_digest"]
