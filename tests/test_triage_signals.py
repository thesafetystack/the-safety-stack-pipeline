"""Triage signals (D724; ``stack/gate.py`` "Signals", ``workflows/triage/main.py``): every document is
asked, also outside the question scope (a ``sql:`` fetch, or a primary role that is ``ok``), and an
answer there is stored as a signal (the fill with the verdict) that decides nothing.

Pure cases on :func:`stack.gate.answer_due` and the signal helpers; store cases run the real
``gather`` and ``triage`` stages over synthetic sources with the LLM asker over the fake transport
(``tests/test_triage_questions.py``'s ``fake``): the deployment case (rows an earlier run left with
the plain fill, under an unchanged triage config, are asked by the next run and decide as before;
acquire's ``permit`` and X14 agree), and a prompt bump that makes every document due again.
"""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stack import gate
from stack.annotate import stage as annotate_stage
from stack.plan import Plan
from tests.fakes.llm import FakeLlm
from tests.synth.rows import sid
from tests.test_gather_identity import Store
from tests.test_gather_merge import write_documents_part
from tests.test_triage_questions import Q, acquired_row, decisions, fake, plan_with, run_triage, title, verdict
from tests.test_triage_rules import ann, paper
from tests.test_triage_standing import x14
from workflows.acquire.scope import permit
from workflows.triage import ask as triage_ask

__all__ = ["fake"]  # the fixture, imported for this module's tests

Row = dict[str, Any]
NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------- pure
QS = [gate.Question("safety_relevance", "triage.safety-relevance-v1", {}, "qd1")]


def row_with(question: Row | None, *, basis: str = "fd", state: str = "acquired") -> Row:
    qs = [] if question is None else [question]
    return {"doc_id": "d1", "state": state, "identity": {"is_masked": False},
            "metadata": {"fields_digest": "fd"},
            "annotations": {"triage": {"basis_digest": basis, "payload": {"decision": "fetch", "questions": qs}}}}


def qrow(status: str, error_class: str | None, *, verdict_: str | None = None, evidence_id: str | None = None,
         digest: str = "qd1") -> Row:
    return {"question": "safety_relevance", "status": status, "error_class": error_class, "verdict": verdict_,
            "confidence": 0.7 if verdict_ else None, "evidence_id": evidence_id, "config_digest": digest}


def due(row: Row, ev: dict[str, Row] | None = None, *, askable: bool = True, have_prior: bool = True) -> bool:
    return gate.answer_due(row, QS, ev or {}, NOW, askable=lambda _r: askable, have_prior=have_prior)


def test_answer_due() -> None:
    fill = qrow("skipped", "not_needed")
    assert due(row_with(fill))                                   # the plain fill: never asked
    assert not due(row_with(fill), askable=False)                # nothing to ask on (no basis, no title)
    assert due(row_with(qrow("skipped", "not_requested")))
    assert due(row_with(None))                                   # no row for the question
    assert due(row_with(qrow("ok", None, verdict_="relevant", digest="old")))  # an older question digest
    assert not due(row_with(qrow("skipped", "not_needed", verdict_="relevant", evidence_id="e1")))  # a signal
    assert not due(row_with(qrow("ok", None, verdict_="relevant")))           # an answer
    assert not due(row_with(qrow("failed_permanent", "schema_invalid")))      # a real answer, final
    assert not due(row_with(qrow("not_attempted", "budget")))                 # question_due's, not this rule's
    # a fill with failure evidence: the retry rule decides; missing evidence is due (R2-1), unless no prior
    perm = {"e2": {"status": "failed_permanent", "attempts": 1, "retry_after": None}}
    trans = {"e2": {"status": "failed_transient", "attempts": 1, "retry_after": NOW + dt.timedelta(hours=1)}}
    trans_due = {"e2": {"status": "failed_transient", "attempts": 1, "retry_after": NOW}}
    failed = row_with(qrow("skipped", "not_needed", evidence_id="e2"))
    assert not due(failed, perm) and not due(failed, trans) and due(failed, trans_due)
    assert due(failed, {}) and not due(failed, {}, have_prior=False)
    # out of this rule: a changed basis (in_scope's), a retired or masked row
    assert not due(row_with(fill, basis="other"))
    assert not due(row_with(fill, state="retired"))
    masked = row_with(fill)
    masked["identity"]["is_masked"] = True
    assert not due(masked)


def test_answer_due_with_accepted_configs() -> None:
    """D758: an accepted answer (an ``ok`` answer or a signal of an accepted digest) is settled and carried, under its
    own digest; a failure of an accepted digest is never accepted; another digest is not accepted."""
    acc = {"safety_relevance": ("old",)}
    old_ok = qrow("ok", None, verdict_="relevant", digest="old")
    old_signal = qrow("skipped", "not_needed", verdict_="relevant", evidence_id="e1", digest="old")
    assert not gate.answer_due(row_with(old_ok), QS, {}, NOW, askable=lambda _r: True, accepted=acc)
    assert not gate.answer_due(row_with(old_signal), QS, {}, NOW, askable=lambda _r: True, accepted=acc)
    assert gate.answer_due(row_with(qrow("failed_permanent", "schema_invalid", digest="old")), QS, {}, NOW,
                           askable=lambda _r: True, accepted=acc)
    assert gate.answer_due(row_with(qrow("ok", None, verdict_="relevant", digest="other")), QS, {}, NOW,
                           askable=lambda _r: True, accepted=acc)
    assert gate.answer_due(row_with(old_ok), QS, {}, NOW, askable=lambda _r: True)  # no list: due as before
    assert gate.carried_answers(row_with(old_ok), QS, acc) == {"safety_relevance": old_ok}
    assert gate.carried_answers(row_with(old_signal), QS, acc)["safety_relevance"]["status"] == "ok"
    assert gate.carried_answers(row_with(old_ok), QS) == {}


def test_signal_row_and_verdict_of() -> None:
    answer = qrow("ok", None, verdict_="borderline", evidence_id="e1")
    s = gate.signal_row(answer, "not_needed")
    assert s == {**answer, "status": "skipped", "error_class": "not_needed"}
    assert gate.is_signal(s) and gate.verdict_of(s) == "borderline" and gate.verdict_of(answer) == "borderline"
    failed = gate.signal_row(qrow("failed_transient", "http_5xx", evidence_id="e3"), "not_needed")
    assert (failed["verdict"], failed["evidence_id"], gate.is_signal(failed)) == (None, "e3", False)
    assert gate.verdict_of(qrow("skipped", "no_basis")) is None
    # a signal is carried as the ok answer it records; a plain fill is not carried
    assert gate.carried_answers(row_with(s), QS) == {"safety_relevance": {**s, "status": "ok", "error_class": None}}
    assert gate.carried_answers(row_with(qrow("skipped", "not_needed")), QS) == {}
    # a signal decides as its fill did (arm 6 here, no clause matched)
    cls_ = gate.clauses(Plan.model_validate({"plan": "p", "schema_version": 2,
                                             "gate": {"screen": [{**Q, "fetch_on": ["borderline"]}]}}).gate)
    assert gate.decide(cls_, (), {"safety_relevance": s}).arm == 6


# ---------------------------------------------------------------- the stage (store)
SQL_GATE = {"screen": [{"sql": f"doc_id = '{sid(1)}'"}, {**Q, "fetch_on": ["relevant"]}]}


def outcome(row: Row) -> tuple[Any, ...]:
    """What acquire and every later stage read of a triaged row (decision, status, error class,
    origin, wanted_by, state), and acquire's ``permit``."""
    a = ann(row)
    return (a["payload"]["decision"], a["status"], a["error_class"], a["origin"],
            tuple(a["payload"]["wanted_by"]), row["state"], permit(row, True))


def decision_inputs(row: Row) -> list[tuple[Any, ...]]:
    """The question rows as :func:`stack.gate.decide` reads them (status, error class, digest)."""
    return [(q["question"], q["status"], q["error_class"], q["config_digest"]) for q in ann(row)["payload"]["questions"]]


@pytest.mark.slow
def test_fetched_documents_are_asked_and_decide_as_before(tmp_path: Path, fake: FakeLlm,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment case. Run 1 asks as the code before D724 did (an asker that is sent only the
    question scope's rows, under the same asker name, so the triage config is the same): ``sid(1)``
    (``sql:`` fetch), ``sid(7)`` (acquired, standing ``fetch``) and ``sid(8)`` (acquired, standing
    ``skip``) keep the plain fill; ``sid(2)`` is decided by its answer. Run 2 (this code, nothing in the
    plan changed) finds the three due, asks them, and stores signals that contradict their decisions
    (``not_relevant`` on the two ``fetch`` rows, ``relevant`` on the ``skip``): every decision, state,
    origin, ``wanted_by`` and acquire ``permit`` is as after run 1, the question rows decide()
    reads are identical, X14 is green. Run 3 asks nothing. A prompt bump (a new pin name) then makes
    every document due again: all four are asked, the signals stay signals."""
    store = Store(tmp_path / "store")
    acquired_row(store, 7)
    acquired_row(store, 8)
    row8 = copy.deepcopy(store.docs()[sid(8)])  # an earlier LLM skip: the standing arm 7 carries
    t8 = row8["annotations"]["triage"]
    t8.update(status="ok", error_class=None, origin="ai_check")
    t8["payload"].update(decision="skip", wanted_by=[])
    write_documents_part(store, [row8], "triage", ["annotations"])
    store.add("sources.arxiv_sweep", [paper(1), paper(2)])
    store.gather()
    fake.on(title(1), verdict("not_relevant"))
    fake.on(title(2), verdict("relevant"))
    fake.on(title(7), verdict("not_relevant"))
    fake.on(title(8), verdict("relevant"))
    plan, path = plan_with(tmp_path, "s", SQL_GATE)
    signal_rows = {sid(1), sid(7), sid(8)}

    class QuestionScopeOnly:
        """The asker as the code before D724 used it: sent the question scope's rows only."""

        def __init__(self, real: Any) -> None:
            self.real, self.name, self.origin = real, real.name, real.origin

        def ask(self, rows: list[Row], qs: list[gate.Question], ctx: Any) -> gate.AskResult:
            return self.real.ask([r for r in rows if r["doc_id"] not in signal_rows], qs, ctx)

    monkeypatch.setattr(gate, "ASKER_HOOK", lambda p, line: QuestionScopeOnly(triage_ask.llm_asker(p, line)))
    run_triage(store, plan)
    before = store.docs()
    assert len(fake.requests) == 1 and fake.count(contains=title(2)) == 1
    for d in signal_rows:
        [q] = ann(before[d])["payload"]["questions"]
        assert (q["status"], q["error_class"], q["verdict"], q["evidence_id"]) == ("skipped", "not_needed", None, None)
    assert {d: outcome(r)[:5] for d, r in before.items()} == {
        sid(1): ("fetch", "ok", None, "rule", ("screen",)),
        sid(2): ("fetch", "ok", None, "ai_check", ("screen",)),
        sid(7): ("fetch", "ok", None, "rule", ("screen",)),
        sid(8): ("skip", "ok", None, "ai_check", ())}

    monkeypatch.setattr(gate, "ASKER_HOOK", None)  # this code's asker; the triage config is unchanged
    rec = run_triage(store, plan)
    after = store.docs()
    assert rec.config_digest == ann(before[sid(2)])["config_digest"]
    assert (rec.counts["scoped"], rec.counts["answers_due"], rec.counts["question_scope"]) == (3, 3, 0)
    assert (rec.counts["signal_pairs"], rec.counts["signals"], rec.counts["signal_not_attempted"]) == (3, 3, 0)
    assert len(fake.requests) == 4 and all(fake.count(contains=title(i)) == 1 for i in (1, 7, 8))
    assert rec.scope_complete is True
    for d in before:
        assert outcome(after[d]) == outcome(before[d]), d
        assert decision_inputs(after[d]) == decision_inputs(before[d]), d
    got = {d: gate.verdict_of(ann(after[d])["payload"]["questions"][0]) for d in after}
    assert got == {sid(1): "not_relevant", sid(2): "relevant", sid(7): "not_relevant", sid(8): "relevant"}
    assert all(gate.is_signal(ann(after[d])["payload"]["questions"][0]) for d in signal_rows)
    assert x14(store, path) == ("ok", [])

    again = run_triage(store, plan)
    assert (again.counts["scoped"], len(fake.requests)) == (0, 4) and again.scope_complete is True

    # a prompt bump: a new pin name moves the question digest and the asker name; every document is due
    plan2, path2 = plan_with(tmp_path, "s2", SQL_GATE, pin={"name": "safety-relevance-v2", "model": "openai/gpt-6-luna"})
    [q2] = gate.questions(plan2)
    rec2 = run_triage(store, plan2)
    bumped = store.docs()
    assert (rec2.counts["scoped"], rec2.counts["asked_pairs"], rec2.counts["signal_pairs"]) == (4, 4, 3)
    assert len(fake.requests) == 8 and all(fake.count(contains=title(i)) == 2 for i in (1, 2, 7, 8))
    for d, r in bumped.items():
        [q] = ann(r)["payload"]["questions"]
        assert q["config_digest"] == q2.config_digest and gate.verdict_of(q) == got[d]
        assert outcome(r) == outcome(before[d]), d
        assert gate.is_signal(q) == (d in signal_rows)
    assert x14(store, path2) == ("ok", [])


# ---------------------------------------------------------------- accepted configs (D758)
def _accept(plan: Plan, target: str, configs: list[str], *, name: str = "safety-relevance-v1") -> Plan:
    """``plan`` with its triage line naming ``target`` for safety_relevance and accepting ``configs``, validated."""
    from stack.plan import StageLine

    lines = [StageLine.model_validate({**s.model_dump(mode="json", by_alias=True), "target": {"safety_relevance": target},
                                       "accepted": [{"question": "safety_relevance", "config": c, "name": name,
                                                     "decision": "D1"} for c in configs]})
             if s.stage == "triage" else s for s in plan.stages]
    return Plan.model_validate({**plan.model_dump(mode="json", by_alias=True), "stages": [
        s.model_dump(mode="json", by_alias=True) for s in lines]})


@pytest.mark.slow
def test_accepted_triage_answers(tmp_path: Path, fake: FakeLlm) -> None:
    """A question digest move (the pin names its template digest: the same request) with an accepted list asks
    nothing, keeps every QUESTION row under its own digest and decides as before; a gate edit then re-decides
    every row from the accepted answers with zero calls. Without the list every row is asked again."""
    from stack.plan import selection_digest
    from tests.test_triage_questions import drop_cache

    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1), paper(2)])
    store.gather()
    fake.on(title(1), verdict("relevant"))
    fake.on(title(2), verdict("not_relevant"))
    gate_ = {"screen": [{**Q, "fetch_on": ["relevant"]}]}
    pa_, _ = plan_with(tmp_path, "a", gate_)
    run_triage(store, pa_)
    before, (qa,) = store.docs(), gate.questions(pa_)
    assert len(fake.requests) == 2
    digest = triage_ask.resolve(qa, annotate_stage.stack_config().llm).template_digest
    pin = {"name": "safety-relevance-v1", "model": "openai/gpt-6-luna", "template_digest": digest}
    pb, _ = plan_with(tmp_path, "b", gate_, pin=pin)
    (qb,) = gate.questions(pb)
    assert qb.config_digest != qa.config_digest
    drop_cache(store)

    rec = run_triage(store, _accept(pb, qb.config_digest, [qa.config_digest]))
    assert len(fake.requests) == 2 and rec.counts["scoped"] == 0
    entry = {"question": "safety_relevance", "config": qa.config_digest, "name": "safety-relevance-v1", "decision": "D1"}
    target = {"safety_relevance": qb.config_digest}
    assert rec.selection == {"digest": selection_digest(target, [entry]), "target": target, "accepted": [entry]}
    assert decisions(store) == {d: (ann(r)["payload"]["decision"], ann(r)["status"], ann(r)["error_class"],
                                    ann(r)["origin"], tuple(ann(r)["payload"]["wanted_by"])) for d, r in before.items()}

    # a gate edit moves the triage config: every row is re-decided from its accepted answer, no call
    gate2 = {"screen": [{**Q, "fetch_on": ["relevant", "borderline"]}]}
    pb2, path2 = plan_with(tmp_path, "b2", gate2, pin=pin)
    rec = run_triage(store, _accept(pb2, qb.config_digest, [qa.config_digest]))
    after = store.docs()
    assert len(fake.requests) == 2 and rec.counts["scoped"] == 2 and rec.counts["carried.accepted"] == 2
    assert rec.counts["asked_pairs"] == 0
    assert {d: ann(r)["payload"]["decision"] for d, r in after.items()} == {sid(1): "fetch", sid(2): "skip"}
    assert {q["config_digest"] for r in after.values() for q in ann(r)["payload"]["questions"]} == {qa.config_digest}
    assert x14(store, path2) == ("ok", [])

    # without the list the moved question digest re-asks every row still under qa
    plain = run_triage(store, plan_with(tmp_path, "b5", gate2, pin=pin)[0])
    assert plain.counts["asked_pairs"] == 2 and plain.selection is not None and plain.selection["accepted"] == []


@pytest.mark.slow
def test_triage_target_and_accepted_configs_are_checked_at_stage_start(tmp_path: Path, fake: FakeLlm) -> None:
    from tests.synth.records import run_line

    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1)])
    store.gather()
    fake.on(title(1), verdict("relevant"))
    gate_ = {"screen": [{**Q, "fetch_on": ["relevant"]}]}
    pa_, _ = plan_with(tmp_path, "a", gate_)
    run_triage(store, pa_)
    (qa,) = gate.questions(pa_)
    digest = triage_ask.resolve(qa, annotate_stage.stack_config().llm).template_digest
    pb, _ = plan_with(tmp_path, "b", gate_, pin={"name": "safety-relevance-v1", "model": "openai/gpt-6-luna",
                                                 "template_digest": digest})
    (qb,) = gate.questions(pb)
    for p, why in ((_accept(pb, qb.config_digest, ["e" * 64]), "is no config of the prior triage_evidence"),
                   (_accept(pb, "f" * 64, [qa.config_digest]), "is not the question's digest"),
                   (_accept(pb, qb.config_digest, [qa.config_digest], name="x"),
                    "name is not a prompt name of its evidence (['safety-relevance-v1'])")):
        rec = run_line(store.root, p, "triage")
        assert rec is not None and rec.status == "failed" and why in (rec.error or ""), rec and rec.error
