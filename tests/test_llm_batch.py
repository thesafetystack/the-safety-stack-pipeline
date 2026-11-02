"""Batch mode of the LLM client (``stack.llm.batch``): collect passes, OpenRouter batches, replay.

Scenario style: a :class:`LlmClient` on the fake model (``tests/fakes/llm.py``, a batch transport
too), :func:`prefetch` over thunks that run one item's ask, then the replay of the same asks,
which is what a stage's normal loop does. A temp ledger and a temp response cache; an injected
clock and sleep, so the polls take no time. No network.
"""

from __future__ import annotations

import json
import pathlib
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from stack.llm import (
    BatchUnanswered,
    BudgetExceeded,
    BudgetScope,
    ContextTooLarge,
    Deferred,
    FatalLlmError,
    Ledger,
    LlmClient,
    LlmError,
    LlmRequest,
    LlmResponse,
    LlmResult,
    ResponseCache,
    UrllibTransport,
    run_calls,
)
from stack.llm.batch import BATCH_PRICE_FACTOR, BatchOptions, BatchReport, BatchTransport, prefetch
from tests.fakes.llm import (
    FakeLlm,
    auth_error,
    ok,
    rate_limited,
    schema_invalid,
    server_error,
    transport_error,
    truncated,
)

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "rationale"],
    "properties": {"verdict": {"enum": ["relevant", "borderline", "irrelevant"]},
                   "rationale": {"type": "string", "maxLength": 400}},
}
VALID = {"verdict": "relevant", "rationale": "in scope"}
BORDER = {"verdict": "borderline", "rationale": "unsure"}
TEMPLATE = "# Role\nYou screen documents.\n\n{{input:document}}\nJSON only.\n"
TEMPLATE_2 = "# Role\nYou adjudicate borderline documents.\n\n{{input:document}}\nJSON only.\n"
LUNA, TERRA = "openai/gpt-6-luna", "openai/gpt-5.6-terra"  # roles screen / screen_adjudicate
CAP = 512  # roles.screen.max_tokens


class Clock:
    """A fake monotonic clock; ``sleep`` advances it and records the waits."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make(config: Any, tmp: pathlib.Path, fake: Any, *, run: str = "r-batch", budget: float | None = None,
         cache: bool = True) -> LlmClient:
    return LlmClient(config=config, transport=fake, ledger=Ledger(cap_usd=100.0),
                     run_id=run, stage="annotate.screen", cache=ResponseCache(tmp / "cache") if cache else None,
                     budget_usd=budget, sleep=lambda s: None)


def ask(c: LlmClient, doc: str, *, model: str | None = None) -> LlmResult:
    return c.complete(role="screen", template=TEMPLATE, refs={}, user=doc, schema=SCHEMA, license_class="open",
                      model=model)


def docs(n: int, tag: str = "Paper") -> list[str]:
    return [f"Title: {tag} {chr(65 + i)}\n\nAbstract: about {tag.lower()} {chr(65 + i)}" for i in range(n)]


def thunks(c: LlmClient, items: list[str], fn: Callable[[LlmClient, str], object] = ask) -> Callable[[], list[Any]]:
    return lambda: [(lambda d=d: fn(c, d)) for d in items]


def batch_run(c: LlmClient, items: list[str], fn: Callable[[LlmClient, str], Any] = ask, *,
              clock: Clock | None = None, label: str = "llm", **opts: Any) -> tuple[BatchReport, list[Any]]:
    """prefetch, then the replay of the same asks: the results, or the exception each raised."""
    clock = clock or Clock()
    opts.setdefault("min_requests", 1)
    report = prefetch(c, thunks(c, items, fn), options=BatchOptions(**opts), label=label, clock=clock,
                      sleep=clock.sleep)
    out: list[Any] = []
    for d in items:
        try:
            out.append(fn(c, d))
        except Exception as exc:  # noqa: BLE001 - the replay's outcome per item is the subject
            out.append(exc)
    return report, out


def lines(c: LlmClient, kind: str) -> list[dict[str, Any]]:
    return [x for x in c.ledger.lines() if x["kind"] == kind]


# ── 1. happy path ────────────────────────────────────────────────────────────


def test_happy_path_one_batch_replayed_through_the_sync_code(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(5)
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=0.002))
    c = make(config, tmp_path / "b", fake)
    report, results = batch_run(c, items)

    assert (report.passes, report.batches, report.requests, report.answered) == (1, 1, 5, 5)
    assert fake.count() == 0 and fake.count_batched() == 5
    fb = fake.batches[0]
    assert fb.keys == ["endpoint", "model", "provider", "requests"]  # the order the API needs
    assert fb.model == LUNA and fb.provider == {"only": ["openai"], "allow_fallbacks": False}
    assert all("provider" not in body and body["model"] == LUNA for _, body in fb.bodies)
    assert report.batch_ids == ["fb-1"] and report.as_counts()["batch.answered"] == 5

    for r in results:
        assert isinstance(r, LlmResult) and r.parsed == VALID
        assert not r.is_cache_hit and r.cost_usd == pytest.approx(0.002)
        assert [(a.kind, a.status, a.error_class) for a in r.attempts] == [("first", 200, None)]
        assert r.latency_ms == 45_000  # three polls of 15 s: submit to result
    assert fake.count() == 0  # the replay sent nothing
    assert run_calls("r-batch") == 5  # counts.llm_calls: requests sent

    entry = c.cache.get(_key_of(items[0]))  # type: ignore[union-attr]
    assert entry is not None and entry["via"] == "batch" and entry["batch_id"] == "fb-1"

    # the same digests, parsed answer and cache fields as a sync call of the same item
    sync_fake = FakeLlm().on("Title: Paper", ok(VALID, cost=0.002))
    s = make(config, tmp_path / "s", sync_fake, run="r-sync")
    ref = ask(s, items[0])
    digests = ("template_digest", "model_digest", "rendered_digest", "basis_digest", "response_digest", "parsed")
    assert {k: getattr(results[0], k) for k in digests} == {k: getattr(ref, k) for k in digests}
    sync_entry = s.cache.get(_key_of(items[0]))  # type: ignore[union-attr]
    assert sync_entry is not None
    assert set(entry) - set(sync_entry) == {"via", "batch_id"}
    assert sync_fake.requests[0].payload == {**fb.bodies[0][1], "provider": sync_fake.requests[0].payload["provider"]}

    # ledger: 5 reserves at half the sync reservation, 5 reconciles at the actual, none open
    reserves = lines(c, "reserve")
    assert len(reserves) == 5 and len(lines(c, "reconcile")) == 5
    assert reserves[0]["cost_usd"] == pytest.approx(lines(s, "reserve")[0]["cost_usd"] * BATCH_PRICE_FACTOR)
    assert [x["actual_usd"] for x in lines(c, "reconcile")] == pytest.approx([0.002] * 5)
    assert c.ledger.outstanding(["r-batch"]) == []


def _key_of(doc: str) -> str:
    from stack.core import hashes
    from stack.llm import cache_key, render_call, rendered_digest

    r = render_call(TEMPLATE, {}, model=LUNA, max_tokens=CAP, schema=SCHEMA)
    idig = hashes.input_digest(doc)
    return str(cache_key(LUNA, rendered_digest(r.rendered_hash, idig), idig))


# ── 2. chunking ──────────────────────────────────────────────────────────────


def test_chunks_close_at_max_requests_and_max_bytes(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(5)
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    report, _ = batch_run(make(config, tmp_path / "a", fake), items, max_requests=2)
    assert [b.n for b in fake.batches] == [2, 2, 1] and report.batches == 3 and report.answered == 5

    size = len(json.dumps({"custom_id": "r1-1", "body": fake.batches[0].bodies[0][1]}).encode())
    fake2 = FakeLlm().on("Title: Paper", ok(VALID))
    batch_run(make(config, tmp_path / "b", fake2), items, max_bytes=int(size * 2.5))
    assert [b.n for b in fake2.batches] == [2, 2, 1]

    # one request larger than max_bytes goes alone; the chunk before it closes first
    big = "Title: Paper BIG\n\n" + "x" * (4 * size)
    fake3 = FakeLlm().on("Title: Paper", ok(VALID))
    report3, results3 = batch_run(make(config, tmp_path / "c", fake3), [items[0], items[1], big, items[2]],
                                  max_bytes=3 * size)
    assert [b.n for b in fake3.batches] == [2, 1, 1]
    assert "BIG" in json.dumps(fake3.batches[1].bodies[0][1]) and report3.answered == 4
    assert all(isinstance(r, LlmResult) for r in results3)


# ── 3. groups ────────────────────────────────────────────────────────────────


def test_two_models_in_one_pass_make_two_batches(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(4)
    fake = FakeLlm().on(model=TERRA, reply=ok(BORDER)).on("Title: Paper", ok(VALID))

    def mixed(c: LlmClient, d: str) -> LlmResult:
        return ask(c, d, model=TERRA if d in (items[1], items[3]) else None)

    report, results = batch_run(make(config, tmp_path, fake), items, mixed)
    assert sorted((b.model, b.n) for b in fake.batches) == sorted([(LUNA, 2), (TERRA, 2)])
    assert all(body["model"] == b.model for b in fake.batches for _, body in b.bodies)
    assert [r.parsed["verdict"] for r in results] == ["relevant", "borderline", "relevant", "borderline"]
    assert report.batches == 2 and fake.count() == 0


# ── 4. two passes (tier-2 style) ─────────────────────────────────────────────


def test_a_second_pass_asks_what_the_first_answers_admit(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(5)
    fake = (FakeLlm().on(model=TERRA, reply=ok(VALID, cost=0.01))
            .on("Paper B", ok(BORDER)).on("Paper D", ok(BORDER)).on("Title: Paper", ok(VALID)))
    passes: list[int] = []

    def screen(c: LlmClient, d: str) -> tuple[LlmResult, LlmResult | None]:
        passes.append(c.batch_pass)
        a = ask(c, d)
        if a.parsed["verdict"] != "borderline":
            return a, None
        b = c.complete(role="screen_adjudicate", template=TEMPLATE_2, refs={}, user=d + "\n" + a.parsed["rationale"],
                       schema=SCHEMA, license_class="open", attempt_kind="adjudicate")
        return a, b

    c = make(config, tmp_path, fake)
    report, results = batch_run(c, items, screen)
    assert [(b.model, b.n) for b in fake.batches] == [(LUNA, 5), (TERRA, 2)]
    assert report.passes == 2 and report.answered == 7 and fake.count() == 0
    assert sorted(set(passes)) == [1, 2, 3, 4] and c.batch_pass == 4  # 3 collect passes, then the replay
    tier2 = {d[:14]: r[1] for d, r in zip(items, results, strict=True) if r[1] is not None}
    assert set(tier2) == {"Title: Paper B", "Title: Paper D"}
    assert all(r.model == TERRA and r.attempts[0].kind == "adjudicate" and not r.is_cache_hit
               for r in tier2.values())


# ── 5. partial failures in a completed batch ─────────────────────────────────


def test_unusable_results_are_asked_sync_in_the_replay(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(7)  # A..G
    fake = (FakeLlm()
            .on("Paper A", [server_error(), ok(VALID, cost=0.003)])
            .on("Paper B", [transport_error(), ok(VALID, cost=0.003)])
            .on("Paper C", ok(VALID, cost=0.003))
            .on("Paper D", [schema_invalid(cost=0.001), ok(VALID, cost=0.003)])
            .on("Paper E", [truncated(cost=0.002), ok(VALID, cost=0.003)])
            .on("Paper F", ok(VALID, cost=0.002))
            .on("Paper G", auth_error(401))
            .batch(drop=lambda text: "Paper C" in text))
    client = make(config, tmp_path, fake)
    report, results = batch_run(client, items)

    assert (report.answered, report.retry_sync, report.fatal) == (1, 5, 1)
    by = dict(zip("ABCDEFG", results, strict=True))
    assert isinstance(by["F"], LlmResult) and by["F"].attempts[0].status == 200
    assert isinstance(by["G"], FatalLlmError)
    batch_first = {"A": (503, "http_5xx", 0.0), "B": (None, "batch_error", 0.0), "C": (None, "batch_error", 0.0),
                   "D": (200, "schema_invalid", 0.001), "E": (200, "truncated", 0.002)}
    for k, (status, error_class, cost) in batch_first.items():
        r = by[k]
        assert isinstance(r, LlmResult) and r.parsed == VALID and not r.is_cache_hit, k
        assert [(x.kind, x.status) for x in r.attempts][1:] == [("retry", 200)], k
        assert (r.attempts[0].status, r.attempts[0].error_class) == (status, error_class), k
        assert r.cost_usd == pytest.approx(cost + 0.003), k
    assert fake.count() == 5  # A..E asked sync once each
    # the truncated batch reply: the sync retry starts at twice the cap, as the sync call loop would
    sync_e = [r for r in fake.requests if "Paper E" in json.dumps(r.payload)]
    assert [r.payload["max_tokens"] for r in sync_e] == [2 * CAP]
    assert client.ledger.outstanding(["r-batch"]) == []
    with pytest.raises(FatalLlmError):  # the terminal error halts the rest of the run, as in sync mode
        ask(client, items[0])


_USAGE = {"prompt_tokens": 900, "completion_tokens": 0}


@pytest.mark.parametrize(("body", "cost"), [
    ({"error": {"code": 502, "message": "Provider returned error"}}, 0.0),
    ({"choices": [{"message": {"content": ""}, "finish_reason": "error"}], "usage": _USAGE}, 0.0),
    ({"choices": [{"message": {"content": json.dumps(VALID)}, "finish_reason": "error"}],
      "usage": {**_USAGE, "cost": 0.0004}}, 0.0004),
    ({"choices": [{"message": {"content": json.dumps(VALID)}}], "error": {"code": 502, "message": "cut"},
      "usage": _USAGE}, 0.0),
], ids=["error-object", "finish-error", "finish-error-parsable", "error-with-choices"])
def test_a_200_result_carrying_a_provider_error_is_a_5xx(
        config: Any, tmp_path: pathlib.Path, body: dict[str, Any], cost: float) -> None:
    """D404 in batch mode (D429): a batch result with HTTP 200 and a provider error is settled as a
    5xx result, at its ``usage.cost`` or $0 (no token estimate), and asked sync in the replay, never
    answered from (and cached with) parsable content, never labelled ``parse_error``/``schema_invalid``."""
    items = docs(2)
    fake = FakeLlm().on("Paper A", [LlmResponse(200, {}, body), ok(VALID, cost=0.003)]).on("Paper B", ok(VALID))
    client = make(config, tmp_path, fake)
    report, results = batch_run(client, items)
    assert (report.answered, report.retry_sync) == (1, 1)
    r = results[0]
    assert isinstance(r, LlmResult) and r.parsed == VALID and fake.count() == 1
    assert [(a.status, a.error_class) for a in r.attempts] == [(200, "http_5xx"), (200, None)]
    assert r.attempts[0].cost_usd == pytest.approx(cost) and r.cost_usd == pytest.approx(cost + 0.003)
    assert client.ledger.outstanding(["r-batch"]) == []


def test_a_context_too_large_result_is_the_items_failure(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(2)
    too_large = LlmResponse(400, {}, {"error": {"message": "This model's maximum context length is 128000"}})
    fake = FakeLlm().on("Paper A", too_large).on("Title: Paper", ok(VALID))
    report, results = batch_run(make(config, tmp_path, fake), items)
    assert isinstance(results[0], ContextTooLarge) and isinstance(results[1], LlmResult)
    assert report.error == 1 and fake.count() == 0


# ── 6. failed / expired batches ──────────────────────────────────────────────


@pytest.mark.parametrize(("final", "batch_cost", "counter"), [
    ("expired", None, "batches_expired"), ("failed", 0.004, "batches_failed"), ("cancelled", None, "batches_failed"),
])
def test_a_batch_without_results_leaves_its_requests_unanswered(
    config: Any, tmp_path: pathlib.Path, final: str, batch_cost: float | None, counter: str,
) -> None:
    items = docs(5)
    fake = FakeLlm().on("Title: Paper", ok(VALID)).batch(final=final, batch_cost=batch_cost)
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, items)
    assert all(isinstance(r, BatchUnanswered) for r in results)
    assert (report.unanswered, getattr(report, counter), report.answered) == (5, 1, 0)
    assert fake.count() == 0
    assert c.ledger.outstanding(["r-batch"]) == []
    # released (nothing spent), or the batch's billed cost shared over its reservations
    assert c.ledger.run_total("r-batch") == pytest.approx(batch_cost or 0.0, abs=1e-12)


def batch_actuals(c: LlmClient) -> list[float]:
    """The actuals the batched requests were reconciled at (attempt ``first``; a sync retry in
    the replay is ``retry``)."""
    return [x["actual_usd"] for x in lines(c, "reconcile") if x["attempt"] == "first"]


@pytest.mark.parametrize("case", ["no_results", "all_dropped", "error_and_missing", "uncosted_and_missing"])
def test_the_batch_cost_left_over_is_shared_over_calls_without_a_costed_result(
    config: Any, tmp_path: pathlib.Path, case: str,
) -> None:
    """A completed batch's ``usage.cost`` that its per-result costs do not account for is shared
    over the calls without a costed result (by reserved amount; a 200 by tokens), so the run's
    ledger total for the batch is the batch cost; those calls stay ``retry_sync`` and the replay
    asks them sync, their cost starting with that share."""
    items = docs(4)  # A..D
    fake = (FakeLlm().on("Paper B", [transport_error(), ok(VALID, cost=0.004)]) if case == "error_and_missing"
            else FakeLlm())
    fake.on("Title: Paper", ok(VALID, cost=0.004))
    script: dict[str, Any] = {
        "no_results": {"no_results": True},
        "all_dropped": {"drop": lambda text: True},
        "error_and_missing": {"drop": lambda text: "Paper C" in text},  # B an error item, C missing
        "uncosted_and_missing": {"drop": lambda text: "Paper C" in text, "strip_item_cost": True},
    }[case]
    fake.batch(batch_cost=0.016, **script)
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, items)
    assert abs(sum(batch_actuals(c)) - 0.016) < 1e-9  # the batch's usage.cost, all of it
    assert c.ledger.outstanding(["r-batch"]) == []
    by = dict(zip("ABCD", results, strict=True))
    unusable = {"no_results": "ABCD", "all_dropped": "ABCD", "error_and_missing": "BC", "uncosted_and_missing": "C"}[case]
    assert report.retry_sync == len(unusable) and fake.count() == len(unusable)
    for k, r in by.items():
        assert isinstance(r, LlmResult) and r.parsed == VALID, k
        if k in unusable:  # the share of the batch cost, then the sync answer
            assert (r.attempts[0].error_class, [a.kind for a in r.attempts][1:]) == ("batch_error", ["retry"]), k
            assert r.cost_usd == pytest.approx(r.attempts[0].cost_usd + 0.004) and r.attempts[0].cost_usd > 0, k
    if case in ("no_results", "all_dropped"):  # equal reservations, equal shares
        assert batch_actuals(c) == pytest.approx([0.004] * 4)


@pytest.mark.parametrize("final", ["expired", "failed"])
def test_a_batch_ending_unfinished_settles_the_results_it_carries(
    config: Any, tmp_path: pathlib.Path, final: str,
) -> None:
    """An expired or failed batch that carries ``results`` for the part it finished: those settle
    as in a completed batch; the rest are ``unanswered`` and share the batch cost left over."""
    items = docs(5)  # A, B finished; C..E not
    fake = (FakeLlm().on("Title: Paper", ok(VALID, cost=0.004))
            .batch(final=final, partial=lambda text: "Paper A" in text or "Paper B" in text, batch_cost=0.011))
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, items)
    assert [type(r).__name__ for r in results] == ["LlmResult"] * 2 + ["BatchUnanswered"] * 3
    assert all(not r.is_cache_hit and r.cost_usd == pytest.approx(0.004) for r in results[:2])
    assert (report.answered, report.unanswered) == (2, 3) and fake.count() == 0
    assert getattr(report, "batches_expired" if final == "expired" else "batches_failed") == 1
    actuals = batch_actuals(c)
    assert abs(sum(actuals) - 0.011) < 1e-9 and actuals[:2] == pytest.approx([0.004, 0.004])
    assert c.cache.get(_key_of(items[0]))["via"] == "batch"  # type: ignore[union-attr,index]
    assert c.ledger.outstanding(["r-batch"]) == []


# ── 7. time cap ──────────────────────────────────────────────────────────────


def test_the_time_cap_abandons_running_batches_at_the_settled_ratio(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(4)
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=0.0001)).batch().batch(never_finish=True)
    c = make(config, tmp_path, fake)
    clock = Clock()
    report, results = batch_run(c, items, clock=clock, max_requests=2, wait_minutes=5)
    assert [isinstance(r, LlmResult) for r in results] == [True, True, False, False]
    assert all(isinstance(r, BatchUnanswered) for r in results[2:])
    assert (report.batches_abandoned, report.unanswered, report.answered) == (1, 2, 2)
    assert report.wait_s >= 300 and clock.t - 1000.0 == pytest.approx(300.0)
    reserved = {x["reservation_id"]: x["cost_usd"] for x in lines(c, "reserve")}
    settled = lines(c, "reconcile")
    ratio = sum(x["actual_usd"] for x in settled[:2]) / sum(reserved[x["reservation_id"]] for x in settled[:2])
    assert ratio == pytest.approx(0.0001 / reserved[settled[0]["reservation_id"]])
    for x in settled[2:]:  # the abandoned batch: billed anyway, reconciled at reserved × ratio
        assert x["actual_usd"] == pytest.approx(reserved[x["reservation_id"]] * ratio)
    assert len(settled) == 4 and c.ledger.outstanding(["r-batch"]) == []


# ── 8. settled cost ──────────────────────────────────────────────────────────


def test_result_costs_batch_cost_shares_and_the_token_fallback(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(3)
    tokens = {"Paper A": 100, "Paper B": 300, "Paper C": 600}

    def scripted(**batch: Any) -> FakeLlm:
        fake = FakeLlm().batch(**batch)
        for k, n in tokens.items():
            fake.on(k, ok(VALID, cost=0.001 * n / 100, prompt_tokens=n, completion_tokens=10))
        return fake

    # (a) the per-result usage.cost is the actual, whatever the batch says
    c = make(config, tmp_path / "a", scripted(batch_cost=0.5))
    batch_run(c, items)
    assert [x["actual_usd"] for x in lines(c, "reconcile")] == pytest.approx([0.001, 0.003, 0.006])

    # (b) results without it share the batch cost, by tokens × price
    c = make(config, tmp_path / "b", scripted(strip_item_cost=True, batch_cost=0.02))
    _, results = batch_run(c, items)
    actual = [x["actual_usd"] for x in lines(c, "reconcile")]
    assert abs(sum(actual) - 0.02) < 1e-9
    price = config.llm.price_for(LUNA)
    weights = [price.cost(n, 10) for n in tokens.values()]
    assert actual == pytest.approx([0.02 * w / sum(weights) for w in weights])
    assert [r.cost_usd for r in results] == pytest.approx(actual)

    # (c) with neither: tokens × price × the batch factor
    c = make(config, tmp_path / "c", scripted(strip_item_cost=True, no_batch_usage=True))
    batch_run(c, items)
    assert [x["actual_usd"] for x in lines(c, "reconcile")] == pytest.approx([w * BATCH_PRICE_FACTOR for w in weights])


# ── 9. the response cache across runs ────────────────────────────────────────


def test_a_later_run_reuses_the_cache_and_batches_only_the_rest(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(4)
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    batch_run(make(config, tmp_path, fake, run="r-1"), items[:2])
    assert [b.n for b in fake.batches] == [2]

    report, results = batch_run(make(config, tmp_path, fake, run="r-2"), items)
    assert [b.n for b in fake.batches] == [2, 2] and report.requests == 2
    assert [r.is_cache_hit for r in results] == [True, True, False, False]

    report, results = batch_run(make(config, tmp_path, fake, run="r-3"), items)
    assert len(fake.batches) == 2 and report.requests == 0 and report.passes == 0
    assert all(r.is_cache_hit and r.cost_usd == 0 for r in results) and fake.count() == 0


def test_a_key_asked_twice_in_the_replay_is_a_cache_hit_the_second_time(config: Any, tmp_path: pathlib.Path) -> None:
    item = docs(1)[0]
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=0.002))
    report, results = batch_run(make(config, tmp_path, fake), [item, item])
    assert report.requests == 1  # registered once per key
    assert [(r.is_cache_hit, r.cost_usd) for r in results] == [(False, 0.002), (True, 0.0)]


# ── 10. budget ───────────────────────────────────────────────────────────────


def _reservation(config: Any, tmp: pathlib.Path) -> float:
    probe = make(config, tmp, FakeLlm().on("Title: Paper", ok(VALID)), run="r-probe")
    batch_run(probe, docs(1))
    return float(lines(probe, "reserve")[0]["cost_usd"])


def test_a_budget_refusal_waits_for_in_flight_batches_then_goes_on(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(5)
    res = _reservation(config, tmp_path / "probe")
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=res / 100))
    c = make(config, tmp_path, fake, budget=2.5 * res)
    report, results = batch_run(c, items, max_requests=1)
    assert all(isinstance(r, LlmResult) for r in results)
    assert (report.answered, report.refused_budget, report.batches) == (5, 0, 5)


def test_the_budget_stop_refuses_a_suffix(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(5)
    res = _reservation(config, tmp_path / "probe")
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=res))
    c = make(config, tmp_path, fake, budget=2.5 * res)
    report, results = batch_run(c, items, max_requests=1)
    assert [type(r).__name__ for r in results] == ["LlmResult", "LlmResult"] + ["BudgetExceeded"] * 3
    assert (report.answered, report.refused_budget, report.batches, fake.count()) == (2, 3, 2, 0)
    # the stage's BudgetScope stops at the first refused key
    scope = BudgetScope()
    replayed = [scope.call(lambda d=d: ask(c, d)) for d in items]  # type: ignore[misc]
    assert [r is not None for r in replayed] == [True, True, False, False, False] and scope.tripped
    assert c.ledger.outstanding(["r-batch"]) == []


# ── 11. small passes go sync ─────────────────────────────────────────────────


def test_a_pass_below_min_requests_is_asked_sync(config: Any, tmp_path: pathlib.Path) -> None:
    items = docs(3)
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=0.002))
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, items, min_requests=5)
    assert fake.batches == [] and report.sync == 3 and report.requests == 0
    assert fake.count() == 3 and c.batch_sync_fallback == 0
    assert all(not r.is_cache_hit and [a.kind for a in r.attempts] == ["first"] for r in results)
    assert lines(c, "reserve")[0]["cost_usd"] > 0 and len(lines(c, "reserve")) == 3  # sync: full price


def test_a_key_no_pass_collected_is_asked_sync_and_counted(config: Any, tmp_path: pathlib.Path) -> None:
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    c = make(config, tmp_path, fake)
    batch_run(c, docs(2))
    ask(c, docs(3)[2])  # never collected: e.g. a tier 2 the replay admits
    assert fake.count() == 1 and c.batch_sync_fallback == 1


# ── 12. submit errors ────────────────────────────────────────────────────────


def test_submit_retries_transient_errors(config: Any, tmp_path: pathlib.Path) -> None:
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    fake.submit_replies = [server_error(), rate_limited()]
    clock = Clock()
    report, results = batch_run(make(config, tmp_path, fake), docs(3), clock=clock)
    assert len(fake.batches) == 1 and report.submit_errors == 0 and report.answered == 3
    assert clock.slept[:2] == [2.0, 8.0]


def test_a_submit_lost_to_the_network_is_not_sent_again(config: Any, tmp_path: pathlib.Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """No answer to the POST: the server may have accepted (and will bill) the batch, which no one
    can find by content, so it is not resubmitted; its reservations are reconciled at reserved ×
    ratio (1.0 here: nothing settled yet) and its requests are unanswered."""
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    fake.submit_replies = [transport_error("read timed out")]  # a retry would be accepted
    clock = Clock()
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, docs(3), clock=clock)
    assert fake.batches == [] and fake.submit_replies == [] and 2.0 not in clock.slept
    assert (report.submit_errors, report.unanswered, report.requests) == (1, 3, 0)
    assert all(isinstance(r, BatchUnanswered) for r in results) and fake.count() == 0
    reserved = sum(x["cost_usd"] for x in lines(c, "reserve"))
    assert c.ledger.run_total("r-batch") == pytest.approx(reserved) and c.ledger.outstanding(["r-batch"]) == []
    assert "not sent again" in capsys.readouterr().err


def test_submit_rejected_leaves_the_chunk_unanswered(config: Any, tmp_path: pathlib.Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    fake.submit_replies = [LlmResponse(400, {}, {"error": {"message": 'provider: Unrecognized key: "data_collection"'}})]
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, docs(3))
    assert all(isinstance(r, BatchUnanswered) for r in results)
    assert (report.submit_errors, report.unanswered, report.requests) == (1, 3, 0)
    assert 'Unrecognized key: "data_collection"' in capsys.readouterr().err
    assert c.ledger.run_total("r-batch") == pytest.approx(0.0, abs=1e-12) and fake.count() == 0


def test_submit_transient_errors_exhausted(config: Any, tmp_path: pathlib.Path) -> None:
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    fake.submit_replies = [server_error()] * 4
    clock = Clock()
    report, results = batch_run(make(config, tmp_path, fake), docs(2), clock=clock)
    assert (report.submit_errors, report.unanswered) == (1, 2) and clock.slept == [2.0, 8.0, 30.0]


def test_submit_auth_error_is_fatal(config: Any, tmp_path: pathlib.Path) -> None:
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    fake.submit_replies = [auth_error(402)]
    c = make(config, tmp_path, fake)
    report, results = batch_run(c, docs(3), max_requests=1)
    assert report.fatal == 3 and report.batches == 0 and fake.batches == []
    assert all(isinstance(r, FatalLlmError) for r in results)
    assert c.ledger.outstanding(["r-batch"]) == []


# ── 13. the signals and the contract ─────────────────────────────────────────


def test_the_signals_are_not_llm_errors_and_bugs_propagate(config: Any, tmp_path: pathlib.Path) -> None:
    assert not issubclass(Deferred, LlmError) and not issubclass(BatchUnanswered, LlmError)
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    items = docs(3)
    caught: list[str] = []

    def workflow(c: LlmClient, d: str) -> LlmResult | None:
        try:
            return ask(c, d)
        except LlmError:  # what a workflow catches: the signals pass through it
            caught.append(d)
            return None

    report, results = batch_run(make(config, tmp_path / "w", fake), items, workflow)
    assert caught == [] and report.answered == 3 and all(isinstance(r, LlmResult) for r in results)

    def buggy(c: LlmClient, d: str) -> LlmResult:
        if d == items[1]:
            raise KeyError("a code bug")
        return ask(c, d)

    c = make(config, tmp_path / "k", fake)
    with pytest.raises(KeyError):
        prefetch(c, thunks(c, items, buggy), options=BatchOptions(min_requests=1))

    with pytest.raises(ValueError, match="response cache"):
        prefetch(make(config, tmp_path / "n", fake, cache=False), lambda: [], options=BatchOptions())

    class SyncOnly:
        def post(self, request: LlmRequest) -> LlmResponse:
            raise AssertionError

    with pytest.raises(TypeError, match="BatchTransport"):
        prefetch(make(config, tmp_path / "t", SyncOnly()), lambda: [], options=BatchOptions())
    assert isinstance(fake, BatchTransport) and not isinstance(SyncOnly(), BatchTransport)


class Interrupt:
    """Raises ``exc`` once, the first time ``when()`` holds (a clock or heartbeat seam)."""

    def __init__(self, exc: BaseException, when: Callable[[], bool]) -> None:
        self.exc, self.when, self.fired = exc, when, False

    def check(self) -> None:
        if not self.fired and self.when():
            self.fired = True
            raise self.exc


@pytest.mark.parametrize("where", ["thunk", "heartbeat", "submit", "reserved_unsent"])
def test_an_exception_out_of_prefetch_bills_the_batches_in_flight(
    config: Any, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], where: str,
) -> None:
    """Whatever raises out of the passes (a thunk's bug, the heartbeat, Ctrl-C during a submit or
    right after a reservation) propagates; every batch in flight is abandoned as at the deadline
    (reconciled at reserved × ratio, so the run's ledger is not left at $0 by ``close_run``), a
    submit under way is billed the same way, a reservation not yet sent is released; the client is
    in the replay pass (its keys ``unanswered``) and the batch ids are printed."""
    items = docs(5)
    fake = FakeLlm().on("Title: Paper", ok(VALID, cost=0.0001))
    c = make(config, tmp_path, fake)
    clock = Clock()
    fn: Callable[[LlmClient, str], object] = ask
    heartbeat = None
    if where == "thunk":  # the brief's reproduction: item 4 raises with [A, B] in flight
        def fn(cl: LlmClient, d: str) -> object:
            if d == items[3]:
                raise KeyError("a code bug")
            return ask(cl, d)
        exc_type: type[BaseException] = KeyError
    elif where == "heartbeat":  # the first batch settles, the second never finishes
        fake.batch().batch(never_finish=True)
        stop = Interrupt(KeyboardInterrupt(), lambda: len(lines(c, "reconcile")) == 2)
        heartbeat, exc_type = stop.check, KeyboardInterrupt
    elif where == "submit":  # [A, B] accepted; Ctrl-C while [C, D] is being sent
        fake.submit_replies = [None, KeyboardInterrupt()]
        exc_type = KeyboardInterrupt
    else:  # [A, B] accepted; Ctrl-C right after C is reserved, before its chunk is sent
        stop = Interrupt(KeyboardInterrupt(), lambda: len(lines(c, "reserve")) == 3)

        class Ticking(Clock):
            def __call__(self) -> float:
                stop.check()
                return self.t
        clock, exc_type = Ticking(), KeyboardInterrupt

    with pytest.raises(exc_type):
        prefetch(c, thunks(c, items, fn), options=BatchOptions(min_requests=1, max_requests=2), clock=clock,
                 sleep=clock.sleep, heartbeat=heartbeat)
    err = capsys.readouterr().err
    assert "fb-1" in err and "interrupted:" in err
    assert c.ledger.outstanding(["r-batch"]) == []
    reserves = {x["reservation_id"]: x["cost_usd"] for x in lines(c, "reserve")}
    actual = {x["reservation_id"]: x["actual_usd"] for x in lines(c, "reconcile")}
    released = {x["reservation_id"] for x in lines(c, "release")}
    if where == "heartbeat":  # [A, B] settled at 0.0001 each; [C, D] abandoned at the ratio
        ratio = 0.0001 / next(iter(reserves.values()))
        assert sorted(actual.values()) == pytest.approx([0.0001] * 2 + [ratio * r for r in list(reserves.values())[2:]])
    elif where == "reserved_unsent":  # [A, B] billed at reserved; C released
        assert len(reserves) == 3 and len(actual) == 2 and released == set(list(reserves)[2:])
        assert sorted(actual.values()) == pytest.approx(sorted(list(reserves.values())[:2]))
    else:  # every reservation billed in full (ratio 1.0: nothing settled)
        assert len(reserves) == (2 if where == "thunk" else 4) and released == set()
        assert actual == pytest.approx(reserves)
    assert c.batch_pass == 2  # the replay pass
    for d in items[:2] if where != "heartbeat" else items[2:4]:
        with pytest.raises(BatchUnanswered):
            ask(c, d)


def test_options_from_a_stage_line(config: Any) -> None:
    opt = BatchOptions.from_options({"batch_max_requests": 500, "batch_max_mb": 2.5, "batch_min_requests": 0,
                                     "batch_wait_minutes": 90, "other": "x"})
    assert (opt.max_requests, opt.max_bytes, opt.min_requests, opt.wait_minutes) == (500, 2_500_000, 0, 90.0)
    assert BatchOptions.from_options({}) == BatchOptions()
    for key, bad in [("batch_max_requests", 0), ("batch_max_requests", 1.5), ("batch_max_mb", "big"),
                     ("batch_min_requests", -1), ("batch_wait_minutes", True)]:
        with pytest.raises(ValueError, match=key):
            BatchOptions.from_options({key: bad})


def test_progress_lines_honour_stack_progress(config: Any, tmp_path: pathlib.Path,
                                              capsys: pytest.CaptureFixture[str],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLlm().on("Title: Paper", ok(VALID))
    batch_run(make(config, tmp_path / "a", fake), docs(2), label="screen")
    err = capsys.readouterr().err
    assert "[batch screen] pass 1: submitted 2 (batch fb-1)" in err and "batch fb-1 completed" in err
    monkeypatch.setenv("STACK_PROGRESS", "0")
    batch_run(make(config, tmp_path / "b", fake), docs(3), label="screen")
    err = capsys.readouterr().err
    assert "submitted" not in err and "[batch screen] done:" in err and "fb-2" in err


# ── the production transport's GET ───────────────────────────────────────────


def test_urllib_transport_get_and_batch_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """A localhost server stands in for OpenRouter: the GET carries the key, a non-2xx status is a
    response; a POST to /batches is sent like any POST."""
    seen: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path.endswith("/missing"):
                self._reply(404, {"error": {"message": "no such batch"}})
            else:
                self._reply(200, {"id": "b1", "status": "in_progress"})

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, ",".join(body)))
            self._reply(202, {"id": "b1", "status": "validating"})

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("TEST_BATCH_KEY", "k-test")
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(var, raising=False)
        t = UrllibTransport(api_key_env="TEST_BATCH_KEY", timeout=5)
        base = f"http://127.0.0.1:{server.server_port}/api/v1"
        assert isinstance(t, BatchTransport)
        got = t.get(f"{base}/batches/b1", {"Content-Type": "application/json"})
        assert (got.status, got.body["status"]) == (200, "in_progress")
        assert t.get(f"{base}/batches/missing", {}).status == 404
        posted = t.post(LlmRequest(url=f"{base}/batches", headers={"Content-Type": "application/json"},
                                   payload={"endpoint": "e", "model": "m", "provider": {}, "requests": []},
                                   model="m", attempt=0, rendered_hash="", input_digest=""))
        assert posted.status == 202
        assert seen == [("GET", "/api/v1/batches/b1", "Bearer k-test"), ("GET", "/api/v1/batches/missing", "Bearer k-test"),
                        ("POST", "/api/v1/batches", "endpoint,model,provider,requests")]
    finally:
        server.shutdown()
