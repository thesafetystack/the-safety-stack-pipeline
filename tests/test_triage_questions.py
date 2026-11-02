"""Triage questions with the LLM asker (T4.2; §3.1 gate table, A.2, A.5; D36, D37): the real
``triage`` stage over a synthetic ``documents`` chain, the asker built by
:func:`stack.gate.asker_for` (``workflows/triage/ask.py``), the fake model scripted per row.

Arms 1-5 and 7 from questions (arm 7 = a row outside the question scope, acquired with its primary
role ``ok``, keeps its earlier rule ``fetch``: the carried standing, T5.5), ``wanted_by`` over two consumers sharing one question (asked once), the
``triage_evidence`` rows, an in-band excerpt as the only basis, a rerun with the earlier
``triage_evidence`` as its prior (``--in``) that makes
zero transport calls with identical decisions, a due ``failed_transient`` retried with ``attempts
2`` after its backoff, a budget refusal (``not_attempted[budget]``, nothing sent) as a planned stop
re-asked by the next runs, the prior evidence carried by a run that asks nothing, pin refusals,
X14/X16 green on the result store; X16's rule against the check fixtures (one failing). The test
session's no-network default (D160 (C)) is guarded in ``tests/test_conftest_guards.py``.

The fake transport goes in through :data:`stack.annotate.stage.TRANSPORT_HOOK` (the asker reuses
the annotate seams); ``OPENROUTER_API_KEY`` is unset and the ledger is a temp file.
"""

from __future__ import annotations

import copy
import datetime as dt
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from stack import gate
from stack.annotate import stage as annotate_stage
from stack.annotate.basis import work_digest
from stack.annotate.evidence import result_digest
from stack.checks.verify import run_checks
from stack.plan import Plan, load_plan
from stack.runs import RunRecord
from stack.store.blobs import BlobStore, open_blobs
from stack.store.chain import head_of
from tests.fakes.llm import FakeLlm, malformed, ok, prompt_text, server_error
from tests.fixtures.checks import build_store, clean_world, write_world
from tests.synth.records import run_line, sdir
from tests.synth.rows import _content, blob, dg, rid, sid
from tests.test_gather_identity import DEV_PLAN, Store
from tests.test_gather_merge import write_documents_part
from tests.test_triage_rules import ann, assert_green, fid, output, paper
from workflows.triage import ask as triage_ask

Row = dict[str, Any]
Q = {"question": "safety_relevance", "prompt": "triage.safety-relevance-v1"}
GATE = {"screen": [{**Q, "fetch_on": ["relevant"]}],
        "review": [{**Q, "fetch_on": ["relevant", "borderline"]}]}
NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)
EXCERPT = "Excerpt six: we red-team a deployed refusal classifier and report its failure modes."


def verdict(v: str, *, cost: float = 0.001, confidence: float = 0.8) -> Any:
    return ok({"verdict": v, "confidence": confidence, "reasoning": f"because {v}"}, cost=cost)


def title(i: int) -> str:
    return f"Title: Paper {i}\n"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeLlm:
    f = FakeLlm()
    clock = {"now": NOW}
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # no network path, whatever happens
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(gate, "ASKER_HOOK", None)  # the real wiring: asker_for builds the LLM asker
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: f)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: clock["now"])
    f.clock = clock  # type: ignore[attr-defined]
    return f


def plan_with(tmp: Path, name: str, gate_: dict[str, Any], *, budget: float | None = None,
              pin: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> tuple[Plan, Path]:
    """The dev plan with ``gate_`` (and optionally the triage line's ``budget.max_usd`` and the
    question's pin), written to ``tmp/<name>.yaml``."""
    data = yaml.safe_load(DEV_PLAN.read_text())
    data["gate"] = gate_
    if budget is not None:
        for line in data["stages"]:
            if line["stage"] == "triage":
                line["budget"] = {"max_usd": budget}
    if options is not None:
        for line in data["stages"]:
            if line["stage"] == "triage":
                line["options"] = options
    if pin is not None:
        data["prompts"]["triage.safety-relevance-v1"] = pin
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_plan(path), path


def run_triage(store: Store, plan: Plan, *, no_prior: bool = False) -> RunRecord:
    """The triage line's run: it reads ``documents`` and the ``triage_evidence`` series (the prior
    evidence is the directory's last file); ``no_prior``: documents only, as a run that never saw
    the evidence (a ``restart`` full into the non-empty evidence directory)."""
    rec = (run_line(store.root, plan, "triage", ins=[sdir("documents")], restart=True) if no_prior
           else run_line(store.root, plan, "triage"))
    assert rec is not None and rec.status == "ok", rec and rec.error
    return rec


def drop_cache(store: Store) -> None:
    """Remove the response cache, so a call that does not happen is the asker's doing."""
    shutil.rmtree(store.root / "cache" / "llm-responses", ignore_errors=True)


def decisions(store: Store) -> dict[str, tuple[Any, ...]]:
    return {d: (ann(r)["payload"]["decision"], ann(r)["status"], ann(r)["error_class"], ann(r)["origin"],
                tuple(ann(r)["payload"]["wanted_by"]))
            for d, r in store.docs().items()}


def excerpt_paper(store: Store, i: int) -> Row:
    """``sid(i)`` with no abstract, its only basis an in-band excerpt whose text blob is stored."""
    digest = open_blobs(store.root).put(EXCERPT.encode())
    row = paper(i, abstract=None, inband=True)
    row["inband"]["entries"][0]["text"] = blob(digest, len(EXCERPT.encode()), "text/markdown")
    return row


def acquired_row(store: Store, i: int) -> None:
    """Gather + rule-triage ``sid(i)`` (dev gate: ``fetch``), then an acquire part with its
    ``full_text`` role ``ok``: outside any later question scope (arm 7 carries its ``fetch``)."""
    store.add("sources.arxiv_sweep", [paper(i)])
    store.gather()
    rec = run_line(store.root, store.plan, "triage")
    assert rec is not None and rec.status == "ok" and ann(store.docs()[sid(i)])["payload"]["decision"] == "fetch"
    row = copy.deepcopy(store.docs()[sid(i)])
    content = _content(i, 600)
    content["members_digest"] = row["identity"]["members_digest"]
    row["content"], row["state"] = content, "acquired"
    write_documents_part(store, [row], "acquire", ["content"])


# ---------------------------------------------------------------- arms 1-6, evidence, prior evidence
@pytest.mark.slow
def test_question_arms_evidence_and_prior_rerun(tmp_path: Path, fake: FakeLlm) -> None:
    store = Store(tmp_path / "store")
    acquired_row(store, 7)
    store.add("sources.arxiv_sweep", [paper(1), paper(2), paper(3, abstract=None), paper(4), paper(5),
                                      excerpt_paper(store, 6), paper(8)])
    store.gather()
    fake.on(title(1), verdict("relevant", confidence=0.9))
    fake.on(title(2), verdict("not_relevant"))
    fake.on(title(4), malformed())                                  # a prose refusal, 3 schema attempts
    fake.on(title(5), [server_error()] * 8 + [verdict("relevant")])  # two calls of 4 × 5xx, then (later) ok
    fake.on(EXCERPT, verdict("relevant"))
    fake.on(title(8), verdict("borderline"))
    fake.on(title(7), verdict("not_relevant"))  # D724: asked outside the question scope, a signal
    plan, path = plan_with(tmp_path, "q", GATE)
    ev0 = head_of(store.root, sdir("triage_evidence").rstrip("/"))  # acquired_row's (empty) triage evidence
    rec = run_triage(store, plan)

    both = ("review", "screen")
    want = {sid(1): ("fetch", "ok", None, "ai_check", both),
            sid(2): ("skip", "ok", None, "ai_check", ()),
            sid(3): ("pending", "skipped", "no_basis", "rule", ()),
            sid(4): ("pending", "skipped", "gate_failed", "rule", ()),
            sid(5): ("pending", "failed_transient", "http_5xx", "rule", ()),
            sid(6): ("fetch", "ok", None, "ai_check", both),
            sid(7): ("fetch", "ok", None, "rule", ("screen",)),  # arm 7 (was arm 6 before T5.5)
            sid(8): ("fetch", "ok", None, "ai_check", ("review",))}
    assert decisions(store) == want
    assert [rec.counts[f"arm{i}"] for i in range(1, 8)] == [3, 1, 1, 1, 1, 0, 1]
    assert rec.scope_complete is True and rec.counts["question_scope"] == 7
    assert (rec.counts["signal_pairs"], rec.counts["signals"]) == (1, 1)  # D724: sid(7)
    docs = store.docs()
    assert docs[sid(7)]["state"] == "acquired" and docs[sid(3)]["state"] == "triaged"
    # One question, two consumers: asked once per row. 1+1+3+4+1+1 transport calls, and sid(7)'s
    # signal (D724: asked although its primary role is ok; its not_relevant decides nothing).
    assert fake.count(contains=title(1)) == 1 and fake.count(contains=EXCERPT) == 1
    assert fake.count(contains=title(4)) == 3 and fake.count(contains=title(5)) == 4
    assert fake.count(contains=title(7)) == 1
    assert len(fake.requests) == 12 and fake.count(contains=title(3)) == 0

    ev, eh = output(store, rec, "triage_evidence")
    by_doc = {e["doc_id"]: e for e in ev}
    assert sorted(by_doc) == [sid(i) for i in (1, 2, 4, 5, 6, 7, 8)] and len(ev) == 7
    assert (eh["kind"], eh["base"]) == ("full", ev0)  # evidence is materialised full, on the prior file
    prompt = triage_ask.resolve(gate.questions(plan)[0], annotate_stage.stack_config().llm)
    for d, e in by_doc.items():
        q = next(a for a in ann(docs[d])["payload"]["questions"])
        assert q["evidence_id"] == e["evidence_id"] and q["config_digest"] == e["config_digest"]
        assert e["work_digest"] == work_digest(d, docs[d]["metadata"]["fields_digest"], e["config_digest"])
        assert (e["question"], e["basis_kind"], e["origin"], e["item_key"]) == ("safety_relevance", "fields", "ai_check", "")
        assert e["basis_digest"] == docs[d]["metadata"]["fields_digest"]
    for d in (sid(1), sid(2), sid(6), sid(7), sid(8)):
        e = by_doc[d]
        assert (e["status"], e["prompt_name"], e["model"]) == ("ok", "safety-relevance-v1", "openai/gpt-6-luna")
        assert e["template_digest"] == prompt.template_digest and e["reasoning"].startswith("because ")
        assert e["result_digest"] == result_digest({k: e[k] for k in ("question", "verdict", "confidence", "reasoning")})
    assert (by_doc[sid(1)]["verdict"], by_doc[sid(1)]["confidence"]) == ("relevant", 0.9)
    e4, e5 = by_doc[sid(4)], by_doc[sid(5)]
    assert (e4["status"], e4["error_class"], e4["verdict"], e4["result_digest"]) == ("failed_permanent", "schema_invalid", None, None)
    assert (e5["status"], e5["error_class"], e5["attempts"]) == ("failed_transient", "http_5xx", 1)
    assert e5["retry_after"] == NOW  # the first retry is due at once (round 4, passes)
    q5 = ann(docs[sid(5)])["payload"]["questions"][0]
    assert (q5["status"], q5["error_class"], q5["evidence_id"]) == ("failed_transient", "http_5xx", e5["evidence_id"])
    q3, q7 = ann(docs[sid(3)])["payload"]["questions"][0], ann(docs[sid(7)])["payload"]["questions"][0]
    assert (q3["status"], q3["error_class"], q3["evidence_id"]) == ("skipped", "no_basis", None)
    # D724: the signal is the fill with the answer's verdict, confidence and evidence id
    assert (q7["status"], q7["error_class"], q7["verdict"], q7["evidence_id"]) == (
        "skipped", "not_needed", "not_relevant", by_doc[sid(7)]["evidence_id"])
    assert gate.verdict_of(q7) == "not_relevant" and gate.is_signal(q7)
    build = {"plan": str(path), "files": {"documents": {"file_id": fid(store)}}}
    assert_green(store.root, build)
    x16 = {r.check: r.status for r in run_checks(store.root, names=["lookup_gate_current"], build=build)}
    assert x16 == {"lookup_gate_current": "ok"}

    # The first retry is due at once (round 4, passes): a minute later the next run asks sid(5)
    # again on the prior chain; it fails again (attempt 2, superseding), so it backs off 2 h.
    fake.clock["now"] = NOW + dt.timedelta(minutes=1)  # type: ignore[attr-defined]
    rec1b = run_triage(store, plan)
    assert len(fake.requests) == 16 and rec1b.counts["questions_due"] == 1
    ev1b, _ = output(store, rec1b, "triage_evidence")
    e5b = next(e for e in ev1b if e["doc_id"] == sid(5) and e["attempts"] == 2)
    assert (e5b["status"], e5b["supersedes"], e5b["retry_after"]) == (
        "failed_transient", e5["evidence_id"], NOW + dt.timedelta(minutes=1, hours=2))
    assert decisions(store) == want

    # A gate edit puts every row back in scope; the final answers carry from the annotations, and
    # sid(5)'s transient failure (not due) carries from the prior evidence: zero calls.
    drop_cache(store)
    plan2, _ = plan_with(tmp_path, "q2", {**GATE, "noop": [{"sql": "FALSE"}]})
    rec2 = run_triage(store, plan2)
    assert len(fake.requests) == 16
    assert rec2.counts["scoped"] == 8 and rec2.counts["ask.carried_evidence"] == 1
    assert rec2.counts["signal_pairs"] == 0  # sid(7)'s signal carries from its annotation
    assert decisions(store) == want
    ev2, _ = output(store, rec2, "triage_evidence")
    key = lambda e: e["evidence_id"]
    assert sorted(ev2, key=key) == sorted(ev1b, key=key)  # cumulative: the prior rows, nothing new

    # Three hours on, sid(5) is due: asked again on the prior chain, attempts 3, superseding.
    fake.clock["now"] = NOW + dt.timedelta(hours=3)  # type: ignore[attr-defined]
    plan3, _ = plan_with(tmp_path, "q3", {**GATE, "noop": [{"sql": "doc_id IS NULL"}]})
    rec3 = run_triage(store, plan3)
    assert len(fake.requests) == 17 and rec3.counts["ask.asked"] == 1
    assert decisions(store)[sid(5)] == ("fetch", "ok", None, "ai_check", both)
    ev3, _ = output(store, rec3, "triage_evidence")
    retry = next(e for e in ev3 if e["doc_id"] == sid(5) and e["status"] == "ok")
    assert (retry["attempts"], retry["supersedes"], retry["first_attempt_at"]) == (3, e5b["evidence_id"], NOW)
    assert len(ev3) == 9


@pytest.mark.slow
def test_fields_reverted_reuses_prior_evidence_with_zero_calls(tmp_path: Path, fake: FakeLlm) -> None:
    """A row whose fields change and change back is out of its annotation's carry (basis changed
    twice) but its first answer's work key is current again: the prior evidence reuses it, no call."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1)])
    store.gather()
    fake.on("An abstract", verdict("not_relevant"))
    fake.on("A changed abstract", verdict("relevant"))
    plan, _ = plan_with(tmp_path, "q", GATE)
    rec1 = run_triage(store, plan)
    first = decisions(store)
    store.add("sources.arxiv_sweep", [paper(1, abstract="A changed abstract", t=30)])
    store.gather()
    run_triage(store, plan)
    assert decisions(store)[sid(1)][0] == "fetch" and len(fake.requests) == 2
    store.add("sources.arxiv_sweep", [paper(1, abstract="An abstract", t=40)])
    store.gather()
    drop_cache(store)
    rec3 = run_triage(store, plan)
    assert len(fake.requests) == 2 and rec3.counts["ask.carried_evidence"] == 1
    assert decisions(store) == first
    q = ann(store.docs()[sid(1)])["payload"]["questions"][0]
    ev1, _ = output(store, rec1, "triage_evidence")
    assert q["evidence_id"] == ev1[0]["evidence_id"]


# ---------------------------------------------------------------- budget
@pytest.mark.slow
def test_budget_refusal_is_not_attempted_budget_and_nothing_is_sent(tmp_path: Path, fake: FakeLlm) -> None:
    """The first answer costs $0.01 against a $0.0101 stage budget: every later reservation
    (≥ max_tokens × the pinned model's output price alone: 512 × $0.50/1M for gpt-6-luna) is refused
    before the request."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 5)])
    store.gather()
    fake.default = verdict("relevant", cost=0.01)
    plan, _ = plan_with(tmp_path, "b", GATE, budget=0.0101, options={"workers": 1})  # sequential (default 64, D692)
    rec = run_triage(store, plan)
    got = decisions(store)
    assert got[sid(1)][0] == "fetch" and len(fake.requests) == 1
    for i in (2, 3, 4):
        assert got[sid(i)] == ("pending", "not_attempted", "budget", "rule", ())
    assert rec.counts["ask.not_attempted.budget"] == 3 and rec.counts["arm5"] == 3
    ev, _ = output(store, rec, "triage_evidence")
    assert [e["doc_id"] for e in ev] == [sid(1)]  # refused pairs leave no evidence


@pytest.mark.slow
def test_budget_cut_is_a_planned_stop_and_the_next_runs_ask_the_rest(tmp_path: Path, fake: FakeLlm) -> None:
    """D159(1)/(3), D161 (C); §3.3 "planned stops finish ok with scope_complete = false": a
    budget cut ends the run ``ok`` with ``not_attempted[budget]`` rows and ``scope_complete =
    false``; the next run of the **same** plan (no gate, plan or fields change) puts those rows
    back in scope as questions due and asks them, one per run under this budget, until complete."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 4)])
    store.gather()
    fake.default = verdict("relevant", cost=0.01)
    plan, _ = plan_with(tmp_path, "b", GATE, budget=0.0101, options={"workers": 1})  # sequential (default 64, D692)
    rec = run_triage(store, plan)  # asserts status ok
    assert rec.scope_complete is False and rec.counts["not_attempted"] == 2 and len(fake.requests) == 1
    for n_due, n_calls, complete in ((2, 2, False), (1, 3, True)):
        rec = run_triage(store, plan)
        assert (rec.counts["questions_due"], rec.counts["scoped"]) == (n_due, n_due)
        assert len(fake.requests) == n_calls and rec.scope_complete is complete
    assert {d[0] for d in decisions(store).values()} == {"fetch"}
    ev, _ = output(store, rec, "triage_evidence")
    assert sorted(e["doc_id"] for e in ev) == [sid(1), sid(2), sid(3)]  # cumulative over the three runs
    # nothing left: nothing in scope, no call, and the evidence is carried, not emptied
    last = run_triage(store, plan)
    assert (last.counts["scoped"], last.counts["questions_due"], len(fake.requests)) == (0, 0, 3)
    assert last.scope_complete is True and output(store, last, "triage_evidence")[0] == ev


@pytest.mark.slow
def test_a_carried_not_attempted_pair_outside_the_question_scope_is_settled(tmp_path: Path, fake: FakeLlm) -> None:
    """D697: a budget cut leaves ``sid(2)``, ``sid(3)`` ``not_attempted[budget]``; before the next
    triage ``sid(2)``'s primary role turns ``ok`` (acquire's in-band role, whatever the gate said), so
    it leaves the question scope and nothing asks it. Its carried answer is not carried: the row
    settles on the fill (arm 6, no standing), the run is complete once ``sid(3)`` is asked, and no
    later run finds it due. Before, the carried pair kept the row due and every run incomplete
    (``scope_complete = false``, reason ``unspecified``). Under ``--only select`` a carried pair in
    the question scope counts ``not_attempted.carried`` (reason ``not_run``).

    D724: outside the question scope the pair is asked again, as a signal, after the question
    scope's ``sid(3)``; the budget cuts it (``signal_not_attempted``), which leaves the run complete;
    the next run finds ``sid(2)`` due (:func:`stack.gate.answer_due`) and asks it; its ``relevant``
    signal leaves the decision at arm 6."""
    from stack.build import incomplete_reasons

    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 4)])
    store.gather()
    fake.default = verdict("relevant", cost=0.01)
    plan, _ = plan_with(tmp_path, "b", GATE, budget=0.0101, options={"workers": 1})
    rec = run_triage(store, plan)
    assert rec.counts["ask.not_attempted.budget"] == 2 and len(fake.requests) == 1
    # a fetch_on edit re-scopes every row (new triage config_digest) and keeps the question digest
    plan, _ = plan_with(tmp_path, "b2", {**GATE, "review": [{**Q, "fetch_on": ["relevant"]}]}, budget=0.0101,
                        options={"workers": 1})
    sel = run_line(store.root, plan, "triage", only=["select"])  # sid(2), sid(3): carried, not asked
    assert sel is not None and sel.scope_complete is False
    assert (sel.counts["not_attempted"], sel.counts["not_attempted.carried"]) == (2, 2)
    assert incomplete_reasons(sel.counts) == ["not_run"]
    row = copy.deepcopy(store.docs()[sid(2)])
    content = _content(2, 600)
    content["members_digest"] = row["identity"]["members_digest"]
    row["content"], row["state"] = content, "acquired"
    write_documents_part(store, [row], "acquire", ["content"])
    rec = run_triage(store, plan)
    assert (rec.counts["questions_due"], rec.counts["question_scope"]) == (2, 1)
    assert "not_attempted.carried" not in rec.counts
    assert rec.counts["settled_out_of_question_scope"] == 1 and len(fake.requests) == 2
    got = decisions(store)
    assert got[sid(2)] == ("pending", "skipped", "not_needed", "rule", ())
    assert got[sid(3)][0] == "fetch"
    assert rec.scope_complete is True and rec.counts["not_attempted"] == 0
    assert (rec.counts["signal_pairs"], rec.counts["signal_not_attempted"]) == (1, 1)  # D724
    assert fake.count(contains=title(3)) == 1 and fake.count(contains=title(2)) == 0  # question scope first
    [q2] = ann(store.docs()[sid(2)])["payload"]["questions"]
    assert (q2["status"], q2["error_class"], q2["verdict"]) == ("skipped", "not_needed", None)
    sig = run_triage(store, plan)
    assert (sig.counts["scoped"], sig.counts["answers_due"], sig.counts["questions_due"]) == (1, 1, 0)
    assert (sig.counts["signal_pairs"], sig.counts["signals"], len(fake.requests)) == (1, 1, 3)
    assert decisions(store)[sid(2)] == ("pending", "skipped", "not_needed", "rule", ())
    [q2] = ann(store.docs()[sid(2)])["payload"]["questions"]
    assert (q2["status"], q2["error_class"], q2["verdict"]) == ("skipped", "not_needed", "relevant")
    last = run_triage(store, plan)
    assert (last.counts["scoped"], last.counts["questions_due"], len(fake.requests)) == (0, 0, 3)
    assert last.scope_complete is True


@pytest.mark.slow
def test_a_transient_whose_evidence_is_missing_is_due_again(tmp_path: Path, fake: FakeLlm) -> None:
    """R2-1 (round 5): a ``failed_transient`` answer whose evidence is not in the prior (the run that
    wrote it never reached a prior: its build failed, or a stand-alone run without the ``triage_evidence`` ``--in``) is
    due, asked as a first attempt (``attempts`` restart, nothing superseded); from its new evidence
    on the backoff holds again. Before, the row stayed ``pending`` for good."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1), paper(5)])
    store.gather()
    fake.on(title(1), verdict("not_relevant"))
    fake.on(title(5), [server_error()] * 8 + [verdict("relevant")])
    plan, _ = plan_with(tmp_path, "t", GATE)
    run_triage(store, plan)  # its evidence file is stranded: no later run reads it
    assert len(fake.requests) == 5 and decisions(store)[sid(5)][1:3] == ("failed_transient", "http_5xx")
    # no prior (as after a failed build): due anyway, asked as attempt 1, fails again
    rec2 = run_triage(store, plan, no_prior=True)
    assert (rec2.counts["scoped"], rec2.counts["questions_due"], len(fake.requests)) == (1, 1, 9)
    (e5,) = [e for e in output(store, rec2, "triage_evidence")[0] if e["doc_id"] == sid(5)]
    assert (e5["attempts"], e5["supersedes"], e5["status"]) == (1, None, "failed_transient")
    # with that evidence as the prior: the retry rule again (first retry due at once), and it lands
    rec3 = run_triage(store, plan)
    assert (rec3.counts["questions_due"], len(fake.requests)) == (1, 10)
    assert decisions(store)[sid(5)][:2] == ("fetch", "ok") and rec3.scope_complete is True
    ok5 = next(e for e in output(store, rec3, "triage_evidence")[0] if e["doc_id"] == sid(5) and e["status"] == "ok")
    assert (ok5["attempts"], ok5["supersedes"]) == (2, e5["evidence_id"])


@pytest.mark.slow
def test_due_transient_is_reasked_without_a_gate_edit_and_evidence_carries(tmp_path: Path,
                                                                          fake: FakeLlm) -> None:
    """D159(2)/(3), D161 (C): a run that asks nothing writes the prior evidence unchanged
    (also under ``--only select``), so the history survives a quiet run; a ``failed_transient``
    question is re-asked once its backoff passed (the first retry at once, round 4 passes; the
    second after 2 h), with no gate edit, superseding its evidence. (A transient whose evidence is
    not in the prior: :func:`test_a_transient_whose_evidence_is_missing_is_due_again`.)"""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1), paper(5)])
    store.gather()
    fake.on(title(1), verdict("not_relevant"))
    fake.on(title(5), [server_error()] * 8 + [verdict("relevant")])
    plan, _ = plan_with(tmp_path, "t", GATE)
    rec1 = run_triage(store, plan)
    assert len(fake.requests) == 5 and decisions(store)[sid(5)][1:3] == ("failed_transient", "http_5xx")
    ev1, _ = output(store, rec1, "triage_evidence")
    e5 = next(e for e in ev1 if e["doc_id"] == sid(5))
    assert e5["retry_after"] == NOW
    # due at once: the next run (two minutes on) asks again, fails again (attempts 2, due 2 h later)
    fake.clock["now"] = NOW + dt.timedelta(minutes=2)  # type: ignore[attr-defined]
    rec1b = run_triage(store, plan)
    assert (rec1b.counts["questions_due"], len(fake.requests)) == (1, 9)
    ev1b, _ = output(store, rec1b, "triage_evidence")
    e5b = next(e for e in ev1b if e["doc_id"] == sid(5) and e["attempts"] == 2)
    assert (e5b["supersedes"], e5b["retry_after"]) == (e5["evidence_id"], NOW + dt.timedelta(minutes=2, hours=2))
    # not due yet: nothing in scope, nothing asked, the prior rows written unchanged
    rec2 = run_triage(store, plan)
    assert (rec2.counts["scoped"], len(fake.requests)) == (0, 9)
    assert output(store, rec2, "triage_evidence")[0] == ev1b
    rec2s = run_line(store.root, plan, "triage", only=["select"])
    assert rec2s is not None and rec2s.status == "ok" and output(store, rec2s, "triage_evidence")[0] == ev1b
    # three hours on: due on the carried chain, asked again (attempts 3), no gate edit
    fake.clock["now"] = NOW + dt.timedelta(hours=3)  # type: ignore[attr-defined]
    rec3 = run_triage(store, plan)
    assert (rec3.counts["questions_due"], rec3.counts["ask.asked"], len(fake.requests)) == (1, 1, 10)
    assert decisions(store)[sid(5)][:2] == ("fetch", "ok") and rec3.scope_complete is True
    retry = next(e for e in output(store, rec3, "triage_evidence")[0]
                 if e["doc_id"] == sid(5) and e["status"] == "ok")
    assert (retry["attempts"], retry["supersedes"]) == (3, e5b["evidence_id"])


@pytest.mark.slow
def test_max_rows_leaves_the_rest_not_attempted_limit(tmp_path: Path, fake: FakeLlm) -> None:
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 4)])
    store.gather()
    fake.default = verdict("not_relevant")
    plan, _ = plan_with(tmp_path, "l", GATE, options={"max_rows": 2})
    rec = run_triage(store, plan)
    got = decisions(store)
    assert len(fake.requests) == 2 and rec.scope_complete is False and rec.counts["ask.limit"] == 1
    assert got[sid(1)][0] == got[sid(2)][0] == "skip"
    assert got[sid(3)] == ("pending", "not_attempted", "limit", "rule", ())


# ---------------------------------------------------------------- pins
@pytest.mark.parametrize(("pin", "match"), [
    ({"name": "safety-relevance-v1", "modle": "x"}, "keys"),
    ({"model": "openai/gpt-6-luna"}, "needs a `name`"),
    ({"name": "no-such-prompt"}, "no template"),
    ({"name": "safety-relevance-v1", "role": "no_role"}, "role"),
    ({"name": "safety-relevance-v1", "template_digest": dg("other")}, "template_digest"),
    (None, "max_rows"),
])
def test_bad_pin_is_refused_before_any_call(tmp_path: Path, fake: FakeLlm, pin: dict[str, Any] | None,
                                            match: str) -> None:
    plan, _ = plan_with(tmp_path, "p", GATE, pin=pin, options={"max_rows": -1} if pin is None else None)
    with pytest.raises(gate.GateError, match=match):
        gate.asker_for(plan, plan.stage_line("triage"))
    assert fake.requests == []


def test_asker_for_wiring(tmp_path: Path, fake: FakeLlm, monkeypatch: pytest.MonkeyPatch) -> None:
    sql_only = load_plan(DEV_PLAN)
    assert gate.asker_for(sql_only, sql_only.stage_line("triage")) is None
    plan, _ = plan_with(tmp_path, "w", GATE, budget=0.5)
    a = gate.asker_for(plan, plan.stage_line("triage"))
    assert isinstance(a, triage_ask.LlmAsker) and a.origin == "ai_check" and a.budget_usd == 0.5
    assert a.name.startswith("llm:") and len(a.name) == 20
    other, _ = plan_with(tmp_path, "m", GATE, pin={"name": "safety-relevance-v1", "model": "openai/gpt-5.2"})
    assert gate.asker_for(other, other.stage_line("triage")).name != a.name  # type: ignore[union-attr]
    monkeypatch.setattr(gate, "ASKER_HOOK", lambda _p, _l: None)  # a hook wins, also a None one
    assert gate.asker_for(plan, plan.stage_line("triage")) is None


def test_classify_1k_role_raises_the_output_cap(tmp_path: Path, fake: FakeLlm) -> None:
    """D390 (e2e-fix): a pin naming ``role: classify_1k`` asks gpt-6-luna with a 1,024-token output cap; the
    default role ``classify`` stays at 512, so a plan that does not name the new role keeps its asker name
    (and the triage ``config_digest`` with it)."""
    llm = annotate_stage.stack_config().llm
    plan, _ = plan_with(tmp_path, "k", GATE, pin={"name": "safety-relevance-v1", "role": "classify_1k"})
    p = triage_ask.resolve(gate.questions(plan)[0], llm)
    assert (p.role, p.model, p.max_tokens) == ("classify_1k", "openai/gpt-6-luna", 1024)
    default, _ = plan_with(tmp_path, "d", GATE)
    d = triage_ask.resolve(gate.questions(default)[0], llm)
    assert (d.role, d.model, d.max_tokens) == ("classify", "openai/gpt-6-luna", 512)
    assert triage_ask.asker_name([p]) != triage_ask.asker_name([d])


@pytest.mark.slow
def test_excerpt_blobs_are_read_in_parallel_and_kept_capped(tmp_path: Path, fake: FakeLlm,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-94: the queue's excerpt blobs are read on a pool before the first call (a fresh container
    downloads each), not one by one: two reads must be in flight together (a barrier of 2 that a
    serial read breaks after its timeout). Only ``MAX_EXCERPT_CHARS + 1`` characters are kept; the
    message is the one the whole text gives."""
    store = Store(tmp_path / "store")
    long_text = "Excerpt long: " + "a red-team study " * 400
    texts = {6: EXCERPT, 9: long_text}
    rows = []
    for i, text in texts.items():
        row = paper(i, abstract=None, inband=True)
        row["inband"]["entries"][0]["text"] = blob(open_blobs(store.root).put(text.encode()), len(text.encode()),
                                                   "text/markdown")
        rows.append(row)
    store.add("sources.arxiv_sweep", rows)
    store.gather()
    fake.on(EXCERPT, verdict("relevant"))
    fake.on("Excerpt long:", verdict("relevant"))
    wanted = {r["inband"]["entries"][0]["text"]["sha256"] for r in rows}
    both = threading.Barrier(2, timeout=5)
    get = BlobStore.get

    def paired(self: BlobStore, digest: str) -> bytes:
        if digest in wanted:
            both.wait()
        return get(self, digest)

    monkeypatch.setattr(BlobStore, "get", paired)
    given: list[str | None] = []
    text_of = triage_ask.document_text
    monkeypatch.setattr(triage_ask, "document_text", lambda r, e: (given.append(e), text_of(r, e))[1])
    plan, _ = plan_with(tmp_path, "q", GATE)
    rec = run_triage(store, plan)
    assert rec.counts["arm1"] == 2 and fake.count(contains="Excerpt long:") == 1
    kept = long_text[:triage_ask.MAX_EXCERPT_CHARS + 1]
    assert sorted(given, key=len) == [EXCERPT, kept]
    row = {"metadata": {"title": "T", "abstract": None, "authors": []}}
    assert text_of(row, kept) == text_of(row, long_text) and text_of(row, kept).endswith("…[truncated]")


def test_document_text_and_basis() -> None:
    row = {"metadata": {"title": "T", "abstract": "x" * (triage_ask.MAX_ABSTRACT_CHARS + 5),
                        "authors": [{"name": f"A{i}"} for i in range(15)]}, "inband_text_sha256": None}
    text = triage_ask.document_text(row, "e" * 10)
    assert text.startswith("Title: T\n\nAuthors: A0, A1") and "(+3 more)" in text and "…[truncated]" in text
    assert text.endswith("Excerpt (start of the text):\n" + "e" * 10)
    assert triage_ask.has_basis(row)
    assert not triage_ask.has_basis({"metadata": {"title": "T", "abstract": None}, "inband_text_sha256": None})
    assert triage_ask.has_basis({"metadata": {"title": None, "abstract": None}, "inband_text_sha256": dg("x")})


# ---------------------------------------------------------------- X16
def _x16(root: Path) -> tuple[str, list[str]]:
    r = {x.check: x for x in run_checks(root, names=["lookup_gate_current"])}["lookup_gate_current"]
    return r.status, r.items


@pytest.mark.slow
def test_x16_lookup_exclusion_only_under_the_build_gate(tmp_path: Path) -> None:
    dev = load_plan(DEV_PLAN)
    cases = {"none": None, "current": dev.gate_digest, "old": dg("the gate before a new consumer")}
    got = {}
    for name, digest in cases.items():
        w = clean_world()
        w.rows("coverage")[0]["gate_digest"] = digest
        write_world(w, tmp_path / name)
        got[name] = _x16(tmp_path / name)[0]
    assert got == {"none": "ok", "current": "ok", "old": "failed"}
    # A record listing its runs: a stale exclusion by a run outside the build is not this build's.
    w = clean_world()
    w.rows("coverage")[0]["gate_digest"] = dg("old")
    assert w.build is not None
    w.build["runs"] = [rid(5)]
    write_world(w, tmp_path / "runs")
    assert _x16(tmp_path / "runs")[0] == "ok"
    w.build["runs"] = [rid(5), rid(6)]
    write_world(w, tmp_path / "runs6")
    status, items = _x16(tmp_path / "runs6")
    assert status == "failed" and "excluded under gate" in items[0]


@pytest.mark.slow
def test_x16_fixture_is_red_on_x16_alone(tmp_path: Path) -> None:
    build_store(tmp_path, "lookup_gate_current")
    red = {r.check for r in run_checks(tmp_path) if r.status in ("failed", "warn")}
    assert red == {"lookup_gate_current"}


# ---------------------------------------------------------------- title basis (D527)
TITLE_PIN = {"name": "safety-relevance-v3"}  # the template with a title variant (triage_ask.SPECS)
WORK = {"id": "https://openalex.org/W9", "cited_by_count": 4,
        "primary_topic": {"display_name": "AI Safety", "subfield": {"display_name": "Artificial Intelligence"},
                          "field": {"display_name": "Computer Science"}},
        "topics": [{"display_name": "AI Safety"}, {"display_name": "Reinforcement Learning"}],
        "keywords": [{"display_name": "Reward hacking"}, {"display_name": "Specification gaming"}]}


def oa_paper(store: Store, i: int, *, record: bool = True) -> Row:
    """An OpenAlex source ``sid(i)`` with no abstract, found by the ``umbrella`` group, with (``record``) its
    ``provider_record`` (``WORK``) stored."""
    from stack.collect.inband import record_bytes
    from tests.test_gather_identity import src

    row = src(i, [("openalex", f"W{i}")], channel="openalex")
    row["metadata"]["abstract"] = None
    row["metadata"]["matched_by"] = [{"query_id": "group:umbrella", "query_digest": dg("q"), "label": "umbrella"}]
    if record:
        body = record_bytes(WORK)
        row["inband"]["entries"] = [{"source_id": sid(i), "content_role": "provider_record",
                                     "raw": blob(open_blobs(store.root).put(body), len(body), "application/json"),
                                     "text": None, "captured_at": NOW}]
    return row


def title_msg(i: int) -> str:
    return f"Title: Paper {i}\n\nAuthors: A. Author\n\nAbstract: none"


def evidence_of(store: Store, rec: RunRecord) -> dict[str, Row]:
    """Per document, the ``triage_evidence`` row its current triage answer names (``evidence_id``). Not the
    largest ``evidence_id``: ids are random (``stack.core.ids.mint``), so that order is no time order."""
    ev, _ = output(store, rec, "triage_evidence")
    by_id = {e["evidence_id"]: e for e in ev}
    return {d: by_id[q["evidence_id"]] for d, r in store.docs().items()
            for q in ann(r)["payload"]["questions"] if q["evidence_id"] is not None}


@pytest.mark.slow
def test_title_basis_asks_no_basis_rows_with_topics_labels_and_reasks_on_an_abstract(
        tmp_path: Path, fake: FakeLlm) -> None:
    """Without ``title_basis`` a row with no abstract and no excerpt is ``skipped[no_basis]`` (and acquire
    would fetch it blind); with it the row is asked on its title and metadata with the title variant
    (evidence ``basis: title``), the OpenAlex topics read from its ``provider_record`` and the query labels
    from ``sources``; a row with no record is asked without them. ``skip`` is not fetched, ``fetch`` is. A
    later abstract changes ``fields_digest``: asked again with the normal prompt (``basis: fields``)."""
    from workflows.acquire.scope import FETCH, SKIP, permit

    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1), paper(3, abstract=None)])
    store.add("sources.openalex", [oa_paper(store, 9)])
    store.gather()
    fake.on(title(1), verdict("relevant"))
    plan_n, _ = plan_with(tmp_path, "n", GATE, pin=TITLE_PIN)
    rec0 = run_triage(store, plan_n)
    assert len(fake.requests) == 1 and rec0.counts["ask.no_basis"] == 2
    docs0 = store.docs()
    for d in (sid(3), sid(9)):
        assert decisions(store)[d] == ("pending", "skipped", "no_basis", "rule", ())
        assert permit(docs0[d], no_basis_fetches=True) == FETCH  # today: fetched blind

    fake.on(title_msg(3), verdict("not_relevant", confidence=0.95))
    fake.on(title_msg(9), verdict("borderline"))
    plan_t, _ = plan_with(tmp_path, "t", GATE, pin=TITLE_PIN, options={"title_basis": True})
    rec1 = run_triage(store, plan_t)
    assert len(fake.requests) == 3 and rec1.counts["ask.title_basis"] == 2  # sid(1) carried, no call
    got = decisions(store)
    assert got[sid(3)] == ("skip", "ok", None, "ai_check", ())
    assert got[sid(9)] == ("fetch", "ok", None, "ai_check", ("review",))
    assert got[sid(1)] == decisions(store)[sid(1)] and got[sid(1)][0] == "fetch"
    docs1 = store.docs()
    assert permit(docs1[sid(3)], no_basis_fetches=True) == SKIP   # a title-basis skip is not fetched blind
    assert permit(docs1[sid(9)], no_basis_fetches=True) == FETCH
    ev = evidence_of(store, rec1)
    for d in (sid(3), sid(9)):
        e = ev[d]
        assert (e["basis"], e["prompt_name"], e["status"]) == ("title", "safety-relevance-title-v1", "ok")
    assert ev[sid(1)]["basis"] == "fields"
    msg9 = next(r for r in fake.requests if title_msg(9) in prompt_text(r))
    text9 = prompt_text(msg9)
    assert "Primary topic: AI Safety (Artificial Intelligence / Computer Science)" in text9
    assert "Topics: AI Safety; Reinforcement Learning" in text9
    assert "Keywords: Reward hacking; Specification gaming" in text9
    assert "Search groups that found it: umbrella" in text9
    assert "fetch whenever the item could plausibly be relevant" in text9.lower()  # the variant template
    text3 = prompt_text(next(r for r in fake.requests if title_msg(3) in prompt_text(r)))
    assert "Primary topic:" not in text3 and "Keywords:" not in text3  # no record: asked anyway
    assert "Search groups that found it:" not in text3  # an arXiv source with no matched_by here
    assert_green(store.root, {"plan": str(tmp_path / "t.yaml"), "files": {"documents": {"file_id": fid(store)}}})

    rec2 = run_triage(store, plan_t)  # nothing new: zero calls
    assert len(fake.requests) == 3 and rec2.counts.get("ask.asked", 0) == 0

    # an abstract arrives for sid(3) (a lookup): fields_digest moves, the normal prompt asks it
    fd_before = docs1[sid(3)]["metadata"]["fields_digest"]
    store.add("sources.arxiv_sweep", [paper(3, abstract="We study reward hacking in RLHF-trained assistants.")])
    store.gather()
    assert store.docs()[sid(3)]["metadata"]["fields_digest"] != fd_before
    fake.on("We study reward hacking in RLHF", verdict("relevant"))
    rec3 = run_triage(store, plan_t)
    assert len(fake.requests) == 4 and rec3.counts.get("ask.title_basis", 0) == 0
    assert decisions(store)[sid(3)][:2] == ("fetch", "ok")
    e3 = evidence_of(store, rec3)[sid(3)]
    assert (e3["basis"], e3["prompt_name"], e3["attempts"]) == ("fields", "safety-relevance-v3", 1)
    assert "Abstract:\nWe study reward hacking" in prompt_text(fake.requests[-1])


@pytest.mark.slow
def test_v4_pin_reasks_every_asked_row_with_the_version_2_record(tmp_path: Path, fake: FakeLlm) -> None:
    """D656: v3 → v4 is a new pin name, so the question's ``config_digest``
    and every work key move and each row the question scope holds is asked again, fields and title basis alike (title-v1 →
    title-v2); the v4 records carry ``Published:`` and ``Source:``, the v3 ones never did; a rerun under v4
    carries everything (zero calls)."""
    store = Store(tmp_path / "store")
    store.add("sources.arxiv_sweep", [paper(1), paper(3, abstract=None)])
    store.add("sources.openalex", [oa_paper(store, 9)])
    store.gather()
    for i in (1, 3, 9):
        fake.on(title(i), verdict("relevant"))
    plan3, _ = plan_with(tmp_path, "v3", GATE, pin=TITLE_PIN, options={"title_basis": True})
    rec3 = run_triage(store, plan3)
    assert len(fake.requests) == 3 and rec3.counts["ask.title_basis"] == 2
    assert fake.count(contains="Published: ") == fake.count(contains="Source: ") == 0
    plan4, _ = plan_with(tmp_path, "v4", GATE, pin={"name": "safety-relevance-v4"}, options={"title_basis": True})
    rec4 = run_triage(store, plan4)
    assert len(fake.requests) == 6 and rec4.counts["ask.asked"] == 3 and rec4.counts["ask.title_basis"] == 2
    new = [prompt_text(r) for r in fake.requests[3:]]
    year = paper(1)["metadata"]["published_at"].year
    assert all(f"Published: {year}" in t for t in new)
    assert sum("Source: arxiv" in t for t in new) == 2 and sum("Source: openalex" in t for t in new) == 1
    ev = evidence_of(store, rec4)
    assert {d: (e["basis"], e["prompt_name"]) for d, e in ev.items()} == {
        sid(1): ("fields", "safety-relevance-v4"), sid(3): ("title", "safety-relevance-title-v2"),
        sid(9): ("title", "safety-relevance-title-v2")}
    rec5 = run_triage(store, plan4)
    assert len(fake.requests) == 6 and rec5.counts.get("ask.asked", 0) == 0


def test_title_basis_option_and_variants_are_strict(tmp_path: Path, fake: FakeLlm) -> None:
    """``title_basis`` must be a boolean; a question whose template has no title variant is refused
    before any call; the option moves the asker name (the triage config_digest) only when on."""
    plan_n, _ = plan_with(tmp_path, "n", GATE, pin=TITLE_PIN)
    llm = annotate_stage.stack_config().llm
    base = triage_ask.llm_asker(plan_n, plan_n.stage_line("triage"))
    assert base is not None and not base.asks_without_basis
    plan_t, _ = plan_with(tmp_path, "t", GATE, pin=TITLE_PIN, options={"title_basis": True})
    on = triage_ask.llm_asker(plan_t, plan_t.stage_line("triage"))
    assert on is not None and on.asks_without_basis and on.name != base.name
    prompt = triage_ask.resolve(gate.questions(plan_n)[0], llm)
    assert base.name == triage_ask.asker_name([prompt])  # unchanged without the option
    plan_f, _ = plan_with(tmp_path, "f", GATE, pin=TITLE_PIN, options={"title_basis": False})
    assert triage_ask.llm_asker(plan_f, plan_f.stage_line("triage")).name == base.name
    plan_bad, _ = plan_with(tmp_path, "b", GATE, pin=TITLE_PIN, options={"title_basis": "yes"})
    with pytest.raises(gate.GateError, match="title_basis"):
        triage_ask.llm_asker(plan_bad, plan_bad.stage_line("triage"))
    plan_v1, _ = plan_with(tmp_path, "v1", GATE, options={"title_basis": True})  # safety-relevance-v1
    with pytest.raises(gate.GateError, match="no title variant"):
        triage_ask.llm_asker(plan_v1, plan_v1.stage_line("triage"))
    assert fake.requests == []


def test_subjects_and_title_text() -> None:
    assert triage_ask.subjects(WORK) == {
        "primary_topic": "AI Safety (Artificial Intelligence / Computer Science)",
        "topics": ["AI Safety", "Reinforcement Learning"], "keywords": ["Reward hacking", "Specification gaming"]}
    assert triage_ask.subjects({"id": "W1"}) == {} and triage_ask.subjects(None) == {}
    row = {"metadata": {"title": "Reward hacking", "authors": [
        {"name": "A", "affiliation_raw": "Lab X"}, {"name": "B", "affiliation_raw": "Lab X"},
        {"name": "C", "affiliation_raw": None}]}}
    text = triage_ask.title_text(row, None, ["umbrella"])
    assert text.split("\n\n") == ["Title: Reward hacking", "Authors: A, B, C", "Affiliations: Lab X",
                                  "Abstract: none (no abstract or text is available for this record)",
                                  "Search groups that found it: umbrella"]
