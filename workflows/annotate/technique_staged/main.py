"""``annotate.technique_staged`` — technique roles on the taxonomy v0.9, in one of four modes (techpipe,
D602-D605). ``annotate.technique_roles`` (v0.8) is a separate stage and is not touched.

The plan pin's ``mode`` selects the architecture; all modes share the rubric ``rubric-v0.9-a`` (prompt set
``technique-staged-v2``: ``rubric-v0.9-b`` in the grading call; ``technique-staged-v3``: ``rubric-v0.9-c`` in the
single call, with the inventory contract; ``prompts/README.md``), the quote verifier
and post-processing of technique_roles' ``judge.py`` (loaded read-only by path, D605), and the payload family
``technique_staged`` (``stack/schemas/payloads/technique_staged.py``):

* ``single`` — one call: the whole v0.9 menu in the system part, the document as the user part (the
  current one-call design re-pointed at v0.9; the baseline).
* ``sweep`` — the segmenter (``segment.py``) cuts the body (references excluded) into paragraph-numbered
  chunks; the menu-free pre-pass **P** reads each chunk and returns compact occurrence-level notes
  (strict schema); the notes are appended as ``# READING NOTES`` to the single call's user part (its
  system part is the single prompt's, byte for byte).
* ``staged`` — P, then selection **S** (strict schema; the full menu in the system part; each note with
  its code-cut snippet -> 0-5 candidates or a ``none`` reason; ``doc_level`` <= 5 from the opening;
  batches of 150 notes; ids the answer left out re-asked once), the lexical matcher **L**
  (``lexical.py``), the union capped at 100 as the **sub-menu** (``stages.assemble_submenu``), then the
  grading call **G_sub** (the whole document plus the sub-menu with full definitions and pointers; the
  compact catalogue index in the system part for rows the candidates missed; every candidate a row or
  ``rejected``).
* ``lexical`` — L's body hits as the sub-menu, then G_sub (a control without P).

G_sub always runs, an empty candidate list included (C1). The pieces of each call are in
``stages.py`` (pure); this module makes the calls and assembles the answer.

**P failures** (DESIGN v2 §3.10): a chunk whose answer is schema-invalid after the client's attempts,
truncated twice (16,000 then 32,000 tokens), too large for the model, or ``overflow`` (or more than
120 notes) is split once at a paragraph boundary and both halves are read (repair
``prepass_split:<part>:<why>``); a half that fails again keeps the notes it returned (an overflowing
half) and sets the document flag ``prepass_incomplete`` (``prepass_failed:<part>:<why>``). **S**:
a failure after the client's retries fails the document; ids still missing after the one re-ask set
``select_incomplete``. **Status**: a document is complete without those two flags; complete ->
``ok`` with rows, else ``empty``; incomplete with rows -> ``ok`` plus the flag ``partial``; incomplete
without rows -> raised as the failed call's error (``failed_permanent[schema_invalid|truncated|
too_large]``, cost and attempts of every call of the ask on it; see NOTES for why not
``failed_transient``). Any other LLM error (transport, budget, fatal) propagates with the totals so far.

**Post-processing** (every mode): ``judge.normalize`` with every v0.9 slug and group node as the menu
(unknown slugs to extras, quotes anchored against the whole text, the loose tier, clamps, sweeps
counted, ``menu_gap:`` flags); ``judge.roles_degenerate`` fails the answer as in technique_roles; then
``no_techniques_found`` only when both ``techniques`` and ``extra_techniques`` are empty (P4). G_sub
only: ``rejected`` (slug, reason) goes to the payload's ``prepass.rejected`` (never a row); a row for a
slug outside the sub-menu is kept with repair ``outside_submenu:<slug>``; candidates neither graded nor
rejected are counted (``ungraded_candidates``).

**Cost** (DESIGN §7.4, :func:`merged_llm`): ``Answer.llm`` is the call's own :class:`~stack.llm.LlmResult`
when the ask made exactly one call and no failed one (``single``, ``lexical``), else the mapping of
the evidence row's LLM columns over every call: ``cost_usd`` (failed P calls' cost included),
``tokens_in`` and ``tokens_out`` summed, ``latency_ms`` the ask's wall clock, ``is_cache_hit`` = every
call hit, ``model`` / ``model_digest`` / ``template_digest`` the grading call's, ``rendered_digest`` /
``response_digest`` sha256 over the calls' digests in call order, ``prompt_name`` the pin's set name.
The payload's ``prepass.calls`` lists each call's stable detail (stage, part, model, tokens, response
digest) for per-stage accounting.

**Plan pin** ``prompts.annotate.technique_staged``: ``mode`` (required: ``single | sweep | staged |
lexical``), ``name`` (the prompt set, :data:`PROMPT_SETS`, default :data:`DEFAULT_NAME`; written as
``prompt_name``), ``role`` (``llm.yaml`` role, default :data:`DEFAULT_ROLE`: luna with a luna fallback),
``model`` (the grading call: single, sweep, G_sub; default the role's primary), ``prepass_model``
(sweep, staged; default ``model``), ``select_model`` (staged; default ``model``), ``window`` / ``glean``
(single; booleans: the windowed call of a long body, D614, and the gleaning turn, D615; a gleaning turn that
fails as one call keeps the first answer, repair ``glean_failed:<error_class>``), ``sectors`` (single, not with
``window``; boolean: after the first answer and its gleaning turn, one more turn per sector of the set's sector file,
:data:`SECTOR_FILES`, each listing that sector's entries and adding only new rows; the 2026-10-27 sector-turn round), ``doc_cap`` (single:
the characters of the text the call reads, default :data:`DOC_CHAR_CAP`, at most :data:`DOC_CAP_MAX`; a windowed
single segments that much), ``window_min`` (single with ``window: true``: the body length above which the
document is read in parts, default :data:`WINDOW_MIN`; e.g. the cap 300,000 windows only what one call cannot
read), ``max_tokens`` (every mode: the output cap of every call, default :data:`MAX_TOKENS`, from
:data:`MAX_TOKENS_MIN` to :data:`MAX_TOKENS_MAX`; the client retries a truncated reply once at twice it; in each
call's params, so another value is another template digest, and the default value is the default pin), ``taxonomy`` (a
repo-relative path, default :data:`DEFAULT_TAXONOMY`) and the framework's ``template_digest``. A key
the mode does not use is refused, as is any other key. Reasoning effort is the role's
(``llm.yaml`` ``reasoning_effort``, e.g. role ``extract_staged_low``; D612), so it is in each call's params
and template digest; there is no pin key for it.

**Provenance** (:func:`prompt`): ``template_digests`` = per call type of the mode and per model that
call may use (the stage model and the role's fallbacks), the template digest of its system part with
its schema and params; the digest of every prompt file the mode uses (includes covered); the taxonomy
file's sha256; the sector file's sha256 under ``sectors``; and one digest over the segmenter and lexical versions
the mode uses. ``ask`` refuses
a taxonomy whose sha256 is not pinned. Code changes in ``stages.py`` or technique_roles' ``judge.py``
(:data:`JUDGE_SHA256`, pinned by a test) do not move the digests: bump the registry ``version``.

**Eval support**: :func:`user_message` (the deterministic user message of ``single`` and ``lexical``),
:func:`prepass_messages` and :func:`freeze_info` (what an eval freeze records for a text).

Imports ``stack.*``; its siblings and technique_roles' ``judge.py`` by path (D605).
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from stack import yamlio
from stack.anchor import Anchorer
from stack.annotate import (
    OPTIONS,
    PURE_SUBSTAGES,
    SUBSTAGES,
    Answer,
    AskContext,
    AskItem,
    PromptPin,
    Workflow,
    run_annotate,
)
from stack.annotate.prompt_files import PromptFile, load_prompt
from stack.core.hashes import input_digest, jcs, template_hash
from stack.llm import (
    AttemptsExhausted,
    ContextTooLarge,
    LicenseClass,
    LlmError,
    LlmResult,
    SchemaValidationError,
    TransportError,
    TruncatedOutput,
    fold_message,
    strict_schema,
)
from stack.stage_runner import StageContext, StageError, StageResult, load_path_module

if TYPE_CHECKING:
    from stack.config import LlmConfig

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _load(modname: str, path: Path) -> Any:
    module = load_path_module(modname, path)  # registered in sys.modules (L1-8)
    if module is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"annotate.technique_staged: cannot load {path}")
    return module


taxonomy = _load("_technique_staged__taxonomy", HERE / "taxonomy.py")
segment = _load("_technique_staged__segment", HERE / "segment.py")
lexical = _load("_technique_staged__lexical", HERE / "lexical.py")
stages = _load("_technique_staged__stages", HERE / "stages.py")
#: technique_roles' judge, read-only, under a module name of this stage's own (D605): the existing
#: stage keeps its own copy; this file is never edited from here.
JUDGE_PATH = REPO / "workflows" / "annotate" / "technique_roles" / "judge.py"
judge = _load("_technique_staged__technique_roles_judge", JUDGE_PATH)
#: The sha256 of :data:`JUDGE_PATH` this stage was built against; a test fails when the file changes,
#: so a judge change is noticed (and the registry ``version`` of technique_staged bumped).
JUDGE_SHA256 = "3bbcef8867f86aff021193063e7a17e200c644ec501dd9dfaa278a9827d23187"

WORKFLOW_ID = "annotate.technique_staged"
MODES: tuple[str, ...] = ("single", "sweep", "staged", "lexical")
#: The call types each mode makes, in order.
CALLS: dict[str, tuple[str, ...]] = {"single": ("single",), "sweep": ("prepass", "sweep"),
                                     "staged": ("prepass", "select", "grade"), "lexical": ("grade",)}
#: Prompt set name -> the file per call type (``prompts/<file>.md``); a set is never edited, a new
#: version is a new set.
PROMPT_SETS: dict[str, dict[str, str]] = {
    "technique-staged-v1": {"prepass": "technique-staged-prepass-v1", "select": "technique-staged-selection-v1",
                            "grade": "technique-staged-grading-v1", "sweep": "technique-staged-sweep-v1",
                            "single": "technique-staged-single-v1"},
    # staged / lexical: S lists every supported entry, G keeps co-candidates, rubric 0.9-b; P, single and sweep
    # are v1's files, so v2's P requests are v1's byte for byte (cached P answers serve both sets)
    "technique-staged-v2": {"prepass": "technique-staged-prepass-v1", "select": "technique-staged-selection-v2",
                            "grade": "technique-staged-grading-v2", "sweep": "technique-staged-sweep-v1",
                            "single": "technique-staged-single-v1"},
    # single: the inventory-first contract (sweeps.passages, not_graded, the facet checklist, a reminder after the
    # document) with rubric 0.9-c (0.9-a + precedents 16, 17, 19); every other call is v2's file
    "technique-staged-v3": {"prepass": "technique-staged-prepass-v1", "select": "technique-staged-selection-v2",
                            "grade": "technique-staged-grading-v2", "sweep": "technique-staged-sweep-v1",
                            "single": "technique-staged-single-v2",
                            # read only under the single-mode pin keys `window` (D614) and `glean` (D615); their
                            # system parts are single-v2's byte for byte, so v3's plain single call is unchanged
                            "window": "technique-staged-window-v1", "glean": "technique-staged-glean-v1"},
    # experimental single-call variants of v3's contract (gold-v3 prompt round, eval only; prompts/README.md): a1
    # a rewrite, a2 section coverage, a3 a menu audit (written by Codex Astra); c1 the outline + per-section
    # coverage (user part takes {{outline}}), c2 the menu by area + an area pass. Same schema, rubric 0.9-c. Only a2
    # has window / glean files (system part a2's single file's byte for byte; user tails window-v1's / glean-v1's
    # with a2's section names), so a2 alone takes the pin keys window / glean.
    **{f"technique-staged-v4-{v}": {"prepass": "technique-staged-prepass-v1",
                                     "select": "technique-staged-selection-v2", "grade": "technique-staged-grading-v2",
                                     "sweep": "technique-staged-sweep-v1", "single": f"technique-staged-single-v4-{v}",
                                     **({"window": "technique-staged-window-v4-a2", "glean": "technique-staged-glean-v4-a2"}
                                        if v == "a2" else {})}
       for v in ("a1", "a2", "a3", "c1", "c2")},
    # round-2 variants (gold-v3 prompt round 2, eval only; prompts/README.md): each a single file and a gleaning
    # file whose system part is the single file's byte for byte (user tail: the variant's own second turn); a1 / a2
    # written by Codex Astra (a1 catalogue identity / filing, a2 secondary evidence, backward gleaning), f1 / f2 by
    # Fable (f1 filing by the entries' exclusion clauses + a re-file gleaning step, f2 finding). a1.1 is a1 with one
    # sentence of the shared system part changed: no stated generation budget (a1 named 16,000 tokens, the default
    # max_tokens; the production pin sets max_tokens 64,000), the production set (D708)
    **{f"technique-staged-v5-{v}": {"prepass": "technique-staged-prepass-v1",
                                     "select": "technique-staged-selection-v2", "grade": "technique-staged-grading-v2",
                                     "sweep": "technique-staged-sweep-v1", "single": f"technique-staged-single-v5-{v}",
                                     "glean": f"technique-staged-glean-v5-{v}"}
       for v in ("a1", "a1.1", "a2", "f1", "f2")},
    # the 2026-10-27 sector-turn round (eval only): v5-a1.1's single and gleaning files plus a sector-turn file whose system part
    # is theirs byte for byte; the sectors (technique-sectors-v1.yaml) are listed in SECTOR_FILES
    "technique-staged-v6-s1": {"prepass": "technique-staged-prepass-v1", "select": "technique-staged-selection-v2",
                               "grade": "technique-staged-grading-v2", "sweep": "technique-staged-sweep-v1",
                               "single": "technique-staged-single-v5-a1.1", "glean": "technique-staged-glean-v5-a1.1",
                               "sector": "technique-staged-sector-v6-s1"},
    # p1: v5-a1.1 with two edits of the shared system part (evaluation and attack methods applied as tools are their
    # entries, red-teaming as a defence's test included; the interpretability-auditing check spelled out); s2 = p1 plus
    # the sector turns (its sector file's system part is p1's byte for byte)
    "technique-staged-v6-p1": {"prepass": "technique-staged-prepass-v1", "select": "technique-staged-selection-v2",
                               "grade": "technique-staged-grading-v2", "sweep": "technique-staged-sweep-v1",
                               "single": "technique-staged-single-v6-p1", "glean": "technique-staged-glean-v6-p1"},
    "technique-staged-v6-s2": {"prepass": "technique-staged-prepass-v1", "select": "technique-staged-selection-v2",
                               "grade": "technique-staged-grading-v2", "sweep": "technique-staged-sweep-v1",
                               "single": "technique-staged-single-v6-p1", "glean": "technique-staged-glean-v6-p1",
                               "sector": "technique-staged-sector-v6-s2"},
}
#: Prompt set -> its sector file (``prompts/<file>``) for the pin key ``sectors``: ``version`` and ``sectors`` (each
#: ``id``, ``title``, ``scope``, ``slugs``; ``areas`` informal); every technique of the taxonomy in exactly one sector,
#: no group node (:func:`sectors_of` refuses anything else). Never edited: a change is a new file.
SECTOR_FILES: dict[str, str] = {"technique-staged-v6-s1": "technique-sectors-v1.yaml",
                                "technique-staged-v6-s2": "technique-sectors-v1.yaml"}
#: The experimental v4 single files (each an inventory-contract file).
V4_SINGLE_FILES = frozenset(f"technique-staged-single-v4-{v}" for v in ("a1", "a2", "a3", "c1", "c2"))
#: The round-2 single files (inventory contract), one per ``technique-staged-v5-<v>`` set.
V5_SINGLE_FILES = frozenset(f["single"] for k, f in PROMPT_SETS.items() if k.startswith("technique-staged-v5-"))
#: The v6 single files of their own (inventory contract; v6-s1 reuses v5-a1.1's).
V6_SINGLE_FILES = frozenset({"technique-staged-single-v6-p1"})
#: Prompt files with the inventory contract: their answers' ``passages`` / ``not_graded`` are counted
#: (``stages.inventory_accounting``), never rows; the counts appear only for these files.
INVENTORY_FILES = frozenset({"technique-staged-single-v2", "technique-staged-window-v1", *V4_SINGLE_FILES,
                             "technique-staged-window-v4-a2", *V5_SINGLE_FILES, *V6_SINGLE_FILES})
DEFAULT_NAME = "technique-staged-v1"
DEFAULT_ROLE = "extract_staged"
DEFAULT_TAXONOMY = "taxonomy/taxonomy-v0.9.yaml"
PIN_KEYS = frozenset({"name", "role", "model", "mode", "prepass_model", "select_model", "taxonomy",
                      "template_digest", "window", "glean", "sectors", "doc_cap", "window_min", "max_tokens"})
#: Pin keys only some modes read (a key the mode does not use is refused).
MODE_KEYS: dict[str, frozenset[str]] = {"prepass_model": frozenset({"sweep", "staged"}),
                                        "select_model": frozenset({"staged"}),
                                        "window": frozenset({"single"}), "glean": frozenset({"single"}),
                                        "sectors": frozenset({"single"}),
                                        "doc_cap": frozenset({"single"}), "window_min": frozenset({"single"})}
#: The boolean single-mode options and the call type each adds (its file must be in the prompt set).
OPTION_CALLS: dict[str, str] = {"window": "window", "glean": "glean", "sectors": "sector"}
#: Windowed single (D614): a body (references excluded) over WINDOW_MIN characters is read in parts packed to
#: WINDOW_TARGET (at most WINDOW_MAX), one paragraph of overlap; each part's call shows the first
#: WINDOW_OPENING characters of the body and the outline. Changing these is a WINDOW_VERSION bump.
WINDOW_MIN = 60_000
WINDOW_TARGET = 45_000
WINDOW_MAX = 60_000
WINDOW_OPENING = 3_000
WINDOW_VERSION = "win-v1"
#: Merged rows keep at most this many quotes (union over parts, first seen first).
MERGE_QUOTES = 3
#: Output cap of every call (P per DESIGN §3.2; the client retries a truncated reply once at twice it); the pin
#: ``max_tokens`` replaces it within [MAX_TOKENS_MIN, MAX_TOKENS_MAX] (high reasoning effort on dense documents
#: reaches 16,000 and the 32,000 retry).
MAX_TOKENS = 16_000
MAX_TOKENS_MIN = 1_024
MAX_TOKENS_MAX = 64_000
DOC_CHAR_CAP = 300_000
#: The largest pin ``doc_cap``: about 240k tokens of text at ~4 characters per token, so with the ~40k-token
#: system part a call stays inside gpt-6-luna's 1,050,000-token context (OpenRouter listing, 2026-10-23) with
#: room to spare; above 272k prompt tokens that listing doubles the input price.
DOC_CAP_MAX = 1_000_000
TRUNCATION_NOTE = "\n[DOCUMENT TRUNCATED AT CHARACTER CAP]\n"
P_SCHEMA: dict[str, Any] = strict_schema(stages.prepass_schema())
S_SCHEMA: dict[str, Any] = strict_schema(stages.select_schema())
#: single / sweep: technique_roles' permissive schema (D275); G_sub: the same plus ``rejected`` (P3).
SINGLE_SCHEMA: dict[str, Any] = judge.output_schema()
G_SCHEMA: dict[str, Any] = stages.grading_schema(judge.output_schema())
SCHEMAS: dict[str, dict[str, Any]] = {"prepass": P_SCHEMA, "select": S_SCHEMA, "grade": G_SCHEMA,
                                      "sweep": SINGLE_SCHEMA, "single": SINGLE_SCHEMA}
#: The inventory contract's single call (single-v2): SINGLE_SCHEMA plus ``sweeps.passages`` and ``not_graded``.
INVENTORY_SCHEMA: dict[str, Any] = stages.inventory_schema(SINGLE_SCHEMA)
#: The gleaning turn (D615): only new rows, ``techniques`` and ``extra_techniques`` in single's row shape.
GLEAN_SCHEMA: dict[str, Any] = {"type": "object", "required": ["techniques"], "properties": {
    k: SINGLE_SCHEMA["properties"][k] for k in ("techniques", "extra_techniques")}}
SCHEMAS.update(window=INVENTORY_SCHEMA, glean=GLEAN_SCHEMA, sector=GLEAN_SCHEMA)  # a sector turn: gleaning's shape
#: A prompt file whose call sends another schema than :data:`SCHEMAS` names for its call type.
FILE_SCHEMAS: dict[str, dict[str, Any]] = {"technique-staged-single-v2": INVENTORY_SCHEMA,
                                           **{f: INVENTORY_SCHEMA for f in V4_SINGLE_FILES | V5_SINGLE_FILES |
                                              V6_SINGLE_FILES}}
#: The schema objects of a final single / sweep call and its window / gleaning calls (the eval tells call types
#: apart by schema identity; all of them are the final stage).
FINAL_SCHEMAS: tuple[dict[str, Any], ...] = (SINGLE_SCHEMA, INVENTORY_SCHEMA, GLEAN_SCHEMA)
#: The P failures that split a chunk (any other LlmError propagates).
SPLIT_ERRORS: tuple[type[LlmError], ...] = (SchemaValidationError, TruncatedOutput, ContextTooLarge)
#: The gleaning-turn failures after which the first answer stands (repair ``glean_failed:<error_class>``, count
#: ``glean_failed``): one call's failures, not the run's (budget, fatal transport, egress and config errors propagate).
GLEAN_FALLBACK_ERRORS: tuple[type[LlmError], ...] = (AttemptsExhausted, TransportError, SchemaValidationError,
                                                     TruncatedOutput, ContextTooLarge)
INCOMPLETE_FLAGS = ("prepass_incomplete", "select_incomplete")


class Pin(NamedTuple):
    name: str
    role: str
    mode: str
    model: str
    prepass_model: str
    select_model: str
    taxonomy: Path
    window: bool = False
    glean: bool = False
    sectors: bool = False
    doc_cap: int = DOC_CHAR_CAP
    window_min: int = WINDOW_MIN
    max_tokens: int = MAX_TOKENS

    def calls(self) -> tuple[str, ...]:
        """The call types this pin may make: the mode's, plus a single-mode option's."""
        return (*CALLS[self.mode], *(c for k, c in OPTION_CALLS.items() if getattr(self, k)))

    def model_of(self, call: str) -> str:
        return {"prepass": self.prepass_model, "select": self.select_model}.get(call, self.model)


class Catalogue(NamedTuple):
    """One loaded taxonomy file: the parsed taxonomy, its sha256 and version, every valid slug
    (techniques then group nodes), the menu (S, single, sweep) and the compact index (G_sub)."""

    tax: Any
    sha256: str
    version: str
    slugs: tuple[str, ...]
    menu: str
    index_text: str


def pin_of(pin: Mapping[str, Any], llm: LlmConfig) -> Pin:
    """The plan pin, checked (module docstring); anything else refuses with :class:`StageError`."""
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise StageError(f"{WORKFLOW_ID}: plan pin keys {extra} not in {sorted(PIN_KEYS)}")
    mode = pin.get("mode")
    if mode not in MODES:
        raise StageError(f"{WORKFLOW_ID}: plan pin mode {mode!r} must be one of {list(MODES)}")
    unused = sorted(k for k, modes in MODE_KEYS.items() if k in pin and mode not in modes)
    if unused:
        raise StageError(f"{WORKFLOW_ID}: plan pin keys {unused} are not used by mode {mode!r}")
    name = pin.get("name") or DEFAULT_NAME
    if not isinstance(name, str) or name not in PROMPT_SETS:
        raise StageError(f"{WORKFLOW_ID}: plan pin name {name!r} is not a prompt set ({sorted(PROMPT_SETS)})")
    role = pin.get("role") or DEFAULT_ROLE
    if not isinstance(role, str) or role not in llm.roles:
        raise StageError(f"{WORKFLOW_ID}: plan pin role {role!r} is not an llm.yaml role")
    model = pin.get("model") or llm.roles[role].model
    stage_models = {k: pin.get(k) or model for k in ("prepass_model", "select_model")}
    for k, v in {"model": model, **stage_models}.items():
        if not isinstance(v, str):
            raise StageError(f"{WORKFLOW_ID}: plan pin {k} must be a string, got {v!r}")
    tax = pin.get("taxonomy") or DEFAULT_TAXONOMY
    if not isinstance(tax, str) or tax.startswith("/") or ".." in Path(tax).parts:
        raise StageError(f"{WORKFLOW_ID}: plan pin taxonomy {tax!r} must be a repo-relative path")
    options = {k: pin.get(k, False) for k in OPTION_CALLS}
    for k, v in options.items():
        if not isinstance(v, bool):
            raise StageError(f"{WORKFLOW_ID}: plan pin {k} must be true or false, got {v!r}")
        if v and OPTION_CALLS[k] not in PROMPT_SETS[name]:
            raise StageError(f"{WORKFLOW_ID}: plan pin {k}: prompt set {name!r} has no {OPTION_CALLS[k]!r} file")
    sizes = {k: pin.get(k, d) for k, d in (("doc_cap", DOC_CHAR_CAP), ("window_min", WINDOW_MIN))}
    for k, v in sizes.items():
        if not isinstance(v, int) or isinstance(v, bool) or not WINDOW_MAX <= v <= DOC_CAP_MAX:
            raise StageError(f"{WORKFLOW_ID}: plan pin {k} must be an integer from {WINDOW_MAX} to {DOC_CAP_MAX}, "
                             f"got {v!r}")
    if options["sectors"] and options["window"]:
        raise StageError(f"{WORKFLOW_ID}: plan pin sectors is read only without window")
    if options["sectors"] and name not in SECTOR_FILES:
        raise StageError(f"{WORKFLOW_ID}: plan pin sectors: prompt set {name!r} has no sector file")
    if "window_min" in pin and not options["window"]:
        raise StageError(f"{WORKFLOW_ID}: plan pin window_min is read only with window: true")
    max_tokens = pin.get("max_tokens", MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or \
            not MAX_TOKENS_MIN <= max_tokens <= MAX_TOKENS_MAX:
        raise StageError(f"{WORKFLOW_ID}: plan pin max_tokens must be an integer from {MAX_TOKENS_MIN} to "
                         f"{MAX_TOKENS_MAX}, got {max_tokens!r}")
    return Pin(name=name, role=role, mode=mode, model=model, prepass_model=stage_models["prepass_model"],
               select_model=stage_models["select_model"], taxonomy=REPO / tax, **options, **sizes,
               max_tokens=max_tokens)


def schema_of(name: str, call: str) -> dict[str, Any]:
    """The schema ``call`` sends in prompt set ``name`` (:data:`FILE_SCHEMAS`, else :data:`SCHEMAS`)."""
    return FILE_SCHEMAS.get(PROMPT_SETS[name][call], SCHEMAS[call])


def prompt_file(name: str, call: str) -> PromptFile:
    """The prompt file of ``call`` in prompt set ``name``."""
    return load_prompt(HERE / "prompts", PROMPT_SETS[name][call], what=WORKFLOW_ID)


def catalogue(path: Path) -> Catalogue:
    """The catalogue of the taxonomy file at ``path`` (cached by its bytes, so an edit is seen)."""
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise StageError(f"{WORKFLOW_ID}: cannot read the taxonomy: {exc}") from exc
    return _catalogue(str(path), hashlib.sha256(blob).hexdigest())


@functools.lru_cache(maxsize=4)
def _catalogue(path: str, digest: str) -> Catalogue:
    try:
        tax = taxonomy.load_taxonomy(Path(path))
    except ValueError as exc:
        raise StageError(f"{WORKFLOW_ID}: taxonomy {path}: {exc}") from exc
    if taxonomy.taxonomy_digest(Path(path)) != digest:
        raise StageError(f"{WORKFLOW_ID}: the taxonomy file changed while loading")
    return Catalogue(tax=tax, sha256=digest, version=tax.version, slugs=taxonomy.all_slugs(tax),
                     menu=taxonomy.menu_text(tax), index_text=taxonomy.compact_index(tax))


class Sector(NamedTuple):
    id: str
    title: str
    scope: str
    slugs: tuple[str, ...]


class Sectors(NamedTuple):
    sha256: str
    version: str
    sectors: tuple[Sector, ...]


def sectors_of(name: str, cat: Catalogue) -> Sectors:
    """The sector file of prompt set ``name`` (:data:`SECTOR_FILES`), checked against ``cat``: every technique in
    exactly one sector, no group node, no unknown slug; anything else refuses with :class:`StageError`."""
    path = HERE / "prompts" / SECTOR_FILES[name]
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise StageError(f"{WORKFLOW_ID}: cannot read the sector file: {exc}") from exc
    return _sectors(str(path), hashlib.sha256(blob).hexdigest(), cat.sha256, cat)


@functools.lru_cache(maxsize=4)
def _sectors(path: str, digest: str, cat_digest: str, cat: Catalogue) -> Sectors:
    where = f"{WORKFLOW_ID}: sector file {Path(path).name}"
    data = yamlio.load(Path(path).read_bytes())
    if not isinstance(data, Mapping) or set(data) != {"version", "sectors"} or not isinstance(data["sectors"], list):
        raise StageError(f"{where}: top-level keys must be version and sectors (a list)")
    techniques = {t.slug for t in cat.tax.techniques}
    out: list[Sector] = []
    seen: dict[str, str] = {}
    for e in data["sectors"]:
        if not isinstance(e, Mapping) or not {"id", "title", "scope", "slugs"} <= set(e) or \
                not set(e) <= {"id", "title", "scope", "slugs", "areas"}:
            raise StageError(f"{where}: a sector needs id, title, scope, slugs (areas optional): {e!r}")
        slugs = tuple(str(x) for x in e["slugs"])
        for s in slugs:
            if s not in techniques:
                raise StageError(f"{where}: sector {e['id']!r}: {s!r} is not a technique of {cat.version}")
            if s in seen:
                raise StageError(f"{where}: {s!r} is in sectors {seen[s]!r} and {e['id']!r}")
            seen[s] = str(e["id"])
        out.append(Sector(id=str(e["id"]), title=str(e["title"]), scope=str(e["scope"]), slugs=slugs))
    missing = sorted(techniques - set(seen))
    if missing:
        raise StageError(f"{where}: techniques in no sector: {missing[:5]} ({len(missing)})")
    return Sectors(sha256=digest, version=str(data["version"]), sectors=tuple(out))


def sector_fills(sec: Sector, k: int, n: int, cat: Catalogue) -> dict[str, str]:
    """The sector turn's user fills: the sector's position, title, scope and its entries (``- slug: name``)."""
    names = {t.slug: t.name for t in cat.tax.techniques}
    return {"sector_index": str(k), "sector_count": str(n), "sector_title": sec.title, "sector_scope": sec.scope,
            "sector_entries": "\n".join(f"- {s}: {names[s]}" for s in sec.slugs)}


def system_template(pf: PromptFile, call: str, cat: Catalogue) -> str:
    """The system part of ``call`` with its catalogue fill (the ``template`` of every such call)."""
    fills = {"select": {"taxonomy_menu": cat.menu}, "sweep": {"taxonomy_menu": cat.menu},
             "single": {"taxonomy_menu": cat.menu}, "window": {"taxonomy_menu": cat.menu},
             "glean": {"taxonomy_menu": cat.menu}, "sector": {"taxonomy_menu": cat.menu},
             "grade": {"catalogue_index": cat.index_text}}.get(call, {})
    return pf.system(fills)


def versions_digest(mode: str, *, window: bool = False, doc_cap: int = DOC_CHAR_CAP,
                    window_min: int = WINDOW_MIN, outline: bool = False) -> str | None:
    """The digest over the segmenter and lexical versions ``mode`` uses (None for plain ``single``; a windowed
    single, or a single file whose user part takes ``{{outline}}``, adds the segmenter; a windowed one also
    :data:`WINDOW_VERSION`), plus the pin's ``doc_cap`` / ``window_min`` when they differ from the defaults (so a
    default pin's digests are unchanged and another size is another config digest)."""
    used: dict[str, Any] = ({"segmenter": segment.SEGMENTER_VERSION}
                            if mode in ("sweep", "staged", "lexical") or window or outline else {})
    if mode in ("staged", "lexical"):
        used["lexical"] = lexical.LEXICAL_VERSION
    if window:
        used["window"] = WINDOW_VERSION
    if doc_cap != DOC_CHAR_CAP:
        used["doc_cap"] = doc_cap
    if window_min != WINDOW_MIN:
        used["window_min"] = window_min
    return hashlib.sha256(jcs(used).encode("utf-8")).hexdigest() if used else None


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The prompt pin (no network; module docstring "Provenance")."""
    p = pin_of(pin, llm)
    cat = catalogue(p.taxonomy)
    role = llm.roles[p.role]
    digests: list[str] = []
    files: list[str] = []
    models: list[str] = []
    for call in p.calls():
        pf = prompt_file(p.name, call)
        template = system_template(pf, call, cat)
        for model in dict.fromkeys([p.model_of(call), *role.fallbacks]):
            params: dict[str, Any] = {"model": model, "max_tokens": p.max_tokens}
            if role.temperature is not None:
                params["temperature"] = role.temperature
            if role.reasoning_effort is not None:
                params["reasoning_effort"] = role.reasoning_effort
            digests.append(template_hash(template, schema_of(p.name, call), params))
            models.append(model)
        files.append(pf.digest)
    outline = p.mode == "single" and takes_outline(prompt_file(p.name, "single"))
    extra = [d for d in (versions_digest(p.mode, window=p.window, doc_cap=p.doc_cap, window_min=p.window_min,
                                         outline=outline),) if d]
    if p.sectors:
        extra.append(sectors_of(p.name, cat).sha256)
    return PromptPin(name=p.name, template_digests=tuple(dict.fromkeys([*digests, *files, cat.sha256, *extra])),
                     models=tuple(dict.fromkeys(models)))


# ── deterministic fills (also read by the eval freeze) ───────────────────────────────────────


def doc_fills(text: str, cap: int = DOC_CHAR_CAP) -> dict[str, str]:
    """``document`` and ``truncation`` (technique_roles' window and note; ``cap``: the pin's ``doc_cap``)."""
    return {"document": text[:cap], "truncation": TRUNCATION_NOTE if len(text) > cap else ""}


def takes_outline(pf: PromptFile) -> bool:
    """Whether a single file's user part shows the document's outline (``{{outline}}``)."""
    return "outline" in pf.user_placeholders


def single_fills(pf: PromptFile, text: str, cap: int = DOC_CHAR_CAP) -> dict[str, str]:
    """The single call's user fills: :func:`doc_fills`, plus ``outline`` (the segmenter's body headings of the
    text read, :func:`segment.outline`) when the file takes it."""
    fills = doc_fills(text, cap)
    if takes_outline(pf):
        fills["outline"] = segment.outline(segment.segment(text, cap=cap)) or "(no headings found)"
    return fills


def lexical_hits(cat: Catalogue, text: str, seg: Any) -> dict[str, dict[str, Any]]:
    """The matcher's hits with at least one body hit (slug -> hit)."""
    lex = lexical.lexicon_for(cat.tax, cat.sha256)
    return {s: h for s, h in lex.match(text, seg.paragraphs).items() if h["hits_body"] > 0}


def section_map(seg: Any) -> dict[str, str]:
    return {p.pid: p.section for p in seg.paragraphs}


def grade_user(pf: PromptFile, text: str, cat: Catalogue, sub: Any) -> str:
    cands = taxonomy.submenu_text(cat.tax, list(sub.slugs), sub.pointers) if sub.slugs else stages.NO_CANDIDATES
    return pf.user({**doc_fills(text), "candidate_count": str(len(sub.slugs)), "candidates": cands})


def user_message(text: str, *, mode: str = "single", name: str = DEFAULT_NAME,
                 taxonomy_path: Path | None = None, doc_cap: int = DOC_CHAR_CAP) -> str:
    """The user message of ``mode``'s one call when it does not depend on another call's answer:
    ``single`` (the document) and ``lexical`` (the document plus the lexical sub-menu). ``sweep``
    and ``staged`` raise ``ValueError`` (their final message depends on P and S; see
    :func:`prepass_messages`). Pure."""
    if mode == "single":
        pf = prompt_file(name, "single")
        return pf.user(single_fills(pf, text, doc_cap))
    if mode == "lexical":
        cat = catalogue(taxonomy_path or REPO / DEFAULT_TAXONOMY)
        seg = segment.segment(text)
        sub = stages.assemble_submenu([], [], lexical_hits(cat, text, seg), section_map(seg), [])
        return grade_user(prompt_file(name, "grade"), text, cat, sub)
    raise ValueError(f"{WORKFLOW_ID}: mode {mode!r} has no answer-independent final user message")


def prepass_messages(text: str, *, name: str = DEFAULT_NAME) -> list[str]:
    """P's user messages for ``text``, one per chunk (before any split). Pure."""
    seg = segment.segment(text)
    pf = prompt_file(name, "prepass")
    return [pf.user(stages.prepass_fills(part, seg.opening)) for part in stages.parts_of(seg)]


def freeze_info(text: str, *, name: str = DEFAULT_NAME, taxonomy_path: Path | None = None) -> dict[str, Any]:
    """What an eval freeze records for ``text``: versions, the taxonomy sha256, digests of the
    segmentation summary and of the lexical hits, ``staged_fills_digest`` (sha256 over the two and the
    taxonomy sha256, DESIGN §7.6) and the client's ``input_digest`` of every answer-independent user
    message: ``single``, ``lexical`` and each P chunk (``prepass``, shared by sweep and staged)."""
    cat = catalogue(taxonomy_path or REPO / DEFAULT_TAXONOMY)
    seg = segment.segment(text)
    summary = seg.summary()
    hits = lexical_hits(cat, text, seg)
    seg_digest = hashlib.sha256(jcs(summary).encode("utf-8")).hexdigest()
    lex_digest = hashlib.sha256(jcs({s: h["hits_body"] for s, h in sorted(hits.items())}).encode("utf-8")).hexdigest()
    return {
        "segmenter_version": segment.SEGMENTER_VERSION, "lexical_version": lexical.LEXICAL_VERSION,
        "taxonomy_sha256": cat.sha256, "segmentation_digest": seg_digest, "lexical_digest": lex_digest,
        "staged_fills_digest": hashlib.sha256(jcs([seg_digest, lex_digest, cat.sha256]).encode("utf-8")).hexdigest(),
        "chunks": len(seg.chunks),
        "user_digests": {"single": input_digest(user_message(text, mode="single", name=name)),
                         "lexical": input_digest(user_message(text, mode="lexical", name=name,
                                                              taxonomy_path=taxonomy_path)),
                         "prepass": [input_digest(m) for m in prepass_messages(text, name=name)]},
    }


# ── the calls of one ask ─────────────────────────────────────────────────────────────────────


class _Ask:
    """One ``ask``'s calls: makes them, keeps every result and every failed call's cost, and merges
    them for the evidence row (module docstring "Cost")."""

    def __init__(self, item: AskItem, actx: AskContext, p: Pin, cat: Catalogue) -> None:
        self.item, self.actx, self.p, self.cat = item, actx, p, cat
        self.t0 = time.monotonic()
        self.results: list[tuple[str, str | None, LlmResult]] = []  # (stage, part, result)
        self.failed_cost = 0.0
        self.failed_attempts: list[Any] = []
        self.failures = 0

    def call(self, call: str, user: str, *, part: str | None = None,
             prior: Sequence[Mapping[str, str]] = ()) -> LlmResult:
        pf = prompt_file(self.p.name, call)
        extra: dict[str, Any] = {"prior": list(prior)} if prior else {}
        schema = schema_of(self.p.name, call)
        try:
            res = self.actx.client.complete(
                role=self.p.role, model=self.p.model_of(call), template=system_template(pf, call, self.cat),
                refs={}, user=user, schema=schema, max_tokens=self.p.max_tokens,
                license_class=cast(LicenseClass, self.item.license_class), prompt_name=self.actx.pin.name,
                basis_digest=self.item.basis_digest, **extra)
        except LlmError as exc:
            self.failures += 1
            self.failed_cost += float(getattr(exc, "cost_usd", 0.0) or 0.0)
            self.failed_attempts.extend(getattr(exc, "attempts", ()) or ())
            raise
        self.results.append((call, part, res))
        return res

    def fail(self, exc: LlmError) -> LlmError:
        """``exc`` carrying the cost and attempts of every call of the ask (the stage's failure row
        reads them). Call after :meth:`call` recorded ``exc``'s own cost, or for a raised-here error."""
        exc.cost_usd = self.cost()  # type: ignore[attr-defined]
        exc.attempts = self.attempts()  # type: ignore[attr-defined]
        return exc

    def cost(self) -> float:
        return sum(r.cost_usd for _, _, r in self.results) + self.failed_cost

    def attempts(self) -> tuple[Any, ...]:
        return tuple(a for _, _, r in self.results for a in r.attempts) + tuple(self.failed_attempts)

    def llm(self) -> Any:
        """``Answer.llm``: the single call's result, or the merged mapping (module docstring)."""
        if len(self.results) == 1 and not self.failures:
            return self.results[0][2]
        return merged_llm([r for _, _, r in self.results], self.failed_cost, self.actx.pin.name,
                          int((time.monotonic() - self.t0) * 1000))

    def calls_detail(self) -> list[dict[str, Any]]:
        return [{"stage": s, "part": part, "model": r.model, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                 "response_digest": r.response_digest} for s, part, r in self.results]


def merged_llm(results: Sequence[LlmResult], failed_cost: float, prompt_name: str | None,
               latency_ms: int) -> dict[str, Any]:
    """The evidence row's LLM columns over several calls (DESIGN §7.4); the last result is the
    grading call."""
    grading = results[-1]

    def over(key: str) -> str:
        return hashlib.sha256(jcs([getattr(r, key) for r in results]).encode("utf-8")).hexdigest()

    return {"is_cache_hit": all(r.is_cache_hit for r in results), "template_digest": grading.template_digest,
            "model_digest": grading.model_digest, "prompt_name": prompt_name,
            "rendered_digest": over("rendered_digest"), "model": grading.model,
            "response_digest": over("response_digest"),
            "cost_usd": sum(r.cost_usd for r in results) + failed_cost,
            "tokens_in": sum(r.tokens_in for r in results), "tokens_out": sum(r.tokens_out for r in results),
            "latency_ms": latency_ms, **usage_sums(results)}


def usage_sums(results: Sequence[LlmResult]) -> dict[str, int | None]:
    """The usage-detail columns over several calls (D714): each the sum of the calls that report it, None when
    none does."""
    out: dict[str, int | None] = {}
    for key in ("tokens_cached", "tokens_cache_write", "tokens_reasoning"):
        vals = [getattr(r, key) for r in results if getattr(r, key) is not None]
        out[key] = sum(vals) if vals else None
    return out


def usage_counts(results: Sequence[tuple[str, Any]]) -> dict[str, int]:
    """Run counts of the provider usage per call type (D714; run record only, not the payload): for each
    ``(call, result)`` answered by the provider (no response-cache hit), ``calls.<call>``,
    ``tokens.prompt.<call>``, ``tokens.cached.<call>``, ``tokens.cache_write.<call>``, ``tokens.reasoning.<call>``
    and ``tokens.out.<call>`` (a gleaning turn is call ``glean``)."""
    counts: dict[str, int] = {}

    def add(k: str, v: int | None) -> None:
        if v:
            counts[k] = counts.get(k, 0) + int(v)

    for call, r in results:
        if r.is_cache_hit:
            continue
        add(f"calls.{call}", 1)
        add(f"tokens.prompt.{call}", r.tokens_in)
        add(f"tokens.cached.{call}", r.tokens_cached)
        add(f"tokens.cache_write.{call}", r.tokens_cache_write)
        add(f"tokens.reasoning.{call}", r.tokens_reasoning)
        add(f"tokens.out.{call}", r.tokens_out)
    return counts


class _Prepass(NamedTuple):
    mentions: list[Any]
    incomplete: bool
    error_class: str | None  # the last failure's (raised as the document's when nothing else is left)
    splits: int


def _read_part(a: _Ask, pf: PromptFile, part: Any, opening: str,
               repairs: list[str]) -> tuple[str, list[Any], str | None]:
    """One P call: ``("ok" | "overflow" | "failed", notes, error_class)``."""
    try:
        res = a.call("prepass", pf.user(stages.prepass_fills(part, opening)), part=part.label)
    except SPLIT_ERRORS as exc:
        return "failed", [], exc.error_class
    notes = stages.clean_mentions(res.parsed, part, repairs)
    raw = res.parsed.get("mentions") or []
    if res.parsed.get("overflow") or len(raw) > stages.MENTION_CAP:
        return "overflow", notes[:stages.MENTION_CAP], "truncated"
    return "ok", notes, None


def run_prepass(a: _Ask, seg: Any, repairs: list[str]) -> _Prepass:
    """P over every chunk, one half-split level (module docstring)."""
    pf = prompt_file(a.p.name, "prepass")
    notes: list[Any] = []
    parts = stages.parts_of(seg)
    order: dict[str, int] = {}
    incomplete, error_class, splits = False, None, 0
    for part in parts:
        order[part.label] = len(order)
        how, got, err = _read_part(a, pf, part, seg.opening, repairs)
        if how == "ok":
            notes.extend(got)
            continue
        halves = stages.split_part(part)
        if halves is None:
            repairs.append(f"prepass_failed:{part.label}:{err}")
            notes.extend(got)
            incomplete, error_class = True, err
            continue
        repairs.append(f"prepass_split:{part.label}:{'overflow' if how == 'overflow' else err}")
        splits += 1
        for half in halves:
            order[half.label] = len(order)
            how2, got2, err2 = _read_part(a, pf, half, seg.opening, repairs)
            notes.extend(got2)
            if how2 != "ok":
                repairs.append(f"prepass_failed:{half.label}:{'overflow' if how2 == 'overflow' else err2}")
                incomplete, error_class = True, err2
    kept, deduped = stages.dedupe_overlap(notes, parts)
    if deduped:
        repairs.append(f"mentions_deduped:{deduped}")
    return _Prepass(mentions=stages.assign_ids(kept, order), incomplete=incomplete, error_class=error_class,
                    splits=splits)


def run_selection(a: _Ask, opening: str, mentions: list[Any],
                  repairs: list[str]) -> tuple[list[dict[str, Any]], bool, int]:
    """S over the notes in batches, one re-ask of missing ids; ``(doc_level, incomplete, calls)``."""
    pf = prompt_file(a.p.name, "select")
    valid = frozenset(a.cat.slugs)
    doc_level: list[dict[str, Any]] = []
    missing: list[Any] = []
    groups = stages.batches(mentions)
    calls = 0
    for k, batch in enumerate(groups, start=1):
        note = stages.batch_note(k, len(groups))
        res = a.call("select", pf.user(stages.select_fills(opening, batch, note)), part=f"batch{k}")
        calls += 1
        dl, miss = stages.apply_selection(res.parsed, {m.id: m for m in batch}, valid, repairs, read_doc_level=k == 1)
        doc_level = dl if k == 1 else doc_level
        missing.extend(m for m in batch if m.id in set(miss))
    if missing:
        repairs.append(f"select_reask:{len(missing)}")
        res = a.call("select", pf.user(stages.select_fills(opening, missing, stages.batch_note(1, 1, reask=True))),
                     part="reask")
        calls += 1
        _dl, still = stages.apply_selection(res.parsed, {m.id: m for m in missing}, valid, repairs,
                                            read_doc_level=False)
        for mid in still:
            repairs.append(f"select_missing:{mid}")
        return doc_level, bool(still), calls
    return doc_level, False, calls


def _rejected(parsed: Mapping[str, Any], submenu: Sequence[str], repairs: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in parsed.get("rejected") or []:
        if not isinstance(r, Mapping) or not isinstance(r.get("slug"), str) or not r["slug"].strip():
            repairs.append("bad_rejected")
            continue
        slug, reason = r["slug"].strip()[:120], str(r.get("reason") or "").strip()[:60]
        if reason not in stages.REJECT_REASONS:
            repairs.append(f"rejected_reason_unknown:{slug}")
        if slug not in submenu:
            repairs.append(f"rejected_not_candidate:{slug}")
        out.append({"slug": slug, "reason": reason or "unspecified"})
    return out


def _no_techniques(normalized: dict[str, Any]) -> None:
    """P4: ``no_techniques_found`` only when both row lists are empty (judge.normalize keys it on
    ``techniques`` alone)."""
    flags, repairs = normalized["flags"], normalized["repairs"]
    want = not normalized["techniques"] and not normalized["extra_techniques"]
    flag = judge.NO_TECHNIQUES
    if not want and flag in flags:
        flags.remove(flag)
        if "no_techniques_found_added" in repairs:
            repairs.remove("no_techniques_found_added")
        else:
            repairs.append("no_techniques_found_dropped")


def _segmenter_record(seg: Any) -> dict[str, Any]:
    s = seg.summary()
    refs = s["references"]
    return {"segmenter_version": s["segmenter_version"], "lexical_version": lexical.LEXICAL_VERSION,
            "doc_chars": s["doc_chars"], "read_chars": s["read_chars"], "truncated": s["truncated"],
            "n_paragraphs": s["n_paragraphs"], "body_chars": s["body_chars"],
            "references_method": refs["method"], "reference_paragraphs": refs["paragraphs"],
            "reference_chars": refs["chars"], "references_first_pid": refs["first_pid"],
            "chunks": [{"index": c["index"], "total": c["total"], "first_pid": c["first_pid"],
                        "last_pid": c["last_pid"], "n_paragraphs": c["n_paragraphs"],
                        "overlap": len(c["overlap_pids"]), "chars": c["chars"]} for c in s["chunks"]]}


def _mention_record(m: Any) -> dict[str, Any]:
    return {"id": m.id, "chunk": m.part, "paras": list(m.paras), "surface": m.surface, "gloss": m.gloss or None,
            "stance_hint": m.stance, "implicit": m.implicit,
            "candidates": [{"slug": s, "confidence": c} for s, c in m.candidates], "none_reason": m.none_reason}


def ask(item: AskItem, actx: AskContext) -> Answer:
    """Annotate one document in the pin's mode (module docstring)."""
    p = pin_of(actx.plan_pin, actx.config.llm)
    cat = catalogue(p.taxonomy)
    if cat.sha256 not in actx.pin.template_digests:
        raise StageError(f"{WORKFLOW_ID}: {p.taxonomy.name} changed during the run (sha256 {cat.sha256} is not "
                         f"among the pinned {list(actx.pin.template_digests)})")
    text = item.text or ""  # the framework skips a document without text (skipped[no_text])
    a = _Ask(item, actx, p, cat)
    try:
        return _ask(a, text)
    except LlmError as exc:
        a.fail(exc)
        raise


def _ask(a: _Ask, text: str) -> Answer:
    p, cat = a.p, a.cat
    pre_repairs: list[str] = []
    flags: list[str] = []
    counts: dict[str, int] = {}
    seg = segment.segment(text) if p.mode != "single" else None  # a windowed single segments in _single_calls
    mentions: list[Any] = []
    doc_level: list[dict[str, Any]] = []
    hits: dict[str, dict[str, Any]] = {}
    sub = None
    pre = None
    if p.mode in ("sweep", "staged"):
        pre = run_prepass(a, seg, pre_repairs)
        mentions = pre.mentions
        if pre.incomplete:
            flags.append("prepass_incomplete")
        counts.update(p_calls=sum(1 for s, _, _ in a.results if s == "prepass") + a.failures,
                      p_splits=pre.splits, mentions=len(mentions))
    select_incomplete = False
    if p.mode == "staged" and mentions:
        assert seg is not None
        doc_level, select_incomplete, n = run_selection(a, seg.opening, mentions, pre_repairs)
        counts["s_calls"] = n
        if select_incomplete:
            flags.append("select_incomplete")
    if p.mode in ("staged", "lexical"):
        hits = lexical_hits(cat, text, seg)
        sub = stages.assemble_submenu(mentions, doc_level, hits, section_map(seg), pre_repairs)
        counts.update(submenu=len(sub.slugs), submenu_dropped=len(sub.dropped))
    # the grading call
    if p.mode == "single":
        answer = _single_calls(a, text, counts, pre_repairs)
    elif p.mode == "sweep":
        answer = a.call("sweep", prompt_file(p.name, "sweep").user(
            {**doc_fills(text), "reading_notes": stages.reading_notes(mentions)})).parsed
    else:
        assert sub is not None
        answer = a.call("grade", grade_user(prompt_file(p.name, "grade"), text, cat, sub)).parsed
    model = a.results[-1][2].model
    parsed = dict(answer)
    rejected = _rejected(parsed, sub.slugs, pre_repairs) if sub is not None else []
    parsed.pop("rejected", None)
    inventory = dict(parsed) if PROMPT_SETS[p.name][CALLS[p.mode][-1]] in INVENTORY_FILES else None
    parsed.pop("not_graded", None)  # inventory bookkeeping: never a row (normalize reads only the row lists)
    anchorer = Anchorer.from_config(a.actx.config, workflow=WORKFLOW_ID)
    normalized = judge.normalize(parsed, slugs=cat.slugs, text=text, anchorer=anchorer)
    if text.strip() and judge.roles_degenerate(normalized):
        dropped = sum(r.startswith("empty_roles_dropped:") for r in normalized["repairs"])
        raise SchemaValidationError(
            f"{WORKFLOW_ID}: every row of the answer ({dropped}) has an empty roles map, nothing kept "
            f"(model {model}); stored as a failure, not as an empty result", [f"empty_roles_dropped x{dropped}"])
    _no_techniques(normalized)
    repairs = normalized["repairs"]
    if inventory is not None:
        counts.update(stages.inventory_accounting(inventory, frozenset(cat.slugs),
                                                  frozenset(t["slug"] for t in normalized["techniques"]), repairs))
    if sub is not None:
        graded = {str(r.get("slug") or "").strip() for r in parsed.get("techniques") or [] if isinstance(r, Mapping)}
        outside = [t["slug"] for t in normalized["techniques"] if t["slug"] not in sub.slugs]
        repairs.extend(f"outside_submenu:{s}" for s in outside)
        answered = graded | {r["slug"] for r in rejected}
        counts.update(outside_submenu=len(outside), rejected=len(rejected),
                      ungraded_candidates=sum(s not in answered for s in sub.slugs))
    payload = judge.to_payload(normalized)
    payload["repairs"] = [*pre_repairs, *payload["repairs"]]
    rows = [*payload["techniques"], *payload["extra_techniques"]]
    incomplete = bool(flags)
    if incomplete and not rows:
        raise _incomplete_error(pre, select_incomplete)
    payload["flags"] = [*payload["flags"], *flags, *(["partial"] if incomplete else [])]
    gaps = _candidate_gaps(mentions, payload)
    unresolved = [{"surface": m.surface, "gloss": m.gloss or None, "snippet": m.snippet}
                  for m in mentions if m.none_reason == "too_vague"]
    prepass = {"segmenter": _segmenter_record(seg) if seg is not None else None,
               "mentions": [_mention_record(m) for m in mentions], "doc_level": doc_level,
               "lexical": [{"slug": s, "hits_body": int(h["hits_body"])} for s, h in sorted(hits.items())],
               "submenu": list(sub.slugs) if sub is not None else [], "rejected": rejected,
               "calls": a.calls_detail(), "counts": []}
    counts.update(rows=len(payload["techniques"]), extra_rows=len(payload["extra_techniques"]),
                  candidate_gaps=len(gaps), unresolved=len(unresolved),
                  quote_unverified_rows=sum(judge.QUOTE_UNVERIFIED in r["flags"] for r in rows),
                  dropped_no_quote=sum(r.startswith("no_quote_dropped:") for r in payload["repairs"]),
                  menu_gap_flags=sum(f.startswith(judge.MENU_GAP) for f in payload["flags"]),
                  sweeps_absent=int(normalized["sweeps"] is None), truncated_docs=int(len(text) > p.doc_cap))
    prepass["counts"] = [{"name": k, "value": int(v)} for k, v in sorted(counts.items())]
    payload.update(mode=p.mode, taxonomy_version=cat.version, candidate_gaps=gaps, unresolved=unresolved,
                   prepass=prepass)
    # "rows" is the framework's own count (rows written): technique rows are "technique_rows" here
    run_counts = {("technique_rows" if k == "rows" else k): v for k, v in counts.items() if v}
    run_counts.update({"partial_docs": 1} if incomplete else {})
    run_counts.update({"repaired_docs": 1} if payload["repairs"] else {})
    run_counts.update(usage_counts([(call, r) for call, _, r in a.results]))
    return Answer(payload, llm=a.llm(), status="ok" if rows else "empty", counts=run_counts)


def window_fills(seg: Any, part: Any, *, last: bool, truncation: str) -> dict[str, str]:
    """The window call's user-part fills for ``part`` (a :class:`segment.Chunk` from :func:`segment.windows`)."""
    body = "\n\n".join(q.text for q in seg.paragraphs if q.kind == "body")
    return {"part": str(part.index), "parts": str(part.total), "opening": body[:WINDOW_OPENING],
            "section_labels": "; ".join(part.section_labels), "outline": segment.outline(seg) or "(no headings)",
            "chunk": segment.plain_chunk(part), "truncation": truncation if last else ""}


def _single_calls(a: _Ask, text: str, counts: dict[str, int], repairs: list[str]) -> Mapping[str, Any]:
    """The single mode's answer: one call on the raw text, or (pin ``window``) one call per part of a long body,
    merged by :func:`merge_answers`; with the pin ``glean``, each call is followed by its gleaning turn
    (:func:`glean_merge`); when that turn fails with one of :data:`GLEAN_FALLBACK_ERRORS`, the call's first answer
    stands. Returns the parsed answer to post-process."""
    p = a.p
    seg = segment.segment(text, cap=p.doc_cap) if p.window else None
    if seg is not None and seg.body_chars > p.window_min:
        parts = segment.windows(seg, single_max=p.window_min, target=WINDOW_TARGET, cap=WINDOW_MAX)
        pf = prompt_file(p.name, "window")
        trunc = doc_fills(text, p.doc_cap)["truncation"]
        asks = [("window", pf.user(window_fills(seg, part, last=part.index == part.total, truncation=trunc)),
                 str(part.index)) for part in parts]
        counts["windows"] = len(parts)
    else:
        spf = prompt_file(p.name, "single")
        asks = [("single", spf.user(single_fills(spf, text, p.doc_cap)), None)]
    answers: list[Mapping[str, Any]] = []
    for call, user, part in asks:
        res = a.call(call, user, part=part)
        answer: Mapping[str, Any] = res.parsed
        if p.glean:
            # the first call's conversation as sent: its user part, the degrade's schema turn when it engaged (so the
            # second prompt starts with the first one whole and the provider cache serves system + document), the answer
            fold = [fold_message(schema_of(p.name, call))] if res.degraded else []
            prior = [{"role": "user", "content": user}, *fold,
                     {"role": "assistant", "content": json.dumps(res.parsed, ensure_ascii=False)}]
            try:
                more = a.call("glean", prompt_file(p.name, "glean").user({}), part=part, prior=prior)
            except GLEAN_FALLBACK_ERRORS as exc:
                # the first answer stands (its cost and the failed call's are on the row); a budget, fatal or
                # config error still propagates, as from any other call
                repairs.append(f"glean_failed:{exc.error_class}")
                counts["glean_failed"] = counts.get("glean_failed", 0) + 1
            else:
                answer, added = glean_merge(res.parsed, more.parsed)
                repairs.extend(f"glean_added:{s}" for s in added)
                counts["glean_added"] = counts.get("glean_added", 0) + len(added)
                gfold = [fold_message(schema_of(p.name, "glean"))] if more.degraded else []
                prior = [*prior, {"role": "user", "content": prompt_file(p.name, "glean").user({})}, *gfold,
                         {"role": "assistant", "content": json.dumps(more.parsed, ensure_ascii=False)}]
        if p.sectors:
            if not p.glean:
                fold = [fold_message(schema_of(p.name, call))] if res.degraded else []
                prior = [{"role": "user", "content": user}, *fold,
                         {"role": "assistant", "content": json.dumps(res.parsed, ensure_ascii=False)}]
            answer = _sector_turns(a, answer, prior, counts, repairs)
        answers.append(answer)
    return answers[0] if len(answers) == 1 else merge_answers(answers)


def _sector_turns(a: _Ask, answer: Mapping[str, Any], prior: Sequence[Mapping[str, str]], counts: dict[str, int],
                  repairs: list[str]) -> Mapping[str, Any]:
    """The sector turns (pin ``sectors``): one call per sector of the set's sector file, each continuing the same
    conversation (the first answer and its gleaning turn: one cached prefix for every sector turn) and adding only
    slugs and extras not yet in the answer (:func:`glean_merge`; repair ``sector_added:<slug>``). A turn that fails
    with one of :data:`GLEAN_FALLBACK_ERRORS` is skipped (repair ``sector_failed:<id>:<error_class>``)."""
    sec = sectors_of(a.p.name, a.cat)
    pf = prompt_file(a.p.name, "sector")
    n = len(sec.sectors)
    for k, s in enumerate(sec.sectors, start=1):
        try:
            more = a.call("sector", pf.user(sector_fills(s, k, n, a.cat)), part=s.id, prior=prior)
        except GLEAN_FALLBACK_ERRORS as exc:
            repairs.append(f"sector_failed:{s.id}:{exc.error_class}")
            counts["sector_failed"] = counts.get("sector_failed", 0) + 1
            continue
        answer, added = glean_merge(answer, more.parsed)
        repairs.extend(f"sector_added:{x}" for x in added)
        counts["sector_added"] = counts.get("sector_added", 0) + len(added)
    return answer


def _slug_of(row: Any) -> str | None:
    return row["slug"].strip() if isinstance(row, Mapping) and isinstance(row.get("slug"), str) else None


def _name_of(row: Any) -> str | None:
    n = row.get("name_as_written") if isinstance(row, Mapping) else None
    return n.strip() if isinstance(n, str) and n.strip() else None


def _merge_row(base: dict[str, Any], other: Mapping[str, Any]) -> None:
    """``other`` into ``base`` (same slug or name): each integer role its maximum, quotes and flags unioned
    (quotes capped at :data:`MERGE_QUOTES`), the first non-empty note / gloss / hint kept."""
    roles = dict(base.get("roles")) if isinstance(base.get("roles"), Mapping) else {}
    for role, v in (other.get("roles") or {}).items() if isinstance(other.get("roles"), Mapping) else ():
        cur = roles.get(role)
        ints = all(isinstance(x, int) and not isinstance(x, bool) for x in (cur, v))
        if cur is None or (ints and v > cur):
            roles[role] = v
    base["roles"] = roles
    for key, cap in (("quotes", MERGE_QUOTES), ("flags", None)):
        seen = [x for x in (base.get(key) or []) if isinstance(base.get(key), list)]
        for x in other.get(key) or [] if isinstance(other.get(key), list) else []:
            if x not in seen:
                seen.append(x)
        base[key] = seen[:cap] if cap else seen
    for key in ("note", "gloss", "hint_technique"):
        if not base.get(key) and other.get(key):
            base[key] = other[key]


def _merge_rows(lists: Sequence[Any], key: Any) -> list[Any]:
    out: list[Any] = []
    index: dict[str, dict[str, Any]] = {}
    for rows in lists:
        for row in rows if isinstance(rows, list) else []:
            k = key(row)
            if k is None:
                out.append(row)  # malformed: normalize repairs or drops it
            elif k in index:
                _merge_row(index[k], row)
            else:
                index[k] = dict(row)
                out.append(index[k])
    return out


def merge_answers(answers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The window parts' answers as one answer, before ``judge.normalize`` (D614): ``techniques`` by slug and
    ``extra_techniques`` by name (:func:`_merge_row`), ``doc_kind`` from part 1, the sweeps' lists, ``not_graded``
    and the document ``flags`` concatenated (flags deduplicated). Pure."""
    sweeps: dict[str, list[Any]] = {}
    for ans in answers:
        sw = ans.get("sweeps")
        for k, v in sw.items() if isinstance(sw, Mapping) else ():
            if isinstance(v, list):
                sweeps.setdefault(k, []).extend(v)
    flags: list[Any] = []
    for ans in answers:
        for f in ans.get("flags") or [] if isinstance(ans.get("flags"), list) else []:
            if f not in flags:
                flags.append(f)
    return {"sweeps": sweeps, "doc_kind": answers[0].get("doc_kind"),
            "techniques": _merge_rows([a.get("techniques") for a in answers], _slug_of),
            "extra_techniques": _merge_rows([a.get("extra_techniques") for a in answers], _name_of),
            "not_graded": [e for a in answers for e in (a.get("not_graded") or [])
                           if isinstance(a.get("not_graded"), list)],
            "flags": flags}


def glean_merge(first: Mapping[str, Any], more: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The gleaning turn's rows added to the first answer (D615): pass 1 wins, so a slug or extra name it
    already has is ignored; returns the answer and the added slugs (in the turn's order). Pure."""
    out = dict(first)
    have = {s for s in map(_slug_of, first.get("techniques") or []) if s}
    names = {n for n in map(_name_of, first.get("extra_techniques") or []) if n}
    added: list[str] = []
    rows = list(first.get("techniques") or [])
    for row in more.get("techniques") or [] if isinstance(more.get("techniques"), list) else []:
        s = _slug_of(row)
        if s and s not in have:
            have.add(s)
            rows.append(row)
            added.append(s)
    extras = list(first.get("extra_techniques") or [])
    for row in more.get("extra_techniques") or [] if isinstance(more.get("extra_techniques"), list) else []:
        n = _name_of(row)
        if n and n not in names:
            names.add(n)
            extras.append(row)
    out["techniques"], out["extra_techniques"] = rows, extras
    return out, added


def _incomplete_error(pre: _Prepass | None, select_incomplete: bool) -> LlmError:
    """The error an incomplete document without rows is stored as (module docstring)."""
    cls = pre.error_class if pre is not None and pre.incomplete else "schema_invalid"
    what = "the pre-pass" if pre is not None and pre.incomplete else "the selection"
    msg = (f"{WORKFLOW_ID}: incomplete ({what} left parts unread{' and ids unanswered' if select_incomplete else ''})"
           f" and no row was graded; stored as a failure, not as an empty result")
    if cls == "truncated":
        return TruncatedOutput(msg)
    if cls == "too_large":
        return ContextTooLarge(msg)
    return SchemaValidationError(msg, [msg])


def _candidate_gaps(mentions: Sequence[Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for m in mentions:
        if m.none_reason == "off_taxonomy":
            gaps.append({"name": m.proposed_name or m.surface, "gloss": m.proposed_gloss,
                         "quotes": [{"text": m.snippet, "verified": True}] if m.snippet else [],
                         "reason": "off_taxonomy", "source": "selection"})
    for x in payload["extra_techniques"]:
        gaps.append({"name": x["name_as_written"], "gloss": x.get("gloss"), "quotes": list(x["quotes"]),
                     "reason": None, "source": "extra"})
    for f in payload["flags"]:
        if f.startswith(judge.MENU_GAP):
            gaps.append({"name": f[len(judge.MENU_GAP):], "gloss": None, "quotes": [], "reason": "menu_gap",
                         "source": "menu_gap"})
    return gaps


WORKFLOW = Workflow("technique_staged", ask=ask, prompt=prompt)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = ["CALLS", "DEFAULT_NAME", "DEFAULT_ROLE", "DEFAULT_TAXONOMY", "DOC_CAP_MAX", "DOC_CHAR_CAP", "FILE_SCHEMAS",
           "FINAL_SCHEMAS", "INVENTORY_FILES", "INVENTORY_SCHEMA", "JUDGE_PATH", "JUDGE_SHA256", "MAX_TOKENS",
           "MAX_TOKENS_MAX", "MAX_TOKENS_MIN", "MODES", "OPTIONS", "PIN_KEYS", "PROMPT_SETS", "PURE_SUBSTAGES",
           "SCHEMAS", "SUBSTAGES", "TRUNCATION_NOTE",
           "WORKFLOW", "Catalogue", "Pin", "ask", "catalogue", "doc_fills", "freeze_info", "merged_llm", "pin_of",
           "prepass_messages", "prompt", "prompt_file", "run", "schema_of", "system_template", "user_message",
           "versions_digest"]
