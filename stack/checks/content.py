"""V6 ``V-content`` (Appendix B; D10): the ``content`` block of every ``documents``/``corpus`` row
is internally consistent. Per family file, ``(table, header, spec) -> list[Finding]`` like the
other V checks (:mod:`stack.checks.family`); ``run_checks`` runs it over every ``data/`` file
that passed S1.

On every row with a non-NULL ``content``:

1. **pointers resolve**: ``current_snapshot_id`` NULL ⇔ ``current_text_status = 'no_raw'``; a set
   ``current_snapshot_id`` names a ``raw``/``inband`` snapshot of ``snapshots[]`` whose
   ``source_id``/``content_role`` are ``current_source_id``/``current_role``;
2. ``current_text_status = 'ok'`` ⇔ ``current_text_sha256`` set, and an ``ok`` reference is the
   current snapshot's ``text.sha256``; ``current_text_media`` is the pointer's for that snapshot
   (:func:`workflows.acquire.point.media_of`: ``txt`` for an in-band ``text/plain`` body);
3. **no wayback over a live candidate**: a current snapshot with ``via = 'wayback'`` has no
   non-wayback candidate (``raw``/``inband``, a live member's) of the same role;
4. roles unique per ``(source_id, content_role)``;
5. **in-band converted same run** (D10, ``acquired`` rows only): an in-band entry of a live member
   in a pointer-rank role with a ``text`` never leaves the pointer NULL
   (``convert_pending`` on a raw-only in-band entry is legal);
6. **one size per blob** (D690): a snapshot whose ``text`` is its raw bytes (equal ``sha256``) records
   the raw's ``bytes`` for it.
"""

from __future__ import annotations

from collections import Counter

import pyarrow as pa

from stack.checks import Finding, register
from stack.checks.rows import iter_rows
from stack.schemas.families import FamilySpec
from stack.schemas.vocab import POINTER_RANK
from workflows.acquire.point import media_of

Header = dict[str, str]
_EXAMPLES = 5
_ROW_FAMILIES = ("documents", "corpus")


def _content_problems(row: dict) -> list[str]:
    d, c = row["doc_id"], row["content"]
    out: list[str] = []
    snaps = {s["snapshot_id"]: s for s in c["snapshots"] or []}
    cur_id, status, digest = c["current_snapshot_id"], c["current_text_status"], c["current_text_sha256"]
    cur = snaps.get(cur_id) if cur_id is not None else None
    if (cur_id is None) != (status == "no_raw"):
        out.append(f"{d}: current_snapshot_id {cur_id!r} with current_text_status {status!r}")
    if cur_id is not None and (cur is None or cur["kind"] not in ("raw", "inband")):
        out.append(f"{d}: current_snapshot_id {cur_id} is not a raw/inband snapshot of the row")
    if cur is not None and (cur["source_id"], cur["content_role"]) != (c["current_source_id"], c["current_role"]):
        out.append(f"{d}: current_source_id/current_role disagree with snapshot {cur_id}")
    if (status == "ok") != (digest is not None):
        out.append(f"{d}: current_text_status {status!r} with current_text_sha256 {digest!r}")
    cur_text = (cur or {}).get("text") or {}
    if status == "ok" and cur is not None and digest != cur_text.get("sha256"):
        out.append(f"{d}: current_text_sha256 is not snapshot {cur_id}'s text.sha256")
    if status == "ok" and cur is not None and c["current_text_media"] != media_of(cur):
        out.append(f"{d}: current_text_media {c['current_text_media']!r} but snapshot {cur_id} is "
                   f"{media_of(cur)!r}")
    for s in snaps.values():
        t = s["text"]
        if t is not None and t["sha256"] == s["raw"]["sha256"] and t["bytes"] != s["raw"]["bytes"]:
            out.append(f"{d}: snapshot {s['snapshot_id']}'s text is its raw bytes but records {t['bytes']} bytes, "
                       f"the raw {s['raw']['bytes']}")
    live = {m["source_id"] for m in row["identity"]["members"] if m["is_live"]}
    if cur is not None and cur["via"] == "wayback":
        rivals = [s["snapshot_id"] for s in snaps.values()
                  if s["kind"] in ("raw", "inband") and s["via"] != "wayback" and s["source_id"] in live
                  and s["content_role"] == cur["content_role"]]
        if rivals:
            out.append(f"{d}: wayback snapshot {cur_id} current over live candidate(s) {rivals[:3]}")
    dup = [k for k, n in Counter((r["source_id"], r["content_role"]) for r in c["roles"] or []).items() if n > 1]
    if dup:
        out.append(f"{d}: roles repeated per (source_id, content_role): {dup[:3]}")
    if row["state"] == "acquired" and cur_id is None:
        texts = [e for e in row["inband"]["entries"] or []
                 if e["text"] is not None and e["source_id"] in live and e["content_role"] in POINTER_RANK]
        if texts:
            out.append(f"{d}: in-band text ({texts[0]['source_id']}/{texts[0]['content_role']}) "
                       "left the pointer NULL")
    return out


@register("V6", "V-content")
def v_content(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V6 (module docstring)."""
    if spec.kind not in _ROW_FAMILIES:
        return []
    bad: list[str] = []
    for row in iter_rows(table, ["doc_id", "state", "identity", "inband", "content"]):
        if row["content"] is not None:
            bad.extend(_content_problems(row))
    if not bad:
        return []
    return [Finding("V-content", "hard", f"{spec.name}: {len(bad)} content inconsistenc(ies)", len(bad),
                    tuple(bad[:_EXAMPLES]))]


__all__ = ["v_content"]
