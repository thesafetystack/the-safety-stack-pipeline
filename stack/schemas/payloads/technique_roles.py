"""``TECHNIQUE_ROLES`` payload (A.4, round 5 T-ANN): which roles each catalogue technique plays in
the document (``annotate.technique_roles``; one judge call against the rubric v0.3.1
(``workflows/annotate/technique_roles/prompts/rubric-v0.3.1.md``) with the whole taxonomy v0.8 as the menu,
ported from the off-DB prototype ``tomas/technique-roles`` df411eb, approach A).

``ROLES`` holds the rubric's twelve categories (§3, in rubric order), each a strength 0-4; a role
the judge omitted (rubric §6 expresses zero by omission) is stored as an explicit ``0``.
``techniques``: one row per catalogue slug the judge found (taxonomy order), with the judge's
quotes (each ``verified`` = it anchors in the stored text, :mod:`stack.anchor`), the row flags
(the rubric's ``claims_absent``, ``confirmed``, ``recommended``, ``rejected``, ``supportive``,
``variant:<name>``, plus the pipeline's ``quote_unverified`` when no quote verified) and an
optional ``note``; ``extra_techniques``: off-menu techniques (verbatim name, gloss, the nearest
menu slug as ``hint_technique``), including any row the judge filed under a slug that is not on the
menu; ``flags``: the document flags (``no_techniques_found``, ``menu_gap:<surface>``); ``repairs``:
every post-processing repair the pipeline applied. The prototype's ``judge``, ``low_confidence``
and ``taxonomy_stamp`` are not carried (the evidence row carries model, prompt name and template
digest; the config digest covers rubric, template and the taxonomy file).
"""

from __future__ import annotations

from stack.schemas.structs import LS, ST, B, I, S

#: Rubric §3's role categories, in rubric order (prototype ``render.ROLE_NAMES``, v0.3.1).
ROLE_NAMES: tuple[str, ...] = (
    "mentioned",
    "cited_work",
    "discussed",
    "used",
    "evaluated",
    "modified",
    "introduced",
    "analyzed",
    "attested_use",
    "suspected_use",
    "considered",
    "critiqued",
)
#: Rubric §6's ``doc_kind`` menu (the prototype's ``DOC_KINDS``, = model_roles').
DOC_KINDS: tuple[str, ...] = (
    "paper",
    "blogpost",
    "commentary",
    "model_card",
    "system_card",
    "technical_report",
    "eval_report",
    "news",
    "policy",
    "other",
)

ROLES = ST(**{f"{r}_": I for r in ROLE_NAMES})
QUOTE = ST(text_=S, verified_=B)
TECHNIQUE_ROW = ST(slug_=S, roles_=ROLES, quotes_=LS(QUOTE), flags_=LS(S), note=S)
EXTRA_ROW = ST(name_as_written_=S, gloss=S, hint_technique=S, roles_=ROLES, quotes_=LS(QUOTE), flags_=LS(S))
TECHNIQUE_ROLES = ST(
    doc_kind=S,
    techniques=LS(TECHNIQUE_ROW),
    extra_techniques=LS(EXTRA_ROW),
    flags=LS(S),
    repairs=LS(S),
)
