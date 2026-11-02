"""The fake model: a scripted :class:`stack.llm.Transport` for every LLM test (no network).

Script replies by a substring of the rendered prompt (the system prefix + every user turn as
sent) and/or by model; the first matching rule answers. A rule with a list of replies plays them
in order and then repeats the last. A call no rule matches raises :class:`UnscriptedCall`
(loud, with the prompt's head) unless a ``default`` reply is set::

    fake = FakeLlm()
    fake.on("Title: Paper A", ok({"verdict": "relevant", "rationale": "x"}))
    fake.on("Title: Paper B", [server_error(), ok({...})])      # 5xx, then ok
    fake.on(model="openai/gpt-5.2", reply=ok({...}, cost=0.002))
    client = LlmClient(config=..., transport=fake, ...)
    ...; assert fake.count() == 2; assert fake.count(model="openai/gpt-5.2") == 1

Reply builders: :func:`ok` (JSON with ``usage`` incl. ``cost``), :func:`rate_limited` (429),
:func:`server_error` (5xx), :func:`auth_error` (401/402/403), :func:`malformed` (a 200 whose
content is not JSON), :func:`schema_invalid` (a 200 with JSON the caller's schema rejects — any
dict, named for readability), :func:`anomaly` (an ok reply served by an unsanctioned provider),
:func:`truncated` (a 200 cut off at the output cap: ``finish_reason = "length"``),
:func:`transport_error` (raised: the network failed). ``hook`` runs inside ``post`` before the
reply (tests use it for barriers); it runs with no lock held.

**Batch mode** (:mod:`stack.llm.batch`): ``FakeLlm`` is a ``BatchTransport`` too. A ``post`` to
``…/batches`` stores a :class:`FakeBatch` (``fb-<n>``; its ``model``, ``provider``, the
``(custom_id, body)`` pairs, the body's key order) and answers 202, unless
``fake.submit_replies`` (a list consumed one per POST: an ``LlmResponse`` or an exception to
raise; ``None`` = accept) says otherwise. Each ``get`` of ``…/batches/<id>`` advances that batch
one step through its status sequence (default ``validating, in_progress, completed``); on
``completed`` every body is answered by the same rules as a sync post (an exception reply becomes
the item's ``error``, an ``LlmResponse`` its ``response``), and the batch ``usage.cost`` is the sum
of the replies' costs. Per-batch scripting: ``fake.batch(...)`` appends a :class:`BatchScript`
used by the next submitted batch (the last one repeats): ``status_sequence``, ``final``
(``completed``/``failed``/``expired``/``cancelled``), ``never_finish`` (stays ``in_progress``),
``drop_ids`` / ``drop`` (a predicate on the prompt text: no result for those), ``strip_item_cost``
(results without ``usage.cost``; the batch usage keeps it), ``batch_cost`` (override),
``no_batch_usage`` (batch ``usage`` null), ``no_results`` (a completed batch whose ``results`` is
null), ``partial`` (a predicate on the prompt text: on a ``failed``/``expired``/``cancelled`` final
status those requests are answered and the batch carries them in ``results``, the part it
finished), ``lost`` (an HTTP status, 404 or 410: every poll answers it, a batch the provider
no longer knows). ``fake.batches`` lists the submitted batches;
``count()`` counts sync posts only, ``count_batched()`` the batched requests.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from stack.llm import LlmRequest, LlmResponse, TransportError

Reply = LlmResponse | Exception


class UnscriptedCall(AssertionError):
    """The fake got a request no rule matches (and no default is set)."""


def ok(
    parsed: Any,
    *,
    cost: float | None = 0.001,
    prompt_tokens: int = 120,
    completion_tokens: int = 24,
    cached_tokens: int = 0,
    provider: str | None = None,
    finish_reason: str | None = None,
) -> LlmResponse:
    """A 200 whose message content is ``json.dumps(parsed)``, with ``usage`` (``cost`` omitted
    when None, so the client falls back to tokens × price) and ``choices[0].finish_reason`` when
    given (omitted by default, so existing bodies and their digests are unchanged)."""
    usage: dict[str, Any] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if cost is not None:
        usage["cost"] = cost
    body: dict[str, Any] = {"choices": [{"message": {"content": json.dumps(parsed)}}], "usage": usage}
    if finish_reason is not None:
        body["choices"][0]["finish_reason"] = finish_reason
    if provider is not None:
        body["provider"] = provider
    return LlmResponse(200, {}, body)


def schema_invalid(parsed: Any = None, **kw: Any) -> LlmResponse:
    """A 200 with JSON that does not match the caller's schema (default ``{"nope": 1}``)."""
    return ok({"nope": 1} if parsed is None else parsed, **kw)


def malformed(text: str = "I cannot answer that in JSON.", *, cost: float | None = 0.0005) -> LlmResponse:
    """A 200 whose content is not a JSON object (not even after fence recovery)."""
    usage: dict[str, Any] = {"prompt_tokens": 100, "completion_tokens": 10}
    if cost is not None:
        usage["cost"] = cost
    return LlmResponse(200, {}, {"choices": [{"message": {"content": text}}], "usage": usage})


def truncated(
    text: str = '{"verdict": "relevant", "rationale": "cut o', *, cost: float | None = 0.002,
    completion_tokens: int = 8192,
) -> LlmResponse:
    """A 200 cut off at the output cap: ``finish_reason = "length"`` (OpenRouter's normalized
    value), content ``text`` (by default a JSON object cut mid-string), ``usage`` as :func:`ok`."""
    usage: dict[str, Any] = {"prompt_tokens": 120, "completion_tokens": completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    return LlmResponse(200, {}, {"choices": [{"message": {"content": text}, "finish_reason": "length"}],
                                 "usage": usage})


def anomaly(parsed: Any, *, provider: str = "Amazon Bedrock", **kw: Any) -> LlmResponse:
    """An ok reply OpenRouter says it served via ``provider`` (outside the sanctioned set)."""
    return ok(parsed, provider=provider, **kw)


def rate_limited(retry_after: str = "1") -> LlmResponse:
    return LlmResponse(429, {"Retry-After": retry_after}, {"error": {"message": "rate limited"}})


def server_error(status: int = 503) -> LlmResponse:
    return LlmResponse(status, {}, {"error": {"message": "upstream unavailable"}})


def auth_error(status: int = 401) -> LlmResponse:
    return LlmResponse(status, {}, {"error": {"message": "no credit"}})


def transport_error(message: str = "timed out") -> TransportError:
    return TransportError(message)


def prompt_text(request: LlmRequest) -> str:
    """Everything the request sends as text: system parts and every message, in order (an Anthropic request's
    top-level ``system`` first, D722)."""
    parts: list[str] = []
    system = request.payload.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(str(b.get("text", "")) for b in system if isinstance(b, Mapping))
    for m in request.payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(b.get("text", "")) for b in content if isinstance(b, Mapping))
    return "\n".join(parts)


@dataclass
class _Rule:
    contains: str | None
    model: str | None
    replies: list[Reply]
    served: int = 0

    def matches(self, request: LlmRequest, text: str) -> bool:
        return (self.model is None or request.model == self.model) and (
            self.contains is None or self.contains in text
        )

    def next(self) -> Reply:
        reply = self.replies[min(self.served, len(self.replies) - 1)]
        self.served += 1
        return reply


@dataclass(frozen=True)
class BatchScript:
    """How one fake batch behaves (module docstring)."""

    status_sequence: tuple[str, ...] = ("validating", "in_progress", "completed")
    final: str | None = None  # replaces the sequence's last status
    never_finish: bool = False
    drop_ids: frozenset[str] = frozenset()
    drop: Callable[[str], bool] | None = None
    strip_item_cost: bool = False
    batch_cost: float | None = None
    no_batch_usage: bool = False
    no_results: bool = False
    partial: Callable[[str], bool] | None = None
    lost: int | None = None

    def status(self, gets: int) -> str:
        seq = ("validating", "in_progress") if self.never_finish else (
            self.status_sequence if self.final is None else (*self.status_sequence[:-1], self.final))
        return seq[min(gets, len(seq)) - 1]


@dataclass
class FakeBatch:
    """One submitted fake batch."""

    id: str
    model: str
    provider: Mapping[str, Any]
    bodies: list[tuple[str, Mapping[str, Any]]]
    keys: list[str]  # the submit body's key order
    script: BatchScript
    gets: int = 0
    results: list[dict[str, Any]] | None = None
    cost: float = 0.0
    tokens: tuple[int, int] = (0, 0)

    @property
    def n(self) -> int:
        return len(self.bodies)


@dataclass
class FakeLlm:
    """A scripted transport that records every request (thread-safe)."""

    default: Reply | None = None
    hook: Callable[[LlmRequest], None] | None = None
    requests: list[LlmRequest] = field(default_factory=list)
    batches: list[FakeBatch] = field(default_factory=list)
    submit_replies: list[LlmResponse | BaseException | None] = field(default_factory=list)
    _scripts: list[BatchScript] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def batch(self, **script: Any) -> FakeLlm:
        """Script the next submitted batch (:class:`BatchScript` fields; the last one repeats)."""
        if "drop_ids" in script:
            script["drop_ids"] = frozenset(script["drop_ids"])
        with self._lock:
            self._scripts.append(BatchScript(**script))
        return self

    def on(
        self,
        contains: str | None = None,
        reply: Reply | list[Reply] | None = None,
        *,
        model: str | None = None,
    ) -> FakeLlm:
        """Answer requests whose prompt text contains ``contains`` (and whose model is
        ``model``, when given) with ``reply`` (a list plays in order, then repeats its last)."""
        if reply is None:
            raise ValueError("on(): reply is required")
        replies = list(reply) if isinstance(reply, list) else [reply]
        if not replies:
            raise ValueError("on(): empty reply list")
        with self._lock:
            self._rules.append(_Rule(contains, model, replies))
        return self

    def post(self, request: LlmRequest) -> LlmResponse:
        if request.url.rstrip("/").endswith("/batches"):
            return self._submit(request)
        with self._lock:
            self.requests.append(request)
        reply = self._match(request)
        if self.hook is not None:
            self.hook(request)
        if reply is None:
            raise UnscriptedCall(f"no rule for model {request.model!r}, prompt: {prompt_text(request)[:300]!r}")
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _match(self, request: LlmRequest) -> Reply | None:
        text = prompt_text(request)
        with self._lock:
            rule = next((r for r in self._rules if r.matches(request, text)), None)
            return rule.next() if rule is not None else self.default

    def _reply(self, request: LlmRequest) -> Reply:
        reply = self._match(request)
        if reply is None:
            raise UnscriptedCall(f"no rule for model {request.model!r}, prompt: {prompt_text(request)[:300]!r}")
        return reply

    def _submit(self, request: LlmRequest) -> LlmResponse:
        with self._lock:
            scripted = self.submit_replies.pop(0) if self.submit_replies else None
        if isinstance(scripted, BaseException):  # incl. KeyboardInterrupt (a Ctrl-C during the POST)
            raise scripted
        if scripted is not None:
            return scripted
        p = request.payload
        with self._lock:
            script = self._scripts[min(len(self.batches), len(self._scripts) - 1)] if self._scripts else BatchScript()
            fb = FakeBatch(id=f"fb-{len(self.batches) + 1}", model=p["model"], provider=p["provider"],
                           bodies=[(r["custom_id"], r["body"]) for r in p["requests"]], keys=list(p),
                           script=script)
            self.batches.append(fb)
        return LlmResponse(202, {}, {"id": fb.id, "status": "validating", "usage": None, "results": None,
                                     "error": None, "request_counts": {"total": fb.n, "completed": 0, "failed": 0}})

    def get(self, url: str, headers: Mapping[str, str]) -> LlmResponse:
        """A batch poll: advances that batch one status (module docstring)."""
        batch_id = url.rstrip("/").rsplit("/", 1)[-1]
        with self._lock:
            fb = next((b for b in self.batches if b.id == batch_id), None)
            if fb is None:
                return LlmResponse(404, {}, {"error": {"message": f"batch {batch_id} not found"}})
            fb.gets += 1
            if fb.script.lost is not None:
                return LlmResponse(fb.script.lost, {}, {"error": {"message": f"batch {batch_id} gone"}})
            status = fb.script.status(fb.gets)
        if status in ("completed", "failed", "expired", "cancelled") and fb.results is None:
            self._answer(fb, completed=status == "completed")
        obj: dict[str, Any] = {"id": fb.id, "status": status, "usage": None, "results": None, "error": None,
                               "request_counts": {"total": fb.n, "completed": 0, "failed": 0}}
        if fb.results is not None and status in ("completed", "failed", "expired", "cancelled"):
            cost = fb.script.batch_cost if fb.script.batch_cost is not None else fb.cost
            if not fb.script.no_batch_usage:
                obj["usage"] = {"prompt_tokens": fb.tokens[0], "completion_tokens": fb.tokens[1],
                                "total_tokens": sum(fb.tokens), "cost": cost, "is_byok": False}
            if (status == "completed" and not fb.script.no_results) or (
                    status != "completed" and fb.script.partial is not None):
                obj["results"] = fb.results
                obj["request_counts"] = {"total": fb.n, "completed": sum(r["response"] is not None for r in fb.results),
                                         "failed": sum(r["error"] is not None for r in fb.results)}
            if status != "completed":
                obj["error"] = {"message": f"fake batch {status}"}
        return LlmResponse(200, {}, obj)

    def _answer(self, fb: FakeBatch, *, completed: bool) -> None:
        """The batch's results by the sync rules (``completed=False``: a failed/expired batch, whose
        results are those its ``partial`` predicate picks, none without one)."""
        results: list[dict[str, Any]] = []
        cost, t_in, t_out = 0.0, 0, 0
        for i, (cid, body) in enumerate(fb.bodies):
            request = LlmRequest(url="fake://chat/completions", headers={}, payload=body, model=str(body["model"]),
                                 attempt=1, rendered_hash="", input_digest="")
            if not completed and (fb.script.partial is None or not fb.script.partial(prompt_text(request))):
                continue
            if cid in fb.script.drop_ids or (fb.script.drop is not None and fb.script.drop(prompt_text(request))):
                continue
            reply = self._reply(request)
            item: dict[str, Any] = {"id": f"{fb.id}-{i}", "custom_id": cid, "response": None, "error": None}
            if isinstance(reply, Exception):
                item["error"] = {"code": 502, "message": str(reply)}
            else:
                rbody = json.loads(json.dumps(reply.body))
                usage = rbody.get("usage") if isinstance(rbody.get("usage"), dict) else {}
                cost += float(usage.get("cost") or 0.0)
                t_in += int(usage.get("prompt_tokens") or 0)
                t_out += int(usage.get("completion_tokens") or 0)
                if fb.script.strip_item_cost:
                    usage.pop("cost", None)
                item["response"] = {"status_code": reply.status, "request_id": f"req-{cid}", "body": rbody}
            results.append(item)
        with self._lock:
            fb.results, fb.cost, fb.tokens = results, cost, (t_in, t_out)

    def count(self, *, model: str | None = None, contains: str | None = None) -> int:
        """Requests seen (optionally only those of ``model`` / whose prompt contains ``contains``)."""
        with self._lock:
            reqs = list(self.requests)
        return sum(
            1 for r in reqs
            if (model is None or r.model == model) and (contains is None or contains in prompt_text(r))
        )


    def count_batched(self, *, model: str | None = None, contains: str | None = None) -> int:
        """Requests submitted in batches (optionally only those of ``model`` / whose prompt
        contains ``contains``)."""
        with self._lock:
            pairs = [(b.model, body) for b in self.batches for _, body in b.bodies]
        return sum(
            1 for m, body in pairs
            if (model is None or m == model) and (contains is None or contains in prompt_text(
                LlmRequest(url="", headers={}, payload=body, model=m, attempt=1, rendered_hash="", input_digest="")))
        )


#: The fake :func:`installed` hands out: the seam ``$STACK_LLM_TRANSPORT=tests.fakes.llm:installed``
#: (``stack.llm.env_transport``) for runs through the ``stack`` entry point. A test sets it.
INSTALLED: FakeLlm | None = None


def installed() -> FakeLlm:
    """:data:`INSTALLED`, or a loud error when no test installed one (never a silent default)."""
    if INSTALLED is None:
        raise RuntimeError("tests.fakes.llm.INSTALLED is not set: install a FakeLlm before the run")
    return INSTALLED


def session_default() -> FakeLlm:
    """The test session's default transport: ``tests/conftest.py`` sets
    ``$STACK_LLM_TRANSPORT=tests.fakes.llm:session_default`` for every test that does not opt in
    to the real one (the ``real_llm`` fixture). :data:`INSTALLED` when a test installed one, else
    a :class:`FakeLlm` with no rules: any call raises :class:`UnscriptedCall` (the run fails
    loudly), and nothing reaches the network whatever keys the environment holds."""
    return INSTALLED if INSTALLED is not None else FakeLlm()
