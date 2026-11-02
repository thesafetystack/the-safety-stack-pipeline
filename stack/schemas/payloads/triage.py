"""``TRIAGE`` payload (A.4): the gate's decision over current fields, one per document."""

from __future__ import annotations

from stack.schemas.structs import LS, QUESTION, ST, S

TRIAGE = ST(decision_=S, wanted_by_=LS(S), questions=LS(QUESTION))
