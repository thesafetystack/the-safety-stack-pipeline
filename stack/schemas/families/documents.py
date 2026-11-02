"""``documents`` (A.3) and ``corpus`` (= ``documents``, §12): one document as its last writer
left it / as a fold selected it. Key ``doc_id``."""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.blocks import DOCUMENT_FIELDS
from stack.schemas.families import FamilySpec

#: S5 for a documents row: members by source; roles per (source_id, content_role); snapshots by
#: id; in-band entries per (source_id, content_role, raw.sha256, text.sha256); triage questions (each its own
#: evidence) by question; untyped annotations per (workflow, version, item_key).
NESTED_UNIQUE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("identity.members[]", ("source_id",)),
    ("content.roles[]", ("source_id", "content_role")),
    ("content.snapshots[]", ("snapshot_id",)),
    ("inband.entries[]", ("source_id", "content_role", "raw.sha256", "text.sha256")),
    ("annotations.triage.payload.questions[]", ("question",)),
    ("annotations.other[]", ("workflow", "version", "payload.item_key")),
)

SPEC = FamilySpec(
    name="documents",
    kind="documents",
    schema=pa.schema(DOCUMENT_FIELDS),
    key=("doc_id",),
    nested_unique=NESTED_UNIQUE,
)
CORPUS_SPEC = FamilySpec(
    name="corpus",
    kind="corpus",
    schema=pa.schema(DOCUMENT_FIELDS),
    key=("doc_id",),
    nested_unique=NESTED_UNIQUE,
)
