"""V1-V4: per family file checks (Appendix B; D20: per family file, cross-row allowed).

Each is ``(table, header, spec) -> list[Finding]`` like the S-checks; :func:`run_checks`
(:mod:`stack.checks.verify`) runs them over every ``data/`` file that passed S1.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from stack.checks import Finding, register
from stack.checks.digests import row_fields_digest, row_members_digest
from stack.checks.rows import iter_rows
from stack.schemas.families import FamilySpec, ids_digest
from stack.schemas.vocab import (
    DOCUMENT_STATES,
    INBAND_ROLES,
    MASK_REASONS,
    RECORD_ROLES,
    SOURCE_ORIGINS,
    SOURCE_STATES,
)

Header = dict[str, str]
_EXAMPLES = 5
_ROW_FAMILIES = ("documents", "corpus")


def _finding(check: str, message: str, bad: list[str]) -> list[Finding]:
    return [Finding(check, "hard", message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


# ---------------------------------------------------------------- V1, V2
#: ``sources.record_format`` of a miss row (§5.2, D13); a ``missing`` row is always one.
MISS_FORMAT = "miss"


@register("V1", "V-sources")
def v_sources(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V1 (``sources``): a miss (``record_format = 'miss'``, which every ``missing`` row is) has
    ``state`` ``missing`` or ``retracted`` and ``status = 'empty'`` (D13: ``retracted`` = a miss past
    ``miss_cap``); every row's ``metadata.origin`` is in ``SOURCE_ORIGINS``; every in-band entry's role
    is in ``INBAND_ROLES``, and a ``provider_record`` entry (D522, D524) has a ``raw`` and no
    ``text`` (a miss carries its source's last record, as it carries its content entries)."""
    if spec.kind != "sources":
        return []
    bad: list[str] = []
    for r in iter_rows(table, ["source_id", "state", "record_format", "status", "metadata", "inband"]):
        s, md = r["source_id"], r["metadata"]
        for e in (r["inband"] or {}).get("entries") or []:
            if e["content_role"] not in INBAND_ROLES:
                bad.append(f"{s}: in-band role {e['content_role']!r} not in {INBAND_ROLES}")
            elif e["content_role"] in RECORD_ROLES and (e["raw"] is None or e["text"] is not None):
                bad.append(f"{s}: {e['content_role']} entry without a raw blob or with a text")
        if r["state"] == "missing" and r["record_format"] != MISS_FORMAT:
            bad.append(f"{s}: missing with record_format {r['record_format']!r}")
        if r["record_format"] == MISS_FORMAT:
            if r["state"] not in ("missing", "retracted"):
                bad.append(f"{s}: a miss with state {r['state']!r}")
            if r["status"] != "empty":
                bad.append(f"{s}: a miss with status {r['status']!r}")
        if md["origin"] not in SOURCE_ORIGINS:
            bad.append(f"{s}: metadata.origin {md['origin']!r} not in {SOURCE_ORIGINS}")
    return _finding("V-sources", f"{spec.name}: {{n}} row(s) break the miss rules", bad)


@register("V2", "V-coverage")
def v_coverage(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V2 (``coverage``): an exhausted window has no ``next_cursor``."""
    if spec.kind != "coverage":
        return []
    bad = [f"{r['channel']}/{r['query_digest'][:12]}: exhausted with next_cursor {r['next_cursor']!r}"
           for r in iter_rows(table, ["channel", "query_digest", "is_exhausted", "next_cursor"])
           if r["is_exhausted"] and r["next_cursor"] is not None]
    return _finding("V-coverage", f"{spec.name}: {{n}} exhausted window(s) with a cursor", bad)


# ---------------------------------------------------------------- V3
@register("V3", "V-state")
def v_state(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V3: the state column's vocabulary (``documents``/``corpus``: §2.5's four, ``sources``:
    D13's three); ``identity.merged_into`` only on a ``retired`` row; a ``retired`` row has
    ``merged_into`` (a merge) or is a split (its ``doc_id`` is in another row's
    ``former_doc_ids`` in the same file, since one ``gather`` writes the tombstone and the
    survivors); masking is orthogonal to state: ``is_masked`` ⇔ ``mask_reason`` set, from
    ``{operator, review_required}``. Required blocks per state are S8's."""
    if spec.kind == "sources":
        wrong = [f"{r['source_id']}: state {r['state']!r}"
                 for r in iter_rows(table, ["source_id", "state"])
                 if r["state"] not in SOURCE_STATES]
        return _finding("V-state", f"{spec.name}: {{n}} row(s) with a state outside {SOURCE_STATES}",
                        wrong)
    if spec.kind not in _ROW_FAMILIES:
        return []
    # the one cross-row input, the file's former_doc_ids (a NULL list contributes nothing)
    formers = set(pc.list_flatten(pc.struct_field(table.column("identity"), "former_doc_ids")).to_pylist())
    bad: list[str] = []
    for r in iter_rows(table, ["doc_id", "state", "identity"]):
        d, st, ident = r["doc_id"], r["state"], r["identity"]
        if st not in DOCUMENT_STATES:
            bad.append(f"{d}: state {st!r} not in {DOCUMENT_STATES}")
            continue
        merged = ident["merged_into"]
        if merged is not None and st != "retired":
            bad.append(f"{d}: merged_into {merged} on a {st} row")
        if st == "retired" and merged is None and d not in formers:
            bad.append(f"{d}: retired with no merged_into and not a split "
                       "(no row lists it in former_doc_ids)")
        if merged is not None and merged == d:
            bad.append(f"{d}: merged into itself")
        masked, reason = ident["is_masked"], ident["mask_reason"]
        if masked and reason not in MASK_REASONS:
            bad.append(f"{d}: masked with mask_reason {reason!r}")
        if not masked and reason is not None:
            bad.append(f"{d}: mask_reason {reason!r} on an unmasked row")
    return _finding("V-state", f"{spec.name}: {{n}} row(s) break §2.5's state rules", bad)


# ---------------------------------------------------------------- V4
@register("V4", "V-digests")
def v_digests(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V4: the digests a file states recompute: ``stack.ids_digest`` over the key columns (every
    family); on ``documents``/``corpus`` rows ``metadata.fields_digest`` and
    ``identity.members_digest`` (:mod:`stack.checks.digests`, §2.7, D19)."""
    bad: list[str] = []
    if header.get("ids_digest") != ids_digest(table, spec.key):
        bad.append(f"{header.get('file_id')}: stack.ids_digest does not recompute")
    if spec.kind in _ROW_FAMILIES:
        for r in iter_rows(table, ["doc_id", "identity", "metadata", "inband"]):
            if r["metadata"]["fields_digest"] != row_fields_digest(r):
                bad.append(f"{r['doc_id']}: stale metadata.fields_digest")
            if r["identity"]["members_digest"] != row_members_digest(r):
                bad.append(f"{r['doc_id']}: stale identity.members_digest")
    return _finding("V-digests", f"{spec.name}: {{n}} digest(s) do not recompute", bad)
