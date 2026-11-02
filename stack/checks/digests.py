"""The row digests of ``documents`` (§2.7, D19): ``fields_digest`` (triage's basis) and
``members_digest`` (acquisition's basis). V4 recomputes them; ``gather`` (M2) writes them with the
same functions, so the writer and the check cannot drift.

Canonical rendering (proposed decision, T1.4): ``H(x) = sha256(jcs(x))`` hex, with
:func:`stack.core.hashes.jcs` (RFC 8785-style canonical JSON) and timestamps rendered as UTC
``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (as D50's ``ids_digest``).

* ``fields_digest = H([title, abstract, [authors[].name ...], published_at, doc_type,
  inband_excerpt_sha256])`` — a JSON array in that order; NULLs render ``null``; authors in their
  list order (author order is part of the record); a NULL ``authors`` list renders ``[]``.
* ``inband_excerpt_sha256`` (D19, named D690) = the ``text.sha256`` of the anchor's highest-ranked in-band
  entry: entries with ``source_id = identity.anchor_source_id``, a ``content_role`` in the pointer
  rank (§2.6; ``xml``/``archive`` never) and a ``text``; best rank first, ties by
  newest ``captured_at``, then smallest ``text.sha256``; NULL if none.
* ``members_digest = H(sorted(identity.members[].source_id))`` — every member, live or not (C18).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from stack.core.hashes import jcs
from stack.schemas.vocab import POINTER_RANK

_RANK = {role: i for i, role in enumerate(POINTER_RANK)}


def H(value: object) -> str:
    """sha256 hex of the canonical JSON of ``value``."""
    return hashlib.sha256(jcs(value).encode("utf-8")).hexdigest()


def render_ts(v: dt.datetime | None) -> str | None:
    """A timestamp as UTC ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (``None`` stays ``None``)."""
    if v is None:
        return None
    if v.tzinfo is None:
        raise ValueError(f"digest: naive timestamp {v!r}")
    return v.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def inband_excerpt_sha256(anchor_source_id: str | None,
                          entries: Iterable[Mapping[str, Any]] | None) -> str | None:
    """D19: ``text.sha256`` of the anchor's highest-ranked in-band entry (module docstring)."""
    best: tuple[int, float, str] | None = None
    for e in entries or ():
        role, text = e["content_role"], (e["text"] or {}).get("sha256")
        if e.get("source_id") != anchor_source_id or role not in _RANK or text is None:
            continue
        at = e.get("captured_at")
        key = (_RANK[role], -at.timestamp() if at is not None else 0.0, text)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def fields_digest(*, title: str | None, abstract: str | None, author_names: Sequence[str | None],
                  published_at: dt.datetime | None, doc_type: str | None,
                  inband_excerpt_sha256: str | None) -> str:
    """§2.7's ``fields_digest`` over the merged fields (module docstring)."""
    return H([title, abstract, list(author_names), render_ts(published_at), doc_type,
              inband_excerpt_sha256])


def members_digest(member_ids: Iterable[str]) -> str:
    """§2.7's ``members_digest``: ``H(sorted member source ids)``."""
    return H(sorted(member_ids))


def row_fields_digest(row: Mapping[str, Any]) -> str:
    """``fields_digest`` of a ``documents`` row (as ``Table.to_pylist`` yields it)."""
    md, ident = row["metadata"] or {}, row["identity"] or {}
    return fields_digest(
        title=md.get("title"), abstract=md.get("abstract"),
        author_names=[a.get("name") for a in md.get("authors") or []],
        published_at=md.get("published_at"), doc_type=md.get("doc_type"),
        inband_excerpt_sha256=inband_excerpt_sha256(
            ident.get("anchor_source_id"), (row["inband"] or {}).get("entries")))


def row_members_digest(row: Mapping[str, Any]) -> str:
    """``members_digest`` of a ``documents`` row: over ``identity.members[].source_id``."""
    return members_digest(m["source_id"] for m in (row["identity"] or {}).get("members") or [])


__all__ = ["H", "fields_digest", "inband_excerpt_sha256", "members_digest", "render_ts",
           "row_fields_digest", "row_members_digest"]
