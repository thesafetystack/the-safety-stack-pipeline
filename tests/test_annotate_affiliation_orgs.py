"""``annotate.affiliation_orgs`` (draft) through the real stage runner: the fake ``annotate.affiliation`` of
``tests/fakes/annotate.py`` answers bylines, then the real, model-free ``affiliation_orgs`` stage resolves their
organisations with a toy vocabulary (``tests/fixtures/orgvocab/``).

Pinned: the payload per author and affiliation; origin ``computed`` and no LLM call; a rerun with its prior
carries every row; a changed affiliation answer recomputes exactly that document (the ``chained`` basis); a
vocabulary edit (another file in the pin) recomputes every document; the annotate checks stay green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import stack.annotate.stage as annotate_stage
from stack.checks.verify import run_checks
from stack.stage_runner import run_stage
from tests.fakes.annotate import (
    STAGES_ROOT,
    doc_with_text,
    fold_corpus,
    rows_of,
    write_documents,
    write_plan_file,
)
from tests.fakes.llm import FakeLlm, ok
from tests.synth.records import SERIES
from tests.synth.rows import sid

pytestmark = pytest.mark.slow  # runner-driven scenario store

VOCAB_A = "tests/fixtures/orgvocab/orgs-test-a.yaml"
VOCAB_B = "tests/fixtures/orgvocab/orgs-test-b.yaml"
BYLINES = {
    0: [{"name": "Ann", "orgs": ["Department of Computer Science, Example University, USA"]},
        {"name": "Bob", "orgs": ["Example Corp Research", "Example University"]}],
    1: [{"name": "Cy", "orgs": ["Independent Researcher"]}, {"name": "Di", "orgs": ["Sample Lab Institute"]}],
    2: [],
}


def plan_for(cwd: Path, vocab: str) -> Any:
    return write_plan_file(
        cwd, "affiliation-orgs-test",
        prompts={"annotate.affiliation_orgs": {"name": "orgs-test", "vocab": vocab}},
        stages=[{"stage": "fold.corpus"},
                {"stage": "annotate.affiliation", "prior": ["affiliation", "affiliation_evidence"],
                 "options": {"workers": 1}},
                {"stage": "annotate.affiliation_orgs", "prior": ["affiliation_orgs", "affiliation_orgs_evidence"],
                 "options": {"workers": 1}},
                {"stage": "fold.build"}],
        folded=["affiliation", "affiliation_orgs"]).model_copy(update={"series": SERIES})


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = FakeLlm()
    for i, authors in BYLINES.items():
        fake.on(f"Title: Paper {i}\n", ok({"authors": authors}))
    monkeypatch.setenv("STACK_WORKFLOWS_ROOT", str(STAGES_ROOT))
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.chdir(tmp_path)  # X6 finds the plan file by name under ./configs/plans
    store = tmp_path / "store"
    write_documents(store, [doc_with_text(store, i, f"Body of paper {i}.") for i in BYLINES], run=1)
    return {"store": store, "fake": fake, "cwd": tmp_path}


def orgs_run(store: Path, p: Any, corpus: str, aff: Any, prior: Any = None) -> Any:
    pri = [] if prior is None else [prior.outputs[f]["path"] for f in ("affiliation_orgs", "affiliation_orgs_evidence")]
    rec = run_stage(store, p, "annotate.affiliation_orgs", [corpus, aff.outputs["affiliation"]["path"], *pri],
                    series=SERIES, restart=prior is None and (store / "data" / "affiliation_orgs").exists())
    assert rec.status == "ok", rec.error
    return rec


def green(store: Path) -> None:
    results = run_checks(store)
    red = {r.check: r.items for r in results if r.status not in ("ok", "skipped")}
    assert red == {}, red


def test_orgs_resolved_carried_and_recomputed(env: dict[str, Any]) -> None:
    store, fake, cwd = env["store"], env["fake"], env["cwd"]
    p = plan_for(cwd, VOCAB_A)
    corpus = fold_corpus(store, p)
    aff = run_stage(store, p, "annotate.affiliation", [corpus], series=SERIES)
    assert aff.status == "ok", aff.error
    calls = fake.count()
    r1 = orgs_run(store, p, corpus, aff)
    assert fake.count() == calls  # no model
    rows = {r["doc_id"]: r for r in rows_of(store, r1, "affiliation_orgs")}
    d0 = rows[sid(0)]
    assert (d0["status"], d0["origin"]) == ("ok", "computed")
    assert [[(a["status"], a["org_ids"]) for a in it["affiliations"]] for it in d0["items"]] == [
        [("matched", ["example-university"])],
        [("matched", ["example-corp-research"]), ("matched", ["example-university"])]]
    assert d0["org_ids"] == ["example-university", "example-corp-research"]
    assert "root_org_ids" not in d0  # D737
    assert d0["vocab_version"] == "0.0.1"
    d1 = rows[sid(1)]
    assert [a["status"] for it in d1["items"] for a in it["affiliations"]] == ["non_org", "unmatched"]
    assert d1["org_ids"] == [] and d1["status"] == "ok"
    assert rows[sid(2)]["status"] == "empty"  # no author: affiliation is empty
    green(store)

    r2 = orgs_run(store, p, corpus, aff, prior=r1)  # nothing changed: carried
    assert r2.counts.get("carried") == 3 and "asked" not in r2.counts
    assert rows_of(store, r2, "affiliation_orgs") == rows_of(store, r1, "affiliation_orgs")

    pb = plan_for(cwd, VOCAB_B)  # a vocabulary edit: every document recomputed, doc 1 now matched
    r3 = orgs_run(store, pb, corpus, aff, prior=r2)
    assert r3.counts.get("asked") == 3 and fake.count() == calls
    rows3 = {r["doc_id"]: r for r in rows_of(store, r3, "affiliation_orgs")}
    assert rows3[sid(1)]["org_ids"] == ["example-university"]
    assert {r["config_digest"] for r in rows3.values()}.isdisjoint({r["config_digest"] for r in rows.values()})
    green(store)


def test_unknown_pin_key_and_absolute_vocab_are_refused(env: dict[str, Any]) -> None:
    from stack.stage_runner import load_path_module

    real = load_path_module("_affiliation_orgs__t", Path(__file__).resolve().parents[1] / "workflows" / "annotate"
                            / "affiliation_orgs" / "main.py")
    with pytest.raises(Exception, match="plan pin keys"):
        real.vocab_path({"vocab": VOCAB_A, "model": "x"})
    with pytest.raises(Exception, match="repository-relative"):
        real.vocab_path({"vocab": "/abs/orgs.yaml"})
