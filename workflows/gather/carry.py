"""The ``content`` and ``annotations`` blocks of a gathered row (§2.4, §6; D6, D7).

``gather`` owns neither block; it carries them. A row keeps its head row's ``content`` and
``annotations`` (a new row: ``content`` NULL, D7's empty ``annotations``) except where members
moved between documents in this run, then ``content`` is **carried** (D6):

* **merge**: a head row retired in this run with ``merged_into = D`` gives ``D`` *all* its
  ``snapshots[]`` and ``roles[]`` (the union D6 names);
* **split / move**: a head row that is live in the head and loses members to ``D`` without
  merging into it (a split tombstone, or a document that continues with fewer members) gives
  ``D`` the entries whose ``source_id`` is one of the moved members ("each part gets its
  members' role entries", §6). The losing document keeps its own block unchanged: the pointer
  rule never considers split-off members (§5.5) and ``acquire`` regroups (X21).

The union is keyed — snapshots by ``snapshot_id``, roles by ``(source_id, content_role)`` — with
the receiving row's own entries first, then the donors' by ``doc_id``; a key already present is
not added again, so attempts are never summed. Every other field stays as the base block has it
(the receiving row's own block, else the first donor's): the pointer fields, ``lane``,
``convert_pin``, ``revisions`` and ``content.members_digest`` are ``acquire``'s to recompute
when it regroups (the row is ``identified``: its ``members_digest`` changed). If the base's
``current_snapshot_id`` is not among the carried snapshots (a split part that got a subset), the
pointer fields are cleared to ``current_text_status = 'no_raw'`` so that V6's "pointers resolve"
holds until ``acquire`` re-points. ``same_raw_doc_ids`` = the union, minus the receiving id and
the ids merged into it. A carried block is stamped by this run.

``annotations`` always come from the receiving row's own head row; a retired row's triage
decision is not carried (it was decided on another document's fields).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from stack.schemas.families import family

Row = dict[str, Any]

#: The pointer fields cleared when the current snapshot did not come along (V6 rule 1).
POINTER_FIELDS = ("current_snapshot_id", "current_source_id", "current_role", "current_text_sha256",
                  "current_text_media", "current_text_quality", "current_revision")


def empty_annotations() -> Row:
    """D7: typed fields NULL, ``other = []``."""
    ann = family("documents").schema.field("annotations").type
    return {f.name: ([] if f.name == "other" else None) for f in ann}


def _members(row: Row) -> list[str]:
    return [m["source_id"] for m in (row["identity"] or {}).get("members") or []]


def donors(docs: Mapping[str, Sequence[str]], head: Mapping[str, Row],
           merged_into: Mapping[str, str | None]) -> dict[str, list[tuple[str, frozenset[str] | None]]]:
    """Per live ``doc_id`` the head rows that give it content: ``(donor doc_id, sources)``, with
    ``sources`` ``None`` for a merge (everything) or the moved members (split / move). Only head
    rows live in the head give: a row already retired gave when it retired."""
    owner = {s: d for d, ms in docs.items() for s in ms}
    out: dict[str, list[tuple[str, frozenset[str] | None]]] = {}
    for f, row in sorted(head.items()):
        if row["state"] == "retired" or row["content"] is None:
            continue
        if f not in docs and (target := merged_into[f]) is not None:  # merged into `target` this run
            out.setdefault(target, []).append((f, None))
            continue
        moved: dict[str, set[str]] = {}
        for s in _members(row):
            d = owner.get(s)
            if d is not None and d != f:
                moved.setdefault(d, set()).add(s)
        for d, ss in sorted(moved.items()):
            out.setdefault(d, []).append((f, frozenset(ss)))
    return out


def _take(block: Row, sources: frozenset[str] | None) -> tuple[list[Row], list[Row]]:
    snaps = [s for s in block["snapshots"] or [] if sources is None or s["source_id"] in sources]
    roles = [r for r in block["roles"] or [] if sources is None or r["source_id"] in sources]
    return snaps, roles


def carried_content(doc_id: str, own: Row | None, gives: Sequence[tuple[str, frozenset[str] | None]],
                    head: Mapping[str, Row], absorbed: Sequence[str]) -> Row | None:
    """The receiving row's ``content`` without its stamps, or ``None`` when nothing is carried
    (the caller keeps ``own`` as is). ``absorbed``: ids merged into ``doc_id`` this run."""
    parts = [(f, *_take(head[f]["content"], ss)) for f, ss in gives]
    if not any(snaps or roles for _f, snaps, roles in parts):
        return None
    base = own if own is not None else head[gives[0][0]]["content"]
    snaps: dict[str, Row] = {s["snapshot_id"]: s for s in (own or {}).get("snapshots") or []}
    roles: dict[tuple[str | None, str], Row] = {
        (r["source_id"], r["content_role"]): r for r in (own or {}).get("roles") or []}
    same: list[str] = list((own or {}).get("same_raw_doc_ids") or [])
    for f, ss, rs in parts:
        for s in ss:
            snaps.setdefault(s["snapshot_id"], s)
        for r in rs:
            roles.setdefault((r["source_id"], r["content_role"]), r)
        same.extend(head[f]["content"].get("same_raw_doc_ids") or [])
    drop = {doc_id, *absorbed}
    out = {k: v for k, v in base.items() if k not in ("run_id", "updated_at")}
    out.update(snapshots=list(snaps.values()), roles=list(roles.values()),
               same_raw_doc_ids=sorted(set(same) - drop))
    if out["current_snapshot_id"] is not None and out["current_snapshot_id"] not in snaps:
        out.update(dict.fromkeys(POINTER_FIELDS), current_text_status="no_raw")
    return out


__all__ = ["POINTER_FIELDS", "carried_content", "donors", "empty_annotations"]
