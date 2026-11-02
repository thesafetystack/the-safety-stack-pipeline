"""``SCREEN`` payload (A.4; v3's ``stack/types/payloads.py:127-150``): the relevance screen over text."""

from __future__ import annotations

from stack.schemas.structs import LS, QUOTE, ST, D, I, S

SCREEN = ST(
    verdict=S,
    basis_role=S,
    buckets=LS(S),
    quotes=LS(QUOTE),
    off_topic_reason=S,
    confidence=D,
    reasoning=S,
    adjudication_tier=I,
    tier1_verdict=S,
    tier1_model=S,
)
