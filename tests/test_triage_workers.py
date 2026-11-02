"""Triage ``options.workers`` (T-RUN B6): the LLM asker's questions from a thread pool, with
annotate's semantics (``workflows/triage/ask.py``): admission (halt, budget, limit) in the calling
thread, in ``doc_id`` order; pairs in flight when the budget trips or the run halts finish and are
kept; evidence assembled in admission order, so the output equals one worker's for the same
answers; ``workers`` stays out of the triage ``config_digest``.

Concurrency is proven with a barrier in the fake transport: N requests must be in flight at once
or the barrier breaks and the run fails (``BrokenBarrierError`` is a code bug to the stage).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stack import gate
from stack.annotate import stage as annotate_stage
from stack.llm import LlmRequest
from tests.fakes.llm import FakeLlm, auth_error, malformed, prompt_text
from tests.synth.rows import sid
from tests.test_gather_identity import Store
from tests.test_triage_questions import (
    GATE,
    NOW,
    decisions,
    output,
    plan_with,
    run_triage,
    title,
    verdict,
)
from tests.test_triage_rules import paper
from workflows.triage import ask as triage_ask
from workflows.triage.main import digest_line

Row = dict[str, Any]
#: Evidence fields that do not depend on the run (ids, stamps, latency) or on thread timing.
STABLE = ("doc_id", "question", "status", "error_class", "verdict", "confidence", "reasoning", "attempts",
          "cost_usd", "basis_digest", "config_digest", "work_digest", "result_digest")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeLlm:
    """``tests/test_triage_questions.py``'s fixture: the fake model through the annotate seams."""
    f = FakeLlm()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # no network path, whatever happens
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(gate, "ASKER_HOOK", None)  # the real wiring: asker_for builds the LLM asker
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: f)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: NOW)
    return f


def barrier_hook(parties: int, first: int | None = None) -> Any:
    """A hook that makes the first ``first`` requests (default ``parties``) wait until ``parties``
    of them are in flight at once; later requests pass."""
    bar = threading.Barrier(parties, timeout=10)
    seen = {"n": 0}
    lock = threading.Lock()
    limit = parties if first is None else first

    def hook(_req: LlmRequest) -> None:
        with lock:
            seen["n"] += 1
            mine = seen["n"]
        if mine <= limit:
            bar.wait()

    return hook


def stable(rows: list[Row]) -> list[tuple[Any, ...]]:
    return [tuple(r[k] for k in STABLE) for r in rows]


def script(fake: FakeLlm) -> None:
    fake.on(title(5), malformed())  # a failed pair among the answers (3 schema attempts)
    for i in range(1, 9):
        fake.on(title(i), verdict(("relevant", "not_relevant", "borderline")[i % 3], confidence=i / 10))


def world(tmp_path: Path, name: str) -> Store:
    store = Store(tmp_path / name)
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 9)])
    store.gather()
    return store


@pytest.mark.slow
def test_workers_give_one_workers_output(tmp_path: Path, fake: FakeLlm) -> None:
    """The same rows and answers under ``workers: 1`` and ``workers: 4`` (four in flight, proven by
    the barrier, and finishing in reverse order) give the same decisions and evidence, in the same
    order."""
    script(fake)
    one, _ = plan_with(tmp_path, "w1", GATE, options={"workers": 1})
    rec1 = run_triage(s1 := world(tmp_path, "s1"), one)
    ev1, _ = output(s1, rec1, "triage_evidence")
    n1 = len(fake.requests)

    slow = {title(1): 0.4, title(2): 0.3, title(3): 0.2}
    bar = barrier_hook(4)

    def hook(req: LlmRequest) -> None:
        bar(req)
        text = prompt_text(req)
        for t, delay in slow.items():
            if t in text:
                time.sleep(delay)  # earlier documents finish last

    fake.hook = hook
    four, _ = plan_with(tmp_path, "w4", GATE, options={"workers": 4})
    rec4 = run_triage(s4 := world(tmp_path, "s4"), four)
    ev4, _ = output(s4, rec4, "triage_evidence")
    assert len(fake.requests) - n1 == n1 == 10  # 7 answers + 3 schema attempts of sid(5)
    assert rec4.counts["ask.workers"] == 4 and "ask.workers" not in rec1.counts
    assert decisions(s4) == decisions(s1)
    assert decisions(s1)[sid(5)][1:3] == ("skipped", "gate_failed")
    by_doc = lambda ev: sorted(stable(ev))  # the file is in evidence_id order, and the ids hash the run id
    assert by_doc(ev4) == by_doc(ev1) and len(ev1) == 8
    part1, _ = output(s1, rec1, "documents")
    part4, _ = output(s4, rec4, "documents")
    assert [r["doc_id"] for r in part4] == [r["doc_id"] for r in part1] == sorted(r["doc_id"] for r in part1)
    keep = ("ask.asked", "ask.failed.schema_invalid", "scoped", "fetch", "skip", "pending", "not_attempted")
    assert {k: rec4.counts.get(k) for k in keep} == {k: rec1.counts.get(k) for k in keep}


@pytest.mark.slow
def test_budget_trip_keeps_in_flight_answers_and_sends_nothing_after(tmp_path: Path, fake: FakeLlm) -> None:
    """Four pairs are in flight together (all four reservations made before any answer settles);
    each costs $0.01 against a $0.0101 budget: the four are kept, the fifth's reservation is
    refused (the scope trips, nothing sent) and the sixth is never admitted."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 7)])
    store.gather()
    fake.default = verdict("relevant", cost=0.01)
    fake.hook = barrier_hook(4)
    plan, _ = plan_with(tmp_path, "b", GATE, budget=0.0101, options={"workers": 4})
    rec = run_triage(store, plan)
    got = decisions(store)
    assert len(fake.requests) == 4
    assert [got[sid(i)][0] for i in range(1, 5)] == ["fetch"] * 4
    for i in (5, 6):
        assert got[sid(i)] == ("pending", "not_attempted", "budget", "rule", ())
    assert rec.counts["ask.not_attempted.budget"] == 2 and rec.scope_complete is False
    ev, _ = output(store, rec, "triage_evidence")
    assert sorted(e["doc_id"] for e in ev) == [sid(i) for i in range(1, 5)]


@pytest.mark.slow
def test_fatal_error_halts_admission_in_flight_finish(tmp_path: Path, fake: FakeLlm) -> None:
    """An auth failure among four in-flight pairs halts the run: the other three (1 s slower, so the
    halt is seen before any slot frees) are kept, later pairs are ``not_attempted[halted]``
    without a request; the auth-failed pair too, with no evidence row and no attempt (D430)."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 7)])
    store.gather()
    fake.default = verdict("not_relevant")
    fake.on(title(2), auth_error())
    bar = barrier_hook(4)

    def hook(req: LlmRequest) -> None:
        bar(req)
        if title(2) not in prompt_text(req):
            time.sleep(1.0)  # the halt is seen first, also on a loaded machine

    fake.hook = hook
    plan, _ = plan_with(tmp_path, "h", GATE, options={"workers": 4})
    rec = run_triage(store, plan)
    got = decisions(store)
    assert len(fake.requests) == 4
    assert [got[sid(i)][0] for i in (1, 3, 4)] == ["skip"] * 3
    ev, _ = output(store, rec, "triage_evidence")
    assert sorted(e["doc_id"] for e in ev) == [sid(i) for i in (1, 3, 4)]
    for i in (2, 5, 6):
        assert got[sid(i)] == ("pending", "not_attempted", "halted", "rule", ())
    assert rec.counts["ask.not_attempted.halted"] == 3 and rec.scope_complete is False


@pytest.mark.slow
def test_max_rows_under_workers_takes_the_first_documents(tmp_path: Path, fake: FakeLlm) -> None:
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 7)])
    store.gather()
    fake.default = verdict("not_relevant")
    plan, _ = plan_with(tmp_path, "l", GATE, options={"max_rows": 3, "workers": 8})
    rec = run_triage(store, plan)
    got = decisions(store)
    assert len(fake.requests) == 3 and rec.counts["ask.limit"] == 3
    assert [got[sid(i)][0] for i in (1, 2, 3)] == ["skip"] * 3
    assert all(got[sid(i)] == ("pending", "not_attempted", "limit", "rule", ()) for i in (4, 5, 6))


@pytest.mark.slow
def test_workers_change_rescopes_nothing(tmp_path: Path, fake: FakeLlm) -> None:
    """``workers`` is not in the triage ``config_digest``: a rerun under another worker count has
    nothing in scope and makes no call (``max_rows``, which is, would re-scope every row)."""
    store = world(tmp_path, "s")
    fake.default = verdict("relevant")
    one, _ = plan_with(tmp_path, "w1", GATE)
    run_triage(store, one)
    calls = len(fake.requests)
    sixteen, _ = plan_with(tmp_path, "w16", GATE, options={"workers": 16})
    asker = gate.asker_for(sixteen, sixteen.stage_line("triage"))
    assert digest_line(sixteen.stage_line("triage")).digest() == one.stage_line("triage").digest()
    assert gate.triage_config_digest(digest_line(sixteen.stage_line("triage")), sixteen.gate, asker) == \
        gate.triage_config_digest(digest_line(one.stage_line("triage")), one.gate, asker)
    rec16 = run_triage(store, sixteen)
    assert (rec16.counts["scoped"], len(fake.requests)) == (0, calls)
    capped, _ = plan_with(tmp_path, "cap", GATE, options={"max_rows": 100, "workers": 16})
    assert digest_line(capped.stage_line("triage")).digest() != one.stage_line("triage").digest()


@pytest.mark.parametrize("bad", [0, 1025, "4", True, 2.0])
def test_bad_workers_are_refused(tmp_path: Path, fake: FakeLlm, bad: Any) -> None:
    plan, _ = plan_with(tmp_path, "bad", GATE, options={"workers": bad})
    with pytest.raises(gate.GateError, match="options.workers"):
        triage_ask.llm_asker(plan, plan.stage_line("triage"))


@pytest.mark.parametrize("n", [1, 128, 256, 1024])
def test_workers_up_to_the_cap_are_accepted(tmp_path: Path, fake: FakeLlm, n: int) -> None:
    """The cap is 1024 (D716; was 256, D692), shared with annotate."""
    plan, _ = plan_with(tmp_path, f"ok{n}", GATE, options={"workers": n})
    asker = triage_ask.llm_asker(plan, plan.stage_line("triage"))
    assert asker is not None and asker.workers == n and annotate_stage.MAX_WORKERS == 1024


def test_a_line_without_workers_asks_64_at_once(tmp_path: Path, fake: FakeLlm) -> None:
    """The default when a line sets none is 64 (D692; was 1)."""
    plan, _ = plan_with(tmp_path, "default", GATE, options={})
    asker = triage_ask.llm_asker(plan, plan.stage_line("triage"))
    assert asker is not None and asker.workers == 64 == annotate_stage.DEFAULT_WORKERS
