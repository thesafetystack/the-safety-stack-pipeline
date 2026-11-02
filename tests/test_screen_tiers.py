"""The two-tier screen on the annotate framework (PLAN T4.3b): ``workflows/annotate/screen`` run
through the real stage runner on a tiny ``documents`` chain → ``fold.corpus`` →
``annotate.screen``, the fake model keyed on the MODEL each request names (tier 1 = the plan pin's
``model``, ``dev-v2``: gpt-6-luna; tier 2 = ``llm.yaml`` role ``screen_adjudicate``'s), so a test
sees which tier answered and what was stored. No network: the transport is
``tests.fakes.llm.FakeLlm`` via ``stack.annotate.stage.TRANSPORT_HOOK``, the key env var is unset,
the ledger is a tmp file.

v3's seven cases (``tests/test_screen_tiers.py`` at 9312c8c, DB-bound) re-harnessed: tier 2 re-asks
a borderline; tier 2 may still say borderline; a tier-2 refusal keeps tier 1's verdict; tier 2
off + a limit; a tier-2 invalid/oversize response keeps borderline; parallel screening saves every
result (workers 1 and 4); a parallel auth failure drains the call in flight. Added: a tier-2
budget refusal defers the document; per-document transport failures and the run-wide breaker;
the ``SCREEN`` payload round trip through the ``screen`` writer and reader; the plan pin's
``name`` selects the template both tiers send (``screen-v7`` → ``screen.md``, ``screen-v8`` →
``screen-v8.md``; an unknown name refuses before any call).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

import stack.annotate.stage as annotate_stage
import stack.llm as stack_llm
from stack.annotate import config_digest
from stack.checks.verify import run_checks
from stack.config import load
from stack.llm import LlmRequest, LlmResponse, render_call
from stack.plan import Plan
from stack.schemas.families import family
from stack.schemas.payloads.registry import payload
from stack.schemas.payloads.screen import SCREEN
from stack.stage_runner import run_stage
from stack.store.layout import BLOBS
from stack.store.parquet import read_family, write_family
from tests.fakes.annotate import (
    AT_RUN,
    annotate,
    doc_with_text,
    fold_corpus,
    plan,
    rows_of,
    write_documents,
)
from tests.fakes.llm import FakeLlm, auth_error, ok, transport_error
from tests.synth.rows import sid
from workflows.annotate.screen import main as screen

pytestmark = pytest.mark.slow  # runner-driven stores (D90; `docs/SETUP.md` §7)

CONFIG = load()
LUNA = "openai/gpt-6-luna"  # dev-v2's prompts.annotate.screen.model
TERRA = CONFIG.llm.roles["screen_adjudicate"].model
RELEVANT_MARK = "definitely about reward hacking"
BORDERLINE_MARK = "a thin abstract"
ANNOTATE_CHECKS = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_ledgers: Any) -> dict[str, Any]:
    fake = FakeLlm()
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows/annotate/screen
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)    # no network path, whatever happens
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    return {"store": tmp_path / "store", "fake": fake, "ledger": run_ledgers}


def splan(pin: dict[str, Any] | None = None, **line: Any) -> Plan:
    """``dev-v2`` with a bare ``annotate.screen`` line (+ ``line`` keys) and its pin updated."""
    p = plan(screen=line or None)
    if pin:
        prompts = dict(p.prompts)
        prompts["annotate.screen"] = {**prompts["annotate.screen"], **pin}
        p = p.model_copy(update={"prompts": prompts})
    return p


def verdict(v: str, rationale: str, cost: float = 0.001, **more: Any) -> LlmResponse:
    return ok({"verdict": v, "rationale": rationale, **more}, cost=cost)


def tiers(fake: FakeLlm, *, tier1: str = "relevant", tier2: Any = None) -> FakeLlm:
    """Tier 2 (the adjudicate model) answers ``tier2`` (a verdict or a reply); tier 1 answers
    ``tier1`` on :data:`RELEVANT_MARK` texts, ``borderline`` otherwise."""
    if tier2 is not None:
        fake.on(model=TERRA, reply=verdict(tier2, "strong reader decided", 0.02) if isinstance(tier2, str) else tier2)
    fake.on(RELEVANT_MARK, verdict(tier1, "decided scope", buckets=["paper"],
                                   quotes=[RELEVANT_MARK, "a sentence the text never says"]), model=LUNA)
    fake.on(model=LUNA, reply=verdict("borderline", "thin abstract, unsure"))
    return fake


def store_docs(store: Path, texts: list[str], run: int = 1) -> None:
    write_documents(store, [doc_with_text(store, i, t) for i, t in enumerate(texts)], run=run)


def by_doc(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["doc_id"]: r for r in rows}


def evidence_of(store: Path, rec: Any, i: int) -> list[dict[str, Any]]:
    """Document ``i``'s evidence rows of ``rec``, in chain order (``supersedes``)."""
    ev = [e for e in rows_of(store, rec, "screen_evidence") if e["doc_id"] == sid(i)]
    out = [e for e in ev if e["supersedes"] not in {x["evidence_id"] for x in ev}]
    while len(out) < len(ev):
        out.append(next(e for e in ev if e["supersedes"] == out[-1]["evidence_id"]))
    return out


def user_text(request: LlmRequest) -> str:
    return str(request.payload["messages"][1]["content"])


def all_green(store: Path, *, pin_moved: bool = False) -> None:
    """Every check ok. ``pin_moved``: the run's plan (built in memory) pins ``annotate.screen``
    otherwise than the plan file of the same name, which X6 reports as its ``warn`` (R1-1: the
    evidence is judged against the run's recorded pin, the file's pin "moved since")."""
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    if pin_moved:
        x6 = red.pop("policy_pins", None)
        assert x6 and all("pin differs from the one this build's run asked under" in i for i in x6), x6
    assert red == {}, red
    by = {r.check: r for r in results}
    want = {n: "warn" if pin_moved and n == "policy_pins" else "ok" for n in ANNOTATE_CHECKS}
    assert {n: by[n].status for n in ANNOTATE_CHECKS} == want


@pytest.mark.parametrize("ordinary", ["relevant", "irrelevant"])
def test_borderline_is_re_asked_by_the_strong_tier(env: dict[str, Any], ordinary: str) -> None:
    store, fake = env["store"], tiers(env["fake"], tier1=ordinary, tier2="relevant")
    full_text = f"{BORDERLINE_MARK} " + "x" * 9000 + " LATE_SAFETY_EVIDENCE"
    store_docs(store, [f"An abstract {RELEVANT_MARK} and more.", full_text])
    p = splan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    rows = by_doc(rows_of(store, r1, "screen"))

    easy, hard = rows[sid(0)], rows[sid(1)]
    assert (easy["verdict"], easy["adjudication_tier"], easy["tier1_verdict"], easy["tier1_model"]) == (
        ordinary, 1, ordinary, LUNA)
    assert easy["buckets"] == ["paper"] and easy["reasoning"] == "decided scope"
    # the anchored quote keeps its offset into the text; the invented one is dropped
    text0 = f"An abstract {RELEVANT_MARK} and more."
    assert easy["quotes"] == [{"text": RELEVANT_MARK, "offset": text0.index(RELEVANT_MARK)}]
    assert r1.counts["quotes_voided"] == 1
    assert (hard["verdict"], hard["reasoning"], hard["adjudication_tier"]) == ("relevant", "strong reader decided", 2)
    assert (hard["tier1_verdict"], hard["tier1_model"], hard["status"]) == ("borderline", LUNA, "ok")

    # one evidence row per tier: tier 2 supersedes tier 1 and is the selected one
    e0, (t1, t2) = evidence_of(store, r1, 0), evidence_of(store, r1, 1)
    assert len(e0) == 1 and easy["evidence_id"] == e0[0]["evidence_id"]
    assert (t1["model"], t1["verdict"], t1["cost_usd"], t1["status"]) == (LUNA, "borderline", 0.001, "ok")
    assert (t2["model"], t2["verdict"], t2["cost_usd"]) == (TERRA, "relevant", 0.02)
    assert t2["supersedes"] == t1["evidence_id"] and hard["evidence_id"] == t2["evidence_id"]
    assert t1["work_digest"] == t2["work_digest"] and t1["attempts"] == t2["attempts"] == 1
    assert r1.counts["tier2"] == 1 and "tier2_failed" not in r1.counts

    # exactly one tier-2 request: the tier-1 rationale and the whole text; tier 1 saw the excerpt
    t2_reqs = [r for r in fake.requests if r.model == TERRA]
    assert len(t2_reqs) == 1 and len(fake.requests) == 3
    assert "thin abstract, unsure" in user_text(t2_reqs[0]) and full_text in user_text(t2_reqs[0])
    assert t2_reqs[0].payload["max_tokens"] == CONFIG.llm.roles["screen_adjudicate"].max_tokens
    first = [user_text(r) for r in fake.requests if r.model == LUNA]
    assert all("LATE_SAFETY_EVIDENCE" not in t for t in first)
    assert any(t.endswith("…[truncated]") for t in first)
    lines = env["ledger"](r1.run_id).lines()
    assert [(x["model"], x["attempt"]) for x in lines if x["kind"] == "reserve"
            and x["model"] == TERRA] == [(TERRA, "adjudicate")]
    all_green(store)

    # rerun with r1's outputs as its prior (--in): nothing is asked again, the same rows
    r2 = annotate(store, p, "screen", corpus, prior=r1)
    assert len(fake.requests) == 3 and r2.counts["carried"] == 2
    assert rows_of(store, r2, "screen") == rows_of(store, r1, "screen")
    all_green(store)


def test_tier2_may_still_say_borderline(env: dict[str, Any]) -> None:
    store, fake = env["store"], tiers(env["fake"], tier2="borderline")
    store_docs(store, [f"{BORDERLINE_MARK} of a paper."])
    p = splan()
    r = annotate(store, p, "screen", fold_corpus(store, p))
    row = rows_of(store, r, "screen")[0]
    assert (row["verdict"], row["adjudication_tier"], row["tier1_verdict"]) == ("borderline", 2, "borderline")
    assert evidence_of(store, r, 0)[-1]["model"] == TERRA and fake.count(model=TERRA) == 1
    all_green(store)


def test_tier2_refusal_keeps_tier1_verdict(env: dict[str, Any]) -> None:
    """Tier 2 pinned to a model the egress policy does not sanction (an unmapped provider
    prefix: fail closed): refused before any bytes leave. Not a failed document: tier 1 decided,
    its ``borderline`` stays selected; the refusal is tier 2's own evidence row. A rerun with the
    first run's outputs as its prior (``--in``) asks nothing (v3: tier 1's verdict stands)."""
    store, fake = env["store"], tiers(env["fake"])
    store_docs(store, [f"{BORDERLINE_MARK} of a paper."])
    p = splan({"adjudicate_model": "meta-llama/llama-4-maverick"})
    corpus = fold_corpus(store, p)
    r = annotate(store, p, "screen", corpus)
    row = rows_of(store, r, "screen")[0]
    t1, t2 = evidence_of(store, r, 0)
    assert (row["status"], row["verdict"], row["adjudication_tier"], row["evidence_id"]) == (
        "ok", "borderline", 1, t1["evidence_id"])
    assert (t2["status"], t2["error_class"], t2["supersedes"], t2["cost_usd"]) == (
        "failed_permanent", "license_gated", t1["evidence_id"], None)
    assert "tier 2" in t2["error"] and t1["cost_usd"] == 0.001
    assert fake.count() == 1 and r.counts["tier2_failed"] == 1
    all_green(store, pin_moved=True)  # splan's adjudicate_model is not the plan file's
    r2 = annotate(store, p, "screen", corpus, prior=r)
    assert fake.count() == 1 and r2.counts["carried"] == 1
    assert rows_of(store, r2, "screen") == rows_of(store, r, "screen")


def test_tier2_can_be_switched_off_and_limit_caps_the_run(env: dict[str, Any]) -> None:
    store, fake = env["store"], tiers(env["fake"], tier2="relevant")
    store_docs(store, [f"{BORDERLINE_MARK} number {i}." for i in range(3)])
    p = splan({"tier2": False}, options={"max_rows": 2})
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    rows = rows_of(store, r1, "screen")
    assert [(r["status"], r["error_class"], r["verdict"], r["adjudication_tier"]) for r in rows] == [
        ("ok", None, "borderline", 1), ("ok", None, "borderline", 1), ("not_attempted", "limit", None, None)]
    assert fake.count(model=TERRA) == 0 and fake.count() == 2 and not r1.scope_complete
    all_green(store, pin_moved=True)  # tier2 off is not the plan file's pin
    # the third is still pending: a rerun without the limit picks it up, and only it
    r2 = annotate(store, splan({"tier2": False}), "screen", corpus, prior=r1)
    assert fake.count() == 3 and r2.counts["asked"] == 1 and r2.scope_complete
    # tier 2 on is another config, so another work key (a rerun would ask everything again)
    on, off = splan().prompts["annotate.screen"], p.prompts["annotate.screen"]
    digest = {k: config_digest(payload("screen"), pin, screen.prompt(pin, CONFIG.llm)) for k, pin in
              (("on", on), ("off", off))}
    assert digest["on"] != digest["off"] and rows[0]["config_digest"] == digest["off"]
    assert screen.prompt(off, CONFIG.llm).models == (LUNA,)
    assert screen.prompt(on, CONFIG.llm).models == (LUNA, TERRA)


@pytest.mark.parametrize(("reply", "status", "error_class"), [
    (LlmResponse(400, {}, {"error": {"message": "context length exceeded"}}), "failed_permanent", "too_large"),
    (LlmResponse(413, {}, {"error": {"message": "payload too large"}}), "failed_permanent", "too_large"),
    # a 200 carrying a provider error is a transient 5xx, not schema-invalid output (D404)
    (LlmResponse(200, {}, {"error": {"message": "no choices"}}), "failed_transient", "http_5xx"),
])
def test_tier2_invalid_or_oversize_response_keeps_borderline(
        env: dict[str, Any], reply: LlmResponse, status: str, error_class: str) -> None:
    store, fake = env["store"], tiers(env["fake"], tier2=reply)
    full_text = BORDERLINE_MARK + "x" * 9000 + " END_OF_SNAPSHOT"
    store_docs(store, [full_text])
    p = splan()
    r = annotate(store, p, "screen", fold_corpus(store, p))
    row = rows_of(store, r, "screen")[0]
    t1, t2 = evidence_of(store, r, 0)
    assert (row["verdict"], row["adjudication_tier"], row["evidence_id"]) == ("borderline", 1, t1["evidence_id"])
    assert (t2["status"], t2["error_class"]) == (status, error_class)
    assert all(full_text in user_text(q) for q in fake.requests if q.model == TERRA)
    all_green(store)


def test_tier2_budget_refusal_defers_the_document(env: dict[str, Any]) -> None:
    """A stage budget that admits tier 1's reservation but not tier 2's: the document is
    ``not_attempted[budget]`` (no evidence); the rerun with room takes tier 1 from the response
    cache (no tier-1 request, a free cache hit) and pays only tier 2."""
    store, fake = env["store"], tiers(env["fake"], tier2="relevant")
    store_docs(store, [f"{BORDERLINE_MARK} of a paper."])
    tight = splan(budget={"max_usd": 0.02})
    corpus = fold_corpus(store, tight)
    r1 = annotate(store, tight, "screen", corpus)
    row = rows_of(store, r1, "screen")[0]
    assert (row["status"], row["error_class"], row["evidence_id"]) == ("not_attempted", "budget", None)
    assert fake.count(model=LUNA) == 1 and fake.count(model=TERRA) == 0 and not r1.scope_complete
    all_green(store)
    r2 = annotate(store, splan(), "screen", corpus, prior=r1)
    t1, t2 = evidence_of(store, r2, 0)
    assert (t1["is_cache_hit"], t1["cost_usd"], t2["is_cache_hit"], t2["verdict"]) == (True, 0.0, False, "relevant")
    assert fake.count(model=LUNA) == 1 and fake.count(model=TERRA) == 1
    all_green(store)


@pytest.mark.parametrize("workers", [1, 4])
def test_parallel_screen_saves_every_result(env: dict[str, Any], workers: int) -> None:
    """``options.workers`` calls at once: every worker's call is in flight together (a barrier
    in the transport); every result is saved, each with its own ledger lines."""
    store = env["store"]
    barrier = threading.Barrier(workers)
    fake = env["fake"]
    fake.hook = lambda _r: barrier.wait(timeout=30)
    fake.on(model=LUNA, reply=verdict("relevant", "safety method"))
    store_docs(store, [f"{RELEVANT_MARK} {i}" for i in range(workers)])
    p = splan(options={"workers": workers})
    r = annotate(store, p, "screen", fold_corpus(store, p))
    rows = rows_of(store, r, "screen")
    assert [x["status"] for x in rows] == ["ok"] * workers and r.counts["asked"] == workers
    assert len(rows_of(store, r, "screen_evidence")) == workers
    assert {q.model for q in fake.requests} == {LUNA} and fake.count() == workers
    assert env["ledger"](r.run_id).total() == pytest.approx(workers * 0.001)
    assert r.spend == {"usd_micro": {f"openrouter/{LUNA}": workers * 1000}, "reserved_open_usd_micro": 0,
                       "batch_ids": [], "abandoned_batch_ids": []}
    all_green(store)


def test_parallel_auth_failure_drains_successes(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Two workers, five documents: the first call gets a 401 while the second is in flight.
    The 401 halts the run (its row ``not_attempted[halted]``, no evidence row, D430), the call in
    flight finishes and is kept, nothing more is sent: the other three are ``not_attempted[halted]``."""
    store = env["store"]
    barrier, guard, calls = threading.Barrier(2), threading.Lock(), []

    class FailingTransport:
        def post(self, request: LlmRequest) -> LlmResponse:
            with guard:
                calls.append(request)
                number = len(calls)
            if number <= 2:
                barrier.wait(timeout=30)
            if number == 1:
                return auth_error(401)
            time.sleep(1.0)  # the 401 lands first, so the coordinator halts before refilling
            return verdict("relevant", "safety technique")

    transport = FailingTransport()
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: transport)
    store_docs(store, [f"{RELEVANT_MARK} {i}" for i in range(5)])
    p = splan(options={"workers": 2})
    r = annotate(store, p, "screen", fold_corpus(store, p))
    rows = rows_of(store, r, "screen")
    got = sorted((x["status"], x["error_class"]) for x in rows)
    assert got == sorted([("ok", None)] + [("not_attempted", "halted")] * 4)
    assert len(calls) == 2 and r.counts["halted"] == 1 and not r.scope_complete
    kept = [x["doc_id"] for x in rows if x["status"] != "not_attempted"]
    assert kept in ([sid(0)], [sid(1)])  # whichever of the first two was not the 401
    assert [e["doc_id"] for e in rows_of(store, r, "screen_evidence")] == kept
    all_green(store)


def test_transport_failures_fail_one_document_breaker_halts(env: dict[str, Any],
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A document whose calls keep failing at the network is ``failed_transient[network]`` with
    ``retry_after`` once the client's per-call cap (4) is spent; the run goes on. The run-wide
    breaker (25 consecutive failures) halts the run: that row is ``failed_transient[network]``,
    every later one ``not_attempted[halted]``."""
    store, fake = env["store"], env["fake"]
    fake.on("Title: Paper 1\n", transport_error("connection reset"))
    fake.on(model=LUNA, reply=verdict("relevant", "safety method"))
    store_docs(store, [f"{RELEVANT_MARK} {i}" for i in range(3)])
    p = splan()
    r = annotate(store, p, "screen", fold_corpus(store, p))
    rows = by_doc(rows_of(store, r, "screen"))
    assert [(rows[sid(i)]["status"], rows[sid(i)]["error_class"]) for i in range(3)] == [
        ("ok", None), ("failed_transient", "network"), ("ok", None)]
    assert rows[sid(1)]["retry_after"] == AT_RUN and r.scope_complete  # the first retry is due at once
    assert fake.count(contains="Title: Paper 1\n") == 4 and r.counts["failed.network"] == 1
    all_green(store)

    store2 = store.parent / "store2"
    down = FakeLlm(default=transport_error("connection refused"))
    monkeypatch.setattr(stack_llm, "_MIN_FAILURE_STREAK_S", 0.0)  # count-only breaker (D400 adds a minimum streak)
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: down)
    store_docs(store2, [f"{RELEVANT_MARK} {i}" for i in range(8)])
    r2 = annotate(store2, p, "screen", fold_corpus(store2, p))
    rows2 = by_doc(rows_of(store2, r2, "screen"))
    assert [(rows2[sid(i)]["status"], rows2[sid(i)]["error_class"]) for i in range(8)] == (
        [("failed_transient", "network")] * 7 + [("not_attempted", "halted")])
    assert down.count() == 25 and r2.counts["halted"] == 1
    assert "consecutive" in next(e["error"] for e in rows_of(store2, r2, "screen_evidence") if e["doc_id"] == sid(6))


def test_transport_errors_mid_run_leave_no_open_reservation(env: dict[str, Any],
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """D165 (C): a run that meets connection errors and finishes ``ok`` (one document recovers on
    its retry, one gives up ``failed_transient[network]``) leaves no open ledger reservation, and
    its reported cost (the evidence rows' ``cost_usd``) equals what the ledger charges the run (the
    settled sum). A stage that dies between reserve and settle (a non-network exception in the
    transport: the run ends ``failed``) has its open reservation released at run end."""
    store, fake = env["store"], env["fake"]
    fake.on("Title: Paper 1\n", [transport_error("connection reset"), verdict("relevant", "x", 0.004)])
    fake.on("Title: Paper 2\n", transport_error("connection refused"))
    fake.on(model=LUNA, reply=verdict("relevant", "safety method", 0.001))
    store_docs(store, [f"{RELEVANT_MARK} {i}" for i in range(3)])
    p = splan()
    r = annotate(store, p, "screen", fold_corpus(store, p))
    rows = by_doc(rows_of(store, r, "screen"))
    assert [rows[sid(i)]["status"] for i in range(3)] == ["ok", "ok", "failed_transient"]
    ledger = env["ledger"](r.run_id)
    assert ledger.outstanding([r.run_id]) == []
    reported = sum(e["cost_usd"] or 0.0 for e in rows_of(store, r, "screen_evidence") if e["run_id"] == r.run_id)
    assert reported == pytest.approx(0.001 + 0.004)
    assert ledger.run_total(r.run_id) == pytest.approx(reported)

    store2 = store.parent / "store2"
    broken = FakeLlm(default=RuntimeError("a bug between reserve and settle"))
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: broken)
    store_docs(store2, [f"{RELEVANT_MARK} {i}" for i in range(2)])
    p2 = splan()
    rec = run_stage(store2, p2, "annotate.screen", [fold_corpus(store2, p2)], series="main")
    assert rec.status == "failed" and rec.error_class == "code_bug"
    ledger = env["ledger"](rec.run_id)
    assert broken.count() == 1 and any(x["run_id"] == rec.run_id for x in ledger.lines())
    assert ledger.outstanding([rec.run_id]) == [] and ledger.run_total(rec.run_id) == pytest.approx(0.0)


def test_screen_payload_round_trip(env: dict[str, Any], tmp_path: Path) -> None:
    """A full ``SCREEN`` value (every field set) through the ``screen`` writer and reader: the
    family's payload columns are the frozen ``SCREEN`` fields, the value comes back equal."""
    store, fake = env["store"], tiers(env["fake"], tier2="relevant")
    store_docs(store, [f"{BORDERLINE_MARK} of a paper."])
    p = splan()
    r = annotate(store, p, "screen", fold_corpus(store, p))
    tbl, header = read_family(store / r.outputs["screen"]["path"])
    schema = family("screen").schema
    for f in SCREEN:
        assert schema.field(f.name).type == f.type, f.name
    full = {"verdict": "relevant", "basis_role": "full_text", "buckets": ["paper", "eval_report"],
            "quotes": [{"text": "a thin abstract", "offset": 0}, {"text": "of a paper", "offset": 16}],
            "off_topic_reason": "none — kept", "confidence": 0.75, "reasoning": "it is in scope",
            "adjudication_tier": 2, "tier1_verdict": "borderline", "tier1_model": LUNA}
    assert sorted(full) == sorted(f.name for f in SCREEN)
    row = tbl.to_pylist()[0] | full
    out = tmp_path / "copy" / r.outputs["screen"]["path"]  # same layout: a copy under its own data/
    write_family(out, pa.Table.from_pylist([row], schema=schema), header)
    back, back_header = read_family(out)
    assert back.to_pylist() == [row] and back_header == header
    assert {k: back.to_pylist()[0][k] for k in full} == full
    assert (store / BLOBS).is_dir() and fake.count() == 2


def system_text(request: LlmRequest) -> str:
    return str(request.payload["messages"][0]["content"][0]["text"])


@pytest.mark.parametrize(("name", "file"), [("screen-v7", "screen.md"), ("screen-v8", "screen-v8.md")])
def test_pin_name_selects_the_template(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                       name: str, file: str) -> None:
    """Both tiers send the named template's bytes (the system block the fake recorded is the
    file rendered with the enum menus); its digests are the evidence's ``template_digest`` and
    differ between the names, and so do the work keys. The module attribute ``TEMPLATE`` selects
    nothing (the eval harness assigns it for ``--prompt``)."""
    monkeypatch.setattr(screen, "TEMPLATE", "assigning the attribute selects nothing")
    store, fake = env["store"], tiers(env["fake"], tier2="relevant")
    store_docs(store, [f"An abstract {RELEVANT_MARK} and more.", f"{BORDERLINE_MARK} of a paper."])
    p = splan({"name": name})
    r = annotate(store, p, "screen", fold_corpus(store, p))
    text = (Path(screen.__file__).parent / file).read_text("utf-8")
    refs = {"enums": screen._render_menu(CONFIG, screen.MENUS)}
    for req in fake.requests:  # tier 1 twice, tier 2 once (the borderline)
        role = CONFIG.llm.roles["screen_adjudicate" if req.model == TERRA else "screen"]
        sent = render_call(text, refs, model=req.model, max_tokens=role.max_tokens, schema=screen.SCHEMA,
                           temperature=role.temperature)
        assert system_text(req) == sent.sent_prefix, (name, req.model)
    assert sorted(r.model for r in fake.requests) == sorted([LUNA, LUNA, TERRA])
    pins = {n: p.prompts["annotate.screen"] | {"name": n} for n in ("screen-v7", "screen-v8")}
    pp = {n: screen.prompt(pin, CONFIG.llm) for n, pin in pins.items()}
    assert set(pp["screen-v7"].template_digests).isdisjoint(pp["screen-v8"].template_digests)
    ev = [e for i in (0, 1) for e in evidence_of(store, r, i)]
    assert len(ev) == 3 and {e["template_digest"] for e in ev} <= set(pp[name].template_digests)
    assert {e["prompt_name"] for e in ev} == {name}
    cd = {n: config_digest(payload("screen"), pins[n], pp[n]) for n in pins}
    assert cd["screen-v7"] != cd["screen-v8"] and {e["config_digest"] for e in ev} == {cd[name]}
    if name == "screen-v7":
        all_green(store)
    else:  # R1-1: X6 judges the evidence by the run's recorded pin (screen-v8): nothing off it; the
        # plan file (dev-v2: screen-v7) pins otherwise since, a warn finding
        red = {c.check: c.findings for c in run_checks(store) if c.status not in ("ok", "skipped")}
        (x6,) = red.pop("policy_pins")
        assert red == {} and x6.level == "warn" and x6.n == 1, x6
        assert all("prompt 'screen-v8' -> 'screen-v7'" in x for x in x6.items), x6


@pytest.mark.parametrize("name", ["screen-v99", "../../triage/safety-relevance-v1", 7])
def test_unknown_template_name_refuses_before_any_call(env: dict[str, Any], name: Any) -> None:
    store, fake = env["store"], tiers(env["fake"])
    store_docs(store, [f"An abstract {RELEVANT_MARK} and more."])
    p = splan({"name": name})
    rec = run_stage(store, p, "annotate.screen", [fold_corpus(store, p)], series="main")
    want = "no template" if name == "screen-v99" else "not a template name"
    assert rec.status != "ok" and want in str(rec.error), rec.error
    assert fake.count() == 0
