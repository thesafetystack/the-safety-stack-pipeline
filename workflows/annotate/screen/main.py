"""``annotate.screen`` — the relevance screen on the annotate framework (§3.1, A.4 ``SCREEN``).

One include/exclude decision per document of the build's ``fold.corpus`` file, on its current
text (``basis_kind = 'text'``, ``content.current_text_sha256``), with a co-located template
the plan pin's ``name`` selects (below) and the ``screen_verdict`` output schema (plus the
``quotes`` channel).
The framework (:mod:`stack.annotate`) does scope, prior-evidence carry, attempts, budget, the
``screen`` / ``screen_evidence`` files and ``select``; this module is the per-document ``ask``
and the prompt pin. ADAPT of v3's ``workflows/annotate/screen/main.py`` (9312c8c:
``_screen_one``, ``_adjudicate``, ``_anchor_quotes``, ``_output_schema``, ``_render_menu``,
``_truncate``); v3's DB run loop, receipts, batching and thread pool went into the framework.

**Plan pin** ``prompts.annotate.screen`` (part of the config digest, so of the work key):
``name`` (the ``prompt_name`` written, and the template both tiers send, :func:`template`:
``screen-v7`` or no name → ``screen.md``, production since v3; any other name →
``workflows/annotate/screen/<name>.md``, e.g. ``screen-v8``; a name with no such file or with a
path separator is refused before any call, as triage's ``workflows/triage/<name>.md``, D157;
the name also selects the output schema, below),
``model`` (tier 1; default ``llm.yaml`` role ``screen``'s model), ``adjudicate_model`` (tier 2;
default role ``screen_adjudicate``'s model), ``tier2`` (default true; false = tier 1 only, v3's
``STACK_SCREEN_TIER2=0``), ``tier1_chars`` / ``tier2_chars`` (the screening-text limits, below), and
the framework's optional ``template_digest`` (checked against the selected template's digests).
Other keys are refused (a typo must not pass silently). Each tier
keeps its role's fallbacks and ``max_tokens``, except tier 1 for the names in
the names whose :data:`SPECS` row sets it (below). A changed prompt is a new file under a new name: a
template edited under the same name changes the template digests (so the work keys), not the
``prompt_name``. :data:`TEMPLATE` is ``screen.md``'s text, kept for importers; ``ask`` and
``prompt`` never read it (the name alone selects; assigning the attribute changes nothing, so the
eval harness's per-process swap for ``--prompt <name>``, D175 (E), is now redundant and harmless:
the name selects the same file).

**Output schema by name** (D194 (E)). The pin name also selects the schema both tiers send and
validate against (:func:`schema_for`): ``screen-v9`` (question mode ``scope``: the
template that asks the Shallow Review scope fields of ``evals/rubrics/screen-v2.md``) gets :data:`SCHEMA_SR` = :data:`SCHEMA` plus ``scope`` (``core`` / ``sr_only`` / ``out``,
required), ``sr_category`` (the SR 2025 section or ``none``, nullable) and ``sr_agenda`` (string
of at most 80 characters, nullable); every other name gets :data:`SCHEMA`, so ``screen-v7`` /
``screen-v8`` and every existing work key keep their template digests byte for byte. **Known
gap:** the ``SCREEN`` payload struct (``stack/schemas/payloads/screen.py``) has no scope columns
and :func:`_payload` does not read them, so production with a ``screen-v9`` pin pays for the
scope fields and drops them at the payload; storing them is a shared-schema change (track C).
The eval harness reads them from the parsed answers (``stack/evals/harness.py``).

**Two relevance questions** (D202 (E)). ``screen-v10``, ``screen-stack-v1`` and ``screen-sr-v1``
share a byte-identical preamble defining question S (Stack: defence-in-depth technical safety)
and question R (Shallow Review of Technical AI Safety scope, with its category and agenda) and
differ in the task: v10 answers both in one call, the other two one question each (so a
two-call design shares the cached preamble). Schemas (:func:`schema_for`, all strict):
``screen-stack-v1`` → :data:`SCHEMA_STACK` = :data:`SCHEMA` plus ``confidence`` (0-1,
required); ``screen-sr-v1`` → :data:`SCHEMA_SR_QUESTION` = :data:`SCHEMA` plus ``confidence``,
``category`` (the SR section or ``none``; required and not nullable: claude-haiku-5.5 answered
HTTP 400 on the first attempt of every call with screen-v9's nullable-enum schema, cause
unverified) and ``agenda`` (≤ 80 characters, nullable); ``screen-v10`` → :data:`SCHEMA_DUAL` =
``{verdict, rationale, buckets, off_topic_reason, stack: {verdict, confidence, quotes}, sr:
{verdict, confidence, quotes, category, agenda}}`` (top-level ``verdict`` = the more inclusive
answer; no top-level ``quotes`` or ``grounding_idx``). Tier 1 sends ``max_tokens`` from
its :data:`SPECS` row (1,024 for ``screen-v9`` and the two single-question names, 1,536 for
``screen-v10``; the ``screen`` role's 512 truncated luna, whose reasoning tokens count: 3 of 40
``screen-v9`` answers were parse errors at 512), and :func:`prompt` hashes what is sent (so the
``screen-v9`` tier-1 digests moved with it; its template and schema did not); tier 2 keeps its
role's budget. Payload: for ``screen-v10`` ``quotes`` = the Stack quotes then the SR quotes,
exact repeats dropped (:func:`answer_quotes`), anchored as below; for the single-question names
``confidence`` = the model's.

**Annotation records** (track S, S1). The per-question answers are stored as annotation records
(registry ``produced_by: screen``; :attr:`Spec.records`, :func:`records`): ``screen-v10``
produces ``stack_relevance`` (``RELEVANCE``) and ``sr_relevance`` (``SR_RELEVANCE``) beside the
``screen`` record, which stays the call-level summary (overall verdict = the more inclusive,
buckets, rationale, merged quotes, tiers). Per tier, each record gets the question's verdict and
confidence, the question's own quotes anchored separately against the whole text (voided quotes
are dropped; ``quotes_voided`` still counts the merged list only), SR's ``category`` and ``agenda``,
``prompt_name`` = the pin name, ``model`` = the model that answered that tier, ``basis_role``,
``adjudication_tier`` and ``tier1_verdict`` = tier 1's verdict on that question. Tier escalation
stays on the top-level verdict (D202). The single-question names (v7, v8, v9, ``screen-stack-v1``,
``screen-sr-v1``) produce no records.

**Text usability** (track S, S3). ``screen-v11`` is ``screen-v10`` plus question U (is the screening
text a usable rendering of the document's body: ``usable`` / ``degraded`` / ``unusable`` and the
issue codes of :data:`stack.schemas.payloads.usability.USABILITY_ISSUES`); its schema
:data:`SCHEMA_DUAL_USABILITY` is :data:`SCHEMA_DUAL` plus ``usability: {verdict, issues, note}``
(enums not nullable, ``note`` a nullable string of at most 200 characters), tier 1 sends 1,536
``max_tokens``. It produces ``stack_relevance``, ``sr_relevance`` and ``text_usability``
(``USABILITY``: the answer's ``verdict``, ``issues``, ``note`` and ``prompt_name``, ``model``,
``basis_role``, ``adjudication_tier``; no tier-1 field). Question U never feeds the verdict or the
escalation; tier 2 sends the same template and its answer replaces every record, as for v10.

**Screening-text limits** (track S, S2; :func:`input_limits`). Pin keys ``tier1_chars`` and
``tier2_chars``, each an int (the first N characters, cut by :func:`_truncate` with its
``…[truncated]`` marker: v3's tier-1 rendering), ``[head, tail]`` (:func:`excerpt`: the text whole
when it fits, else the first ``head`` and the last ``tail`` characters around a line
``[… N characters omitted by the screener …]``, both cut at whitespace near the boundary, under a
first line ``Document length: L characters; shown: …``), or null (the whole text). Absent keys take
the name's defaults, :attr:`Spec.limits`: ``screen-v11`` → tier 1 ``[12000, 2000]`` (the S2 eval
found accuracy flat from 8,000 to 32,000 characters, so the cheaper limit; RUNLOG (S) S2/S3), tier 2
the whole text (null: tier 2 runs on few documents and decides the hard cases with depth);
``screen-v12`` → tier 1 ``[12000, 2000]``, tier 2 ``[48000, 8000]`` (round 5: with ``gpt-6-luna`` as
tier 2 the cap bounds a call at about $0.003 and changed no measured accuracy; RUNLOG (SC)); every
other name → :data:`LEGACY_LIMITS` (tier 1 the first :data:`_MAX_DOC_CHARS`, tier 2 the whole
text), so their user messages are byte-identical to before the keys existed and, the keys being
absent from their pins, their config digests and work keys do not move. A name's defaults are
part of the name, like its template and schema: changing them is a new name (the config digest
hashes the pin as written, not the defaults). Tier 2 with a limit other than null gets
:data:`_TIER2_PREAMBLE_LIMITED` (it says the text may be the excerpt the first pass saw) instead of
v3's "entire stored snapshot" preamble. Quotes are anchored against the whole text either way.

**Document type** (track T-SCREEN, round 5). ``screen-v12`` is ``screen-v11`` plus question D
(``doc_class: {doc_type, self_published, primary_lang}``, ``doc_type`` from ``spec/`` doc_class's
enum, :data:`SCHEMA_V12`) and produces the ``doc_class`` record (registry ``produced_by: screen``):
``doc_type`` is the channel's where the channel decides (:func:`channel_doc_type`:
``metadata.doc_type`` in :data:`CHANNEL_DOC_TYPES`, e.g. a forum post, a tweet, a lab blog page),
else the model's, except that a ``paper`` / ``preprint`` answer takes the metadata's paper-vs-preprint
where the collector's typed metadata settles it (:func:`metadata_paper_type`: OpenAlex ``type``, S2
``publicationTypes``, arXiv; the model decides that distinction only when the metadata is silent);
``self_published`` and ``primary_lang`` are the model's. Its menus are written
into ``screen-v12.md`` (:attr:`Spec.inline_menus`; no ``{{ref:enums}}`` fill, ``ask`` sends
no refs), so the template digest, and with it the config digest and the work keys, covers them
(review R5-2: a ref fill is outside the digest; v7-v11 keep that gap). ``SCREEN.confidence`` for
v12 is the confidence of the question(s) that gave the top-level verdict, the larger when both did
(:attr:`Spec.confidence` ``top``); v10/v11 store NULL there (their confidences are only in the
records).

**Registered names, v13 and the record lines** (D656). Every template name has one row in
:data:`SPECS` (:class:`Spec`: schema, question mode, tier-1 ``max_tokens``, records, input limits, which
confidence ``SCREEN`` stores, inline menus, record lines), read through :func:`spec` by every call site; a new
prompt version is one row there plus its file. :func:`spec` refuses a name whose file exists but which has no row,
before any call (an unlisted name used to fall back to v7's schema, the Stack question only, no records and v3's
limits: the review's first eval runs of two v13 drafts answered question S only for that reason).
``screen-v13`` is ``screen-v12`` plus the narrow-settings paragraph and a note on the record lines, and
``screen-v13-p4`` is v13 plus the technique-provenance clause for question S only (D657; no plan pins it until an
independent evaluation promotes it); both rows are v12's with ``record_lines`` on. With ``record_lines`` both tiers'
user messages carry,
after the title, ``Published: <year>`` of ``metadata.published_at`` and ``Source: <channels>`` of
``identity.channels`` (:func:`record_lines`, each left out when the row lacks it); every other name sends
the message it sent before byte for byte. The user message is in no digest that decides re-asking (the
work key hashes the text basis and the config digest), so the lines came with a new name.

**Tier-2 allowance** (review R5-1, round 5; :class:`Tier2Allowance`). Stage-line ``options``
``tier2_max_usd`` (USD per run), ``tier2_budget_share`` (share of ``budget.max_usd``) and
``tier2_max_share`` (share of the run's settled screen spend) and ``screen_usd_per_1k`` (the run's
screen spend, tier 1 + tier 2, per 1,000 documents tier 1 answered so far), any combination, the
tightest applying; ``screen_usd_per_1k`` admits a run's tier-2 call while nothing of tier 2 is spent
or in flight (D706). A tier-1 ``borderline`` whose tier-2 call does not fit returns None from ``ask``: the
framework records that document ``not_attempted[budget]`` (no evidence row, no attempt counted)
without tripping the run's budget scope, so tier 1 goes on for the other documents, and the next run
asks it again with tier 1 served from the response cache; ``scope_complete`` is false meanwhile, and
the run counts it ``not_attempted.deferred`` (build reason ``tier2_allowance``, D706).
Without these options tier 2 is bounded only by the stage budget, as before.
In batch mode (``llm_mode: batch``, :mod:`stack.annotate.batchmode`) the allowance is per run and
per pass (keyed by ``client.batch_pass``): each collect pass and the replay pass start from zero
and see the same documents in the same order. In batch mode the allowance bounds tier 2's
*estimated* spend at the batch price: a tier-2 call's estimate is priced × ``BATCH_PRICE_FACTOR``
and every tier-2 call settles at its estimate in every pass, collect and replay alike, whatever it
actually cost, so every pass admits the same documents (no pass is spent chasing room freed by
cheaper actuals, and nothing is admitted only in the replay, which would go sync at full price,
``batch.sync_fallback``). The run's budget is enforced by the reservations the
batch driver makes at submit time, at the batch price.

**Two tiers** (v3, 2026-09-02). Tier 1 reads the title and the first :data:`_MAX_DOC_CHARS`
characters. A ``borderline`` from tier 1 is re-asked by tier 2 with the whole stored text and
tier 1's rationale (ledger attempt kind ``adjudicate``); tier 2's answer becomes the second
evidence row of the invocation (``supersedes`` tier 1's) and is what ``select`` keeps; it may
itself say ``borderline``. A tier-2 failure that is not a budget refusal or a fatal error
(egress refusal, too large, schema-invalid after retries, transport failures past the client's
per-call cap) keeps tier 1's ``borderline``: the failure is its own evidence row and
``select`` keeps the newest *decided* row (:func:`stack.annotate.evidence.selected`). A budget
refusal on tier 2 makes the whole document ``not_attempted[budget]`` (tier 1's answer waits in
the response cache, so the rerun pays only tier 2); a fatal error halts the run as on tier 1.

**Payload** (``SCREEN``): ``verdict``, ``buckets``, ``off_topic_reason`` as the model gave
them; ``reasoning`` = the model's ``rationale``; ``basis_role`` = ``content.current_role``;
``confidence`` NULL except per :attr:`Spec.confidence` (``answer``: the model's); ``adjudication_tier`` 1 or 2; ``tier1_verdict`` /
``tier1_model`` = tier 1's (on both tiers' rows). ``quotes`` = the model's quotes anchored
against the whole text (:class:`stack.anchor.Anchorer`, ``policies.yaml`` threshold): each kept
quote is the anchored span of the text with its ``offset``; a quote that does not anchor is
dropped (v3 voided it; counted ``quotes_voided``). The model's ``grounding_idx`` is not stored
(``SCREEN`` has no field for it). Counts: ``tier2`` (adjudications asked), ``tier2_failed``,
``quotes_voided``.
"""

from __future__ import annotations

import json
import math
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from stack.anchor import Anchorer
from stack.annotate import (
    OPTIONS as ANNOTATE_OPTIONS,
)
from stack.annotate import (
    PURE_SUBSTAGES,
    SUBSTAGES,
    Answer,
    AskContext,
    AskItem,
    PromptPin,
    Workflow,
    run_annotate,
)
from stack.annotate.provenance import record_lines
from stack.core.hashes import template_hash
from stack.llm import (
    RESERVE_OVERHEAD_TOKENS,
    RESERVE_SAFETY_FACTOR,
    BudgetExceeded,
    FatalLlmError,
    LicenseClass,
    LlmError,
    LlmResult,
    estimate_tokens,
    strict_schema,
)
from stack.llm.batch import BATCH_PRICE_FACTOR
from stack.stage_runner import StageContext, StageError, StageResult

if TYPE_CHECKING:
    from stack.config import LlmConfig, RoleConfig, StackConfig

HERE = Path(__file__).resolve().parent
SPEC_ROOT = HERE.parents[2] / "spec"

# DECISION (v3, text truncation): the screen decision is a title+abstract-level relevance call; v1 used
# ~6000 chars of abstract for screening, so we cap a little above that to include a lead section.
_MAX_DOC_CHARS = 8_000

ROLE = "screen"
ADJUDICATE_ROLE = "screen_adjudicate"
SCHEMA_REL = "schemas/payloads/screen_verdict.schema.json"
MENUS = ["relevance_verdict", "triage_bucket"]
PIN_KEYS = frozenset({"name", "model", "adjudicate_model", "tier2", "template_digest", "tier1_chars",
                      "tier2_chars"})
_TIER2_PREAMBLE = (
    "A cheaper first-pass screener could not decide and called this document `borderline`, "
    "giving this reason: {rationale}\n"
    "You are the second reader. You receive the entire stored snapshot, which may contain "
    "additional text beyond the first-pass excerpt, but may itself be partial. "
    "Judge the supplied text on its merits — the first pass is "
    "context, not a prior to defer to — and answer `relevant` or `irrelevant` unless the text "
    "genuinely cannot settle it.\n\n"
)
#: Tier 2's preamble when its screening text has a limit (:attr:`Spec.limits`; module docstring).
_TIER2_PREAMBLE_LIMITED = (
    "A cheaper first-pass screener could not decide and called this document `borderline`, "
    "giving this reason: {rationale}\n"
    "You are the second reader. You receive the stored text within the screener's length limit "
    "(possibly the same excerpt the first pass saw; the stored text may itself be partial). "
    "Judge the supplied text on its merits — the first pass is "
    "context, not a prior to defer to — and answer `relevant` or `irrelevant` unless the text "
    "genuinely cannot settle it.\n\n"
)


def _output_schema(rel: str) -> dict[str, Any]:
    """The registered ``screen_verdict`` payload schema (loaded from repo ``spec/``), extended into
    the LLM-output form: an optional ``quotes: [string]`` array is added (the grounding channel the
    stored schema omits) and ``additionalProperties`` is relaxed so that quotes validate. The
    grounding_idx ``$ref`` is inlined so the schema is self-contained. Not what is sent:
    :data:`SCHEMA` is its strict form; this optional-key form stays for v3's
    ``tools/eval_screen.py``, which validates answers with the null keys dropped."""
    schema = cast("dict[str, Any]", json.loads((SPEC_ROOT / rel).read_text()))
    props = dict(schema.get("properties", {}))
    if isinstance(props.get("grounding_idx"), dict):
        props["grounding_idx"] = {"type": "array", "items": {"type": "integer", "minimum": 0}}
    props["quotes"] = {"type": "array", "items": {"type": "string"}}
    schema["properties"] = props
    schema.pop("additionalProperties", None)
    return schema


#: The pin name of ``screen.md`` (production's template; dev-v2/std-v2 pin it).
DEFAULT_NAME = "screen-v7"
TEMPLATE = (HERE / "screen.md").read_text("utf-8")
#: The LLM-output schema both tiers send and validate against: :func:`_output_schema` made
#: strict-mode shaped (:func:`stack.llm.strict_schema`, D185 (C)): closed, every key required, the
#: optional ones (``buckets``, ``off_topic_reason``, ``grounding_idx``, ``quotes``) nullable, so the
#: model answers ``null`` where the templates say "optional" (:func:`_payload` reads ``null`` as
#: absent); ``maxLength`` holds client-side (the wire form drops it). Part of the template digest,
#: so changing it changed the screen work keys.
SCHEMA = strict_schema(_output_schema(SCHEMA_REL))
SR_CATEGORIES = ("labs", "black_box", "white_box", "safety_by_construction", "make_ai_solve_it", "theory",
                 "multi_agent_first", "evals", "none")


def _output_schema_sr(rel: str) -> dict[str, Any]:
    """:func:`_output_schema` plus the SR scope fields: ``scope`` required, ``sr_category`` and
    ``sr_agenda`` optional (nullable once strict)."""
    schema = _output_schema(rel)
    schema["properties"] = {
        **schema["properties"],
        "scope": {"type": "string", "enum": ["core", "sr_only", "out"]},
        "sr_category": {"type": "string", "enum": list(SR_CATEGORIES)},
        "sr_agenda": {"type": "string", "maxLength": 80},
    }
    schema["required"] = [*schema.get("required", []), "scope"]
    return schema


#: :data:`SCHEMA` plus the SR scope fields, strict-mode shaped: ``screen-v9`` (question mode ``scope``).
SCHEMA_SR = strict_schema(_output_schema_sr(SCHEMA_REL))
_CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
#: The SR category of the per-question answers: required, never nullable (module docstring).
_CATEGORY = {"type": "string", "enum": list(SR_CATEGORIES)}
_AGENDA = {"type": "string", "maxLength": 80}


def _output_schema_question(rel: str, question: str) -> dict[str, Any]:
    """:func:`_output_schema` plus a required ``confidence`` (``stack``, ``sr``) and, for ``sr``,
    a required ``category`` and an optional ``agenda``: one question answered at the top level."""
    schema = _output_schema(rel)
    extra: dict[str, Any] = {"confidence": _CONFIDENCE}
    required = ["confidence"]
    if question == "sr":
        extra.update(category=_CATEGORY, agenda=_AGENDA)
        required.append("category")
    schema["properties"] = {**schema["properties"], **extra}
    schema["required"] = [*schema.get("required", []), *required]
    return schema


def _output_schema_dual(rel: str) -> dict[str, Any]:
    """The two-question answer: the verdict fields of :func:`_output_schema` (no ``quotes``, no
    ``grounding_idx``) and one object per question, ``stack: {verdict, confidence, quotes}`` and
    ``sr: {verdict, confidence, quotes, category, agenda}`` (``agenda`` optional)."""
    base = _output_schema(rel)
    props = {k: v for k, v in base["properties"].items() if k not in ("quotes", "grounding_idx")}
    answer = {"verdict": props["verdict"], "confidence": _CONFIDENCE,
              "quotes": {"type": "array", "items": {"type": "string"}}}
    props["stack"] = {"type": "object", "properties": dict(answer), "required": list(answer)}
    props["sr"] = {"type": "object", "properties": {**answer, "category": _CATEGORY, "agenda": _AGENDA},
                   "required": [*answer, "category"]}
    return {**base, "properties": props, "required": [*base.get("required", []), "stack", "sr"]}


def _output_schema_dual_usability(rel: str) -> dict[str, Any]:
    """:func:`_output_schema_dual` plus ``usability: {verdict, issues, note}`` (question U;
    ``note`` optional, so nullable once strict)."""
    from stack.schemas.payloads.usability import USABILITY_ISSUES, USABILITY_VERDICTS

    base = _output_schema_dual(rel)
    usability = {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": list(USABILITY_VERDICTS)},
        "issues": {"type": "array", "items": {"type": "string", "enum": list(USABILITY_ISSUES)}},
        "note": {"type": "string", "maxLength": 200}}, "required": ["verdict", "issues"]}
    return {**base, "properties": {**base["properties"], "usability": usability},
            "required": [*base["required"], "usability"]}


DOC_CLASS_SCHEMA_REL = "schemas/payloads/doc_class.schema.json"
#: The ``doc_type`` values the model may answer: the stored payload schema's enum (``spec/``,
#: the ``enums.yaml`` ``doc_type`` domain; read once, so it is part of the template digest).
DOC_TYPES: tuple[str, ...] = tuple(json.loads((SPEC_ROOT / DOC_CLASS_SCHEMA_REL).read_text())
                                   ["properties"]["doc_type"]["enum"])


def _output_schema_v12(rel: str) -> dict[str, Any]:
    """:func:`_output_schema_dual_usability` plus ``doc_class: {doc_type, self_published,
    primary_lang}`` (question D; ``primary_lang`` optional, so nullable once strict)."""
    base = _output_schema_dual_usability(rel)
    doc_class = {"type": "object", "properties": {
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "self_published": {"type": "boolean"},
        "primary_lang": {"type": "string", "maxLength": 16}}, "required": ["doc_type", "self_published"]}
    return {**base, "properties": {**base["properties"], "doc_class": doc_class},
            "required": [*base["required"], "doc_class"]}


#: :data:`SCHEMA` plus a required ``confidence``: ``screen-stack-v1`` (question S alone).
SCHEMA_STACK = strict_schema(_output_schema_question(SCHEMA_REL, "stack"))
#: :data:`SCHEMA` plus ``confidence``, ``category`` (required) and ``agenda``: ``screen-sr-v1``.
SCHEMA_SR_QUESTION = strict_schema(_output_schema_question(SCHEMA_REL, "sr"))
#: Both questions in one answer: ``screen-v10``.
SCHEMA_DUAL = strict_schema(_output_schema_dual(SCHEMA_REL))
#: Both questions and the text usability: ``screen-v11``.
SCHEMA_DUAL_USABILITY = strict_schema(_output_schema_dual_usability(SCHEMA_REL))
#: Both questions, the text usability and the document type: ``screen-v12``.
SCHEMA_V12 = strict_schema(_output_schema_v12(SCHEMA_REL))
#: The menus ``screen-v12`` carries inline (a test checks them against ``configs/enums.yaml``).
INLINE_MENUS = ["relevance_verdict", "triage_bucket", "doc_type"]
_TEMPLATES: dict[str, str] = {DEFAULT_NAME: TEMPLATE}
#: A screening-text limit (module docstring): the first N characters, ``(head, tail)``, or None = whole.
Limit = int | tuple[int, int] | None
#: v3's limits (tier 1 the first 8,000 characters, tier 2 the whole text): every name up to ``screen-v10``.
LEGACY_LIMITS: tuple[Limit, Limit] = (_MAX_DOC_CHARS, None)


class Spec(NamedTuple):
    """What a template name selects (module docstring "Registered names"): one row of :data:`SPECS`."""

    #: The output schema both tiers send and validate against.
    schema: dict[str, Any]
    #: Which question(s): ``stack`` / ``sr`` (one, at the top level), ``dual`` (both, nested), ``scope`` (v9's SR
    #: scope fields beside the Stack verdict).
    mode: str
    #: Tier 1's ``max_tokens``; None = the ``screen`` role's.
    tier1_max_tokens: int | None
    #: The annotation records produced beside ``screen`` (:func:`records`).
    records: tuple[str, ...]
    #: The default ``(tier1_chars, tier2_chars)``; the pin keys override (:func:`input_limits`).
    limits: tuple[Limit, Limit]
    #: ``SCREEN.confidence``: None (NULL), ``answer`` (the single answer's ``confidence``) or ``top`` (the
    #: confidence of the question(s) that gave the top-level verdict, the larger when both did; :func:`_top_confidence`).
    confidence: str | None = None
    #: The menus are written into the template file: ``ask`` sends no ``{{ref:enums}}`` fill, so the template digest
    #: covers them (review R5-2).
    inline_menus: bool = False
    #: Both tiers' user messages carry :func:`record_lines` after the title.
    record_lines: bool = False


_V12_RECORDS = ("stack_relevance", "sr_relevance", "text_usability", "doc_class")
#: Every template name and what it selects (module docstring "Registered names"); ``screen-v7`` is ``screen.md``,
#: every other name its own file. A new prompt version is one row here (and its file).
SPECS: dict[str, Spec] = {
    DEFAULT_NAME: Spec(SCHEMA, "stack", None, (), LEGACY_LIMITS),
    "screen-v8": Spec(SCHEMA, "stack", None, (), LEGACY_LIMITS),
    "screen-v9": Spec(SCHEMA_SR, "scope", 1024, (), LEGACY_LIMITS),
    "screen-stack-v1": Spec(SCHEMA_STACK, "stack", 1024, (), LEGACY_LIMITS, confidence="answer"),
    "screen-sr-v1": Spec(SCHEMA_SR_QUESTION, "sr", 1024, (), LEGACY_LIMITS, confidence="answer"),
    "screen-v10": Spec(SCHEMA_DUAL, "dual", 1536, ("stack_relevance", "sr_relevance"), LEGACY_LIMITS),
    "screen-v11": Spec(SCHEMA_DUAL_USABILITY, "dual", 1536, ("stack_relevance", "sr_relevance", "text_usability"),
                       ((12_000, 2_000), None)),
    "screen-v12": Spec(SCHEMA_V12, "dual", 1536, _V12_RECORDS, ((12_000, 2_000), (48_000, 8_000)),
                       confidence="top", inline_menus=True),
    "screen-v13": Spec(SCHEMA_V12, "dual", 1536, _V12_RECORDS, ((12_000, 2_000), (48_000, 8_000)),
                       confidence="top", inline_menus=True, record_lines=True),
    "screen-v13-p4": Spec(SCHEMA_V12, "dual", 1536, _V12_RECORDS, ((12_000, 2_000), (48_000, 8_000)),
                          confidence="top", inline_menus=True, record_lines=True),
}
#: The collector's ``metadata.doc_type`` values that decide ``doc_class.doc_type`` without the model
#: (the channel fixes the genre: a forum post, a tweet, a lab blog page, a lab's cards), mapped to the
#: ``doc_type`` value stored (``social_post`` is x-api's name for a tweet). Every other value (``paper``,
#: ``preprint``, ``journal``, ``conference``, ``webpage``, ``other``, NULL, …) leaves the model's answer.
CHANNEL_DOC_TYPES: dict[str, str] = {
    "forum_post": "forum_post", "tweet": "tweet", "social_post": "tweet", "lab_blog": "lab_blog",
    "system_card": "system_card", "model_card": "model_card", "framework_policy": "framework_policy"}


#: The collector's ``metadata.doc_type`` values that settle **paper vs preprint** from typed metadata
#: (round 5 follow-up): OpenAlex ``type`` (``preprint`` → preprint; ``article`` / ``review`` / ``conference-paper`` /
#: ``book-chapter`` → paper), S2 ``publicationTypes`` (``Preprint`` or an arXiv id → preprint; journal,
#: conference, review, book types → paper), the arXiv channel (preprint); ``journal`` / ``conference``
#: are venue-type values some rows carry. ``other`` / NULL are silent.
METADATA_PAPER_TYPES: dict[str, str] = {"paper": "paper", "journal": "paper", "conference": "paper",
                                        "preprint": "preprint"}


def metadata_paper_type(row: Mapping[str, Any]) -> str | None:
    """``paper`` / ``preprint`` when the row's metadata settles that distinction
    (:data:`METADATA_PAPER_TYPES`), else None (the model's answer stands)."""
    dt = (row.get("metadata") or {}).get("doc_type")
    return METADATA_PAPER_TYPES.get(dt) if isinstance(dt, str) else None


def channel_doc_type(row: Mapping[str, Any]) -> str | None:
    """The ``doc_type`` the source channel decides for corpus row ``row`` (:data:`CHANNEL_DOC_TYPES`
    over ``metadata.doc_type``), or None when the model decides."""
    dt = (row.get("metadata") or {}).get("doc_type")
    return CHANNEL_DOC_TYPES.get(dt) if isinstance(dt, str) else None


def spec(name: object) -> Spec:
    """The :data:`SPECS` row of plan-pin ``name`` (``None`` → :data:`DEFAULT_NAME`). A non-string, a path
    separator, a name with no template file, or a template whose name has no row refuses (:class:`StageError`),
    before any call: an unlisted name must not get some other name's schema or records silently."""
    key = DEFAULT_NAME if name is None else name
    if not isinstance(key, str) or not key or "/" in key or "\\" in key or key.startswith("."):
        raise StageError(f"annotate.screen: plan pin name {name!r} is not a template name "
                         f"(workflows/annotate/screen/<name>.md)")
    got = SPECS.get(key)
    if got is None:
        path = HERE / f"{key}.md"
        if not path.is_file():
            raise StageError(f"annotate.screen: plan pin name {key!r}: no template {path}")
        raise StageError(f"annotate.screen: plan pin name {key!r} has a template but is not registered: add its "
                         f"row to SPECS in workflows/annotate/screen/main.py")
    return got


def records(pin: Mapping[str, Any]) -> tuple[str, ...]:
    """The annotation records plan pin ``pin`` produces (its :data:`SPECS` row)."""
    return spec(pin.get("name")).records


def schema_for(name: object) -> dict[str, Any]:
    """The output schema plan-pin ``name`` selects (its :data:`SPECS` row)."""
    return spec(name).schema


def question_mode(name: object) -> str:
    """The question mode of plan-pin ``name`` (its :data:`SPECS` row; ``stack`` for v7, v8 and None)."""
    return spec(name).mode


def tier1_max_tokens(name: object) -> int | None:
    """Tier 1's ``max_tokens`` for plan-pin ``name``; None = the ``screen`` role's."""
    return spec(name).tier1_max_tokens


def template(name: object) -> str:
    """The template plan-pin ``name`` selects (module docstring): ``screen-v7`` / None →
    ``screen.md``; another name → ``<name>.md`` beside this file, read once per process (so
    ``prompt``'s digests and ``ask``'s calls send the same text). A name :func:`spec` refuses, or a
    missing file, refuses (:class:`StageError`)."""
    spec(name)
    key = DEFAULT_NAME if name is None else cast(str, name)
    if key not in _TEMPLATES:
        path = HERE / f"{key}.md"
        if not path.is_file():
            raise StageError(f"annotate.screen: plan pin name {key!r}: no template {path}")
        _TEMPLATES[key] = path.read_text("utf-8")
    return _TEMPLATES[key]


def _render_menu(config: StackConfig, vocabs: list[str]) -> str:
    lines: list[str] = []
    for name in vocabs:
        lines.append(f"{name}:")
        for slug, gloss in config.enums.enums[name].values.items():
            lines.append(f"- {slug}: {gloss}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _truncate(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + "\n…[truncated]"


class Limits(NamedTuple):
    """The screening-text limits of a plan pin (:func:`input_limits`)."""

    tier1: Limit
    tier2: Limit


def _limit(key: str, value: object) -> Limit:
    """One pin limit, checked: an int >= 1, ``[head >= 1, tail >= 0]`` (a tuple), or None."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            and value[0] >= 1 and value[1] >= 0):
        return (int(value[0]), int(value[1]))
    raise StageError(f"annotate.screen: plan pin {key} must be an int >= 1, [head >= 1, tail >= 0] or null; "
                     f"got {value!r}")


def input_limits(pin: Mapping[str, Any]) -> Limits:
    """The limits plan pin ``pin`` sends (module docstring): its ``tier1_chars`` / ``tier2_chars``,
    else its name's (:data:`SPECS`). A bad value refuses."""
    t1, t2 = spec(pin.get("name")).limits
    return Limits(_limit("tier1_chars", pin["tier1_chars"]) if "tier1_chars" in pin else t1,
                  _limit("tier2_chars", pin["tier2_chars"]) if "tier2_chars" in pin else t2)


#: How far from a head / tail boundary :func:`excerpt` looks for whitespace to cut at.
_CUT_WINDOW = 200


def excerpt(text: str, head: int, tail: int) -> tuple[str, int]:
    """``(rendered, chars shown)`` of ``text`` under a ``(head, tail)`` limit (module docstring):
    a first line ``Document length: L characters; shown: all.`` and the text, when it fits in
    ``head + tail``; else ``…; shown: the first H and the last T.``, the first ``H <= head``
    characters (cut at the last whitespace within :data:`_CUT_WINDOW` before ``head``, unless the
    character at ``head`` is whitespace), a line ``[… N characters omitted by the screener …]`` and
    the last ``T <= tail`` characters (cut after the first whitespace within the window from
    ``len - tail``, unless the character before it is whitespace); no whitespace there → a hard cut.
    ``N = L - H - T`` (the whitespace at the cuts counts as omitted)."""
    n = len(text)
    if n <= head + tail:
        return f"Document length: {n} characters; shown: all.\n\n{text}", n
    cut = head  # already at a boundary when text[head] is whitespace
    if not text[head].isspace():
        ws = [i for i in range(head - 1, max(0, head - _CUT_WINDOW), -1) if text[i].isspace()]
        if ws:
            cut = ws[0]
    start = n - tail  # already at a boundary when text[start - 1] is whitespace
    if tail and not text[start - 1].isspace():
        ws = [i for i in range(start, min(n, start + _CUT_WINDOW)) if text[i].isspace()]
        if ws:
            start = ws[0] + 1
    h, t = text[:cut], text[start:] if tail else ""
    omitted = n - len(h) - len(t)
    body = f"{h}\n\n[… {omitted} characters omitted by the screener …]" + (f"\n\n{t}" if t else "")
    return (f"Document length: {n} characters; shown: the first {len(h)} and the last {len(t)}.\n\n"
            + body), len(h) + len(t)


def screening_text(text: str, limit: Limit) -> tuple[str, int]:
    """``(rendered, document chars shown)`` of ``text`` under one limit: an int → :func:`_truncate`
    (v3's tier 1), ``(head, tail)`` → :func:`excerpt`, None → the text unchanged."""
    if limit is None:
        return text, len(text)
    if isinstance(limit, int):
        return _truncate(text, limit), min(len(text), limit)
    return excerpt(text, *limit)


# ── the plan pin ─────────────────────────────────────────────────────────────────────────────


class Tiers(NamedTuple):
    """The two tiers a plan pin resolves to: tier 1's model, tier 2's (None = tier 2 off). (No
    dataclass here: written when the stage loader ran this file outside ``sys.modules``.)"""

    model: str
    adjudicate_model: str | None


def tiers(pin: Mapping[str, Any], llm: LlmConfig) -> Tiers:
    """The tiers of plan pin ``pin`` (module docstring); unknown keys and missing roles refuse."""
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise StageError(f"annotate.screen: plan pin keys {extra} not in {sorted(PIN_KEYS)}")
    input_limits(pin)  # a bad limit refuses here, before any call
    tier2 = pin.get("tier2", True)
    if not isinstance(tier2, bool):
        raise StageError(f"annotate.screen: plan pin tier2 must be true/false, got {tier2!r}")
    for role in (ROLE, ADJUDICATE_ROLE) if tier2 else (ROLE,):
        if role not in llm.roles:
            raise StageError(f"annotate.screen needs llm.yaml role {role!r}"
                             + (" (or tier2: false in the plan pin)" if role == ADJUDICATE_ROLE else ""))
    model = pin.get("model") or llm.roles[ROLE].model
    adjudicate = (pin.get("adjudicate_model") or llm.roles[ADJUDICATE_ROLE].model) if tier2 else None
    return Tiers(model=model, adjudicate_model=adjudicate)


def _digests(role: RoleConfig, model: str, text: str, schema: Mapping[str, Any],
             max_tokens: int | None = None) -> list[tuple[str, str]]:
    """``(template_digest, model)`` for every model a tier may call (its model, then the role's
    fallbacks) sending template ``text`` with output ``schema`` and ``max_tokens`` (None: the
    role's), as :func:`stack.llm.render_call` computes ``template_digest``."""
    out = []
    for m in [model, *role.fallbacks]:
        params: dict[str, Any] = {"model": m, "max_tokens": max_tokens if max_tokens is not None else role.max_tokens}
        if role.temperature is not None:
            params["temperature"] = role.temperature
        if role.reasoning_effort is not None:
            params["reasoning_effort"] = role.reasoning_effort
        out.append((template_hash(text, schema, params), m))
    return out


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The prompt pin: both tiers' digests of the named template (read once) and models."""
    t = tiers(pin, llm)
    text = template(pin.get("name"))
    s = spec(pin.get("name"))
    schema = s.schema
    pairs = _digests(llm.roles[ROLE], t.model, text, schema, s.tier1_max_tokens)
    if t.adjudicate_model is not None:
        pairs += _digests(llm.roles[ADJUDICATE_ROLE], t.adjudicate_model, text, schema)
    return PromptPin(name=pin.get("name"), template_digests=tuple(dict.fromkeys(d for d, _ in pairs)),
                     models=tuple(dict.fromkeys(m for _, m in pairs)))


# ── tier-2 allowance ─────────────────────────────────────────────────────────────────────────

#: Stage-line options bounding tier 2's spend in one run (review R5-1, round 5), any combination; the
#: tightest applies. Run controls like ``max_rows``: not in the plan pin, so not in the config digest
#: or the work keys. ``tier2_max_usd``: USD. ``tier2_budget_share``: a share of the stage's
#: ``budget.max_usd``. ``tier2_max_share``: a share of the run's screen spend (tier 1 + tier 2,
#: settled; a tier-2 call fits when tier 2's spend with it is at most that share of the total with it,
#: so a run's first borderlines wait until tier 1 has spent enough, and a run whose tier 1 is all cache
#: hits admits none). ``screen_usd_per_1k``: the run's screen spend (tier 1 + tier 2, settled, with the
#: call) at most this many USD per 1,000 documents tier 1 has answered in the run so far (cache hits
#: count as documents at $0), so tier 2 draws from what tier 1 leaves of that rate (D255: under $3 per
#: 1,000 documents); while nothing of tier 2 is spent or in flight in the run the rate admits the
#: call anyway (D706: overshoot at most one estimate; ``tier2_max_usd`` and the shares still refuse).
TIER2_OPTIONS = ("tier2_max_usd", "tier2_budget_share", "tier2_max_share", "screen_usd_per_1k")
#: The line's known ``options`` (D434): the framework's and the tier-2 bounds
OPTIONS: frozenset[str] = ANNOTATE_OPTIONS | frozenset(TIER2_OPTIONS)


class Tier2Allowance:
    """Tier 2's spend bounds for one run (:data:`TIER2_OPTIONS`). A tier-2 call is admitted only when
    the settled tier-2 spend, the estimates of the tier-2 calls in flight and this call's estimate
    (:func:`tier2_estimate`, the client's reservation formula) fit every bound; it then settles to the
    call's actual cost (0 on a cache hit; in batch mode to its estimate, at the batch price). Tier 1's settled cost is added per document
    (:meth:`add_tier1`). The bounds hold whatever the borderline rate does; they are exceeded only by
    an admitted call's retries or fallbacks costing more than its estimate. Thread-safe."""

    def __init__(self, max_usd: float | None, max_share: float | None, label: str,
                 usd_per_1k: float | None = None) -> None:
        self.max_usd, self.max_share, self.label, self.usd_per_1k = max_usd, max_share, label, usd_per_1k
        self.docs = 0
        self.tier1 = 0.0
        self.spent = 0.0
        self.in_flight = 0.0
        self.admitted = 0
        self.deferred = 0
        self._lock = threading.Lock()

    def add_tier1(self, cost: float) -> None:
        """One document's tier-1 answer, at its settled ``cost``."""
        with self._lock:
            self.docs += 1
            self.tier1 += cost

    def _fits(self, estimate: float) -> bool:
        t2 = self.spent + self.in_flight + estimate
        if self.max_usd is not None and t2 - self.max_usd > 1e-12:
            return False
        # The rate admits a call while nothing of tier 2 is spent or in flight in the run, so it overshoots
        # by at most one estimate: else a run whose first answer is a long-text borderline (estimate up to
        # $0.0034, one document's room $0.003) and a pass retrying one deferred borderline refuse forever (D706).
        if (self.usd_per_1k is not None and self.spent + self.in_flight > 0
                and self.tier1 + t2 - self.usd_per_1k * self.docs / 1000 > 1e-12):
            return False
        return not (self.max_share is not None and t2 - self.max_share * (self.tier1 + t2) > 1e-12)

    def admit(self, estimate: float) -> bool:
        with self._lock:
            ok = self._fits(estimate)
            if ok:
                self.in_flight += estimate
                self.admitted += 1
            else:
                self.deferred += 1
            first_refusal = not ok and self.deferred == 1
            spent, tier1 = self.spent, self.tier1
        if first_refusal:
            print(f"annotate.screen: tier-2 allowance ({self.label}) reached at tier 2 ${spent:.4f}, tier 1 "
                  f"${tier1:.4f} settled; a tier-1 borderline that does not fit is not_attempted[budget] and is "
                  f"asked again by the next run (tier 1 from the response cache)", file=sys.stderr, flush=True)
        return ok

    def settle(self, estimate: float, actual: float) -> None:
        with self._lock:
            self.in_flight -= estimate
            self.spent += actual


_ALLOWANCES: dict[tuple[str, int], Tier2Allowance] = {}
_ALLOWANCES_LOCK = threading.Lock()


def _share(key: str, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 1:
        raise StageError(f"annotate.screen: options.{key} must be a number in [0, 1], got {raw!r}")
    return float(raw)


def tier2_allowance(actx: AskContext) -> Tier2Allowance | None:
    """The run's :class:`Tier2Allowance` (one per ``run_id`` and batch pass, ``client.batch_pass``: 0
    in sync mode), or None when no :data:`TIER2_OPTIONS` key is set (tier 2 bounded by the stage
    budget only, as before round 5). A bad value, or
    ``tier2_budget_share`` on a stage line without ``budget.max_usd``, refuses (:class:`StageError`)
    before any call."""
    opts = actx.options
    if not any(opts.get(k) is not None for k in TIER2_OPTIONS):
        return None
    caps: list[float] = []
    label: list[str] = []
    raw_usd = opts.get("tier2_max_usd")
    if raw_usd is not None:
        if isinstance(raw_usd, bool) or not isinstance(raw_usd, (int, float)) or raw_usd < 0:
            raise StageError(f"annotate.screen: options.tier2_max_usd must be a number >= 0, got {raw_usd!r}")
        caps.append(float(raw_usd))
        label.append(f"tier2_max_usd ${float(raw_usd):.4f}")
    if opts.get("tier2_budget_share") is not None:
        share = _share("tier2_budget_share", opts["tier2_budget_share"])
        if actx.budget_usd is None:
            raise StageError("annotate.screen: options.tier2_budget_share is a share of the stage's "
                             "budget.max_usd, and this stage line has none")
        caps.append(share * actx.budget_usd)
        label.append(f"tier2_budget_share {share} of ${actx.budget_usd:.2f}")
    per_1k = None
    if opts.get("screen_usd_per_1k") is not None:
        per_1k = opts["screen_usd_per_1k"]
        if isinstance(per_1k, bool) or not isinstance(per_1k, (int, float)) or per_1k < 0:
            raise StageError(f"annotate.screen: options.screen_usd_per_1k must be a number >= 0, got {per_1k!r}")
        per_1k = float(per_1k)
        label.append(f"screen_usd_per_1k ${per_1k:.2f}")
    max_share = None
    if opts.get("tier2_max_share") is not None:
        max_share = _share("tier2_max_share", opts["tier2_max_share"])
        label.append(f"tier2_max_share {max_share} of screen spend")
    key = (actx.run_id, getattr(actx.client, "batch_pass", 0) if actx.opened else 0)
    with _ALLOWANCES_LOCK:
        got = _ALLOWANCES.get(key)
        if got is None:
            got = _ALLOWANCES[key] = Tier2Allowance(min(caps) if caps else None, max_share, "; ".join(label),
                                                    per_1k)
        return got


def tier2_estimate(llm: LlmConfig, model: str, *, template: str, refs: Mapping[str, str], user: str,
                   schema: Mapping[str, Any]) -> float:
    """A tier-2 call's cost estimate at the ``llm.yaml`` price, the client's reservation formula
    (:func:`stack.llm.reserve_tokens_in` over the template, refs, user text and schema, as JSON
    strings; ``max_tokens`` of the ``screen_adjudicate`` role as output)."""
    body = json.dumps([template, dict(refs), user, dict(schema)], ensure_ascii=False)
    est_in = math.ceil((estimate_tokens(body) + RESERVE_OVERHEAD_TOKENS) * RESERVE_SAFETY_FACTOR)
    role = llm.roles[ADJUDICATE_ROLE]
    return llm.price_for(model).cost(est_in, role.max_tokens)


# ── ask ──────────────────────────────────────────────────────────────────────────────────────


def ask(item: AskItem, actx: AskContext) -> Answer | None:
    """Screen one document (module docstring). None: tier 1 said ``borderline`` and the run's
    tier-2 allowance cannot take the call (:class:`Tier2Allowance`): the framework records the
    document ``not_attempted[budget]`` without stopping the run, and the next run asks it again."""
    t = tiers(actx.plan_pin, actx.config.llm)
    allowance = tier2_allowance(actx) if t.adjudicate_model is not None else None
    name = actx.plan_pin.get("name")
    s = spec(name)
    tmpl = template(name)
    schema = s.schema
    text = item.text or ""
    title = item.row["metadata"]["title"] or "Unknown"
    refs = {} if s.inline_menus else {"enums": _render_menu(actx.config, MENUS)}
    channel_type = channel_doc_type(item.row)
    paper_type = metadata_paper_type(item.row)
    license_class = cast(LicenseClass, item.license_class)
    anchorer = Anchorer.from_config(actx.config, workflow="annotate.screen")
    basis_role = (item.row.get("content") or {}).get("current_role")
    produced = s.records
    limits = input_limits(actx.plan_pin)
    head = f"Title: {title}\n\n" + ("".join(f"{line}\n\n" for line in record_lines(item.row))
                                     if s.record_lines else "")

    first = actx.client.complete(
        role=ROLE, model=t.model, template=tmpl, refs=refs, schema=schema,
        user=(head + "Screening text (abstract or document excerpt):\n"
              + screening_text(text, limits.tier1)[0]),
        license_class=license_class, max_tokens=s.tier1_max_tokens,
        prompt_name=actx.pin.name, basis_digest=item.basis_digest)
    if allowance is not None:
        allowance.add_tier1(float(first.cost_usd or 0.0))
    tier1_verdict = str(first.parsed["verdict"])
    p1, voided = _payload(first, text, anchorer, basis_role=basis_role, tier=1,
                          tier1_verdict=tier1_verdict, tier1_model=first.model, confidence=s.confidence)
    r1 = _records(first, text, anchorer, produced, prompt_name=actx.pin.name, basis_role=basis_role,
                  tier=1, tier1=first.parsed, channel_type=channel_type, paper_type=paper_type)
    counts = {"quotes_voided": voided} if voided else {}
    if tier1_verdict != "borderline" or t.adjudicate_model is None:
        return Answer(p1, llm=first, counts=counts, records=r1)

    rationale = str(first.parsed.get("rationale", ""))[:400]
    preamble = _TIER2_PREAMBLE if limits.tier2 is None else _TIER2_PREAMBLE_LIMITED
    user2 = (preamble.format(rationale=rationale)
             + head + "Screening text:\n" + screening_text(text, limits.tier2)[0])
    estimate = 0.0
    batch_mode = getattr(actx.client, "batch_pass", 0) > 0  # the eval harness's recorder has none
    if allowance is not None:
        estimate = tier2_estimate(actx.config.llm, t.adjudicate_model, template=tmpl, refs=refs, user=user2,
                                  schema=schema) * (BATCH_PRICE_FACTOR if batch_mode else 1.0)
        if not allowance.admit(estimate):
            return None
    try:
        second = _adjudicate(actx, model=t.adjudicate_model, template=tmpl, refs=refs, schema=schema,
                             license_class=license_class, basis_digest=item.basis_digest, user=user2)
    except BaseException:  # in batch mode incl. Deferred: every pass settles a tier-2 call at its estimate
        if allowance is not None:
            allowance.settle(estimate, estimate if batch_mode else 0.0)
        raise
    if allowance is not None:
        allowance.settle(estimate, estimate if batch_mode else float(getattr(second, "cost_usd", 0.0) or 0.0))
    counts["tier2"] = 1
    if isinstance(second, LlmError):
        counts["tier2_failed"] = 1
        return Answer(p1, llm=first, followups=(second,), counts=counts, records=r1)
    p2, voided2 = _payload(second, text, anchorer, basis_role=basis_role, tier=2,
                           tier1_verdict=tier1_verdict, tier1_model=first.model, confidence=s.confidence)
    r2 = _records(second, text, anchorer, produced, prompt_name=actx.pin.name, basis_role=basis_role,
                  tier=2, tier1=first.parsed, channel_type=channel_type, paper_type=paper_type)
    if voided2:
        counts["quotes_voided"] = counts.get("quotes_voided", 0) + voided2
    return Answer(p1, llm=first, followups=(Answer(p2, llm=second, records=r2),), counts=counts, records=r1)


def _adjudicate(actx: AskContext, *, model: str, template: str, refs: Mapping[str, str], user: str,
                schema: Mapping[str, Any], license_class: LicenseClass, basis_digest: str
                ) -> LlmResult | LlmError:
    """The tier-2 call: its result, or the :class:`~stack.llm.LlmError` it failed with (egress
    refusal, too large, schema-invalid, transport failures past the per-call cap). A budget
    refusal and a fatal client error propagate (the framework's ``not_attempted[budget]`` /
    halt). Tier 1's verdict stands whenever this returns an error."""
    try:
        return actx.client.complete(
            role=ADJUDICATE_ROLE, model=model, template=template, refs=refs, user=user, schema=schema,
            license_class=license_class, prompt_name=actx.pin.name, basis_digest=basis_digest,
            attempt_kind="adjudicate")
    except (BudgetExceeded, FatalLlmError):
        raise
    except LlmError as exc:
        if not str(exc).startswith("tier 2"):
            exc.args = (f"tier 2 ({model}): {exc}", *exc.args[1:])
        return exc


def _top_confidence(parsed: Mapping[str, Any]) -> float | None:
    """A two-question answer's confidence in its top-level verdict: the ``confidence`` of the
    question(s) whose verdict equals it, the larger when both do; None when neither carries one."""
    top = parsed.get("verdict")
    confs = [sub.get("confidence") for q in ("stack", "sr")
             if isinstance(sub := parsed.get(q), Mapping) and sub.get("verdict") == top]
    nums = [float(c) for c in confs if isinstance(c, (int, float)) and not isinstance(c, bool)]
    return max(nums) if nums else None


def _payload(res: LlmResult, text: str, anchorer: Anchorer, *, basis_role: str | None, tier: int,
             tier1_verdict: str, tier1_model: str, confidence: str | None = None) -> tuple[dict[str, Any], int]:
    """The ``SCREEN`` payload of one tier's answer and the number of quotes dropped; ``confidence`` is the
    name's :attr:`Spec.confidence` (``answer``: the answer's ``confidence`` is stored; ``top``:
    :func:`_top_confidence`; None: NULL)."""
    p = res.parsed
    quotes, voided = _anchor_quotes(anchorer, text, answer_quotes(p))
    conf = p.get("confidence") if confidence == "answer" else _top_confidence(p) if confidence == "top" else None
    return {
        "verdict": str(p["verdict"]),
        "basis_role": basis_role,
        "buckets": [str(b) for b in p.get("buckets") or []],
        "quotes": quotes,
        "off_topic_reason": p.get("off_topic_reason"),
        "confidence": float(conf) if isinstance(conf, (int, float)) else None,
        "reasoning": p.get("rationale"),
        "adjudication_tier": tier,
        "tier1_verdict": tier1_verdict,
        "tier1_model": tier1_model,
    }, len(voided)


#: Record name → the answer object it reads (``screen-v10``'s ``stack`` / ``sr``, ``screen-v11``'s
#: ``usability``).
_RECORD_QUESTION: dict[str, str] = {"stack_relevance": "stack", "sr_relevance": "sr", "text_usability": "usability",
                                    "doc_class": "doc_class"}


def _records(res: LlmResult, text: str, anchorer: Anchorer, produced: tuple[str, ...], *,
             prompt_name: str | None, basis_role: str | None, tier: int,
             tier1: Mapping[str, Any], channel_type: str | None = None,
             paper_type: str | None = None) -> dict[str, dict[str, Any]]:
    """The record payloads of one tier's answer (module docstring), for the ``produced`` records.
    A produced record whose question object is missing from a schema-valid answer raises (the
    schema requires it; a mismatch is a code bug). ``doc_class``: ``doc_type`` = ``channel_type``
    (:func:`channel_doc_type`) when the channel decides, else the model's; a ``paper`` / ``preprint``
    answer then takes ``paper_type`` (:func:`metadata_paper_type`) when the metadata settles it."""
    out: dict[str, dict[str, Any]] = {}
    for rec in produced:
        q = _RECORD_QUESTION.get(rec)
        if q is None:
            raise StageError(f"annotate.screen: no answer mapping for record {rec!r}")
        sub, sub1 = res.parsed.get(q), tier1.get(q)
        if not isinstance(sub, Mapping) or not isinstance(sub1, Mapping):
            raise StageError(f"annotate.screen: record {rec}: the answer has no {q!r} object")
        if q == "doc_class":
            sp, lang = sub.get("self_published"), sub.get("primary_lang")
            dt = channel_type or str(sub["doc_type"])
            if dt in ("paper", "preprint") and paper_type is not None:
                dt = paper_type
            out[rec] = {"doc_type": dt,
                        "self_published": sp if isinstance(sp, bool) else None,
                        "primary_lang": lang if isinstance(lang, str) and lang else None}
            continue
        if q == "usability":
            out[rec] = {"verdict": str(sub["verdict"]), "issues": _str_list(sub.get("issues")),
                        "note": sub.get("note"), "prompt_name": prompt_name, "model": res.model,
                        "basis_role": basis_role, "adjudication_tier": tier}
            continue
        quotes, _ = _anchor_quotes(anchorer, text, _str_list(sub.get("quotes")))
        conf = sub.get("confidence")
        payload: dict[str, Any] = {
            "verdict": str(sub["verdict"]),
            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
            "quotes": quotes,
            "prompt_name": prompt_name, "model": res.model, "basis_role": basis_role,
            "adjudication_tier": tier, "tier1_verdict": str(sub1["verdict"]),
        }
        if q == "sr":
            payload |= {"category": sub.get("category"), "agenda": sub.get("agenda")}
        out[rec] = payload
    return out


# ── grounding ────────────────────────────────────────────────────────────────────────────────


def _str_list(raw: object) -> list[str]:
    return [str(q) for q in raw] if isinstance(raw, list) else []


def answer_quotes(parsed: Mapping[str, Any]) -> list[str]:
    """The quotes of a parsed answer, in order: the top-level ``quotes``, or for a two-question
    answer (``screen-v10``: ``stack`` and ``sr`` objects) the Stack quotes then the SR quotes with
    exact repeats dropped."""
    if isinstance(parsed.get("stack"), Mapping) or isinstance(parsed.get("sr"), Mapping):
        out: list[str] = []
        for q in ("stack", "sr"):
            sub = parsed.get(q)
            for quote in _str_list(sub.get("quotes") if isinstance(sub, Mapping) else None):
                if quote not in out:
                    out.append(quote)
        return out
    return _str_list(parsed.get("quotes"))


def _anchor_quotes(anchorer: Anchorer, text: str, quotes: list[str]
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Anchor each model-emitted quote against the document text. Returns ``(kept, voided)``:
    ``kept`` = ``QUOTE`` values ``{text, offset}`` (the anchored span of ``text`` and its start,
    so ``text[offset:offset + len(q)] == q`` holds for every kept quote), in the model's order;
    ``voided`` = ``{quote, reason}`` of each quote that did not anchor (dropped, never stored as
    if grounded; v3's §6.1 void recipe)."""
    kept: list[dict[str, Any]] = []
    voided: list[dict[str, Any]] = []
    for quote in quotes:
        outcome = anchorer.anchor_detail(quote, text)
        if outcome.anchor is not None:
            start, end = outcome.anchor.span
            kept.append({"text": text[start:end], "offset": start})
        else:
            voided.append({"quote": quote, "reason": outcome.reason or "no_match"})
    return kept, voided


WORKFLOW = Workflow("screen", ask=ask, prompt=prompt, records=records)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = [
    "OPTIONS", "ADJUDICATE_ROLE",
    "CHANNEL_DOC_TYPES",
    "DEFAULT_NAME",
    "DOC_TYPES",
    "LEGACY_LIMITS",
    "METADATA_PAPER_TYPES",
    "PURE_SUBSTAGES",
    "ROLE",
    "SCHEMA",
    "SCHEMA_DUAL",
    "SCHEMA_DUAL_USABILITY",
    "SCHEMA_SR",
    "SCHEMA_SR_QUESTION",
    "SCHEMA_STACK",
    "SCHEMA_V12",
    "SPECS",
    "SUBSTAGES",
    "TEMPLATE",
    "TIER2_OPTIONS",
    "WORKFLOW",
    "Limits",
    "Spec",
    "Tier2Allowance",
    "Tiers",
    "answer_quotes",
    "ask",
    "channel_doc_type",
    "excerpt",
    "input_limits",
    "metadata_paper_type",
    "prompt",
    "question_mode",
    "record_lines",
    "records",
    "run",
    "schema_for",
    "screening_text",
    "spec",
    "template",
    "tier1_max_tokens",
    "tier2_allowance",
    "tier2_estimate",
    "tiers",
]
