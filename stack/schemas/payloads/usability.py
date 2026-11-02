"""``USABILITY`` payload (A.4): is the current text usable as the document's body?

Annotation record ``text_usability`` (``produced_by: screen``; no prompt produces it yet, the
``screen-v11`` template adds it). ``verdict`` in :data:`USABILITY_VERDICTS`, ``issues`` a subset of
:data:`USABILITY_ISSUES`, ``note`` free text from the model (``text_bearing``).
"""

from __future__ import annotations

from stack.schemas.structs import LS, ST, I, S

USABILITY_VERDICTS: tuple[str, ...] = ("usable", "degraded", "unusable")
USABILITY_ISSUES: tuple[str, ...] = ("not_body", "cut_off", "garbled_maths", "garbled_tables", "bad_encoding",
                                     "mostly_markup", "stub", "other")
USABILITY = ST(
    verdict=S,
    issues=LS(S),
    note=S,
    prompt_name=S,
    model=S,
    basis_role=S,
    adjudication_tier=I,
)
