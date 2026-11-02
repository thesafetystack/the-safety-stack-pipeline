"""Batch mode for one stage run: OpenRouter's ``/batches`` API at half the sync price.

**Collect, submit, replay.** :func:`prefetch` runs the stage's per-item asks (thunks against one
:class:`stack.llm.LlmClient`) in *collect* passes. In a collect pass ``complete()`` answers from
the response cache or this run's batch outcomes; otherwise it records the first request the call
loop would send and raises :class:`stack.llm.Deferred` (the item's ask stops there). The driver
submits the collected requests as batches while the pass goes on (one model and one provider per
batch: a group per ``(model, provider slug)``; a chunk closes at ``max_requests`` or
``max_bytes``), polls every batch to a terminal status, and settles each result through the
client's own HTTP-200 code (parse, strict-schema validation, usage, truncation, provider check,
ledger reconcile, the response-cache entry with ``via: batch``). Another collect pass follows
(an answer can admit a further call, e.g. screen's tier 2) until a pass collects nothing new or
``max_passes``. Then :meth:`stack.llm.LlmClient.batch_replay` switches the client to the
*replay* pass, in which the stage's normal loop runs once more and every call gets its outcome:

* ``answered`` — the cached answer, ``is_cache_hit=False``, the batch attempt's cost;
* ``retry_sync`` — the batch reply was not usable (5xx/other status, a 200 carrying a provider
  error, settled as a 5xx (D429), an ``error`` item, a missing result, parse error, schema-invalid,
  truncated): asked sync through the call loop, whose
  ``attempts``/``cost_usd`` start with the paid batch attempt;
* ``sync`` — the pass collected fewer than ``min_requests`` (nothing of it submitted): asked sync;
* ``refused`` — the budget stop: :class:`stack.llm.BudgetExceeded`;
* ``fatal`` — 401/402/403 at submit or in a result: :class:`stack.llm.FatalLlmError`;
* ``error`` — 413 or a context-length 400 in a result: :class:`stack.llm.ContextTooLarge`;
* ``unanswered`` — time cap, failed/expired/lost batch, submit error: :class:`stack.llm.BatchUnanswered`
  (the stage marks the item ``not_attempted[unanswered]``; the next run asks again).

**Money.** Each request is reserved right before its chunk is submitted, at the client's
reservation formula × :data:`BATCH_PRICE_FACTOR`. A refusal with batches in flight submits what
the chunk reserved so far, waits for the in-flight batches to settle (their actuals free headroom)
and retries; with nothing in flight it is the budget stop (this request and every one not yet
submitted ``refused``, no further pass). There is no cancel: a batch still running at the time
cap (``wait_minutes``; else a hard stop at 26 h) is *abandoned*, billed anyway, and each of its
reservations is reconciled at ``reserved × ratio`` (Σ actual / Σ reserved over this run's
batched requests settled so far, 1.0 if none). So is everything in flight when anything raises out
of the passes (a thunk's bug, the heartbeat, ``KeyboardInterrupt``; reserved
requests not yet sent are released, the client goes to the replay pass, the exception propagates),
and a submit whose answer was lost to the network (it may have been accepted and cannot be found by
content, so it is not sent again; its requests are ``unanswered``). A result's actual is its
``usage.cost``; the batch ``usage.cost`` left over is shared over the calls without one (a 200
without a cost, an ``error`` item, a missing result) in proportion to tokens × price × factor for a
200 and to the reserved amount for the others, so the run's ledger total for a batch is its
``usage.cost`` unless every result carries its own; with no batch cost, a 200 without one is
tokens × price × factor and the others cost nothing. A ``failed``/``expired``/``cancelled`` batch
that carries a ``results`` list (the part it finished) settles those results as a completed batch
does; its other requests are ``unanswered``. A **lost** batch (a poll answers 404 or 410: the
provider no longer knows it; it may have run and been billed) is charged in full: each of its
reservations is reconciled at the reserved amount (D370), its requests are ``unanswered``, and its
id moves to ``spend.abandoned_batch_ids`` (D366).

**Wire.** ``POST {base_url}/batches`` with keys in this order (the API stream-parses): ``endpoint``,
``model``, ``provider`` (``only`` + ``allow_fallbacks`` only: the egress rule's
``data_collection`` is not accepted there), ``requests: [{custom_id, body}]`` where ``body`` is the
sync payload without its ``provider`` block; ``GET {base_url}/batches/{id}`` polls. A transport
must be a :class:`BatchTransport` (``post`` + ``get``).

Nothing is persisted between runs except the response cache: the batch ids are on the
:class:`BatchReport`, printed to stderr for the run's log, and, while in flight, in the run
record's ``spend.batch_ids`` (:meth:`stack.llm.ledger.Ledger.batch_submitted`), and once abandoned in its
``spend.abandoned_batch_ids``, final record included. Reservations are
``batched``, so the run's end (:func:`stack.llm.ledger.close_run`) never releases one at $0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Protocol, runtime_checkable

from stack.llm import (
    BatchCall,
    BatchOutcome,
    LlmClient,
    LlmRequest,
    LlmResponse,
    _count_call,
    _error_status_cost,
    _provider_error_200,
    _too_large,
    _usage,
)
from stack.llm.errors import (
    BatchUnanswered,
    BudgetExceeded,
    ContextTooLarge,
    Deferred,
    FatalLlmError,
    LlmError,
    TransportError,
)

#: OpenRouter model list, 2026-10-13: every ``:batch`` variant is priced at exactly half the sync
#: price, on every price line (input, output, cache reads).
BATCH_PRICE_FACTOR = 0.5
ENDPOINT = "/v1/chat/completions"
TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})
HARD_STOP_S = 26 * 3600.0  # the API's 24 h completion window, plus slack
SUBMIT_BACKOFF_S = (2.0, 8.0, 30.0)  # the retries of a submit answered 5xx or 429 (never after a network error)
_MSG_CHARS = 300


@runtime_checkable
class BatchTransport(Protocol):
    """A :class:`stack.llm.Transport` that can also GET (a batch poll). Same error contract:
    any HTTP status is an :class:`LlmResponse`; only a network failure raises ``TransportError``."""

    def post(self, request: LlmRequest) -> LlmResponse: ...

    def get(self, url: str, headers: Mapping[str, str]) -> LlmResponse: ...


@dataclass(frozen=True)
class BatchOptions:
    """How :func:`prefetch` batches. A chunk closes at ``max_requests`` or ``max_bytes`` (one
    request larger than ``max_bytes`` goes alone). A pass that wants fewer than ``min_requests``,
    with nothing of it submitted yet, is asked sync instead. ``wait_minutes``: the time cap from
    the driver's start (None: until every batch is terminal, hard stop at 26 h). ``poll_s``: the
    poll interval for a batch younger than 15 min, younger than 1 h, older."""

    max_requests: int = 2000
    max_bytes: int = 40_000_000
    min_requests: int = 100
    wait_minutes: float | None = None
    max_passes: int = 4
    poll_s: tuple[float, ...] = (15, 30, 60)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> BatchOptions:
        """From a stage line's options (``batch_max_requests``, ``batch_max_mb``,
        ``batch_min_requests``, ``batch_wait_minutes``; other keys ignored). A bad value raises
        ``ValueError`` naming its key."""
        kw: dict[str, Any] = {}
        if "batch_max_requests" in options:
            kw["max_requests"] = _number(options, "batch_max_requests", integer=True, minimum=1)
        if "batch_max_mb" in options:
            kw["max_bytes"] = int(_number(options, "batch_max_mb", integer=False, minimum=0.001) * 1_000_000)
        if "batch_min_requests" in options:
            kw["min_requests"] = _number(options, "batch_min_requests", integer=True, minimum=0)
        if options.get("batch_wait_minutes") is not None:
            kw["wait_minutes"] = float(_number(options, "batch_wait_minutes", integer=False, minimum=0.001))
        return cls(**kw)


def _number(options: Mapping[str, Any], key: str, *, integer: bool, minimum: float) -> Any:
    value = options[key]
    ok = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok or value < minimum:
        kind = "an integer" if integer else "a number"
        raise ValueError(f"option {key}: {value!r} is not {kind} >= {minimum}")
    return value


@dataclass
class BatchReport:
    """What :func:`prefetch` did; :meth:`as_counts` goes into the run's counts. Per request:
    ``requests`` submitted, then exactly one of ``answered``, ``retry_sync``, ``sync``,
    ``refused_budget``, ``unanswered``, ``fatal``, ``error`` per collected key."""

    passes: int = 0
    requests: int = 0
    batches: int = 0
    answered: int = 0
    retry_sync: int = 0
    sync: int = 0
    refused_budget: int = 0
    unanswered: int = 0
    fatal: int = 0
    error: int = 0
    batches_failed: int = 0  # failed, cancelled or lost (404/410 on a poll)
    batches_expired: int = 0
    batches_abandoned: int = 0
    submit_errors: int = 0
    wait_s: int = 0
    batch_ids: list[str] = field(default_factory=list)

    def as_counts(self, prefix: str = "batch.") -> dict[str, int]:
        return {f"{prefix}{f.name}": getattr(self, f.name) for f in fields(self) if f.name != "batch_ids"}


_COUNTER = {"answered": "answered", "retry_sync": "retry_sync", "sync": "sync", "refused": "refused_budget",
            "unanswered": "unanswered", "fatal": "fatal", "error": "error"}


def prefetch(
    client: LlmClient,
    asks: Callable[[], Iterable[Callable[[], object]]],
    *,
    options: BatchOptions,
    label: str = "llm",
    heartbeat: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchReport:
    """Run the collect passes and the batches for ``client``'s run, then switch it to the replay
    pass (module docstring). ``asks()`` returns a fresh iterable of thunks per pass, each running
    one item's ask against ``client``; ``Deferred``, ``BatchUnanswered`` and every ``LlmError``
    a thunk raises are swallowed, anything else propagates. ``heartbeat()`` runs at every poll
    round. Needs the response cache (``ValueError``) and a :class:`BatchTransport` (``TypeError``)."""
    if client.cache is None:
        raise ValueError("batch mode needs the response cache (open the client with a store)")
    if not isinstance(client._transport, BatchTransport):
        raise TypeError(f"batch mode needs a BatchTransport (post + get), not {type(client._transport).__name__}")
    return _Driver(client, asks, options, label, heartbeat, clock, sleep).run()


@dataclass
class _Batch:
    id: str
    calls: list[BatchCall]
    submitted_at: float
    status: str


class _Driver:
    def __init__(
        self, client: LlmClient, asks: Callable[[], Iterable[Callable[[], object]]], options: BatchOptions,
        label: str, heartbeat: Callable[[], None] | None, clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.client, self.asks, self.opt, self.label = client, asks, options, label
        self.heartbeat, self.clock, self.sleep = heartbeat, clock, sleep
        self.transport: BatchTransport = client._transport  # type: ignore[assignment]
        self.base = client._llm.provider.base_url.rstrip("/")
        self.report = BatchReport()
        self.start = clock()
        self.deadline = self.start + (options.wait_minutes * 60 if options.wait_minutes is not None else HARD_STOP_S)
        self.inflight: list[_Batch] = []
        self.headers: Mapping[str, str] = {"Content-Type": "application/json"}
        self.stop: str | None = None  # "budget" | "fatal" | "deadline": nothing more is submitted
        self.stop_exc: LlmError | None = None
        self.settled_actual = 0.0  # Σ actual / Σ reserved of settled batched requests: the abandon ratio
        self.settled_reserved = 0.0
        # requests whose reservation is open outside a batch in flight: reserved, not yet sent
        # (released if anything raises), being sent (billed at the ratio), being settled
        self.unposted: list[BatchCall] = []
        self.posting: list[BatchCall] = []
        self.settling: _Batch | None = None
        self.n = 0
        self.silent = os.environ.get("STACK_PROGRESS", "").lower() in ("0", "off", "false")
        # per pass
        self.open: dict[tuple[str, str], list[BatchCall]] = {}
        self.open_bytes: dict[tuple[str, str], int] = {}
        self.held: list[list[BatchCall]] = []
        self.wanted = 0
        self.pass_submitted = False

    # ── passes ────────────────────────────────────────────────────────────────

    def run(self) -> BatchReport:
        try:
            for _ in range(self.opt.max_passes):
                self.client.batch_collect()
                if not self._collect_pass():
                    break
                self.report.passes += 1  # passes that collected requests
                if self.stop is not None:
                    break
        except BaseException:
            self._interrupted()
            raise
        self.client.batch_replay()
        return self._summary("done")

    def _interrupted(self) -> None:
        """Something raised out of the passes: every batch in flight (and one being settled, for
        its calls still open) is abandoned as at the deadline, a submit under way is billed as a
        lost one, reserved requests not yet sent are released; the client goes to the replay pass
        and the batch ids are printed. Best effort: a failure here is printed, and the caller
        re-raises the original exception either way."""
        try:
            if self.stop is None:
                self.stop = "interrupted"
            open_ids = {x["reservation_id"] for x in self.client.ledger.outstanding([self.client.run_id])}
            if self.settling is not None:
                self.inflight.append(self.settling)
                self.settling = None
            self._abandon(open_ids)
            if self.posting:
                posting, self.posting = self.posting, []
                self._submit_lost(posting, "interrupted while submitting")
            unposted, self.unposted = self.unposted, []
            self._release(unposted)
            for call in unposted:
                self._outcome(call, BatchOutcome("unanswered"))
        except Exception as exc:  # noqa: BLE001 - the original exception is re-raised by run()
            sys.stderr.write(f"[batch {self.label}] cleanup after an exception failed: {type(exc).__name__}: "
                             f"{str(exc)[:_MSG_CHARS]}\n")
        finally:
            self.client.batch_replay()
            self._summary("interrupted")

    def _summary(self, how: str) -> BatchReport:
        r = self.report
        r.wait_s = int(self.clock() - self.start)
        sys.stderr.write(
            f"[batch {self.label}] {how}: {r.passes} pass(es), {r.requests:,} requests in {r.batches} batch(es); "
            f"{r.answered:,} answered, {r.retry_sync:,} retry_sync, {r.sync:,} sync, {r.unanswered:,} unanswered, "
            f"{r.refused_budget:,} refused, {r.fatal + r.error:,} fatal/error; {r.wait_s // 60} min"
            + (f"; batches: {', '.join(r.batch_ids)}" if r.batch_ids else "") + "\n")
        return r

    def _collect_pass(self) -> bool:
        self.open, self.open_bytes, self.held, self.wanted, self.pass_submitted = {}, {}, [], 0, False
        for thunk in self.asks():
            try:
                thunk()
            except (Deferred, BatchUnanswered, LlmError):
                pass
            for call in self.client._batch_take():
                self._add(call)
        rest = [*self.held, *self.open.values()]
        self.held, self.open, self.open_bytes = [], {}, {}
        if self.stop is not None:
            self._doom([c for chunk in rest for c in chunk])
        elif self.wanted < self.opt.min_requests and not self.pass_submitted:
            for call in (c for chunk in rest for c in chunk):
                self._outcome(call, BatchOutcome("sync"))
        else:
            for chunk in rest:
                self._submit(chunk)
        self._wait()
        return self.wanted > 0

    def _add(self, call: BatchCall) -> None:
        self.wanted += 1
        if self.stop is not None:
            self._doom([call])
            return
        assert call.request is not None
        self.n += 1
        call.custom_id = f"r{self.client.batch_pass}-{self.n}"
        call.size = len(json.dumps({"custom_id": call.custom_id, "body": _body(call)}).encode("utf-8"))
        group = (call.model, call.slug)
        if group in self.open and self.open_bytes[group] + call.size > self.opt.max_bytes:
            self._close(group)
        chunk = self.open.setdefault(group, [])
        chunk.append(call)
        self.open_bytes[group] = self.open_bytes.get(group, 0) + call.size
        if len(chunk) >= self.opt.max_requests or self.open_bytes[group] >= self.opt.max_bytes:
            self._close(group)

    def _close(self, group: tuple[str, str]) -> None:
        """A full chunk: submitted once the pass has ``min_requests`` (held until then)."""
        self.held.append(self.open.pop(group))
        del self.open_bytes[group]
        if self.wanted >= self.opt.min_requests or self.pass_submitted:
            held, self.held = self.held, []
            for chunk in held:
                self._submit(chunk)

    # ── submit ────────────────────────────────────────────────────────────────

    def _submit(self, chunk: list[BatchCall]) -> None:
        self.pass_submitted = True
        self.unposted = []  # reserved, not yet sent
        for i, call in enumerate(chunk):
            while True:
                if self.stop is None and self.clock() >= self.deadline:
                    self._abandon()
                if self.stop is not None:
                    self._doom(chunk[i:])
                    unposted, self.unposted = self.unposted, []
                    if unposted:
                        self._doom(unposted)
                    return
                assert call.request is not None
                try:
                    call.reservation = self.client._reserve(
                        kind=call.attempt_kind, request=call.request, factor=BATCH_PRICE_FACTOR, batched=True)
                    self.unposted.append(call)
                    break
                except BudgetExceeded as exc:
                    if self.unposted:
                        self._post()
                    if self.inflight:
                        self._wait()  # their actuals free headroom; then retry this request
                        continue
                    if self.stop is None:
                        self.stop, self.stop_exc = "budget", exc
        if self.unposted:
            self._post()

    def _post(self) -> None:
        """Submit the reserved requests (:attr:`unposted`) as one batch."""
        self.posting, self.unposted = self.unposted, []
        self._send(self.posting)
        self.posting = []

    def _send(self, calls: list[BatchCall]) -> None:
        first = calls[0]
        assert first.request is not None
        self.headers = first.request.headers
        payload = {  # key order matters: the API stream-parses the body
            "endpoint": ENDPOINT,
            "model": first.model,
            "provider": {"only": [first.slug], "allow_fallbacks": False},
            "requests": [{"custom_id": c.custom_id, "body": _body(c)} for c in calls],
        }
        request = LlmRequest(url=f"{self.base}/batches", headers=first.request.headers, payload=payload,
                             model=first.model, attempt=0, rendered_hash="", input_digest="")
        for attempt in range(len(SUBMIT_BACKOFF_S) + 1):
            try:
                resp = self.transport.post(request)
            except TransportError as exc:  # the server may have accepted it: never sent twice
                self._submit_lost(calls, f"network: {str(exc)[:_MSG_CHARS]}")
                return
            if resp.status in (200, 201, 202) and isinstance(resp.body.get("id"), str):
                self._accepted(calls, resp.body)
                return
            if resp.status in (401, 402, 403):
                self._release(calls)
                if self.stop is None or self.stop == "budget":
                    self.stop = "fatal"
                    self.stop_exc = FatalLlmError(
                        f"batch submit: fatal status {resp.status} (auth/credit); run stops (resumable)")
                self._doom(calls)
                self._say(f"submit refused: HTTP {resp.status} (auth/credit); stopping", force=True)
                return
            problem = f"HTTP {resp.status}: {_message(resp.body)}"
            if (resp.status >= 500 or resp.status == 429) and attempt < len(SUBMIT_BACKOFF_S):
                self.sleep(SUBMIT_BACKOFF_S[attempt])  # the server answered: nothing was accepted
                continue
            break
        self.report.submit_errors += 1
        self._release(calls)
        for call in calls:
            self._outcome(call, BatchOutcome("unanswered"))
        self._say(f"submit of {len(calls):,} requests failed ({problem}); they stay unanswered", force=True)

    def _submit_lost(self, calls: list[BatchCall], why: str) -> None:
        """A submit with no answer: the batch may exist and be billed, and it cannot be found by
        content, so it is not sent again; its reservations are reconciled at reserved × ratio, as
        an abandoned batch's, and its requests are ``unanswered``."""
        self.report.submit_errors += 1
        ratio = self._bill_at_ratio(calls, "submit_lost")
        for call in calls:
            self._outcome(call, BatchOutcome("unanswered"))
        self._say(f"submit of {len(calls):,} requests got no answer ({why}); not sent again (the batch may "
                  f"exist and be billed): reconciled at reserved × {ratio:.3f}, unanswered", force=True)

    def _accepted(self, calls: list[BatchCall], body: Mapping[str, Any]) -> None:
        batch_id = str(body["id"])
        for call in calls:
            call.request = None  # the payload is sent: drop it
            _count_call(self.client.run_id)
        self.inflight.append(_Batch(batch_id, calls, self.clock(), str(body.get("status") or "validating")))
        self.client.ledger.batch_submitted(batch_id)  # in the run record's spend.batch_ids until it ends
        self.report.requests += len(calls)
        self.report.batches += 1
        self.report.batch_ids.append(batch_id)
        self._say(f"submitted {len(calls):,} (batch {batch_id})")

    # ── poll and settle ───────────────────────────────────────────────────────

    def _wait(self) -> None:
        """Poll every in-flight batch to a terminal status, settling each; at the deadline the
        rest is abandoned."""
        while self.inflight:
            now = self.clock()
            if now >= self.deadline:
                self._abandon()
                return
            interval = min(self._interval(now - b.submitted_at) for b in self.inflight)
            self.sleep(min(interval, self.deadline - now))
            if self.heartbeat is not None:
                self.heartbeat()
            for b in list(self.inflight):
                if b in self.inflight:  # a fatal poll abandons the rest of the round
                    self._poll(b)

    def _interval(self, age_s: float) -> float:
        p = self.opt.poll_s
        return p[0] if age_s < 15 * 60 else p[1] if age_s < 3600 else p[-1]

    def _poll(self, b: _Batch) -> None:
        try:
            resp = self.transport.get(f"{self.base}/batches/{b.id}", self.headers)
        except TransportError:
            return  # retried at the next round
        if resp.status in (404, 410):  # lost: listed as abandoned (D366), charged in full (D370)
            self.inflight.remove(b)
            self.client.ledger.batch_ended(b.id, abandoned=True)
            self.report.batches_failed += 1
            for call in b.calls:
                assert call.reservation is not None
                self.client._batch_attempt(call, status=resp.status, error_class="batch_lost",
                                           actual_usd=call.reservation.reserved_usd)
                self._outcome(call, BatchOutcome("unanswered"))
            self._say(f"batch {b.id} lost (HTTP {resp.status}); {len(b.calls):,} unanswered, charged in full "
                      "(it may have run and been billed)", force=True)
            return
        if resp.status in (401, 402, 403):
            if self.stop != "fatal":
                self.stop = "fatal"
                self.stop_exc = FatalLlmError(f"batch poll: fatal status {resp.status} (auth/credit); run stops")
            self._say(f"poll refused: HTTP {resp.status} (auth/credit); abandoning in-flight batches", force=True)
            self._abandon()
            return
        if resp.status != 200:
            return  # 5xx, 429, other: retried at the next round
        status = str(resp.body.get("status"))
        if status in TERMINAL:
            self.inflight.remove(b)
            self._settle(b, resp.body)
            self.client.ledger.batch_ended(b.id)
        if status != b.status:
            b.status = status
            self._say(f"batch {b.id} {status}")

    def _settle(self, b: _Batch, obj: Mapping[str, Any]) -> None:
        """A terminal batch. Each result present settles by its status (a ``failed``/``expired``/
        ``cancelled`` batch may carry the part it finished); a call without a usable result (an
        ``error`` item, none) is ``retry_sync`` after a completed batch, else ``unanswered``."""
        self.settling = b
        latency_ms = round((self.clock() - b.submitted_at) * 1000)
        usage = obj.get("usage")
        batch_cost = _num(usage.get("cost")) if isinstance(usage, Mapping) else None
        completed = obj.get("status") == "completed"
        results = obj.get("results")
        by_id = {r.get("custom_id"): r for r in (results if isinstance(results, list) else [])
                 if isinstance(r, Mapping)}
        if not completed:
            if obj.get("status") == "expired":
                self.report.batches_expired += 1
            else:
                self.report.batches_failed += 1
            self._say(f"batch {b.id} {obj.get('status')}: {_message(obj)}"
                      + (f"; {len(by_id):,} result(s) settled" if by_id else ""), force=True)
        costs = self._costs(b, by_id, batch_cost)
        for call in b.calls:
            self._settle_one(b, call, by_id.get(call.custom_id), costs, latency_ms, completed)
        self.settling = None

    def _costs(self, b: _Batch, by_id: Mapping[Any, Mapping[str, Any]], batch_cost: float | None) -> dict[str, float]:
        """custom_id → actual: a result's own ``usage.cost``; the batch cost these leave is shared
        over the calls without one, in proportion to tokens × price × factor (a 200) or the
        reserved amount (an ``error`` item, no result). With no batch cost a 200 costs tokens ×
        price × factor; a call missing from the map is released."""
        present: dict[str, float] = {}
        estimated: dict[str, float] = {}  # 200 results without a cost
        missing: dict[str, float] = {}  # no response: an `error` item or no result
        for call in b.calls:
            response = _response(by_id.get(call.custom_id))
            if response is None:
                assert call.reservation is not None
                missing[call.custom_id] = call.reservation.reserved_usd
                continue
            body = _body_of(response)
            cost = _num(body.get("usage", {}).get("cost") if isinstance(body.get("usage"), Mapping) else None)
            if cost is not None:
                present[call.custom_id] = cost
            elif response.get("status_code") == 200 and not _provider_error_200(body):
                price = self.client._llm.price_for(call.model)
                t_in, _, t_out, _ = _usage(body, price=price, est_in=call.est_in, est_fixed=call.est_fixed)
                estimated[call.custom_id] = price.cost(t_in, t_out) * BATCH_PRICE_FACTOR
        out = dict(present)
        rest = max(0.0, batch_cost - sum(present.values())) if batch_cost is not None else 0.0
        weights = {**estimated, **missing}
        if batch_cost is not None and weights:
            total = sum(weights.values())
            for cid, w in weights.items():
                share = rest * (w / total if total > 0 else 1 / len(weights))
                if cid in estimated or share > 0:
                    out[cid] = share
        else:
            out.update(estimated)
        return out

    def _settle_one(self, b: _Batch, call: BatchCall, result: Mapping[str, Any] | None,
                    costs: Mapping[str, float], latency_ms: int, completed: bool) -> None:
        response = _response(result)
        if response is None:  # an `error` item, or no result for this custom_id
            share = costs.get(call.custom_id)  # its part of the batch cost left over, if any
            attempt = self.client._batch_attempt(call, status=None, actual_usd=share, latency_ms=latency_ms,
                                                 error_class="batch_error" if completed else "batch_failed")
            if share is not None:
                self._settled(call, share)
            self._outcome(call, BatchOutcome("retry_sync", cost_usd=share or 0.0, attempts=(attempt,))
                          if completed else BatchOutcome("unanswered"))
            return
        status = _int(response.get("status_code"))
        body = _body_of(response)
        provider_error = status == 200 and _provider_error_200(body)  # D404 in batch mode (D429)
        if status == 200 and not provider_error:
            cost = costs[call.custom_id]
            outcome = self.client._batch_settle_200(call, body, cost=cost, batch_id=b.id, latency_ms=latency_ms)
            self._settled(call, cost)
            self._outcome(call, outcome)
            return
        actual = _error_status_cost(body)
        if status in (401, 402, 403):
            error_class = "provider_auth"
        elif _too_large(status, body):
            error_class = "too_large"
        else:
            error_class = ("http_5xx" if status >= 500 or provider_error else "rate_limit" if status == 429
                           else f"http_{status}")
        attempt = self.client._batch_attempt(call, status=status, error_class=error_class, actual_usd=actual,
                                             latency_ms=latency_ms)
        self._settled(call, actual)
        if error_class in ("provider_auth", "too_large"):
            exc: LlmError = (FatalLlmError(f"batch result: fatal status {status} (auth/credit) for {call.model!r}; "
                                           f"run stops (resumable)") if error_class == "provider_auth" else
                             ContextTooLarge(f"request exceeds context or payload limit for {call.model!r} "
                                             f"(HTTP {status}, batch)"))
            exc.attempts = (attempt,)  # type: ignore[attr-defined]
            exc.cost_usd = actual  # type: ignore[attr-defined]
            if error_class == "provider_auth" and self.stop in (None, "budget"):
                self.stop, self.stop_exc = "fatal", exc
            self._outcome(call, BatchOutcome("fatal" if error_class == "provider_auth" else "error", cost_usd=actual,
                                             attempts=(attempt,), error=exc))
            return
        self._outcome(call, BatchOutcome("retry_sync", cost_usd=actual, attempts=(attempt,)))

    def _abandon(self, open_ids: set[str] | None = None) -> None:
        """The deadline, a fatal poll or an exception: every in-flight batch keeps running and is
        billed; its reservations (those in ``open_ids`` when given: a batch interrupted while
        settling) are reconciled at reserved × the settled ratio; its requests are unanswered
        (fatal after a fatal poll)."""
        if self.stop is None:
            self.stop = "deadline"
        for b in self.inflight:
            self.report.batches_abandoned += 1
            calls = [c for c in b.calls if open_ids is None
                     or (c.reservation is not None and c.reservation.reservation_id in open_ids)]
            self._bill_at_ratio(calls, "abandoned")
            self._doom(calls)
            self._say(f"batch {b.id} abandoned ({b.status}): it keeps running and is billed", force=True)
            self.client.ledger.batch_ended(b.id, abandoned=True)
        self.inflight = []

    def _bill_at_ratio(self, calls: list[BatchCall], error_class: str) -> float:
        """Reconcile each reservation at reserved × the ratio (Σ actual / Σ reserved of this run's
        settled batched requests, 1.0 if none): requests billed that this run will not see settled."""
        ratio = self.settled_actual / self.settled_reserved if self.settled_reserved > 0 else 1.0
        for call in calls:
            assert call.reservation is not None
            self.client._batch_attempt(call, status=None, error_class=error_class,
                                       actual_usd=call.reservation.reserved_usd * ratio)
        return ratio

    # ── outcomes ──────────────────────────────────────────────────────────────

    def _doom(self, calls: list[BatchCall]) -> None:
        """Calls that will not be (or were not) answered by a batch after a stop: released, and
        ``refused`` (budget), ``fatal`` or ``unanswered`` (deadline)."""
        self._release(calls)
        for call in calls:
            if self.stop == "budget":
                self._outcome(call, BatchOutcome("refused", error=self.stop_exc))
            elif self.stop == "fatal":
                self._outcome(call, BatchOutcome("fatal", error=self.stop_exc))
            else:
                self._outcome(call, BatchOutcome("unanswered"))

    def _release(self, calls: list[BatchCall]) -> None:
        for call in calls:
            if call.reservation is not None:
                self.client.ledger.release(call.reservation)  # no-op when already closed

    def _settled(self, call: BatchCall, actual: float) -> None:
        assert call.reservation is not None
        self.settled_actual += actual
        self.settled_reserved += call.reservation.reserved_usd

    def _outcome(self, call: BatchCall, outcome: BatchOutcome) -> None:
        self.client._batch_set(call.key, outcome)
        setattr(self.report, _COUNTER[outcome.kind], getattr(self.report, _COUNTER[outcome.kind]) + 1)

    def _say(self, event: str, *, force: bool = False) -> None:
        if self.silent and not force:
            return
        r = self.report
        sys.stderr.write(
            f"[batch {self.label}] pass {self.client.batch_pass}: {event}; {len(self.inflight)} in flight; "
            f"{r.answered:,} answered; {r.unanswered:,} unanswered; {int(self.clock() - self.start) // 60} min\n")


def _body(call: BatchCall) -> dict[str, Any]:
    """The request's sync payload without its ``provider`` block (the batch-level one replaces it)."""
    assert call.request is not None
    return {k: v for k, v in call.request.payload.items() if k != "provider"}


def _response(result: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    response = result.get("response") if result is not None else None
    return response if isinstance(response, Mapping) else None


def _body_of(response: Mapping[str, Any]) -> Mapping[str, Any]:
    body = response.get("body")
    return body if isinstance(body, Mapping) else {}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _message(obj: Mapping[str, Any]) -> str:
    """``error.message`` (or ``error`` itself), at most 300 characters."""
    err = obj.get("error")
    msg = err.get("message") if isinstance(err, Mapping) else err
    return str(msg or "no message")[:_MSG_CHARS]
