"""``<wf>_evidence`` rows (A.1 EVI, A.5): one row per invocation of a workflow.

v3 kept an invocation as an ``annotations`` row plus a mandatory ``ann_type='receipt'`` row
(``stack/core/writer.py:1487-1530`` at 9312c8c) and counted attempts from the receipts
(``workflows/annotate/screen/main.py:503-510``). v4 keeps both facts on the one evidence row:
EVI (who, on what basis, under which config, with which model, at what cost) + the payload's
top-level columns + T2 (status, attempts, backoff).

Rules:

* ``evidence_id`` is minted per :mod:`stack.core.ids` (kind ``annotation``, letter ``a``; D51);
* ``work_digest`` is the work key (:func:`stack.annotate.basis.work_digest`), ``item_key = ''``
  (an invocation is per document at both grains);
* ``origin = 'ai_check'`` for a model answer (an :class:`~stack.llm.LlmResult` or its EVI
  fields), ``'computed'`` for a workflow that answers without a model;
* ``supersedes`` = the evidence a retry follows (the newest evidence of the same work key);
* ``result_digest`` = :func:`result_digest` of the payload for ``ok``/``empty``, NULL otherwise;
  V8 recomputes it and ``work_digest`` from the row. A producer's row carries its records'
  payloads in the record struct columns (``records``; registry ``produced_by``): the digest then
  covers the payload plus every **non-NULL** record column by name (:func:`evidence_payload`), so
  a row without records (a pin that produces none, or a row written before the columns existed)
  keeps the payload-only digest;
* attempts: ``attempts`` counts the invocations of one work key (a new key starts at 1);
  ``failed_transient`` backs off :func:`backoff`: the first failure is due again at once
  (:data:`FIRST_RETRY`, so the next run, the next pass of ``stack build --passes``, retries it),
  later ones ``1h·2^(attempts-1)`` capped at 7 d (§3.1); a transient failure on the cap's attempt
  (annotate cap 3) is ``quarantined[cap_reached]`` instead. Run-level stops write no evidence
  (``not_attempted[budget|halted|limit|unanswered]``), so they never count an attempt.

The evidence file of a run is cumulative: every prior evidence row, byte for byte, then
this run's rows (D-row "evidence carry"), so a chain of one still holds the history ``select``
and X8 read.

**The selected row of a work key** (:func:`selected`; ``select``, the prior carry and X8 all use
it): rows of one key form a chain through ``supersedes``; order is the chain position, then
``decided_at``, then ``evidence_id``. The newest **decided** row (``ok``/``empty``) is selected
when there is one, else the newest row. A decided result is thus never displaced by a later
failure of the same work key, which only a multi-tier answer produces (screen: a failed tier-2
adjudication after tier 1's ``borderline`` keeps tier 1's verdict; the failure stays in the
evidence as history). Tier rows of one invocation share ``decided_at``, so the chain position,
not the random ``evidence_id``, orders them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from stack.annotate.basis import work_digest
from stack.checks.digests import H, render_ts
from stack.core import ids
from stack.schemas.families.evidence import TRIAGE_QUESTION, USAGE_WORKFLOWS
from stack.schemas.payloads.registry import PayloadSpec, records_of
from stack.schemas.payloads.registry import payload as registry_entry

Row = dict[str, Any]

#: Annotate cap (§3.1, frozen): invocations per work key before ``quarantined[cap_reached]``.
CAP = 3
BACKOFF_BASE = dt.timedelta(hours=1)
BACKOFF_MAX = dt.timedelta(days=7)
#: The delay after a work key's first transient failure (round 4, passes: due at the next run).
FIRST_RETRY = dt.timedelta(0)
#: The EVI columns an LLM call fills (:meth:`stack.llm.LlmResult.evidence_fields`).
LLM_COLUMNS: tuple[str, ...] = ("is_cache_hit", "template_digest", "model_digest", "prompt_name",
                                "rendered_digest", "model", "response_digest", "cost_usd",
                                "tokens_in", "tokens_out", "latency_ms")
#: The usage-detail columns of the evidence of :data:`USAGE_WORKFLOWS` (D714; ``LlmResult.usage_fields``).
USAGE_COLUMNS: tuple[str, ...] = ("tokens_cached", "tokens_cache_write", "tokens_reasoning")
OK_LIKE: frozenset[str] = frozenset({"ok", "empty"})
#: ``triage_evidence``'s payload columns (A.5: ``question, verdict, confidence, reasoning``, one
#: question per row) as a registry-shaped spec: the evidence builder makes triage's rows with it
#: (``workflows/triage/ask.py``) and V8 recomputes their digests with it (D166 (C)). The
#: registry's ``triage`` spec is the annotation payload (``TRIAGE``), not the evidence's.
TRIAGE_EVIDENCE_SPEC = PayloadSpec(workflow="triage", version=1, payload_schema=pa.struct(TRIAGE_QUESTION),
                                   grain="document", text_bearing=(), evidence=True, basis_kind="fields")


def backoff(attempts: int) -> dt.timedelta:
    """:data:`FIRST_RETRY` after the first attempt, else ``1h·2^(attempts-1)`` capped at 7 d
    (attempt 2 → 2 h)."""
    if attempts <= 1:
        return FIRST_RETRY
    step: dt.timedelta = BACKOFF_BASE * (2 ** max(attempts - 1, 0))
    return min(step, BACKOFF_MAX)


def _canon(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return render_ts(v)
    if isinstance(v, Mapping):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v


def result_digest(payload: Mapping[str, Any] | None) -> str | None:
    """``H(payload)`` with every payload field present (missing ones NULL); NULL payload → NULL."""
    return None if payload is None else H(_canon(dict(payload)))


def payload_fields(spec: PayloadSpec) -> list[str]:
    """The payload's top-level field names (the evidence columns after EVI)."""
    return [f.name for f in spec.payload_schema]


def payload_of(row: Mapping[str, Any], spec: PayloadSpec) -> dict[str, Any]:
    """The payload of an evidence row (its top-level payload columns)."""
    return {n: row.get(n) for n in payload_fields(spec)}


def evidence_payload(row: Mapping[str, Any], spec: PayloadSpec) -> dict[str, Any]:
    """What an evidence row's ``result_digest`` covers: the payload columns plus the producer's
    non-NULL record columns by name (module docstring)."""
    recs = {r: row.get(r) for r in records_of(spec.workflow) if row.get(r) is not None}
    return payload_of(row, spec) | recs


def record_payload(row: Mapping[str, Any], spec: PayloadSpec) -> dict[str, Any] | None:
    """The payload ``row`` (a ``<wf>_evidence`` row) gives ``spec``: its payload columns for the
    producer itself, the record's struct column for a record (``produced_by``); None when NULL."""
    if not spec.is_record:
        return payload_of(row, spec)
    val = row.get(spec.workflow)
    return None if val is None else {n: val.get(n) for n in payload_fields(spec)}


def complete_payload(payload: Mapping[str, Any], spec: PayloadSpec) -> dict[str, Any]:
    """``payload`` with every top-level field present; an unknown key raises ``ValueError``
    (a workflow returning a field the frozen payload type lacks is a bug, D43)."""
    names = payload_fields(spec)
    extra = sorted(set(payload) - set(names))
    if extra:
        raise ValueError(f"{spec.workflow}: payload fields {extra} not in the registry type {names}")
    return {n: payload.get(n) for n in names}


def llm_columns(llm: Any) -> dict[str, Any]:
    """The EVI LLM columns from an :class:`~stack.llm.LlmResult` (``evidence_fields()``) or a
    mapping of those columns (a two-tier workflow merges its calls into one)."""
    fields = llm.evidence_fields() if hasattr(llm, "evidence_fields") else {
        k: v for k, v in dict(llm).items() if k not in USAGE_COLUMNS}
    extra = sorted(set(fields) - set(LLM_COLUMNS))
    if extra:
        raise ValueError(f"LLM evidence fields {extra} are not EVI columns {list(LLM_COLUMNS)}")
    return {c: fields.get(c) for c in LLM_COLUMNS}


def usage_columns(llm: Any) -> dict[str, Any]:
    """The usage-detail columns (:data:`USAGE_COLUMNS`) of an :class:`~stack.llm.LlmResult` or a merged mapping
    (absent keys NULL); all NULL without ``llm``."""
    if llm is None:
        return dict.fromkeys(USAGE_COLUMNS)
    fields = llm.usage_fields() if hasattr(llm, "usage_fields") else dict(llm)
    return {c: fields.get(c) for c in USAGE_COLUMNS}


def _empty_llm() -> dict[str, Any]:
    return {c: None for c in LLM_COLUMNS} | {"is_cache_hit": False}


def evidence_row(spec: PayloadSpec, *, doc_id: str, source_id: str, basis_digest: str,
                 config_digest: str, run_id: str, now: dt.datetime, status: str,
                 payload: Mapping[str, Any] | None, llm: Any | None, origin: str,
                 attempts: int, first_attempt_at: dt.datetime | None,
                 supersedes: str | None, error_class: str | None = None, error: str | None = None,
                 cost_usd: float | None = None, evidence_id: str | None = None,
                 records: Mapping[str, Mapping[str, Any] | None] | None = None) -> Row:
    """One evidence row (module docstring). ``status`` ``ok``/``empty`` carries ``payload`` and
    ``records`` (record name → its payload, for the records the producer's pin produces; every
    other record column NULL); a failure carries ``error_class``/``error`` and NULL payload and
    record columns; ``failed_transient`` gets ``retry_after = now + backoff(attempts)`` and
    becomes ``quarantined[cap_reached]`` at :data:`CAP`. ``cost_usd`` (failures: the attempts'
    reconciled cost) overrides ``llm``'s. A record name the producer does not produce raises."""
    mine = records_of(spec.workflow)
    extra = sorted(set(records or {}) - set(mine))
    if extra:
        raise ValueError(f"{spec.workflow}: records {extra} are not records of it (registry: {list(mine)})")
    recs: dict[str, Any] = dict.fromkeys(mine)
    if status in OK_LIKE:
        if payload is None:
            raise ValueError(f"{spec.workflow}: an {status} evidence row needs a payload")
        pay = complete_payload(payload, spec)
        for r, val in (records or {}).items():
            recs[r] = None if val is None else complete_payload(val, registry_entry(r))
        error_class = error = None
    else:
        pay = {n: None for n in payload_fields(spec)}
    retry_after = None
    if status == "failed_transient":
        if attempts >= CAP:
            status, error_class = "quarantined", "cap_reached"
        else:
            retry_after = now + backoff(attempts)
    cols = llm_columns(llm) if llm is not None else _empty_llm()
    if cost_usd is not None:
        cols["cost_usd"] = cost_usd
    usage = usage_columns(llm) if spec.workflow in USAGE_WORKFLOWS else {}
    return {
        "evidence_id": evidence_id or ids.mint("annotation"),
        "work_digest": work_digest(source_id, basis_digest, config_digest, ""),
        "source_id": source_id, "basis_kind": spec.basis_kind, "basis_digest": basis_digest,
        "config_digest": config_digest, "item_key": "", "doc_id": doc_id, "origin": origin,
        "decided_at": now, "supersedes": supersedes, **cols,
        "result_digest": result_digest(evidence_payload(pay | recs, spec)) if status in OK_LIKE else None,
        **pay, **usage, **recs,
        "status": status, "run_id": run_id, "error_class": error_class, "error": error,
        "attempts": attempts, "first_attempt_at": first_attempt_at or now, "last_attempt_at": now,
        "retry_after": retry_after,
    }


def with_digests(row: Mapping[str, Any], spec: PayloadSpec) -> Row:
    """``row`` with ``work_digest`` and ``result_digest`` recomputed (what V8 checks); for
    synthetic evidence rows made field by field (tests)."""
    out = dict(row)
    out["work_digest"] = work_digest(row["source_id"], row["basis_digest"], row["config_digest"],
                                     row["item_key"])
    out["result_digest"] = (result_digest(evidence_payload(row, spec)) if row["status"] in OK_LIKE
                            else None)
    return out


def _chain_order(rows: Sequence[Mapping[str, Any]]) -> Any:
    """Sort key of rows of one work key: ``(supersedes depth within rows, decided_at, evidence_id)``."""
    ids = {e["evidence_id"]: e for e in rows}

    def depth(e: Mapping[str, Any]) -> int:
        d, seen = 0, {e["evidence_id"]}
        while e["supersedes"] in ids and e["supersedes"] not in seen:
            e = ids[e["supersedes"]]
            seen.add(e["evidence_id"])
            d += 1
        return d

    return lambda e: (depth(e), e["decided_at"], e["evidence_id"])


def newest(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The newest evidence row of one work key (chain position, ``decided_at``, ``evidence_id``)."""
    return max(rows, key=_chain_order(rows), default=None)


def selected(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The selected evidence row of one work key: the newest decided (``ok``/``empty``) row,
    else the newest row (module docstring)."""
    decided = [e for e in rows if e["status"] in OK_LIKE]
    return max(decided or rows, key=_chain_order(rows), default=None)


def by_work(evidence: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Work key → its selected evidence row (:func:`selected`)."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for e in evidence:
        groups.setdefault(e["work_digest"], []).append(e)
    return {k: selected(v) for k, v in groups.items()}  # type: ignore[misc]


def accepted_row(latest: Mapping[str, Mapping[str, Any]], source_id: str, basis_digest: str,
                 configs: Sequence[str]) -> Mapping[str, Any] | None:
    """The accepted evidence row of a document (D758, :mod:`stack.plan` "Accepted configs"): of ``configs`` (a
    line's accepted configs, in preference order), the first whose work key for the same source and basis has a
    **decided** (``ok``/``empty``) selected row in ``latest`` (:func:`by_work`); ``None`` when none has. A
    failure is never accepted, and another basis is never crossed (the work key holds both)."""
    for cfg in configs:
        e = latest.get(work_digest(source_id, basis_digest, cfg, ""))
        if e is not None and e["status"] in OK_LIKE:
            return e
    return None


def prior_decision(prev: Mapping[str, Any] | None, now: dt.datetime) -> str:
    """The prior-evidence table for one work key: ``ask`` (no evidence; or a due
    ``failed_transient`` under the cap), else ``carry`` (``ok``/``empty``, a transient not yet
    due, ``failed_permanent``, ``quarantined``). A ``failed_permanent`` row (``truncated``,
    ``schema_invalid``, …) is re-asked only under a new work key: a pin, prompt, schema or output
    cap change moves the config digest, so ``prev`` is None for the new key."""
    if prev is None:
        return "ask"
    if (prev["status"] == "failed_transient" and prev["attempts"] < CAP
            and (prev["retry_after"] is None or prev["retry_after"] <= now)):
        return "ask"
    return "carry"


__all__ = [
    "BACKOFF_BASE",
    "BACKOFF_MAX",
    "CAP",
    "FIRST_RETRY",
    "LLM_COLUMNS",
    "USAGE_COLUMNS",
    "OK_LIKE",
    "accepted_row",
    "backoff",
    "by_work",
    "complete_payload",
    "evidence_payload",
    "evidence_row",
    "llm_columns",
    "newest",
    "payload_fields",
    "payload_of",
    "prior_decision",
    "record_payload",
    "result_digest",
    "selected",
    "usage_columns",
    "with_digests",
]
