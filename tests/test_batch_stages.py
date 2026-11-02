"""``llm_mode: batch`` in the stages (T-BATCH; :mod:`stack.annotate.batchmode`, :mod:`stack.llm.batch`):
triage's asker and the annotate framework (screen with both tiers, affiliation) run through the real
stage runner with the fake model as a batch transport (``tests/fakes/llm.py``: ``fake.batch(...)``,
``fake.batches``, ``count()`` = sync posts only). The batch driver's clock is
:data:`stack.annotate.batchmode.CLOCK`, its sleep the framework's ``SLEEP``: one fake clock that the
polls advance, so the time cap takes no time. No network; each run's ledger is in memory (``run_ledgers``).

Cases: batch and sync give the same evidence and decisions on identical fakes (triage incl. a
schema-invalid batch reply asked sync in the replay; screen's two tiers in two passes; affiliation
incl. its no-call XML tier and a failure row), a pass below ``batch_min_requests`` is asked sync;
screen's tier-2 allowance in batch mode bounds tier 2's estimate at the batch price, settled at the
estimate in every pass, so the collect pass and the replay admit the same tier-2 set; the time cap and an expired batch leave ``not_attempted[unanswered]`` (scope incomplete,
build reason ``unanswered``) and the next run asks only those; the budget stop leaves a
``not_attempted[budget]`` suffix; a bad ``llm_mode`` or ``batch_*`` value is refused before any call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import stack.annotate.stage as annotate_stage
from stack import gate
from stack.annotate import batchmode
from stack.build import incomplete_reasons
from stack.config import load
from stack.llm.batch import BATCH_PRICE_FACTOR
from stack.runs import find_run_record
from stack.stage_runner import run_stage
from tests.fakes.annotate import (
    AT_RUN,
    annotate,
    doc_with_text,
    fold_corpus,
    rows_of,
    write_documents,
)
from tests.fakes.llm import FakeLlm, ok
from tests.synth.records import run_line
from tests.synth.rows import sid
from tests.test_affiliation_stage import (
    BYLINE_TEXT,
    JATS,
    ROSTER_1,
    XML_TEXT,
    aplan,
    paper,
)
from tests.test_screen_tiers import (
    BORDERLINE_MARK,
    LUNA,
    RELEVANT_MARK,
    TERRA,
    all_green,
    splan,
    tiers,
    verdict,
)
from tests.test_triage_questions import (
    GATE,
    NOW,
    decisions,
    plan_with,
    run_triage,
)
from tests.test_triage_questions import verdict as tverdict
from tests.test_triage_rules import output
from tests.test_triage_workers import script, world
from workflows.annotate.screen import main as screen
from workflows.triage import ask as triage_ask

CONFIG = load()
BATCH = {"llm_mode": "batch", "batch_min_requests": 1}
#: Evidence columns that legitimately differ between a sync and a batch run of the same answers.
DIFFER = frozenset({"evidence_id", "run_id", "supersedes", "latency_ms", "cost_usd"})


class Clock:
    """A fake monotonic clock; the driver's sleeps advance it."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_ledgers: Any) -> dict[str, Any]:
    fake = FakeLlm()
    clock = Clock()
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)    # no network path, whatever happens
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(gate, "ASKER_HOOK", None)  # triage: asker_for builds the LLM asker
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", clock.sleep)
    monkeypatch.setattr(batchmode, "CLOCK", clock)
    return {"tmp": tmp_path, "fake": fake, "clock": clock, "ledger": run_ledgers}


@pytest.fixture
def annotate_env(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    return env


@pytest.fixture
def triage_env(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: NOW)
    return env


def same(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows without the columns a batch run legitimately changes, in a run-independent order."""
    out = [{k: v for k, v in r.items() if k not in DIFFER} for r in rows]
    return sorted(out, key=lambda r: (r["doc_id"], repr(sorted(r.items()))))


def batched(fake: FakeLlm) -> list[tuple[str, int]]:
    return [(b.model, b.n) for b in fake.batches]


def batch_texts(fake: FakeLlm, i: int) -> list[str]:
    """The prompt texts of fake batch ``i``'s requests."""
    return ["\n".join(str(m["content"]) for m in body["messages"]) for _, body in fake.batches[i].bodies]


# ── triage ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.slow  # three runner-driven triage stores, ~13 s
def test_triage_batch_gives_the_sync_evidence_and_decisions(triage_env: dict[str, Any]) -> None:
    """Eight pairs: sync, then batch (one batch of 8; the schema-invalid reply of ``sid(5)`` is
    asked sync in the replay, three schema attempts, ending ``failed_permanent`` as in sync), then
    batch below ``batch_min_requests`` (asked sync, no batch)."""
    tmp, fake = triage_env["tmp"], triage_env["fake"]
    script(fake)
    sync_plan, _ = plan_with(tmp, "sync", GATE)
    rec_s = run_triage(s := world(tmp, "s"), sync_plan)
    n_sync = fake.count()
    assert n_sync == 10 and "batch.requests" not in rec_s.counts  # 7 answers + 3 schema attempts

    bplan, _ = plan_with(tmp, "batch", GATE, options=dict(BATCH))
    rec_b = run_triage(b := world(tmp, "b"), bplan)
    assert batched(fake) == [("openai/gpt-6-luna", 8)]
    assert fake.count() - n_sync == 3  # only sid(5)'s retries went sync
    assert (rec_b.counts["batch.requests"], rec_b.counts["batch.answered"], rec_b.counts["batch.retry_sync"]) == (
        8, 7, 1)
    assert decisions(b) == decisions(s) and decisions(b)[sid(5)][1:3] == ("skipped", "gate_failed")
    ev_s, ev_b = output(s, rec_s, "triage_evidence")[0], output(b, rec_b, "triage_evidence")[0]
    assert same(ev_b) == same(ev_s) and len(ev_b) == 8
    cost_b = {e["doc_id"]: e["cost_usd"] for e in ev_b}
    assert cost_b[sid(5)] > {e["doc_id"]: e["cost_usd"] for e in ev_s}[sid(5)]  # + the paid batch attempt
    keep = ("ask.asked", "ask.failed.schema_invalid", "scoped", "fetch", "skip", "pending", "not_attempted")
    assert {k: rec_b.counts.get(k) for k in keep} == {k: rec_s.counts.get(k) for k in keep}
    assert rec_b.counts["batch.sync_fallback"] == 0 and rec_b.scope_complete

    small, _ = plan_with(tmp, "small", GATE, options={"llm_mode": "batch"})  # min_requests 100 > 8
    rec_m = run_triage(m := world(tmp, "m"), small)
    assert len(fake.batches) == 1 and fake.count() - n_sync == 3 + 10
    assert (rec_m.counts["batch.sync"], rec_m.counts["batch.requests"]) == (8, 0)
    assert decisions(m) == decisions(s)


@pytest.mark.slow  # ~6 s
def test_triage_time_cap_leaves_unanswered_and_the_next_run_asks_only_those(triage_env: dict[str, Any]) -> None:
    """Two batches of 4; the second never finishes and the 1-minute cap abandons it: its pairs are
    ``not_attempted[unanswered]`` (no evidence, scope incomplete, build reason ``unanswered``); the
    next run asks only those, in one batch, and carries the first run's answers."""
    tmp, fake, clock = triage_env["tmp"], triage_env["fake"], triage_env["clock"]
    fake.default = tverdict("relevant")
    fake.batch().batch(never_finish=True).batch()
    p, _ = plan_with(tmp, "cap", GATE, options={**BATCH, "batch_max_requests": 4, "batch_wait_minutes": 1})
    store = world(tmp, "s")
    rec1 = run_triage(store, p)
    got = decisions(store)
    assert batched(fake) == [("openai/gpt-6-luna", 4)] * 2 and fake.count() == 0
    assert [got[sid(i)][0] for i in range(1, 5)] == ["fetch"] * 4
    for i in range(5, 9):
        assert got[sid(i)] == ("pending", "not_attempted", "unanswered", "rule", ())
    assert rec1.counts["ask.not_attempted.unanswered"] == 4 and rec1.counts["batch.batches_abandoned"] == 1
    assert rec1.scope_complete is False and incomplete_reasons(rec1.counts) == ["unanswered"]
    assert clock.t - 1000.0 >= 60
    ev1, _ = output(store, rec1, "triage_evidence")
    assert sorted(e["doc_id"] for e in ev1) == [sid(i) for i in range(1, 5)]

    rec2 = run_triage(store, p)
    assert len(fake.batches) == 3 and fake.batches[2].n == 4 and fake.count() == 0
    assert sorted(t.split("Title: ")[1].split("\n")[0] for t in batch_texts(fake, 2)) == [
        f"Paper {i}" for i in range(5, 9)]
    assert all(v[0] == "fetch" for v in decisions(store).values())
    assert rec2.scope_complete and rec2.counts["ask.asked"] == 4
    ev2, _ = output(store, rec2, "triage_evidence")
    assert len(ev2) == 8 and {e["status"] for e in ev2} == {"ok"}


def test_triage_batch_admits_what_max_rows_admits(triage_env: dict[str, Any]) -> None:
    """``max_rows: 3`` over six documents: only the first three documents' pairs are batched; the
    rest are ``not_attempted[limit]`` as in sync."""
    tmp, fake = triage_env["tmp"], triage_env["fake"]
    fake.default = tverdict("not_relevant")
    store = world(tmp, "s")
    p, _ = plan_with(tmp, "l", GATE, options={**BATCH, "max_rows": 3})
    rec = run_triage(store, p)
    got = decisions(store)
    assert [b.n for b in fake.batches] == [3] and fake.count() == 0 and rec.counts["ask.limit"] == 5
    assert [got[sid(i)][0] for i in (1, 2, 3)] == ["skip"] * 3
    assert all(got[sid(i)] == ("pending", "not_attempted", "limit", "rule", ()) for i in range(4, 9))


@pytest.mark.parametrize(("options", "match"), [
    ({"llm_mode": "bogus"}, "llm_mode"), ({"llm_mode": "Batch"}, "llm_mode"),
    ({"llm_mode": "batch", "batch_wait_minutes": -1}, "batch_wait_minutes"),
    ({"llm_mode": "sync", "batch_max_requests": 0}, "batch_max_requests")])
def test_triage_bad_llm_mode_is_refused_before_any_call(triage_env: dict[str, Any], options: dict[str, Any],
                                                        match: str) -> None:
    p, _ = plan_with(triage_env["tmp"], "bad", GATE, options=options)
    with pytest.raises(gate.GateError, match=match):
        triage_ask.llm_asker(p, p.stage_line("triage"))
    store = world(triage_env["tmp"], "s")
    rec = run_line(store.root, p, "triage")
    assert rec is not None and rec.status == "failed" and match in str(rec.error)
    assert triage_env["fake"].count() == 0 and triage_env["fake"].batches == []


def test_triage_batch_options_stay_out_of_the_config_digest(triage_env: dict[str, Any]) -> None:
    from workflows.triage.main import digest_line

    one, _ = plan_with(triage_env["tmp"], "one", GATE)
    b, _ = plan_with(triage_env["tmp"], "b", GATE, options={**BATCH, "batch_max_requests": 5, "batch_max_mb": 1,
                                                            "batch_wait_minutes": 30})
    assert digest_line(b.stage_line("triage")).digest() == one.stage_line("triage").digest()


# ── annotate: screen ─────────────────────────────────────────────────────────────────────────


def store_docs(store: Path, texts: list[str]) -> None:
    write_documents(store, [doc_with_text(store, i, t) for i, t in enumerate(texts)], run=1)


def screen_run(env: dict[str, Any], name: str, texts: list[str], **line: Any) -> tuple[Path, Any]:
    store = env["tmp"] / name
    store_docs(store, texts)
    p = splan(**line)
    return store, annotate(store, p, "screen", fold_corpus(store, p))


def test_screen_two_tiers_in_two_passes_give_the_sync_rows(annotate_env: dict[str, Any]) -> None:
    """Tier 1 for both documents in pass 1, the borderline's tier 2 alone in pass 2; the replay
    writes the tier-1 row and the superseding tier-2 row exactly as sync does; no sync post."""
    fake = tiers(annotate_env["fake"], tier2="relevant")
    texts = [f"An abstract {RELEVANT_MARK} and more.", f"{BORDERLINE_MARK} of a paper."]
    s, rs = screen_run(annotate_env, "s", texts)
    n_sync = fake.count()
    assert n_sync == 3
    b, rb = screen_run(annotate_env, "b", texts, options=dict(BATCH))
    assert batched(fake) == [(LUNA, 2), (TERRA, 1)] and fake.count() == n_sync
    assert batch_texts(fake, 1)[0].count("thin abstract, unsure") == 1  # tier 1's rationale
    assert same(rows_of(b, rb, "screen_evidence")) == same(rows_of(s, rs, "screen_evidence"))
    assert same(rows_of(b, rb, "screen")) == same(rows_of(s, rs, "screen"))
    ev = {(e["doc_id"], e["model"]): e for e in rows_of(b, rb, "screen_evidence")}
    assert ev[(sid(1), TERRA)]["supersedes"] == ev[(sid(1), LUNA)]["evidence_id"]
    row = {r["doc_id"]: r for r in rows_of(b, rb, "screen")}[sid(1)]
    assert (row["verdict"], row["adjudication_tier"], row["evidence_id"]) == (
        "relevant", 2, ev[(sid(1), TERRA)]["evidence_id"])
    assert (rb.counts["batch.passes"], rb.counts["batch.requests"], rb.counts["tier2"]) == (2, 3, 1)
    assert rb.counts["not_attempted.unanswered"] == 0 and rb.scope_complete


def tier2_estimates(fake: FakeLlm, name: str) -> dict[str, float]:
    """Each tier-2 request's allowance estimate (:func:`screen.tier2_estimate`), by its document mark."""
    refs = {"enums": screen._render_menu(CONFIG, screen.MENUS)}
    out = {}
    for r in fake.requests:
        if r.model == TERRA:
            user = str(r.payload["messages"][1]["content"])
            mark = user.split(f"{BORDERLINE_MARK} number ")[1].split(".")[0]
            out[mark] = screen.tier2_estimate(CONFIG.llm, TERRA, template=screen.template(name), refs=refs,
                                              user=user, schema=screen.schema_for(name))
    return out


@pytest.mark.parametrize("cap", ["tier2_max_usd", "screen_usd_per_1k"])
def test_screen_tier2_allowance_batches_what_sync_admits(annotate_env: dict[str, Any],
                                                         monkeypatch: pytest.MonkeyPatch, cap: str) -> None:
    """Four borderlines; tier 2 costs exactly its allowance estimate, and the cap fits two; the batch
    run's cap is the same in batch-price terms (tier 2's share × ``BATCH_PRICE_FACTOR``, as batch
    mode prices the estimate). Sync admits two; batch mode batches exactly those two in pass 2 (each
    pass has its own allowance, so tier 1 is not counted twice and pass 2's tier 2 is not carried
    into the replay) and the replay writes the same rows: the other two ``not_attempted[budget]``,
    nothing asked sync."""
    texts = [f"{BORDERLINE_MARK} number {i}." for i in range(4)]
    probe = tiers(FakeLlm(), tier2="relevant")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: probe)
    screen_run(annotate_env, "probe", texts)
    est = tier2_estimates(probe, "screen-v7")
    assert sorted(est) == ["0", "1", "2", "3"] and max(est.values()) - min(est.values()) < 1e-12
    e = max(est.values())

    fake = annotate_env["fake"]
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    tier1 = 0.001
    for i in range(4):
        fake.on(f"number {i}.", verdict("relevant", "strong reader decided", est[str(i)]), model=TERRA)
    fake.on(model=LUNA, reply=verdict("borderline", "thin abstract, unsure", tier1))
    # tier2_max_usd: 2.5 estimates, the first two fit; screen_usd_per_1k: tier 1 + 0.7 estimates per
    # document, so the first document's tier 2 fits only as nothing of tier 2 is spent yet (D706),
    # the second's does not, the third's does, the fourth's not
    options = {"tier2_max_usd": 2.5 * e} if cap == "tier2_max_usd" else {"screen_usd_per_1k": 1000 * (tier1 + 0.7 * e)}
    s, rs = screen_run(annotate_env, "s", texts, options=options)
    sync_t2 = fake.count(model=TERRA)
    budget = [(r["doc_id"], r["status"], r["error_class"]) for r in rows_of(s, rs, "screen")
              if r["status"] == "not_attempted"]
    assert sync_t2 == 2 and [x[1:] for x in budget] == [("not_attempted", "budget")] * 2
    assert [x[0] for x in budget] == ([sid(2), sid(3)] if cap == "tier2_max_usd" else [sid(1), sid(3)])
    f = BATCH_PRICE_FACTOR
    batch_options = ({"tier2_max_usd": 2.5 * e * f} if cap == "tier2_max_usd"
                     else {"screen_usd_per_1k": 1000 * (tier1 + 0.7 * e * f)})
    b, rb = screen_run(annotate_env, "b", texts, options={**batch_options, **BATCH})
    assert batched(fake) == [(LUNA, 4), (TERRA, 2)] and fake.count(model=TERRA) == sync_t2
    assert same(rows_of(b, rb, "screen")) == same(rows_of(s, rs, "screen"))
    assert same(rows_of(b, rb, "screen_evidence")) == same(rows_of(s, rs, "screen_evidence"))
    assert rb.counts["batch.sync_fallback"] == 0 and not rb.scope_complete


def test_screen_tier2_allowance_decides_the_same_in_every_batch_pass(annotate_env: dict[str, Any],
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """A binding ``tier2_max_usd`` (3.5 tier-2 estimates at the batch price) and tier 2 actually
    costing a third of its sync-price estimate: batch mode prices the estimate at the batch price
    and settles every tier-2 call at it in every pass, so pass 2 batches three of the four, no third
    pass chases the room their cheaper actuals would leave, the replay admits the same three
    (nothing asked sync), and the fourth is ``not_attempted[budget]``."""
    texts = [f"{BORDERLINE_MARK} number {i}." for i in range(4)]
    probe = tiers(FakeLlm(), tier2="relevant")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: probe)
    screen_run(annotate_env, "probe", texts)
    est = tier2_estimates(probe, "screen-v7")
    e = max(est.values())

    fake = annotate_env["fake"]
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    for i in range(4):
        fake.on(f"number {i}.", verdict("relevant", "strong reader decided", est[str(i)] / 3), model=TERRA)
    fake.on(model=LUNA, reply=verdict("borderline", "thin abstract, unsure", 0.001))
    b, rb = screen_run(annotate_env, "b", texts, options={"tier2_max_usd": 3.5 * e * BATCH_PRICE_FACTOR, **BATCH})
    assert batched(fake) == [(LUNA, 4), (TERRA, 3)] and fake.count() == 0
    assert (rb.counts["batch.passes"], rb.counts["batch.sync_fallback"], rb.counts["tier2"]) == (2, 0, 3)
    pass2 = {sid(int(t.split(" number ")[1].split(".")[0])) for t in batch_texts(fake, 1)}
    rows = rows_of(b, rb, "screen")
    assert {r["doc_id"] for r in rows if r["adjudication_tier"] == 2} == pass2
    assert [(r["status"], r["error_class"]) for r in rows if r["doc_id"] not in pass2] == [("not_attempted", "budget")]


@pytest.mark.parametrize("second", [{"never_finish": True}, {"final": "expired"}])
@pytest.mark.slow
def test_screen_unanswered_batch_then_the_next_run(annotate_env: dict[str, Any], second: dict[str, Any]) -> None:
    """Two batches of 2 tier-1 requests; the second never finishes (the 1-minute cap abandons it)
    or expires: its documents are ``not_attempted[unanswered]``, no evidence, scope incomplete; a
    rerun with this run's outputs as its prior (``--in``) asks only those and carries the rest."""
    fake = tiers(annotate_env["fake"])
    fake.batch().batch(**second).batch()
    texts = [f"Paper {i}: {RELEVANT_MARK}." for i in range(4)]
    line = {"options": {**BATCH, "batch_max_requests": 2, "batch_wait_minutes": 1}}
    store = annotate_env["tmp"] / "s"
    store_docs(store, texts)
    p = splan(**line)
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    rows = rows_of(store, r1, "screen")
    assert [(r["status"], r["error_class"]) for r in rows] == [("ok", None)] * 2 + [
        ("not_attempted", "unanswered")] * 2
    assert [r["evidence_id"] for r in rows[2:]] == [None, None]
    assert sorted(e["doc_id"] for e in rows_of(store, r1, "screen_evidence")) == [sid(0), sid(1)]
    key = "batch.batches_abandoned" if "never_finish" in second else "batch.batches_expired"
    assert r1.counts[key] == 1 and r1.counts["not_attempted.unanswered"] == 2 and r1.counts["batch.unanswered"] == 2
    assert not r1.scope_complete and incomplete_reasons(r1.counts) == ["unanswered"]
    assert "failed.unanswered" not in str(r1.counts) and "halted" not in r1.counts and fake.count() == 0
    all_green(store)  # V7/V8/X8 accept the unreached rows (vocab: not_attempted[unanswered])

    r2 = annotate(store, p, "screen", corpus, prior=r1)
    assert len(fake.batches) == 3 and fake.batches[2].n == 2 and fake.count() == 0
    assert sorted(t.split("Title: ")[1].split("\n")[0] for t in batch_texts(fake, 2)) == ["Paper 2", "Paper 3"]
    assert r2.counts["carried"] == 2 and r2.scope_complete
    assert {r["status"] for r in rows_of(store, r2, "screen")} == {"ok"}


def test_screen_spend_in_the_final_record_settled_and_abandoned_at_the_ratio(annotate_env: dict[str, Any]) -> None:
    """T-STORE spend: two batches of 2; the first settles at the reported costs, the second never
    finishes and is abandoned at the 1-minute cap, so its reservations are reconciled at reserved ×
    the run's actual/reserved ratio (D308), never released at $0. The final record on disk holds
    the run's spend: settled micro-USD per ``openrouter/<model>``, nothing open, no batch in flight,
    and the abandoned batch's id (F1: it used to vanish when it left the in-flight list)."""
    fake = tiers(annotate_env["fake"])
    fake.batch().batch(never_finish=True)
    store = annotate_env["tmp"] / "s"
    store_docs(store, [f"Paper {i}: {RELEVANT_MARK}." for i in range(4)])
    p = splan(options={**BATCH, "batch_max_requests": 2, "batch_wait_minutes": 1})
    r = annotate(store, p, "screen", fold_corpus(store, p))
    assert r.counts["batch.batches_abandoned"] == 1
    lines = annotate_env["ledger"](r.run_id).lines()
    reserved = {x["reservation_id"]: x["cost_usd"] for x in lines if x["kind"] == "reserve"}
    recon = [x for x in lines if x["kind"] == "reconcile"]
    assert len(reserved) == 4 and len(recon) == 4 and not [x for x in lines if x["kind"] == "release"]
    settled, abandoned = recon[:2], recon[2:]
    ratio = sum(x["actual_usd"] for x in settled) / sum(reserved[x["reservation_id"]] for x in settled)
    assert ratio > 0 and ratio != pytest.approx(1.0)
    for x in abandoned:
        assert x["actual_usd"] == pytest.approx(reserved[x["reservation_id"]] * ratio) and x["actual_usd"] > 0
    total = sum(x["actual_usd"] for x in recon)
    never = [b.id for b in fake.batches if b.script.never_finish]
    assert len(never) == 1 and len(fake.batches) == 2
    want = {"usd_micro": {f"openrouter/{LUNA}": round(total * 1e6)}, "reserved_open_usd_micro": 0, "batch_ids": [],
            "abandoned_batch_ids": never}
    assert r.spend == want
    on_disk = find_run_record(store, r.run_id)
    assert on_disk is not None and on_disk.spend == want


def test_a_lost_batch_is_listed_as_abandoned_and_charged_in_full(annotate_env: dict[str, Any]) -> None:
    """T-STORE C2 (D366) and G1 (D370): a batch whose poll answers 404/410 (the provider no longer
    knows it) is named in ``spend.abandoned_batch_ids`` instead of vanishing, and each of its calls
    is charged at its full reservation (it may have run and been billed: counted as full),
    never released at $0; the run's end does not release or charge them again."""
    fake = tiers(annotate_env["fake"])
    fake.batch().batch(lost=404)
    store = annotate_env["tmp"] / "s"
    store_docs(store, [f"Paper {i}: {RELEVANT_MARK}." for i in range(4)])
    p = splan(options={**BATCH, "batch_max_requests": 2, "batch_wait_minutes": 1})
    r = annotate(store, p, "screen", fold_corpus(store, p))
    lost = [b.id for b in fake.batches if b.script.lost]
    assert len(lost) == 1 and len(fake.batches) == 2
    assert r.counts["batch.unanswered"] == 2
    lines = annotate_env["ledger"](r.run_id).lines()
    reserved = {x["reservation_id"]: x["cost_usd"] for x in lines if x["kind"] == "reserve"}
    recon = [x for x in lines if x["kind"] == "reconcile"]
    assert len(reserved) == 4 and len(recon) == 4 and not [x for x in lines if x["kind"] == "release"]
    # the lost batch's two calls at their full reservation; the other batch's at its reported costs
    full = [x for x in recon if x["actual_usd"] == pytest.approx(reserved[x["reservation_id"]])]
    assert len(full) == 2 and all(x["actual_usd"] > 0 for x in full)
    total = sum(x["actual_usd"] for x in recon)
    want = {"usd_micro": {f"openrouter/{LUNA}": round(total * 1e6)}, "reserved_open_usd_micro": 0, "batch_ids": [],
            "abandoned_batch_ids": lost}
    assert r.spend == want
    on_disk = find_run_record(store, r.run_id)
    assert on_disk is not None and on_disk.spend == want


def test_screen_batch_admits_what_max_rows_admits(annotate_env: dict[str, Any]) -> None:
    fake = tiers(annotate_env["fake"])
    store, r = screen_run(annotate_env, "s", [f"Paper {i}: {RELEVANT_MARK}." for i in range(4)],
                          options={**BATCH, "max_rows": 2})
    assert batched(fake) == [(LUNA, 2)] and fake.count() == 0
    assert [(x["status"], x["error_class"]) for x in rows_of(store, r, "screen")] == [("ok", None)] * 2 + [
        ("not_attempted", "limit")] * 2


def test_screen_budget_stop_leaves_a_not_attempted_budget_suffix(annotate_env: dict[str, Any]) -> None:
    """A stage budget of 2.5 batch reservations, each answer costing more than the budget: two
    requests are reserved and submitted, the third refused with nothing in flight (the budget stop):
    rows ok, ok, then ``not_attempted[budget]``, as the replay's budget scope trips at the first
    refused key."""
    fake = annotate_env["fake"]
    texts = [f"Paper {i}: {RELEVANT_MARK}." for i in range(4)]
    probe_store = annotate_env["tmp"] / "probe"
    store_docs(probe_store, texts)
    fake.on(RELEVANT_MARK, verdict("relevant", "decided", 1.0), model=LUNA)
    p = splan(options=dict(BATCH))
    rp = annotate(probe_store, p, "screen", fold_corpus(probe_store, p))
    res = [x["cost_usd"] for x in annotate_env["ledger"](rp.run_id).lines()
           if x["kind"] == "reserve" and x["run_id"] == rp.run_id]
    assert len(res) == 4 and max(res) - min(res) < 1e-12
    store = annotate_env["tmp"] / "s"
    store_docs(store, texts)
    tight = splan(options=dict(BATCH), budget={"max_usd": 2.5 * res[0]})
    r = annotate(store, tight, "screen", fold_corpus(store, tight))
    rows = rows_of(store, r, "screen")
    assert [(x["status"], x["error_class"]) for x in rows] == [("ok", None)] * 2 + [("not_attempted", "budget")] * 2
    assert fake.batches[-1].n == 2 and fake.count() == 0
    assert (r.counts["batch.refused_budget"], r.counts["not_attempted.budget"]) == (2, 2)
    assert not r.scope_complete and incomplete_reasons(r.counts) == ["budget"]


@pytest.mark.parametrize("options", [{"llm_mode": "bogus"}, {"llm_mode": "batch", "batch_min_requests": -1}])
def test_annotate_bad_llm_mode_is_refused_before_any_call(annotate_env: dict[str, Any], options: dict[str, Any]) -> None:
    fake = tiers(annotate_env["fake"])
    store = annotate_env["tmp"] / "s"
    store_docs(store, [f"An abstract {RELEVANT_MARK}."])
    p = splan(options=options)
    rec = run_stage(store, p, "annotate.screen", [fold_corpus(store, p)], series="main")
    assert rec.status == "failed" and ("llm_mode" in str(rec.error) or "batch_min_requests" in str(rec.error))
    assert fake.count() == 0 and fake.batches == []


# ── annotate: affiliation ────────────────────────────────────────────────────────────────────


def test_affiliation_batch_gives_the_sync_rows(annotate_env: dict[str, Any]) -> None:
    """The XML tier (no call), a model-read byline, an empty answer and a schema-invalid reply
    (asked sync in the replay, ending in the failure row sync writes): the same ``affiliation`` and
    evidence rows as sync."""
    fake = annotate_env["fake"]
    fake.on("MARK_ONE", ok({"authors": [{"author_surface": "Alice Smith", "affiliations": [
        {"surface": "Anthropic, San Francisco", "marker": "1"}]}]}, cost=0.002), model=LUNA)
    fake.on("MARK_TWO", ok({"authors": []}))
    fake.on("MARK_FAIL", ok({"nonsense": True}))

    def docs(store: Path) -> None:
        write_documents(store, [
            paper(store, 0, XML_TEXT, [("Doe, Jane", None), ("Bob Roe", None)], xml=JATS),
            paper(store, 1, BYLINE_TEXT, ROSTER_1),
            paper(store, 2, "# Paper 2\n\nMARK_TWO a post with no byline.", []),
            paper(store, 3, "# Paper 3\n\nAnn Other\nMARK_FAIL", [("Ann Other", None)]),
        ], run=1)

    out = {}
    for name, line in (("s", {}), ("b", {"options": dict(BATCH)})):
        store = annotate_env["tmp"] / name
        docs(store)
        p = aplan(**line)
        out[name] = (store, annotate(store, p, "affiliation", fold_corpus(store, p)))
    (s, rs), (b, rb) = out["s"], out["b"]
    assert batched(fake) == [(LUNA, 3)] and rb.counts["batch.retry_sync"] == 1
    assert same(rows_of(b, rb, "affiliation")) == same(rows_of(s, rs, "affiliation"))
    assert same(rows_of(b, rb, "affiliation_evidence")) == same(rows_of(s, rs, "affiliation_evidence"))
    fail = [e for e in rows_of(b, rb, "affiliation_evidence") if e["doc_id"] == sid(3)]
    assert [(e["status"], e["error_class"]) for e in fail] == [("failed_permanent", "schema_invalid")]
    assert rb.counts["xml_tier"] == 1 and rb.scope_complete
