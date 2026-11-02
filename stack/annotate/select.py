"""``select`` (§3.1, D32): the ``<wf>`` rows from the ``corpus`` rows in scope, the evidence and
the ``<wf>`` overrides (target ``<wf>`` of the ``overrides`` family, :mod:`stack.overrides`). Pure (no I/O, no calls), so it always runs (``--only select``
included) and X8 reruns it to check a ``<wf>`` file.

Per document, in ``doc_id`` order (v3's ``annotations_canon``, ``db/0002_views.sql:38-43``,
ranked by origin, version, recency; here the work key does what version did):

1. **evidence**: the row's current basis gives the work key
   (:func:`stack.annotate.basis.work_digest`); of the evidence rows with that
   ``work_digest``, :func:`stack.annotate.evidence.selected` picks one (the newest decided
   ``ok``/``empty`` row, else the newest row; chain position through ``supersedes``, then
   ``decided_at``, then ``evidence_id``). Evidence of another
   basis, config or supplying member is never selected (it stays in the evidence file as
   history), except an **accepted** row (D758, :mod:`stack.plan` "Accepted configs"): when the current work key has
   no decided row, the first config of ``accepted`` (the line's accepted configs, in preference order) with a
   decided row for the same source and basis (:func:`stack.annotate.evidence.accepted_row`) is selected, its
   provenance kept (``evidence_id``, ``config_digest``, ``decided_at``, ``run_id``: the row stays labelled with the
   config that judged it); a failure is never accepted, and the current key's own failure or retry state gives
   way to an accepted decided row. No basis → ``skipped[no_text|no_basis]``; a
   basis but no evidence → the status
   the ask substage recorded for the row (``unreached``: ``not_attempted[limit|budget|halted|unanswered]``),
   default ``not_attempted[limit]``.
2. **overrides** (the run's pinned ``overrides`` rows of target ``<wf>``, revokes applied,
   keyed ``(doc_id, item_key)``): a row with no ``scope_members`` makes the document
   ``skipped[review_required]``; a row applies when ``scope_members`` equals the document's
   live members and ``scope_basis_digest`` is unset or equals the current basis, otherwise its
   id goes to ``override_stale_ids`` and it is not applied. Of the applicable rows, one wins per
   ``(item_key, field)`` and per quarantine (:func:`stack.overrides.winners`: the last by tier,
   ``ai_check`` before ``human``, then ``created_at``, then ``override_id``; D337). The winning
   ``row`` rows apply first (a ``row`` replaces the payload; item grain: the item ``item_key``),
   then the winning ``field`` rows compose over them (one payload field; item grain: of item
   ``item_key``); a ``field`` row with no ``ok``/``empty`` result under it, or a payload the
   frozen type refuses, makes the document ``skipped[review_required]``. A used ``row``/``field``
   row makes the document ``ok`` with ``origin`` = the highest tier used; a winning
   ``quarantine`` makes it ``quarantined[operator]`` (payload kept), whatever else applies. Used
   ids go to ``override_ids``. When the used rows supply the whole payload (document grain: a
   ``row`` row is among them; item grain: no item of the evidence's payload is left), the
   overrides are the decision and the row takes their provenance (D717): ``decided_at`` = the
   newest ``created_at`` of the used rows, ``config_digest`` = the fingerprint of the used rows
   (:func:`stack.overrides.rows_digest`: no config produced the payload, and the column is a
   non-null digest, A.4 and S6), no ``evidence_id``, ``run_id`` and T2 as a row without
   evidence. Field rows over the evidence's payload keep the evidence's provenance. The author
   and note stay on the override rows (``override_ids``); the ANN common fields have no place
   for them.
3. **rows**: document grain, one row; item grain, one row per payload item (``item_key`` from
   the item) or, with no items or no payload, one row with ``item_key = ''`` carrying the
   document's status (``fold.build`` reads it so, D99). The ANN common fields, SEL ``source_id``
   and T2 are per document (``fold.build`` requires the item rows of a document to agree); T2,
   ``decided_at``, ``basis_digest``, ``config_digest`` and ``run_id`` come from the selected
   evidence (an override-supplied payload: step 2), so a rerun that selects the same prior
   evidence writes the same rows. A row without evidence takes ``now`` and this run's
   ``run_id``, ``attempts = 0``.
   ``is_basis_current`` is :func:`stack.annotate.basis.is_basis_current` (D99; a ``chained``
   basis reads the rows as the stage joined them with its reads, :mod:`stack.annotate.inputs`);
   ``result_digest`` = :func:`stack.annotate.evidence.result_digest` of the row's payload
   (item grain: of its item).

**Records** (registry ``produced_by``): ``spec`` may be an annotation record; ``evidence`` is
then its producer's (``<producer>_evidence``) and ``config_digest`` the producer's, so the work
key and the selected evidence row are the producer's, and the record's payload is that row's
``<record>`` struct column (:func:`stack.annotate.evidence.record_payload`). Status, error class,
T2, digests, ``evidence_id``, ``decided_at`` and ``origin`` come from the selected row exactly as
the producer's row gets them; ``overrides`` are the record's own (target ``<record>``). A decided
(``ok``/``empty``) row whose record column is NULL is a code bug of the producer: :class:`RecordMissing`
(call ``select`` for a record only when the producer's pin produces it).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from stack.annotate.basis import basis_of, is_basis_current, live_members, work_digest
from stack.annotate.evidence import OK_LIKE, accepted_row, by_work, record_payload, result_digest
from stack.overrides import TIERS, Override, rows_digest, winners
from stack.schemas.payloads.registry import PayloadSpec

Row = dict[str, Any]


class RecordMissing(ValueError):
    """A decided evidence row without the payload of a record its producer's pin produces."""


def _applies(o: Override, live: list[str], cur: str | None) -> bool:
    assert o.scope_members
    return sorted(set(o.scope_members)) == live and (o.scope_basis_digest is None or o.scope_basis_digest == cur)


def _items_of(payload: Mapping[str, Any] | None, spec: PayloadSpec) -> list[dict[str, Any]]:
    if payload is None:
        return []
    (items_field,) = list(spec.payload_schema)
    return [dict(i) for i in payload.get(items_field.name) or []]


def _valid(payload: Mapping[str, Any], spec: PayloadSpec) -> str | None:
    try:
        pa.array([dict(payload)], type=spec.payload_schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError, KeyError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _apply(spec: PayloadSpec, base: Mapping[str, Any] | None, used: Sequence[Override]
           ) -> tuple[dict[str, Any] | None, str | None]:
    """The payload after ``used`` (row/field rows, in order) on ``base``; ``(None, why)`` when
    a row cannot apply or the result does not fit the frozen type."""
    names = [f.name for f in spec.payload_schema]
    if spec.grain == "document":
        pay = None if base is None else dict(base)
        for o in used:
            if o.item_key != "":
                return None, f"{o.override_id}: item_key {o.item_key!r} on a document-grain workflow"
            if o.kind == "row":
                bad = sorted(set(o.value) - set(names))
                if bad:
                    return None, f"{o.override_id}: unknown payload fields {bad}"
                pay = {n: None for n in names} | dict(o.value)
            else:
                if pay is None:
                    return None, f"{o.override_id}: field override without an ok/empty result"
                if o.field not in names:
                    return None, f"{o.override_id}: unknown payload field {o.field!r}"
                pay[o.field] = o.value
    else:
        (items_field,) = list(spec.payload_schema)
        item_names = [f.name for f in items_field.type.value_type]
        items = {i["item_key"]: i for i in _items_of(base, spec)}
        for o in used:
            if o.item_key == "":
                return None, f"{o.override_id}: an item-grain override names an item_key"
            if o.kind == "row":
                bad = sorted(set(o.value) - set(item_names))
                if bad:
                    return None, f"{o.override_id}: unknown item fields {bad}"
                items[o.item_key] = {n: None for n in item_names} | dict(o.value) | {"item_key": o.item_key}
            else:
                if base is None or o.item_key not in items:
                    return None, f"{o.override_id}: field override without an item {o.item_key!r}"
                if o.field not in item_names or o.field == "item_key":
                    return None, f"{o.override_id}: unknown item field {o.field!r}"
                items[o.item_key][o.field] = o.value
        pay = {items_field.name: [items[k] for k in sorted(items)]}
    if pay is not None:
        why = _valid(pay, spec)
        if why is not None:
            return None, f"override payload refused by the frozen type: {why}"
    return pay, None


def _wholly_supplied(spec: PayloadSpec, base: Mapping[str, Any] | None, edits: Sequence[Override]) -> bool:
    """Whether ``edits`` (applied cleanly over ``base``) leave nothing of the evidence's payload: at document
    grain a used ``row`` override, at item grain no item of ``base`` (every item an override's) (D717)."""
    if spec.grain == "document":
        return any(o.kind == "row" for o in edits)
    return not _items_of(base, spec)


def select(spec: PayloadSpec, docs: Sequence[Mapping[str, Any]],
           evidence: Sequence[Mapping[str, Any]], overrides: Sequence[Override], *,
           config_digest: str, run_id: str, now: dt.datetime,
           unreached: Mapping[str, tuple[str, str]] | None = None,
           accepted: Sequence[str] = ()) -> list[Row]:
    """The ``<wf>`` rows of ``docs`` (the ``corpus`` rows in scope), sorted by ``(doc_id,
    item_key)`` (module docstring). ``overrides`` are the ``overrides`` rows of target
    ``spec.workflow`` with the revokes applied (:func:`stack.overrides.active`); ``accepted`` the
    line's accepted configs in preference order (empty: none)."""
    unreached = unreached or {}
    latest = by_work(evidence)
    mine_by_doc: dict[str, list[Override]] = {}
    for o in overrides:
        if o.target != spec.workflow or o.kind == "revoke":
            raise ValueError(f"select {spec.workflow}: override {o.override_id} is a {o.target} {o.kind} row")
        mine_by_doc.setdefault(o.key, []).append(o)
    out: list[Row] = []
    for d in sorted(docs, key=lambda r: r["doc_id"]):
        doc = d["doc_id"]
        b = basis_of(d, spec.basis_kind, spec.reads)
        sel = (latest.get(work_digest(b.source_id, b.digest, config_digest, ""))
               if b.digest is not None else None)
        if accepted and b.digest is not None and (sel is None or sel["status"] not in OK_LIKE):
            sel = accepted_row(latest, b.source_id, b.digest, accepted) or sel
        mine = mine_by_doc.get(doc, [])
        unscoped = sorted(o.override_id for o in mine if not o.is_scoped)
        scoped = [o for o in mine if o.is_scoped]
        live = live_members(d)
        applicable = [o for o in scoped if _applies(o, live, b.digest)]
        stale = sorted(o.override_id for o in scoped if o not in applicable)

        payload = record_payload(sel, spec) if sel is not None and sel["status"] in OK_LIKE else None
        if payload is None and sel is not None and sel["status"] in OK_LIKE:
            raise RecordMissing(f"{spec.workflow}: {sel['status']} {spec.evidence_family} row "
                                f"{sel['evidence_id']} (doc {doc}) has no {spec.workflow} payload")
        if sel is not None:
            status, error_class, error, origin = (sel["status"], sel["error_class"], sel["error"],
                                                  sel["origin"])
        elif b.digest is None:
            status, error_class, error, origin = "skipped", b.missing, None, "computed"
        else:
            status, error_class = unreached.get(doc, ("not_attempted", "limit"))
            error, origin = None, "computed"
        used: list[str] = []
        supplied: list[Override] = []  # the used edits when they supply the whole payload (D717)
        if unscoped:
            status, error_class, payload = "skipped", "review_required", None
            error = f"override without scope.members: {unscoped}"
        else:
            chosen = winners(applicable)
            edits = [o for o in chosen if o.kind == "row"] + [o for o in chosen if o.kind == "field"]
            if edits:
                new, why = _apply(spec, payload, edits)
                if why is not None:
                    status, error_class, error, payload = "skipped", "review_required", why, None
                else:
                    if _wholly_supplied(spec, payload, edits):
                        supplied = edits
                    payload, status, error_class, error = new, "ok", None, None
                    origin = max((o.tier for o in edits), key=TIERS.__getitem__)
                used = [o.override_id for o in edits]
            quarantine = [o for o in chosen if o.kind == "quarantine"]
            if quarantine and status != "skipped":
                status, error_class = "quarantined", "operator"
                error = f"quarantined by {[o.override_id for o in quarantine]}"
                used += [o.override_id for o in quarantine]
                origin = "human" if any(o.tier == "human" for o in quarantine) else origin

        basis_digest = sel["basis_digest"] if sel is not None else b.digest
        # The evidence's provenance, unless the overrides supplied the whole payload (D717)
        ev = sel if not supplied else None
        common: Row = {
            "doc_id": doc, "evidence_id": ev["evidence_id"] if ev is not None else None,
            "source_id": sel["source_id"] if sel is not None else b.source_id,
            "override_ids": used, "override_stale_ids": stale,
            "workflow": spec.workflow, "version": spec.version, "status": status,
            "error_class": error_class, "basis_kind": spec.basis_kind, "basis_digest": basis_digest,
            "config_digest": (ev["config_digest"] if ev is not None
                              else rows_digest(supplied) if supplied else config_digest),
            "decided_at": (ev["decided_at"] if ev is not None
                           else max(o.created_at for o in supplied) if supplied else now),
            "origin": origin,
            "is_basis_current": is_basis_current(spec.basis_kind, basis_digest, d, spec.workflow),
            "run_id": ev["run_id"] if ev is not None else run_id, "error": error,
            "attempts": ev["attempts"] if ev is not None else 0,
            "first_attempt_at": ev["first_attempt_at"] if ev is not None else None,
            "last_attempt_at": ev["last_attempt_at"] if ev is not None else None,
            "retry_after": ev["retry_after"] if ev is not None else None,
        }
        if spec.grain == "document":
            names = [f.name for f in spec.payload_schema]
            pay = payload if payload is not None else {n: None for n in names}
            out.append({**common, "item_key": "", **pay,
                        "result_digest": result_digest(payload)})
        else:
            (items_field,) = list(spec.payload_schema)
            item_names = [f.name for f in items_field.type.value_type]
            items = _items_of(payload, spec)
            if not items:
                out.append({**common, **{n: None for n in item_names}, "item_key": "",
                            "result_digest": result_digest(payload)})
            for it in items:
                out.append({**common, **{n: it.get(n) for n in item_names},
                            "item_key": it["item_key"], "result_digest": result_digest(it)})
    return sorted(out, key=lambda r: (r["doc_id"], r["item_key"]))


__all__ = ["RecordMissing", "select"]
