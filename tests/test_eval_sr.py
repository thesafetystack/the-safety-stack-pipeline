"""Track E2 (Shallow Review scope) in the eval harness, gold and scorer, on the fake model only
(``tests/test_eval_harness.py``'s tiny sets and CLI driver):

(1) screen rows carry ``scope`` / ``sr_category`` / ``sr_agenda`` of the final tier's answer and
``tier1_scope`` (null for a v8 prompt); (2) the gold file a run uses (``gold-v2.jsonl`` when the
set has it, ``--gold`` overrides, recorded in ``run.json``; ``score`` follows run.json, an old run
without the key re-scores to identical numbers on ``gold.jsonl``); (3) the screen scope metrics and
(4) the triage recall per gold scope and the recall-only set equal hand-computed values; (5)
``gold merge|finalize --version v2``: disagreements by field, sr_agenda rule, carry-over, new
items, the reference labels, agreement and transition reports, and v1 left byte-for-byte alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import tests.fakes.llm as fakes
from stack.evals import gold as G
from tests.fakes.llm import FakeLlm, ok, schema_invalid
from tests.test_eval_harness import (
    SET,
    TERRA,
    H,
    cli,
    read_jsonl,
    run_dirs,
    screen_set,
    triage_set,
    write_jsonl,
)
from workflows.annotate.screen import main as screen


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    f = FakeLlm()
    monkeypatch.setattr(fakes, "INSTALLED", f)
    return f


@pytest.fixture(autouse=True)
def v9_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """``screen-v9`` as the conductor wrote it, or a stand-in until the file exists."""
    if not (screen.HERE / "screen-v9.md").is_file():
        monkeypatch.setitem(screen._TEMPLATES, "screen-v9", (screen.HERE / "screen-v8.md").read_text("utf-8"))


def sr(verdict: str, scope: str, category: str | None = None, agenda: str | None = None,
       buckets: list[str] | None = None) -> Any:
    return ok({"verdict": verdict, "rationale": "r", "buckets": buckets or ["paper"], "quotes": [],
               "scope": scope, "sr_category": category, "sr_agenda": agenda})


def v8(verdict: str) -> Any:
    return ok({"verdict": verdict, "rationale": "r", "buckets": ["paper"], "quotes": []})


#: gold v2 of the screen set (Golf, Hotel, India, Juliet): verdicts as v1 gold, plus the scope fields.
SCREEN_GOLD_V2 = [("relevant", "core", "black_box"), ("irrelevant", "out", None), ("relevant", "sr_only", "theory"),
                  ("borderline", "core", "none")]


def write_screen_gold_v2(d: Path) -> None:
    v1 = read_jsonl(d / "gold.jsonl")
    write_jsonl(d / "gold-v2.jsonl", [
        {**g, "label": {**g["label"], "scope": s, "sr_category": c, "sr_agenda": None}}
        for g, (_, s, c) in zip(v1, SCREEN_GOLD_V2, strict=True)])


def script_screen_v9(fake: FakeLlm) -> None:
    fake.on("Title: Golf study", sr("relevant", "core", "black_box", "rlhf"), model=H)
    fake.on("Title: Hotel study", sr("borderline", "sr_only", "theory"), model=H)
    fake.on("Title: Hotel study", sr("irrelevant", "out", buckets=["offtopic"]), model=TERRA)
    fake.on("Title: India study", sr("irrelevant", "out", buckets=["offtopic"]), model=H)
    fake.on("Title: Juliet study", sr("borderline", "core", "labs", "new:x"), model=H)
    fake.on("Title: Juliet study", schema_invalid(), model=TERRA)  # tier 2 fails: tier 1's answer stands


def script_screen_v8(fake: FakeLlm) -> None:
    for t, v in (("Golf", "relevant"), ("Hotel", "irrelevant"), ("India", "irrelevant"), ("Juliet", "borderline")):
        fake.on(f"Title: {t} study", v8(v), model=H)
    fake.on("Title: Juliet study", v8("borderline"), model=TERRA)


def test_screen_rows_carry_final_tier_scope_and_metrics(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    write_screen_gold_v2(d)
    script_screen_v9(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v9", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    assert json.loads((run / "run.json").read_text())["gold"] == "gold-v2.jsonl"  # default: v2 when present
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    got = {i: (r["verdict"], r["scope"], r["sr_category"], r["sr_agenda"], r["tier1_scope"]) for i, r in rows.items()}
    assert got == {"scr-0001": ("relevant", "core", "black_box", "rlhf", "core"),
                   "scr-0002": ("irrelevant", "out", None, None, "sr_only"),   # tier 2's answer
                   "scr-0003": ("irrelevant", "out", None, None, "out"),
                   "scr-0004": ("borderline", "core", "labs", "new:x", "core")}  # tier 2 failed: tier 1's
    assert rows["scr-0004"]["tier2_error_class"] == "schema_invalid"
    s = json.loads((run / "summary.json").read_text())
    assert s["gold_file"] == "gold-v2.jsonl"
    sb = s["per_model"][H]["screen"]
    assert sb["accuracy"] == pytest.approx(3 / 4)
    sp = sb["scope"]
    # scope pairs (model, gold): core/core, out/out, out/sr_only, core/core
    assert (sp["scope_n"], sp["scope_accuracy"]) == (4, pytest.approx(3 / 4))
    assert sp["scope_kappa"] == pytest.approx(0.6)  # po .75, pe (2*2 + 2*1)/16
    assert sp["kept_recall_by_scope"] == {"core": {"n": 2, "kept_recall": 1.0}, "sr_only": {"n": 1, "kept_recall": 0.0}}
    assert (sp["sr_category_n"], sp["sr_category_accuracy"]) == (2, pytest.approx(1 / 2))  # Golf ok, Juliet labs≠none
    # SR export: gold {Golf, India}, model {Golf, Juliet}; Stack export: gold = model = {Golf, Juliet}
    assert (sp["sr_export"]["precision"], sp["sr_export"]["recall"]) == (0.5, 0.5)
    assert (sp["stack_export"]["precision"], sp["stack_export"]["recall"]) == (1.0, 1.0)
    md = (run / "summary.md").read_text()
    assert "### Shallow Review scope (gold v2)" in md and "50.0% / 50.0%" in md and "gold-v2.jsonl" in md


def test_v8_rows_have_null_scope_and_na_metrics(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    write_screen_gold_v2(d)
    script_screen_v8(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v8", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    rows = read_jsonl(run / "rows.jsonl")
    assert all(r["scope"] is None and r["sr_category"] is None and r["tier1_scope"] is None for r in rows)
    sb = json.loads((run / "summary.json").read_text())["per_model"][H]["screen"]
    sp = sb["scope"]
    assert sp["model_has_scope"] is False
    assert (sp["scope_accuracy"], sp["scope_kappa"], sp["sr_export"], sp["stack_export"]) == (None, None, None, None)
    assert sb["accuracy"] == pytest.approx(3 / 4)  # existing metrics as on v1 gold (same verdicts)
    assert "n/a" in (run / "summary.md").read_text().split("### Shallow Review scope")[1].split("###")[0]


def test_gold_file_choice_and_old_runs_rescore_identically(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    script_screen_v8(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v8", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    rj = json.loads((run / "run.json").read_text())
    assert rj["gold"] == "gold.jsonl"  # no gold-v2.jsonl in the set
    before = json.loads((run / "summary.json").read_text())
    # an old run (no "gold" key) after the set gained gold-v2.jsonl: still scored on gold.jsonl, same numbers
    del rj["gold"]
    (run / "run.json").write_text(json.dumps(rj), "utf-8")
    write_screen_gold_v2(d)
    cli_score(tmp_path, run)
    after = json.loads((run / "summary.json").read_text())
    assert after["gold_file"] == "gold.jsonl" and after["per_model"] == before["per_model"]
    assert "scope" not in after["per_model"][H]["screen"]
    cli_score(tmp_path, run, "--gold", "gold-v2.jsonl")  # override
    v2 = json.loads((run / "summary.json").read_text())
    assert v2["gold_file"] == "gold-v2.jsonl" and "scope" in v2["per_model"][H]["screen"]
    assert v2["per_model"][H]["screen"]["accuracy"] == before["per_model"][H]["screen"]["accuracy"]
    out = cli_score(tmp_path, run, "--gold", "gold-nope.jsonl", code=1)
    assert "gold-nope.jsonl" in out
    # a new run: the set has gold-v2.jsonl now; --gold picks another file; a missing one refuses before any call
    n = fake.count()
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v8", "--gold", "gold.jsonl")
    newest = max(run_dirs(tmp_path, "screen"), key=lambda p: p.stat().st_mtime)
    assert json.loads((newest / "run.json").read_text())["gold"] == "gold.jsonl"
    out = cli(tmp_path, "screen", "--set", SET, "--models", H, "--gold", "gold-nope.jsonl", code=1)
    assert "--gold" in out and fake.count() == n  # (the second run was all cache hits)


def cli_score(root: Path, run: Path, *args: str, code: int = 0) -> str:
    from typer.testing import CliRunner

    from stack.cli import app

    res = CliRunner().invoke(app, ["eval", "score", "--run", str(run), *args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


def tri(verdict: str) -> Any:
    return ok({"verdict": verdict, "confidence": 0.8, "reasoning": "r"})


def test_triage_recall_by_scope_and_recall_only_set(tmp_path: Path, fake: FakeLlm) -> None:
    d = triage_set(tmp_path)
    v1 = read_jsonl(d / "gold.jsonl")
    scopes = ["core", "sr_only", "out", None, "out", "sr_only"]  # Delta (None): carried from v1
    write_jsonl(d / "gold-v2.jsonl", [{**g, "label": {**g["label"], "scope": s}} for g, s in zip(v1, scopes, strict=True)])
    write_jsonl(d / "gold-recall.jsonl", [g for g in v1 if g["label"]["fetch"]])  # Alpha, Bravo, Delta, Foxtrot
    for t, v in (("Alpha", "relevant"), ("Bravo", "borderline"), ("Charlie", "not_relevant"), ("Delta", "relevant"),
                 ("Echo", "not_relevant"), ("Foxtrot", "not_relevant")):
        fake.on(f"Title: {t} ", tri(v), model=H)
    cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--workers", "1")
    (run,) = run_dirs(tmp_path)
    assert json.loads((run / "run.json").read_text())["gold"] == "gold-v2.jsonl"
    t = json.loads((run / "summary.json").read_text())["per_model"][H]["triage"]
    assert t["recall_only"] is False
    assert t["recall_by_scope"] == {
        "core": {"gold_fetch": 1, "recall_fetch": 1.0, "recall_fetch_rb": 1.0},
        "sr_only": {"gold_fetch": 2, "recall_fetch": 0.0, "recall_fetch_rb": 0.5},
        "null": {"gold_fetch": 1, "recall_fetch": 1.0, "recall_fetch_rb": 1.0}}
    assert t["all"]["precision_fetch"] == pytest.approx(1.0)
    assert "recall of gold fetch by gold scope" in (run / "summary.md").read_text()

    cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--gold", "gold-recall.jsonl")
    run2 = next(p for p in run_dirs(tmp_path) if p != run)
    t = json.loads((run2 / "summary.json").read_text())["per_model"][H]["triage"]
    a = t["all"]
    assert t["recall_only"] is True and a["n"] == 4
    assert (a["recall_fetch"], a["recall_fetch_rb"]) == (pytest.approx(2 / 4), pytest.approx(3 / 4))
    assert (a["precision_fetch"], a["precision_fetch_rb"], a["kappa3"]) == (None, None, None)
    assert a["not_applicable"] == ["precision_fetch", "precision_fetch_rb", "kappa3"]
    assert t["verdicts"] == {"borderline": 1, "not_relevant": 1, "relevant": 2}
    assert [x["item_id"] for x in t["skipped_gold_fetch"]] == ["tri-0002", "tri-0006"]
    md = (run2 / "summary.md").read_text()
    assert "precision and κ are not applicable" in md and "| not applicable |" in md


# ── gold v2 ──────────────────────────────────────────────────────────────────────────────────


def lab(iid: str, verdict: str, scope: str | None = None, **kw: Any) -> dict[str, Any]:
    return {"item_id": iid, "verdict": verdict, "confidence": 0.8, "rationale": f"{iid} {verdict}",
            "quote": "", **({"scope": scope} if scope is not None else {}), **kw}


def triage_gold_set(root: Path) -> Path:
    d = root / "triage" / "s"
    items = [{"item_id": f"t{n}", "doc_id": f"d{n}", "split": "dev", "input": {"record": f"record {n}"}}
             for n in range(1, 6)]
    write_jsonl(d / "items.jsonl", items)
    v1 = {"t1": "relevant", "t2": "borderline", "t3": "not_relevant", "t4": "relevant", "t5": "not_relevant"}
    write_jsonl(d / "labels" / "opus-1.jsonl", [lab(i, v) for i, v in v1.items()])
    write_jsonl(d / "labels" / "codex-1.jsonl", [lab(i, v) for i, v in v1.items()])
    return d


def test_gold_v2_triage_merge_finalize_reference_carry(tmp_path: Path) -> None:
    d = triage_gold_set(tmp_path)
    G.merge("triage", d)
    G.finalize("triage", d)
    v1_bytes = {p: (d / p).read_bytes() for p in ("gold.jsonl", "labels/disagreements.jsonl")}
    with (d / "items.jsonl").open("a") as fh:  # a new item, no v1 gold
        fh.write(json.dumps({"item_id": "t6", "doc_id": "d6", "split": "test", "input": {"record": "r6"}}) + "\n")
    L = d / "labels-v2"
    write_jsonl(L / "opus-1.jsonl", [lab("t1", "relevant", "core"), lab("t2", "borderline", "sr_only"),
                                     lab("t3", "not_relevant", "out"), lab("t5", "relevant", "core"),
                                     lab("t6", "not_relevant", "out")])
    write_jsonl(L / "codex-1.jsonl", [lab("t1", "relevant", "core"), lab("t2", "relevant", "sr_only"),
                                      lab("t3", "borderline", "sr_only"), lab("t5", "relevant", "sr_only"),
                                      lab("t6", "not_relevant", "out")])
    write_jsonl(L / "tomas-part1.jsonl", [{"item_id": "t1", "verdict": "borderline", "fetch": True, "note": "n1"},
                                          {"item_id": "t3", "verdict": "borderline", "fetch": True, "note": "n3"}])
    rep = G.merge_v2("triage", d)
    assert (rep["both"], rep["agree"], rep["disagree"], rep["unlabelled_carried"]) == (5, 2, 3, 1)
    assert rep["disagree_by_field"] == {"verdict": 2, "scope": 2}
    assert (rep["verdict"]["raw"], rep["verdict"]["kappa"]) == (pytest.approx(0.6), pytest.approx(0.375))
    assert (rep["scope"]["raw"], rep["scope"]["kappa"]) == (pytest.approx(0.6), pytest.approx(0.32 / 0.72))
    assert rep["v1_to_v2"]["opus"]["flips"] == 1 and rep["v1_to_v2"]["codex"]["flips"] == 3
    assert rep["v1_to_v2"]["codex"]["table"]["not_relevant->borderline"] == 1
    assert rep["v1_to_v2"]["agreed"] == {"n": 1, "flips": 0, "table": {"relevant->relevant": 1}}
    ref = rep["reference_tomas"]
    assert (ref["opus"]["verdict"]["agree"], ref["codex"]["verdict"]["agree"]) == (0, 1)
    assert (ref["opus"]["fetch"]["agree"], ref["codex"]["fetch"]["agree"]) == (1, 2)
    assert ref["would_be_gold"]["verdict"]["n"] == 1 and ref["would_be_gold"]["pending_adjudication"] == ["t3"]
    dis = {r["item_id"]: r["fields"] for r in read_jsonl(L / "disagreements.jsonl")}
    assert dis == {"t2": ["verdict"], "t3": ["verdict", "scope"], "t5": ["scope"]}

    with pytest.raises(SystemExit, match="t2"):  # disagreements need the adjudication
        G.finalize_v2("triage", d)
    write_jsonl(L / "adjudication.jsonl", [lab("t2", "relevant", "sr_only", adjudication_note="a2"),
                                           lab("t3", "borderline", "sr_only", adjudication_note="a3"),
                                           lab("t5", "relevant", "core", adjudication_note="a5")])
    rep = G.finalize_v2("triage", d)
    gold = {g["item_id"]: g for g in read_jsonl(d / "gold-v2.jsonl")}
    assert {i: (g["label"]["verdict"], g["label"]["fetch"], g["label"]["scope"]) for i, g in gold.items()} == {
        "t1": ("borderline", True, "core"),      # Tomáš's verdict, the labelers' scope
        "t2": ("relevant", True, "sr_only"), "t3": ("borderline", True, "sr_only"),
        "t4": ("relevant", True, None),          # carried from v1
        "t5": ("relevant", True, "core"), "t6": ("not_relevant", False, "out")}
    assert gold["t4"]["carried_from"] == "v1" and all("carried_from" not in g for i, g in gold.items() if i != "t4")
    assert gold["t1"]["reference"] == "tomas" and gold["t1"]["label_without_reference"]["verdict"] == "relevant"
    assert gold["t3"]["disagreement_fields"] == ["verdict", "scope"] and gold["t3"]["contested"]
    assert [x["who"] for x in gold["t3"]["labelers"]] == ["opus", "codex-gpt-6-sol", "adjudicator"]
    assert rep["v1_to_v2"]["flips"] == 4 and rep["new_items"] == 1 and rep["carried_from_v1"] == 1
    assert rep["reference_overrode_verdict"] == 1 and rep["reference_scope_conflicts"] == []
    assert rep["contested_by_field"] == {"verdict": 2, "scope": 2}
    # v1 untouched by v2 (and v1 finalize still reproduces it)
    assert {p: (d / p).read_bytes() for p in v1_bytes} == v1_bytes
    write_jsonl(d / "items.jsonl", read_jsonl(d / "items.jsonl")[:5])
    G.merge("triage", d)
    G.finalize("triage", d)
    assert {p: (d / p).read_bytes() for p in v1_bytes} == v1_bytes


def test_gold_v2_screen_sr_agenda_and_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    d = tmp_path / "evals" / "screen" / "s"
    write_jsonl(d / "items.jsonl", [{"item_id": f"s{n}", "doc_id": f"d{n}", "split": "dev",
                                     "input": {"title": "t", "text": "x"}} for n in (1, 2, 3)])
    L = d / "labels-v2"
    write_jsonl(L / "opus-1.jsonl", [
        lab("s1", "relevant", "core", sr_category="black_box", sr_agenda="a", buckets=["paper"]),
        lab("s2", "relevant", "sr_only", sr_category="theory", sr_agenda="x", buckets=["paper"]),
        lab("s3", "irrelevant", "out", sr_category=None, sr_agenda=None, buckets=["offtopic"])])
    write_jsonl(L / "codex-1.jsonl", [
        lab("s1", "relevant", "core", sr_category="white_box", sr_agenda="a", buckets=["paper"]),
        lab("s2", "relevant", "sr_only", sr_category="theory", sr_agenda="y", buckets=["paper", "critique_commentary"]),
        lab("s3", "borderline", "sr_only", sr_category="theory", sr_agenda="z", buckets=["paper"])])
    write_jsonl(L / "adjudication.jsonl", [
        lab("s1", "relevant", "core", sr_category="black_box", sr_agenda=None, buckets=["paper"]),
        lab("s3", "borderline", "sr_only", sr_category="theory", sr_agenda="new:z2", buckets=["paper"])])
    monkeypatch.setattr(G, "ROOT", tmp_path / "evals")
    monkeypatch.setattr(sys, "argv", ["gold", "merge", "screen", "--set", "s", "--version", "v2"])
    G.main()
    rep = json.loads(capsys.readouterr().out)
    assert rep["disagree_by_field"] == {"verdict": 1, "scope": 1, "sr_category": 2}
    assert rep["sr_category_both_kept"]["n"] == 2 and rep["sr_category_both_kept"]["agree"] == 1
    dis = {r["item_id"]: r["fields"] for r in read_jsonl(L / "disagreements.jsonl")}
    assert dis == {"s1": ["sr_category"], "s3": ["verdict", "scope", "sr_category"]}
    monkeypatch.setattr(sys, "argv", ["gold", "finalize", "screen", "--set", "s", "--version", "v2"])
    G.main()
    gold = {g["item_id"]: g for g in read_jsonl(d / "gold-v2.jsonl")}
    assert not (d / "gold.jsonl").exists()
    assert gold["s1"]["label"] == {"verdict": "relevant", "buckets": ["paper"], "scope": "core",
                                   "sr_category": "black_box", "sr_agenda": "a",  # adjudicator gave none: common
                                   "stack_verdict": "relevant", "sr_verdict": "relevant"}  # derived (D???(E))
    assert gold["s2"]["label"]["sr_agenda"] is None and gold["s2"]["sr_agenda_labels"] == {
        "opus": "x", "codex-gpt-6-sol": "y"}  # agreed item, agendas differ: null, both kept
    assert gold["s2"]["contested"] is False
    assert gold["s3"]["label"]["sr_agenda"] == "new:z2" and gold["s3"]["label"]["verdict"] == "borderline"


def test_scope_block_denominators() -> None:
    """Scope pairs only from answered rows with a scope; category accuracy only where both kept; a
    failed row and an answer without a scope are not members of either export."""
    from stack.evals.score import scope_block

    def row(i: str, status: str, verdict: str | None, scope: str | None, cat: str | None) -> dict[str, Any]:
        return {"item_id": i, "status": status, "verdict": verdict, "scope": scope, "sr_category": cat}

    rows = [row("a", "ok", "relevant", "core", "black_box"), row("b", "failed_permanent", None, None, None),
            row("c", "ok", "irrelevant", "out", "theory"), row("d", "ok", "relevant", None, None)]
    gold = {i: {"label": {"verdict": v, "scope": s, "sr_category": c}} for i, v, s, c in (
        ("a", "relevant", "core", "black_box"), ("b", "relevant", "sr_only", "theory"),
        ("c", "borderline", "sr_only", "theory"), ("d", "relevant", "core", "none"))}
    sp = scope_block(rows, gold)
    assert (sp["scope_n"], sp["scope_accuracy"]) == (2, 0.5)
    assert (sp["sr_category_n"], sp["sr_category_accuracy"]) == (1, 1.0)
    assert sp["kept_recall_by_scope"] == {"core": {"n": 2, "kept_recall": 1.0}, "sr_only": {"n": 2, "kept_recall": 0.0}}
    assert (sp["sr_export"]["precision"], sp["sr_export"]["recall"]) == (1.0, pytest.approx(1 / 3))
    assert (sp["stack_export"]["precision"], sp["stack_export"]["recall"]) == (1.0, 0.5)


# ── the two-question screen (D???(E)): per-question rows, gold, scoring, pair-screens ───────────

#: gold v2 of the screen set with the per-question verdicts as finalize derives them from
#: SCREEN_GOLD_V2 (Golf core/black_box, Hotel out, India sr_only, Juliet core/none).
GOLD_Q = {"scr-0001": ("relevant", "relevant"), "scr-0002": ("irrelevant", "irrelevant"),
          "scr-0003": ("irrelevant", "relevant"), "scr-0004": ("borderline", "irrelevant")}
QUOTE = "The exact phrase from the text is here"


def write_screen_gold_q(d: Path) -> None:
    write_screen_gold_v2(d)
    gold = read_jsonl(d / "gold-v2.jsonl")
    for g in gold:
        g["label"]["stack_verdict"], g["label"]["sr_verdict"] = GOLD_Q[g["item_id"]]
    gold[1]["label"]["sr_category"] = "theory"  # Hotel: a category on a gold R-irrelevant row (category n)
    write_jsonl(d / "gold-v2.jsonl", gold)


def dual(verdict: str, stack: tuple[str, float, list[str]], sr: tuple[str, float, list[str], str, str | None]) -> Any:
    return ok({"verdict": verdict, "rationale": "r", "buckets": ["paper"], "off_topic_reason": None,
               "stack": {"verdict": stack[0], "confidence": stack[1], "quotes": stack[2]},
               "sr": {"verdict": sr[0], "confidence": sr[1], "quotes": sr[2], "category": sr[3], "agenda": sr[4]}})


def one(verdict: str, conf: float, quotes: list[str], **sr: Any) -> Any:
    return ok({"verdict": verdict, "confidence": conf, "rationale": "r", "buckets": ["paper"], "quotes": quotes,
               "off_topic_reason": None, "grounding_idx": None, **sr})


def test_dual_rows_and_per_question_scores(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    write_screen_gold_q(d)
    fake.on("Title: Golf study", dual("relevant", ("relevant", 0.9, [QUOTE, "made up quote"]),
                                      ("relevant", 0.8, ["jailbreak robustness"], "black_box", "various_redteams")),
            model=H)
    fake.on("Title: Hotel study", dual("borderline", ("irrelevant", 0.7, ["not in the text at all"]),
                                       ("borderline", 0.4, ["red-teaming study"], "theory", None)), model=H)
    fake.on("Title: India study", dual("relevant", ("relevant", 0.6, []),
                                       ("relevant", 0.9, ["evaluation protocols"], "theory", None)), model=H)
    fake.on("Title: Juliet study", schema_invalid(), model=H)  # a parse failure on both questions
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v10", "--no-tier2", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    g = rows["scr-0001"]
    assert {k: g[k] for k in ("question_mode", "stack_verdict", "stack_confidence", "stack_quotes_returned",
                              "stack_quotes_kept", "sr_verdict", "sr_confidence", "sr_quotes_returned",
                              "sr_quotes_kept", "sr_category", "sr_agenda")} == {
        "question_mode": "dual", "stack_verdict": "relevant", "stack_confidence": 0.9, "stack_quotes_returned": 2,
        "stack_quotes_kept": 1, "sr_verdict": "relevant", "sr_confidence": 0.8, "sr_quotes_returned": 1,
        "sr_quotes_kept": 1, "sr_category": "black_box", "sr_agenda": "various_redteams"}
    assert (g["quotes_kept"], g["quotes_voided"]) == (2, 1)  # payload: stack then sr quotes, one voided
    assert rows["scr-0004"]["status"] != "ok" and rows["scr-0004"]["error_class"] == "schema_invalid"
    s = json.loads((run / "summary.json").read_text())["per_model"][H]["screen"]
    q = s["questions"]
    st, sr_ = q["stack"], q["sr"]
    # S: (model, gold) = (rel, rel), (irr, irr), (rel, irr), (no answer, bl)
    assert (st["rows"], st["answered"], st["parse_failures"]) == (4, 3, 1)
    assert (st["accuracy"], st["kappa"]) == (0.5, pytest.approx(1 / 3))
    assert (st["kept_recall"], st["kept_precision"], st["relevant_recall"], st["relevant_precision"]) == (
        0.5, 0.5, 1.0, 0.5)
    assert (st["quotes_per_kept"], st["verbatim_rate"], st["kept_quoted_share"]) == (1.0, pytest.approx(1 / 3), 0.5)
    assert (st["confidence_correct"], st["confidence_incorrect"]) == (pytest.approx(0.8), pytest.approx(0.6))
    assert st["category_n"] is None
    # R: (rel, rel), (bl, irr), (rel, rel), (no answer, irr)
    assert (sr_["accuracy"], sr_["kappa"]) == (0.5, pytest.approx(1 / 3))
    assert (sr_["kept_recall"], sr_["kept_precision"], sr_["relevant_recall"], sr_["relevant_precision"]) == (
        1.0, pytest.approx(2 / 3), 1.0, 1.0)
    assert (sr_["quotes_per_kept"], sr_["verbatim_rate"], sr_["kept_quoted_share"]) == (1.0, 1.0, 1.0)
    assert (sr_["confidence_correct"], sr_["confidence_incorrect"]) == (pytest.approx(0.85), pytest.approx(0.4))
    assert (sr_["category_n"], sr_["category_accuracy"]) == (2, 1.0)  # Hotel: gold does not keep R
    ov = q["overall"]
    assert (ov["accuracy"], ov["answered"], ov["parse_failures"]) == (0.5, 3, 1)
    assert ov["usd_per_1000_settled"] is not None and ov["latency_ms_p50_sequential"] == ov["latency_ms_p50_parallel"]
    assert s["questions_mode"] == "dual"
    md = (run / "summary.md").read_text()
    assert "### per question" in md and "| R (Shallow Review) |" in md and "100.0% (n 2)" in md


def test_v9_rows_derive_question_verdicts(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    write_screen_gold_q(d)
    script_screen_v9(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v9", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    got = {i: (r["question_mode"], r["stack_verdict"], r["sr_verdict"], r["stack_quotes_returned"],
               r["sr_confidence"]) for i, r in rows.items()}
    assert got == {"scr-0001": ("scope", "relevant", "relevant", None, None),       # core, black_box
                   "scr-0002": ("scope", "irrelevant", "irrelevant", None, None),   # tier 2: out
                   "scr-0003": ("scope", "irrelevant", "irrelevant", None, None),
                   "scr-0004": ("scope", "borderline", "borderline", None, None)}   # core, labs
    q = json.loads((run / "summary.json").read_text())["per_model"][H]["screen"]["questions"]
    assert q["stack"]["accuracy"] == 1.0 and q["sr"]["accuracy"] == 0.5
    assert q["stack"]["verbatim_rate"] is None and q["stack"]["confidence_correct"] is None


def test_v8_rows_fill_the_stack_side_only(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    write_screen_gold_q(d)
    script_screen_v8(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-v8", "--workers", "1")
    (run,) = run_dirs(tmp_path, "screen")
    rows = read_jsonl(run / "rows.jsonl")
    assert all(r["question_mode"] == "stack" and r["sr_verdict"] is None and r["stack_verdict"] == r["verdict"]
               for r in rows)
    q = json.loads((run / "summary.json").read_text())["per_model"][H]["screen"]["questions"]
    assert q["sr"]["applicable"] is False and q["sr"]["accuracy"] is None
    assert "| R (Shallow Review) | 4 | 0 | 0 | n/a |" in (run / "summary.md").read_text()


def script_single(fake: FakeLlm) -> None:
    fake.on("Title: Golf study", one("relevant", 0.9, [QUOTE]), model=H)
    fake.on("Title: Hotel study", one("irrelevant", 0.8, []), model=H)
    fake.on("Title: India study", one("borderline", 0.5, ["evaluation protocols"]), model=H)
    fake.on("Title: Juliet study", one("relevant", 0.7, ["TAIL-MARKER only the whole text carries"]), model=H)


def script_sr(fake: FakeLlm) -> None:
    fake.on("Title: Golf study", one("relevant", 0.8, ["jailbreak robustness"], category="black_box", agenda=None),
            model=H)
    fake.on("Title: Hotel study", one("irrelevant", 0.9, [], category="none", agenda=None), model=H)
    fake.on("Title: India study", one("relevant", 0.9, ["nope not here"], category="theory", agenda="x"), model=H)
    fake.on("Title: Juliet study", schema_invalid(), model=H)


def cli_pair(*args: str, code: int = 0) -> str:
    from typer.testing import CliRunner

    from stack.cli import app

    res = CliRunner().invoke(app, ["eval", "pair-screens", *args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


def test_pair_screens(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    write_screen_gold_q(d)
    script_single(fake)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-stack-v1", "--no-tier2", "--workers", "1")
    (run_s,) = run_dirs(tmp_path, "screen")
    fake2 = FakeLlm()
    fakes.INSTALLED = fake2
    script_sr(fake2)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--prompt", "screen-sr-v1", "--no-tier2", "--workers", "1")
    (run_r,) = [p for p in run_dirs(tmp_path, "screen") if p != run_s]
    rs = {r["item_id"]: r for r in read_jsonl(run_s / "rows.jsonl")}
    rr = {r["item_id"]: r for r in read_jsonl(run_r / "rows.jsonl")}
    assert rs["scr-0004"]["stack_quotes_kept"] == 1  # anchored against the whole text (beyond tier 1's cut)
    assert rs["scr-0001"]["stack_confidence"] == 0.9 and rs["scr-0001"]["confidence"] == 0.9  # payload confidence
    assert rs["scr-0001"]["sr_verdict"] is None and rr["scr-0001"]["stack_verdict"] is None
    assert (rr["scr-0003"]["sr_category"], rr["scr-0003"]["sr_agenda"]) == ("theory", "x")

    out = cli_pair("--stack-run", str(run_s), "--sr-run", str(run_r))
    assert "| S (Stack) |" in out and "written: " in out
    res_path = Path(out.strip().splitlines()[-1].removeprefix("written: "))
    assert res_path.is_file() and res_path.parent == run_s.parent
    from stack.evals import combine as comb

    res = comb.pair_screens(run_s, run_r, write=False)
    assert res["joined"] == 4 and res["gold_file"] == "gold-v2.jsonl"
    b = res["per_model"][H]
    st, sr_, ov = b["stack"], b["sr"], b["overall"]
    # S: (rel, rel), (irr, irr), (bl, irr), (rel, bl)
    assert (st["accuracy"], st["kept_recall"], st["kept_precision"]) == (0.5, 1.0, pytest.approx(2 / 3))
    assert (st["verbatim_rate"], st["quotes_per_kept"]) == (1.0, 1.0)
    # R: (rel, rel), (irr, irr), (rel, rel), (no answer, irr)
    assert (sr_["accuracy"], sr_["kept_recall"], sr_["kept_precision"], sr_["parse_failures"]) == (0.75, 1.0, 1.0, 1)
    assert (sr_["verbatim_rate"], sr_["category_n"], sr_["category_accuracy"]) == (0.5, 2, 1.0)
    assert st["parse_failures"] == 0
    # overall keep = the more inclusive: Golf rel, Hotel irr, India rel (bl|rel), Juliet rel (SR failed)
    assert (ov["accuracy"], ov["answered"], ov["parse_failures"]) == (0.75, 4, 1)
    want = 1000 * sum(rs[i]["call_cost_usd"] + rr[i]["call_cost_usd"] for i in rs) / 4
    assert ov["usd_per_1000_settled"] == pytest.approx(want)
    assert ov["usd_per_1000_settled"] > 1000 * sum(rs[i]["call_cost_usd"] for i in rs) / 4
    assert ov["usd_per_1000_nocache"] == pytest.approx(
        1000 * sum(rs[i]["cost_nocache_usd"] + rr[i]["cost_nocache_usd"] for i in rs) / 4)
    assert ov["latency_rows"] == 3  # Juliet's SR call failed: no latency pair
    # the wrong runs refuse loudly
    assert "question_mode" in cli_pair("--stack-run", str(run_r), "--sr-run", str(run_s), code=1)
    assert "stack_verdict" in cli_pair("--stack-run", str(run_s), "--sr-run", str(run_r), "--gold", "gold.jsonl", code=1)


def test_pair_overall_latency_and_inclusive() -> None:
    from stack.evals.combine import pair_overall

    def row(i: str, status: str, verdict: str | None, lat: int, cost: float = 0.001, hit: bool = False) -> dict[str, Any]:
        return {"item_id": i, "status": status, "verdict": verdict, "latency_ms": lat, "cache_hit": hit,
                "error_class": None if status == "ok" else "parse_error", "call_cost_usd": cost,
                "cost_nocache_usd": 2 * cost}

    gold = {i: {"label": {"verdict": v}} for i, v in (("a", "relevant"), ("b", "borderline"), ("c", "irrelevant"),
                                                       ("d", "irrelevant"))}
    pairs = [(row("a", "ok", "irrelevant", 100), row("a", "ok", "relevant", 300)),
             (row("b", "ok", "borderline", 200, hit=True), row("b", "ok", "irrelevant", 50)),
             (row("c", "failed_permanent", None, 0), row("c", "failed_permanent", None, 0)),
             (row("d", "ok", "irrelevant", 500), row("d", "ok", "irrelevant", 700, cost=0.003))]
    ov = pair_overall(pairs, gold)
    assert (ov["accuracy"], ov["answered"], ov["parse_failures"]) == (0.75, 3, 1)  # c: no answer
    assert ov["latency_rows"] == 2  # b has a cache hit, c failed
    assert (ov["latency_ms_p50_sequential"], ov["latency_ms_p50_parallel"]) == (800.0, 500.0)  # (400, 1200), (300, 700)
    assert ov["usd_per_1000_settled"] == pytest.approx(1000 * (0.002 * 3 + 0.004) / 4)
    assert ov["usd_per_1000_nocache"] == pytest.approx(2 * 1000 * (0.002 * 3 + 0.004) / 4)


def test_question_verdicts_derivation() -> None:
    qv = G.question_verdicts
    assert qv("relevant", "core", "black_box") == ("relevant", "relevant")
    assert qv("relevant", "core", "none") == ("relevant", "irrelevant")
    assert qv("borderline", "core", None) == ("borderline", "irrelevant")
    assert qv("relevant", "sr_only", "theory") == ("irrelevant", "relevant")
    assert qv("relevant", "sr_only", None) == ("irrelevant", "relevant")
    assert qv("irrelevant", "out", None) == ("irrelevant", "irrelevant")
    assert qv("relevant", None, "theory") == (None, None)  # carried from v1: unknown


def test_gold_v2_screen_per_question_and_reference(tmp_path: Path) -> None:
    d = tmp_path / "screen" / "s"
    write_jsonl(d / "items.jsonl", [{"item_id": f"s{n}", "doc_id": f"d{n}", "split": "dev",
                                     "input": {"title": "t", "text": "x"}} for n in (1, 2, 3, 4)])
    write_jsonl(d / "gold.jsonl", [{"item_id": "s4", "doc_id": "d4", "label": {"verdict": "relevant", "buckets": []}}])
    L = d / "labels-v2"
    labs = [lab("s1", "relevant", "core", sr_category="theory", sr_agenda=None, buckets=["paper"]),
            lab("s2", "relevant", "sr_only", sr_category="theory", sr_agenda=None, buckets=["paper"]),
            lab("s3", "relevant", "core", sr_category="none", sr_agenda=None, buckets=["paper"])]
    write_jsonl(L / "opus-1.jsonl", labs)
    write_jsonl(L / "codex-1.jsonl", labs)
    write_jsonl(L / "tomas-part2.jsonl", [
        {"item_id": "s1", "verdict": "borderline", "stack_verdict": "borderline", "sr_verdict": "irrelevant",
         "note": "n1"},
        {"item_id": "s4", "verdict": "irrelevant", "stack_verdict": "irrelevant", "sr_verdict": "irrelevant"}])
    rep = G.merge_v2("screen", d)  # the screen reference has no fetch: compared on its own fields
    ref = rep["reference_tomas"]
    assert set(ref["opus"]) == {"verdict", "stack_verdict", "sr_verdict"}
    assert (ref["opus"]["verdict"]["agree"], ref["opus"]["sr_verdict"]["agree"]) == (0, 0)
    assert ref["would_be_gold"]["stack_verdict"]["n"] == 1 and ref["without_v2_labels"] == ["s4"]
    rep = G.finalize_v2("screen", d)
    gold = {g["item_id"]: g for g in read_jsonl(d / "gold-v2.jsonl")}
    q = {i: (g["label"]["verdict"], g["label"]["stack_verdict"], g["label"]["sr_verdict"]) for i, g in gold.items()}
    assert q == {"s1": ("borderline", "borderline", "irrelevant"),   # the reference's three fields
                 "s2": ("relevant", "irrelevant", "relevant"), "s3": ("relevant", "relevant", "irrelevant"),
                 "s4": ("irrelevant", "irrelevant", "irrelevant")}  # carried from v1, the reference's
    w = gold["s1"]["label_without_reference"]
    assert (w["verdict"], w["stack_verdict"], w["sr_verdict"]) == ("relevant", "relevant", "relevant")
    assert gold["s1"]["reference"] == "tomas" and "fetch" not in gold["s1"]["label"]
    assert (gold["s4"]["label_without_reference"]["stack_verdict"], gold["s4"]["carried_from"]) == (None, "v1")
    assert rep["reference_tomas"] == 2 and rep["reference_overrode_verdict"] == 2
    assert "conductor_override" not in gold["s3"] and rep["conductor_overrides"] == {}
    # a conductor override (after adjudication, before the derivation; the reference still wins on verdicts)
    write_jsonl(L / "conductor-01.jsonl", [{"item_id": "s3", "sr_category": "labs", "note": "topic reading"},
                                          {"item_id": "s1", "sr_category": "none", "note": "n"}])
    rep = G.finalize_v2("screen", d)
    gold = {g["item_id"]: g for g in read_jsonl(d / "gold-v2.jsonl")}
    assert (gold["s3"]["label"]["sr_category"], gold["s3"]["label"]["sr_verdict"]) == ("labs", "relevant")
    assert gold["s3"]["conductor_override"] == {"note": "topic reading",
                                                "fields": {"sr_category": {"from": "none", "to": "labs"}}}
    assert rep["conductor_overrides"] == {"s1": {"sr_category": {"from": "theory", "to": "none"}},
                                          "s3": {"sr_category": {"from": "none", "to": "labs"}}}
    assert gold["s1"]["label"]["sr_verdict"] == "irrelevant"  # the reference's
    assert gold["s1"]["label_without_reference"]["sr_verdict"] == "irrelevant"  # derived after the override
    write_jsonl(L / "conductor-01.jsonl", [{"item_id": "s3", "verdict": "relevant", "note": "not allowed"}])
    with pytest.raises(SystemExit, match="allowed for screen"):
        G.finalize_v2("screen", d)
    (L / "conductor-01.jsonl").unlink()
    write_jsonl(L / "tomas-part2.jsonl", [{"item_id": "s1", "verdict": "borderline", "note": "no per-question"}])
    with pytest.raises(SystemExit, match="stack_verdict"):
        G.finalize_v2("screen", d)
