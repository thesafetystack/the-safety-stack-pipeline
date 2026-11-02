"""Scenario tests of the LLM client's cost ledger, reservations and response cache (PLAN T4.1;
D23, D36; the ledger is per run and in memory since T-STORE). The fake model (``tests/fakes/llm.py``)
stands in for OpenRouter; every store is a ``tmp_path`` one. Prices come from the shipped ``configs/llm.yaml``
(``openai/gpt-6-luna`` $0.10/$0.50 per 1M, pinned as the model by ``ask``; ``openai/gpt-5.2`` at its own
$1.75/$14 entry). Reservations that must reach a given size scale ``max_tokens`` to luna's price.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from stack.llm import (
    AttemptsExhausted,
    BudgetExceeded,
    BudgetScope,
    FatalLlmError,
    Ledger,
    LlmClient,
    ModelRetired,
    ResponseCache,
    open_client,
    run_spend,
)
from stack.llm.cache import cache_key
from stack.llm.ledger import close_run, forget_run, run_ledger
from tests.fakes.llm import (
    FakeLlm,
    anomaly,
    malformed,
    ok,
    rate_limited,
    schema_invalid,
    server_error,
    transport_error,
)

LUNA = "openai/gpt-6-luna"
GPT52 = "openai/gpt-5.2"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {"verdict": {"enum": ["relevant", "irrelevant"]}},
}
TEMPLATE = "Classify the document.\n{{ref:menu}}\n{{input:document}}\nJSON only."
REFS = {"menu": "verdict: relevant | irrelevant"}
VERDICT = {"verdict": "relevant"}


def client(tmp_path: Path, config: Any, fake: FakeLlm, *, run_id: str = "r1",
           budget_usd: float | None = None, cache: bool = False, **kw: Any) -> LlmClient:
    return LlmClient(
        config=config, transport=fake, ledger=Ledger(), run_id=run_id,
        stage="annotate.screen", budget_usd=budget_usd,
        cache=ResponseCache.for_store(tmp_path / "store") if cache else None,
        sleep=lambda _s: None, **kw,
    )


def ask(c: LlmClient, user: str = "Title: Paper A", **kw: Any) -> Any:
    kw.setdefault("role", "classify")
    # The scenarios price gpt-6-luna ($0.10/$0.50): pin it, so a later role move (D201 (E)) does not
    # change the prices under the tests; the role still supplies max_tokens and fallbacks.
    kw.setdefault("model", LUNA)
    return c.complete(template=TEMPLATE, refs=REFS, user=user, schema=SCHEMA,
                      license_class="open", **kw)


# (1) ─────────────────────────────────────────────────────────────────────────


def test_cap_refuses_before_the_request(tmp_path: Path, config: Any) -> None:
    """A reservation that would cross the run's budget is refused before the request: the fake is
    never called, nothing is recorded, and the stage contract maps it to not_attempted[budget]."""
    fake = FakeLlm(default=ok(VERDICT))
    c = client(tmp_path, config, fake, budget_usd=0.0001)  # a luna call reserves ≈ $0.0003 (512 × $0.50/1M)
    with pytest.raises(BudgetExceeded):
        ask(c)
    assert fake.count() == 0
    assert c.ledger.lines() == []
    scope = BudgetScope()
    assert scope.call(lambda: ask(c)) is None and scope.tripped
    assert BudgetScope.NOT_ATTEMPTED == ("not_attempted", "budget")
    assert fake.count() == 0


# (2) ─────────────────────────────────────────────────────────────────────────


def test_two_threads_race_on_the_last_dollar(tmp_path: Path, config: Any) -> None:
    """A $1.00 run budget, each call reserves ≈ $0.60 (1.2M max_tokens × $0.50/1M): of two threads
    released together, exactly one is admitted. The admitted call is held inside the transport
    until the other has been refused, so a reconcile cannot free the room in between."""
    refused = threading.Event()
    fake = FakeLlm(default=ok(VERDICT, cost=0.001), hook=lambda _r: refused.wait(timeout=5))
    c = client(tmp_path, config, fake, budget_usd=1.0)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def run(i: int) -> None:
        start.wait(timeout=5)
        try:
            ask(c, user=f"Title: Paper {i}", max_tokens=1_200_000)
            outcomes.append("ok")
        except BudgetExceeded:
            outcomes.append("refused")
            refused.set()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert sorted(outcomes) == ["ok", "refused"]
    assert fake.count() == 1
    kinds = [x.get("kind") for x in c.ledger.lines()]
    assert kinds == ["reserve", "reconcile"]
    assert c.ledger.total() == pytest.approx(0.001)


# (3) + (6) ───────────────────────────────────────────────────────────────────


def test_every_attempt_is_reserved_and_reconciled(tmp_path: Path, config: Any) -> None:
    """Primary 5xx → fallback ok; 429 → retry ok; an adjudication tier; parse error →
    schema-invalid → ok from an unsanctioned provider. Each outbound attempt has its own
    reserve and reconcile line, and the ledger sums to the actual costs."""
    fake = FakeLlm()
    fake.on("Paper F", server_error(), model=LUNA)
    fake.on("Paper F", ok(VERDICT, cost=0.002), model=GPT52)
    fake.on("Paper R", [rate_limited(), ok(VERDICT, cost=0.0015)])
    fake.on("Paper J", ok(VERDICT, cost=0.003))
    fake.on("Paper S", [malformed(cost=0.0005), schema_invalid(cost=0.0005),
                        anomaly(VERDICT, cost=0.001)])
    c = client(tmp_path, config, fake)

    fb = ask(c, user="Title: Paper F", role="extract_main")
    assert fb.model == GPT52 and fb.cost_usd == pytest.approx(0.002)
    assert [(a.model, a.kind, a.error_class) for a in fb.attempts] == [
        (LUNA, "first", "http_5xx"), (GPT52, "fallback", None)]

    rt = ask(c, user="Title: Paper R")
    assert [(a.kind, a.status) for a in rt.attempts] == [("first", 429), ("retry", 200)]

    adj = ask(c, user="Title: Paper J", attempt_kind="adjudicate")
    assert [a.kind for a in adj.attempts] == ["adjudicate"]

    sv = ask(c, user="Title: Paper S")
    assert [a.error_class for a in sv.attempts] == ["parse_error", "schema_invalid", None]
    assert sv.provider_anomaly and sv.cost_usd == pytest.approx(0.002)

    lines = c.ledger.lines()
    assert [(x["kind"], x["model"], x["attempt"]) for x in lines] == [
        ("reserve", LUNA, "first"), ("reconcile", LUNA, "first"),
        ("reserve", GPT52, "fallback"), ("reconcile", GPT52, "fallback"),
        ("reserve", LUNA, "first"), ("reconcile", LUNA, "first"),
        ("reserve", LUNA, "retry"), ("reconcile", LUNA, "retry"),
        ("reserve", LUNA, "adjudicate"), ("reconcile", LUNA, "adjudicate"),
        *[(k, LUNA, a) for a in ("first", "retry", "retry") for k in ("reserve", "reconcile")],
    ]
    reserves = {x["reservation_id"]: x["cost_usd"] for x in lines if x["kind"] == "reserve"}
    assert all(v > 0 for v in reserves.values())
    # the GPT-5.2 fallback is reserved at its own table price ($1.75/$14, llm.yaml 1.8.2): 8192 × $14/1M dominates
    assert 8192 * 14 / 1e6 < lines[2]["cost_usd"] < 8192 * 15 / 1e6
    actual = 0.0 + 0.002 + 0.0 + 0.0015 + 0.003 + 0.0005 + 0.0005 + 0.001
    assert c.ledger.total() == pytest.approx(actual)
    assert c.ledger.outstanding() == []
    assert sum(c.ledger.settled().values()) == pytest.approx(actual)
    assert fake.count() == 8


# (4) ─────────────────────────────────────────────────────────────────────────


def test_run_budget_then_not_attempted(tmp_path: Path, config: Any) -> None:
    """``budget.max_usd`` = $0.01, each call costs $0.004 and reserves ≈ $0.0026 (5,120 max_tokens ×
    $0.50/1M): two items
    run, the third reservation is refused (BudgetExceeded), and it and every
    later item are not_attempted[budget] without a request. Another run is unaffected."""
    fake = FakeLlm(default=ok(VERDICT, cost=0.004))
    c = client(tmp_path, config, fake, run_id="r-budget", budget_usd=0.01)
    scope = BudgetScope()
    out = [scope.call(lambda i=i: ask(c, user=f"Title: Paper {i}", max_tokens=5120)) for i in range(5)]
    status = [("ok", None) if r is not None else BudgetScope.NOT_ATTEMPTED for r in out]
    assert status == [("ok", None)] * 2 + [("not_attempted", "budget")] * 3
    assert isinstance(scope.refusal, BudgetExceeded)
    assert fake.count() == 2
    assert c.ledger.run_total("r-budget") == pytest.approx(0.008)
    other = client(tmp_path, config, fake, run_id="r-other", budget_usd=0.01)
    assert ask(other, user="Title: Paper 9", max_tokens=5120).cost_usd == pytest.approx(0.004)


# (5) ─────────────────────────────────────────────────────────────────────────


def test_second_identical_call_is_a_free_cache_hit(tmp_path: Path, config: Any) -> None:
    fake = FakeLlm(default=ok(VERDICT, cost=0.0012))
    c = client(tmp_path, config, fake, cache=True)
    first = ask(c, prompt_name="classify-v1", basis_digest="b" * 64)
    assert not first.is_cache_hit and first.cost_usd == pytest.approx(0.0012)
    key = cache_key(first.model, first.rendered_digest, "b" * 64)
    assert (tmp_path / "store" / "cache" / "llm-responses" / f"{key}.json.zst").is_file()
    n_lines = len(c.ledger.lines())

    again = client(tmp_path, config, fake, run_id="r2", cache=True)  # a later run, same store
    hit = ask(again, prompt_name="classify-v1", basis_digest="b" * 64)
    assert hit.is_cache_hit and hit.cost_usd == 0.0 and hit.attempts == ()
    assert hit.parsed == first.parsed
    assert (hit.rendered_digest, hit.response_digest, hit.model_digest, hit.template_digest) == (
        first.rendered_digest, first.response_digest, first.model_digest, first.template_digest)
    assert fake.count() == 1
    assert len(c.ledger.lines()) == n_lines
    assert hit.evidence_fields()["is_cache_hit"] is True

    other_basis = ask(again, prompt_name="classify-v1", basis_digest="c" * 64)
    assert not other_basis.is_cache_hit and fake.count() == 2


def test_a_fallback_models_cached_answer_never_serves_a_primary_lookup(tmp_path: Path, config: Any) -> None:
    """The lookup tries only the first gated model, the one the call sends to first. A fallback's
    answer is cached under the fallback's own key but does not answer a later call: the re-run asks
    the primary, and from then on the primary's own entry is the hit. Red under: the lookup over
    every gated model (the client before this fix answered the re-run with gpt-5.2's entry)."""
    fake = FakeLlm()
    fake.on("Paper F", [server_error(), ok(VERDICT, cost=0.001)], model=LUNA)
    fake.on("Paper F", ok(VERDICT, cost=0.002), model=GPT52)
    fb = ask(client(tmp_path, config, fake, cache=True), user="Title: Paper F", role="extract_main")
    assert fb.model == GPT52 and fake.count() == 2
    store = ResponseCache.for_store(tmp_path / "store")
    assert store.get(cache_key(GPT52, fb.rendered_digest, fb.basis_digest)) is not None
    fresh = ask(client(tmp_path, config, fake, run_id="r2", cache=True), user="Title: Paper F", role="extract_main")
    assert not fresh.is_cache_hit and fresh.model == LUNA and fake.count() == 3
    again = ask(client(tmp_path, config, fake, run_id="r3", cache=True), user="Title: Paper F", role="extract_main")
    assert again.is_cache_hit and again.model == LUNA and fake.count() == 3


def test_a_stage_model_switch_is_not_served_the_fallback_models_cache(tmp_path: Path, config: Any) -> None:
    """A stage that switches its model (a plan pin) on a store whose cache holds the role fallback's
    answer for the same request asks the pinned model: ``extract_staged`` falls back to luna, so a
    haiku-5.5-pinned call must not read luna's entry. Red under: the lookup over every gated model."""
    haiku = "anthropic/claude-haiku-5.5"
    fake = FakeLlm()
    fake.on("Paper S", ok(VERDICT, cost=0.001), model=LUNA)
    fake.on("Paper S", ok(VERDICT, cost=0.001), model=haiku)
    first = ask(client(tmp_path, config, fake, cache=True), user="Title: Paper S", role="extract_staged")
    assert first.model == LUNA and fake.count() == 1
    switched = ask(client(tmp_path, config, fake, run_id="r2", cache=True), user="Title: Paper S",
                   role="extract_staged", model=haiku)
    assert not switched.is_cache_hit and switched.model == haiku and fake.count() == 2
    back = ask(client(tmp_path, config, fake, run_id="r3", cache=True), user="Title: Paper S",
               role="extract_staged")
    assert back.is_cache_hit and back.model == LUNA and fake.count() == 2


# (7) ─────────────────────────────────────────────────────────────────────────


def test_transport_error_releases_its_reservation(tmp_path: Path, config: Any) -> None:
    """A transport error (no response, no ``usage.cost``) releases its reservation at once (D165
    (C); was "stays charged", D95): cost 0, nothing open, the ledger total back to zero."""
    fake = FakeLlm(default=transport_error())
    c = client(tmp_path, config, fake, run_id="r-net", max_consecutive_failures=1,
               min_failure_streak_s=0.0)  # count-only breaker (D400 adds a minimum streak)
    with pytest.raises(FatalLlmError) as ei:
        ask(c)
    attempts = ei.value.attempts  # type: ignore[attr-defined]
    assert [(a.error_class, a.cost_usd) for a in attempts] == [("network", 0.0)]
    assert [x["kind"] for x in c.ledger.lines()] == ["reserve", "release"]
    assert c.ledger.outstanding() == [] and c.ledger.total() == pytest.approx(0.0)
    with pytest.raises(FatalLlmError):
        ask(c, user="Title: another")  # the breaker stays tripped; nothing more is reserved
    assert fake.count() == 1


def test_transport_errors_mid_run_leave_nothing_open(tmp_path: Path, config: Any) -> None:
    """A run whose calls meet connection errors and still finish: one call recovers on its retry,
    one gives up (``AttemptsExhausted``, the run goes on), one is clean. Afterwards the run has no
    open reservation and its ledger total equals the cost its calls report (the settled sum)."""
    fake = FakeLlm()
    fake.on("Paper A", [transport_error("connection reset"), ok(VERDICT, cost=0.002)])
    fake.on("Paper B", transport_error("connection refused"))
    fake.on("Paper C", ok(VERDICT, cost=0.003))
    c = client(tmp_path, config, fake, run_id="r-net")
    a = ask(c, user="Title: Paper A")
    with pytest.raises(AttemptsExhausted) as ei:
        ask(c, user="Title: Paper B")
    cc = ask(c, user="Title: Paper C")
    b_cost = ei.value.cost_usd  # type: ignore[attr-defined]
    assert [(x.error_class, x.cost_usd) for x in a.attempts] == [("network", 0.0), (None, 0.002)]
    assert b_cost == 0.0 and fake.count(contains="Paper B") == 4
    assert c.ledger.outstanding(["r-net"]) == []
    reported = a.cost_usd + b_cost + cc.cost_usd
    assert reported == pytest.approx(0.005)
    assert c.ledger.run_total("r-net") == pytest.approx(reported)
    settled = sum(x.get("actual_usd", 0.0) for x in c.ledger.lines() if x["kind"] == "reconcile")
    assert settled == pytest.approx(reported)


def test_close_run_releases_what_a_run_left_open_and_reports_spend(config: Any) -> None:
    """``close_run`` (the stage runner's run-end hook, before the final record) releases every
    reservation the run left open, once, and touches no other run; its spend is the settled
    micro-USD per ``openrouter/<model>`` (plus ``x`` and ``openalex``). A reconcile that arrives
    after the release charges the whole actual; a second reconcile is refused."""
    try:
        led = run_ledger("r-end")
        other = run_ledger("r-other")
        done = led.reserve(run_id="r-end", stage="s", model=LUNA, attempt="first",
                           amount_usd=0.2, tokens_in=0, tokens_out=0)
        led.reconcile(done, actual_usd=0.05, tokens_in=1, tokens_out=1)
        left = led.reserve(run_id="r-end", stage="s", model=LUNA, attempt="first",
                           amount_usd=0.1, tokens_in=0, tokens_out=0)
        keep = other.reserve(run_id="r-other", stage="s", model=LUNA, attempt="first",
                             amount_usd=0.2, tokens_in=0, tokens_out=0)
        x = run_ledger("r-end", "x", cap_usd=10.0)
        xr = x.reserve(run_id="r-end", stage="s", model="x-api/lookup", attempt="first",
                       amount_usd=0.5, tokens_in=0, tokens_out=0)
        x.reconcile(xr, actual_usd=0.015, tokens_in=3, tokens_out=0)
        assert run_spend("r-end") == {"usd_micro": {f"openrouter/{LUNA}": 50_000, "x": 15_000},
                                      "reserved_open_usd_micro": 100_000, "batch_ids": [],
                                      "abandoned_batch_ids": []}
        assert close_run("r-end") == {"usd_micro": {f"openrouter/{LUNA}": 50_000, "x": 15_000},
                                      "reserved_open_usd_micro": 0, "batch_ids": [], "abandoned_batch_ids": []}
        assert led.outstanding() == [] and len([x for x in led.lines() if x["kind"] == "release"]) == 1
        close_run("r-end")
        assert len([x for x in led.lines() if x["kind"] == "release"]) == 1
        assert [r["reservation_id"] for r in other.outstanding()] == [keep.reservation_id]
        led.reconcile(left, actual_usd=0.03, tokens_in=1, tokens_out=1)
        assert led.run_total("r-end") == pytest.approx(0.08)
        with pytest.raises(ValueError, match="not open"):
            led.reconcile(left, actual_usd=0.03, tokens_in=1, tokens_out=1)
        assert led.run_total("r-end") == pytest.approx(0.08)
    finally:
        forget_run("r-end")
        forget_run("r-other")
    assert run_spend("r-end") == {"usd_micro": {}, "reserved_open_usd_micro": 0, "batch_ids": [],
                                  "abandoned_batch_ids": []}


def test_close_never_releases_a_batched_reservation_at_zero() -> None:
    """The backstop for batch mode (D308): a batched reservation still open at the run's end (the
    driver's own cleanup failed) is reconciled at reserved × the ratio of the run's reconciled
    batched reservations, never released at $0; 1.0 before any settled. Plain ones are released."""
    led = Ledger()
    kw: dict[str, Any] = {"run_id": "r", "stage": "s", "model": LUNA, "attempt": "first", "tokens_in": 0,
                          "tokens_out": 0}
    first = Ledger()
    lone = first.reserve(amount_usd=0.4, batched=True, **kw)
    first.batch_submitted("b-0")
    assert first.batch_ids() == ["b-0"] and first.abandoned_batch_ids() == []
    assert first.close() == 1 and first.batch_ids() == []
    assert first.abandoned_batch_ids() == ["b-0"]  # billed at the ratio: an abandoned batch
    assert first.settled() == {LUNA: pytest.approx(0.4)}  # ratio 1.0: nothing settled before
    assert lone.batched
    settled = led.reserve(amount_usd=1.0, batched=True, **kw)
    led.reconcile(settled, actual_usd=0.25, tokens_in=1, tokens_out=1)
    open_b = led.reserve(amount_usd=0.8, batched=True, **kw)
    plain = led.reserve(amount_usd=0.3, **kw)
    assert led.open_usd() == pytest.approx(1.1)
    assert led.close() == 2
    kinds = {x["reservation_id"]: x["kind"] for x in led.lines() if x["kind"] != "reserve"}
    assert kinds[open_b.reservation_id] == "reconcile" and kinds[plain.reservation_id] == "release"
    assert led.settled() == {LUNA: pytest.approx(0.25 + 0.8 * 0.25)}
    assert led.outstanding() == [] and led.open_usd() == 0.0


def test_abandoned_batch_ids_stay_in_the_spend() -> None:
    """F1: a batch that ends abandoned leaves ``batch_ids`` (in flight) for ``abandoned_batch_ids``
    and stays there through :func:`close_run`; a settled batch leaves no trace."""
    led = run_ledger("r-ab")
    try:
        for b in ("b-1", "b-2", "b-3"):
            led.batch_submitted(b)
        led.batch_ended("b-1")  # settled
        led.batch_ended("b-2", abandoned=True)
        spend = run_spend("r-ab")
        assert (spend["batch_ids"], spend["abandoned_batch_ids"]) == (["b-3"], ["b-2"])
        spend = close_run("r-ab")  # b-3 still in flight at the end: billed as abandoned
        assert (spend["batch_ids"], spend["abandoned_batch_ids"]) == ([], ["b-2", "b-3"])
    finally:
        forget_run("r-ab")


def test_two_runs_in_one_process_do_not_share_a_cap(tmp_path: Path, config: Any) -> None:
    """Each run's clients reserve in that run's own ledger (``open_client`` → ``run_ledger``):
    run A exhausting its budget leaves run B's whole budget, and two clients of one run share
    their run's budget."""
    fake = FakeLlm(default=ok(VERDICT, cost=0.004))
    try:
        a1, a2, b = (open_client(config, run_id=r, stage="annotate.screen", store_root=None, budget_usd=0.01,
                                 transport=fake, sleep=lambda _s: None) for r in ("r-a", "r-a", "r-b"))
        assert a1.ledger is a2.ledger is run_ledger("r-a") and b.ledger is not a1.ledger
        assert ask(a1, user="Title: 1", max_tokens=5120).cost_usd == pytest.approx(0.004)
        assert ask(a2, user="Title: 2", max_tokens=5120).cost_usd == pytest.approx(0.004)
        with pytest.raises(BudgetExceeded):
            ask(a1, user="Title: 3", max_tokens=5120)  # 0.008 + ≈0.0026 > 0.01: the run's shared budget
        for i in range(2):
            assert ask(b, user=f"Title: b{i}", max_tokens=5120).cost_usd == pytest.approx(0.004)
        assert fake.count() == 4
        assert run_spend("r-a")["usd_micro"] == run_spend("r-b")["usd_micro"] == {f"openrouter/{LUNA}": 8000}
    finally:
        forget_run("r-a")
        forget_run("r-b")


# (8) ─────────────────────────────────────────────────────────────────────────


def test_smoke_request_reservation_is_conservative(tmp_path: Path, config: Any,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the exact ``tools/llm_smoke.py`` request reserves at least the input tokens
    OpenRouter really billed. Observed in the T4.1 real smoke (D110, 2026-10-12, on the then test
    model): the old prefix+user estimate reserved tokens_in=19 while usage reported prompt_tokens=166,
    completion_tokens=8 — a reservation below the actual cost. The cost floor is those billed tokens
    at the smoke model's table price ($0.000206 on the model of the observation)."""
    import llm_smoke  # tools/ is on sys.path (conftest)

    made: list[Ledger] = []
    monkeypatch.setattr(llm_smoke, "Ledger", lambda: made.append(Ledger()) or made[-1])
    assert llm_smoke.main(["--fake", "--store", str(tmp_path / "store")]) == 0
    reserve = next(x for x in made[0].lines() if x["kind"] == "reserve")
    assert reserve["tokens_in"] >= 166
    assert reserve["tokens_out"] == 16
    assert reserve["model"] == "openai/gpt-6-luna"  # llm_smoke's default model
    assert reserve["cost_usd"] >= config.llm.price_for(reserve["model"]).cost(166, 8)


# (9) ─────────────────────────────────────────────────────────────────────────


RETIRED = "anthropic/claude-haiku-4.5"  # llm.yaml 1.9.1: priced for old rows only ("Abandon Haiku 4.5")


def test_a_retired_model_is_refused_before_cache_and_ledger(tmp_path: Path, config: Any) -> None:
    """A pin naming a retired model raises :class:`ModelRetired` before the cache lookup and before
    any reservation: no request, no ledger line, no cache hit served; the error is fatal (the stage
    stops), so a later call on the same client re-raises it. The price entry stays for eval
    combine's backfill of old cache entries."""
    assert config.llm.is_retired(RETIRED) and config.llm.price_for(RETIRED).out_usd_per_m == 5.0
    fake = FakeLlm(default=ok(VERDICT, cost=0.001))
    seeded = client(tmp_path, config, fake, run_id="r-old", cache=True)
    old = ask(seeded, model=LUNA, basis_digest="b" * 64)  # a cached answer exists for the luna pin
    c = client(tmp_path, config, fake, run_id="r-retired", cache=True)
    n_lines = len(c.ledger.lines())
    with pytest.raises(ModelRetired) as ei:
        ask(c, model=RETIRED, basis_digest="b" * 64)
    assert (ei.value.status, ei.value.error_class) == ("failed_permanent", "config_missing")
    assert isinstance(ei.value, FatalLlmError)
    assert fake.count() == 1 and len(c.ledger.lines()) == n_lines
    with pytest.raises(ModelRetired):
        ask(c, model=LUNA, basis_digest="b" * 64)  # fatal for the client, even for a cached pin
    assert ask(seeded, model=LUNA, basis_digest="b" * 64).parsed == old.parsed


def test_a_role_naming_a_retired_model_is_refused_at_load(config: Any) -> None:
    from pydantic import ValidationError

    data = config.llm.model_dump()
    data["roles"]["classify"]["fallbacks"] = [RETIRED]
    with pytest.raises(ValidationError, match="retired"):
        type(config.llm).model_validate(data)
