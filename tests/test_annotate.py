"""Scenario tests of the annotate framework (``stack.annotate``, PLAN T4.3 slot 1; §3.1, D19, D32,
D99): a tiny ``documents`` chain with real text blobs → ``fold.corpus`` → ``annotate.<wf>``
through the real stage runner (``stack.stage_runner.run_stage``), the workflows being the test
fakes of ``tests/fakes/annotate.py`` on the fake model (no network; the ledger is a tmp file).
Every store ends with ``run_checks`` green on all implemented checks (V7, V8, X6, X7, X8 among
them).
"""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

import stack.annotate.stage as annotate_stage
from stack.checks.verify import run_checks
from stack.llm import FatalLlmError, TransportError
from stack.overrides import rows_digest, run_add
from stack.overrides import rows_of as overrides_rows
from stack.stage_runner import StageError, run_stage
from stack.store.chain import read_view, resolve
from tests.fakes.annotate import (
    AT_RUN,
    FAIL,
    REPO,
    STAGES_ROOT,
    annotate,
    doc_with_text,
    fold_corpus,
    plan,
    revise,
    rows_of,
    write_documents,
)
from tests.fakes.llm import FakeLlm, malformed, ok
from tests.fakes.overrides import OVERRIDES_DIR, add_overrides
from tests.synth.records import SERIES
from tests.synth.rows import rid, sid, with_digests
from tests.synth.world import write_on_head

pytestmark = pytest.mark.slow  # runner-driven scenario stores (D90; `docs/SETUP.md` §7)

ANNOTATE_CHECKS = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Store, fake model, ledger, and the test workflows' stage root."""
    fake = FakeLlm()
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(STAGES_ROOT))
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    return {"store": tmp_path / "store", "fake": fake}


def verdicts(fake: FakeLlm, n: int) -> None:
    for i in range(n):
        fake.on(f"Title: Paper {i}\n", ok({"verdict": "relevant" if i % 2 == 0 else "not_relevant",
                                          "confidence": 0.5 + i / 100}))


def all_green(store: Path) -> None:
    """No check red; the five annotate checks ``ok`` (not skipped: no build here, so the X ones
    take the newest file per workflow). Build-referenced checks skip with ``no_builds``."""
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert red == {}, red
    by = {r.check: r for r in results}
    assert {n: by[n].status for n in ANNOTATE_CHECKS} == dict.fromkeys(ANNOTATE_CHECKS, "ok")
    assert {r.error_class for r in results if r.status == "skipped"} <= {"no_builds", "not_implemented"}


def test_screen_prior_rerun_and_changed_text(env: dict[str, Any]) -> None:
    store, fake = env["store"], env["fake"]
    verdicts(fake, 4)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(4)], run=1)
    p = plan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    assert fake.count() == 4 and r1.scope_complete
    s1, e1 = rows_of(store, r1, "screen"), rows_of(store, r1, "screen_evidence")
    assert [(r["doc_id"], r["status"], r["verdict"], r["origin"]) for r in s1] == [
        (sid(i), "ok", "relevant" if i % 2 == 0 else "not_relevant", "ai_check") for i in range(4)]
    assert {r["evidence_id"] for r in s1} == {e["evidence_id"] for e in e1} and len(e1) == 4
    assert all(r["is_basis_current"] and r["run_id"] == r1.run_id and r["attempts"] == 1 for r in s1)
    assert all(e["prompt_name"] == p.prompts["annotate.screen"]["name"] and not e["is_cache_hit"] for e in e1)
    all_green(store)

    # rerun with r1's outputs as its prior (--in): no call, the same <wf> rows, the evidence carried byte for byte
    r2 = annotate(store, p, "screen", corpus, prior=r1)
    assert fake.count() == 4 and r2.counts["carried"] == 4 and "asked" not in r2.counts
    assert rows_of(store, r2, "screen") == s1
    assert rows_of(store, r2, "screen_evidence") == e1

    # doc 2's text changes: only doc 2 is asked again; the others keep their evidence
    revise(store, 2, "Body of paper 2. Revised.", run=2)
    corpus2 = fold_corpus(store, p)
    r3 = annotate(store, p, "screen", corpus2, prior=r2)
    assert fake.count() == 5 and fake.count(contains="Title: Paper 2\n") == 2
    s3 = rows_of(store, r3, "screen")
    assert [r for r in s3 if r["doc_id"] != sid(2)] == [r for r in s1 if r["doc_id"] != sid(2)]
    new = next(r for r in s3 if r["doc_id"] == sid(2))
    old = next(r for r in s1 if r["doc_id"] == sid(2))
    assert new["evidence_id"] != old["evidence_id"] and new["basis_digest"] != old["basis_digest"]
    assert new["run_id"] == r3.run_id and new["is_basis_current"]
    assert len(rows_of(store, r3, "screen_evidence")) == 5
    all_green(store)


def test_budget_refusal_and_no_text(env: dict[str, Any]) -> None:
    """A stage budget that admits a few calls: the first refused reservation trips the scope,
    that row and every later one is ``not_attempted[budget]`` (never sent, no evidence), the run
    is ``ok`` with ``scope_complete = false``; a row without text is ``skipped[no_text]``. A
    rerun with room, the first run's outputs as its prior (``--in``), asks exactly the refused rows."""
    store, fake = env["store"], env["fake"]
    verdicts(fake, 7)
    docs = [doc_with_text(store, i, f"Body of paper {i}.") for i in range(6)]
    docs.append(doc_with_text(store, 6, None, state="triaged"))  # no content: no text
    write_documents(store, docs, run=1)
    tight = plan(screen={"budget": {"max_usd": 0.0045}})
    corpus = fold_corpus(store, tight, filter="everything")
    r1 = annotate(store, tight, "screen", corpus)
    rows = {r["doc_id"]: r for r in rows_of(store, r1, "screen")}
    status = [(rows[sid(i)]["status"], rows[sid(i)]["error_class"]) for i in range(7)]
    asked = fake.count()
    assert 1 <= asked < 6, status
    assert status == [("ok", None)] * asked + [("not_attempted", "budget")] * (6 - asked) + [
        ("skipped", "no_text")]
    assert not r1.scope_complete and r1.counts["not_attempted.budget"] == 6 - asked
    assert len(rows_of(store, r1, "screen_evidence")) == asked
    assert rows[sid(6)]["basis_digest"] is None and rows[sid(6)]["is_basis_current"] is None
    assert all(rows[sid(i)]["evidence_id"] is None for i in range(asked, 7))
    all_green(store)

    roomy = plan()
    r2 = annotate(store, roomy, "screen", corpus, prior=r1)
    assert fake.count() == 6 and r2.scope_complete and r2.counts["carried"] == asked
    assert [r["status"] for r in rows_of(store, r2, "screen")] == ["ok"] * 6 + ["skipped"]
    all_green(store)


def test_override_row_wins(env: dict[str, Any]) -> None:
    """``<wf>`` overrides (``overrides.add``) in ``select`` (``--only select``, no call): a scoped human row
    override wins over the model's evidence; an override pinned to another basis is stale and
    not applied; a quarantine; an unscoped entry needs review. X8 recomputes all of it."""
    store, fake = env["store"], env["fake"]
    verdicts(fake, 4)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(5)], run=1)
    fake.on("Title: Paper 4\n", ok({"verdict": "relevant", "confidence": 0.9}))
    p = plan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    s1 = {r["doc_id"]: r for r in rows_of(store, r1, "screen")}
    path = store.parent / "screen.yaml"
    path.write_text(f"""
- override_id: o-row
  author: tomas
  created_at: 2026-10-12T09:00:00Z
  key: {{doc_id: {sid(0)}}}
  scope: {{members: [{sid(0)}]}}
  row: true
  value: {{verdict: not_relevant, reasoning: read it myself}}
- override_id: o-stale
  key: {{doc_id: {sid(1)}}}
  scope: {{members: [{sid(1)}], basis_digest: '{'f' * 64}'}}
  field: verdict
  value: relevant
- override_id: o-quarantine
  key: {{doc_id: {sid(2)}}}
  scope: {{members: [{sid(2)}]}}
  quarantine: true
- override_id: o-unscoped
  key: {{doc_id: {sid(3)}}}
  field: verdict
  value: relevant
""")
    assert run_add(store, path, OVERRIDES_DIR).status == "ok"
    # D495: the same run without the series' overrides is refused (it would drop every override row)
    with pytest.raises(StageError, match=r"annotate.screen: data/overrides/main/ has overrides"):
        run_stage(store, p, "annotate.screen", [corpus, *(r1.outputs[f]["path"] for f in ("screen", "screen_evidence"))],
                  only=["select"], series=SERIES)
    r2 = annotate(store, p, "screen", corpus, prior=r1, only=["select"])
    assert r2.inputs["overrides"] == resolve(store, OVERRIDES_DIR).head  # the pinned head (design v4.1 §8)
    assert fake.count() == 5 and r2.counts["carried"] == 5
    s2 = {r["doc_id"]: r for r in rows_of(store, r2, "screen")}
    row = s2[sid(0)]
    assert (row["status"], row["origin"], row["verdict"], row["reasoning"], row["override_ids"]) == (
        "ok", "human", "not_relevant", "read it myself", ["o-row"])
    # D717: the row override supplied the whole payload, so the row takes its provenance (was the evidence's)
    assert row["confidence"] is None and row["evidence_id"] is None
    assert (row["decided_at"], row["attempts"]) == (dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC), 0)
    assert row["config_digest"] != s1[sid(0)]["config_digest"]
    assert row["result_digest"] != s1[sid(0)]["result_digest"]
    assert (s2[sid(1)]["override_stale_ids"], s2[sid(1)]["override_ids"], s2[sid(1)]["verdict"]) == (
        ["o-stale"], [], s1[sid(1)]["verdict"])
    assert (s2[sid(2)]["status"], s2[sid(2)]["error_class"], s2[sid(2)]["verdict"]) == (
        "quarantined", "operator", s1[sid(2)]["verdict"])
    assert (s2[sid(3)]["status"], s2[sid(3)]["error_class"]) == ("skipped", "review_required")
    assert s2[sid(4)] == s1[sid(4)]
    all_green(store)


def test_override_scope_fallback_and_field_over_row(env: dict[str, Any]) -> None:
    """D337 in ``select``: a winner whose scope no longer matches is stale and the older
    applicable row stands (doc 0); the winning field row composes over the winning row-level row
    whatever their times and tiers (doc 1)."""
    store, fake = env["store"], env["fake"]
    verdicts(fake, 2)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(2)], run=1)
    p = plan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)

    def o(oid: str, i: int, at: str, **more: Any) -> dict[str, Any]:
        return {"override_id": oid, "created_at": f"2026-10-0{at}T00:00:00Z", "key": {"doc_id": sid(i)},
                "scope": {"members": more.pop("members", [sid(i)])}, **more}

    add_overrides(store, "screen", [
        o("o-ai", 0, "1", origin="ai_check", field="verdict", value="not_relevant"),
        o("o-stale", 0, "2", members=[sid(0), sid(9)], field="verdict", value="relevant"),
        o("o-row", 1, "1", row=True, value={"verdict": "relevant", "reasoning": "mine"}),
        o("o-field", 1, "2", origin="ai_check", field="reasoning", value="checked"),
    ])
    r2 = annotate(store, p, "screen", corpus, prior=r1, only=["select"])
    s2 = {r["doc_id"]: r for r in rows_of(store, r2, "screen")}
    d0, d1 = s2[sid(0)], s2[sid(1)]
    assert (d0["verdict"], d0["origin"], d0["override_ids"], d0["override_stale_ids"]) == (
        "not_relevant", "ai_check", ["o-ai"], ["o-stale"])
    assert (d1["verdict"], d1["reasoning"], d1["origin"], d1["override_ids"]) == (
        "relevant", "checked", "human", ["o-row", "o-field"])
    all_green(store)


def test_override_supplied_row_carries_the_overrides_provenance(env: dict[str, Any]) -> None:
    """D717: a row override that supplies the whole payload is the decision, so the ``<wf>`` row and the folded
    ``annotations.<wf>`` take its provenance, not the selected evidence's: ``decided_at`` = the override's
    ``created_at``, ``config_digest`` = the override row's fingerprint, no ``evidence_id``, ``origin`` = its tier; ``run_id`` and T2 as a
    row without evidence. Doc 0: the model failed permanently and an ``ai_check`` stand-in answer replaces it (the
    refused-document stand-in case); doc 1: a human field override over the model's answer keeps the evidence's provenance; doc 2
    untouched. The author and note stay on the override row, reached through ``override_ids``."""
    store, fake = env["store"], env["fake"]
    fake.on("Title: Paper 0\n", malformed())
    verdicts(fake, 3)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(3)], run=1)
    p = plan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    s1 = {r["doc_id"]: r for r in rows_of(store, r1, "screen")}
    assert (s1[sid(0)]["status"], s1[sid(0)]["error_class"]) == ("failed_permanent", "schema_invalid")
    at = dt.datetime(2026, 10, 24, 13, 6, 30, tzinfo=dt.UTC)
    add_overrides(store, "screen", [
        {"override_id": "o-standin", "origin": "ai_check", "author": "ai:test-model",
         "created_at": at.isoformat(), "note": "stand-in answer (model, method)",
         "key": {"doc_id": sid(0)}, "scope": {"members": [sid(0)], "basis_digest": s1[sid(0)]["basis_digest"]},
         "row": True, "value": {"verdict": "relevant", "confidence": 0.7}},
        {"override_id": "o-field", "author": "tomas", "created_at": at.isoformat(),
         "key": {"doc_id": sid(1)}, "scope": {"members": [sid(1)]}, "field": "verdict", "value": "relevant"},
    ])
    r2 = annotate(store, p, "screen", corpus, prior=r1, only=["select"])
    s2 = {r["doc_id"]: r for r in rows_of(store, r2, "screen")}
    d0, d1 = s2[sid(0)], s2[sid(1)]
    (standin,) = [o for o in overrides_rows(read_view(resolve(store, OVERRIDES_DIR))) if o.override_id == "o-standin"]
    assert (standin.author, standin.note) == ("ai:test-model", "stand-in answer (model, method)")
    standin_digest = rows_digest([standin])  # the fingerprint of what decided it: no config did
    assert {k: d0[k] for k in ("status", "error_class", "error", "origin", "override_ids", "verdict", "decided_at",
                               "config_digest", "evidence_id", "run_id", "attempts", "first_attempt_at",
                               "last_attempt_at", "retry_after", "basis_digest")} == {
        "status": "ok", "error_class": None, "error": None, "origin": "ai_check", "override_ids": ["o-standin"],
        "verdict": "relevant", "decided_at": at, "config_digest": standin_digest, "evidence_id": None,
        "run_id": r2.run_id, "attempts": 0, "first_attempt_at": None, "last_attempt_at": None,
        "retry_after": None, "basis_digest": s1[sid(0)]["basis_digest"]}
    # a field override over the model's answer: the answer is still the evidence's, so is its provenance
    assert (d1["verdict"], d1["origin"], d1["override_ids"]) == ("relevant", "human", ["o-field"])
    assert {k: d1[k] for k in ("decided_at", "config_digest", "evidence_id", "run_id", "attempts")} == {
        k: s1[sid(1)][k] for k in ("decided_at", "config_digest", "evidence_id", "run_id", "attempts")}
    assert s2[sid(2)] == s1[sid(2)]
    all_green(store)  # V7 takes an override-supplied ok row without evidence; X8 reproduces it

    rec = run_stage(store, p, "fold.build", [corpus, r2.outputs["screen"]["path"]], series=SERIES)
    assert rec.status == "ok", rec.error
    built = {r["doc_id"]: r["annotations"]["screen"] for r in rows_of(store, rec, "corpus")}
    b0 = built[sid(0)]
    assert {k: b0[k] for k in ("status", "origin", "decided_at", "config_digest", "evidence_id",
                               "is_basis_current")} == {
        "status": "ok", "origin": "ai_check", "decided_at": at, "config_digest": standin_digest,
        "evidence_id": None, "is_basis_current": True}
    assert b0["payload"]["verdict"] == "relevant"
    assert built[sid(1)]["config_digest"] == s1[sid(1)]["config_digest"] != standin_digest


def test_override_supplied_items_carry_the_overrides_provenance(env: dict[str, Any]) -> None:
    """D717 at item grain: doc 0 (the model failed) gets every item from overrides, so its rows take the overrides'
    provenance (``decided_at`` the newest ``created_at``, the used rows' fingerprint, no evidence); doc 1 keeps one
    model item beside the override's, so the evidence's provenance stands."""
    store, fake = env["store"], env["fake"]
    fake.on("Title: Paper 0\n", malformed())
    fake.on("Title: Paper 1\n", ok({"authors": [{"name": "Ada Lovelace", "orgs": ["Example Lab"]},
                                               {"name": "Alan Turing", "orgs": []}]}))
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(2)], run=1)
    p = plan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "affiliation", corpus)
    a1 = {(r["doc_id"], r["item_key"]): r for r in rows_of(store, r1, "affiliation")}
    assert a1[(sid(0), "")]["status"] == "failed_permanent"

    def item(oid: str, i: int, key: str, name: str, at: str) -> dict[str, Any]:
        return {"override_id": oid, "origin": "ai_check", "author": "ai:test-model", "created_at": at,
                "key": {"doc_id": sid(i), "item_key": key}, "scope": {"members": [sid(i)]}, "row": True,
                "value": {"author_name": name, "byline_index": int(key)}}

    add_overrides(store, "affiliation", [item("o-a", 0, "0", "Grace Hopper", "2026-10-24T10:00:00Z"),
                                         item("o-b", 0, "1", "Edsger Dijkstra", "2026-10-24T11:00:00Z"),
                                         item("o-c", 1, "1", "Alan M. Turing", "2026-10-24T12:00:00Z")])
    r2 = annotate(store, p, "affiliation", corpus, prior=r1, only=["select"])
    a2 = {(r["doc_id"], r["item_key"]): r for r in rows_of(store, r2, "affiliation")}
    assert sorted(a2) == [(sid(0), "0"), (sid(0), "1"), (sid(1), "0"), (sid(1), "1")]
    view = {o.override_id: o for o in overrides_rows(read_view(resolve(store, OVERRIDES_DIR)))}
    for k in ("0", "1"):
        r = a2[(sid(0), k)]
        assert (r["status"], r["origin"], r["override_ids"], r["evidence_id"], r["attempts"]) == (
            "ok", "ai_check", ["o-a", "o-b"], None, 0)
        assert r["decided_at"] == dt.datetime(2026, 10, 24, 11, 0, tzinfo=dt.UTC)
        assert r["config_digest"] == rows_digest([view["o-a"], view["o-b"]])
    assert a2[(sid(0), "1")]["author_name"] == "Edsger Dijkstra"
    ev1 = a1[(sid(1), "0")]
    for k in ("0", "1"):
        r = a2[(sid(1), k)]
        assert (r["origin"], r["override_ids"]) == ("ai_check", ["o-c"])
        assert {c: r[c] for c in ("evidence_id", "config_digest", "decided_at", "run_id", "attempts")} == {
            c: ev1[c] for c in ("evidence_id", "config_digest", "decided_at", "run_id", "attempts")}
    all_green(store)

    rec = run_stage(store, p, "fold.build", [corpus, r2.outputs["affiliation"]["path"]], series=SERIES)
    assert rec.status == "ok", rec.error
    built = {r["doc_id"]: r["annotations"]["affiliation"] for r in rows_of(store, rec, "corpus")}
    assert (built[sid(0)]["evidence_id"], built[sid(0)]["config_digest"]) == (
        None, rows_digest([view["o-a"], view["o-b"]]))
    assert [i["author_name"] for i in built[sid(0)]["payload"]["items"]] == ["Grace Hopper", "Edsger Dijkstra"]
    assert built[sid(1)]["evidence_id"] == ev1["evidence_id"]


def test_item_grain_round_trip(env: dict[str, Any]) -> None:
    """``affiliation`` (item grain): one invocation per document (one evidence row holding the
    ``items`` list), one ``<wf>`` row per item, an ``empty`` document as one ``item_key = ''``
    row; ``fold.build`` joins the rows back into ``payload.items[]``."""
    store, fake = env["store"], env["fake"]
    fake.on("Title: Paper 0\n", ok({"authors": [{"name": "Ada Lovelace", "orgs": ["Example Lab"]},
                                               {"name": "Alan Turing", "orgs": []}]}))
    fake.on("Title: Paper 1\n", ok({"authors": []}))
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(2)], run=1)
    p = plan()
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "affiliation", corpus)
    rows = rows_of(store, r1, "affiliation")
    assert [(r["doc_id"], r["item_key"], r["status"], r["author_name"]) for r in rows] == [
        (sid(0), "0", "ok", "Ada Lovelace"), (sid(0), "1", "ok", "Alan Turing"), (sid(1), "", "empty", None)]
    ev = {e["doc_id"]: e for e in rows_of(store, r1, "affiliation_evidence")}
    assert [i["author_name"] for i in ev[sid(0)]["items"]] == ["Ada Lovelace", "Alan Turing"]
    assert ev[sid(1)]["items"] == [] and ev[sid(1)]["status"] == "empty"
    all_green(store)

    rec = run_stage(store, p, "fold.build", [corpus, r1.outputs["affiliation"]["path"]], series=SERIES)
    assert rec.status == "ok", rec.error
    built = {r["doc_id"]: r["annotations"]["affiliation"] for r in rows_of(store, rec, "corpus")}
    assert [i["author_name"] for i in built[sid(0)]["payload"]["items"]] == ["Ada Lovelace", "Alan Turing"]
    assert (built[sid(0)]["status"], built[sid(0)]["is_basis_current"]) == ("ok", True)
    assert (built[sid(1)]["status"], built[sid(1)]["payload"]["items"]) == ("empty", [])


def test_attempts_backoff_cap_and_halt(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempts per work key (§3.1 as amended for passes, round 4): the first transient failure is
    due at once (the next run asks again, ``supersedes`` the failed evidence), later ones back off
    ``1h·2^(attempts-1)``; a rerun before ``retry_after`` carries it; the third transient failure is
    ``quarantined[cap_reached]`` and is never asked again. A fatal error (auth/credit) halts the run: later rows ``not_attempted[halted]``."""
    store, fake = env["store"], env["fake"]
    verdicts(fake, 3)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(3)], run=1)
    p = plan()
    corpus = fold_corpus(store, p)
    clock = {"now": AT_RUN}
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: clock["now"])
    monkeypatch.setitem(FAIL, sid(1), TransportError("fake: upstream timed out"))
    hour = dt.timedelta(hours=1)

    def run(prior: Any, after: dt.timedelta) -> tuple[Any, dict[str, Any]]:
        clock["now"] = AT_RUN + after
        rec = annotate(store, p, "screen", corpus, prior=prior)
        return rec, {r["doc_id"]: r for r in rows_of(store, rec, "screen")}

    r1, s = run(None, dt.timedelta(0))
    row = s[sid(1)]
    assert (row["status"], row["error_class"], row["attempts"], row["retry_after"]) == (
        "failed_transient", "network", 1, AT_RUN)  # the first retry is due at once
    assert s[sid(0)]["status"] == s[sid(2)]["status"] == "ok" and fake.count() == 2
    r2, s = run(r1, 0.5 * hour)  # due: attempt 2, backs off 2 h
    ev = {e["evidence_id"]: e for e in rows_of(store, r2, "screen_evidence")}
    row = s[sid(1)]
    assert (row["status"], row["attempts"], row["retry_after"]) == ("failed_transient", 2, AT_RUN + 2.5 * hour)
    assert ev[row["evidence_id"]]["supersedes"] == r1_failed(store, r1)
    r3, s = run(r2, 1.5 * hour)  # not due: carried
    assert s[sid(1)]["attempts"] == 2 and r3.counts["carried"] == 3
    r4, s = run(r3, 3 * hour)  # attempt 3 = the cap
    assert (s[sid(1)]["status"], s[sid(1)]["error_class"], s[sid(1)]["attempts"], s[sid(1)]["retry_after"]) == (
        "quarantined", "cap_reached", 3, None)
    r5, s = run(r4, 30 * 24 * hour)  # quarantined: never asked again
    assert s[sid(1)]["status"] == "quarantined" and r5.counts["carried"] == 3 and fake.count() == 2
    all_green(store)

    # an auth/credit stop halts: no evidence row and no attempt for its row (D430), every later row
    # is not attempted; the next run asks it at attempt 1
    monkeypatch.setitem(FAIL, sid(0), FatalLlmError("fake: 402 no credit"))
    write_documents(store, [doc_with_text(store, i, f"New body {i}.") for i in range(3)], run=3)
    corpus2 = fold_corpus(store, p)
    r6 = annotate(store, p, "screen", corpus2, prior=r5)
    s = {r["doc_id"]: r for r in rows_of(store, r6, "screen")}
    assert [(s[sid(i)]["status"], s[sid(i)]["error_class"]) for i in range(3)] == [("not_attempted", "halted")] * 3
    assert not any(e["run_id"] == r6.run_id for e in rows_of(store, r6, "screen_evidence"))
    assert not r6.scope_complete and r6.counts["halted"] == 1 and fake.count() == 2
    del FAIL[sid(0)]
    r7 = annotate(store, p, "screen", corpus2, prior=r6)
    ev7 = {e["doc_id"]: e for e in rows_of(store, r7, "screen_evidence") if e["run_id"] == r7.run_id}
    assert ev7[sid(0)]["status"] == "ok" and ev7[sid(0)]["attempts"] == 1


def test_a_self_cancelled_run_stops_asking(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The sync ask loop heartbeats each ``wait()`` round (L5-7, D428): once the run's ticks set the
    cancel flag (D354), the loop raises ``JobCancelled`` within a round instead of asking its whole
    queue and discarding it at publish. The flag is set by the fake model on its first call."""
    from stack.store import job as J

    store, fake = env["store"], env["fake"]
    n = 40
    verdicts(fake, n)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(n)], run=1)
    p = plan({"options": {"workers": 2}})
    corpus = fold_corpus(store, p)
    jobs: list[Any] = []
    init = J.Job.__init__

    def track(self: Any, *a: Any, **kw: Any) -> None:
        init(self, *a, **kw)
        jobs.append(self)

    def cancel(_request: Any) -> None:  # what a run of failed ticks does (stack.store.job)
        jobs[-1].cancel_reason = "test: ticks failing"
        jobs[-1].cancel.set()

    monkeypatch.setattr(J.Job, "__init__", track)
    fake.hook = cancel
    rec = run_stage(store, p, "annotate.screen", [corpus], series=SERIES)
    assert (rec.status, rec.error_class) == ("cancelled", "tick_failed"), rec.error
    assert fake.count() <= 4, fake.count()  # before D428: all 40 asked, then discarded at publish


def r1_failed(store: Path, r1: Any) -> str:
    """The evidence id of run 1's failed row (doc 1)."""
    return next(e["evidence_id"] for e in rows_of(store, r1, "screen_evidence") if e["doc_id"] == sid(1))


def test_html_only_paper_is_screened_and_affiliated(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """D485: an arXiv paper acquired HTML-only (``arxiv_pdf: on_miss | never``: ``current_role = 'html'``,
    its ``full_text`` ``skipped[not_requested]``) is in the default corpus (``has_text`` reads the
    pointer), is screened, and passes std-v3's ``annotate.affiliation`` filter (taken from the shipped
    plan) next to a PDF paper; a paper whose current text is only its abstract does not pass that filter."""
    from stack.plan import load_plan
    from tests.synth.rows import html_only

    store, fake = env["store"], env["fake"]
    html = doc_with_text(store, 0, "Body of paper 0, from arXiv's LaTeXML page.")
    html["content"] = html_only(html["content"])
    pdf = doc_with_text(store, 1, "Body of paper 1.")
    abstract = doc_with_text(store, 2, "Abstract 2.")
    abstract["content"]["current_role"] = "abstract"
    write_documents(store, [html, pdf, abstract], run=1)
    std = load_plan(REPO / "configs" / "plans" / "std-v3.yaml")
    (aff_filter,) = [line.filter for line in std.stages if line.stage == "annotate.affiliation"]
    p = plan(affiliation={"filter": aff_filter})
    corpus = fold_corpus(store, p)

    verdicts(fake, 3)
    r_screen = annotate(store, p, "screen", corpus)
    screened = {r["doc_id"]: r for r in rows_of(store, r_screen, "screen")}
    assert sorted(screened) == [sid(0), sid(1), sid(2)] and screened[sid(0)]["status"] == "ok"

    aff_fake = FakeLlm()
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: aff_fake)
    for i in (0, 1, 2):
        aff_fake.on(f"Title: Paper {i}\n", ok({"authors": [{"name": f"Author {i}", "orgs": ["Example Lab"]}]}))
    r_aff = annotate(store, p, "affiliation", corpus)
    rows = rows_of(store, r_aff, "affiliation")
    assert [(r["doc_id"], r["status"], r["author_name"]) for r in rows] == [
        (sid(0), "ok", "Author 0"), (sid(1), "ok", "Author 1")]
    assert aff_fake.count(contains="Title: Paper 2\n") == 0


@pytest.mark.parametrize(("raw", "ok"), [(1, True), (256, True), (1024, True), (0, False), (1025, False)])
def test_workers_cap_is_1024(raw: int, ok: bool) -> None:
    """``options.workers`` is 1..1024 (D716; 1..256 in D692, was 32), 64 when unset (was 1); triage shares both."""
    assert annotate_stage._workers({}) == 64
    if ok:
        assert annotate_stage._workers({"workers": raw}) == raw
    else:
        with pytest.raises(StageError, match="options.workers must be between 1 and 1024"):
            annotate_stage._workers({"workers": raw})



def test_a_pool_that_cannot_start_another_thread_still_asks_every_row(env: dict[str, Any],
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """D786: the 2026-11-01 cycle's ``annotate.technique_staged`` (1024 workers) failed
    ``code_bug`` at ``pool.submit``, ``RuntimeError: can't start new thread`` (Railway's ``pids.max``
    1000). With thread starts failing after 4, the run asks all 30 rows on 4 threads and records them."""
    import time

    from tests.test_stage_runner import failing_thread_starts

    store, fake = env["store"], env["fake"]
    n = 30
    verdicts(fake, n)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(n)], run=1)
    p = plan({"options": {"workers": 64}})
    corpus = fold_corpus(store, p)
    fake.hook = lambda _req: time.sleep(0.02)  # calls overlap, so the pool wants more than 4 threads
    started = failing_thread_starts(monkeypatch, "annotate-screen", allowed=4)
    rec = run_stage(store, p, "annotate.screen", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert (rec.counts["asked"], rec.counts["workers"], rec.counts["workers.started"]) == (n, 64, 4)
    assert len(started) == 4 and fake.count() == n


def test_an_annotate_pool_keeps_the_containers_reserve(env: dict[str, Any]) -> None:
    """D786: with 132 tasks left in the container's cgroup, a 64-worker line runs on 132 - 128 = 4 threads."""
    from stack import stage_runner

    store, fake = env["store"], env["fake"]
    n = 12
    verdicts(fake, n)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(n)], run=1)
    p = plan({"options": {"workers": 64}})
    corpus = fold_corpus(store, p)
    stage_runner.CGROUP_ROOT.mkdir(parents=True)
    (stage_runner.CGROUP_ROOT / "pids.max").write_text("1000\n")
    (stage_runner.CGROUP_ROOT / "pids.current").write_text("868\n")
    rec = run_stage(store, p, "annotate.screen", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert (rec.counts["asked"], rec.counts["workers"], rec.counts["workers.started"]) == (n, 64, 4)


def test_workers_started_counts_the_threads_a_capped_pool_started(env: dict[str, Any],
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """D786: under ``STACK_POOL_MAX_THREADS=5`` three rows start three threads; ``workers.started`` says 3, not
    the cap. Uncapped, no ``workers.started``."""
    import time

    store, fake = env["store"], env["fake"]
    verdicts(fake, 3)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(3)], run=1)
    p = plan({"options": {"workers": 64}})
    corpus = fold_corpus(store, p)
    fake.hook = lambda _req: time.sleep(0.05)
    monkeypatch.setenv("STACK_POOL_MAX_THREADS", "5")
    rec = run_stage(store, p, "annotate.screen", [corpus], series=SERIES)
    assert rec.status == "ok", rec.error
    assert (rec.counts["asked"], rec.counts["workers"], rec.counts["workers.started"]) == (3, 64, 3)

# ─────────────────────────── accepted configs (D758, stack.plan "Accepted configs") ───────────────────────────


def _repinned(p: Any, wf: str = "screen", **extra: Any) -> Any:
    """``p`` with the ``annotate.<wf>`` plan pin given ``extra`` keys: another config digest, the same prompt (the
    fakes ignore other pin keys), as the 1.1.0 vocabulary envelope moved the digests and sent the same bytes."""
    pin = {**p.prompts[f"annotate.{wf}"], **extra}
    return p.model_copy(update={"prompts": {**p.prompts, f"annotate.{wf}": pin}})


def _accepting(p: Any, target: str, configs: list[str], *, name: str = "screen-v7", wf: str = "screen") -> Any:
    """``p`` with its ``annotate.<wf>`` line naming ``target`` and accepting ``configs`` (in order), validated."""
    from stack.plan import StageLine

    lines = []
    for line in p.stages:
        if line.stage == f"annotate.{wf}":
            line = StageLine.model_validate({**line.model_dump(mode="json", by_alias=True), "target": target,
                                             "accepted": [{"config": c, "name": name, "decision": "D1"}
                                                          for c in configs]})
        lines.append(line)
    return p.model_copy(update={"stages": lines})


def accepted_green(store: Path) -> None:
    """:func:`all_green`, except X6's warning that the plan file's current pin is not the run's: these runs use
    plans built in memory (config B), not the ``dev-v2`` file X6 compares with."""
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    moved = red.pop("policy_pins", [])
    assert red == {} and all("the next build re-asks" in m for m in moved), (red, moved)


def _by_doc(store: Path, rec: Any, family: str = "screen") -> dict[str, dict[str, Any]]:
    return {r["doc_id"]: r for r in rows_of(store, rec, family)}


def _digest(p: Any, wf: str = "screen") -> str:
    """The config digest ``annotate.<wf>`` runs under in plan ``p`` (computed as the stage does, no run)."""
    from stack.stage_runner import load_stage_module

    w = load_stage_module(f"annotate.{wf}").WORKFLOW
    pin = dict(p.prompts.get(f"annotate.{wf}", {}))
    return annotate_stage.config_digest(w.spec, pin, annotate_stage.resolve_pin(w, pin, annotate_stage.stack_config))


@pytest.fixture
def two_configs(env: dict[str, Any]) -> dict[str, Any]:
    """Three documents answered under config A; plan B differs only in its pin (config digest B)."""
    store, fake = env["store"], env["fake"]
    verdicts(fake, 3)
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in range(3)], run=1)
    pa_ = plan()
    corpus = fold_corpus(store, pa_)
    ra = annotate(store, pa_, "screen", corpus)
    pb = _repinned(pa_, note="b")
    assert fake.count() == 3
    return {**env, "pa": pa_, "pb": pb, "corpus": corpus, "ra": ra}


def test_accepted_rows_are_selected_with_their_own_labels_and_not_asked(two_configs: dict[str, Any]) -> None:
    from stack.plan import selection_digest

    t = two_configs
    store, fake, ra = t["store"], t["fake"], t["ra"]
    cfg_a, cfg_b = ra.config_digest, _digest(t["pb"])
    assert cfg_a != cfg_b and cfg_a == _digest(t["pa"])
    assert ra.selection == {"digest": selection_digest(cfg_a, []), "target": cfg_a, "accepted": [],
                            "same_basis": True}  # D807: recorded beside the digest
    pb = _accepting(t["pb"], cfg_b, [cfg_a])
    rb = annotate(store, pb, "screen", t["corpus"], prior=ra)
    assert fake.count() == 3 and "asked" not in rb.counts and "cache_hits" not in rb.counts
    assert rb.counts["carried"] == rb.counts["carried.accepted"] == 3 and rb.config_digest == cfg_b
    # the rows are A's, labelled A's: evidence, config, decided_at and run are the answer's own
    assert _by_doc(store, rb) == _by_doc(store, ra)
    assert {r["config_digest"] for r in rows_of(store, rb, "screen")} == {cfg_a}
    entry = {"config": cfg_a, "name": "screen-v7", "decision": "D1"}
    assert rb.selection == {"digest": selection_digest(cfg_b, [entry]), "target": cfg_b, "accepted": [entry],
                            "same_basis": True}
    assert rb.prompt_pin is not None and rb.prompt_pin["role_params"]["name"] == "screen"  # the default role
    accepted_green(store)  # X6 judges A's rows by A's pin, X8 replays the run's selection


def test_without_an_accepted_list_a_config_change_asks_as_before(two_configs: dict[str, Any]) -> None:
    t = two_configs
    store, ra = t["store"], t["ra"]
    rb = annotate(store, t["pb"], "screen", t["corpus"], prior=ra)
    assert rb.counts["asked"] == 3 and "carried.accepted" not in rb.counts
    assert {r["config_digest"] for r in rows_of(store, rb, "screen")} == {_digest(t["pb"])}


def test_a_permanent_failure_keeps_the_accepted_answer_a_due_transient_is_retried(
        two_configs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from stack.llm import ContextTooLarge

    t = two_configs
    store, fake, ra = t["store"], t["fake"], t["ra"]
    monkeypatch.setitem(FAIL, sid(0), ContextTooLarge("fake: too large"))
    monkeypatch.setitem(FAIL, sid(1), TransportError("fake: upstream timed out"))
    plain = annotate(store, t["pb"], "screen", t["corpus"], prior=ra)  # B before the acceptance: 1 ok, 2 failures
    assert plain.counts["asked"] == 1
    pb = _accepting(t["pb"], plain.config_digest, [ra.config_digest])
    del FAIL[sid(1)]
    rb = annotate(store, pb, "screen", t["corpus"], prior=plain)
    assert rb.counts["asked"] == 1  # sid(1): the due transient, retried
    old, new = _by_doc(store, ra), _by_doc(store, rb)
    assert new[sid(0)] == old[sid(0)]  # the current key's permanent failure gives way to the accepted answer
    assert new[sid(1)]["config_digest"] == plain.config_digest and new[sid(1)]["attempts"] == 2
    assert new[sid(2)]["run_id"] == plain.run_id  # decided under B already: B's answer wins
    accepted_green(store)


def test_a_basis_change_is_not_crossed(two_configs: dict[str, Any]) -> None:
    t = two_configs
    store, fake, ra, p = t["store"], t["fake"], t["ra"], t["pb"]
    cfg_b = _digest(p)
    revise(store, 2, "Body of paper 2. Revised.", run=2)
    corpus2 = fold_corpus(store, p)
    rb = annotate(store, _accepting(p, cfg_b, [ra.config_digest]), "screen", corpus2, prior=ra)
    assert rb.counts["asked"] == 1 and rb.counts["carried.accepted"] == 2 and fake.count() == 4
    new = _by_doc(store, rb)
    assert new[sid(2)]["config_digest"] == cfg_b and new[sid(2)]["is_basis_current"]
    accepted_green(store)


def test_accepted_order_first_decided_config_wins(two_configs: dict[str, Any],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from stack.llm import ContextTooLarge

    t = two_configs
    store, fake, ra = t["store"], t["fake"], t["ra"]
    pa2 = _repinned(t["pa"], note="a2")
    monkeypatch.setitem(FAIL, sid(1), ContextTooLarge("fake: too large"))
    ra2 = annotate(store, pa2, "screen", t["corpus"], prior=ra)  # A2: docs 0, 2 decided, doc 1 failed
    del FAIL[sid(1)]
    cfg_b = _digest(t["pb"])
    rb = annotate(store, _accepting(t["pb"], cfg_b, [ra2.config_digest, ra.config_digest]), "screen",
                  t["corpus"], prior=ra2)
    assert "asked" not in rb.counts and rb.counts["carried.accepted"] == 3
    new = _by_doc(store, rb)
    assert [new[sid(i)]["config_digest"] for i in range(3)] == [ra2.config_digest, ra.config_digest,
                                                                 ra2.config_digest]
    accepted_green(store)


def test_target_names_and_recorded_compatible_configs_are_checked_at_stage_start(two_configs: dict[str, Any]) -> None:
    import dataclasses

    from stack.annotate.stage import PIN_DIGEST_KEYS
    from stack.checks.digests import H
    from stack.core import ids
    from stack.runs import write_final_record

    t = two_configs
    store, ra = t["store"], t["ra"]
    cfg_a, cfg_b = ra.config_digest, _digest(t["pb"])
    ins = [t["corpus"], *[ra.outputs[f]["path"] for f in ("screen", "screen_evidence")]]

    def refused(p: Any, why: str) -> None:
        rec = run_stage(store, p, "annotate.screen", ins, series=SERIES)
        assert rec.status == "failed" and why in (rec.error or ""), rec.error
        assert rec.counts.get("llm_calls", 0) == 0  # refused before any call

    refused(_accepting(t["pb"], "f" * 64, [cfg_a]), "is not the config its pin gives")  # a stale or mistyped target
    refused(_accepting(t["pb"], cfg_b, ["e" * 64]), "is no config this stage recorded")
    refused(_accepting(t["pb"], cfg_b, [cfg_a], name="screen-v6"), "is not the recorded config's 'screen-v7'")
    for key, value in (("registry_version", 99), ("produced", ["doc_class"])):
        assert ra.prompt_pin is not None
        pin = {**ra.prompt_pin, key: value, "plan_pin": {**ra.prompt_pin["plan_pin"], "v": key}}
        fake_run = dataclasses.replace(ra, run_id=ids.mint("run"), prompt_pin=pin, outputs={}, version=None)
        write_final_record(store, fake_run)
        refused(_accepting(t["pb"], cfg_b, [H({k: pin[k] for k in PIN_DIGEST_KEYS})]),
                f"has another {key} than the current config")


def test_config_name_tags_the_vendored_vocabularies() -> None:
    import hashlib

    from stack.annotate.stage import config_name

    tech = hashlib.sha256((REPO / "taxonomy" / "techniques-1.0.0.yaml").read_bytes()).hexdigest()
    assert config_name({"prompt_name": "technique-staged-v5-a1.1", "template_digests": ["t" * 64, tech]}) == \
        "technique-staged-v5-a1.1@techniques-1.0.0"
    assert config_name({"prompt_name": None, "plan_pin": {"name": "orgs-0.2.0"}, "template_digests": []}) == "orgs-0.2.0"


# ---------------------------------------------------------------- same basis, another supplier (D807)
def _two_suppliers(store: Path, i: int, lic_a: str, lic_b: str) -> dict[str, Any]:
    """Document ``i`` with its text from member A (its anchor, class ``lic_a``) and a second member B (an
    OpenAlex record of the same paper, class ``lic_b``) that supplies nothing yet."""
    row = doc_with_text(store, i, f"Body of paper {i}.")
    a = row["identity"]["members"][0]
    a["license_class"] = lic_a
    b = {**copy.deepcopy(a), "source_id": sid(100 + i), "channel": "openalex", "key_kind": "doi",
         "key_value": f"10.48550/arxiv.2610.{i:05d}", "license_class": lic_b, "edge_kind": "key:doi",
         "edge_to_source_id": row["doc_id"]}
    row["identity"]["members"].append(b)
    row["identity"]["member_ids"].append(b["source_id"])
    return with_digests(row)


def _supplied_by_b(store: Path, rows: list[dict[str, Any]], run: int) -> list[dict[str, Any]]:
    """An ``acquire`` part: each row's same text now comes from B's newer snapshot, which the pointer names."""
    out = []
    for row in rows:
        row = copy.deepcopy(row)
        c = row["content"]
        s0 = c["snapshots"][0]
        b = row["identity"]["members"][1]["source_id"]
        s1 = {**copy.deepcopy(s0), "snapshot_id": f"{rid(run)}.{row['doc_id'][-4:]}", "source_id": b,
              "captured_at": s0["captured_at"] + dt.timedelta(hours=1)}
        c["snapshots"].append(s1)
        c["current_snapshot_id"], c["current_source_id"] = s1["snapshot_id"], b
        row["run_id"] = c["run_id"] = rid(run)
        out.append(row)
    write_on_head(store, "documents", "documents", out, stage="acquire", run=run, blocks_written=["content"],
                  kind="part")
    return out


def test_an_unchanged_text_from_another_supplier_keeps_its_answer(env: dict[str, Any],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """D807 (B): the text's supplier moves from A to B with the same text. Doc 0 (open → open) keeps its answer
    (``carried.same_basis``, no call; the row keeps A's evidence and provenance); doc 1 (open → restricted, a
    stricter egress) is asked again; doc 2, whose answer under A failed, is asked again (failures are never
    reused). The store checks stay green (X8 replays the run's recorded ``same_basis``); without the flag in
    the run record X8 no longer reproduces the file."""
    from stack.llm import ContextTooLarge
    from stack.runs import find_run_record
    from tests.synth.records import rewrite_record

    store, fake = env["store"], env["fake"]
    verdicts(fake, 3)
    docs = [_two_suppliers(store, 0, "open", "open"), _two_suppliers(store, 1, "open", "restricted"),
            _two_suppliers(store, 2, "open", "open")]
    write_documents(store, docs, run=1)
    p = plan()
    monkeypatch.setitem(FAIL, sid(2), ContextTooLarge("fake: too large"))
    r1 = annotate(store, p, "screen", fold_corpus(store, p))
    s1 = {r["doc_id"]: r for r in rows_of(store, r1, "screen")}
    assert fake.count() == 2 and s1[sid(2)]["status"] == "failed_permanent"
    del FAIL[sid(2)]

    _supplied_by_b(store, docs, run=2)
    r2 = annotate(store, p, "screen", fold_corpus(store, p), prior=r1)
    s2 = {r["doc_id"]: r for r in rows_of(store, r2, "screen")}
    asked = {e["doc_id"] for e in rows_of(store, r2, "screen_evidence") if e["run_id"] == r2.run_id}
    assert r2.counts["carried.same_basis"] == 1 and asked == {sid(1), sid(2)}  # doc 1 may come from the cache
    assert fake.count(contains="Title: Paper 0\n") == 1
    kept = s2[sid(0)]
    assert {k: kept[k] for k in ("evidence_id", "source_id", "run_id", "status")} == {
        k: s1[sid(0)][k] for k in ("evidence_id", "source_id", "run_id", "status")}
    assert kept["source_id"] == sid(0) and kept["is_basis_current"]
    for i in (1, 2):
        assert s2[sid(i)]["source_id"] == sid(100 + i) and s2[sid(i)]["run_id"] == r2.run_id
        assert s2[sid(i)]["status"] == "ok"
    assert r2.selection is not None and r2.selection["same_basis"] is True
    all_green(store)

    # a run recorded before D807 selects without the rule: X8 replays it so, and here that does not reproduce
    rec = find_run_record(store, r2.run_id)
    assert rec is not None and rec.selection is not None
    rec.selection = {k: v for k, v in rec.selection.items() if k != "same_basis"}
    rewrite_record(store, rec)
    by = {r.check: r for r in run_checks(store)}
    assert by["selection_recompute"].status == "failed" and sid(0) in " ".join(by["selection_recompute"].items)


def test_a_failure_of_the_new_supplier_gives_way_to_a_same_basis_answer(env: dict[str, Any],
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """D807 (B): B, now the supplier again, has a due ``failed_transient`` answer on the same text and config (from
    an earlier spell as supplier); A's ``ok`` answer stands for it as ``select`` takes it, so nothing is asked."""
    store, fake = env["store"], env["fake"]
    verdicts(fake, 3)
    docs = [_two_suppliers(store, 0, "open", "open")]
    write_documents(store, docs, run=1)
    at_b = _supplied_by_b(store, docs, run=2)
    p = plan()
    monkeypatch.setitem(FAIL, sid(0), TransportError("fake: upstream timed out"))
    r1 = annotate(store, p, "screen", fold_corpus(store, p))
    assert [r["status"] for r in rows_of(store, r1, "screen")] == ["failed_transient"]
    del FAIL[sid(0)]

    def point_at(n: int, run: int) -> None:
        out = copy.deepcopy(at_b)
        for row in out:
            c, s_ = row["content"], row["content"]["snapshots"][n]
            c["current_snapshot_id"], c["current_source_id"] = s_["snapshot_id"], s_["source_id"]
            row["run_id"] = c["run_id"] = rid(run)
        write_on_head(store, "documents", "documents", out, stage="acquire", run=run, blocks_written=["content"],
                      kind="part")

    point_at(0, run=3)  # back to A: asked (B's failure is no answer to reuse)
    r2 = annotate(store, p, "screen", fold_corpus(store, p), prior=r1)
    (a_row,) = rows_of(store, r2, "screen")
    assert (a_row["source_id"], a_row["status"], fake.count()) == (sid(0), "ok", 1)
    point_at(1, run=4)  # B again, its failure due
    r3 = annotate(store, p, "screen", fold_corpus(store, p), prior=r2)
    assert r3.counts["carried.same_basis"] == 1
    assert not [e for e in rows_of(store, r3, "screen_evidence") if e["run_id"] == r3.run_id]  # nothing asked
    assert [r["evidence_id"] for r in rows_of(store, r3, "screen")] == [a_row["evidence_id"]]
    all_green(store)


def test_same_basis_row_reuses_only_decided_answers_for_a_no_stricter_supplier() -> None:
    """D807 (B), the lookup: same document, basis and config, another member still in the document, its class no
    looser than the new supplier's; the newest such decided row; a failure is never reused."""
    from stack.annotate.evidence import by_basis, same_basis_row

    def ev(n: int, source: str, status: str = "ok", at: int = 0) -> dict[str, Any]:
        return {"evidence_id": f"a{n}", "work_digest": f"w{n}", "doc_id": "d", "source_id": source,
                "basis_digest": "t", "config_digest": "c", "status": status, "supersedes": None,
                "decided_at": AT_RUN + dt.timedelta(hours=at)}

    lic = {"A": "restricted", "B": "open", "C": "api_tos", "N": "open"}
    idx = by_basis({e["work_digest"]: e for e in (ev(1, "A"), ev(2, "B", at=1), ev(3, "C", "failed_permanent", 2),
                                                  ev(4, "Z", at=3))})
    assert same_basis_row(idx, "d", "N", "t", "c", lic)["evidence_id"] == "a2"  # newest fitting; Z left the doc
    assert same_basis_row(idx, "d", "A", "t", "c", lic) is None  # B (open) is looser than restricted A
    assert same_basis_row(idx, "d", "C", "t", "c", lic)["evidence_id"] == "a1"  # tied with restricted; C failed
    assert same_basis_row(idx, "d", "N", "t", "other config", lic) is None
    assert same_basis_row(idx, "other doc", "N", "t", "c", lic) is None
    assert same_basis_row(idx, "d", "N", "t", "c", {**lic, "N": None})["evidence_id"] == "a1"  # NULL: restricted
