"""``<wf>`` (A.5): the selected result of one annotation workflow per document or item.

Columns: SEL + ANN common fields + payload fields as flat columns + T2, concatenated in that
order and keeping the first occurrence of a name that repeats (``evidence_id`` from SEL;
``status``, ``error_class`` with their ANN meaning; one ``run_id`` from T2). At item grain the
payload's one ``list<struct>`` is exploded: one row per item, the item's fields flat, its
``item_key`` the SEL key column. Key ``(doc_id, item_key)``; ``item_key = ''`` at document grain.
"""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.blocks import ANN_COMMON, LS, T2, S, fields
from stack.schemas.families import FamilySpec, merge_columns
from stack.schemas.payloads.registry import PayloadSpec, payload

SEL = fields(
    doc_id_=S,
    item_key_=S,
    evidence_id=S,
    source_id=S,
    override_ids_=LS(S),
    override_stale_ids_=LS(S),
    result_digest=S,
)


def payload_columns(p: PayloadSpec) -> list[pa.Field]:
    """The payload's fields as flat columns (item grain: the item struct's fields)."""
    if p.grain == "item":
        (items,) = list(p.payload_schema)
        return list(items.type.value_type)
    return list(p.payload_schema)


def wf_schema(workflow: str) -> pa.Schema:
    """The ``<wf>`` schema of a registered workflow."""
    return pa.schema(merge_columns(SEL, ANN_COMMON, payload_columns(payload(workflow)), T2))


def spec(workflow: str) -> FamilySpec:
    return FamilySpec(
        name=workflow,
        kind="wf",
        schema=wf_schema(workflow),
        key=("doc_id", "item_key"),
        workflow=workflow,
    )
