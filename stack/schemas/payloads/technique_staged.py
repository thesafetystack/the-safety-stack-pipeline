"""``TECHNIQUE_STAGED`` payload (techpipe, D604): ``annotate.technique_staged``, the technique-role
annotation on the taxonomy v0.9 in one of four modes (``single``, ``sweep``, ``staged``, ``lexical``;
``workflows/annotate/technique_staged/main.py``).

The first five fields are ``TECHNIQUE_ROLES``' and reuse its row structs, so a reader of technique
rows reads both families alike: ``doc_kind``, ``techniques`` (one row per catalogue slug or group node,
quotes ``verified`` against the stored text), ``extra_techniques`` (off-catalogue techniques),
``flags`` (``no_techniques_found`` only when both row lists are empty, ``menu_gap:<surface>``,
``prepass_incomplete``, ``select_incomplete``, ``partial``), ``repairs``. Then:

* ``mode`` (the pin's mode) and ``taxonomy_version`` (the taxonomy file's ``version``);
* ``candidate_gaps``: safety techniques the catalogue may lack, one per source: the selection step's
  ``off_taxonomy`` proposals (``source = 'selection'``, ``quotes`` = the code-cut snippet of the
  mention, ``verified`` true), every grader extra (``source = 'extra'``, its quotes) and every
  ``menu_gap:`` flag (``source = 'menu_gap'``, no quotes); ``reason`` = the step's reason
  (``off_taxonomy``, ``menu_gap``) or NULL for an extra;
* ``unresolved``: mentions the selection step called ``too_vague`` (a statement that identifies no
  technique), with the code-cut snippet; never counted as slugs;
* ``prepass``: the pre-pass record (empty lists and NULL ``segmenter`` for ``single``):
  ``segmenter`` (the segmenter summary: versions, sizes, truncation, references span, chunks),
  ``mentions`` (P's notes after code post-processing, with S's answer per mention), ``doc_level``
  (S's document-level candidates), ``lexical`` (the matcher's body hits per slug), ``submenu`` (the
  graded candidate slugs, in rendering order), ``rejected`` (the grader's rejected candidates; never
  rows), ``calls`` (one entry per LLM call of the answer: stage, chunk part, model, tokens, response
  digest; costs are on the evidence row, merged) and ``counts`` (name → value, sorted by name).
"""

from __future__ import annotations

from stack.schemas.payloads.technique_roles import EXTRA_ROW, QUOTE, TECHNIQUE_ROW
from stack.schemas.structs import LS, ST, B, I, S

#: The modes (plan pin ``mode``).
MODES: tuple[str, ...] = ("single", "sweep", "staged", "lexical")
#: ``candidate_gaps[].source``.
GAP_SOURCES: tuple[str, ...] = ("selection", "extra", "menu_gap")

CANDIDATE_GAP = ST(name_=S, gloss=S, quotes_=LS(QUOTE), reason=S, source_=S)
UNRESOLVED = ST(surface_=S, gloss=S, snippet=S)
CANDIDATE = ST(slug_=S, confidence_=S)
MENTION = ST(
    id_=S,
    chunk_=S,  # the P call that produced it: "1", "2", or a half "2a" / "2b" after a split
    paras_=LS(I),
    surface_=S,
    gloss=S,
    stance_hint_=S,
    implicit_=B,
    candidates_=LS(CANDIDATE),
    none_reason=S,  # S's reason when it mapped the mention to nothing; NULL when it gave candidates or did not answer
)
DOC_LEVEL = ST(slug_=S, confidence_=S, why=S)
LEXICAL_HIT = ST(slug_=S, hits_body_=I)
REJECTED = ST(slug_=S, reason_=S)
CALL = ST(stage_=S, part=S, model_=S, tokens_in=I, tokens_out=I, response_digest=S)
COUNT = ST(name_=S, value_=I)
SEG_CHUNK = ST(index_=I, total_=I, first_pid_=S, last_pid_=S, n_paragraphs_=I, overlap=I, chars_=I)
SEGMENTER = ST(
    segmenter_version_=S,
    lexical_version_=S,
    doc_chars_=I,
    read_chars_=I,
    truncated_=B,
    n_paragraphs_=I,
    body_chars_=I,
    references_method_=S,
    reference_paragraphs_=I,
    reference_chars_=I,
    references_first_pid=S,
    chunks_=LS(SEG_CHUNK),
)
PREPASS = ST(
    segmenter=SEGMENTER,
    mentions_=LS(MENTION),
    doc_level_=LS(DOC_LEVEL),
    lexical_=LS(LEXICAL_HIT),
    submenu_=LS(S),
    rejected_=LS(REJECTED),
    calls_=LS(CALL),
    counts_=LS(COUNT),
)
TECHNIQUE_STAGED = ST(
    doc_kind=S,
    techniques=LS(TECHNIQUE_ROW),
    extra_techniques=LS(EXTRA_ROW),
    flags=LS(S),
    repairs=LS(S),
    mode=S,
    taxonomy_version=S,
    candidate_gaps=LS(CANDIDATE_GAP),
    unresolved=LS(UNRESOLVED),
    prepass=PREPASS,
)
