"""``coverage`` (A.5, D20): one query window or lookup batch."""

from __future__ import annotations

import pyarrow as pa

from stack.schemas.blocks import T2, TS, B, I, S, fields
from stack.schemas.families import FamilySpec

SPEC = FamilySpec(
    name="coverage",
    kind="coverage",
    schema=pa.schema(
        fields(
            channel_=S,
            query_digest_=S,
            window_start_at_=TS,
            window_end_at_=TS,
            query_=S,
            seen=I,
            kept=I,
            is_exhausted_=B,
            next_cursor=S,
            gate_digest=S,  # D20
        )
        + T2
    ),
    key=("channel", "query_digest", "window_start_at", "window_end_at"),
)
