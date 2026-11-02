"""``annotate.model_roles`` — which vocabulary models does each document name, and which roles do they play?

Ported from v3 ``workflows/annotate/model_roles/main.py`` and ``model_mentions/main.py`` (branch
``tomas/model-roles``, d011e5e; round 5 T-ANN, D235), single-judge mode (v3's shipped mode 1; the
verifier mode is not ported). D591 merged ``annotate.model_mentions`` into this stage: one invocation
per document of the build's ``fold.corpus`` file, on its current text (``basis_kind = 'text'``). The
framework (:mod:`stack.annotate`) does scope, prior-evidence carry, attempts, budget, the files and
``select``; this module is the per-document ``ask`` and the prompt pin. Pieces: ``match.py`` (the
LLM-free matcher over ``configs/models.yaml``), ``render.py`` (scan + vocabulary + text → the menu
and its text), ``prompts/<name>.md`` (the prompt, :mod:`stack.annotate.prompt_files`), ``judge.py``
(the parsed reply → the stored payload, every repair listed).

**One ask** (registry version 3: every document in scope is judged, whatever the scan found):
scan the whole text (:func:`match.Scanner.scan`); the payload's ``matches`` / ``ambiguous`` are every
hit (:func:`match.payload_fields`). The menu is v3's: the strong mentions plus the weak ones not among
them (flagged ``[WEAK MATCH]``), built against the window the judge sees (:data:`DOC_CHAR_CAP`; nodes
only past it are withheld and come back as ungraded ``beyond_window:<node>`` rows); it may be weak-only
or empty (counts ``menu_weak_only``, ``menu_empty``), and the prompt tells the judge what to do then
(report any named model under ``extra_models``). One call of the pin's role with the prompt file's system part as the
fixed prefix and its user part (document → rubric → menu → output spec, v3's calibrated order) as
the user message, with fills ``document`` (the window), ``truncation`` (:data:`TRUNCATION_NOTE` when
the text is longer) and ``menu`` (:func:`render.menu_text`); quotes are verified against the window;
each ``extra_models`` row gets the ``node_id`` its ``name_as_written`` resolves to
(:func:`match.resolve_name`); a model row's ``refined_name`` (the specific model the document
determines behind a family, weak or product-name mention) is resolved the same way and accepted only
in the menu node's lineage (:func:`refiner`), the row then grading that node with ``matched_node_id``
= the menu node. The matcher's hits (``match.payload_fields``) are the record ``model_regexp_matches``
of every answer (``Answer.records``; :func:`records`). Status ``empty`` when the judge graded nothing (no menu row with a role,
no variant or extra row), else ``ok``; an evidence row with the call either way. Before version 3
(D591) a document without a strong mention was answered without a call (``empty`` or ``ok`` with the
flag ``not_judged:no_strong_match``); since 2026-10-17 every document is judged, so off-catalogue models are found too.

**Provenance**: :func:`prompt`'s ``template_digests`` = the system part's template digest per model
(what the client writes as ``template_digest``), the prompt file's digest (its bytes and its
includes: the rubric) and the vocabulary file's sha256 (:func:`stack.modelvocab.vocab_digest`), so a
prompt, rubric or vocabulary edit changes ``config_digest`` and re-asks every document in scope (a
re-asked document whose menu did not change is served from the LLM cache). A matcher or judge code
change does not change the digests: bump the registry ``version`` (also in the config digest).

**Plan pin** ``prompts.annotate.model_roles``: ``name`` (selects ``prompts/<name>.md``, default
:data:`DEFAULT_NAME`, and is the ``prompt_name`` written; a name with a path separator or no such file
is refused before any call), ``role`` (``llm.yaml`` role, default :data:`DEFAULT_ROLE`), ``model``
(replaces the role's primary; the role's fallbacks stay), ``doc_cap`` (the characters of the text the judge
reads and the menu is built against, default :data:`DOC_CHAR_CAP`, at most :data:`DOC_CAP_MAX`; another value adds
the digest of ``{"doc_cap": N}`` to the template digests, so a default pin's digests are unchanged) and the
framework's optional ``template_digest``; other keys are refused.

Imports ``stack.*`` only, never another workflow.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

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
from stack.core.hashes import jcs, template_hash
from stack.llm import LicenseClass, strict_schema
from stack.modelvocab import Vocabulary, vocab_digest
from stack.stage_runner import StageContext, StageError, StageResult, load_path_module

if TYPE_CHECKING:
    from stack.config import LlmConfig

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str) -> Any:
    """Import a sibling module by PATH (``workflows/`` is not a package)."""
    path = HERE / f"{name}.py"
    module = load_path_module(f"_model_roles__{name}", path)  # registered in sys.modules (L1-8)
    if module is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"annotate.model_roles: cannot load sibling module {path}")
    return module


judge = _load_sibling("judge")
match = _load_sibling("match")
render = _load_sibling("render")

WORKFLOW_ID = "annotate.model_roles"
#: ``llm.yaml`` role when the plan pin names none (v3 had its own ``model_roles`` role on luna).
DEFAULT_ROLE = "extract_main"
#: The prompt file when the plan pin names none (``prompts/<name>.md``).
DEFAULT_NAME = "model-roles-v2"
PIN_KEYS = frozenset({"name", "role", "model", "template_digest", "doc_cap"})
#: The output schema sent and validated: ``judge.output_schema()`` in OpenAI strict mode's shape
#: (D185 (C); round 5 T-ANN: the open ``roles`` map drew a 400 and a degrade on every call).
SCHEMA: dict[str, Any] = strict_schema(judge.output_schema())
#: v3's doc cap (a cost and context decision: 300k chars ≈ 75k tokens).
DOC_CHAR_CAP = 300_000
#: The largest pin ``doc_cap`` (as technique_staged's: gpt-6-luna's context is 1,050,000 tokens, OpenRouter listing
#: 2026-10-23; above 272k prompt tokens that listing doubles the input price).
DOC_CAP_MAX = 1_000_000
#: The ``truncation`` fill of a text longer than :data:`DOC_CHAR_CAP` (v3's template line).
TRUNCATION_NOTE = "\n[DOCUMENT TRUNCATED AT CHARACTER CAP]\n"
#: The record of the matcher's hits each answer carries (registry ``produced_by="model_roles"``).
RECORD = "model_regexp_matches"
#: Output budget of one grading call (T-ANN real run R1, 2026-10-13: the role's 8,192 truncated the reply on a
#: document with a 30-node menu three times, failed_permanent[schema_invalid]; technique_roles sends 16,000).
MAX_TOKENS = 16_000


class Pin(NamedTuple):
    role: str
    model: str
    doc_cap: int = DOC_CHAR_CAP


def pin_of(pin: Mapping[str, Any], llm: LlmConfig) -> Pin:
    """The role and model of plan pin ``pin``; unknown keys or role refuse (as affiliation's)."""
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise StageError(f"{WORKFLOW_ID}: plan pin keys {extra} not in {sorted(PIN_KEYS)}")
    role = pin.get("role") or DEFAULT_ROLE
    if not isinstance(role, str) or role not in llm.roles:
        raise StageError(f"{WORKFLOW_ID}: plan pin role {role!r} is not an llm.yaml role")
    model = pin.get("model") or llm.roles[role].model
    if not isinstance(model, str):
        raise StageError(f"{WORKFLOW_ID}: plan pin model must be a string, got {model!r}")
    cap = pin.get("doc_cap", DOC_CHAR_CAP)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1_000 <= cap <= DOC_CAP_MAX:
        raise StageError(f"{WORKFLOW_ID}: plan pin doc_cap must be an integer from 1000 to {DOC_CAP_MAX}, got {cap!r}")
    return Pin(role=role, model=model, doc_cap=cap)


def prompt_file(name: object) -> PromptFile:
    """The prompt file plan-pin ``name`` selects (``None`` → :data:`DEFAULT_NAME`)."""
    return load_prompt(HERE / "prompts", DEFAULT_NAME if name is None else name, what=WORKFLOW_ID)


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The prompt pin (no network): the system part's template digest for the pinned model and each
    of the role's fallbacks, the prompt file's digest and the vocabulary's sha256 (module docstring)."""
    p = pin_of(pin, llm)
    pf = prompt_file(pin.get("name"))
    template = pf.system()
    role = llm.roles[p.role]
    pairs = []
    for m in dict.fromkeys([p.model, *role.fallbacks]):
        params: dict[str, Any] = {"model": m, "max_tokens": MAX_TOKENS}
        if role.temperature is not None:
            params["temperature"] = role.temperature
        if role.reasoning_effort is not None:
            params["reasoning_effort"] = role.reasoning_effort
        pairs.append((template_hash(template, SCHEMA, params), m))
    cap = () if p.doc_cap == DOC_CHAR_CAP else (hashlib.sha256(jcs({"doc_cap": p.doc_cap}).encode("utf-8")).hexdigest(),)
    digests = (*dict.fromkeys(d for d, _ in pairs), pf.digest, vocab_digest(), *cap)
    return PromptPin(name=pf.name, template_digests=digests, models=tuple(m for _, m in pairs))


def compiled(actx: AskContext) -> Any:
    """The run's vocabulary and scanner (:func:`match.scanner_for`), checked to be the vocabulary the
    pin hashed: a file that changed since the run resolved its pin fails the run (the config digest
    would not describe the answers)."""
    c = match.scanner_for(actx.config.config_dir)
    if c.vocab.digest not in actx.pin.template_digests:
        raise StageError(f"{WORKFLOW_ID}: configs/models.yaml changed during the run (sha256 {c.vocab.digest} "
                         f"is not among the pinned {list(actx.pin.template_digests)})")
    return c


def fills(text: str, scan: Mapping[str, Any], vocab: Vocabulary, cap: int = DOC_CHAR_CAP
          ) -> tuple[list[Any], list[str], dict[str, str]]:
    """``(menu, beyond, fills)`` for one document: the menu of the scan's strong and weak hits against
    the window (:func:`render.window_split`, :func:`render.build_menu`), the nodes withheld as beyond
    it, and the prompt's per-document fills ``document``, ``truncation``, ``menu`` (module docstring),
    which every prompt version fills alike (``stack eval model_roles`` freezes their digest at the default
    ``cap``; the pin's ``doc_cap`` replaces it). Pure."""
    nodes = render.vocab_view(vocab.validated)
    window = text[:cap]
    in_window, beyond = render.window_split({"mentions": scan["mentions"], "weak": scan["weak"]}, text, cap)
    menu = render.build_menu(in_window, nodes, window)
    return menu, beyond, {"document": window, "truncation": TRUNCATION_NOTE if len(text) > cap else "",
                          "menu": render.menu_text(menu)}


def user_message(pf: PromptFile, text: str, scan: Mapping[str, Any],
                 vocab: Vocabulary, cap: int = DOC_CHAR_CAP) -> tuple[list[Any], list[str], str]:
    """``(menu, beyond, user)``: :func:`fills` put into the prompt file's user part. Pure."""
    menu, beyond, f = fills(text, scan, vocab, cap)
    return menu, beyond, pf.user(f)


def in_lineage(nodes: Mapping[str, Any], menu_node: str, target: str) -> bool:
    """Whether a refinement of ``menu_node`` to ``target`` stays in its lineage: ``target`` is the node
    itself, a descendant of it, or a descendant of its parent (a sibling version: "Opus 5.5" matched as
    claude-opus-5 before the catalogue had it), by the catalogue's parent chain."""
    if target == menu_node or match.is_ancestor(nodes, menu_node, target):
        return True
    parent = (nodes.get(menu_node) or {}).get("parent")
    return bool(parent) and match.is_ancestor(nodes, parent, target)


def refiner(c: Any) -> Any:
    """``judge.normalize``'s ``refine``: a ``refined_name`` resolves with the matcher
    (:func:`match.resolve_name`, exactly one strong node) and is accepted in lineage (:func:`in_lineage`)."""
    def refine(menu_node: str, name: str) -> tuple[str | None, str]:
        target = match.resolve_name(c.scanner, name)
        if target is None:
            return None, "unresolved"
        if not in_lineage(c.vocab.nodes, menu_node, target):
            return None, f"not_in_lineage:{target}"
        return target, ""
    return refine


def records(_pin: Mapping[str, Any]) -> tuple[str, ...]:
    """Every pin produces the matcher's record (registry ``model_regexp_matches``)."""
    return (RECORD,)


def ask(item: AskItem, actx: AskContext) -> Answer:
    """Detect and grade one document (module docstring)."""
    c = compiled(actx)
    text = item.text or ""
    scan = c.scanner.scan(text)
    p = pin_of(actx.plan_pin, actx.config.llm)
    pf = prompt_file(actx.plan_pin.get("name"))
    menu, beyond, user = user_message(pf, text, scan, c.vocab, p.doc_cap)
    res = actx.client.complete(role=p.role, model=p.model, template=pf.system(), refs={}, user=user, schema=SCHEMA,
                               license_class=cast(LicenseClass, item.license_class), prompt_name=actx.pin.name,
                               basis_digest=item.basis_digest, max_tokens=MAX_TOKENS,
                               # D714: nothing continues this call, so only the system part is cached (on the
                               # OpenAI API; the document is not written at the cache-write rate)
                               cache_prefix_only=True)
    normalized = judge.normalize(
        res.parsed,
        menu_nodes=[e.node_id for e in menu],
        menu_names={e.node_id: e.name for e in menu},
        # denotation: the variant-vs-sibling guard in judge._derives_from turns on it (rubric §5)
        menu_kinds={e.node_id: e.denotation for e in menu},
        # quotes are verified against the WINDOW: the judge could not quote what it was not shown
        text=text[:p.doc_cap],
        beyond_window=beyond,
        refine=refiner(c),
    )
    payload = judge.to_payload(normalized)
    for x in payload["extra_models"]:
        x["node_id"] = match.resolve_name(c.scanner, x["name_as_written"])
    repairs = normalized["repairs"]
    graded = (any(any(m["roles"].values()) for m in payload["models"]) or payload["variant_models"]
              or payload["extra_models"])
    counts = {"repaired_docs": int(bool(repairs)),
              "quote_unverified": sum(r.startswith("quote_unverified:") for r in repairs),
              "beyond_window": len(beyond),
              "extra_resolved": sum(x["node_id"] is not None for x in payload["extra_models"]),
              "extra_models": len(payload["extra_models"]),
              "menu_empty": int(not menu),
              "menu_weak_only": int(bool(menu) and all(e.weak for e in menu)),
              "instruct_folded": sum(r.startswith("instruct_folded:") for r in repairs),
              "refined": sum(r.startswith("refined:") for r in repairs),
              "refinement_dropped": sum(r.startswith("refinement_dropped:") for r in repairs)}
    if not res.is_cache_hit:  # provider usage of the call (D714; run record only)
        counts.update({"calls.single": 1, "tokens.prompt.single": res.tokens_in,
                       "tokens.cached.single": res.tokens_cached or 0,
                       "tokens.cache_write.single": res.tokens_cache_write or 0,
                       "tokens.reasoning.single": res.tokens_reasoning or 0, "tokens.out.single": res.tokens_out})
    return Answer(payload, llm=res, status="ok" if graded else "empty",
                  counts={k: v for k, v in counts.items() if v},
                  records={RECORD: match.payload_fields(scan)})


WORKFLOW = Workflow("model_roles", ask=ask, prompt=prompt, records=records)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = ["OPTIONS", "DEFAULT_NAME", "DEFAULT_ROLE", "DOC_CAP_MAX", "DOC_CHAR_CAP", "PIN_KEYS", "RECORD", "in_lineage", "records", "refiner",
           "PURE_SUBSTAGES", "SCHEMA", "SUBSTAGES", "TRUNCATION_NOTE", "WORKFLOW", "Pin", "ask", "compiled", "fills", "pin_of",
           "prompt", "prompt_file", "run", "user_message"]
