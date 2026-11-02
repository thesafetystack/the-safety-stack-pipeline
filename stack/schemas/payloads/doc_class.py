"""``DOC_CLASS`` payload (A.4): document type, self-publication, primary language, over fields."""

from __future__ import annotations

from stack.schemas.structs import ST, B, S

DOC_CLASS = ST(doc_type=S, self_published=B, primary_lang=S)
