"""The effort-high test-set confirmation (evals/technique_staged/2026-10-28a): the frozen protocol names the
set's items, the scorer's unit rules (group nodes, zero roles, dont_care on both sides), the frozen decision rule at
its boundaries, and the scorer reproducing the judge set's committed production-vs-judge numbers (an independent
implementation, stack.evals.release_quality). No LLM, no store, no text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SET = REPO / "evals/technique_staged/2026-10-28a"
pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)


@pytest.fixture(scope="module")
def C():
    from stack.stage_runner import load_path_module

    return load_path_module("_confirm_28a", SET / "scripts/confirm.py")


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


def test_protocol_names_the_frozen_items():
    proto = json.loads((SET / "protocol.json").read_text("utf-8"))
    items = _jsonl(SET / "items.jsonl")
    assert proto["items_sha256"] == hashlib.sha256((SET / "items.jsonl").read_bytes()).hexdigest()
    man = json.loads((SET / "manifest.json").read_text("utf-8"))
    assert man["items_sha256"] == proto["items_sha256"]
    assert [(i["item_id"], i["doc_id"], i["text_sha256"]) for i in proto["items"]] == [
        (i["item_id"], i["doc_id"], i["input"]["text_digest"]) for i in items]
    judge = _jsonl(REPO / "evals/judge-2026-10-25j/technique_staged/items.jsonl")
    assert {(j["item_id"], j["doc_id"], j["input"]["text_sha256"]) for j in judge} == {
        (i["item_id"], i["doc_id"], i["text_sha256"]) for i in proto["items"]}
    assert proto["rule"]["confirm_iff"] == {"delta_f1_min": 0.02, "delta_precision_min": -0.03,
                                            "hall_median_latency_s_max_exclusive": 180}


def test_label_units_drop_groups_zero_roles_and_unknown(C):
    valid, groups, _ent = C.taxonomy()
    tech = sorted(valid - groups)[:2]
    group = sorted(groups)[0]
    label = {"techniques": [
        {"slug": tech[0], "roles": {"used": 1, "mentioned": 0}, "quotes": ["a", {"text": "b", "verified": True}]},
        {"slug": tech[1], "roles": {"used": 0}, "quotes": ["c"]},
        {"slug": group, "roles": {"used": 2}, "quotes": []},
        {"slug": "not-a-slug", "roles": {"used": 1}}]}
    from collections import Counter

    bad: Counter = Counter()
    assert C.label_units(label, valid, groups, bad, "x") == {tech[0]: ["a", "b"]}
    assert bad == {"x": 1}


def test_doc_counts_remove_dont_care_from_both_sides(C):
    ref = {"a": "in", "b": "in", "c": "dont_care"}
    assert C.doc_counts({"a", "c", "d"}, ref) == (1, 1, 1)  # tp a, fp d, fn b; c ignored
    assert C.doc_counts(set(), {"c": "dont_care"}) == (0, 0, 0)


def test_replicates_pool_by_averaging_counts(C):
    ref = {"d1": {"a": "in", "b": "in"}}
    pd = C.per_doc([{"d1": {"a"}}, {"d1": {"a", "b", "x"}}], ref, ["d1"])
    assert pd["d1"] == (1.5, 0.5, 0.5)
    assert C.total(pd, ["d1"])["f1"] == pytest.approx(3 / 4)


def _d(f1, lo, hi, p=0.0):
    return {"f1": {"d": f1, "lo": lo, "hi": hi}, "p": {"d": p, "lo": p, "hi": p}, "r": {"d": 0, "lo": 0, "hi": 0}}


@pytest.mark.parametrize("d, lat, judges, adv, confirms, reading", [
    (_d(0.02, 0.0, 0.04), 179.9, [0.01] * 4, 0.01, True, "robust"),
    (_d(0.02, 0.0, 0.04), 180.0, [0.01] * 4, 0.01, False, "refuted"),
    (_d(0.03, 0.0, 0.05, p=-0.031), 100, [0.01] * 4, 0.01, False, "refuted"),
    (_d(0.03, 0.0, 0.05, p=-0.03), 100, [0.01, 0.01, -0.001, 0.01], 0.01, True, "not robust"),
    (_d(0.03, 0.0, 0.05), 100, [0.01] * 4, -0.001, True, "not robust"),
    (_d(0.019, -0.01, 0.05), 100, [0.01] * 4, 0.01, False, "inconclusive"),
    (_d(0.01, -0.01, 0.019), 100, [0.01] * 4, 0.01, False, "refuted"),
])
def test_decision_rule_boundaries(C, d, lat, judges, adv, confirms, reading):
    rule = json.loads((SET / "protocol.json").read_text("utf-8"))["rule"]["confirm_iff"]
    v = C.decide(d, lat, rule, judges, adv)
    assert (v["confirms"], v["reading"]) == (confirms, reading)


def test_scorer_reproduces_the_committed_production_vs_judge_numbers(C):
    src = C.Sources({})
    committed = json.loads((REPO / "evals/judge-2026-10-25j/technique_staged/scores.json").read_text("utf-8"))
    sp = committed["scores"]["slug presence"]
    prod = src.slugs("PROD")
    for j in C.JUDGES:
        got = C.total(C.per_doc(prod, C.set_ref(src.judge_slugs(j)), src.items), src.items)
        want = sp[f"production_vs_{j}"]
        assert (got["tp"], got["fp"], got["fn"]) == (want["tp"], want["fp"], want["fn"])
        assert got["f1"] == pytest.approx(want["f1"], abs=5e-5)


def test_committed_reference_matches_its_scores():
    ref = SET / "results/reference-t.jsonl"
    if not ref.exists():
        pytest.skip("no reference committed yet")
    rows = _jsonl(ref)
    scores = json.loads((SET / "results/scores.json").read_text("utf-8"))
    from collections import Counter

    assert dict(Counter(r["verdict"] for r in rows)) == scores["reference"]["verdicts"]
    assert {(r["item_id"], r["slug"]) for r in rows if r["how"] == "3/3"} <= {
        (r["item_id"], r["slug"]) for r in rows if r["support"] == 3}
    assert scores["primary_reference"] == "ref-t"
