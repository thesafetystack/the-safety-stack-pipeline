"""``annotate.technique_roles`` — which catalogue techniques play which roles in this document?

Ported from the off-DB prototype (branch ``tomas/technique-roles``, df411eb: ``render.py``,
``rubric.md`` v0.3.1, ``prompts/a.md.j2``, the parse and clamps of
``tools/technique_roles_smoke.py``; round 5 T-ANN, D268), approach A only (NOTES: approach B dropped);
the critic, the adjudication and the technique×model binding are not ported. One invocation per
document of the build's ``fold.corpus`` file, ``basis_kind = 'text'``. The framework
(:mod:`stack.annotate`) does scope, prior-evidence carry, attempts, budget, the files and ``select``;
this module is the per-document ``ask`` and the prompt pin. Pieces: ``prompts/<name>.md`` (the
prompt, :mod:`stack.annotate.prompt_files`, D591), ``render.py`` (the taxonomy → the menu text),
``judge.py`` (the parsed reply → the stored payload, with every repair listed).

**The menu** is the whole technique taxonomy, :data:`TAXONOMY_PATH` (v0.8: 292 techniques, 17
families), loaded here by ``render.load_taxonomy`` and rendered by ``render.full_menu``;
``stack.config``'s ``TaxonomyConfig`` (pinned by ``configs/registry.yaml`` to v0.6) is not used (v0.8
fails it on ``aliases``). It is the prompt file's system-part fill ``taxonomy_menu``: one menu per
taxonomy file contents, the same for every document of a run.

**One call**: the pin's role and model, ``template`` = the prompt file's system part (rubric → menu →
output contract, then the client's ``{{input:document}}`` marker; byte-identical across the run, so the
client's cached ``system`` block), ``user`` = its user part with fills ``document`` (the window,
:data:`DOC_CHAR_CAP`) and ``truncation`` (:data:`TRUNCATION_NOTE` when the text is longer, else
empty), ``schema`` = ``judge.output_schema()`` (permissive, fixed-key optional ``roles``; D275, D290),
``max_tokens`` = :data:`MAX_TOKENS` (the prototype's first budget; the role's 8192 is below it).
``technique-roles-v2`` (rubric v0.4.0, the default) asks for a ``sweeps`` object first (deployment
claims, recommendations, controls discussed, menu gaps; optional in the schema, read by
``judge.normalize``, not stored); ``technique-roles-v1`` (rubric v0.3.1, the pre-prompt-file prompt
byte for byte) stays for A/B. Quotes are anchored against the whole text, strictly or on the loose
projection (``judge.loosen``: citations, LaTeX, table cells; counted ``loose_verified_quotes``). Status ``ok`` when a technique or extra technique row survives the
repairs, else ``empty`` (still an LLM answer: origin ``ai_check``), EXCEPT the empty-roles answer
(``judge.roles_degenerate``: rows present, every one dropped for an empty roles map, nothing kept;
R6 item 5): it raises :class:`~stack.llm.SchemaValidationError`, so the evidence row is
``failed_permanent[schema_invalid]`` carrying the call's cost, which the prior carry keeps (the
response cache holds the same answer, so a re-ask under the same work key could only replay it);
a pin, prompt-file, taxonomy or schema change re-asks it. Why it exists: claude-haiku-5.5 answered
``"roles": {}`` on every row against the earlier open-map schema, and 25 of 25 documents came
out ``empty`` with no failure anywhere. An answer with no rows at all stays ``empty`` (eval set
2026-10-13a: 2 of 25 gold documents have neither a technique nor an extra technique).

**Not ported**: the prototype's retry of a truncated reply with ``max_tokens`` 60000. The client
does it instead (D273, :class:`stack.llm.errors.TruncatedOutput`): a reply with ``finish_reason =
length`` is sent once more at twice the call's cap; cut off again, the unit is
``failed_permanent[truncated]`` with both attempts' cost, which the prior carry KEEPS: it is not
re-asked until its work key changes (a pin, prompt-file, taxonomy or text change).

**Provenance**: :func:`prompt`'s ``template_digests`` = the system part's template digest per model
(what the client writes as ``template_digest``; it covers rubric, menu and output contract), the
prompt file's digest (its bytes and its includes: the rubric) and the taxonomy file's sha256, so a
prompt, rubric or taxonomy edit changes ``config_digest`` and re-asks every document. ``ask`` refuses
to run when the taxonomy file no longer has the pinned sha256 (it changed since the run resolved its
pin). A render or judge code change does not change the digests: bump the registry ``version`` (2 since
the loose quote tier, the pipeline-set ``no_techniques_found`` and the ``menu_gap`` flags from the
sweeps).

**Plan pin** ``prompts.annotate.technique_roles``: ``name`` (selects ``prompts/<name>.md``, default
:data:`DEFAULT_NAME`, and is the ``prompt_name`` written; a name with a path separator or no such file
is refused before any call), ``role`` (``llm.yaml`` role, default :data:`DEFAULT_ROLE`), ``model``
(replaces the role's primary; the role's fallbacks stay) and the framework's optional
``template_digest``; other keys are refused. ``options.max_rows`` is the framework's.

Imports ``stack.*`` only, never another workflow; no dataclass (written when the stage loader ran this
file outside ``sys.modules``; registered since D435).
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

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
from stack.core.hashes import template_hash
from stack.llm import LicenseClass, SchemaValidationError
from stack.stage_runner import StageContext, StageError, StageResult, load_path_module

if TYPE_CHECKING:
    from stack.config import LlmConfig

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str) -> Any:
    """Import a sibling module by PATH (``workflows/`` is not a package)."""
    path = HERE / f"{name}.py"
    module = load_path_module(f"_technique_roles__{name}", path)  # registered in sys.modules (L1-8)
    if module is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"annotate.technique_roles: cannot load sibling module {path}")
    return module


judge = _load_sibling("judge")
render = _load_sibling("render")

WORKFLOW_ID = "annotate.technique_roles"
#: ``llm.yaml`` role when the plan pin names none.
DEFAULT_ROLE = "extract_main"
#: The prompt file when the plan pin names none (``prompts/<name>.md``).
DEFAULT_NAME = "technique-roles-v2"
PIN_KEYS = frozenset({"name", "role", "model", "template_digest"})
#: THE MENU SOURCE (D268): the only place this stage names its taxonomy. ``taxonomy/taxonomy-v0.8.yaml``
#: (the cleanup, 2e0b2a5, moved the prototype's ``taxman/taxonomy-v0.8.yaml`` here), read with
#: ``render.load_taxonomy``; NOT ``configs/registry.yaml``'s ``taxonomy`` pin (0.6, ``stack.config``'s
#: ``TaxonomyConfig``, which v0.8 fails on ``aliases``; that file says so next to the pin). Its sha256 is in
#: the config digest and in the eval sets' manifests (``frozen_with.taxonomy_sha256``).
TAXONOMY_PATH = HERE.parents[2] / "taxonomy" / "taxonomy-v0.8.yaml"
SCHEMA: dict[str, Any] = judge.output_schema()
#: The prototype's document window (= model_roles'): 300k chars ≈ 75k tokens.
DOC_CHAR_CAP = render.DOC_CHAR_CAP
#: The ``truncation`` fill of a text longer than :data:`DOC_CHAR_CAP` (the prototype template's line).
TRUNCATION_NOTE = "\n[DOCUMENT TRUNCATED AT CHARACTER CAP]\n"
#: The prototype's first output budget (``call_openrouter(max_tokens=16_000)``).
MAX_TOKENS = 16_000
#: The client's marker that ends the system part (where the user message goes); it is stripped from
#: the bytes sent and kept in the template digest (:mod:`stack.llm`).
INPUT_MARK = "{{input:document}}"


class Pin(NamedTuple):
    role: str
    model: str


class Menu(NamedTuple):
    """The loaded menu (one per taxonomy file contents): its slugs in taxonomy order, its text (the
    prompt file's ``taxonomy_menu`` fill), the file's sha256 and the taxonomy's ``version``."""

    slugs: tuple[str, ...]
    text: str
    taxonomy_sha256: str
    taxonomy_version: str


def pin_of(pin: Mapping[str, Any], llm: LlmConfig) -> Pin:
    """The role and model of plan pin ``pin``; unknown keys or role refuse (as model_roles')."""
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise StageError(f"{WORKFLOW_ID}: plan pin keys {extra} not in {sorted(PIN_KEYS)}")
    role = pin.get("role") or DEFAULT_ROLE
    if not isinstance(role, str) or role not in llm.roles:
        raise StageError(f"{WORKFLOW_ID}: plan pin role {role!r} is not an llm.yaml role")
    model = pin.get("model") or llm.roles[role].model
    if not isinstance(model, str):
        raise StageError(f"{WORKFLOW_ID}: plan pin model must be a string, got {model!r}")
    return Pin(role=role, model=model)


def prompt_file(name: object) -> PromptFile:
    """The prompt file plan-pin ``name`` selects (``None`` → :data:`DEFAULT_NAME`)."""
    return load_prompt(HERE / "prompts", DEFAULT_NAME if name is None else name, what=WORKFLOW_ID)


def menu() -> Menu:
    """The menu of the current taxonomy file (cached by its bytes, so an edit is seen)."""
    try:
        blob = TAXONOMY_PATH.read_bytes()
    except OSError as exc:
        raise StageError(f"{WORKFLOW_ID}: cannot read the taxonomy: {exc}") from exc
    return _menu(str(TAXONOMY_PATH), hashlib.sha256(blob).hexdigest())


@functools.lru_cache(maxsize=4)
def _menu(taxonomy: str, digest: str) -> Menu:
    tax = render.load_taxonomy(Path(taxonomy))
    if hashlib.sha256(Path(taxonomy).read_bytes()).hexdigest() != digest:
        raise StageError(f"{WORKFLOW_ID}: the taxonomy file changed while loading")
    return Menu(slugs=tuple(render.menu_slugs(tax)), text=render.full_menu(tax), taxonomy_sha256=digest,
                taxonomy_version=tax.version)


def system_template(pf: PromptFile, m: Menu) -> str:
    """The prompt file's system part with the menu filled in: the ``template`` of every call."""
    return pf.system({"taxonomy_menu": m.text})


def user_message(pf: PromptFile, text: str) -> str:
    """The prompt file's user part for one document (module docstring). Pure."""
    window, truncated = render.doc_window(text, DOC_CHAR_CAP)
    return pf.user({"document": window, "truncation": TRUNCATION_NOTE if truncated else ""})


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The prompt pin (no network): the system part's template digest for the pinned model and each
    of the role's fallbacks, the prompt file's digest and the taxonomy's sha256 (module docstring)."""
    p = pin_of(pin, llm)
    pf = prompt_file(pin.get("name"))
    m = menu()
    template = system_template(pf, m)
    role = llm.roles[p.role]
    pairs = []
    for model in dict.fromkeys([p.model, *role.fallbacks]):
        params: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS}
        if role.temperature is not None:
            params["temperature"] = role.temperature
        if role.reasoning_effort is not None:
            params["reasoning_effort"] = role.reasoning_effort
        pairs.append((template_hash(template, SCHEMA, params), model))
    digests = (*dict.fromkeys(d for d, _ in pairs), pf.digest, m.taxonomy_sha256)
    return PromptPin(name=pf.name, template_digests=digests, models=tuple(model for _, model in pairs))


def ask(item: AskItem, actx: AskContext) -> Answer:
    """Grade one document (module docstring)."""
    p = pin_of(actx.plan_pin, actx.config.llm)
    pf = prompt_file(actx.plan_pin.get("name"))
    m = menu()
    if m.taxonomy_sha256 not in actx.pin.template_digests:
        raise StageError(f"{WORKFLOW_ID}: {TAXONOMY_PATH.name} changed during the run (sha256 {m.taxonomy_sha256} "
                         f"is not among the pinned {list(actx.pin.template_digests)})")
    text = item.text or ""  # the framework skips a document without text (skipped[no_text])
    res = actx.client.complete(role=p.role, model=p.model, template=system_template(pf, m), refs={},
                               user=user_message(pf, text), schema=SCHEMA, max_tokens=MAX_TOKENS,
                               license_class=cast(LicenseClass, item.license_class), prompt_name=actx.pin.name,
                               basis_digest=item.basis_digest)
    anchorer = Anchorer.from_config(actx.config, workflow=WORKFLOW_ID)
    normalized = judge.normalize(res.parsed, slugs=m.slugs, text=text, anchorer=anchorer)
    if text.strip() and judge.roles_degenerate(normalized):
        dropped = sum(r.startswith("empty_roles_dropped:") for r in normalized["repairs"])
        exc = SchemaValidationError(
            f"{WORKFLOW_ID}: every row of the answer ({dropped}) has an empty roles map, nothing kept "
            f"(model {res.model}); stored as a failure, not as an empty result",
            [f"empty_roles_dropped x{dropped}"])
        exc.cost_usd = res.cost_usd  # type: ignore[attr-defined]  # the stage's failure row reads it
        exc.attempts = res.attempts  # type: ignore[attr-defined]
        raise exc
    payload = judge.to_payload(normalized)
    repairs = payload["repairs"]
    rows = [*payload["techniques"], *payload["extra_techniques"]]
    counts = {"repaired_docs": int(bool(repairs)),
              "rows": len(payload["techniques"]),
              "extra_rows": len(payload["extra_techniques"]),
              "quote_unverified_rows": sum(judge.QUOTE_UNVERIFIED in r["flags"] for r in rows),
              "dropped_no_quote": sum(r.startswith("no_quote_dropped:") for r in repairs),
              "loose_verified_quotes": sum(r.startswith("quote_verified_loose:") for r in repairs),
              "sweep_without_row": sum(r.startswith("sweep_without_row:") for r in repairs),
              "sweeps_absent": int(normalized["sweeps"] is None),
              "menu_gap_flags": sum(f.startswith(judge.MENU_GAP) for f in payload["flags"]),
              "truncated_docs": int(len(text) > DOC_CHAR_CAP)}
    return Answer(payload, llm=res, status="ok" if rows else "empty", counts={k: v for k, v in counts.items() if v})


WORKFLOW = Workflow("technique_roles", ask=ask, prompt=prompt)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = ["OPTIONS", "DEFAULT_NAME", "DEFAULT_ROLE", "DOC_CHAR_CAP", "MAX_TOKENS", "PIN_KEYS", "PURE_SUBSTAGES",
           "SCHEMA", "SUBSTAGES", "TAXONOMY_PATH", "TRUNCATION_NOTE", "WORKFLOW", "Menu", "Pin", "ask", "menu",
           "pin_of", "prompt", "prompt_file", "run", "system_template", "user_message"]
