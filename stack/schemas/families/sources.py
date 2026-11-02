"""``sources`` (A.5): one provider's view of one item seen by one run, or a miss. Key ``source_id``
(within one file; in a chain ``(source_id, run_id)``, §2.3).

Amendment to A.5 as written (round 4, P): ``metadata.matched_by`` lists every query of the run
that returned the source (:data:`MATCH`: the collector's readable ``query_id``, e.g.
``kw:group:<id>``, ``au:<Last_First>``, ``feed:<channel>``; the ``coverage.query_digest`` it joins
to; the human ``label``), deduplicated and sorted; ``metadata.query_digest`` is the first one's.
Nullable, so stored files upcast with NULL (``stack migrate upcast --family sources``, D208).

Amendment (D524): ``metadata.record_ref`` is retired. The provider's record of the item is the
``provider_record`` in-band entry (D522); the v3 import's ``meta_id`` is a ``v3_meta_id`` identity key.
Files of versions 1-3 do not read since D690 (every store was upcast on disk once, D691; the steps went
with D693)."""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.blocks import AUTHOR, INBAND, KEY, LS, ST, T1, TS, B, I, S, fields
from stack.schemas.families import FamilySpec

#: One query that returned the source in the run (``metadata.matched_by[]``; module docstring).
MATCH = ST(query_id_=S, query_digest_=S, label=S)
SOURCE_IDENTITY = ST(source_id_=S, channel_=S, external_id=S, canonical_url=S, keys_=LS(KEY))
SOURCE_METADATA = ST(
    title=S,
    abstract=S,
    authors=LS(AUTHOR),
    published_at=TS,
    provider_updated_at=TS,
    doc_type=S,
    is_withdrawn=B,
    sensitivity=LS(S),
    license_class=S,
    origin_=S,
    provider_version=I,
    lookup_of_source_ids=LS(S),
    miss_count=I,
    query_digest=S,
    matched_by=LS(MATCH),
)

SPEC = FamilySpec(
    name="sources",
    kind="sources",
    schema=pa.schema(
        fields(source_id_=S, state_=S, record_digest_=S, record_format_=S, observed_at_=TS)
        + T1
        + fields(identity=SOURCE_IDENTITY, metadata=SOURCE_METADATA, inband=INBAND)
    ),
    key=("source_id",),
    nested_unique=(("inband.entries[]", ("source_id", "content_role", "raw.sha256", "text.sha256")),),
)
