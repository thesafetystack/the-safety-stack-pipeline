"""``RELEVANCE`` and ``SR_RELEVANCE`` payloads (A.4): the per-question answers of one screen call.

Annotation records (``produced_by: screen``): ``stack_relevance`` (question S, the Stack's
scope) and ``sr_relevance`` (question R, Shallow Review topic relevance, with SR's category and
agenda). Each keeps the question's own verdict, confidence and quotes (anchored against the whole
text, as ``SCREEN.quotes``), the prompt name and the model that answered (for humans; the version
a reader pins is the record's ``config_digest``), the basis role, the tier that answered and tier
1's verdict on the same question.
"""

from __future__ import annotations

from stack.schemas.structs import LS, QUOTE, ST, D, I, S

RELEVANCE = ST(
    verdict=S,
    confidence=D,
    quotes=LS(QUOTE),
    prompt_name=S,
    model=S,
    basis_role=S,
    adjudication_tier=I,
    tier1_verdict=S,
)
SR_RELEVANCE = ST(
    verdict=S,
    confidence=D,
    quotes=LS(QUOTE),
    category=S,
    agenda=S,
    prompt_name=S,
    model=S,
    basis_role=S,
    adjudication_tier=I,
    tier1_verdict=S,
)
