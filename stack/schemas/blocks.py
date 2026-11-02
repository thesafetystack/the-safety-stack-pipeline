"""The blocks of a ``documents`` row (A.3) and the annotation types (A.4), frozen (D43).

Column order is file order. Every struct block carries ``run_id`` and ``updated_at`` (§5.1).
The A.2 structs live in :mod:`stack.schemas.structs` and are re-exported here.

Choices on top of A.3 as written (T1.1 builder, proposed decisions):

* ``content`` drops ``lane`` (D11); it is NULL until ``acquire`` (D7), so the column is nullable.
* ``identity``, ``metadata``, ``inband``, ``annotations`` are non-nullable columns: every state of
  §2.5 requires the first three, and D7 makes ``inband`` (``entries = []``) and ``annotations``
  (typed fields NULL, ``other = []``) non-NULL from ``gather`` on.
"""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.payloads.registry import REGISTRY
from stack.schemas.structs import (
    AFFIL,
    AUTHOR,
    BLOB,
    INBAND_ENTRY,
    KEY,
    KEYSRC,
    LS,
    MEMBER,
    QUESTION,
    QUOTE,
    REVISION,
    ROLE,
    SNAPSHOT,
    ST,
    T0,
    T1,
    T2,
    TS,
    B,
    D,
    I,
    L,
    S,
    fields,
)

__all__ = [
    "AFFIL",
    "ANNOTATIONS",
    "ANN_COMMON",
    "ANN_JSON",
    "AUTHOR",
    "BLOB",
    "BLOCK_NAMES",
    "CONTENT",
    "DOCUMENT_FIELDS",
    "IDENTITY",
    "INBAND",
    "INBAND_ENTRY",
    "KEY",
    "KEYSRC",
    "LS",
    "MEMBER",
    "METADATA",
    "QUESTION",
    "QUOTE",
    "REVISION",
    "ROLE",
    "SNAPSHOT",
    "ST",
    "STATE_FIELDS",
    "T0",
    "T1",
    "T2",
    "TS",
    "B",
    "D",
    "I",
    "L",
    "S",
    "ann_type",
    "fields",
]

# ---------------------------------------------------------------- A.4 annotations
#: ANN common fields (exact; frozen after adoption).
ANN_COMMON = fields(
    workflow_=S,
    version_=I,
    status_=S,
    error_class=S,
    basis_kind_=S,
    basis_digest=S,
    config_digest_=S,
    decided_at_=TS,
    evidence_id=S,
    origin_=S,
    is_basis_current=B,
)


def ann_type(payload: pa.DataType) -> pa.StructType:
    """``ANN<P> = ST(ANN common fields..., payload P)``."""
    return pa.struct([*ANN_COMMON, pa.field("payload", payload)])


#: ``ANN<JSON>``: an untyped annotation, identified by ``workflow`` and ``version``.
ANN_JSON = ann_type(ST(item_key_=S, payload_json_=S))

#: The ``annotations`` block: one typed ``ANN<P>`` per registered workflow in registry order,
#: then ``other! LS(ANN<JSON>)``.
ANNOTATIONS = pa.struct(
    [pa.field(wf, ann_type(spec.payload_schema)) for wf, spec in REGISTRY.items()]
    + [pa.field("other", LS(ANN_JSON), nullable=False)]
)

# ---------------------------------------------------------------- A.3 blocks
STATE_FIELDS = fields(doc_id_=S, state_=S, run_id_=S, created_at_=TS, updated_at_=TS)

IDENTITY = ST(
    anchor_source_id_=S,
    members_=LS(MEMBER),
    member_ids_=LS(S),
    keys_=LS(KEYSRC),
    channels_=LS(S),
    former_doc_ids_=LS(S),
    merged_into=S,
    is_masked_=B,
    mask_reason=S,
    override_ids_=LS(S),
    members_digest_=S,
    run_id_=S,
    updated_at_=TS,
)
METADATA = ST(
    title=S,
    abstract=S,
    authors=LS(AUTHOR),
    published_at=TS,
    provider_updated_at=TS,
    doc_type=S,
    is_withdrawn_=B,
    sensitivity_=LS(S),
    field_sources_=LS(ST(field_=S, source_id_=S, origin_=S)),
    fields_digest_=S,
    run_id_=S,
    updated_at_=TS,
)
INBAND = ST(entries_=LS(INBAND_ENTRY), run_id_=S, updated_at_=TS)
CONTENT = ST(
    convert_pin=S,  # D690 (was config_digest)
    snapshots_=LS(SNAPSHOT),
    roles_=LS(ROLE),
    current_snapshot_id=S,
    current_source_id=S,
    current_role=S,
    current_text_sha256=S,  # D690 (was current_text_digest)
    current_text_status_=S,
    current_text_media=S,
    current_text_quality=S,
    current_revision=S,
    revisions=LS(REVISION),
    members_digest_=S,
    same_raw_doc_ids_=LS(S),
    run_id_=S,
    updated_at_=TS,
)

#: Block columns in file order; ``blocks_written`` names a subset of these.
BLOCK_NAMES: tuple[str, ...] = ("identity", "metadata", "inband", "content", "annotations")

#: The ``documents`` (= ``corpus``) columns in file order.
DOCUMENT_FIELDS: list[pa.Field] = STATE_FIELDS + [
    pa.field("identity", IDENTITY, nullable=False),
    pa.field("metadata", METADATA, nullable=False),
    pa.field("inband", INBAND, nullable=False),
    pa.field("content", CONTENT, nullable=True),
    pa.field("annotations", ANNOTATIONS, nullable=False),
]
