"""``<wf>_evidence`` (A.5): one invocation of a workflow. Key ``evidence_id``.

Columns: EVI + payload columns + one nullable struct column per annotation record the workflow
produces (``<record> ST(<record payload>)``, registry order,
:func:`stack.schemas.payloads.registry.records_of`) + T2 (repeated names keep their first
occurrence, as in ``<wf>``). A record has no evidence family of its own.
``triage_evidence`` is one question's invocation: EVI + ``question! S, verdict S, confidence D,
reasoning S`` + ``basis S`` (D527) + T2. Other workflows' payload columns are the payload struct's top-level fields:
an invocation is per document, so an item-grain payload stays one ``items`` list here.
"""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.blocks import T2, TS, B, D, I, S, fields
from stack.schemas.families import FamilySpec, merge_columns
from stack.schemas.payloads.registry import payload, records_of

EVI = fields(
    evidence_id_=S,
    work_digest_=S,
    source_id_=S,
    basis_kind_=S,
    basis_digest_=S,
    config_digest_=S,
    item_key_=S,
    doc_id_=S,
    origin_=S,
    decided_at_=TS,
    supersedes=S,
    is_cache_hit_=B,
    template_digest=S,
    model_digest=S,
    prompt_name=S,
    rendered_digest=S,
    model=S,
    response_digest=S,
    cost_usd=D,
    tokens_in=I,
    tokens_out=I,
    latency_ms=I,
    result_digest=S,
)
TRIAGE_QUESTION = fields(question_=S, verdict=S, confidence=D, reasoning=S)
#: ``triage_evidence`` only, after the payload columns and outside the payload (so ``result_digest`` is
#: unchanged): what the question was asked on (D527): ``fields`` (title, authors, abstract, excerpt) or
#: ``title`` (a row with neither abstract nor excerpt, asked on its title and metadata under the triage
#: option ``title_basis``); NULL on rows written before D527 (``fields``).
TRIAGE_BASIS = fields(basis=S)
#: The provider usage details of an invocation's calls (D714), summed over its calls: prompt tokens read from and
#: written to the provider's prompt cache, and reasoning tokens (part of ``tokens_out``); NULL when no call
#: reported them (a cache hit, a failure, a reply without the fields). After the payload columns and outside the
#: payload (``result_digest`` unchanged), on the evidence of the workflows in :data:`USAGE_WORKFLOWS` only.
LLM_USAGE = fields(tokens_cached=I, tokens_cache_write=I, tokens_reasoning=I)
USAGE_WORKFLOWS: frozenset[str] = frozenset({"technique_staged", "model_roles"})


def evidence_schema(workflow: str) -> pa.Schema:
    """The ``<wf>_evidence`` schema of a registered workflow with ``evidence: true``."""
    p = payload(workflow)
    if not p.evidence:
        raise ValueError(f"{workflow}: registry says evidence: false; no {workflow}_evidence family")
    if p.is_record:
        raise ValueError(f"{workflow}: a record's evidence is {p.evidence_family}; no {workflow}_evidence family")
    cols = TRIAGE_QUESTION + TRIAGE_BASIS if workflow == "triage" else list(p.payload_schema)
    if workflow in USAGE_WORKFLOWS:
        cols = cols + LLM_USAGE
    recs = [pa.field(r, payload(r).payload_schema) for r in records_of(workflow)]
    return pa.schema(merge_columns(EVI, cols, recs, T2))


def spec(workflow: str) -> FamilySpec:
    return FamilySpec(
        name=f"{workflow}_evidence",
        kind="evidence",
        schema=evidence_schema(workflow),
        key=("evidence_id",),
        workflow=workflow,
    )
