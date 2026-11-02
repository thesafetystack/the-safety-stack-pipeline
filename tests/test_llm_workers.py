"""The LLM client's failure handling under a worker pool (review finding L5-9, S25-44): the L5
repros, time-scaled, beside ``tests/test_llm_breaker.py`` (which has the
1 s 429 blip at 32 workers, the endless outage, the count-only streak, the 402 and the error-200
incident). Here: a 429 window longer than one call's retries churns items to
``failed_transient[rate_limit]`` (``AttemptsExhausted``), never a halt inside the minimum streak;
a mixed incident (429 with ``Retry-After``, 503, network errors, error 200s) followed by recovery;
failures interleaved with successes never trip the breaker; paid 200s carrying rate-limit headers
are answers at 32 workers (D427); a 402 in the middle of a pool stops every later request and
leaves no reservation open. Real threads, offline fake provider, the client's backoff scaled
by an injected ``sleep``; a few seconds in all.

Not pinned here (open decisions, S25-9): whether ``Retry-After`` is honoured
and whether a 429 pauses every worker; the churn tests assert only what holds either way.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from stack.llm import (
    AttemptsExhausted,
    FatalLlmError,
    Ledger,
    LlmClient,
    LlmRequest,
    LlmResponse,
)
from tests.fakes.llm import (
    FakeLlm,
    auth_error,
    ok,
    rate_limited,
    server_error,
    transport_error,
)

SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["verdict"],
                          "properties": {"verdict": {"enum": ["relevant", "irrelevant"]}}}
ANSWER = {"verdict": "relevant"}
SCALE = 0.02  # the client's 0.5/1/2 s backoff sleeps 10/20/40 ms


def scaled_sleep(s: float) -> None:
    time.sleep(s * SCALE)


class Scripted(FakeLlm):
    """Each post: ``latency`` s, then ``reply(n, t)`` (``n`` = the post's number from 0, ``t`` = s since
    the first post); thread-safe; counts posts."""

    def __init__(self, reply: Any, latency: float = 0.01) -> None:
        super().__init__()
        self.reply, self.latency, self.posts, self.t0 = reply, latency, 0, None
        self._plock = threading.Lock()

    def post(self, request: LlmRequest) -> LlmResponse:
        with self._plock:
            n = self.posts
            self.posts += 1
            if self.t0 is None:
                self.t0 = time.monotonic()
            t = time.monotonic() - self.t0
        time.sleep(self.latency)
        out = self.reply(n, t)
        if isinstance(out, Exception):
            raise out
        return out


def client_for(config: Any, transport: Any, run_id: str, **kw: Any) -> LlmClient:
    kw.setdefault("sleep", scaled_sleep)
    return LlmClient(config=config, transport=transport, ledger=Ledger(), run_id=run_id,
                     stage="annotate.screen", cache=None, budget_usd=12.0, **kw)


def run(client: LlmClient, *, workers: int = 32, docs: int = 200) -> tuple[dict[str, int], list[Any]]:
    """Every outcome by kind, and the exceptions raised (any other exception propagates)."""
    res = {"ok": 0, "fatal": 0, "exhausted": 0}
    errors: list[Any] = []

    def one(i: int) -> str:
        try:
            client.complete(role="classify_1k", template="Q {{ref:x}}", refs={"x": "y"}, user=f"doc {i}",
                            schema=SCHEMA, license_class="open")
            return "ok"
        except FatalLlmError as exc:
            errors.append(exc)
            return "fatal"
        except AttemptsExhausted as exc:
            errors.append(exc)
            return "exhausted"

    with ThreadPoolExecutor(workers) as pool:
        for r in pool.map(one, range(docs)):
            res[r] += 1
    return res, errors


@pytest.mark.slow
def test_a_window_longer_than_one_calls_retries_churns_items_not_the_run(config: Any) -> None:
    """A 1.5 s 429 window (``Retry-After: 10``) outlasts each call's 4 failures (~70 ms of scaled
    backoff): the items in it end ``failed_transient[rate_limit]``, due again next run; the run is
    not halted (the window is inside the 120 s minimum streak) and the items after it are answered."""
    fake = Scripted(lambda n, t: LlmResponse(429, {"Retry-After": "10"}, {"error": {"message": "rl"}})
                    if t < 1.5 else ok(ANSWER))
    client = client_for(config, fake, "r-workers-churn")
    res, errors = run(client, docs=600)
    assert res["fatal"] == 0 and res["exhausted"] > 32 and res["ok"] > 0, res
    assert {(e.status, e.error_class) for e in errors} == {("failed_transient", "rate_limit")}
    assert all(len(e.attempts) == 4 for e in errors)  # max_call_failures transport-level attempts each
    assert client.ledger.outstanding([client.run_id]) == []


def test_a_mixed_incident_then_recovery_answers_every_item(config: Any) -> None:
    """A 0.3 s incident of every transport-level failure kind (429 with ``Retry-After``, 503, a
    network error, a 200 carrying a provider error, D404) at 32 workers, then recovery: no halt,
    no item lost to ``schema_invalid``, every item answered once its retries outlast the incident."""
    kinds = [rate_limited("5"), server_error(), transport_error(),
             LlmResponse(200, {}, {"choices": [{"message": {"content": ""}, "finish_reason": "error"}]})]
    fake = Scripted(lambda n, t: kinds[n % 4] if t < 0.3 else ok(ANSWER))
    client = client_for(config, fake, "r-workers-mixed", sleep=time.sleep)  # real backoff: 0.5 s > the incident
    res, _ = run(client, docs=100)
    assert res == {"ok": 100, "fatal": 0, "exhausted": 0}, res
    assert fake.posts > 100  # the incident was real
    assert client.ledger.outstanding([client.run_id]) == []


def test_failures_interleaved_with_successes_never_trip_the_breaker(config: Any) -> None:
    """One post in four fails (503) for the whole run at 32 workers, with the minimum streak off:
    the 200s in between keep resetting the client-wide streak, so it never reaches 25. (A call
    whose own retries all land on failing posts may end ``exhausted``; that is per call.)"""
    fake = Scripted(lambda n, t: server_error() if n % 4 == 0 else ok(ANSWER), latency=0.002)
    client = client_for(config, fake, "r-workers-interleaved", min_failure_streak_s=0.0)
    res, _ = run(client, docs=300)
    assert res["fatal"] == 0 and res["ok"] >= 290, res


def test_paid_200s_with_rate_limit_headers_are_answers_at_32_workers(config: Any) -> None:
    """Every reply is a 200 carrying ``x-ratelimit-remaining: 0`` or ``Retry-After`` (the last
    request of a provider window): each is the answer, sent once (D427)."""
    body = ok(ANSWER).body
    fake = Scripted(lambda n, t: LlmResponse(200, {"x-ratelimit-remaining": "0"} if n % 2 else {"Retry-After": "3"},
                                             body))
    res, _ = run(client_for(config, fake, "r-workers-200-headers"), docs=100)
    assert res == {"ok": 100, "fatal": 0, "exhausted": 0} and fake.posts == 100, (res, fake.posts)


def test_a_402_in_a_pool_stops_every_later_request(config: Any) -> None:
    """The key's credit limit (402) on the 41st post at 32 workers, answered at once while an
    answer takes 50 ms: the calls in flight finish or get the same terminal error
    (``provider_auth``), nothing is posted after them, and no reservation stays open."""

    def reply(n: int, t: float) -> LlmResponse:
        if n == 40:
            return auth_error(402)
        time.sleep(0.05)
        return ok(ANSWER)

    fake = Scripted(reply, latency=0.0)
    client = client_for(config, fake, "r-workers-402")
    res, errors = run(client, docs=300)
    assert res["fatal"] > 0 and res["exhausted"] == 0 and res["ok"] >= 40, res
    assert fake.posts <= 41 + 31, fake.posts  # the 402, and at most the 31 others in flight with it
    assert {e.error_class for e in errors} == {"provider_auth"}
    assert client.ledger.outstanding([client.run_id]) == []
