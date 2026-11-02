"""``acquire``'s scope and due-ness (§3.1 acquire row as amended by D5; D19, D22).

A row is **in scope** when ``state IN ('triaged', 'acquired')`` (so never ``retired``; ``identified``
rows are out of scope, counted ``out_of_scope``) and at least one of:

* ``fetch`` — triage lets ``acquire`` fetch: ``decision = 'fetch'`` with status ``ok``, or status
  ``skipped`` with ``no_basis``/``gate_failed`` (fetched once when ``gate.no_basis_fetches`` is
  true; with it false the row is still in scope, its fetch roles read ``skipped[gated]``);
* ``due`` — a fetch role is due and fetchable (:func:`fetch_items`, what ``fetch`` calls: triage
  lets the row fetch, the row is not masked, the role's member is live, the role has a fetch lane,
  and :func:`is_due`; D19: ``not_attempted``, or ``retry_after <= now``). D698: before, a due role of
  a row triage does not let fetch (a ``failed_transient[rate_limit]`` role kept when a re-triage turned
  ``fetch`` into ``skip``) matched for good: the row was in scope and counted ``scope.due`` on every
  run, and nothing fetched it. A row ``fetch`` matches already, so ``due`` counts rather than widens;
* ``inband`` — an ``inband.entries[]`` content item has no ``inband`` snapshot yet (C20; whatever the
  gate said; a ``provider_record`` entry never counts, D522);
* ``stale`` — ``content`` NULL, or ``content.members_digest IS DISTINCT FROM
  identity.members_digest`` (a membership change to regroup, X21);
* ``repoint`` — ``state = 'triaged'`` with a current ``content`` block (D5: a fields-only change
  re-points and returns the row to ``acquired``);
* ``quality`` — (round 3, Q; only when the run passes its quality gate digest) the current text has
  no verdict of this checks version under this gate
  (:func:`workflows.acquire.quality.needs_verdict`): a text converted before the checks existed,
  an in-band text of a row matching no other clause, or a changed gate; with the run's enabled
  structured roles (``options.structured``) every text the quality substage judges counts, the
  structured ones and their PDF fallback included (B9, B8's note);
* ``convert`` — (D403, review L4-8; only with the run's convert pin) a snapshot ``convert`` would
  convert at the pin has no text yet (:func:`workflows.acquire.convert.awaits_text`: never
  converted, or ``failed_transient``), e.g. an in-band body a convert stop left ``convert_pending``
  on a row triage does not let fetch, which otherwise matches no clause from the next run on. A
  text converted under an older pin does not bring its row into scope (a pin change re-converts
  rows in scope for another clause only). A row in scope for ``convert`` alone is never fetched
  (``permit`` is not ``fetch``, no role is due).

Masked rows stay in scope (their pure substages run, progress is kept) but are never fetched.
Triaged/acquired rows matching no clause are ``idle`` (counted, untouched).

**Filter** (§3.3, the run's ``--filter``): :func:`select`'s ``only`` narrows the scope to those
``doc_id``s before any clause is read; a row it rejects is not in scope (never carried, fetched,
converted or re-pointed by the run) and is counted ``scope.filtered_out`` only.

**Order** (the acquire line's ``options.order``, D391 (e2e-fix)): :func:`select` returns the scope in
``doc_id`` order (``doc_id``, the default) or ``newest_first``: ``metadata.published_at`` descending,
ties by ``doc_id``, rows without a date last (in ``doc_id`` order). Every substage walks the scope in
this order, so ``max_rows`` and ``budget.max_minutes`` stop at the same place in it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from stack.schemas.vocab import RECORD_ROLES
from workflows.acquire.convert import awaits_text
from workflows.acquire.lanes import FETCH_LANES, INBAND
from workflows.acquire.quality import needs_verdict

SCOPE_STATES: tuple[str, ...] = ("triaged", "acquired")
#: ``options.order`` values (module docstring, Order); the first is the default.
ORDERS: tuple[str, ...] = ("doc_id", "newest_first")
NO_BASIS: frozenset[str] = frozenset({"no_basis", "gate_failed"})
#: Values of :func:`permit`: may fetch; would fetch but the plan gates no-basis rows; may not.
FETCH, GATED, SKIP = "fetch", "gated", "skip"


def permit(row: Mapping[str, Any], no_basis_fetches: bool) -> str:
    """What triage lets ``acquire`` do with the row's fetch roles (:data:`FETCH`, :data:`GATED`,
    :data:`SKIP`)."""
    tri = (row.get("annotations") or {}).get("triage")
    if tri is None:
        return SKIP
    if tri["status"] == "ok" and (tri.get("payload") or {}).get("decision") == "fetch":
        return FETCH
    if tri["status"] == "skipped" and tri.get("error_class") in NO_BASIS:
        return FETCH if no_basis_fetches else GATED
    return SKIP


def is_due(role: Mapping[str, Any], now: dt.datetime) -> bool:
    """A fetch role due at ``now`` (D19): ``not_attempted``, or ``retry_after <= now``. In-band
    roles are never due (minted, not fetched)."""
    if role.get("lane") == INBAND:
        return False
    if role["status"] == "not_attempted":
        return True
    ra = role.get("retry_after")
    return ra is not None and ra <= now


def fetch_items(row: Mapping[str, Any], permit_: str, now: dt.datetime) -> list[tuple[int, Mapping[str, Any]]]:
    """``(index into content.roles, live member)`` of the row's fetchable due roles (module
    docstring, ``due``; the fetch substage's work): ``permit_`` is :data:`FETCH`, the row is not
    masked, the role's member is live, the role's lane is a fetch lane and the role :func:`is_due`."""
    c = row.get("content")
    if permit_ != FETCH or row["identity"]["is_masked"] or c is None:
        return []
    live = {m["source_id"]: m for m in row["identity"]["members"] if m["is_live"]}
    return [(i, live[r["source_id"]]) for i, r in enumerate(c["roles"] or [])
            if r.get("source_id") in live and r.get("lane") in FETCH_LANES and is_due(r, now)]


def inband_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(source_id, content_role, sha256)`` of an in-band entry; ``sha256`` of its ``raw``, else its
    ``text`` (D8): the in-band snapshot's ``raw.sha256``."""
    return entry["source_id"], entry["content_role"], (entry["raw"] or entry["text"])["sha256"]


def minted_inband(content: Mapping[str, Any] | None) -> set[tuple[str, str, str]]:
    return {(s["source_id"], s["content_role"], s["raw"]["sha256"])
            for s in (content or {}).get("snapshots") or [] if s["kind"] == "inband"}


def is_stale(row: Mapping[str, Any]) -> bool:
    c = row.get("content")
    return c is None or c["members_digest"] != row["identity"]["members_digest"]


def reasons(row: Mapping[str, Any], now: dt.datetime, no_basis_fetches: bool, *,
            pin: str | None = None, quality_gate: str | None = None,
            structured: frozenset[str] = frozenset()) -> list[str]:
    """The scope clauses ``row`` matches (empty = not in scope). ``pin`` and ``quality_gate`` (the
    convert pin and the quality gate's digest) enable the ``quality`` clause; ``pin`` alone the
    ``convert`` clause."""
    if row["state"] not in SCOPE_STATES:
        return []
    out: list[str] = []
    c = row.get("content")
    permit_ = permit(row, no_basis_fetches)
    if permit_ == FETCH:
        out.append("fetch")
    if fetch_items(row, permit_, now):  # D698: what fetch would call, not every due role
        out.append("due")
    minted = minted_inband(c)
    if any(inband_key(e) not in minted for e in row["inband"]["entries"] or []
           if e["content_role"] not in RECORD_ROLES):  # D522: a record is never minted
        out.append("inband")
    if is_stale(row):
        out.append("stale")
    elif row["state"] == "triaged":
        out.append("repoint")
    if quality_gate is not None and c is not None and needs_verdict(row, pin, quality_gate, structured):
        out.append("quality")
    if pin is not None and c is not None and awaits_text(row, pin, structured):
        out.append("convert")
    return out


def select(rows: Iterable[Mapping[str, Any]], now: dt.datetime, no_basis_fetches: bool,
           counts: dict[str, int], only: Collection[str] | None = None, *, pin: str | None = None,
           quality_gate: str | None = None, structured: frozenset[str] = frozenset(),
           order: str = "doc_id") -> list[str]:
    """The ``doc_id``s in scope, in ``order`` (module docstring, Order); fills ``counts`` (``rows_in_scope``,
    ``out_of_scope`` (``identified``), ``retired``, ``idle``, ``scope.<clause>``). With ``only``
    (the run's filter, module docstring) a row whose ``doc_id`` is not in it is out of scope and
    counted ``scope.filtered_out`` (always set, 0 when nothing is rejected), nothing else."""
    if order not in ORDERS:
        raise ValueError(f"options.order must be one of {list(ORDERS)}, got {order!r}")
    picked: list[str] = []
    published: dict[str, dt.datetime | None] = {}
    if only is not None:
        counts["scope.filtered_out"] = 0
    for row in rows:
        if only is not None and row["doc_id"] not in only:
            counts["scope.filtered_out"] += 1
            continue
        st = row["state"]
        if st == "identified":
            counts["out_of_scope"] = counts.get("out_of_scope", 0) + 1
            continue
        if st == "retired":
            counts["retired"] = counts.get("retired", 0) + 1
            continue
        why = reasons(row, now, no_basis_fetches, pin=pin, quality_gate=quality_gate, structured=structured)
        if not why:
            counts["idle"] = counts.get("idle", 0) + 1
            continue
        for w in why:
            counts[f"scope.{w}"] = counts.get(f"scope.{w}", 0) + 1
        picked.append(row["doc_id"])
        published[row["doc_id"]] = (row.get("metadata") or {}).get("published_at")
    counts["rows_in_scope"] = len(picked)
    out = sorted(picked)
    if order == "newest_first":  # stable: equal dates (and the undated, last) stay in doc_id order
        out.sort(key=lambda d: _newest_first_key(published[d]))
    return out


def _newest_first_key(at: dt.datetime | None) -> tuple[bool, float]:
    """Sort key of ``newest_first``: dated rows newest first, undated after them (a naive
    timestamp is read as UTC)."""
    if at is None:
        return (True, 0.0)
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)
    return (False, -at.timestamp())


__all__ = ["FETCH", "GATED", "NO_BASIS", "ORDERS", "SCOPE_STATES", "SKIP", "fetch_items", "inband_key", "is_due",
           "is_stale",
           "minted_inband", "permit", "reasons", "select"]
