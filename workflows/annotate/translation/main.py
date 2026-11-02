"""``annotate.translation`` — English machine translations of a non-English document's title and abstract
(D755; the design note docs/design/short-field-translation.md, option B).

One call per document in scope, on its metadata (``basis_kind = 'fields'``: ``metadata.fields_digest``, so an
edited title or abstract re-asks): the title and abstract go to the pinned model, which answers
``{from_lang, title_en, abstract_en}``. The originals stay in ``metadata``; the payload (``TRANSLATION``) adds the
English versions and ``method: llm`` beside them, and the record's metadata names the model and prompt. An answer
with ``from_lang = en`` (or nothing to translate) is ``empty``: nothing is stored as a translation. A ``fields``
override of target ``translation`` may set a human translation (``method: human``).

**Scope** is the plan line's ``filter`` (and ``reads``): plan ``batch-2026-10a`` asks the screen-relevant documents
whose title or abstract has a non-Latin letter or whose screen ``doc_class.primary_lang`` is not English
(:data:`SCOPE_FILTER`). Nothing here re-checks the language before the call: the scope decides who is asked, the
model's ``from_lang`` what is stored.

**Plan pin** ``prompts.annotate.translation``: ``name`` (the prompt name written; default
:data:`DEFAULT_PROMPT`), ``role`` (``llm.yaml`` role; default :data:`DEFAULT_ROLE`, gpt-6-luna) and ``model``
(replaces the role's primary: ``openai/gpt-5.6-terra`` is the documented stronger option). Other keys are refused.
The template is ``translate-short-v1.md``; its digest for the pinned model is the pin's template digest.

**Not here** (D755): collect cuts and identity matching do not read translations (Chinese terms are matched directly,
D744; a cross-language duplicate is joined by an identity override), and affiliations are not translated (the
organisation vocabulary has Chinese aliases instead, D756).
"""

from __future__ import annotations

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
from stack.core.hashes import template_hash
from stack.llm import LicenseClass, strict_schema
from stack.stage_runner import StageContext, StageError, StageResult

if TYPE_CHECKING:
    from stack.config import LlmConfig

HERE = Path(__file__).resolve().parent
WORKFLOW_ID = "annotate.translation"
DEFAULT_ROLE = "translate"
DEFAULT_PROMPT = "translate-short-v1"
PIN_KEYS = frozenset({"name", "role", "model"})
TEMPLATE = (HERE / f"{DEFAULT_PROMPT}.md").read_text("utf-8")
#: Characters of a title or abstract sent (a longer abstract is cut there; the translation covers what was sent).
MAX_ABSTRACT_CHARS = 6000
METHOD = "llm"
SCHEMA: dict[str, Any] = strict_schema({
    "type": "object",
    "additionalProperties": False,
    "required": ["from_lang", "title_en", "abstract_en"],
    "properties": {
        "from_lang": {"type": "string", "pattern": "^[a-z]{2,3}$"},
        "title_en": {"type": ["string", "null"], "maxLength": 2000},
        "abstract_en": {"type": ["string", "null"], "maxLength": 30000},
    },
})
#: Non-Latin letters (RE2 / duckdb): a title or abstract with one is in scope (Greek is left out: mathematics).
NON_LATIN_RE = r"[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}\p{Cyrillic}\p{Arabic}\p{Hebrew}\p{Thai}\p{Devanagari}]"
#: The scope plan batch-2026-10a gives the stage line (``reads: [screen, doc_class]``).
SCOPE_FILTER = (
    "annotations.screen.status = 'ok' AND annotations.screen.payload.verdict = 'relevant' AND ("
    f"regexp_matches(coalesce(metadata.title, '') || ' ' || coalesce(metadata.abstract, ''), '{NON_LATIN_RE}')"
    " OR lower(coalesce(annotations.doc_class.payload.primary_lang, 'en')) NOT LIKE 'en%')"
)


class Pin(NamedTuple):
    role: str
    model: str


def pin_of(pin: Mapping[str, Any], llm: LlmConfig) -> Pin:
    """The role and model of plan pin ``pin`` (module docstring); unknown keys or role refuse."""
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


def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin:
    """The prompt pin (no I/O): the template's digest for the pinned model and each of the role's fallbacks."""
    p = pin_of(pin, llm)
    role = llm.roles[p.role]
    pairs = []
    for m in dict.fromkeys([p.model, *role.fallbacks]):
        params: dict[str, Any] = {"model": m, "max_tokens": role.max_tokens}
        if role.temperature is not None:
            params["temperature"] = role.temperature
        if role.reasoning_effort is not None:
            params["reasoning_effort"] = role.reasoning_effort
        pairs.append((template_hash(TEMPLATE, SCHEMA, params), m))
    return PromptPin(name=pin.get("name") or DEFAULT_PROMPT,
                     template_digests=tuple(dict.fromkeys(d for d, _ in pairs)), models=tuple(m for _, m in pairs))


def user_message(fields: Mapping[str, Any]) -> str:
    """The per-document message: the title and (cut to :data:`MAX_ABSTRACT_CHARS`) the abstract."""
    title = str(fields.get("title") or "").strip()
    abstract = str(fields.get("abstract") or "").strip()[:MAX_ABSTRACT_CHARS]
    return f"title:\n{title}\n\nabstract:\n{abstract}" if abstract else f"title:\n{title}\n\nabstract: (none)"


def payload_of(parsed: Mapping[str, Any], has_abstract: bool) -> tuple[dict[str, Any], str]:
    """``(payload, status)`` from the model's answer: ``empty`` (all NULL but ``from_lang``) when the original is
    English or the answer translates nothing."""
    lang = str(parsed.get("from_lang") or "").strip().lower() or None
    title = (parsed.get("title_en") or "").strip() or None
    abstract = ((parsed.get("abstract_en") or "").strip() or None) if has_abstract else None
    if lang == "en" or (title is None and abstract is None):
        return {"from_lang": lang, "title_en": None, "abstract_en": None, "method": None}, "empty"
    return {"from_lang": lang, "title_en": title, "abstract_en": abstract, "method": METHOD}, "ok"


def ask(item: AskItem, actx: AskContext) -> Answer:
    """One document's translations (module docstring)."""
    p = pin_of(actx.plan_pin, actx.config.llm)
    fields = item.fields or {}
    res = actx.client.complete(
        role=p.role, model=p.model, template=TEMPLATE, refs={}, user=user_message(fields), schema=SCHEMA,
        license_class=cast(LicenseClass, item.license_class), prompt_name=actx.pin.name,
        basis_digest=item.basis_digest)
    parsed: Mapping[str, Any] = res.parsed or {}
    payload, status = payload_of(parsed, bool(str(fields.get("abstract") or "").strip()))
    return Answer(payload, llm=res, status=status, counts={f"translated.{status}": 1})


WORKFLOW = Workflow("translation", ask=ask, prompt=prompt)


def run(ctx: StageContext) -> StageResult:
    return run_annotate(ctx, WORKFLOW)


__all__ = ["OPTIONS", "PIN_KEYS", "PURE_SUBSTAGES", "SCOPE_FILTER", "SUBSTAGES", "WORKFLOW", "ask", "payload_of",
           "prompt", "run", "user_message"]
