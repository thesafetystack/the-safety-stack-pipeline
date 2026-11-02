"""Substage ``inband`` (pure; §5.4, C20, D8, D10): every ``inband.entries[]`` item becomes an
``inband`` snapshot, whatever the gate said, so edit history survives ``sources`` compaction.

The snapshot: ``kind = via = 'inband'``, the entry's ``content_role`` and ``captured_at`` (also
``as_of``); ``raw`` = the entry's ``raw`` BLOB if present else its ``text`` BLOB (D8; sizes and media
types as the collector recorded them, D690); ``text`` = the entry's ``text`` with ``text_status =
'ok'`` when set (the collector's text; ``convert_pin`` NULL), else NULL (a raw-only body waits for
``convert``, D10). An entry is minted once per ``(source_id, content_role, sha256)``. A record entry
(:data:`~stack.schemas.vocab.RECORD_ROLES`, ``provider_record``, D522) is not content: it is never minted.

Its role ``(source_id, content_role)`` is ``ok`` on lane ``inband`` pointing at the newest in-band
snapshot of the key; a fetch role of the same key that is not ``ok`` becomes that in-band role; an
``ok`` fetch role keeps its own snapshot. No blob is read (the entry records its sizes, D690); a
blob missing from the store is X5's finding.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from stack.schemas.vocab import RECORD_ROLES
from workflows.acquire.carry import new_role, role_key
from workflows.acquire.lanes import INBAND, LaneEnv
from workflows.acquire.scope import inband_key, minted_inband

Row = dict[str, Any]


def mint_inband(row: Row, content: Row, env: LaneEnv, next_id: Callable[[], str]) -> int:
    """Mint ``row``'s unminted in-band entries into ``content`` (in place); returns how many."""
    minted = minted_inband(content)
    members = set(row["identity"]["member_ids"])
    n = 0
    for e in row["inband"]["entries"] or []:
        if e["content_role"] in RECORD_ROLES:
            continue  # D522: the provider's record, not content
        key = inband_key(e)
        if key in minted or e["source_id"] not in members:
            continue
        snap = {"snapshot_id": next_id(), "source_id": e["source_id"], "kind": "inband",
                "content_role": e["content_role"], "via": "inband",
                "url": None, "final_url": None, "http_status": None, "etag": None, "last_modified": None,
                "captured_at": e["captured_at"], "as_of": e["captured_at"], "revision": None,
                "raw": dict(e["raw"] or e["text"]), "text": dict(e["text"]) if e["text"] else None,
                "text_status": "ok" if e["text"] else None, "text_error_class": None,
                "convert_attempts": 0, "convert_pin": None, "quality": None}
        content["snapshots"].append(snap)
        minted.add(key)
        n += 1
        _point_role(content, e["source_id"], e["content_role"])
    if n:
        env.count("inband_minted", n)
    return n


def _point_role(content: Row, source_id: str, content_role: str) -> None:
    newest = max((s for s in content["snapshots"] if s["kind"] == "inband" and s["source_id"] == source_id
                  and s["content_role"] == content_role), key=lambda s: (s["captured_at"], s["snapshot_id"]))
    for i, r in enumerate(content["roles"]):
        if role_key(r) != (source_id, content_role):
            continue
        if r["status"] == "ok" and r.get("lane") != INBAND:
            return  # a fetched body holds the role; the in-band snapshot is a candidate anyway
        content["roles"][i] = {**r, "status": "ok", "error_class": None, "error": None, "lane": INBAND, "rule": None,
                               "snapshot_id": newest["snapshot_id"]}
        return
    role = new_role(content_role, source_id, status="ok", error_class=None, lane=INBAND)
    role.update(snapshot_id=newest["snapshot_id"], last_outcome="inband")
    content["roles"].append(role)


__all__ = ["mint_inband"]
