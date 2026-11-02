"""The release's quality numbers from its own build (stack/evals/release_quality.py, D728): re-scoring the judge eval
set against a build's production labels (drops, stratum re-assignment, the triage frame's N_h, the committed numbers
reproduced on a build that carries the frozen production labels) and triage's agreement with the screen.

The builds here are synthetic tables (:class:`BuildTables` built directly); the store loader (:func:`load_build`) and
the reproduction on the real build ws1qpn8myzxah run in :func:`test_rescore_reproduces_ws1_on_the_store` only when
``STACK_TEST_WS1_STORE`` names a local copy of that store (skipped otherwise)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from stack.evals import release_quality as rq

REPO = Path(__file__).resolve().parents[1]
EVAL = "evals/judge-2026-10-25j"
ROOT = REPO / EVAL
QD = "q" * 64
pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)


def _jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


def _doc(doc_id: str, verdict: str | None, conf: float | None, ev: str | None, *, status: str = "ok",
         error_class: str | None = None, title: str = "", state: str = "acquired", digest: str = QD) -> dict[str, Any]:
    q = {"question": rq.QUESTION, "status": status, "error_class": error_class, "verdict": verdict,
         "confidence": conf, "evidence_id": ev, "config_digest": digest}
    return {"doc_id": doc_id, "state": state, "metadata": {"title": title},
            "identity": {"former_doc_ids": []}, "content": {"same_raw_doc_ids": [], "current_text_sha256": None},
            "annotations": {"triage": {"payload": {"questions": [q]}}}}


def _ev(ev: str, doc_id: str, verdict: str, conf: float, basis: str, digest: str) -> dict[str, Any]:
    return {"evidence_id": ev, "doc_id": doc_id, "status": "ok", "verdict": verdict, "confidence": conf,
            "basis": basis, "basis_digest": digest, "prompt_name": "p"}


def _ann(status: str, digest: str, payload: Any, origin: str = "ai_check", ev: str | None = "e1") -> dict[str, Any]:
    return {"status": status, "origin": origin, "basis_digest": digest, "evidence_id": ev, "payload": payload}


def frozen_build() -> dict[str, Any]:
    """A build whose production labels are the frozen ``production.jsonl`` of every set (documents, evidence and
    corpus rows of the items' documents only), as Python rows to mutate before :func:`tables`."""
    docs: dict[str, dict[str, Any]] = {}
    evs: dict[str, dict[str, Any]] = {}
    for s in ("triage", "triage_w"):
        prod = {r["item_id"]: r for r in _jsonl(ROOT / s / "production.jsonl")}
        for it in _jsonl(ROOT / s / "items.jsonl"):
            p = prod[it["item_id"]]
            e = it["verification"]["evidence_id"]
            docs[it["doc_id"]] = _doc(it["doc_id"], p["label"]["verdict"], p["label"]["confidence"], e,
                                      title=it["metadata"]["title"] or "")
            evs[e] = _ev(e, it["doc_id"], p["label"]["verdict"], p["label"]["confidence"], it["basis"], f"b:{it['doc_id']}")
    corpus: dict[str, dict[str, Any]] = {}
    for s in rq.CORPUS_SETS:
        prod = {(r["item_id"], r["call"]): r for r in _jsonl(ROOT / s / "production.jsonl")}
        for it in _jsonl(ROOT / s / "items.jsonl"):
            row = corpus.setdefault(it["doc_id"], {"doc_id": it["doc_id"], "annotations": {},
                                                   "metadata": {"doc_type": it["metadata"]["collector_doc_type"]}})
            a, dig = row["annotations"], it["input"]["text_sha256"]
            if s == "screen":
                rel, qual = prod[(it["item_id"], "relevance")]["label"], prod[(it["item_id"], "quality")]["label"]
                a["screen"] = _ann("ok", dig, {"verdict": rel["verdict"]})
                a["stack_relevance"] = _ann("ok", dig, rel["stack"])
                a["sr_relevance"] = _ann("ok", dig, rel["sr"])
                a["text_usability"] = _ann("ok", dig, qual["usability"])
                a["doc_class"] = _ann("ok", dig, qual["doc_class"])
            else:
                p = prod[(it["item_id"], s)]
                a[s] = _ann(p.get("status") or "ok", dig, p["label"])
    return {"docs": docs, "evs": evs, "corpus": corpus}


def tables(b: dict[str, Any]) -> rq.BuildTables:
    return rq.BuildTables("bX", pa.Table.from_pylist(list(b["docs"].values())), "rd.documents",
                          pa.Table.from_pylist(list(b["evs"].values())), "re.triage_evidence", QD, "plan", True,
                          pa.Table.from_pylist(list(b["corpus"].values())))


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    return frozen_build()


@pytest.fixture(scope="module")
def frozen_scores(frozen: dict[str, Any]) -> dict[str, Any]:
    return rq.rescore(REPO, EVAL, tables(frozen))


# ---------------------------------------------------------------- reproduction
#: What re-scoring does not recompute (module docstring): the window splits (they read the texts or describe the
#: judged build's inputs) and score.py's normalisation repair count for Sol (the released labels' quotes are already
#: capped at 300 characters, so 13 of the 16 repairs the validated labels needed do not occur; the units are equal).
EXPECTED_ONLY_COMMITTED = ("_by_window", "_by_tier1_window", "window split (")
EXPECTED_DIFFER = ("scores > judge_normalisation > sol:repaired_docs",)


def _unexpected(diffs: dict[str, list[str]]) -> list[str]:
    return [d for ds in diffs.values() for d in ds
            if not (d.startswith("only in committed:") and any(x in d for x in EXPECTED_ONLY_COMMITTED))
            and not (d.startswith("differs:") and any(x in d for x in EXPECTED_DIFFER))]


def test_frozen_labels_reproduce_the_committed_scores(frozen_scores: dict[str, Any]) -> None:
    """Every number of the committed corpus sets' and the proportional triage set's scores.json, from a build that
    carries the frozen production labels (triage_w's N_h need the whole population: the store test checks them)."""
    diffs = rq.compare_committed(ROOT, {"sets": {s: v for s, v in frozen_scores["sets"].items() if s != "triage_w"}})
    assert _unexpected(diffs) == []
    assert any("_by_window" in d for d in diffs["screen"])  # the expected gaps are real, not an empty comparison
    assert all(c["scored"] == c["items"] and not c["dropped"] for c in frozen_scores["coverage"].values())
    tw = frozen_scores["sets"]["triage_w"]
    # the population here is the items' documents only: triage_w's own items (the proportional set's documents are
    # the five sets' family, excluded from triage_w's frame but in the pooled one)
    assert tw["design"]["strata"]["relevant"]["N_h"] == 30 and tw["design"]["strata"]["relevant"]["n_h"] == 30
    assert sum(d["pooled_N_h"] for d in tw["design"]["strata"].values()) == 300
    files = {f["path"] for f in frozen_scores["files"]}
    assert f"{EVAL}/model_roles/{rq.NORMALIZED_MR}" in files and f"{EVAL}/triage_w/labels-sol.jsonl" in files


@pytest.mark.slow
def test_a_changed_production_label_moves_the_numbers(frozen: dict[str, Any], frozen_scores: dict[str, Any]) -> None:
    """Red check of the reproduction: one production label changed in the build changes the re-scored numbers."""
    b = copy.deepcopy(frozen)
    it = next(i for i in _jsonl(ROOT / "technique_staged" / "items.jsonl"))
    b["corpus"][it["doc_id"]]["annotations"]["technique_staged"]["payload"]["techniques"] = []
    r = rq.rescore(REPO, EVAL, tables(b))
    k = ("slug presence", "production_vs_opus")
    assert r["sets"]["technique_staged"]["scores"][k[0]][k[1]] != frozen_scores["sets"]["technique_staged"]["scores"][k[0]][k[1]]


# ---------------------------------------------------------------- drops and strata
@pytest.mark.slow
def test_drops_by_reason_and_triage_w_restratified(frozen: dict[str, Any]) -> None:
    b = copy.deepcopy(frozen)
    w = _jsonl(ROOT / "triage_w" / "items.jsonl")
    rel = [it for it in w if it["stratum"] == "relevant"]
    # (1) missing; (2) a signal (D724: outside the question scope, decides nothing) is out of the population;
    # (3) re-asked on another basis: input changed; (4) re-asked on the same basis, now not_relevant at 0.99
    del b["docs"][rel[0]["doc_id"]]
    d = b["docs"][rel[1]["doc_id"]]["annotations"]["triage"]["payload"]["questions"][0]
    d.update(status="skipped", error_class="not_needed")
    for it, digest in ((rel[2], "b:changed"), (rel[3], f"b:{rel[3]['doc_id']}")):
        q = b["docs"][it["doc_id"]]["annotations"]["triage"]["payload"]["questions"][0]
        q.update(evidence_id=f"new-{it['item_id']}", verdict="not_relevant", confidence=0.99)
        b["evs"][f"new-{it['item_id']}"] = _ev(f"new-{it['item_id']}", it["doc_id"], "not_relevant", 0.99,
                                                it["basis"], digest)
    # (5) the documents row's verdict is not its evidence's: not scored (the row still counts in its stratum's N_h)
    b["docs"][rel[4]["doc_id"]]["annotations"]["triage"]["payload"]["questions"][0]["verdict"] = "borderline"
    # corpus sets: input changed, out of population (a human screen), missing
    scr = _jsonl(ROOT / "screen" / "items.jsonl")
    b["corpus"][scr[0]["doc_id"]]["annotations"]["screen"]["basis_digest"] = "x" * 64
    b["corpus"][scr[1]["doc_id"]]["annotations"]["screen"]["origin"] = "human"
    aff = _jsonl(ROOT / "affiliation" / "items.jsonl")
    del b["corpus"][aff[0]["doc_id"]]
    r = rq.rescore(REPO, EVAL, tables(b))
    cov = r["coverage"]
    assert cov["triage_w"] == {"items": 200, "scored": 196, "dropped": {"input_changed": 1, "missing": 1,
                                                                        "not_llm_decision": 1, "verdict_mismatch": 1}}
    assert cov["screen"]["dropped"] == {"input_changed": 1, "screen_origin_human": 1}
    assert cov["affiliation"]["dropped"] == {"missing": 1} and cov["triage"]["dropped"] == {}
    st = r["sets"]["triage_w"]["design"]["strata"]
    # the re-asked item moved from relevant to not_relevant|c0.99: n_h and N_h follow this build
    assert st["relevant"]["n_h"] == 25 and st["not_relevant|c0.99"]["n_h"] == 31
    # N_h counts the population: the input-changed document is in it (as not_relevant|c0.99), only not scored
    assert st["relevant"]["N_h"] == 25 and st["not_relevant|c0.99"]["N_h"] == 32
    assert st["borderline"]["N_h"] == 31 and st["borderline"]["n_h"] == 30
    assert st["relevant"]["weight"] == 1.0
    raw = r["sets"]["triage_w"]["per_stratum_raw"]
    assert raw["not_relevant|c0.99"]["n_h"] == 31
    assert r["sets"]["triage_w"]["estimates"]["triage_w"]["majority"]["n_items"] == 196


@pytest.mark.slow
def test_triage_frame_counts_the_population_with_the_exclusions(frozen: dict[str, Any]) -> None:
    b = copy.deepcopy(frozen)
    old_title = json.loads((ROOT / rq.EXISTING_KEYS).read_text())["titles"][0]
    for n in range(5):  # population: not_relevant at 0.97 (fields), one of them an earlier eval set's title
        e = f"fill-e{n}"
        b["docs"][f"fill{n}"] = _doc(f"fill{n}", "not_relevant", 0.97, e, title=old_title if n == 0 else f"t{n}")
        b["evs"][e] = _ev(e, f"fill{n}", "not_relevant", 0.97, "fields", f"b{n}")
    # out of the population: retired, a signal, a rule decision without evidence
    b["docs"]["r1"] = _doc("r1", "relevant", 0.9, "fill-e1", state="retired")
    b["docs"]["s1"] = _doc("s1", "relevant", 0.9, "fill-e2", status="skipped", error_class="not_needed")
    b["docs"]["u1"] = _doc("u1", None, None, None, status="skipped", error_class="not_needed")
    r = rq.rescore(REPO, EVAL, tables(b))
    st = r["sets"]["triage_w"]["design"]["strata"]["not_relevant|c0.95-0.98"]
    base = rq.rescore(REPO, EVAL, tables(frozen))["sets"]["triage_w"]["design"]["strata"]["not_relevant|c0.95-0.98"]
    assert st["N_h"] == base["N_h"] + 4 and st["pooled_N_h"] == base["pooled_N_h"] + 4
    assert r["triage_frame"]["population"] == 300 + 5
    assert r["triage_frame"]["excluded"]["in_existing_eval:title"] == 1


@pytest.mark.slow
def test_rescore_refuses_a_missing_normalised_file(tmp_path: Path) -> None:
    import shutil

    shutil.copytree(ROOT, tmp_path / EVAL, ignore=shutil.ignore_patterns("judge-normalized.jsonl"))
    with pytest.raises(rq.QualityError, match="judge-normalized.jsonl is missing"):
        rq.rescore(tmp_path, EVAL, tables(frozen_build()))
    with pytest.raises(rq.QualityError, match="not a directory"):
        rq.rescore(tmp_path, "evals/nowhere", tables(frozen_build()))


# ---------------------------------------------------------------- triage vs screen
def test_triage_vs_screen_counts_decisions_and_signals() -> None:
    docs, evs, corpus = [], [], []

    def add(d: str, verdict: str | None, screen: str | None, *, signal: bool = False, digest: str = QD,
            basis: str = "fields", conf: float = 0.9, screen_origin: str = "ai_check") -> None:
        e = f"e-{d}"
        docs.append(_doc(d, verdict, conf, e if verdict else None, status="skipped" if signal else "ok",
                         error_class="not_needed" if signal else None, digest=digest))
        if verdict:
            evs.append(_ev(e, d, verdict, conf, basis, "b"))
        if screen:
            corpus.append({"doc_id": d, "metadata": {"doc_type": "paper"},
                           "annotations": {"screen": _ann("ok", "t", {"verdict": screen}, origin=screen_origin)}})
    add("a", "relevant", "relevant")                   # decision tp
    add("b", "borderline", "irrelevant")               # decision fp
    add("c", "not_relevant", "relevant", signal=True)  # signal fn
    add("d", "not_relevant", "irrelevant", signal=True, basis="title", conf=0.99)  # signal tn
    add("e", "relevant", "borderline", signal=True)    # signal tp
    add("f", "relevant", "relevant", digest="old")     # an older prompt's verdict: not compared
    add("g", None, "relevant")                         # no verdict (a pre-D724 fill)
    add("h", "relevant", "relevant", screen_origin="human")  # a human screen: not a model screen
    add("i", "relevant", None)                         # not in the corpus
    bt = rq.BuildTables("bX", pa.Table.from_pylist(docs), "rd", pa.Table.from_pylist(evs), "re", QD, "p", True,
                        pa.Table.from_pylist(corpus))
    r = rq.triage_vs_screen(bt)
    assert r["overall"] == {"n": 5, "tp": 2, "fp": 1, "fn": 1, "tn": 1, "precision": 0.6667, "recall": 0.6667,
                                 "f1": 0.6667}
    assert r["by_kind"]["signal"]["n"] == 3 and r["by_kind"]["decision"]["n"] == 2
    assert r["by_basis"]["title"] == {"n": 1, "tp": 0, "fp": 0, "fn": 0, "tn": 1, "precision": None, "recall": None,
                                      "f1": None}
    assert list(r["by_confidence"]) == ["0.85-0.94", ">=0.99"]
    assert r["coverage"] == {"compared:decision": 2, "compared:signal": 3, "no_triage_verdict": 1,
                             "older_question_digest": 1, "screen_verdicts": 7}
    assert "not accuracy" in r["note"] and "not accuracy" in rq.triage_vs_screen_md(r)


# ---------------------------------------------------------------- the real build (local data only)
@pytest.mark.slow
def test_rescore_reproduces_ws1_on_the_store() -> None:
    root = os.environ.get("STACK_TEST_WS1_STORE")
    if not root:
        pytest.skip("STACK_TEST_WS1_STORE (a local copy of the ws1qpn8myzxah store) not set")
    from stack.fold import corpus_input
    from stack.store.index import store_index

    rec = store_index(Path(root)).build("ws1qpn8myzxah").record
    tbl, _h, _p = corpus_input(Path(root), rec["files"]["corpus"]["path"], fold_config="build")
    r = rq.rescore(REPO, EVAL, rq.load_build(Path(root), rec, tbl))
    assert _unexpected(rq.compare_committed(ROOT, r)) == []


def test_the_existing_eval_key_table_is_the_files_keys() -> None:
    # D795: the release reads triage_w's exclusion keys from a table, the files being partly archived. Red under: a
    # table that drifts from the files it pins (a file's sha256 or any key), or one that misses a sampled file.
    from tests.eval_texts import require_eval_files

    files = json.loads((ROOT / "triage_w" / "sampling.json").read_text())["existing_eval_files"]
    require_eval_files(REPO / "evals", [f.removeprefix("evals/") for f in files])
    assert json.loads((ROOT / rq.EXISTING_KEYS).read_text()) == rq.existing_keys_table(REPO, files)
