"""``MODEL_ROLES`` payload (A.4, D235 round 5 T-ANN; D591): which vocabulary models a document names
and which roles each plays in it (``annotate.model_roles``: the LLM-free matcher over
``configs/models.yaml``, then one judge call over its menu against the rubric of the pinned prompt,
``workflows/annotate/model_roles/prompts/``; ported from v3 ``tomas/model-roles``).

``ROLES`` holds the rubric's ten categories (§3, in rubric order), each a strength 0-4; a role the
judge omitted (rubric §6 expresses zero by omission) is stored as an explicit ``0``. ``models``:
one row per menu node in menu order (graded or all-zero; v3 ``judge.normalize``), with the
verbatim ``evidence`` quotes and an optional ``note``; ``variant_models``: one row per menu base
with the derivatives the document names; ``extra_models``: materially engaged off-menu models;
``flags``: the rubric's row flags (``matcher_fp:<node>``, ``bib_only:<node>``, ``not_found:<node>``,
``subsumed:<a>:<b>``…); ``repairs``: every post-processing repair the pipeline applied. v3's
``judge``, ``verifier``, ``low_confidence`` and ``vocab_stamp`` are not carried (the evidence row
carries model, prompt name and template digest; the config digest covers rubric and template).

D591 (``annotate.model_mentions`` merged in): ``extra_models[].node_id`` is the vocabulary node the
extra's ``name_as_written`` resolves to (exactly one strong hit), else NULL. ``models[].node_id`` is the
node the row grades and ``models[].matched_node_id`` the menu node the matcher found: the same, unless
the judge refined the row to a more specific node the document determines (``refined_name`` in the
reply, accepted only within the menu node's lineage; ``workflows/annotate/model_roles/judge.py``).
Flags (strings, rubric §6): ``matcher_fp``, ``bib_only``, ``not_found``, ``subsumed`` explain an
all-zero row. Registry version 3 judges every document (an empty or weak-only menu included).

The matcher's own output is the record ``model_regexp_matches`` of the same call
(:data:`MODEL_REGEXP_MATCHES`; registry ``produced_by="model_roles"``, its own family and
``annotations.model_regexp_matches``, a struct column of ``model_roles_evidence``): ``matches`` is
every matcher hit, strong entries sorted by ``node_id`` then weak ones (``{node_id, surfaces,
strength}``; surfaces in document order, duplicates kept, so the list length is the occurrence
count; a node with both kinds of hit has one entry of each); ``ambiguous`` the spans the matcher
could not attribute (``{text, candidates, strength}``, document order; ``strength`` ``strong`` iff
any candidate matched through a strong pattern), both as ex-``MODEL_MENTIONS``. Informative: the
judged answer is ``MODEL_ROLES``.
"""

from __future__ import annotations

from stack.schemas.structs import LS, ST, I, S

#: Rubric §3's role categories, in rubric order (v3 ``stack.types.payloads.ROLE_NAMES``).
ROLE_NAMES: tuple[str, ...] = (
    "mentioned",
    "cited_work",
    "discussed",
    "usage_experience",
    "built_on",
    "instrument",
    "modified",
    "evaluated_behavioral",
    "analyzed_internals",
    "introduced",
)
#: Rubric §6's ``doc_kind`` menu (v3 ``stack.types.payloads.DOC_KINDS``).
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
ROLE_EVIDENCE = ST(roles_=LS(S), quote_=S)
MODEL_ROW = ST(node_id_=S, roles_=ROLES, evidence_=LS(ROLE_EVIDENCE), note=S, matched_node_id=S)
VARIANT_ROW = ST(base_node_id_=S, names_as_written_=LS(S), quote_=S, roles_=ROLES)
EXTRA_ROW = ST(name_as_written_=S, quote_=S, roles_=ROLES, node_id=S)
MATCH = ST(node_id_=S, surfaces_=LS(S), strength_=S)
AMBIGUOUS_SPAN = ST(text_=S, candidates_=LS(S), strength_=S)
MODEL_ROLES = ST(
    doc_kind=S,
    models=LS(MODEL_ROW),
    variant_models=LS(VARIANT_ROW),
    extra_models=LS(EXTRA_ROW),
    flags=LS(S),
    repairs=LS(S),
)
MODEL_REGEXP_MATCHES = ST(matches=LS(MATCH), ambiguous=LS(AMBIGUOUS_SPAN))
