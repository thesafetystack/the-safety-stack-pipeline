"""The client-wide failure breaker under a worker pool (review finding L5-1, D400), and replies
that are transport-level failures although they look like answers (L5-10, L5-2, D404).

The breaker trips only when a failure streak (no HTTP 200 since its first failure) has reached
the count AND lasted the minimum time: a ~1 s 429 window at 32 workers fails every in-flight
call at once (25 "consecutive" failures in one round) and must not halt the run; a streak longer
than the minimum still does; an auth/credit status stays fatal at once. Real threads, the real
client backoff, offline fake provider; a few seconds in all.
"""

from __future__ import annotations

import http.client
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self

import pytest

from stack import llm
from stack.llm import (
    AttemptsExhausted,
    FatalLlmError,
    Ledger,
    LlmClient,
    LlmRequest,
    LlmResponse,
    SchemaValidationError,
    TransportError,
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


class Window(FakeLlm):
    """Every post answers 429 for ``window`` s from the first post (``None``: for ever), then 200;
    ``latency`` s per post."""

    def __init__(self, window: float | None, latency: float = 0.05) -> None:
        super().__init__()
        self.window, self.latency, self.posts, self.t0 = window, latency, 0, None
        self._wlock = threading.Lock()

    def post(self, request: LlmRequest) -> LlmResponse:
        with self._wlock:
            self.posts += 1
            if self.t0 is None:
                self.t0 = time.monotonic()
            t = time.monotonic() - self.t0
        time.sleep(self.latency)
        if self.window is None or t < self.window:
            return rate_limited("2")
        return ok({"verdict": "relevant"})


def run(client: LlmClient, *, workers: int = 32, docs: int = 200) -> dict[str, int]:
    res = {"ok": 0, "fatal": 0, "exhausted": 0}

    def one(i: int) -> str:
        try:
            client.complete(role="classify_1k", template="Q {{ref:x}}", refs={"x": "y"}, user=f"doc {i}",
                            schema=SCHEMA, license_class="open")
            return "ok"
        except FatalLlmError:
            return "fatal"
        except AttemptsExhausted:
            return "exhausted"

    with ThreadPoolExecutor(workers) as pool:
        for r in pool.map(one, range(docs)):
            res[r] += 1
    return res


def client_for(config: Any, transport: Any, run_id: str, **kw: Any) -> LlmClient:
    return LlmClient(config=config, transport=transport, ledger=Ledger(), run_id=run_id,
                     stage="annotate.screen", cache=None, budget_usd=12.0, **kw)


@pytest.mark.slow
def test_a_one_second_429_window_at_32_workers_does_not_halt_the_run(config: Any) -> None:
    fake = Window(1.0)
    res = run(client_for(config, fake, "r-breaker-blip"))
    assert res == {"ok": 200, "fatal": 0, "exhausted": 0}, (res, fake.posts)
    assert fake.posts > 32  # the window was real: every first-round call failed and retried


def test_a_streak_longer_than_the_minimum_still_trips(config: Any) -> None:
    fake = Window(None)  # the provider never recovers
    t0 = time.monotonic()
    res = run(client_for(config, fake, "r-breaker-outage", min_failure_streak_s=0.3))
    assert res["ok"] == 0 and res["fatal"] > 0, res
    assert time.monotonic() - t0 >= 0.3


@pytest.mark.slow
def test_the_count_alone_no_longer_trips_within_the_minimum(config: Any) -> None:
    """32 failures in one round (≥ the count of 25) inside the minimum streak: no trip yet; each
    call ends on its own ``max_call_failures`` instead."""
    fake = Window(None)
    res = run(client_for(config, fake, "r-breaker-count", min_failure_streak_s=3600.0), docs=32)
    assert res == {"ok": 0, "fatal": 0, "exhausted": 32}, res


def test_an_auth_error_is_fatal_at_once(config: Any) -> None:
    fake = FakeLlm().on("doc 0", auth_error(402))
    client = client_for(config, fake, "r-breaker-auth")  # the default (long) minimum streak
    with pytest.raises(FatalLlmError, match="auth/credit"):
        client.complete(role="classify_1k", template="Q {{ref:x}}", refs={"x": "y"}, user="doc 0",
                        schema=SCHEMA, license_class="open")
    assert fake.count() == 1


@pytest.mark.parametrize(("reply", "error_class"), [
    (rate_limited("2"), "rate_limit"), (server_error(), "http_5xx"), (transport_error(), "network"),
    (LlmResponse(418, {}, {"error": {"message": "teapot"}}), "network"),
], ids=["429", "5xx", "network", "unexpected-status"])
def test_the_breaker_reports_the_class_that_tripped_it(config: Any, reply: Any, error_class: str) -> None:
    """A 429 storm halts the run as ``rate_limit``, a 5xx one as ``http_5xx`` (D431; before, every
    trip said ``network``); an unexpected status has no ``failed_transient`` class and stays ``network``."""
    fake = FakeLlm(default=reply)
    client = client_for(config, fake, f"r-breaker-class-{error_class}", sleep=lambda s: None,
                        min_failure_streak_s=0.0, max_consecutive_failures=2)
    with pytest.raises(FatalLlmError) as ei:
        client.complete(role="classify_1k", template="Q {{ref:x}}", refs={"x": "y"}, user="doc",
                        schema=SCHEMA, license_class="open")
    assert ei.value.error_class == error_class and fake.count() == 2


# ═════════════ a 200 carrying a provider error is a transport-level failure (L5-10, D404) ═════════════
ERROR_200 = {
    "error-object": {"error": {"code": 502, "message": "Provider returned error"}},
    "finish-error": {"choices": [{"message": {"content": ""}, "finish_reason": "error"}],
                     "error": {"code": 502, "message": "upstream disconnected"}},
    "finish-error-only": {"choices": [{"message": {"content": ""}, "finish_reason": "error"}]},
    "no-choices": {"id": "gen-1", "usage": {"prompt_tokens": 10, "completion_tokens": 0}},
}


def ask(client: LlmClient, user: str = "doc") -> Any:
    return client.complete(role="classify_1k", template="Q {{ref:x}}", refs={"x": "y"}, user=user,
                           schema=SCHEMA, license_class="open")


@pytest.mark.parametrize("name", list(ERROR_200))
def test_a_200_carrying_a_provider_error_is_transient_not_schema_invalid(config: Any, name: str) -> None:
    """OpenRouter answers 200 when an error happens after processing started: retried with backoff
    as a 5xx is, ``failed_transient[http_5xx]`` at worst (due again next run), never
    ``failed_permanent[schema_invalid]`` (carried forever) and no schema-repair message."""
    fake = FakeLlm(default=LlmResponse(200, {}, ERROR_200[name]))
    client = client_for(config, fake, f"r-err200-{name}", sleep=lambda s: None)
    with pytest.raises(AttemptsExhausted) as ei:
        ask(client)
    assert (ei.value.status, ei.value.error_class) == ("failed_transient", "http_5xx")
    assert fake.count() == 4  # max_call_failures transport-level attempts
    assert all("did not match" not in str(r.payload) for r in fake.requests)


def test_a_genuine_schema_invalid_200_stays_permanent(config: Any) -> None:
    fake = FakeLlm(default=ok({"verdict": "maybe"}))
    client = client_for(config, fake, "r-err200-genuine", sleep=lambda s: None)
    with pytest.raises(SchemaValidationError) as ei:
        ask(client)
    assert (ei.value.status, ei.value.error_class) == ("failed_permanent", "schema_invalid") and fake.count() == 3


def test_an_incident_of_error_200s_at_32_workers_trips_the_breaker(config: Any) -> None:
    """Before D404 every such 200 reset the breaker, so an incident never tripped it."""

    class Slow(FakeLlm):
        def post(self, request: LlmRequest) -> LlmResponse:
            time.sleep(0.01)
            return LlmResponse(200, {}, ERROR_200["finish-error"])

    client = client_for(config, Slow(), "r-err200-incident", sleep=lambda s: None, min_failure_streak_s=0.05)
    res = run(client)
    assert res["fatal"] > 0 and res["ok"] == 0, res


# ═════════════ a 200 whose body cannot be read is a transport error (L5-2, D404) ═════════════
class _Resp:
    def __init__(self, raw: bytes | None = None, exc: Exception | None = None) -> None:
        self.raw, self.exc, self.status, self.headers = raw, exc, 200, {}

    def read(self) -> bytes:
        if self.exc is not None:
            raise self.exc
        return self.raw or b""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


@pytest.mark.parametrize("resp", [
    _Resp(raw=b"<html>upstream hiccup</html>"),
    _Resp(raw=b""),
    _Resp(raw=b"\xff\xfe not utf-8"),
    _Resp(exc=http.client.IncompleteRead(b'{"choices": [', 500)),
], ids=["html-200", "empty-200", "not-utf8", "incomplete-read"])
def test_an_unreadable_200_body_is_a_transport_error(monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> None:
    """Retried like a network error (``failed_transient[network]`` at worst), never an escaping
    ``JSONDecodeError``/``IncompleteRead`` that fails the run as ``code_bug``. No network."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)
    monkeypatch.setattr(llm, "provider_key", lambda name: "x")
    t = llm.UrllibTransport(api_key_env="X")
    req = LlmRequest(url="https://example.invalid/chat/completions", headers={}, payload={"model": "m"}, model="m",
                     attempt=1, rendered_hash="h", input_digest="i")
    with pytest.raises(TransportError, match="unreadable 200 body"):
        t.post(req)
