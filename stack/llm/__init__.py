"""``stack.llm`` — the one LLM client: response cache and the run's cost ledger (D23, D36).

The one place a stage turns a prompt template + a document into a validated structured
completion, in this order:

    resolve role → egress gate → render fixed prefix (+ digests) → response-cache lookup
    (free: no reservation, no ledger line) → per attempt: ledger reservation (refused before
    the request when the run's budget would be crossed) → schema-constrained
    call → reconcile → retry / degrade / fallback → cache the schema-valid answer.

ADAPT of v3's ``stack/llm.py`` (9312c8c): prompt fill (``{{ref:}}``/``{{input:}}``, no Jinja2,
D23), :func:`render_call`, the ``cache_control`` prefix, the egress gate, retry/fallback, JSON
recovery and validation and :class:`UrllibTransport` are kept; v3's DB cache lookup and
``llm_calls`` rows are replaced by :mod:`stack.llm.cache` (``<store>/cache/llm-responses``) and
by :class:`LlmResult` carrying every LLM column of an evidence row (EVI, DESIGN A.1). No DB:
this package imports neither ``psycopg`` nor ``stack.core.{writer,db}`` nor ``stack.read``
(D40). Stages build the client with :func:`open_client`.

Invariants kept from v3:

* **Egress gate before any bytes leave**: every attempted model, primary and each fallback, is
  re-gated by its provider prefix against the ``license_class``'s sanctioned providers.
* **Rate limits from status/headers only**, never the reply text.
* **Transport is a seam**: :class:`Transport` is a Protocol; production uses
  :class:`UrllibTransport` (stdlib); tests use ``tests/fakes/llm.py``.
* **No lock over network I/O**: the transport, the backoff sleep and the cache write run
  unlocked; only the run ledger's in-memory critical section is serialised.
* **A transient failure fails one item, not the run**: one call gives up after
  ``max_call_failures`` (4) transport-level failures (network, 5xx, 429, an unexpected status)
  with :class:`AttemptsExhausted` (``failed_transient[network|http_5xx|rate_limit]``, the stage
  backs the item off); only the client-wide breaker (25 consecutive failures across calls over
  ≥ 120 s with no HTTP 200, D400) or an
  auth/credit status raises :class:`FatalLlmError` and stops the run.
* **Every reservation ends settled or released** (D165 (C)): an attempt answered with a status
  is reconciled; one that ended in a :class:`TransportError` (no response, no ``usage.cost``) is
  released at once; when a run ends, the stage runner calls :func:`stack.llm.ledger.close_run`
  before the final record, which releases whatever its clients left open (a stage that raised
  between reserve and settle; a batched reservation is reconciled at the run's ratio instead) and
  returns the run's ``spend`` (:func:`run_spend`). A crashed run's spend is its last tick's.

Digests on the result (the EVI columns; D-row "model_digest"):

* ``template_digest`` = v3 ``template_hash`` (rp1 over the unrendered template, schema, params);
* ``rendered_digest`` = ``sha256(jcs([rendered_hash, input_digest]))``: v3's rp1 over the
  rendered fixed prefix + schema + params, joined with the digest of the per-document ``user``
  text, so it names everything sent;
* ``model_digest`` = ``sha256(jcs(params))``, ``params = {model, max_tokens[, temperature]
  [, reasoning_effort]}`` of the answering attempt (an optional key enters only when the role sets it,
  so a role without it hashes as before; the role's ``reasoning_effort`` is sent as OpenRouter's
  ``reasoning: {effort}``);
* every digest (and the cache key) is computed at the CALL's ``max_tokens``, also when the answer
  came from the doubled-cap retry after a truncated reply (:class:`TruncatedOutput`): only the
  outbound request's cap changes, so a workflow's pinned ``template_digest`` still names it;
* ``response_digest`` = sha256 of the response body's sorted-key JSON.

**Batch mode** (:mod:`stack.llm.batch`, OpenRouter's ``/batches`` at half price): the driver
:func:`stack.llm.batch.prefetch` runs a stage's per-item asks in *collect* passes, where
:meth:`LlmClient.complete` answers from the response cache or this run's batch outcomes and
otherwise records the first request it would have sent and raises :class:`Deferred`; the driver
submits, polls and settles the batches through the same HTTP-200 code as the call loop (and the
same cache entry, plus ``via: batch``). Then :meth:`LlmClient.batch_replay` switches the client
to the *replay* pass, where the stage's normal loop gets every answer (or the outcome's error;
:class:`BatchUnanswered` when this run's batch gave none). The state lives in the one client
of one run; nothing but the response cache outlives it (the spend is in the run record).

**Providers** (D714, D722): each model reference of a call is routed (:meth:`stack.config.LlmConfig.route`) to the
default ``llm.provider`` (OpenRouter), a role's ``provider``, or the ``llm.providers`` entry its qualifier names
(``anthropic:claude-opus-5-5``). The provider's ``dialect`` shapes the wire request: ``openrouter`` and ``openai``
(chat completions) or ``anthropic`` (the Messages API, read through :func:`anthropic_view`). The reference as written
is the model in every digest and the cache key; the provider is not (a role's ``provider``, D714).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, TypeVar, runtime_checkable

from stack.core import hashes
from stack.llm.cache import ResponseCache, cache_key
from stack.llm.errors import (
    AttemptsExhausted,
    BatchUnanswered,
    BudgetExceeded,
    ContextTooLarge,
    Deferred,
    EgressRefused,
    FatalLlmError,
    LlmError,
    MissingRef,
    ModelRetired,
    ProviderRefused,
    RouteInvalid,
    SchemaValidationError,
    TransportError,
    TruncatedOutput,
)
from stack.llm.ledger import LLM_PROVIDER, AttemptKind, Ledger, Reservation, run_ledger, run_spend

if TYPE_CHECKING:
    from stack.config import EgressClass, LlmConfig, ModelPrice, ModelRoute, RoleConfig, StackConfig

__all__ = [
    "DEFAULT_WORKERS",
    "MAX_WORKERS",
    "Attempt",
    "AttemptsExhausted",
    "BatchUnanswered",
    "BudgetExceeded",
    "BudgetScope",
    "ContextTooLarge",
    "Deferred",
    "EgressRefused",
    "FatalLlmError",
    "Ledger",
    "LicenseClass",
    "LlmClient",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "LlmResult",
    "MissingRef",
    "ModelRetired",
    "ProviderRefused",
    "ResponseCache",
    "RouteInvalid",
    "SchemaValidationError",
    "Transport",
    "TransportError",
    "TruncatedOutput",
    "USAGE_COLUMNS",
    "UrllibTransport",
    "anthropic_view",
    "assemble_sse",
    "conversation_input",
    "env_transport",
    "estimate_tokens",
    "fill_nulls",
    "fold_message",
    "is_strict_schema",
    "make_default_transport",
    "open_client",
    "provider_key",
    "render_call",
    "run_calls",
    "run_spend",
    "strict_schema",
    "strip_inputs",
    "substitute_refs",
    "usage_details",
    "validate_json",
    "wire_max_tokens",
    "wire_schema",
]

_log = logging.getLogger("stack.llm")

# LicenseClass selects an egress rule (llm.yaml egress.by_license_class).
LicenseClass = Literal["open", "public_web", "restricted", "shadow", "api_tos"]

_MAX_SCHEMA_ATTEMPTS = 3  # the first attempt + ≤2 retries on schema-invalid output.
_MAX_CONSECUTIVE_FAILURES = 25  # ≥25 consecutive transport failures (across calls) …
#: … AND a failure streak (no HTTP 200 since its first failure) of at least this many seconds ⇒
#: abort. The count alone was sized for v3's one worker; at 32 workers one round of in-flight
#: calls failing in a ~1 s 429/5xx/network blip reaches it (review L5-1, D400).
_MIN_FAILURE_STREAK_S = 120.0
_MAX_CALL_FAILURES = 4  # transport-level failures of one call ⇒ that item fails (AttemptsExhausted).
#: How many calls a stage asks at once over one client (D692; 1024 since D716, was 256): the bound of every LLM stage line's
#: ``options.workers`` (annotate, triage) and of ``stack eval --workers``.
MAX_WORKERS = 1024
#: The count when a stage line or ``stack eval`` sets none (D692): cautious next to the plans' 256 and 1024.
DEFAULT_WORKERS = 64
#: ``module:callable`` returning the :class:`Transport` :func:`open_client` uses instead of the
#: production one (tests and offline scenario runs: ``tests.fakes.llm:installed``).
TRANSPORT_ENV = "STACK_LLM_TRANSPORT"
#: A dotenv-style file the client reads the provider key from when ``$<api_key_env>`` is unset
#: (D160 (C)): the operator passes a *path*, so the key is never on a command line, in shell
#: history, or exported to processes that do not need it (:func:`provider_key`).
ENV_FILE_ENV = "STACK_ENV_FILE"

T = TypeVar("T")


@dataclass(frozen=True)
class LlmRequest:
    """One physical request to the transport counterparty (OpenRouter). ``model`` and
    ``attempt`` are carried alongside the wire ``payload`` so a fake can fingerprint the call
    without re-parsing the body."""

    url: str
    headers: Mapping[str, str]
    payload: Mapping[str, Any]
    model: str
    attempt: int
    rendered_hash: str
    input_digest: str
    #: The env var NAME of this request's key when it is not the transport's default (a role's
    #: ``provider``, D714); never the key itself.
    api_key_env: str | None = None
    #: The request's socket timeout when not the transport's (``providers.<p>.timeout_s``, D714).
    timeout_s: float | None = None
    #: The header the key goes in, when not ``Authorization: Bearer`` (the Anthropic API's ``x-api-key``, D722).
    key_header: str | None = None


@dataclass(frozen=True)
class LlmResponse:
    """A transport response. A non-2xx HTTP status is returned as a response (so the client can
    classify 401/402/403/429/400), not raised; only a genuine network failure raises
    :class:`TransportError`."""

    status: int
    headers: Mapping[str, str]
    body: Mapping[str, Any]


@runtime_checkable
class Transport(Protocol):
    """The HTTP seam. ``post`` returns an :class:`LlmResponse` for any HTTP status; it raises
    :class:`TransportError` only on a network-level failure (connection/timeout)."""

    def post(self, request: LlmRequest) -> LlmResponse: ...


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Attempt:
    """One outbound attempt of a completion, with its ledger reservation. ``status`` is the HTTP
    status (None after a transport error); ``error_class`` uses the §2.6 vocabulary where one
    fits (``rate_limit``, ``http_5xx``, ``network``, ``provider_auth``, ``too_large``,
    ``parse_error``, ``schema_invalid``, ``truncated``) and ``http_<status>`` otherwise
    (``http_400`` is the strict-schema degrade). ``cost_usd`` is the reconciled actual (0.0 for a released
    reservation: a transport error), None while it is open."""

    model: str
    kind: str
    status: int | None
    error_class: str | None
    reservation_id: str
    reserved_usd: float
    cost_usd: float | None
    latency_ms: int


@dataclass(frozen=True)
class LlmResult:
    """One schema-valid completion: the parsed JSON plus every LLM column of an evidence row
    (EVI, DESIGN A.1) and the attempt list (retries, fallbacks; empty on a cache hit).

    ``cost_usd`` is the reconciled cost of every attempt of this call (0 on a cache hit);
    ``tokens_in``/``tokens_out``/``latency_ms`` are the answering attempt's (a hit reports the
    cached call's tokens and latency 0). ``degraded`` = the strict-schema 400 degrade engaged;
    ``unwrapped`` = the reply was fenced/prose-wrapped JSON; ``provider_anomaly`` = OpenRouter
    served via a provider outside the sanctioned egress set (logged; the call is not failed).
    """

    parsed: Mapping[str, Any]
    model: str
    prompt_name: str | None
    template_digest: str
    model_digest: str
    rendered_digest: str
    response_digest: str
    basis_digest: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    latency_ms: int
    is_cache_hit: bool
    attempts: tuple[Attempt, ...] = ()
    tokens_in_fixed: int = 0
    degraded: bool = False
    unwrapped: bool = False
    provider_anomaly: bool = False
    #: The answering attempt's provider usage details (D714; None when the reply does not report them, and on a
    #: cache hit): prompt tokens read from / written to the provider's prompt cache
    #: (``prompt_tokens_details.cached_tokens`` / ``cache_write_tokens``) and reasoning tokens
    #: (``completion_tokens_details.reasoning_tokens``, part of ``tokens_out``).
    tokens_cached: int | None = None
    tokens_cache_write: int | None = None
    tokens_reasoning: int | None = None

    def usage_fields(self) -> dict[str, int | None]:
        """The usage-detail evidence columns (:data:`USAGE_COLUMNS`; outside EVI and every digest)."""
        return {"tokens_cached": self.tokens_cached, "tokens_cache_write": self.tokens_cache_write,
                "tokens_reasoning": self.tokens_reasoning}

    @property
    def ok(self) -> bool:
        """Always True: a failed completion raises instead of returning."""
        return True

    def evidence_fields(self) -> dict[str, Any]:
        """The EVI columns this client owns (A.1), keyed by column name."""
        return {
            "is_cache_hit": self.is_cache_hit, "template_digest": self.template_digest,
            "model_digest": self.model_digest, "prompt_name": self.prompt_name,
            "rendered_digest": self.rendered_digest, "model": self.model,
            "response_digest": self.response_digest, "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Pricing and digests
# ─────────────────────────────────────────────────────────────────────────────


def wire_max_tokens(payload: Mapping[str, Any]) -> int:
    """A request's output cap: ``max_tokens`` (OpenRouter) or ``max_completion_tokens`` (the OpenAI API, D714)."""
    return int(payload["max_tokens"] if "max_tokens" in payload else payload["max_completion_tokens"])


#: The usage-detail columns of :meth:`LlmResult.usage_fields` (D714).
USAGE_COLUMNS: tuple[str, ...] = ("tokens_cached", "tokens_cache_write", "tokens_reasoning")


#: Characters a BPE tokenizer spends about one token on (the Chinese-coverage work): Han ideographs, kana, Hangul syllables and
#: jamo, CJK and fullwidth punctuation. Current tokenizers take 0.6-1.5 tokens per Chinese character, so char/4
#: under-reserved a Chinese prompt about fourfold; one token per character errs high, as a reservation should.
_ONE_TOKEN_CHARS = re.compile(
    "[\u1100-\u11ff\u2e80-\u2fdf\u3000-\u303f\u3040-\u30ff\u3100-\u31ff\u3400-\u4dbf\u4e00-\u9fff"
    "\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef\U00020000-\U0003134f]")


def estimate_tokens(text: str) -> int:
    """A cheap token estimate (never zero for non-empty text): one token per CJK character
    (:data:`_ONE_TOKEN_CHARS`), char/4 over the rest; text without CJK estimates as before (char/4). Used only
    for the reservation and when the provider omits a usage count."""
    if not text:
        return 0
    cjk = len(_ONE_TOKEN_CHARS.findall(text)) if not text.isascii() else 0
    return max(1, math.ceil((len(text) - cjk) / 4) + cjk)


#: Per-request token overhead and safety factor of the reservation's input estimate (D110, amends D36).
RESERVE_OVERHEAD_TOKENS = 64
RESERVE_SAFETY_FACTOR = 1.25


def reserve_tokens_in(request: LlmRequest) -> int:
    """The reservation's input-token estimate: char/4 over the **full serialized request body**
    (system prefix, every message, the response_format JSON schema, provider block), plus a
    fixed per-request overhead, times a safety factor. Observed (T4.1 real smoke, D110):
    a request the old prefix+user estimate put at 19 tokens was billed at 166."""
    body = json.dumps(dict(request.payload), ensure_ascii=False)
    return math.ceil((estimate_tokens(body) + RESERVE_OVERHEAD_TOKENS) * RESERVE_SAFETY_FACTOR)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _conversation(prior: Sequence[Mapping[str, str]], user: str) -> list[dict[str, Any]]:
    """The variable message chain: ``prior`` turns (checked) then the ``user`` message."""
    turns: list[dict[str, Any]] = []
    for t in prior:
        if t.get("role") not in ("user", "assistant") or not isinstance(t.get("content"), str):
            raise LlmError(f"prior turn must be {{role: user|assistant, content: str}}, got keys {sorted(t)}")
        turns.append({"role": t["role"], "content": t["content"]})
    return [*turns, {"role": "user", "content": user}]


def fold_message(schema: Mapping[str, Any]) -> dict[str, str]:
    """The user turn the strict-schema degrade appends after the per-call messages. A caller continuing a
    degraded call's conversation (``prior``, D615) repeats it, so the continuation's prompt starts with the
    first call's whole prompt and the provider's prompt cache can serve it."""
    return {"role": "user", "content": ("Return ONLY a JSON object that conforms to this JSON Schema "
                                        "(no prose, no code fences):\n" + hashes.jcs(dict(schema)))}


def conversation_input(prior: Sequence[Mapping[str, str]], user: str) -> str:
    """The text whose ``input_digest`` names a call's variable part: ``user`` alone without ``prior`` turns
    (every single-turn digest unchanged), else the canonical JSON of the whole chain."""
    return user if not prior else hashes.jcs(_conversation(prior, user))


def rendered_digest(rendered_hash: str, input_digest: str) -> str:
    """``sha256(jcs([rendered_hash, input_digest]))``: the fixed prefix (+ schema, params) and
    the per-document user text together."""
    return _sha(hashes.jcs([rendered_hash, input_digest]))


def model_digest(params: Mapping[str, Any]) -> str:
    """``sha256(jcs(params))`` over the sampling params ``{model, max_tokens[, temperature]
    [, reasoning_effort]}``."""
    return _sha(hashes.jcs(dict(params)))


def response_digest(body: Mapping[str, Any]) -> str:
    return _sha(json.dumps(body, ensure_ascii=False, sort_keys=True))


# ─────────────────────────────────────────────────────────────────────────────
# Rendering — the fixed prefix, the two hashes, the sent form (D23)
# ─────────────────────────────────────────────────────────────────────────────

_REF_RE = re.compile(r"\{\{ref:([a-z0-9_]+)\}\}")
_INPUT_RE = re.compile(r"\{\{input:([a-z0-9_]+)\}\}")


def substitute_refs(template: str, refs: Mapping[str, str]) -> str:
    """Replace every ``{{ref:<kind>}}`` with ``refs[kind]``; a placeholder with no ``refs``
    entry raises :class:`MissingRef`. ``{{input:<name>}}`` placeholders are left LITERAL — they
    are part of the per-call user message, not the fixed prefix, and pass through the hash
    recipes untouched (20-technical §3)."""

    def _sub(m: re.Match[str]) -> str:
        kind = m.group(1)
        if kind not in refs:
            raise MissingRef(
                f"template references {{{{ref:{kind}}}}} but refs has no '{kind}' "
                f"(supplied: {sorted(refs)})"
            )
        return refs[kind]

    return _REF_RE.sub(_sub, template)


def strip_inputs(text: str) -> str:
    """Remove ``{{input:<name>}}`` placeholders (the per-call fills). DECISION: the hash form
    keeps them literal (20-technical §3), but the bytes SENT to the provider must not contain
    placeholder tokens — the real per-doc content is the separate ``user`` block — so the sent
    fixed prefix strips them. The two forms are 1:1 (the placeholders are constant), so the
    cache key stays sound."""
    return _INPUT_RE.sub("", text)


@dataclass(frozen=True)
class Rendered:
    """The pure output of rendering one call (no I/O): the hash subjects, the sent prefix, and
    the sampling params. Exposed so hashing is testable to golden values without a DB."""

    prefix: str  # rendered fixed prefix (refs substituted, inputs literal) — the rendered_hash subject
    sent_prefix: str  # prefix with inputs stripped — the bytes sent in the cached system block
    rendered_hash: str
    template_hash: str
    params: dict[str, Any]


def render_call(
    template: str,
    refs: Mapping[str, str],
    *,
    model: str,
    max_tokens: int,
    schema: Mapping[str, Any],
    temperature: float | None = None,
    reasoning_effort: str | None = None,
) -> Rendered:
    """Render the fixed prefix and compute both hashes for one ``(template, refs, model, params)``
    — a pure function (D23).

    ``rendered_hash`` = rp1 over the rendered fixed prefix (refs substituted, inputs literal);
    ``template_hash`` = rp1 over the unrendered template (versionless ``{{ref:…}}`` intact).
    ``params`` = ``{model, max_tokens[, temperature][, reasoning_effort]}`` (the §3 sampling params;
    an optional key only when set, so the digests of a call without it are unchanged).
    """
    prefix = substitute_refs(template, refs)
    params: dict[str, Any] = {"model": model, "max_tokens": max_tokens}
    if temperature is not None:
        params["temperature"] = temperature
    if reasoning_effort is not None:
        params["reasoning_effort"] = reasoning_effort
    return Rendered(
        prefix=prefix,
        sent_prefix=strip_inputs(prefix),
        rendered_hash=hashes.rendered_hash(prefix, schema, params),
        template_hash=hashes.template_hash(template, schema, params),
        params=params,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Minimal client-side JSON-Schema validator (retry / degrade decisions)
# ─────────────────────────────────────────────────────────────────────────────


def validate_json(instance: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """A DELIBERATELY MINIMAL JSON-Schema check (returns a list of error strings; empty = valid).

    It covers the keywords the stack's payload schemas actually use — ``type``, ``enum``,
    ``const``, ``required``, ``properties``, ``additionalProperties: false``, ``items``,
    ``min/maxItems``, ``min/maxLength``, ``minimum``/``maximum``, and lenient ``anyOf``/``oneOf`` —
    and treats unknown keywords (notably a cross-file ``$ref``) LENIENTLY (no error): its job is
    to decide "does the reply match the requested schema well enough to accept" for the §8 retry
    and degrade paths, not to be a conformant Draft 2020-12 validator. OPEN QUESTION: swap in
    ``jsonschema`` with a ``referencing`` registry if strict ``$ref`` resolution is needed.
    """
    errs: list[str] = []

    if "$ref" in schema:  # cross-file ref (e.g. _core.schema.json) — not resolved here.
        return errs

    if "const" in schema and instance != schema["const"]:
        errs.append(f"{path}: {instance!r} != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: {instance!r} not in enum {schema['enum']}")

    for combiner in ("anyOf", "oneOf"):
        if combiner in schema:
            subs = schema[combiner]
            if isinstance(subs, list) and subs and not any(
                not validate_json(instance, s, path) for s in subs if isinstance(s, Mapping)
            ):
                errs.append(f"{path}: matches no {combiner} branch")

    typ = schema.get("type")
    if typ is not None:
        allowed = [typ] if isinstance(typ, str) else list(typ)
        if not any(_type_ok(instance, t) for t in allowed):
            errs.append(f"{path}: {_typename(instance)} is not one of type {allowed}")
            return errs  # a type mismatch makes the nested checks meaningless

    if isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append(f"{path}: string longer than maxLength {schema['maxLength']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append(f"{path}: string shorter than minLength {schema['minLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append(f"{path}: {instance} > maximum {schema['maximum']}")

    if isinstance(instance, Mapping):
        for req in schema.get("required", []):
            if req not in instance:
                errs.append(f"{path}: missing required property '{req}'")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                errs.append(f"{path}: unexpected properties {sorted(extra)}")
        for key, subschema in props.items():
            if key in instance and isinstance(subschema, Mapping):
                errs.extend(validate_json(instance[key], subschema, f"{path}.{key}"))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errs.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errs.append(f"{path}: more than maxItems {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, elem in enumerate(instance):
                errs.extend(validate_json(elem, items, f"{path}[{i}]"))

    return errs


# ─────────────────────────────────────────────────────────────────────────────
# Strict structured-output schemas (OpenAI strict mode via OpenRouter)
# ─────────────────────────────────────────────────────────────────────────────

#: The keywords a ``response_format`` schema carries on the wire (:func:`wire_schema`): a
#: conservative subset OpenAI's strict mode accepts. ``minimum``/``maximum`` are kept because
#: triage's schema sends them and is accepted (real run ``rxvbysx065a6m``: no 400, no degrade).
WIRE_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "enum", "items",
                           "anyOf", "description", "minimum", "maximum"})
#: The Anthropic API's structured outputs take no numerical constraints (``minimum`` / ``maximum``; D722): its SDKs
#: strip them and validate client-side, as :func:`validate_json` does here.
ANTHROPIC_WIRE_KEYWORDS = WIRE_KEYWORDS - {"minimum", "maximum"}
#: Annotation-only keywords :func:`strict_schema` drops (they constrain nothing).
_ANNOTATIONS = frozenset({"$schema", "$id", "$comment", "title", "examples", "default"})


def is_strict_schema(schema: Mapping[str, Any]) -> bool:
    """Whether ``schema`` is already in strict structured-output shape (:func:`strict_schema` leaves it unchanged);
    OpenAI rejects a strict ``response_format`` that is not (HTTP 400, e.g. an object without
    ``additionalProperties: false``), D714."""
    try:
        return strict_schema(schema) == dict(schema)
    except ValueError:
        return False


def strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` in the shape OpenAI's strict structured outputs require (D185 (C)), pure and
    idempotent: every object is closed (``additionalProperties: false``) and lists ALL its
    properties in ``required``; a property that was optional becomes nullable (a scalar's
    ``type`` gains ``"null"``, and ``None`` joins its ``enum``; an object or array becomes
    ``anyOf: [<it>, {"type": "null"}]``), so the model answers ``null`` where it used to omit
    the key; an ``enum`` of strings without a ``type`` gets ``"type": "string"``; annotation
    keywords (``$schema``, ``title``, ``x-*``, …) are dropped. Validation keywords the wire does
    not carry (``maxLength``, ``minItems``, …) are KEPT: the client validates every answer
    against this schema, so they still hold client-side; :func:`wire_schema` drops them from the
    request. A ``$ref`` raises (inline it first: strict mode resolves no cross-file ref).

    Readers of the answer treat ``null`` as "absent" (``p.get(k) or default``)."""
    return _strict(schema, nullable=False, where="$")


def _strict(schema: Mapping[str, Any], *, nullable: bool, where: str) -> dict[str, Any]:
    if "$ref" in schema:
        raise ValueError(f"strict_schema: {where} has a $ref ({schema['$ref']!r}); inline it")
    out: dict[str, Any] = {k: v for k, v in schema.items()
                           if k not in _ANNOTATIONS and not k.startswith("x-") and k != "$defs"}
    if "anyOf" in out:
        out["anyOf"] = [_strict(s, nullable=False, where=f"{where}|{i}") for i, s in enumerate(out["anyOf"])]
    if "items" in out and isinstance(out["items"], Mapping):
        out["items"] = _strict(out["items"], nullable=False, where=f"{where}[]")
    if "enum" in out and "type" not in out and all(isinstance(v, str) for v in out["enum"] if v is not None):
        out["type"] = "string"
    types = [out["type"]] if isinstance(out.get("type"), str) else list(out.get("type") or [])
    if "properties" in out or "object" in types:
        props = out.get("properties") or {}
        required = set(out.get("required") or [])
        out["properties"] = {k: _strict(v, nullable=k not in required, where=f"{where}.{k}")
                             for k, v in props.items()}
        out["required"] = list(props)
        out["additionalProperties"] = False
    if not nullable or "null" in types or any(s == {"type": "null"} for s in out.get("anyOf", [])):
        return out
    if "object" in types or "array" in types or "anyOf" in out or not types:
        return {"anyOf": [out, {"type": "null"}]}
    out["type"] = [*types, "null"]
    if "enum" in out and None not in out["enum"]:
        out["enum"] = [*out["enum"], None]
    return out


def fill_nulls(instance: Any, schema: Mapping[str, Any]) -> Any:
    """``instance`` with every ABSENT property whose subschema admits ``null`` set to ``None``,
    recursively (``properties``, ``items``, the ``anyOf`` branch of the instance's type). The
    client applies it before validating an answer, so a reply on the degrade path (schema folded
    into the prompt, nothing enforced by the provider) that omits an optional key of a
    :func:`strict_schema` reads as that key ``null``, as strict mode would have produced, instead
    of a schema-invalid retry. A missing key whose schema does not admit ``null`` stays missing
    (the validator rejects it)."""
    if isinstance(instance, Mapping):
        sub = _branch(schema, "object")
        props = sub.get("properties")
        if not isinstance(props, Mapping):
            return instance
        out = dict(instance)
        for key, ps in props.items():
            if not isinstance(ps, Mapping):
                continue
            if key in out:
                out[key] = fill_nulls(out[key], ps)
            elif _admits_null(ps):
                out[key] = None
        return out
    if isinstance(instance, list):
        items = _branch(schema, "array").get("items")
        return [fill_nulls(e, items) for e in instance] if isinstance(items, Mapping) else instance
    return instance


def _branch(schema: Mapping[str, Any], kind: str) -> Mapping[str, Any]:
    """``schema`` itself, or its first ``anyOf`` branch of type ``kind`` (a nullable object/array)."""
    for sub in schema.get("anyOf") or []:
        if isinstance(sub, Mapping) and (sub.get("type") == kind or kind in (sub.get("type") or [])):
            return sub
    return schema


def _admits_null(schema: Mapping[str, Any]) -> bool:
    typ = schema.get("type")
    return (typ == "null" or (isinstance(typ, list) and "null" in typ)
            or any(isinstance(s, Mapping) and _admits_null(s) for s in schema.get("anyOf") or []))


def wire_schema(schema: Mapping[str, Any], keywords: frozenset[str] = WIRE_KEYWORDS) -> dict[str, Any]:
    """The ``response_format`` form of ``schema``: only ``keywords`` (:data:`WIRE_KEYWORDS`; the Anthropic dialect's
    :data:`ANTHROPIC_WIRE_KEYWORDS`), recursively (into ``properties`` values, ``items`` and ``anyOf`` branches).
    Everything else stays client-side (:func:`validate_json` checks the full schema). Applied to every
    schema-constrained request."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in keywords:
            continue
        if key == "properties" and isinstance(value, Mapping):
            value = {k: wire_schema(v, keywords) if isinstance(v, Mapping) else v for k, v in value.items()}
        elif key == "items" and isinstance(value, Mapping):
            value = wire_schema(value, keywords)
        elif key == "anyOf" and isinstance(value, list):
            value = [wire_schema(s, keywords) if isinstance(s, Mapping) else s for s in value]
        out[key] = value
    return out


def _type_ok(instance: Any, t: str) -> bool:
    if t == "object":
        return isinstance(instance, Mapping)
    if t == "array":
        return isinstance(instance, list)
    if t == "string":
        return isinstance(instance, str)
    if t == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if t == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if t == "boolean":
        return isinstance(instance, bool)
    if t == "null":
        return instance is None
    return True  # unknown type token → lenient


def _typename(instance: Any) -> str:
    if isinstance(instance, bool):
        return "boolean"
    if isinstance(instance, Mapping):
        return "object"
    if isinstance(instance, list):
        return "array"
    if isinstance(instance, str):
        return "string"
    if isinstance(instance, int):
        return "integer"
    if isinstance(instance, float):
        return "number"
    if instance is None:
        return "null"
    return type(instance).__name__


# ─────────────────────────────────────────────────────────────────────────────
# Provider response parsing (OpenRouter / OpenAI-compatible)
# ─────────────────────────────────────────────────────────────────────────────


# A ```lang fenced block: greedy inner capture so an INNER ``` inside a JSON string value is not
# mistaken for the closing fence (greedy runs to the LAST ```). DOTALL so it spans newlines.
_FENCE_BLOCK = re.compile(r"```[A-Za-z0-9_+.-]*[ \t]*\r?\n(.*)```", re.DOTALL)


def _recover_json_object(content: str) -> tuple[Mapping[str, Any] | None, bool]:
    """Parse ``content`` into a JSON object, tolerating a markdown code fence / prose wrapper
    (the known v1/taxman lesson — some providers, e.g. Amazon Bedrock via OpenRouter, silently
    ignore the structured-output constraint and wrap valid JSON in a ```json fence).

    Order matters for the "fence-inside-a-string-value must survive" case:

    1. try the content AS-IS — so JSON whose string values legitimately contain ``` parses
       untouched and ``unwrapped`` stays False (never over-strip valid JSON);
    2. else strip a ```…``` fence (greedy inner, so an inner ``` is not the close) and parse;
    3. else slice first ``{`` … last ``}`` and parse.

    Returns ``(obj, unwrapped)`` — ``obj`` is None on total failure; ``unwrapped`` is True iff
    step 2 or 3 recovered it (a non-bare-JSON reply, worth flagging on the result).
    """
    obj = _loads_object(content)
    if obj is not None:
        return obj, False  # bare JSON — the ``` (if any) is inside a string value; do not strip
    m = _FENCE_BLOCK.search(content)
    if m is not None:
        obj = _loads_object(m.group(1).strip())
        if obj is not None:
            return obj, True
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        obj = _loads_object(content[start : end + 1])
        if obj is not None:
            return obj, True
    return None, False


def _loads_object(text: str) -> Mapping[str, Any] | None:
    """``json.loads(text)`` when it yields a JSON object, else None (parse error or non-object)."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, Mapping) else None


def _extract_parsed(body: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str | None, bool]:
    """Pull the structured JSON out of an OpenAI-compatible response body (choices[0].message.
    content), tolerating fenced / prose-wrapped replies. Returns ``(parsed, error, unwrapped)``.
    Never scans the reply text for rate limits (the v1/taxman bug)."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "response has no choices", False
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        return None, "choice has no message", False
    content = message.get("content")
    if isinstance(content, Mapping):
        return content, None, False
    if isinstance(content, list):  # content-block form: concatenate text blocks
        content = "".join(
            b.get("text", "") for b in content if isinstance(b, Mapping) and b.get("type") == "text"
        )
    if not isinstance(content, str):
        return None, "message content is not text", False
    obj, unwrapped = _recover_json_object(content)
    if obj is None:
        return None, "content is not a JSON object (even after fence/brace recovery)", False
    return obj, None, unwrapped


def _finish_reason(body: Mapping[str, Any]) -> str | None:
    """``choices[0].finish_reason`` of a response body (OpenRouter's normalized value: ``stop``,
    ``length``, ``content_filter``, ``tool_calls``, ``error``), None when absent."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) else None


def _provider_error_200(body: Mapping[str, Any]) -> bool:
    """An HTTP 200 that carries a provider error rather than an answer (review L5-10, D404): a
    top-level ``error``, ``finish_reason: "error"``, or no ``choices``. The call loop treats it as
    a 5xx (retried, counted by the breaker), never as schema-invalid model output."""
    choices = body.get("choices")
    return (bool(body.get("error")) or _finish_reason(body) == "error"
            or not isinstance(choices, list) or not choices)


def _int(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _is_rate_limited(status: int, headers: Mapping[str, str]) -> bool:
    """Rate-limit detection from STATUS/HEADERS ONLY — never the reply text (the v1/taxman bug,
    20-technical §8). The headers are read on a non-2xx status only: a 2xx is a paid answer
    whatever ``Retry-After``/``x-ratelimit-remaining`` it carries (review L5-5, D427)."""
    if 200 <= status < 300:
        return False
    if status == 429:
        return True
    lower = {k.lower(): v for k, v in headers.items()}
    if "retry-after" in lower:
        return True
    remaining = lower.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            return int(remaining) <= 0
        except ValueError:
            return False
    return False


#: OpenRouter's served-provider display names (the response ``provider`` field, the
#: ``/endpoints`` ``provider_name``) → egress slugs (the ``tag`` prefix), where lowercasing and
#: hyphenating the name does not give the slug or a name is listed for clarity. From keyless
#: ``/api/v1/models/<id>/endpoints`` reads of 2026-10-12 (gemini-3.5-flash-lite: ``Google`` =
#: ``google-vertex/*``, ``Google AI Studio`` = ``google-ai-studio*``; deepseek-v4.1-flash:
#: ``OpenInference`` = ``open-inference``, ``InferenceNet`` = ``inference-net``,
#: ``AtlasCloud`` = ``atlas-cloud``). Any other name keeps the derived slug (D185 (C)).
PROVIDER_SLUGS: Mapping[str, str] = {
    "google": "google-vertex",
    "google vertex": "google-vertex",
    "google ai studio": "google-ai-studio",
    "openai": "openai",
    "anthropic": "anthropic",
    "deepinfra": "deepinfra",
    "amazon bedrock": "amazon-bedrock",
    "azure": "azure",
    "openinference": "open-inference",
    "inferencenet": "inference-net",
    "atlascloud": "atlas-cloud",
}


def _served_provider_slug(body: Mapping[str, Any]) -> str | None:
    """The egress slug OpenRouter says it actually routed to, from the response ``provider`` field
    (a display name: :data:`PROVIDER_SLUGS`, e.g. ``"Google"`` → ``"google-vertex"``; any other
    name lowercased and hyphenated, ``"Amazon Bedrock"`` → ``"amazon-bedrock"``), or None when
    absent."""
    raw = body.get("provider")
    if not isinstance(raw, str) or not raw.strip():
        return None
    name = " ".join(raw.split()).lower()
    return PROVIDER_SLUGS.get(name, name.replace(" ", "-"))


def _provider_anomaly(body: Mapping[str, Any], allowed: Sequence[str]) -> str | None:
    """The offending provider display name when OpenRouter served the call via a provider NOT in
    the class-``allowed`` egress set (I13 post-hoc backstop), else None. A missing ``provider``
    field is NOT an anomaly (we cannot tell); a name :data:`PROVIDER_SLUGS` does not list is
    compared by its derived slug, so an unknown provider outside the set IS an anomaly (loud)."""
    slug = _served_provider_slug(body)
    if slug is None or slug in allowed:
        return None
    raw = body.get("provider")
    return str(raw)


# ─────────────────────────────────────────────────────────────────────────────
# The Anthropic Messages API (D722): the reply's chat-completions view, SSE reassembly
# ─────────────────────────────────────────────────────────────────────────────

#: The API version the ``anthropic`` dialect sends (``anthropic-version``). 2026-10-21 refuses ``temperature`` /
#: ``top_p`` / ``top_k`` on every model (HTTP 400): :meth:`stack.config.LlmConfig.check_route` refuses a role
#: setting ``temperature`` on this dialect.
ANTHROPIC_VERSION = "2026-10-21"

#: Anthropic ``stop_reason`` → the chat-completions ``finish_reason`` the call loop reads (``length`` = cut off at
#: the output cap; ``content_filter`` = the provider declined). Any other value passes through unchanged.
_ANTHROPIC_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "content_filter"}


def anthropic_view(body: Mapping[str, Any]) -> dict[str, Any]:
    """An Anthropic Messages reply as the chat-completions body the call loop reads (D722), pure: the ``text``
    blocks joined as ``choices[0].message.content`` (thinking blocks dropped), ``stop_reason`` mapped to
    ``finish_reason`` (:data:`_ANTHROPIC_FINISH`), and ``usage`` with ``prompt_tokens`` = ``input_tokens`` (the
    uncached remainder) + ``cache_read_input_tokens`` + ``cache_creation_input_tokens``, ``completion_tokens`` =
    ``output_tokens`` (thinking included) and ``prompt_tokens_details.cached_tokens`` / ``cache_write_tokens`` =
    the two cache fields (absent ones stay absent). No ``usage.cost``: the price table prices the call. An error
    event (``type: error``) or a body without a ``content`` list becomes a provider error (no ``choices``), which
    the loop retries as a 5xx. The response digest and the cache entry keep the raw body."""
    if body.get("type") == "error" or body.get("error"):
        return {"error": body.get("error") or True}
    content = body.get("content")
    if not isinstance(content, list):
        return {}
    text = "".join(str(b.get("text", "")) for b in content if isinstance(b, Mapping) and b.get("type") == "text")
    stop = body.get("stop_reason")
    view: dict[str, Any] = {
        "choices": [{"message": {"content": text},
                     "finish_reason": _ANTHROPIC_FINISH.get(stop, stop) if isinstance(stop, str) else None}],
        "model": body.get("model"),
    }
    raw = body.get("usage")
    if isinstance(raw, Mapping):
        read, write = raw.get("cache_read_input_tokens"), raw.get("cache_creation_input_tokens")
        details = {k: _int(v) for k, v in (("cached_tokens", read), ("cache_write_tokens", write)) if v is not None}
        view["usage"] = {"prompt_tokens": _int(raw.get("input_tokens")) + _int(read) + _int(write),
                         "completion_tokens": _int(raw.get("output_tokens")),
                         **({"prompt_tokens_details": details} if details else {})}
    return view


def assemble_sse(lines: Iterable[str]) -> dict[str, Any]:
    """The final Messages body from a streamed reply's server-sent-event lines (D722), as the non-streamed reply
    would carry it: ``message_start``'s message, each content block from its ``content_block_start`` plus its
    deltas (``text_delta`` → ``text``, ``thinking_delta`` → ``thinking``, ``signature_delta`` → ``signature``),
    ``message_delta``'s ``delta`` fields (``stop_reason``, …) and ``usage`` (cumulative, merged over the start's)
    and nothing from ``ping``. An ``error`` event returns ``{"type": "error", "error": …}`` (a provider error the
    loop retries). A stream that ends before ``message_stop`` raises :class:`TransportError` (counted as
    ``network``; its usage is lost with it)."""
    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    stopped = False
    data: list[str] = []

    def events() -> Iterator[Mapping[str, Any]]:
        for line in [*lines, ""]:
            line = line.rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                payload = "\n".join(data)
                data.clear()
                try:
                    ev = json.loads(payload)
                except ValueError:
                    raise TransportError("stream: an event is not JSON") from None
                if isinstance(ev, Mapping):
                    yield ev

    for ev in events():
        kind = ev.get("type")
        if kind == "error":
            return {"type": "error", "error": ev.get("error")}
        if kind == "message_start" and isinstance(ev.get("message"), Mapping):
            message = dict(ev["message"])
        elif kind == "content_block_start" and isinstance(ev.get("content_block"), Mapping):
            blocks[_int(ev.get("index"))] = dict(ev["content_block"])
        elif kind == "content_block_delta" and isinstance(ev.get("delta"), Mapping):
            block = blocks.setdefault(_int(ev.get("index")), {})
            delta = ev["delta"]
            for dtype, field_name in (("text_delta", "text"), ("thinking_delta", "thinking")):
                if delta.get("type") == dtype:
                    block[field_name] = str(block.get(field_name, "")) + str(delta.get(field_name, ""))
            if delta.get("type") == "signature_delta":
                block["signature"] = delta.get("signature")
        elif kind == "message_delta" and message is not None:
            if isinstance(ev.get("delta"), Mapping):
                message.update(ev["delta"])
            if isinstance(ev.get("usage"), Mapping):
                usage = dict(message.get("usage") or {})
                usage.update({k: v for k, v in ev["usage"].items() if v is not None})
                message["usage"] = usage
        elif kind == "message_stop":
            stopped = True
    if message is None or not stopped:
        raise TransportError("stream ended before message_stop")
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


# ─────────────────────────────────────────────────────────────────────────────
# Default production transport (stdlib urllib — no third-party HTTP dependency)
# ─────────────────────────────────────────────────────────────────────────────


class UrllibTransport:
    """The production transport: a single JSON POST via the stdlib ``urllib.request``.

    DECISION (dependency): neither ``httpx`` nor ``requests`` is in ``uv.lock``, and one POST to
    OpenRouter needs no third-party client; using the stdlib keeps the new-dependency footprint
    at zero. The :class:`Transport` seam means production can swap in ``httpx`` later without
    touching a line of client logic. Offline tests never construct this class.

    The Bearer key is read from ``$<api_key_env>`` at POST time and injected into the headers
    here — it never enters :class:`LlmRequest` (llm.yaml: "Secrets never live here").

    **Sandbox / proxy wiring (Phase-B critical).** This environment routes outbound HTTPS through
    an ``HTTPS_PROXY`` — ``urllib`` honours it AUTOMATICALLY (its default ``ProxyHandler`` reads the
    ``HTTP(S)_PROXY`` env vars), so no proxy code is needed here. TLS verification, however, needs
    the sandbox CA bundle (``/root/.ccr/ca-bundle.crt``): pass its path as ``ca_bundle`` (or wire it
    from ``$SSL_CERT_FILE`` / ``$AWS_CA_BUNDLE`` via :func:`make_default_transport`). ``ca_bundle``
    builds an :class:`ssl.SSLContext` (``cafile=``) that is passed as ``context=`` to ``urlopen``;
    with no ``ca_bundle`` the system default trust store is used. NEVER disable verification.
    """

    def __init__(self, *, api_key_env: str, timeout: float = 120.0, ca_bundle: str | None = None) -> None:
        self._api_key_env = api_key_env
        self._timeout = timeout
        self._ca_bundle = ca_bundle
        self._context: ssl.SSLContext | None = None

    def _ssl_context(self) -> ssl.SSLContext | None:
        """A verifying SSL context built from ``ca_bundle`` (cached), or None (system trust)."""
        if self._ca_bundle and self._context is None:
            self._context = ssl.create_default_context(cafile=self._ca_bundle)
        return self._context

    def post(self, request: LlmRequest) -> LlmResponse:
        # A batch submit carries up to tens of MB: give its upload time (stack.llm.batch).
        timeout = max(self._timeout, 600.0) if request.url.rstrip("/").endswith("/batches") else (
            request.timeout_s or self._timeout)
        data = json.dumps(dict(request.payload)).encode("utf-8")
        return self._send("POST", request.url, request.headers, data, timeout, key_env=request.api_key_env,
                          key_header=request.key_header, stream=request.payload.get("stream") is True)

    def get(self, url: str, headers: Mapping[str, str]) -> LlmResponse:
        """A GET (a batch poll, :mod:`stack.llm.batch`), with the key handling, CA bundle and
        error mapping of :meth:`post`."""
        return self._send("GET", url, headers, None, self._timeout)

    def _send(self, method: str, url: str, headers: Mapping[str, str], data: bytes | None,
              timeout: float, key_env: str | None = None, key_header: str | None = None,
              stream: bool = False) -> LlmResponse:
        import http.client
        import urllib.error
        import urllib.request

        key_name = key_env or self._api_key_env  # a request's own key name (a role's provider, D714)
        key = provider_key(key_name)
        if not key:
            raise TransportError(f"provider API key ${key_name} is unset "
                                 f"(and not in ${ENV_FILE_ENV})")
        auth = {key_header: key} if key_header else {"Authorization": f"Bearer {key}"}  # x-api-key: D722
        req = urllib.request.Request(url, data=data, method=method, headers={**dict(headers), **auth})
        try:
            # context= carries the CA bundle; the default opener adds the HTTP(S)_PROXY handler.
            with urllib.request.urlopen(  # fixed provider URL
                req, timeout=timeout, context=self._ssl_context()
            ) as resp:
                if stream:  # server-sent events (D722): the timeout is per read, and pings keep the socket busy
                    try:
                        body = assemble_sse(raw.decode("utf-8") for raw in resp)
                    except (UnicodeDecodeError, http.client.HTTPException) as exc:
                        raise TransportError(f"transport {method}: unreadable stream ({type(exc).__name__})") from None
                    return LlmResponse(status=resp.status, headers=dict(resp.headers), body=body)
                try:
                    body = json.loads(resp.read().decode("utf-8"))
                except (ValueError, http.client.HTTPException) as exc:  # non-JSON / cut-off body (D404)
                    raise TransportError(f"transport {method}: unreadable 200 body ({type(exc).__name__})") from None
                if not isinstance(body, dict):
                    raise TransportError(f"transport {method}: 200 body is not a JSON object")
                return LlmResponse(status=resp.status, headers=dict(resp.headers), body=body)
        except urllib.error.HTTPError as exc:  # a non-2xx status is a classifiable response, not a failure
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, ValueError, OSError):
                body = {}
            return LlmResponse(status=exc.code, headers=dict(exc.headers or {}), body=body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # genuine network failure
            raise TransportError(f"transport {method} failed: {exc}") from exc


def provider_key(name: str) -> str | None:
    """The provider key: ``$<name>``, else ``<name>``'s value in the file ``$STACK_ENV_FILE``
    names, else None. The file is read for that one name only (``KEY=VALUE`` lines, an optional
    ``export``, one pair of matching quotes stripped, ``#`` comment lines; no interpolation) and
    nothing is put into ``os.environ``. A named file that is missing raises
    :class:`FatalLlmError` naming the path. No message here ever contains a value."""
    import os

    val = os.environ.get(name)
    if val:
        return val
    path = os.environ.get(ENV_FILE_ENV)
    if not path:
        return None
    try:
        text = Path(path).read_text("utf-8")
    except OSError as exc:
        raise FatalLlmError(f"${ENV_FILE_ENV}={path}: cannot read ({type(exc).__name__})") from None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, sep, v = line.partition("=")
        if not sep or k.strip() != name:
            continue
        v = v.strip()
        if v[:1] in ("'", '"') and v.find(v[0], 1) > 0:
            v = v[1:v.find(v[0], 1)]  # a quoted value ends at its closing quote
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        return v or None
    return None


def _usage(
    body: Mapping[str, Any],
    *,
    price: ModelPrice,
    est_in: int,
    est_fixed: int,
) -> tuple[int, int, int, float]:
    """``(tokens_in, tokens_in_fixed, tokens_out, cost_usd)`` from the response usage, falling
    back to estimates (``est_in`` over the sent prefix + user text, ``est_fixed`` over the
    prefix: :func:`estimate_tokens`). ``tokens_in_fixed`` prefers the cached-prompt token fields: OpenAI
    ``prompt_tokens_details.cached_tokens`` or Anthropic ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``. The cost is OpenRouter's ``usage.cost`` when present, else
    tokens × the ``llm.yaml`` price."""
    raw_usage = body.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    tokens_in = _int(usage.get("prompt_tokens"), _int(usage.get("input_tokens"), est_in))
    tokens_out = _int(usage.get("completion_tokens"), _int(usage.get("output_tokens"), 0))

    fixed: int | None = None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping) and details.get("cached_tokens"):
        # a NONZERO cached-token count; `cached_tokens: 0` (a cache miss) is treated as absent.
        fixed = _int(details.get("cached_tokens"))
    elif usage.get("cache_read_input_tokens") is not None or usage.get("cache_creation_input_tokens") is not None:
        fixed = _int(usage.get("cache_read_input_tokens"), 0) + _int(usage.get("cache_creation_input_tokens"), 0)
    tokens_in_fixed = fixed if fixed is not None else est_fixed

    raw_cost = usage.get("cost")
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
        cost = float(raw_cost)
    else:
        # no usage.cost (a direct OpenAI reply, D714): the price table, cached / cache-write tokens at their rates
        cached, write, _ = usage_details(body)
        cost = price.cost(tokens_in, tokens_out, cached or 0, write or 0)
    return tokens_in, tokens_in_fixed, tokens_out, cost


def usage_details(body: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    """``(cached, cache_write, reasoning)`` tokens of a chat-completions reply (D714): ``usage.prompt_tokens_details``
    ``cached_tokens`` / ``cache_write_tokens`` and ``usage.completion_tokens_details.reasoning_tokens``; None
    where the reply does not report the field (0 stays 0)."""
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        return None, None, None

    def get(block: str, key: str) -> int | None:
        d = usage.get(block)
        v = d.get(key) if isinstance(d, Mapping) else None
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    return (get("prompt_tokens_details", "cached_tokens"), get("prompt_tokens_details", "cache_write_tokens"),
            get("completion_tokens_details", "reasoning_tokens"))


def _error_status_cost(body: Mapping[str, Any]) -> float:
    """The actual cost of an attempt that got an HTTP error status back: ``usage.cost`` when the
    body carries one, else $0 (the provider answered with an error, so nothing was generated)."""
    usage = body.get("usage")
    if isinstance(usage, Mapping):
        raw = usage.get("cost")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    return 0.0


def _too_large(status: int, body: Mapping[str, Any]) -> bool:
    """HTTP 413, or a 400 whose body names the context window (:class:`ContextTooLarge`)."""
    if status == 413:
        return True
    return status == 400 and any(term in json.dumps(body).lower() for term in (
        "context_length_exceeded", "context length", "maximum context", "context window",
        "prompt is too long"))  # the Anthropic API's wording (D722)


def _llm_config(config: StackConfig | LlmConfig) -> LlmConfig:
    llm = getattr(config, "llm", None)  # a StackConfig carries .llm; an LlmConfig is itself
    return llm if llm is not None else config  # type: ignore[return-value]


def make_default_transport(config: StackConfig | LlmConfig, *, timeout: float = 120.0) -> UrllibTransport:
    """Build the production :class:`UrllibTransport` from ``llm.provider`` (the api-key env var
    name). The CA bundle defaults from ``$SSL_CERT_FILE`` else ``$AWS_CA_BUNDLE`` when present;
    ``HTTPS_PROXY`` is honoured by urllib automatically. Tests inject a fake instead."""
    import os

    ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("AWS_CA_BUNDLE")
    return UrllibTransport(
        api_key_env=_llm_config(config).provider.api_key_env, timeout=timeout, ca_bundle=ca_bundle
    )


# ─────────────────────────────────────────────────────────────────────────────
# The client
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _CallState:
    """Mutable state of one ``complete`` call (local to its thread). A batch-mode ``retry_sync``
    starts it with the batch attempt (``attempts``, ``cost_usd``) and, after a truncated batch
    reply, the doubled output cap (``out_cap``)."""

    attempts: list[Attempt] = field(default_factory=list)
    cost_usd: float = 0.0
    failures: int = 0  # transport-level failures of this call (network, 5xx, 429, odd status)
    out_cap: int | None = None


@dataclass(frozen=True)
class _Reply:
    """What the HTTP-200 checks found in one response body (:meth:`LlmClient._inspect_200`)."""

    parsed: Mapping[str, Any] | None
    errors: list[str]
    parse_error: bool
    unwrapped: bool
    offending: str | None  # the served provider outside the sanctioned set, if any
    tokens_in: int
    tokens_in_fixed: int
    tokens_out: int
    cost: float
    truncated: bool  # finish_reason == "length"
    tokens_cached: int | None = None
    tokens_cache_write: int | None = None
    tokens_reasoning: int | None = None

    @property
    def error_class(self) -> str | None:
        if self.truncated:
            return "truncated"
        if not self.errors:
            return None
        return "parse_error" if self.parse_error else "schema_invalid"


#: Batch mode (:mod:`stack.llm.batch`): what this run's batches gave one cache key.
#: ``answered`` (cached; served with the batch attempt's cost), ``retry_sync`` (asked sync in the
#: replay pass, after the paid batch attempt), ``sync`` (a pass too small to batch: asked sync),
#: ``refused`` (budget), ``fatal`` (auth/credit), ``error`` (e.g. too large: raised as is),
#: ``unanswered`` (:class:`BatchUnanswered`).
BatchOutcomeKind = Literal["answered", "retry_sync", "sync", "refused", "fatal", "error", "unanswered"]


@dataclass(frozen=True)
class BatchOutcome:
    """One key's outcome in batch mode; ``error`` is the exception ``refused``/``fatal``/``error``
    raise, ``out_cap`` the replay's starting output cap after a truncated batch reply."""

    kind: BatchOutcomeKind
    cost_usd: float = 0.0
    attempts: tuple[Attempt, ...] = ()
    latency_ms: int = 0
    error: LlmError | None = None
    out_cap: int | None = None


@dataclass
class BatchCall:
    """One request a collect pass recorded: the first request the call loop would send, plus
    what settling its batch result needs. The driver sets ``custom_id``, ``size`` and
    ``reservation`` and drops ``request`` once its batch is submitted (payloads are large)."""

    key: str
    request: LlmRequest | None
    model: str
    slug: str  # the egress provider slug the request is pinned to
    attempt_kind: AttemptKind
    prompt_name: str | None
    template_digest: str
    model_digest: str
    rendered_digest: str
    basis_digest: str
    schema: Mapping[str, Any]
    license_class: str
    est_in: int
    est_fixed: int
    max_tokens: int
    custom_id: str = ""
    size: int = 0
    reservation: Reservation | None = None


@dataclass
class _BatchState:
    """The batch-mode state of one client (one run): never persisted, never shared."""

    collecting: bool = True
    pass_no: int = 0
    outcomes: dict[str, BatchOutcome] = field(default_factory=dict)
    registered: set[str] = field(default_factory=set)
    new: list[BatchCall] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def lookup(self, key: str) -> BatchOutcome | None:
        """The key's outcome. In the replay pass an outcome that leads to an answer
        (``answered``, ``retry_sync``, ``sync``) is consumed: a second call of the same key is
        then a plain cache hit, as in sync mode."""
        with self.lock:
            if self.collecting or self.outcomes.get(key, None) is None:
                return self.outcomes.get(key)
            if self.outcomes[key].kind in ("answered", "retry_sync", "sync"):
                return self.outcomes.pop(key)
            return self.outcomes[key]


class LlmClient:
    """The role-based client for one stage run. ``run_id``/``stage`` go on every ledger line;
    ``budget_usd`` is the stage's ``budget.max_usd`` (None: ``llm.yaml`` ``budget.per_run_usd``),
    checked against the run's committed + reserved cost in the run's ledger
    (:func:`stack.llm.ledger.run_ledger`), so it holds across the run's threads and clients.
    The response-cache lookup reads only the key of the first gated model, the one the call sends
    to first: a fallback's answer is cached under the fallback's key but never answers a later call
    (D702; another model's entry for the same request bytes is not an answer of the model asked).
    Thread-safe: call :meth:`complete` from a pool."""

    def __init__(
        self,
        *,
        config: StackConfig | LlmConfig,
        transport: Transport,
        ledger: Ledger,
        run_id: str,
        stage: str,
        cache: ResponseCache | None = None,
        budget_usd: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_consecutive_failures: int = _MAX_CONSECUTIVE_FAILURES,
        max_call_failures: int = _MAX_CALL_FAILURES,
        min_failure_streak_s: float | None = None,  # None: _MIN_FAILURE_STREAK_S, read at construction
    ) -> None:
        self._llm = _llm_config(config)
        self._transport = transport
        self.ledger = ledger
        self.cache = cache
        self.run_id = run_id
        self.stage = stage
        self.budget_usd = budget_usd if budget_usd is not None else self._llm.budget.per_run_usd
        self._sleep = sleep
        self._max_consecutive_failures = max_consecutive_failures
        self._max_call_failures = max_call_failures
        self._min_failure_streak_s = (_MIN_FAILURE_STREAK_S if min_failure_streak_s is None
                                      else min_failure_streak_s)
        # Guards only the breaker state and the terminal error below; never held over I/O.
        self._state_lock = threading.Lock()
        self._consecutive_failures = 0
        self._streak_started: float | None = None  # monotonic time of the streak's first failure
        self._terminal_error: LlmError | None = None
        self._batch: _BatchState | None = None  # batch mode only (stack.llm.batch)
        #: Batch mode: calls the replay pass asked sync with no outcome (a key no collect pass
        #: saw, e.g. a tier-2 call admitted only in the replay); the driver cannot count these.
        self.batch_sync_fallback = 0
        _register_run_calls(run_id)

    # ── public API ────────────────────────────────────────────────────────────

    def complete(
        self,
        *,
        role: str,
        template: str,
        refs: Mapping[str, str],
        user: str,
        schema: Mapping[str, Any],
        license_class: LicenseClass,
        max_tokens: int | None = None,
        prompt_name: str | None = None,
        basis_digest: str | None = None,
        attempt_kind: AttemptKind = "first",
        model: str | None = None,
        prior: Sequence[Mapping[str, str]] = (),
        cache_prefix_only: bool = False,
        fold_schema: bool = False,
    ) -> LlmResult:
        """Run one logical completion for ``role`` with a structured-output ``schema``.

        ``template`` is the fixed prefix with ``{{ref:<kind>}}`` placeholders (filled from
        ``refs``); ``user`` is the per-document content. ``basis_digest`` (the evidence's basis,
        e.g. ``current_text_sha256``) enters the cache key; default: the digest of ``user``.
        ``attempt_kind`` labels the first outbound attempt in the ledger: ``first``, or
        ``adjudicate`` for a second-tier call (the caller calls again per tier); later attempts
        are ``retry`` (same model) or ``fallback`` (another model). ``model`` (a plan's model
        pin) replaces the role's primary model; the role's ``max_tokens``, ``temperature``,
        ``reasoning_effort`` and fallbacks stay. ``prior`` (techpipe D615) = earlier turns of the
        conversation, sent between the fixed prefix and ``user`` (``{"role": "user" | "assistant",
        "content": str}``); with turns, the input digest covers them too (:func:`conversation_input`),
        without them it is ``user``'s as before. ``cache_prefix_only`` (D714): on a ``dialect: openai`` provider, cache
        only the fixed prefix (explicit caching: a breakpoint after the system part), so the per-call messages are
        billed at the plain input rate instead of the cache-write rate; for a call nothing continues (model_roles).
        A transport choice: in no digest or cache key; ignored by the OpenRouter dialect. ``fold_schema`` (D714): send
        the schema folded into the prompt from the first attempt (the strict-schema degrade's request), for a schema
        strict mode rejects (:func:`is_strict_schema` false), so no call pays the doomed strict attempt's round trip;
        the answering request's bytes are the degrade's, so no digest or cache key moves. Set automatically on a
        ``dialect: openai`` or ``anthropic`` provider for a schema that is not strict-shaped. ``model`` and the role's
        models may be provider-qualified (``anthropic:claude-opus-5-5``, D722): each goes to its provider
        (:meth:`stack.config.LlmConfig.route`); on the Anthropic dialect ``cache_prefix_only`` keeps the one
        ``cache_control`` breakpoint on the system part, else the request also caches through its last block.

        Raises :class:`BudgetExceeded` before a request that would
        cross a limit (the stage then marks this and every later item ``not_attempted[budget]``,
        see :class:`BudgetScope`); :class:`FatalLlmError` (auth/credit, or the consecutive
        failure breaker; later calls re-raise it); :class:`AttemptsExhausted` (this call's
        transport-level failures reached ``max_call_failures``: the item fails, the run goes
        on); :class:`ContextTooLarge`;
        :class:`SchemaValidationError`; :class:`TruncatedOutput` (the reply hit the output cap,
        then again at twice the cap); :class:`EgressRefused`; :class:`ModelRetired` (a fatal error
        raised before the cache lookup when the primary model or a fallback is retired in ``llm.yaml``);
        :class:`RouteInvalid` (fatal, before the cache: a model reference the client cannot route, D722);
        :class:`ProviderRefused` (the provider declined on the last model tried, D722). A raised :class:`LlmError`
        carries ``.attempts`` and ``.cost_usd`` of the attempts made.
        """
        with self._state_lock:
            if self._terminal_error is not None:
                raise self._terminal_error
        role_cfg = self._llm.roles.get(role)
        if role_cfg is None:
            raise LlmError(f"role {role!r} is not in llm.roles (known: {sorted(self._llm.roles)})")
        if model is not None and model != role_cfg.model:
            role_cfg = role_cfg.model_copy(update={"model": model})
        effective_max_tokens = max_tokens if max_tokens is not None else role_cfg.max_tokens
        retired = [m for m in [role_cfg.model, *role_cfg.fallbacks] if self._llm.is_retired(m)]
        if retired:
            exc = ModelRetired(f"role {role!r}: {retired} retired in llm.yaml (pricing.models.<id>.retired); "
                               f"pin a current model; no bytes sent")
            with self._state_lock:
                self._terminal_error = exc
            raise exc

        routes = self._routes(role_cfg)
        models = self._gated_models(role_cfg, license_class, routes)
        if not models:
            raise EgressRefused(
                f"license_class {license_class!r}: every attempted model is gated out "
                f"(model={role_cfg.model!r}, fallbacks={role_cfg.fallbacks!r}); no bytes sent"
            )
        turns = _conversation(prior, user)
        idig = hashes.input_digest(conversation_input(prior, user))
        basis = basis_digest if basis_digest is not None else idig
        state = _CallState()
        if routes[models[0]].provider.dialect in ("openai", "anthropic") and not is_strict_schema(schema):
            # D714: OpenAI refuses a strict response_format that is not strict-shaped (HTTP 400, e.g. technique_staged's
            # open single / glean objects), as does the Anthropic API (every object closed, D722); send the
            # degrade's folded request from the first attempt instead
            fold_schema = True
        # D714: one key per role and prompt set, so a call and its continuation (a gleaning turn) share it
        cache_key_wire = f"stack:{role}:{prompt_name}" if prompt_name else f"stack:{role}"

        # Batch mode: this run's outcome for the call's first request, keyed like the cache.
        batch = self._batch
        direct = sorted({n for m in models if (n := routes[m].provider_name) is not None})
        if batch is not None and direct:
            raise LlmError(f"role {role!r} sends to provider(s) {direct}: batch mode sends only to the "
                           f"default provider (D714); run this stage without batch mode")
        outcome: BatchOutcome | None = None
        if batch is not None:
            r0 = render_call(template, refs, model=models[0], max_tokens=effective_max_tokens,
                             schema=schema, temperature=role_cfg.temperature,
                             reasoning_effort=role_cfg.reasoning_effort)
            key = cache_key(models[0], rendered_digest(r0.rendered_hash, idig), basis)
            outcome = batch.lookup(key)
            if outcome is not None:
                served = self._batch_serve(batch, outcome, key, prompt_name, state)
                if served is not None:
                    return served

        # Cache lookup of the first gated model only (D702): free, no reservation, no ledger line.
        if self.cache is not None:
            m = models[0]
            r = render_call(template, refs, model=m, max_tokens=effective_max_tokens,
                            schema=schema, temperature=role_cfg.temperature,
                            reasoning_effort=role_cfg.reasoning_effort)
            entry = self.cache.get(cache_key(m, rendered_digest(r.rendered_hash, idig), basis))
            if entry is not None:
                return self._from_cache(entry, prompt_name)

        if batch is not None and outcome is None:
            if batch.collecting:
                self._batch_register(
                    batch, key=key, rendered=r0, model=models[0], user=user, turns=turns, input_digest=idig,
                    basis_digest=basis, schema=schema, license_class=license_class,
                    max_tokens=effective_max_tokens, temperature=role_cfg.temperature,
                    reasoning_effort=role_cfg.reasoning_effort,
                    prompt_name=prompt_name, attempt_kind=attempt_kind,
                )
            with self._state_lock:
                self.batch_sync_fallback += 1
        try:
            return self._run_call_loop(
                models=models, template=template, refs=refs, schema=schema, user=user, turns=turns,
                input_digest=idig, basis_digest=basis, license_class=license_class,
                temperature=role_cfg.temperature, reasoning_effort=role_cfg.reasoning_effort,
                max_tokens=effective_max_tokens,
                prompt_name=prompt_name, attempt_kind=attempt_kind, state=state,
                routes=routes, prompt_cache_key=cache_key_wire, cache_prefix_only=cache_prefix_only,
                fold_schema=fold_schema,
            )
        except LlmError as exc:
            exc.attempts = tuple(state.attempts)  # type: ignore[attr-defined]
            exc.cost_usd = state.cost_usd  # type: ignore[attr-defined]
            if isinstance(exc, FatalLlmError):
                with self._state_lock:
                    self._terminal_error = exc
            raise

    # ── batch mode (stack.llm.batch drives these) ─────────────────────────────

    @property
    def batch_pass(self) -> int:
        """0 in sync mode; 1, 2, … per batch collect pass; the replay pass gets the next number."""
        return self._batch.pass_no if self._batch is not None else 0

    def batch_collect(self) -> None:
        """Enter (or continue with) a batch collect pass: :meth:`complete` answers from the cache
        or this run's batch outcomes, else records its first request and raises :class:`Deferred`."""
        if self._batch is None:
            self._batch = _BatchState()
        self._batch.collecting = True
        self._batch.pass_no += 1

    def batch_replay(self) -> None:
        """Enter the replay pass: every call is answered from the batch outcomes (or raises the
        outcome's error); a call with no outcome takes the normal path (cache, then sync)."""
        if self._batch is None:
            raise RuntimeError("batch_replay() before any batch_collect()")
        self._batch.collecting = False
        self._batch.pass_no += 1

    def _batch_take(self) -> list[BatchCall]:
        """The requests collected since the last take (the driver's)."""
        assert self._batch is not None
        with self._batch.lock:
            calls, self._batch.new = self._batch.new, []
        return calls

    def _batch_set(self, key: str, outcome: BatchOutcome) -> None:
        assert self._batch is not None
        with self._batch.lock:
            self._batch.outcomes[key] = outcome

    def _batch_serve(
        self, batch: _BatchState, outcome: BatchOutcome, key: str, prompt_name: str | None,
        state: _CallState,
    ) -> LlmResult | None:
        """``complete()`` for a key with an outcome: the result, an exception, or None (go on
        to the normal path with ``state`` carrying the batch attempt)."""
        kind = outcome.kind
        if kind == "answered":
            entry = self.cache.get(key) if self.cache is not None else None
            if entry is not None:
                return replace(self._from_cache(entry, prompt_name), is_cache_hit=False,
                               cost_usd=outcome.cost_usd, attempts=outcome.attempts,
                               latency_ms=outcome.latency_ms)
            kind = "retry_sync"  # the entry is gone: ask again, after the paid batch attempt
        if kind == "refused":
            raise BudgetExceeded(str(outcome.error))
        if kind in ("fatal", "error"):
            assert outcome.error is not None
            if kind == "fatal" and not batch.collecting:  # halts the replay here, as in sync mode
                with self._state_lock:
                    self._terminal_error = outcome.error
            raise outcome.error
        if kind == "unanswered":
            raise BatchUnanswered("asked in a batch this run, no answer when the batch passes ended")
        if batch.collecting:  # retry_sync / sync: the item waits for the replay pass
            raise Deferred(f"{kind}: asked sync in the replay pass")
        state.attempts.extend(outcome.attempts)
        state.cost_usd += outcome.cost_usd
        state.out_cap = outcome.out_cap
        return None

    def _batch_register(
        self, batch: _BatchState, *, key: str, rendered: Rendered, model: str, user: str,
        turns: list[dict[str, Any]], input_digest: str, basis_digest: str, schema: Mapping[str, Any], license_class: str,
        max_tokens: int, temperature: float | None, prompt_name: str | None, attempt_kind: AttemptKind,
        reasoning_effort: str | None = None,
    ) -> NoReturn:
        """Record the call loop's first request for ``key`` (once per key) and raise
        :class:`Deferred`."""
        with batch.lock:
            fresh = key not in batch.registered
            batch.registered.add(key)
        if fresh:
            request = self._build_request(
                model=model, sent_prefix=rendered.sent_prefix, messages=list(turns),
                schema=schema, max_tokens=max_tokens, temperature=temperature,
                reasoning_effort=reasoning_effort, rule=self._egress_rule(license_class), attempt=1,
                rendered_hash=rendered.rendered_hash, input_digest=input_digest,
            )
            call = BatchCall(
                key=key, request=request, model=model, slug=request.payload["provider"]["only"][0],
                attempt_kind=attempt_kind, prompt_name=prompt_name,
                template_digest=rendered.template_hash, model_digest=model_digest(rendered.params),
                rendered_digest=rendered_digest(rendered.rendered_hash, input_digest),
                basis_digest=basis_digest, schema=schema, license_class=license_class,
                est_in=estimate_tokens(rendered.sent_prefix + "\n" + user),
                est_fixed=estimate_tokens(rendered.sent_prefix), max_tokens=max_tokens,
            )
            with batch.lock:
                batch.new.append(call)
        raise Deferred(f"collected for a batch (pass {batch.pass_no})")

    def _batch_attempt(
        self, call: BatchCall, *, status: int | None, error_class: str | None, actual_usd: float | None,
        tokens_in: int = 0, tokens_out: int = 0, latency_ms: int = 0,
    ) -> Attempt:
        """Reconcile (``actual_usd``) or release (None) a batched request's reservation; its Attempt."""
        assert call.reservation is not None
        state = _CallState()
        self._settle(state, call.reservation, status=status, error_class=error_class, actual_usd=actual_usd,
                     tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms)
        return state.attempts[0]

    def _batch_settle_200(
        self, call: BatchCall, body: Mapping[str, Any], *, cost: float, batch_id: str, latency_ms: int,
    ) -> BatchOutcome:
        """Settle a batch result with HTTP 200 through the call loop's 200 checks: a clean reply
        is reconciled at ``cost``, cached (``via: batch``) and ``answered``; a parse error, a
        schema-invalid or a truncated reply is reconciled (it was paid) and ``retry_sync``. A 200
        carrying a provider error (:func:`_provider_error_200`) never comes here: the driver
        settles it as a 5xx result (``retry_sync``, ``http_5xx``; D404, D429)."""
        reply = replace(self._inspect_200(body, model=call.model, license_class=call.license_class,
                                          schema=call.schema, est_in=call.est_in, est_fixed=call.est_fixed),
                        cost=cost)
        attempt = self._batch_attempt(call, status=200, error_class=reply.error_class, actual_usd=cost,
                                      tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                                      latency_ms=latency_ms)
        if reply.error_class is not None:
            return BatchOutcome("retry_sync", cost_usd=cost, attempts=(attempt,),
                                out_cap=2 * call.max_tokens if reply.truncated else None)
        self._answer(reply, body=body, model=call.model, prompt_name=call.prompt_name,
                     template_digest=call.template_digest, model_digest=call.model_digest,
                     rendered_digest=call.rendered_digest, basis_digest=call.basis_digest,
                     cost_usd=cost, attempts=(attempt,), latency_ms=latency_ms, degraded=False,
                     extra={"via": "batch", "batch_id": batch_id})
        return BatchOutcome("answered", cost_usd=cost, attempts=(attempt,), latency_ms=latency_ms)

    # ── egress gate ───────────────────────────────────────────────────────────

    def _egress_rule(self, license_class: str) -> EgressClass:
        egress = self._llm.egress
        return egress.by_license_class.get(license_class, egress.default)

    def _routes(self, role_cfg: RoleConfig) -> dict[str, ModelRoute]:
        """Each model reference of the role (primary, fallbacks) → its route (D722); a reference the client cannot
        route, or whose provider cannot take the role's settings, raises :class:`RouteInvalid` (fatal: every item
        would fail the same way) before the cache and any reservation."""
        out: dict[str, ModelRoute] = {}
        for m in [role_cfg.model, *role_cfg.fallbacks]:
            try:
                out[m] = self._llm.route(m, role_cfg)
                self._llm.check_route(out[m], role_cfg)
            except ValueError as exc:
                err = RouteInvalid(f"{exc}; no bytes sent")
                with self._state_lock:
                    self._terminal_error = err
                raise err from None
        return out

    def _gate_ok(self, model: str, rule: EgressClass, route: ModelRoute | None = None) -> bool:
        """A model passes iff its egress slug is in the class's sanctioned ``providers`` list: the slug of its route
        (D722: a direct provider's ``egress_provider``, else the model id's PREFIX via ``provider_prefixes``); without
        a route, the prefix of ``model``. An unmappable model is gated out (fail closed)."""
        slug = route.egress_slug if route is not None else self._llm.egress.provider_prefixes.get(model.split("/", 1)[0])
        if slug is None:
            return False
        return slug in rule.providers

    def _gated_models(self, role_cfg: RoleConfig, license_class: str,
                      routes: Mapping[str, ModelRoute] | None = None) -> list[str]:
        """The ordered attempt set: the primary, then fallbacks IFF the class allows fallbacks —
        each model independently re-gated by its own route's egress slug (D722; ``routes`` default: the role's)."""
        rule = self._egress_rule(license_class)
        routes = routes if routes is not None else {m: self._llm.route(m, role_cfg)
                                                    for m in [role_cfg.model, *role_cfg.fallbacks]}
        candidates = [role_cfg.model]
        if rule.allow_fallbacks:
            candidates += list(role_cfg.fallbacks)
        return [m for m in candidates if self._gate_ok(m, rule, routes[m])]

    # ── cache ─────────────────────────────────────────────────────────────────

    def _from_cache(self, entry: Mapping[str, Any], prompt_name: str | None) -> LlmResult:
        return LlmResult(
            parsed=entry["parsed"], model=entry["model"],
            prompt_name=prompt_name if prompt_name is not None else entry.get("prompt_name"),
            template_digest=entry["template_digest"], model_digest=entry["model_digest"],
            rendered_digest=entry["rendered_digest"], response_digest=entry["response_digest"],
            basis_digest=entry["basis_digest"], cost_usd=0.0,
            tokens_in=int(entry["tokens_in"]), tokens_out=int(entry["tokens_out"]),
            latency_ms=0, is_cache_hit=True, attempts=(),
            tokens_in_fixed=int(entry.get("tokens_in_fixed", 0)),
            degraded=bool(entry.get("degraded")), unwrapped=bool(entry.get("unwrapped")),
            provider_anomaly=bool(entry.get("provider_anomaly")),
        )

    # ── the call loop ─────────────────────────────────────────────────────────

    def _reserve(self, *, kind: AttemptKind, request: LlmRequest, factor: float = 1.0,
                 batched: bool = False) -> Reservation:
        """Reserve one outbound attempt at a conservative amount (D36): input tokens estimated
        by :func:`reserve_tokens_in` over the full serialized request body, output =
        ``max_tokens``, at the ``llm.yaml`` price (the fallback price for an unknown model),
        times ``factor`` (a batched request: :data:`stack.llm.batch.BATCH_PRICE_FACTOR`, with
        ``batched``, so the run's end never releases it at $0)."""
        est_in = reserve_tokens_in(request)
        max_tokens = wire_max_tokens(request.payload)
        amount = self._llm.price_for(request.model).cost(est_in, max_tokens) * factor
        return self.ledger.reserve(
            run_id=self.run_id, stage=self.stage, model=request.model, attempt=kind,
            amount_usd=amount, tokens_in=est_in, tokens_out=max_tokens,
            run_budget_usd=self.budget_usd, batched=batched,
        )

    def _settle(
        self, state: _CallState, res: Reservation, *, status: int | None, error_class: str | None,
        actual_usd: float | None, tokens_in: int = 0, tokens_out: int = 0, latency_ms: int = 0,
    ) -> None:
        """Reconcile ``res`` to ``actual_usd`` (None: release it, cost 0.0) and record the attempt."""
        if actual_usd is not None:
            self.ledger.reconcile(res, actual_usd=actual_usd, tokens_in=tokens_in, tokens_out=tokens_out)
            state.cost_usd += actual_usd
        else:
            self.ledger.release(res)
            actual_usd = 0.0
        state.attempts.append(Attempt(
            model=res.model, kind=res.attempt, status=status, error_class=error_class,
            reservation_id=res.reservation_id, reserved_usd=res.reserved_usd, cost_usd=actual_usd,
            latency_ms=latency_ms,
        ))

    def _run_call_loop(
        self,
        *,
        models: list[str],
        template: str,
        refs: Mapping[str, str],
        schema: Mapping[str, Any],
        user: str,
        turns: list[dict[str, Any]],
        input_digest: str,
        basis_digest: str,
        license_class: str,
        temperature: float | None,
        max_tokens: int,
        prompt_name: str | None,
        attempt_kind: AttemptKind,
        state: _CallState,
        reasoning_effort: str | None = None,
        routes: Mapping[str, ModelRoute] | None = None,
        prompt_cache_key: str | None = None,
        cache_prefix_only: bool = False,
        fold_schema: bool = False,
    ) -> LlmResult:
        rule = self._egress_rule(license_class)
        # The variable message chain: the per-doc user block, plus any appended validator turns.
        messages: list[dict[str, Any]] = list(turns)
        schema_attempt = 1
        model_idx = 0
        degraded = False
        if fold_schema:  # D714: the degrade's request from the first attempt (a schema strict mode rejects)
            degraded = True
            messages = self._fold_schema(messages, schema)
        backoff = 0.5
        # The outbound cap: ``max_tokens`` until a reply is cut off at it, then doubled once for
        # the rest of the call. The digests stay at ``max_tokens`` (module docstring).
        out_cap = state.out_cap or max_tokens

        while True:
            model = models[model_idx]
            degraded = degraded or fold_schema  # a fallback model's reset never re-sends a doomed strict schema
            # The digests are per-model (the model is in the sampling params), so a fallback
            # re-renders over the same template/refs; sent_prefix is model-independent.
            r = render_call(
                template, refs, model=model, max_tokens=max_tokens, schema=schema, temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
            route = routes[model] if routes is not None else None
            dialect = route.provider.dialect if route is not None else self._llm.provider.dialect
            request = self._build_request(
                model=model, sent_prefix=r.sent_prefix, messages=messages,
                schema=None if degraded else schema, max_tokens=out_cap,
                temperature=temperature, reasoning_effort=reasoning_effort, rule=rule, attempt=schema_attempt,
                rendered_hash=r.rendered_hash, input_digest=input_digest,
                route=route, prompt_cache_key=prompt_cache_key, cache_prefix_only=cache_prefix_only,
            )
            kind: AttemptKind = (
                attempt_kind if not state.attempts else ("retry" if model_idx == 0 else "fallback")
            )
            with self._state_lock:
                if self._terminal_error is not None:
                    raise self._terminal_error
            # Every outbound attempt is reserved first; a refusal raises before the request.
            res = self._reserve(kind=kind, request=request)
            t0 = time.monotonic()
            _count_call(self.run_id)
            try:
                resp = self._transport.post(request)
            except TransportError:
                # No response, so no usage.cost: released (D165 (C)); a billed-but-lost request
                # is not in the ledger (the cap's accuracy is the provider's usage figures).
                self._settle(state, res, status=None, error_class="network", actual_usd=None,
                             latency_ms=_ms(t0))
                model_idx, backoff, degraded = self._after_failure(
                    state, "network", models, model_idx, backoff, degraded)
                continue
            latency_ms = _ms(t0)
            status = resp.status

            if status in (401, 402, 403):
                self._settle(state, res, status=status, error_class="provider_auth",
                             actual_usd=_error_status_cost(resp.body), latency_ms=latency_ms)
                raise FatalLlmError(
                    f"fatal transport status {status} (auth/credit) for {model!r}; run stops (resumable)"
                )

            if _is_rate_limited(status, resp.headers) or status >= 500:
                failure = "http_5xx" if status >= 500 else "rate_limit"
                self._settle(state, res, status=status, error_class=failure,
                             actual_usd=_error_status_cost(resp.body), latency_ms=latency_ms)
                model_idx, backoff, degraded = self._after_failure(
                    state, failure, models, model_idx, backoff, degraded)
                continue

            if _too_large(status, resp.body):
                self._settle(state, res, status=status, error_class="too_large",
                             actual_usd=_error_status_cost(resp.body), latency_ms=latency_ms)
                raise ContextTooLarge(f"request exceeds context or payload limit for {model!r} (HTTP {status})")

            if status == 400 and not degraded:
                # A 400 to a schema-constrained request is the provider rejecting the
                # output-schema constraint itself → degrade: re-send with the schema folded into
                # the prompt and validated client-side (v3's strict-schema degrade).
                self._settle(state, res, status=status, error_class="http_400",
                             actual_usd=_error_status_cost(resp.body), latency_ms=latency_ms)
                degraded = True
                messages = self._fold_schema(messages, schema)
                continue
            if status != 200:
                # a 400 while already degraded, or any other unexpected status → transport failure.
                self._settle(state, res, status=status, error_class=f"http_{status}",
                             actual_usd=_error_status_cost(resp.body), latency_ms=latency_ms)
                model_idx, backoff, degraded = self._after_failure(
                    state, f"http_{status}", models, model_idx, backoff, degraded)
                continue

            # ── HTTP 200 ──────────────────────────────────────────────────────
            # The chat-completions view the checks read (D722: an Anthropic reply's, anthropic_view); the response
            # digest and the cache entry keep the raw body.
            view = anthropic_view(resp.body) if dialect == "anthropic" else resp.body
            if _provider_error_200(view):
                # An error after the provider sent its 200 (D404): a 5xx, not the model's output;
                # checked before the breaker reset so an incident of these still trips it.
                self._settle(state, res, status=status, error_class="http_5xx",
                             actual_usd=_error_status_cost(resp.body), latency_ms=latency_ms)
                model_idx, backoff, degraded = self._after_failure(
                    state, "http_5xx", models, model_idx, backoff, degraded)
                continue
            with self._state_lock:
                self._consecutive_failures = 0
                self._streak_started = None
            reply = self._inspect_200(
                view, model=model, license_class=license_class, schema=schema,
                est_in=estimate_tokens(r.sent_prefix + "\n" + user), est_fixed=estimate_tokens(r.sent_prefix),
            )
            refused = dialect == "anthropic" and resp.body.get("stop_reason") == "refusal"
            self._settle(state, res, status=status, error_class="refused" if refused else reply.error_class,
                         actual_usd=reply.cost, tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
                         latency_ms=latency_ms)
            if refused:
                # D722: the provider's classifiers declined (a paid attempt); the same request would be declined
                # again, so no schema retry: the next fallback model, else the call fails.
                if model_idx + 1 < len(models):
                    model_idx += 1
                    degraded = fold_schema
                    continue
                details = resp.body.get("stop_details")
                raise ProviderRefused(f"model {model!r} declined the request (stop_reason refusal"
                                      + (f", category {details.get('category')!r}" if isinstance(details, Mapping)
                                         else "") + ")")
            if reply.truncated:
                # Cut off at the output cap, whatever parsed: a paid attempt, then ONE retry of the
                # same messages at twice the cap (not a schema attempt); cut off again → permanent.
                if out_cap == max_tokens:
                    out_cap = max_tokens * 2
                    continue
                raise TruncatedOutput(
                    f"model {model!r} reply cut off at the output cap twice (max_tokens {max_tokens}, "
                    f"then {out_cap}; {reply.tokens_out} tokens out on the last attempt)")
            errors = reply.errors

            if errors:
                if schema_attempt >= _MAX_SCHEMA_ATTEMPTS:
                    raise SchemaValidationError(
                        f"model {model!r} produced schema-invalid output on all "
                        f"{_MAX_SCHEMA_ATTEMPTS} attempts",
                        errors,
                    )
                # append the validator error as an extra user turn and retry.
                messages = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the required JSON schema:\n"
                            + "\n".join(errors)
                            + "\nReturn ONLY a corrected JSON object that matches the schema."
                        ),
                    }
                ]
                schema_attempt += 1
                continue

            # ── success ───────────────────────────────────────────────────────
            return self._answer(
                reply, body=resp.body, model=model, prompt_name=prompt_name,
                template_digest=r.template_hash, model_digest=model_digest(r.params),
                rendered_digest=rendered_digest(r.rendered_hash, input_digest),
                basis_digest=basis_digest, cost_usd=state.cost_usd, attempts=tuple(state.attempts),
                latency_ms=latency_ms, degraded=degraded,
            )

    def _inspect_200(
        self, body: Mapping[str, Any], *, model: str, license_class: str, schema: Mapping[str, Any],
        est_in: int, est_fixed: int,
    ) -> _Reply:
        """The checks of an HTTP-200 body (the call loop's and a batch result's): parse (with
        fence recovery), fill nulls, validate against ``schema``, the post-hoc egress backstop
        (logged, never failing the already-paid call), usage and cost, the truncation flag."""
        parsed, parse_err, unwrapped = _extract_parsed(body)
        if parsed is not None:
            parsed = fill_nulls(parsed, schema)
        errors = [parse_err] if parse_err is not None else (
            validate_json(parsed, schema) if parsed is not None else ["no parsed output"]
        )
        rule = self._egress_rule(license_class)
        offending = _provider_anomaly(body, rule.providers)
        if offending is not None:
            _log.warning(
                "egress anomaly: model %r under license_class %r was served by provider %r, "
                "not in the sanctioned set %s (routing pin should have prevented this)",
                model, license_class, offending, list(rule.providers),
            )
        tokens_in, tokens_in_fixed, tokens_out, cost = _usage(
            body, price=self._llm.price_for(model), est_in=est_in, est_fixed=est_fixed)
        cached, write, reasoning = usage_details(body)
        return _Reply(parsed=parsed, errors=errors, parse_error=parse_err is not None, unwrapped=unwrapped,
                      offending=offending, tokens_in=tokens_in, tokens_in_fixed=tokens_in_fixed,
                      tokens_out=tokens_out, cost=cost, truncated=_finish_reason(body) == "length",
                      tokens_cached=cached, tokens_cache_write=write, tokens_reasoning=reasoning)

    def _answer(
        self, reply: _Reply, *, body: Mapping[str, Any], model: str, prompt_name: str | None,
        template_digest: str, model_digest: str, rendered_digest: str, basis_digest: str,
        cost_usd: float, attempts: tuple[Attempt, ...], latency_ms: int, degraded: bool,
        extra: Mapping[str, Any] | None = None,
    ) -> LlmResult:
        """The result of a clean reply, and its response-cache entry (the attempt's own cost
        ``reply.cost``; ``cost_usd`` is every attempt's; ``extra``: batch fields)."""
        assert reply.parsed is not None  # a clean reply ⇒ parsed is present
        result = LlmResult(
            parsed=dict(reply.parsed), model=model, prompt_name=prompt_name,
            template_digest=template_digest, model_digest=model_digest,
            rendered_digest=rendered_digest, response_digest=response_digest(body),
            basis_digest=basis_digest, cost_usd=cost_usd, tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out, latency_ms=latency_ms, is_cache_hit=False, attempts=attempts,
            tokens_in_fixed=reply.tokens_in_fixed, degraded=degraded, unwrapped=reply.unwrapped,
            provider_anomaly=reply.offending is not None, tokens_cached=reply.tokens_cached,
            tokens_cache_write=reply.tokens_cache_write, tokens_reasoning=reply.tokens_reasoning,
        )
        if self.cache is not None:
            self.cache.put(cache_key(model, result.rendered_digest, basis_digest), {
                "model": model, "prompt_name": prompt_name,
                "template_digest": result.template_digest, "model_digest": result.model_digest,
                "rendered_digest": result.rendered_digest,
                "response_digest": result.response_digest, "basis_digest": basis_digest,
                "body": dict(body), "parsed": result.parsed, "tokens_in": reply.tokens_in,
                "tokens_in_fixed": reply.tokens_in_fixed, "tokens_out": reply.tokens_out,
                "cost_usd": reply.cost, "latency_ms": latency_ms, "degraded": degraded,
                "unwrapped": reply.unwrapped, "provider_anomaly": result.provider_anomaly,
                "run_id": self.run_id, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **(extra or {}),
            })
        return result

    def _after_failure(
        self, state: _CallState, failure: str, models: list[str], model_idx: int, backoff: float,
        degraded: bool,
    ) -> tuple[int, float, bool]:
        """Handle one transport-level failure (network error / 5xx / rate limit / an unexpected
        status; ``failure`` = its attempt ``error_class``): bump the client-wide
        consecutive-failure breaker (≥ cap failures AND a streak of ≥ ``min_failure_streak_s``
        since the last HTTP 200 ⇒ :class:`FatalLlmError`, the run stops); else, at
        this call's ``max_call_failures``, raise :class:`AttemptsExhausted` (this item fails, the
        run goes on); else advance to the next re-gated fallback, or — when fallbacks are
        exhausted — back off and restart from the primary. Returns ``(model_idx, backoff,
        degraded)``. When the resulting model DIFFERS from the failed one, ``degraded`` is reset
        to False so each provider gets its native structured-output attempt before the schema is
        folded into the prompt."""
        now = time.monotonic()
        with self._state_lock:
            self._consecutive_failures += 1
            if self._streak_started is None:
                self._streak_started = now
            streak_s = now - self._streak_started
            tripped = (self._consecutive_failures >= self._max_consecutive_failures
                       and streak_s >= self._min_failure_streak_s)
        if tripped:
            exc = FatalLlmError(
                f"≥{self._max_consecutive_failures} consecutive transport failures over {streak_s:.0f} s "
                f"with no success (last: {failure}); run stops (resumable)"
            )
            # the class of the failure that tripped it (a 429 storm says rate_limit, D431); an
            # unexpected status, outside the failed_transient vocabulary, stays network
            exc.error_class = failure if failure in ("network", "http_5xx", "rate_limit") else "network"
            raise exc
        state.failures += 1
        if state.failures >= self._max_call_failures:
            if failure in ("network", "http_5xx", "rate_limit"):
                raise AttemptsExhausted(
                    f"{state.failures} transport-level failures on this call (last: {failure})",
                    error_class=failure)
            raise AttemptsExhausted(
                f"{state.failures} transport-level failures on this call (last: {failure}: "
                f"the provider refuses the request)", error_class="query_invalid",
                status="failed_permanent")
        prev_model = models[model_idx]
        if model_idx + 1 < len(models):
            model_idx += 1  # try the next re-gated fallback (already egress-checked)
        else:
            self._sleep(backoff)  # no lock held
            model_idx = 0
            backoff = min(backoff * 2, 30.0)
        if models[model_idx] != prev_model:
            degraded = False
        return model_idx, backoff, degraded

    # ── request assembly (prompt-caching message structure) ───────────────────

    def _build_request(
        self,
        *,
        model: str,
        sent_prefix: str,
        messages: list[dict[str, Any]],
        schema: Mapping[str, Any] | None,
        max_tokens: int,
        temperature: float | None,
        rule: EgressClass,
        attempt: int,
        rendered_hash: str,
        input_digest: str,
        reasoning_effort: str | None = None,
        route: ModelRoute | None = None,
        prompt_cache_key: str | None = None,
        cache_prefix_only: bool = False,
    ) -> LlmRequest:
        """Assemble the wire request.

        **Prompt caching.** The long shared prefix is its own leading ``role: system`` message
        whose ``content`` is a single text part carrying the Anthropic ``cache_control:
        {type: ephemeral}`` breakpoint (the OpenRouter passthrough shape); only the per-document
        ``user`` messages vary, so the prefix is cache-stable within a run.

        **Egress routing pin.** ``provider.only`` is pinned to the egress slug this model maps to
        (``provider_prefixes``), so the call ships only to the gate-sanctioned counterparty and
        never drifts (v3 saw ``anthropic/…`` silently routed to Amazon Bedrock).
        ``allow_fallbacks`` follows the class rule.

        **Direct OpenAI** (a route whose provider has ``dialect: openai``, D714): :meth:`_build_openai_request`;
        **Anthropic** (``dialect: anthropic``, D722): :meth:`_build_anthropic_request`. ``route`` (D722) is the
        model's route; None = routed without a role (a qualified reference to its provider, else the default
        provider: a caller without a role, the eval harness's probe)."""
        if route is None:  # no role: a qualified reference still goes to its provider, anything else to the default
            from stack.config import RoleConfig

            route = self._llm.route(model, RoleConfig(model=model, max_tokens=1))
        provider = route.provider
        if provider.dialect == "openai":
            return self._build_openai_request(
                model=model, route=route, sent_prefix=sent_prefix, messages=messages, schema=schema,
                max_tokens=max_tokens, temperature=temperature, attempt=attempt, rendered_hash=rendered_hash,
                input_digest=input_digest, reasoning_effort=reasoning_effort, prompt_cache_key=prompt_cache_key,
                cache_prefix_only=cache_prefix_only)
        if provider.dialect == "anthropic":
            return self._build_anthropic_request(
                model=model, route=route, sent_prefix=sent_prefix, messages=messages, schema=schema,
                max_tokens=max_tokens, attempt=attempt, rendered_hash=rendered_hash, input_digest=input_digest,
                reasoning_effort=reasoning_effort, cache_prefix_only=cache_prefix_only)
        system_message: dict[str, Any] = {
            "role": "system",
            "content": [
                {"type": "text", "text": sent_prefix, "cache_control": {"type": "ephemeral"}}
            ],
        }
        provider_block: dict[str, Any] = {
            "data_collection": rule.data_collection,
            "allow_fallbacks": rule.allow_fallbacks,
        }
        model_slug = route.egress_slug
        if model_slug is not None:  # always set here (the model passed the egress gate)
            provider_block["only"] = [model_slug]
        payload: dict[str, Any] = {
            "model": route.wire_model,
            "max_tokens": max_tokens,
            "messages": [system_message, *messages],
            "provider": provider_block,
            "usage": {"include": True},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if reasoning_effort is not None:  # OpenRouter's unified reasoning parameter; absent = provider default
            payload["reasoning"] = {"effort": reasoning_effort}
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "stack_output", "strict": True, "schema": wire_schema(schema)},
            }
        # No Authorization here: the transport reads the key from the env, so it never enters
        # LlmRequest (which a fake records).
        headers = {"Content-Type": "application/json"}
        if provider.referer is not None:
            headers["HTTP-Referer"] = provider.referer
        if provider.title is not None:
            headers["X-Title"] = provider.title
        url = provider.base_url.rstrip("/") + "/chat/completions"
        return LlmRequest(
            url=url, headers=headers, payload=payload, model=model, attempt=attempt,
            rendered_hash=rendered_hash, input_digest=input_digest,
        )

    def _build_openai_request(
        self, *, model: str, route: ModelRoute, sent_prefix: str, messages: list[dict[str, Any]],
        schema: Mapping[str, Any] | None, max_tokens: int, temperature: float | None, attempt: int,
        rendered_hash: str, input_digest: str, reasoning_effort: str | None, prompt_cache_key: str | None,
        cache_prefix_only: bool = False,
    ) -> LlmRequest:
        """The OpenAI API's chat-completions request (D714): the fixed prefix as a plain leading ``system``
        message (the provider caches the longest repeated prefix itself; no ``cache_control``), the per-call
        messages after it, ``max_completion_tokens`` (reasoning models take no ``max_tokens``),
        ``reasoning_effort`` when set, the same strict ``response_format``, and ``prompt_cache_key`` (stable per
        role and prompt set). No OpenRouter routing block: the counterparty is OpenAI itself. The model id
        loses ``strip_model_prefix`` on the wire only. The key's env var NAME rides on the request.
        ``cache_prefix_only``: explicit caching (``prompt_cache_options: {mode: explicit}`` and one
        ``prompt_cache_breakpoint`` on the system part), so only the prefix is written to the cache; in the
        default (implicit) mode OpenAI writes the whole prompt, the document too, at the cache-write rate."""
        provider = route.provider
        wire_model = route.wire_model
        system: dict[str, Any] = (
            {"role": "system", "content": [{"type": "text", "text": sent_prefix,
                                            "prompt_cache_breakpoint": {"mode": "explicit"}}]}
            if cache_prefix_only else {"role": "system", "content": sent_prefix})
        payload: dict[str, Any] = {
            "model": wire_model,
            "max_completion_tokens": max_tokens,
            "messages": [system, *messages],
        }
        if cache_prefix_only:
            payload["prompt_cache_options"] = {"mode": "explicit"}
        if prompt_cache_key is not None:
            payload["prompt_cache_key"] = prompt_cache_key
        if temperature is not None:
            payload["temperature"] = temperature
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "stack_output", "strict": True, "schema": wire_schema(schema)},
            }
        url = provider.base_url.rstrip("/") + "/chat/completions"
        return LlmRequest(
            url=url, headers={"Content-Type": "application/json"}, payload=payload, model=model, attempt=attempt,
            rendered_hash=rendered_hash, input_digest=input_digest, api_key_env=provider.api_key_env,
            timeout_s=provider.timeout_s,
        )

    def _build_anthropic_request(
        self, *, model: str, route: ModelRoute, sent_prefix: str, messages: list[dict[str, Any]],
        schema: Mapping[str, Any] | None, max_tokens: int, attempt: int, rendered_hash: str, input_digest: str,
        reasoning_effort: str | None, cache_prefix_only: bool = False,
    ) -> LlmRequest:
        """The Anthropic Messages API request (D722; API version :data:`ANTHROPIC_VERSION`): the fixed prefix as
        the top-level ``system`` text block with a ``cache_control`` breakpoint (read by every later call of the
        run); the per-call messages as text blocks, consecutive same-role turns merged into one message (the
        schema-retry and fold turns follow the user turn); ``max_tokens``; ``output_config.effort`` when the role
        sets one (thinking is adaptive and on by default on the current models, so no ``thinking`` field) and
        ``output_config.format`` = the JSON schema (:data:`ANTHROPIC_WIRE_KEYWORDS`). Unless ``cache_prefix_only``,
        a top-level ``cache_control`` also caches through the last block, so a continuation (the gleaning turn)
        reads the document from the cache; with it, only the prefix is written (nothing continues the call). No
        ``temperature`` (refused by 2026-10-21; :meth:`stack.config.LlmConfig.check_route`), no routing block. The
        key goes in ``x-api-key`` (its env var NAME rides on the request); ``stream`` when the provider sets it."""
        provider = route.provider
        turns: list[dict[str, Any]] = []
        for m in messages:
            text = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
            if not text:
                continue  # the API refuses an empty text block; an empty turn carries nothing
            block = {"type": "text", "text": text}
            if turns and turns[-1]["role"] == m["role"]:
                turns[-1]["content"].append(block)
            else:
                turns.append({"role": m["role"], "content": [block]})
        payload: dict[str, Any] = {"model": route.wire_model, "max_tokens": max_tokens}
        if sent_prefix:
            payload["system"] = [{"type": "text", "text": sent_prefix, "cache_control": {"type": "ephemeral"}}]
        payload["messages"] = turns
        if not cache_prefix_only:
            payload["cache_control"] = {"type": "ephemeral"}
        output_config: dict[str, Any] = {}
        if reasoning_effort is not None:
            output_config["effort"] = reasoning_effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": wire_schema(schema, ANTHROPIC_WIRE_KEYWORDS)}
        if output_config:
            payload["output_config"] = output_config
        if provider.stream:
            payload["stream"] = True
        url = provider.base_url.rstrip("/") + "/messages"
        return LlmRequest(
            url=url, headers={"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION},
            payload=payload, model=model, attempt=attempt, rendered_hash=rendered_hash, input_digest=input_digest,
            api_key_env=provider.api_key_env, timeout_s=provider.timeout_s, key_header="x-api-key",
        )

    def _fold_schema(
        self, messages: list[dict[str, Any]], schema: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """The strict-schema degrade: fold the output schema into the prompt as an extra user
        turn (validated client-side afterwards), instead of the transport-level constraint."""
        return list(messages) + [fold_message(schema)]


def _ms(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# Stage-side budget contract
# ─────────────────────────────────────────────────────────────────────────────


class BudgetScope:
    """The stage's contract for budget refusals (D36): the first :class:`BudgetExceeded` trips the scope; that item and every later item of the stage's
    scope are ``not_attempted[budget]`` (:attr:`NOT_ATTEMPTED`) and are never sent. Items
    already in flight in other threads finish (their attempts were admitted). Thread-safe::

        scope = BudgetScope()
        for item in items:
            result = scope.call(lambda: client.complete(...))
            if result is None:            # status, error_class = BudgetScope.NOT_ATTEMPTED
                ...
    """

    NOT_ATTEMPTED: tuple[str, str] = ("not_attempted", "budget")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.refusal: BudgetExceeded | None = None

    @property
    def tripped(self) -> bool:
        return self.refusal is not None

    def call(self, fn: Callable[[], T]) -> T | None:
        """``fn()``, or None when the scope is (or by this call becomes) tripped."""
        if self.refusal is not None:
            return None
        try:
            return fn()
        except BudgetExceeded as exc:
            with self._lock:
                if self.refusal is None:
                    self.refusal = exc
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Construction
# ─────────────────────────────────────────────────────────────────────────────


def open_client(
    config: StackConfig | LlmConfig,
    *,
    run_id: str,
    stage: str,
    store_root: str | Path | None,
    budget_usd: float | None = None,
    transport: Transport | None = None,
    ledger: Ledger | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LlmClient:
    """The client a stage uses: the run's ledger (:func:`stack.llm.ledger.run_ledger`, in memory,
    one per run; the cap is ``budget_usd``, else ``llm.yaml`` ``budget.per_run_usd``), the
    response cache of ``store_root`` (None: no cache), and
    the production transport unless one is given or ``$STACK_LLM_TRANSPORT`` names one
    (:func:`env_transport`)."""
    llm = _llm_config(config)
    if transport is None:
        transport = env_transport()
    if transport is None and not provider_key(llm.provider.api_key_env):
        # before any reservation: a run without a key fails at once instead of retrying the network
        raise FatalLlmError(f"provider API key ${llm.provider.api_key_env} is unset and not in "
                            f"${ENV_FILE_ENV} (pass the env file by path; D160 (C))")
    return LlmClient(
        config=llm,
        transport=transport if transport is not None else make_default_transport(llm),
        ledger=ledger if ledger is not None else run_ledger(run_id, LLM_PROVIDER),
        run_id=run_id,
        stage=stage,
        cache=ResponseCache.for_store(store_root) if store_root is not None else None,
        budget_usd=budget_usd,
        sleep=sleep,
    )


#: run_id → transport requests its clients sent (every outbound attempt, retries and fallbacks
#: included; cache hits are not requests); :func:`run_calls` pops an entry.
_RUN_CALLS: dict[str, int] = {}
_RUN_CALLS_LOCK = threading.Lock()


def _register_run_calls(run_id: str) -> None:
    with _RUN_CALLS_LOCK:
        _RUN_CALLS.setdefault(run_id, 0)


def _count_call(run_id: str) -> None:
    with _RUN_CALLS_LOCK:
        _RUN_CALLS[run_id] = _RUN_CALLS.get(run_id, 0) + 1


def run_calls(run_id: str) -> int | None:
    """The transport requests the clients of ``run_id`` sent (each outbound attempt, so a call
    retried twice counts 3), or None when the run opened no client; forgets the run. The stage
    runner writes it as the run's ``counts.llm_calls`` (passes, ``stack build --passes``)."""
    with _RUN_CALLS_LOCK:
        return _RUN_CALLS.pop(run_id, None)


def env_transport() -> Transport | None:
    """The transport ``$STACK_LLM_TRANSPORT`` (``module:callable``, called without arguments)
    names, or None when unset. Announced on stderr: a run on a substitute transport must never
    pass for a real one. An unresolvable name raises (no silent fallback to the network)."""
    import importlib
    import os
    import sys

    spec = os.environ.get(TRANSPORT_ENV)
    if not spec:
        return None
    mod_name, _, attr = spec.partition(":")
    if not mod_name or not attr:
        raise ValueError(f"${TRANSPORT_ENV}={spec!r}: expected 'module:callable'")
    factory = getattr(importlib.import_module(mod_name), attr)
    transport = factory()
    if not isinstance(transport, Transport):
        raise TypeError(f"${TRANSPORT_ENV}={spec!r} returned {type(transport).__name__}, not a Transport")
    sys.stderr.write(f"stack.llm: transport from ${TRANSPORT_ENV}={spec} (not OpenRouter)\n")
    return transport

