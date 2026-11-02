"""``stack eval technique_staged`` (techpipe D606-D607): the frozen set 2026-10-17a and its freeze script, the
harness adapter (pins, the input check, the per-repeat cache shared across arms, per-call accounting, standalone
cost, the estimate) with a scripted transport, and the scorer / analysis (:mod:`stack.evals.technique_staged`) on
hand-computed synthetic runs. No network. Each test names the mutation that turns it red.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tests.fakes.llm as fakes
from stack.cli import app
from stack.evals import annotators, harness
from stack.evals import technique_staged as ts
from stack.llm import LlmRequest, LlmResponse
from tests.eval_texts import eval_text_file, require_eval_texts
from tests.fakes.llm import UnscriptedCall, ok, prompt_text, schema_invalid

pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
SET = "2026-10-17a"
SET_DIR = REPO / "evals" / "technique_staged" / SET
SET_B_DIR = REPO / "evals" / "technique_staged" / "2026-10-17b"  # the audited gold revision (D610)
SET_18A_DIR = REPO / "evals" / "technique_staged" / "2026-10-18a"  # 17b's candidate rows remapped (D627)
SET_18B_DIR = REPO / "evals" / "technique_staged" / "2026-10-18b"  # gold v2, 120 documents (D630)
SET_18C_DIR = REPO / "evals" / "technique_staged" / "2026-10-18c"  # 18b with the reconciliation completed (D632)
PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
OVERLAY = REPO / "evals" / "egress-overlay.yaml"
LUNA = "openai/gpt-6-luna"
HAIKU = "anthropic/claude-haiku-5.5"
ITEM = "an-0001"  # a 1,063-character abstract: one P chunk
QUOTE = "We also conduct French/Spanish and HarmBench robustness checks."
runner = CliRunner()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows), "utf-8")


# ═════════════════════════════════════ the set and its freeze ═══════════════════════════════


def _freeze_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    require_eval_texts(SET_DIR, REPO / "evals" / "technique_roles" / "2026-10-13a")  # 17a's own texts and 2026-10-13a's
    monkeypatch.chdir(REPO)  # the script's ROOT is the cwd
    spec = importlib.util.spec_from_file_location("_ts_freeze_under_test", SET_DIR / "scripts" / "freeze.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.slow
def test_freeze_reproduces_the_set_byte_for_byte(monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: any change to freeze_info / the single user message / the split rule / the gold conversion
    # without a re-freeze, or a hand edit of items.jsonl, gold.jsonl or manifest.json.
    fz = _freeze_module(monkeypatch)
    assert fz.main(False) == 0
    assert "reproduced: items.jsonl, gold.jsonl, manifest.json byte for byte, 36 texts identical" in capsys.readouterr().out


def _copy_set(tmp: Path) -> Path:
    d = tmp / "set"
    shutil.copytree(SET_DIR, d, ignore=shutil.ignore_patterns("__pycache__"))
    return d


@pytest.mark.slow
def test_freeze_refuses_a_moved_text_and_an_unknown_gold_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: texts_and_meta not checking the staged text against the store digest (or the labellers' copy), or
    # build_gold not checking slugs against the taxonomy.
    fz = _freeze_module(monkeypatch)
    d = _copy_set(tmp_path)
    src = eval_text_file(SET_DIR, "an-0036")
    p = d / "texts" / "an-0036.txt.gz"  # the copy's own file is read before the cache's
    p.parent.mkdir(parents=True, exist_ok=True)
    text = gzip.decompress(src.read_bytes()).decode("utf-8")
    p.write_bytes(gzip.compress(text.replace("Codex", "Kodex", 1).encode("utf-8"), mtime=0))
    assert fz.main(False, out=d) == 1
    out = capsys.readouterr().out
    assert "refused: digest mismatch" in out and "an-0036: staged text does not hash to the store's text_digest" in out
    shutil.copyfile(src, p)
    rec = read_jsonl(d / "adjudication" / "reconciled.jsonl")
    rec[0]["techniques"][0]["slug"] = "no-such-technique"
    write_jsonl(d / "adjudication" / "reconciled.jsonl", rec)
    assert fz.main(False, out=d) == 1
    assert "gold slugs not in the v0.9 taxonomy (techniques + group nodes): an-0001:no-such-technique" in \
        capsys.readouterr().out


def test_gold_counts_labellers_and_the_documented_agreement() -> None:
    # Red under: candidate rows not flagged, a labeller mapping that differs from the labelling log, or a README
    # agreement figure that the shipped adjudication files do not reproduce.
    gold = read_jsonl(SET_DIR / "gold.jsonl")
    rows = [t for g in gold for t in g["label"]["techniques"]]
    assert (len(gold), len(rows)) == (36, 406)
    cand = [t for t in rows if t.get("candidate")]
    assert len(cand) == 8 and all(t["slug"].startswith("candidate:") for t in cand)
    assert not any(t["slug"].startswith("candidate:") for t in rows if not t.get("candidate"))
    assert sum({"A", "B"} <= set(t["sources"]) for t in rows) == 297
    assert sum(t["confidence"] == "high" for t in rows) == 150
    labs = [g["labelers"][1] for g in gold]
    assert (labs.count("opus-b"), labs.count("codex-sol"), labs.count("codex-astra")) == (3, 14, 19)  # Sol B1, B3, B5, B7 (17) less the 3 abstract items; Astra B2, B4, B6, B8
    assert all(g["labelers"][0] == "opus-a" and g["adjudicator"] == "opus-reconciler" for g in gold)
    a = {r["item_id"]: {t["slug"] for t in r["techniques"]} for r in read_jsonl(SET_DIR / "adjudication" / "adj-A.jsonl")}
    b = {r["item_id"]: {t["slug"] for t in r["techniques"]} for r in read_jsonl(SET_DIR / "adjudication" / "adj-B.jsonl")}
    tp = sum(len(a[i] & b[i]) for i in a)
    fn = sum(len(a[i] ^ b[i]) for i in a)
    assert round(2 * tp / (2 * tp + fn), 3) == 0.796
    assert "A-vs-B slug agreement F1 0.796" in (SET_DIR / "adjudication" / "README.md").read_text("utf-8")


def test_set_files_carry_no_workspace_paths_store_names_or_personal_names() -> None:
    bad = re.compile(r"/home/|/tmp/|(?i:mimir)|impl/|stack-data-|\bTom[aá]š?\b|Gaven")
    for f in sorted([*SET_DIR.rglob("*"), *SET_B_DIR.rglob("*"), *SET_18A_DIR.rglob("*"),
                     *SET_18B_DIR.rglob("*"), *SET_18C_DIR.rglob("*")]):
        if f.is_file() and f.suffix in (".md", ".py", ".json", ".jsonl", ".sha256"):
            hits = bad.findall(f.read_text("utf-8"))
            assert not hits, (f.name, hits[:5])


def _derive_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    require_eval_texts(SET_DIR, SET_B_DIR)
    monkeypatch.chdir(REPO)  # the script's ROOT is the cwd
    spec = importlib.util.spec_from_file_location("_ts_derive_under_test", SET_B_DIR / "scripts" / "derive.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_17b_is_derived_byte_for_byte_with_the_audit_rules(monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: a hand edit of 17b's gold / manifest, items or texts that drift from 17a, or a rule applied wrongly
    # (a pool-A "no" row kept, a borderline not flagged dont_care, a pool-B "no" added, a pool-C row touched).
    dv = _derive_module(monkeypatch)
    assert dv.main(False) == 0
    assert "reproduced: items.jsonl, gold.jsonl, manifest.json byte for byte, 36 texts identical" in capsys.readouterr().out
    a = {g["item_id"]: {t["slug"]: t for t in g["label"]["techniques"]} for g in read_jsonl(SET_DIR / "gold.jsonl")}
    b = {g["item_id"]: {t["slug"]: t for t in g["label"]["techniques"]} for g in read_jsonl(SET_B_DIR / "gold.jsonl")}
    audit = SET_B_DIR / "adjudication" / "audit"
    ans = dv.verdicts(audit)
    seen = Counter()
    for k in read_jsonl(audit / "key.jsonl"):
        v, i, s = ans[k["id"]]["verdict"], k["item"], k["slug"]
        seen[(k["pool"], v)] += 1
        if k["pool"] == "C" or (k["pool"] == "A" and v == "yes"):
            assert b[i][s] == a[i][s]
        elif k["pool"] == "A":
            assert (s not in b[i]) if v == "no" else (b[i][s]["dont_care"] and b[i][s]["audit"]["id"] == k["id"])
        elif v == "no":
            assert s not in b[i]
        else:
            row = b[i][s]
            assert row["confidence"] == "med" and row["source"] == "blind-audit-2026-10-17" and row["quote"]
            assert bool(row.get("dont_care")) == (v == "borderline") and row["roles"]
    assert sum(seen.values()) == 90 and seen[("A", "no")] == 5 and seen[("B-staged", "yes")] == 13
    rows = [t for g in b.values() for t in g.values()]
    assert (len(rows), sum(bool(t.get("dont_care")) for t in rows)) == (431, 15)
    man = json.loads((SET_B_DIR / "manifest.json").read_text("utf-8"))
    assert (man["gold"]["scored_rows"], man["gold"]["scored_high"]) == (388, 147)
    assert man["items_sha256"] == json.loads((SET_DIR / "manifest.json").read_text("utf-8"))["items_sha256"]
    d = man["derivation"]
    assert (d["rows_removed"], d["rows_dont_care"], d["rows_added"], d["rows_added_dont_care"]) == (5, 8, 23, 7)


def test_revision_17b_refuses_an_audit_item_that_does_not_fit_the_17a_gold(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: build_gold not checking that a pool-B slug is absent from the 17a gold, or a drifted text accepted.
    dv = _derive_module(monkeypatch)
    d = tmp_path / "rev"
    shutil.copytree(SET_B_DIR, d, ignore=shutil.ignore_patterns("__pycache__"))
    key = read_jsonl(d / "adjudication" / "audit" / "key.jsonl")
    b = next(k for k in key if k["pool"] == "B-staged")
    b["slug"] = next(t["slug"] for g in read_jsonl(SET_DIR / "gold.jsonl") if g["item_id"] == b["item"]
                     for t in g["label"]["techniques"] if not t.get("candidate"))
    write_jsonl(d / "adjudication" / "audit" / "key.jsonl", key)
    assert dv.main(False, out=d) == 1
    assert "is already in the 17a gold" in capsys.readouterr().out
    shutil.copyfile(SET_B_DIR / "adjudication" / "audit" / "key.jsonl", d / "adjudication" / "audit" / "key.jsonl")
    (d / "texts").mkdir(parents=True, exist_ok=True)  # the copy's own file is read before the cache's
    (d / "texts" / "an-0001.txt.gz").write_bytes(gzip.compress(b"moved", mtime=0))
    assert dv.main(False, out=d) == 1
    assert "differs: texts/an-0001.txt.gz" in capsys.readouterr().out


def _derive_18a(monkeypatch: pytest.MonkeyPatch) -> Any:
    require_eval_texts(SET_B_DIR, SET_18A_DIR)
    monkeypatch.chdir(REPO)
    spec = importlib.util.spec_from_file_location("_ts_derive_18a_under_test", SET_18A_DIR / "scripts" / "derive.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_18a_remaps_the_candidate_rows_and_nothing_else(monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: a hand edit of 18a's gold / manifest, items or texts that drift from 17b, a candidate row left (or
    # its flag kept), a remapped row without remapped_from, any other field touched, or the manifest naming the
    # 214-entry taxonomy (the scorer would read the wrong group nodes and links).
    dv = _derive_18a(monkeypatch)
    assert dv.main(False) == 0
    out = capsys.readouterr().out
    assert "reproduced: items.jsonl, gold.jsonl, manifest.json byte for byte, 36 texts identical" in out
    b = read_jsonl(SET_B_DIR / "gold.jsonl")
    c = read_jsonl(SET_18A_DIR / "gold.jsonl")
    assert [g["item_id"] for g in b] == [g["item_id"] for g in c]
    remapped = []
    for gb, gc in zip(b, c, strict=True):
        assert {k: v for k, v in gb.items() if k != "label"} == {k: v for k, v in gc.items() if k != "label"}
        assert ({k: v for k, v in gb["label"].items() if k != "techniques"}
                == {k: v for k, v in gc["label"].items() if k != "techniques"})
        for tb, tc in zip(gb["label"]["techniques"], gc["label"]["techniques"], strict=True):
            if tb.get("candidate"):
                assert tc == {**{k: v for k, v in tb.items() if k != "candidate"},
                              "slug": tb["slug"].removeprefix("candidate:"), "remapped_from": tb["slug"]}
                remapped.append((gc["item_id"], tc["slug"]))
            else:
                assert tc == tb
    assert len(remapped) == 8 and ("an-0035", "honesty-lying-eval") in remapped
    man = json.loads((SET_18A_DIR / "manifest.json").read_text("utf-8"))
    assert man["items_sha256"] == json.loads((SET_B_DIR / "manifest.json").read_text("utf-8"))["items_sha256"]
    assert man["frozen_with"]["taxonomy_path"] == "taxonomy/taxonomy-v0.9-234.yaml"
    assert man["frozen_with"]["taxonomy_sha256"] == hashlib.sha256(
        (REPO / "taxonomy" / "taxonomy-v0.9-234.yaml").read_bytes()).hexdigest()
    g = man["gold"]
    assert (g["candidate_rows"], g["remapped_rows"], g["scored_rows"], g["scored_high"]) == (0, 8, 396, 148)
    assert man["derivation"]["duplicates"] == []
    ctx = ts.load_context(SET_18A_DIR)
    assert "honesty-lying-eval" in ctx.kind and len(ctx.links) == 53


def test_revision_18a_refuses_a_candidate_the_taxonomy_lacks_and_keeps_the_higher_confidence_duplicate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: build_gold accepting a candidate slug outside the 234-entry file, or the duplicate rule keeping the
    # lower-confidence row (or both rows).
    dv = _derive_18a(monkeypatch)
    src = tmp_path / "17b"
    shutil.copytree(SET_B_DIR, src, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(dv, "SRC", src)
    gold = read_jsonl(src / "gold.jsonl")
    row = next(t for g in gold for t in g["label"]["techniques"] if t.get("candidate"))
    row["slug"] = "candidate:no-such-entry"
    write_jsonl(src / "gold.jsonl", gold)
    assert dv.main(False, out=tmp_path / "out") == 1
    assert "candidate row 'candidate:no-such-entry' names no technique" in capsys.readouterr().out
    gold = read_jsonl(SET_B_DIR / "gold.jsonl")
    g = next(g for g in gold if g["item_id"] == "an-0035")
    low = next(t for t in g["label"]["techniques"] if t["slug"] == "candidate:persuasion-capability-eval")
    assert low["confidence"] == "low"
    g["label"]["techniques"].append({**low, "slug": "persuasion-capability-eval", "candidate": False,
                                     "confidence": "med", "note": "plain"})
    write_jsonl(src / "gold.jsonl", gold)
    out, derivation = dv.build_gold(*dv.taxonomy()[:2])
    rows = [t for t in next(x for x in out if x["item_id"] == "an-0035")["label"]["techniques"]
            if t["slug"] == "persuasion-capability-eval"]
    assert len(rows) == 1 and rows[0]["confidence"] == "med" and "remapped_from" not in rows[0]
    assert "kept over a duplicate row candidate:persuasion-capability-eval" in rows[0]["note"]
    assert derivation["duplicates"] == [{"item": "an-0035", "slug": "persuasion-capability-eval",
                                         "kept": "persuasion-capability-eval",
                                         "dropped": "candidate:persuasion-capability-eval"}]


# ═════════════════════════════════════ the harness adapter ══════════════════════════════════

P_MARK = "You are the reading step of a pipeline"
S_MARK = "You are the selection step of a pipeline"
G_MARK = "Earlier\nsteps of this pipeline proposed a short list of CANDIDATE"
SWEEP_MARK = "# READING NOTES (pointers, not evidence)"
SINGLE_MARK = "against a fixed catalogue"
MARKS = (("prepass", P_MARK), ("select", S_MARK), ("grade", G_MARK), ("sweep", SWEEP_MARK), ("single", SINGLE_MARK))


class Router:
    """A scripted transport answering by call type (the system part each prompt file sends)."""

    def __init__(self, replies: dict[str, LlmResponse | list[LlmResponse]]) -> None:
        self.replies = {k: (v if isinstance(v, list) else [v]) for k, v in replies.items()}
        self.calls: list[str] = []
        self.models: list[tuple[str, str]] = []  # (call type, requested model) per request
        self.cost = 0.0

    def post(self, request: LlmRequest) -> LlmResponse:
        text = prompt_text(request)
        call = next((c for c, m in MARKS if m in text), None)
        if call is None or call not in self.replies:
            raise UnscriptedCall(f"no reply for {call}")
        served = self.calls.count(call)
        self.calls.append(call)
        self.models.append((call, request.model))
        reply = self.replies[call][min(served, len(self.replies[call]) - 1)]
        self.cost += float((reply.body.get("usage") or {}).get("cost") or 0.0)
        return reply


P_REPLY = ok({"mentions": [{"paras": [1], "surface": "HarmBench robustness checks", "gloss": "jailbreak robustness",
                            "stance_hint": "used", "implicit": False}], "overflow": False}, cost=0.001)
S_REPLY = ok({"mentions": [{"id": "m1", "candidates": [{"slug": "jailbreak-robustness-eval", "confidence": "high"}],
                            "none": None}], "doc_level": []}, cost=0.002)


def g_reply(cost: float = 0.004, rejected: bool = False) -> LlmResponse:
    body: dict[str, Any] = {
        "doc_kind": "paper", "flags": [], "extra_techniques": [],
        "sweeps": {"deployment_claims": [], "recommendations": [], "controls_discussed": [], "menu_gaps": []},
        "techniques": [{"slug": "jailbreak-robustness-eval", "roles": {"mentioned": 2, "used": 2}, "quotes": [QUOTE]}]}
    if rejected:
        body["rejected"] = []
    return ok(body, cost=cost)


def install(monkeypatch: pytest.MonkeyPatch, router: Router) -> Router:
    monkeypatch.setattr(fakes, "INSTALLED", router)
    return router


def cli(root: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--plan", str(PLAN), "eval", *args, "--evals", str(root / "evals"),
                              "--out", str(root / "out"), "--overlay", str(OVERLAY)])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


def copy_set(root: Path, ids: list[str]) -> Path:
    require_eval_texts(SET_DIR)
    d = root / "evals" / "technique_staged" / SET
    write_jsonl(d / "items.jsonl", [i for i in read_jsonl(SET_DIR / "items.jsonl") if i["item_id"] in ids])
    write_jsonl(d / "gold.jsonl", [g for g in read_jsonl(SET_DIR / "gold.jsonl") if g["item_id"] in ids])
    (d / "texts").mkdir(parents=True, exist_ok=True)
    for i in ids:
        shutil.copyfile(eval_text_file(SET_DIR, i), d / "texts" / f"{i}.txt.gz")
    shutil.copyfile(SET_DIR / "manifest.json", d / "manifest.json")
    return d


def adapter(pins: dict[str, Any], set_dir: Path = SET_DIR, models: list[str] | None = None) -> Any:
    from stack.annotate.stage import stack_config

    a = harness.make_adapter("technique_staged", stack_config(), None, prompt=None, models=models or [LUNA],
                             pin_overrides=pins)
    a.set_dir = set_dir
    return a


def run_dirs(root: Path) -> list[Path]:
    base = root / "out" / "technique_staged" / SET
    return sorted((p for p in base.iterdir() if (p / "rows.jsonl").is_file()), key=lambda p: p.stat().st_mtime_ns)


@pytest.mark.parametrize("mode", ["single", *(pytest.param(m, marks=pytest.mark.slow) for m in ("sweep", "staged", "lexical"))])
def test_input_check_passes_on_the_real_set_in_every_mode(mode: str) -> None:
    # Red under: TechniqueStagedAdapter.frozen_problems comparing the wrong freeze_info field for a mode, the
    # adapter building another AskItem text, or production moving the frozen messages without a re-freeze.
    require_eval_texts(SET_DIR)
    a = adapter({"mode": mode})
    items = harness.select_items(harness.load_set(REPO / "evals", "technique_staged", SET).items, "all", None, None)
    assert len(items) == 36
    harness.check_inputs(a, items)
    assert a.checks_user_message()


def test_call_types_are_told_apart_for_every_final_schema() -> None:
    # Red under: stage_of knowing only main.SINGLE_SCHEMA (prompt set v3's single call sends INVENTORY_SCHEMA).
    a = adapter({"mode": "single"})
    m = a.main
    assert a.stage_of({"schema": m.INVENTORY_SCHEMA}) == a.stage_of({"schema": m.SINGLE_SCHEMA}) == "single"
    assert a.stage_of({"schema": m.G_SCHEMA}) == "grade"
    with pytest.raises(harness.EvalError, match="none of main.SCHEMAS"):
        a.stage_of({"schema": dict(m.SINGLE_SCHEMA)})  # identity, not equality


def test_input_check_refuses_moved_inputs(tmp_path: Path) -> None:
    # Red under: frozen_problems skipping the P chunk digests for sweep (the second refusal does not happen).
    d = copy_set(tmp_path, [ITEM])
    items = read_jsonl(d / "items.jsonl")
    items[0]["input"]["staged"]["user_digests"]["prepass"] = ["0" * 64]
    with pytest.raises(harness.EvalError, match=r"an-0001 \(prepass user message\(s\) differ"):
        harness.check_inputs(adapter({"mode": "sweep"}, d), items)
    harness.check_inputs(adapter({"mode": "single"}, d), items)  # single does not read the P digests
    items[0]["input"]["staged"]["lexical_digest"] = "0" * 64
    with pytest.raises(harness.EvalError, match=r"an-0001 \(lexical_digest"):
        harness.check_inputs(adapter({"mode": "staged"}, d), items)


def test_pins_are_validated_before_any_call() -> None:
    # Red under: TechniqueStagedAdapter not refusing --pin model, an unpriced stage model, or a mode-less pin.
    with pytest.raises(harness.EvalError, match="mode"):
        adapter({})
    with pytest.raises(harness.EvalError, match="--models"):
        adapter({"mode": "single", "model": LUNA})
    with pytest.raises(harness.EvalError, match=r"stage models \['openai/no-such-model'\] have no llm.yaml pricing"):
        adapter({"mode": "sweep", "prepass_model": "openai/no-such-model"})
    with pytest.raises(harness.EvalError, match="not used by mode 'single'"):
        adapter({"mode": "single", "prepass_model": LUNA})
    a = adapter({"mode": "sweep", "prepass_model": HAIKU})
    assert a.pin(LUNA).prepass_model == HAIKU and a.mode == "sweep"
    with pytest.raises(harness.EvalError, match="only stack eval screen, technique_staged, model_roles take plan-pin overrides"):
        harness.make_adapter("technique_roles", a.config, None, prompt=None, models=[LUNA],
                             pin_overrides={"mode": "x"})


def test_max_tokens_pin_sets_the_estimate_reservation_and_the_arm(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: the estimate reserving main.MAX_TOKENS whatever the pin says, or the pin not entering the arm's cache
    # directory (a 64k arm would then read the 16k arm's answers).
    copy_set(tmp_path, [ITEM])
    install(monkeypatch, Router({"single": g_reply()}))
    base = ["technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=single", "--max-usd", "1", "--estimate"]
    res = []
    for extra in ([], ["--pin", "max_tokens=64000"]):
        m = re.search(r"reservations \$([0-9.]+)", cli(tmp_path, *base, *extra))
        assert m is not None
        res.append(float(m.group(1)))
    price = 0.5 / 1e6  # gpt-6-luna output, llm.yaml
    assert res[1] - res[0] == pytest.approx((64_000 - 16_000) * price, abs=1e-4)
    a16, a64 = adapter({"mode": "single"}), adapter({"mode": "single", "max_tokens": 64_000})
    assert (a16.pin(LUNA).max_tokens, a64.pin(LUNA).max_tokens) == (16_000, 64_000)
    assert a16.arm_slug(LUNA) != a64.arm_slug(LUNA)


def test_gold_check_refuses_an_unknown_slug_but_not_a_candidate_row(tmp_path: Path) -> None:
    # Red under: gold_ids reading candidate rows (the real gold then refuses) or skipping real slugs.
    a = adapter({"mode": "single"})
    gold = {g["item_id"]: g for g in read_jsonl(SET_DIR / "gold.jsonl")}
    assert a.gold_problems(gold) == []
    gold["an-0001"]["label"]["techniques"][0]["slug"] = "renamed-slug"
    assert a.gold_problems(gold) == ["an-0001:renamed-slug"]


def test_arms_share_p_within_a_repeat_and_not_across_repeats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under (each): P calls in the arm's own cache (the staged run then re-asks P: router prepass count 2 after
    # step 2); one shared cache for all repeats (repeat 2's P is then a hit); standalone cost leaving out cache-served
    # calls (0.006 instead of 0.007); per-call records missing stage / from_response_cache.
    copy_set(tmp_path, [ITEM])
    router = install(monkeypatch, Router({"prepass": P_REPLY, "select": S_REPLY, "grade": g_reply(rejected=True),
                                          "sweep": g_reply()}))
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=sweep", "--max-usd", "1")
    (sweep_dir,) = run_dirs(tmp_path)
    (row,) = read_jsonl(sweep_dir / "rows.jsonl")
    assert row["status"] == "ok" and [c["stage"] for c in row["calls"]] == ["prepass", "sweep"]
    assert row["cost_usd"] == pytest.approx(0.005) and row["standalone_cost_usd"] == pytest.approx(0.005)
    assert router.calls == ["prepass", "sweep"]

    out = cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=staged", "--max-usd", "1",
              "--estimate")
    assert re.search(r"prepass\s+calls\s+1\s+cached\s+1", out)  # the estimate sees the shared P answer
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=staged", "--max-usd", "1")
    staged_dir = run_dirs(tmp_path)[-1]
    (row,) = read_jsonl(staged_dir / "rows.jsonl")
    assert router.calls == ["prepass", "sweep", "select", "grade"]  # P served from the shared cache
    calls = {c["stage"]: c for c in row["calls"]}
    assert calls["prepass"]["from_response_cache"] and calls["prepass"]["cache"] == "shared"
    assert calls["prepass"]["cost_usd"] == 0.0 and calls["prepass"]["call_cost_usd"] == pytest.approx(0.001)
    assert calls["select"]["cache"] == "shared" and not calls["select"]["from_response_cache"]
    assert calls["grade"]["cache"] == "arm" and calls["grade"]["prompt_tokens"] == 120
    assert row["cost_usd"] == pytest.approx(0.006) and row["standalone_cost_usd"] == pytest.approx(0.007)
    assert row["payload"]["techniques"][0]["slug"] == "jailbreak-robustness-eval"

    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=staged", "--max-usd", "1",
        "--repeat", "2")
    rows = read_jsonl(run_dirs(tmp_path)[-1] / "rows.jsonl")
    r1, r2 = (next(r for r in rows if r["repeat"] == k) for k in (1, 2))
    assert r1["cache_hit"] and r1["cost_usd"] == 0.0 and r1["standalone_cost_usd"] == pytest.approx(0.007)
    assert not any(c["from_response_cache"] for c in r2["calls"]) and r2["cost_usd"] == pytest.approx(0.007)
    assert router.calls.count("prepass") == 2
    cache = tmp_path / "out" / "technique_staged" / SET / "cache"
    assert sorted(p.name for p in (cache / "shared").iterdir()) == ["r1", "r2"]
    assert sorted(p.name.split("-")[1] for p in cache.iterdir() if p.name.startswith("arm-")) == ["staged", "sweep"]
    summary = (run_dirs(tmp_path)[-1] / "summary.md").read_text("utf-8")
    assert "stack eval technique_staged" in summary and "union" in summary


def test_a_failed_selection_costs_what_was_spent_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: accounting reading the error's cost at row time (the stage overwrote it with the ask's total, so the
    # P call is counted twice) instead of the CallRecorder's snapshot.
    copy_set(tmp_path, [ITEM])
    router = install(monkeypatch, Router({"prepass": P_REPLY, "select": schema_invalid(cost=0.002),
                                          "grade": g_reply(rejected=True)}))
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=staged", "--max-usd", "1")
    (row,) = read_jsonl(run_dirs(tmp_path)[-1] / "rows.jsonl")
    assert row["status"].startswith("failed") and row["error_class"] == "schema_invalid"
    assert router.calls.count("select") >= 1 and "grade" not in router.calls
    assert row["cost_usd"] == pytest.approx(router.cost)
    assert row["standalone_cost_usd"] == pytest.approx(router.cost)
    assert [c["status"] for c in row["calls"]][0] == "ok" and row["calls"][-1]["error_class"] == "schema_invalid"


SONNET = "anthropic/claude-sonnet-5.5"


def staged_router(monkeypatch: pytest.MonkeyPatch) -> Router:
    return install(monkeypatch, Router({"prepass": P_REPLY, "select": S_REPLY, "grade": g_reply(rejected=True),
                                        "sweep": g_reply()}))


def last_row(root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    d = run_dirs(root)[-1]
    (row,) = read_jsonl(d / "rows.jsonl")
    return row, json.loads((d / "run.json").read_text("utf-8")), d


def row_models(row: dict[str, Any]) -> list[tuple[str, str, bool]]:
    return [(c["stage"], c["model"], c["from_response_cache"]) for c in row["calls"]]


def test_stage_models_default_to_the_graded_model_not_another_arms_cached_answers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: the shared P/S client looking up the role's fallback models in the cache (a luna arm's P and S
    # answers then serve the haiku arm: router sees only the grade call), or stage models defaulting to the role's
    # primary instead of --models. run.json / summary must name the effective per-stage models.
    copy_set(tmp_path, [ITEM])
    router = staged_router(monkeypatch)
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=staged", "--max-usd", "1")
    assert router.models == [("prepass", LUNA), ("select", LUNA), ("grade", LUNA)]
    cli(tmp_path, "technique_staged", "--set", SET, "--models", HAIKU, "--pin", "mode=staged", "--max-usd", "1")
    assert router.models[3:] == [("prepass", HAIKU), ("select", HAIKU), ("grade", HAIKU)]
    row, run, d = last_row(tmp_path)
    assert row_models(row) == [("prepass", HAIKU, False), ("select", HAIKU, False), ("grade", HAIKU, False)]
    assert run["stage_models"] == {HAIKU: {"prepass": HAIKU, "select": HAIKU, "grade": HAIKU}}
    summary = (d / "summary.md").read_text("utf-8")
    assert "## staged claude-haiku-5.5 P=claude-haiku-5.5 S=claude-haiku-5.5" in summary
    assert "observed in calls: prepass claude-haiku-5.5 x1; select claude-haiku-5.5 x1; grade claude-haiku-5.5 x1" \
        in summary
    assert "**warning**" not in summary
    # the identical command again: every call from the haiku entries, still haiku
    cli(tmp_path, "technique_staged", "--set", SET, "--models", HAIKU, "--pin", "mode=staged", "--max-usd", "1")
    assert len(router.models) == 6
    row, _, _ = last_row(tmp_path)
    assert row_models(row) == [("prepass", HAIKU, True), ("select", HAIKU, True), ("grade", HAIKU, True)]


def test_a_prepass_model_pin_is_honoured_in_sweep_and_isolated_in_the_shared_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: the shared cache serving the luna arm's P answer to the haiku P request (the run book's arm order:
    # luna sweep first; the haiku-P arm then sends only the sweep call), --pin prepass_model dropped (P sent with
    # luna), or a haiku P entry answering a luna request.
    copy_set(tmp_path, [ITEM])
    router = staged_router(monkeypatch)
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=sweep", "--max-usd", "1")
    assert router.models == [("prepass", LUNA), ("sweep", LUNA)]
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=sweep",
        "--pin", f"prepass_model={HAIKU}", "--max-usd", "1")
    assert router.models[2:] == [("prepass", HAIKU), ("sweep", LUNA)]
    row, run, d = last_row(tmp_path)
    assert row_models(row) == [("prepass", HAIKU, False), ("sweep", LUNA, False)]
    assert run["stage_models"] == {LUNA: {"prepass": HAIKU, "sweep": LUNA}}
    assert "## sweep gpt-6-luna P=claude-haiku-5.5" in (d / "summary.md").read_text("utf-8")
    # both arms again: each from its own entries, nothing sent
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=sweep", "--max-usd", "1")
    row, _, _ = last_row(tmp_path)
    assert row_models(row) == [("prepass", LUNA, True), ("sweep", LUNA, True)]
    cli(tmp_path, "technique_staged", "--set", SET, "--models", LUNA, "--pin", "mode=sweep",
        "--pin", f"prepass_model={HAIKU}", "--max-usd", "1")
    row, _, _ = last_row(tmp_path)
    assert row_models(row) == [("prepass", HAIKU, True), ("sweep", LUNA, True)]
    assert len(router.models) == 4


def test_explicit_prepass_and_select_pins_route_each_stage_to_its_model(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: pin_of / the adapter swapping prepass_model and select_model, or the grading call taking a stage
    # model; the label and run.json must carry all three.
    copy_set(tmp_path, [ITEM])
    router = staged_router(monkeypatch)
    cli(tmp_path, "technique_staged", "--set", SET, "--models", SONNET, "--pin", "mode=staged",
        "--pin", f"prepass_model={HAIKU}", "--pin", f"select_model={LUNA}", "--max-usd", "1")
    assert router.models == [("prepass", HAIKU), ("select", LUNA), ("grade", SONNET)]
    row, run, d = last_row(tmp_path)
    assert row_models(row) == [("prepass", HAIKU, False), ("select", LUNA, False), ("grade", SONNET, False)]
    assert run["stage_models"] == {SONNET: {"prepass": HAIKU, "select": LUNA, "grade": SONNET}}
    assert "## staged claude-sonnet-5.5 P=claude-haiku-5.5 S=gpt-6-luna" in (d / "summary.md").read_text("utf-8")


def test_scoring_warns_when_calls_ran_on_another_model_than_the_pinned_one(tmp_path: Path) -> None:
    # Red under: summarize not comparing calls[].model with the effective stage models (a run made before the
    # shared-cache fix, P served by luna under a haiku pin, scores silently). Old runs carry no stage_models: the
    # pins and the documented defaults (stage models = the graded model) stand in.
    calls = [{"stage": "prepass", "model": LUNA}, {"stage": "select", "model": LUNA}, {"stage": "grade", "model": HAIKU}]
    rows = [_row(g["item_id"], calls=calls) for g in read_jsonl(SET_DIR / "gold.jsonl")]
    d = tmp_path / "old"
    d.mkdir()
    run = {"run_id": "eval-technique_staged-old", "task": "technique_staged", "set": SET, "set_dir": str(SET_DIR),
           "split": "all", "models": [HAIKU], "mode": "staged",
           "pins": {HAIKU: {"mode": "staged", "model": HAIKU, "name": "technique-staged-v1"}}}
    assert ts.stage_models(run, HAIKU) == {"prepass": HAIKU, "select": HAIKU, "grade": HAIKU}
    s = ts.summarize(run, [{**r, "model": HAIKU} for r in rows], {}, SET_DIR, None)
    assert any("prepass ran on openai/gpt-6-luna x36" in w and "pinned anthropic/claude-haiku-5.5" in w
               for w in s["warnings"])
    assert "**warning**" in s["markdown"]
    (d / "run.json").write_text(json.dumps(run), "utf-8")
    write_jsonl(d / "rows.jsonl", [{**r, "model": HAIKU} for r in rows])
    table = ts.results_table([d])
    assert any("staged claude-haiku-5.5 P=claude-haiku-5.5 S=claude-haiku-5.5 [old]: prepass ran on openai/gpt-6-luna"
               in w for w in table["warnings"]) and "**warning**" in table["markdown"]
    assert ts.MODE_CALLS == {m: list(c) for m, c in annotators.workflow_module("technique_staged").CALLS.items()}


def test_cli_pin_takes_bare_tokens_and_json() -> None:
    # Red under: _pins requiring JSON for strings (mode=sweep refused) or turning numbers into strings.
    from stack.cli.eval import _pins

    assert _pins(["mode=sweep", "prepass_model=anthropic/claude-haiku-5.5", "tier1_chars=8000", 'name="x"']) == {
        "mode": "sweep", "prepass_model": HAIKU, "tier1_chars": 8000, "name": "x"}


# ═════════════════════════════════════ the scorer ═══════════════════════════════════════════


def _ctx(shares: dict[str, float] | None = None) -> ts.Context:
    gold = {
        "X": {"item_id": "X", "doc_kind": "paper", "label": {"techniques": [
            {"slug": "a", "roles": {"mentioned": 3, "used": 3}, "sources": ["A", "B"], "confidence": "high"},
            {"slug": "b", "roles": {"mentioned": 1}, "sources": ["A"], "confidence": "low"},
            {"slug": "g", "roles": {"mentioned": 2}, "sources": ["A", "B"], "confidence": "high"},
            {"slug": "candidate:z", "roles": {"mentioned": 2}, "sources": ["A"], "confidence": "med",
             "candidate": True}]}},
        "Y": {"item_id": "Y", "doc_kind": "blogpost", "label": {"techniques": [
            {"slug": "c", "roles": {"mentioned": 2, "evaluated": 2}, "sources": ["A", "B"], "confidence": "med"}]}}}
    items = {"X": {"item_id": "X", "stratum": {"channel_group": "paper"}, "input": {"full_text_chars": 5_000}},
             "Y": {"item_id": "Y", "stratum": {"channel_group": "url"}, "input": {"full_text_chars": 70_000}}}
    return ts.Context(gold=gold, items=items, kind={"a": "intervention", "b": "monitoring", "c": "assessment",
                                                    "d": "assessment"},
                      groups=frozenset({"g"}), shares=shares or {"<20k": 0.2, "20-60k": 0.0, "60-100k": 0.8,
                                                                  ">100k": 0.0}, shares_name="test")


def _row(item: str, status: str = "ok", repeat: int = 1, techniques: list[dict[str, Any]] | None = None,
         **kw: Any) -> dict[str, Any]:
    payload = {"techniques": techniques or [], "flags": kw.pop("flags", []), "repairs": kw.pop("repairs", []),
               "prepass": kw.pop("prepass", None), "mode": kw.pop("mode", "single")}
    return {"item_id": item, "repeat": repeat, "status": status, "model": LUNA,
            "payload": payload if status == "ok" else None, "cost_usd": kw.pop("cost_usd", 0.0),
            "standalone_cost_usd": kw.pop("standalone_cost_usd", 0.0), "latency_ms": kw.pop("latency_ms", 0),
            "standalone_latency_ms": kw.pop("standalone_latency_ms", 0), "calls": kw.pop("calls", []), **kw}


def _t(slug: str, **roles: int) -> dict[str, Any]:
    return {"slug": slug, "roles": roles, "quotes": [{"text": f"q-{slug}-{sum(roles.values())}"}]}


def test_scorer_hand_case_variants_groups_candidates_failures_weights() -> None:
    # Red under (each): group nodes in the primary slug set (X tp 2), candidate rows scored (X fn +1), a failed row
    # left out of the denominators (R 1/2 not 1/3), the ignore rule under restricted variants (both: d stays an FP
    # but b, outside the variant, would add an FN), weights per observation instead of per band share (weighted R
    # 1/3), or the priority-role threshold (used >= 2 would be a hit).
    ctx = _ctx()
    obs = [ts.obs_of(_row("X", techniques=[_t("a", mentioned=2, used=1), _t("d", mentioned=1), _t("g", mentioned=1),
                                           _t("b", mentioned=1, used=0)][:3])),
           ts.obs_of(_row("Y", status="failed_permanent", error_class="truncated"))]
    s = ts.score_source(obs, ctx)
    assert (s["observations"], s["failed"], s["partial"], s["not_attempted"]) == (2, 1, 0, 0)
    full = s["variants"]["full"]
    assert (full["slugs"]["tp"], full["slugs"]["fp"], full["slugs"]["fn"]) == (1, 1, 2)
    assert full["slugs"]["precision"] == 0.5 and full["slugs"]["recall"] == pytest.approx(1 / 3)
    assert (full["slugs_groups"]["tp"], full["slugs_groups"]["fp"], full["slugs_groups"]["fn"]) == (2, 1, 2)
    assert (full["cells_ge2"]["tp"], full["cells_ge2"]["fp"], full["cells_ge2"]["fn"]) == (1, 0, 3)
    assert full["weighted"]["recall"] == pytest.approx(0.2 / 1.2) and full["weighted"]["precision"] == 0.5
    assert full["priority_roles"]["used>=1"]["recall"] == 1.0 and full["priority_roles"]["used>=2"]["recall"] == 0.0
    assert full["kinds"]["intervention"]["recall"] == 1.0 and full["kinds"]["assessment"]["fp"] == 1
    assert full["complete_only"]["observations"] == 1
    assert full["by_length"]["60-100k"]["slugs"]["recall"] == 0.0 and full["by_doc_kind"]["paper"]["observations"] == 1
    both = s["variants"]["both"]["slugs"]
    assert (both["tp"], both["fp"], both["fn"]) == (1, 1, 1)
    high = s["variants"]["high"]["slugs"]
    assert (high["tp"], high["fp"], high["fn"]) == (1, 1, 0)  # Y's only row is med: outside, ignored


def test_partial_rows_deliver_what_they_have_and_unions_take_the_maximum() -> None:
    # Red under: a partial row scored as a miss (X tp 0), union taking the first observation's strengths (used 1),
    # quotes from another row than the strongest, or union costs not summed.
    ctx = _ctx()
    o1 = ts.obs_of(_row("X", techniques=[_t("a", mentioned=2, used=1)], flags=["partial"], cost_usd=0.01,
                        standalone_cost_usd=0.02))
    o2 = ts.obs_of(_row("X", repeat=2, techniques=[_t("a", mentioned=1, used=3), _t("b", mentioned=1)], cost_usd=0.03,
                        standalone_cost_usd=0.03))
    s = ts.score_source([o1], ctx)
    assert s["partial"] == 1 and s["variants"]["full"]["slugs"]["tp"] == 1
    (u,) = ts.union([o1, o2])
    a = next(r for r in u.rows if r["slug"] == "a")
    assert a["roles"] == {"mentioned": 2, "used": 3} and a["quotes"] == [{"text": "q-a-4"}]
    assert {r["slug"] for r in u.rows} == {"a", "b"} and not u.partial
    assert u.cost_usd == pytest.approx(0.04) and u.standalone_cost_usd == pytest.approx(0.05) and u.members == 2
    rp = ts.score_source([o1, o2], ctx)["repeats"]
    assert rp["items"] == 1 and rp["mean_jaccard"] == pytest.approx(0.5) and rp["identical_share"] == 0.0


def test_stage_block_and_cost_block_hand_case() -> None:
    # Red under: stage recall computed over all predicted slugs, outside-submenu recoveries read from the sub-menu
    # difference instead of the stage's repairs, or the length-weighted cost not weighting by band share.
    ctx = _ctx()
    pre = {"mentions": [{"candidates": [{"slug": "a"}]}], "doc_level": [], "lexical": [{"slug": "b"}],
           "submenu": ["a", "b"]}
    calls = [{"stage": "prepass", "from_response_cache": True, "status": "ok", "prompt_tokens": 100,
              "cached_tokens": 50, "cost_usd": 0.0, "call_cost_usd": 0.001},
             {"stage": "grade", "from_response_cache": False, "status": "ok", "prompt_tokens": 200,
              "cached_tokens": 0, "cost_usd": 0.004, "call_cost_usd": 0.004}]
    x = ts.obs_of(_row("X", techniques=[_t("a", mentioned=2), _t("c", mentioned=1)], prepass=pre, mode="staged",
                       repairs=["outside_submenu:c"], cost_usd=0.004, standalone_cost_usd=0.005, latency_ms=2000,
                       standalone_latency_ms=2500, calls=calls))
    y = ts.obs_of(_row("Y", techniques=[], cost_usd=0.01, standalone_cost_usd=0.02, latency_ms=4000,
                       standalone_latency_ms=4000))
    st = ts.stage_block([x], ctx)
    assert st is not None
    assert st["recall"] == {"selection": 0.5, "lexical": 0.5, "union": 1.0, "submenu": 1.0, "final": 0.5}
    assert (st["outside_submenu_rows"], st["outside_submenu_gold"]) == (1, 0)
    c = ts.cost_block([x, y], ctx)
    assert c["usd_per_1000_actual"] == pytest.approx(7.0) and c["usd_per_1000_standalone"] == pytest.approx(12.5)
    assert c["usd_per_1000_standalone_length_weighted"] == pytest.approx((0.2 * 0.005 + 0.8 * 0.02) * 1000)
    assert c["latency_ms_median"] == 3000.0
    assert c["per_stage"]["prepass"]["response_cache_share"] == 1.0
    assert c["per_stage"]["prepass"]["prompt_cache_share"] == 0.5


def test_paired_bootstrap_is_deterministic_and_brackets_the_point_difference() -> None:
    # Red under: an unseeded RNG (two calls differ), resampling observations instead of items, or B - A reversed.
    ctx = _ctx()
    a = ts.Source("A", [ts.obs_of(_row("X", techniques=[_t("a", mentioned=2)])),
                        ts.obs_of(_row("Y", status="failed_permanent", error_class="truncated"))], [])
    b = ts.Source("B", [ts.obs_of(_row("X", techniques=[_t("a", mentioned=2)])),
                        ts.obs_of(_row("Y", techniques=[_t("c", mentioned=2)]))], [])
    r1 = ts.compare(a, b, ctx, resamples=2000)
    r2 = ts.compare(a, b, ctx, resamples=2000)
    assert r1 == r2
    m = r1["metrics"]["slug_recall"]
    assert m["a"] == pytest.approx(1 / 3) and m["b"] == pytest.approx(2 / 3) and m["diff"] == pytest.approx(1 / 3)
    lo, hi = m["ci95"]
    assert lo <= m["diff"] <= hi and hi > 0 and m["share_b_better"] > 0.5
    same = ts.compare(a, a, ctx, resamples=500)["metrics"]["slug_f1"]
    assert same["diff"] == 0 and same["ci95"] == [0.0, 0.0]


def _fake_run(root: Path, name: str, rows: list[dict[str, Any]], mode: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    run = {"run_id": f"eval-technique_staged-{name}", "task": "technique_staged", "set": SET,
           "set_dir": str(SET_DIR), "split": "all", "models": [LUNA], "mode": mode, "gold": "gold.jsonl",
           "pins": {LUNA: {"mode": mode, "model": LUNA, "name": "technique-staged-v1"}}}
    (d / "run.json").write_text(json.dumps(run), "utf-8")
    write_jsonl(d / "rows.jsonl", rows)
    return d


def test_compare_and_table_cli_on_synthetic_runs(tmp_path: Path) -> None:
    # Red under: load_source ignoring @rK / union:, results_table not adding the union line, or the CLI verbs not
    # writing their files.
    gold = read_jsonl(SET_DIR / "gold.jsonl")

    def rows(keep: float, repeat: int) -> list[dict[str, Any]]:
        out = []
        for g in gold:
            ts_rows = [t for t in g["label"]["techniques"] if not t.get("candidate")]
            kept = ts_rows[: max(0, int(len(ts_rows) * keep))]
            out.append(_row(g["item_id"], repeat=repeat, techniques=[_t(t["slug"], mentioned=2) for t in kept],
                            cost_usd=0.001, standalone_cost_usd=0.002))
        return out

    single = _fake_run(tmp_path, "single", rows(0.5, 1) + rows(0.25, 2), "single")
    sweep = _fake_run(tmp_path, "sweep", rows(0.75, 1), "sweep")
    assert len(ts.load_source(f"{single}@r2").obs) == 36
    assert len(ts.load_source(f"union:{single}").obs) == 36
    res = runner.invoke(app, ["eval", "staged-compare", f"{single}@r1", str(sweep), "--resamples", "300",
                              "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    assert "slug_recall" in res.output and "written:" in res.output
    res = runner.invoke(app, ["eval", "staged-table", str(single), str(sweep), "--source", f"union:{single},{sweep}",
                              "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    assert "union of 2" in res.output and "union(single" in res.output
    written = list((tmp_path / "out" / "technique_staged" / SET / "analysis").glob("*-table.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text("utf-8"))
    labels = [s["label"] for s in data["sources"]]
    assert len(labels) == 6  # single pooled, r1, r2, union; sweep; the cross-run union
    by = {s["label"]: s["metrics"] for s in data["sources"]}
    sw = next(v for k, v in by.items() if k.startswith("sweep"))
    assert sw["variants"]["full"]["slugs"]["precision"] == 1.0


# ═════════════════════════════════════ dont_care rows, scoring on another set revision (D611) ═══════


def _ctx_dont_care() -> ts.Context:
    ctx = _ctx()
    gold = json.loads(json.dumps(ctx.gold))
    gold["X"]["label"]["techniques"].append({"slug": "e", "roles": {"mentioned": 2, "used": 2}, "sources": ["A", "B"],
                                             "confidence": "high", "dont_care": True})
    return ts.Context(gold=gold, items=ctx.items, kind={**ctx.kind, "e": "assessment"}, groups=ctx.groups,
                      shares=ctx.shares, shares_name=ctx.shares_name)


def test_dont_care_rows_leave_the_denominator_and_their_predictions_are_neither_tp_nor_fp() -> None:
    # Red under: dont_care rows kept as gold (X tp 2, R 2/3 in the first case; the second case's fn 2), dropped from
    # the gold without joining the ignore set (e an FP: fp 1), kept in a restricted variant (high: e is high
    # confidence), its cells scored (cells >= 2 tp / fp), or the stage-wise denominator counting it (gold 2 not 3).
    ctx = _ctx_dont_care()
    hit = ts.obs_of(_row("X", techniques=[_t("a", mentioned=2, used=2), _t("e", mentioned=2, used=2)],
                         prepass={"mentions": [], "doc_level": [], "lexical": [], "submenu": []}, mode="staged"))
    miss = ts.obs_of(_row("X", repeat=2, techniques=[_t("a", mentioned=2)]))
    s = ts.score_source([hit], ctx)
    for variant in ("full", "both", "high"):
        v = s["variants"][variant]
        assert v["slugs"]["fp"] == 0 and v["slugs_groups"]["fp"] == 0, variant
    full = s["variants"]["full"]
    assert (full["slugs"]["tp"], full["slugs"]["fp"], full["slugs"]["fn"]) == (1, 0, 1)
    assert (full["cells_ge2"]["tp"], full["cells_ge2"]["fp"]) == (2, 0)  # (a, mentioned), (a, used); e ignored
    assert (s["variants"]["high"]["slugs"]["tp"], s["variants"]["high"]["slugs"]["fn"]) == (1, 0)
    full2 = ts.score_source([miss], ctx)["variants"]["full"]["slugs"]
    assert (full2["tp"], full2["fp"], full2["fn"]) == (1, 0, 1)
    st = ts.stage_block([hit], ctx)
    assert st is not None and st["gold"] == 2
    # the bootstrap reads the same counts
    c = ts.compare(ts.Source("A", [miss], []), ts.Source("B", [hit], []), ctx, resamples=50)
    assert c["metrics"]["slug_precision"]["a"] == 1.0 and c["metrics"]["slug_precision"]["b"] == 1.0


def _revision(tmp: Path, *, same_items: bool = True) -> Path:
    """A copy of the set's items, manifest and texts with a gold whose first scored row of an-0001 is dont_care."""
    d = tmp / "rev"
    d.mkdir()
    for f in ("items.jsonl", "manifest.json"):
        shutil.copyfile(SET_DIR / f, d / f)
    if not same_items:
        (d / "items.jsonl").write_text((SET_DIR / "items.jsonl").read_text("utf-8") + "\n", "utf-8")
    gold = read_jsonl(SET_DIR / "gold.jsonl")
    gold[0]["label"]["techniques"][0]["dont_care"] = True
    write_jsonl(d / "gold.jsonl", gold)
    return d


def _digest_run(root: Path, name: str, rows: list[dict[str, Any]]) -> Path:
    import hashlib

    d = _fake_run(root, name, rows, "single")
    run = json.loads((d / "run.json").read_text("utf-8"))
    run["set_items_digest"] = hashlib.sha256((SET_DIR / "items.jsonl").read_bytes()).hexdigest()
    (d / "run.json").write_text(json.dumps(run), "utf-8")
    return d


def test_score_compare_and_table_on_another_set_revision(tmp_path: Path) -> None:
    # Red under: --set-dir ignored by staged-compare / staged-table (recall unchanged), score writing summary.json
    # whatever --summary says, or a revision with another items.jsonl accepted.
    gold = read_jsonl(SET_DIR / "gold.jsonl")
    first = gold[0]["label"]["techniques"][0]["slug"]
    rows = [_row(g["item_id"], techniques=[_t(t["slug"], mentioned=2) for t in g["label"]["techniques"]
                                            if not t.get("candidate") and (g is not gold[0] or t["slug"] != first)])
            for g in gold]
    run = _digest_run(tmp_path, "run", rows)
    rev = _revision(tmp_path)
    res = runner.invoke(app, ["eval", "score", "--run", str(run), "--set-dir", str(rev), "--summary", "summary-rev"])
    assert res.exit_code == 0, res.output
    assert not (run / "summary.json").exists() and (run / "summary-rev.json").is_file()
    on_rev = json.loads((run / "summary-rev.json").read_text("utf-8"))
    assert on_rev["gold_label"] == "rev/gold.jsonl"
    assert on_rev["per_model"][LUNA]["sources"]["pooled"]["variants"]["full"]["slugs"]["fn"] == 0
    res = runner.invoke(app, ["eval", "score", "--run", str(run)])
    assert res.exit_code == 0, res.output
    on_own = json.loads((run / "summary.json").read_text("utf-8"))
    assert on_own["per_model"][LUNA]["sources"]["pooled"]["variants"]["full"]["slugs"]["fn"] == 1
    out = tmp_path / "out"
    res = runner.invoke(app, ["eval", "staged-table", str(run), "--set-dir", str(rev), "--out", str(out)])
    assert res.exit_code == 0, res.output
    (table,) = (out / "technique_staged" / "rev" / "analysis").glob("*-table.json")
    data = json.loads(table.read_text("utf-8"))
    assert data["gold"] == "rev/gold.jsonl" and data["sources"][0]["metrics"]["variants"]["full"]["slugs"]["recall"] == 1.0
    res = runner.invoke(app, ["eval", "staged-compare", str(run), str(run), "--set-dir", str(rev), "--resamples", "20",
                              "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "rev/gold.jsonl" in res.output
    bad = tmp_path / "bad"
    bad.mkdir()
    other = _revision(bad, same_items=False)
    for args in (["score", "--run", str(run), "--set-dir", str(other), "--summary", "x"],
                 ["staged-table", str(run), "--set-dir", str(other), "--out", str(out)],
                 ["staged-compare", str(run), str(run), "--set-dir", str(other), "--out", str(out)]):
        res = runner.invoke(app, ["eval", *args])
        assert res.exit_code == 1 and "set_items_digest" in res.output, (args, res.output)
    res = runner.invoke(app, ["eval", "score", "--run", str(run), "--summary", "../escape"])
    assert res.exit_code == 1


# ═════════════════════════════════════ hierarchy-tolerant slugs (D626) ══════════════════════════════

Row = dict[str, Any]
#: the structural links of the hand cases: a technique variant under its parent technique, two children of group G
TOL_LINKS = frozenset({("dce.cbrn", "dce"), ("t1", "G"), ("t2", "G")})


def _tctx(rows: list[dict[str, Any]]) -> ts.Context:
    """One item X with gold ``rows`` (defaults: both labellers, high), group node G, :data:`TOL_LINKS`."""
    gold = {"X": {"item_id": "X", "doc_kind": "paper", "label": {"techniques": [
        {"roles": {"mentioned": 2}, "sources": ["A", "B"], "confidence": "high", **r} for r in rows]}}}
    items = {"X": {"item_id": "X", "stratum": {"channel_group": "paper"}, "input": {"full_text_chars": 5_000}}}
    kind = dict.fromkeys(("a", "b", "c", "dce", "dce.cbrn", "t1", "t2"), "assessment")
    return ts.Context(gold=gold, items=items, kind=kind, groups=frozenset({"G"}), links=TOL_LINKS,
                      shares={"<20k": 1.0, "20-60k": 0.0, "60-100k": 0.0, ">100k": 0.0}, shares_name="test")


def _tol(rows: list[dict[str, Any]], pred: list[str], variant: str = "full") -> tuple[Row, Row, Row]:
    """(strict slugs, slugs_levelalt, slugs_tolerant) of one observation predicting ``pred``."""
    s = ts.score_source([ts.obs_of(_row("X", techniques=[_t(p, mentioned=2) for p in pred]))], _tctx(rows))
    v = s["variants"][variant]
    assert s["tolerant_partial"] == ts.TOLERANT_PARTIAL == 0.5
    return v["slugs"], v["slugs_levelalt"], v["slugs_tolerant"]


def _pr(x: Row) -> tuple[float | None, float | None]:
    return x["precision"], x["recall"]


def test_tolerant_exact_matches_score_as_strict() -> None:
    # Red under: tolerant credit for an unlinked prediction (c), or a denominator other than the predicted slugs.
    strict, lvl, tol = _tol([{"slug": "a"}, {"slug": "b"}], ["a", "c"])
    assert _pr(strict) == _pr(lvl) == _pr(tol) == (0.5, 0.5)
    assert (tol["tp_recall"], tol["gold"], tol["tp_precision"], tol["predicted"]) == (1.0, 2.0, 1.0, 2.0)


def test_tolerant_parent_child_technique_link_gets_partial_credit_both_ways() -> None:
    # Red under: links not consulted (0 / 0), links read one way only (the reverse case), or full credit for a link.
    for gold, pred in (("dce", "dce.cbrn"), ("dce.cbrn", "dce")):
        strict, lvl, tol = _tol([{"slug": gold}], [pred])
        assert (strict["tp"], strict["fp"], strict["fn"]) == (0, 1, 1)
        assert _pr(lvl) == (0.0, 0.0) and _pr(tol) == (0.5, 0.5), (gold, pred)


def test_tolerant_group_node_links_in_both_directions_and_no_siblings() -> None:
    # Red under: group nodes left out of the tolerant precision side (P None), a predicted group not crediting its
    # gold child, a gold group row not crediting a predicted child, or siblings linked (t1 / t2).
    strict, lvl, tol = _tol([{"slug": "t1"}], ["G"])
    assert strict["precision"] is None and strict["recall"] == 0.0
    assert _pr(tol) == (0.5, 0.5) and _pr(lvl) == (0.0, 0.0)
    strict, lvl, tol = _tol([{"slug": "G"}], ["t1"])
    assert strict["recall"] is None and strict["precision"] == 0.0  # the gold group row is outside the universe
    assert tol["recall"] is None and tol["precision"] == 0.5 and lvl["precision"] == 0.0
    _, _, tol = _tol([{"slug": "t1"}], ["t2"])
    assert _pr(tol) == (0.0, 0.0)


def test_level_alternatives_count_as_exact_matches_in_tolerant_and_levelalt_only() -> None:
    # Red under: level_alternatives ignored (0 / 0), honoured by the strict metric, or given only partial credit.
    strict, lvl, tol = _tol([{"slug": "dce", "level_alternatives": ["dce.cbrn"]}], ["dce.cbrn"])
    assert _pr(strict) == (0.0, 0.0) and _pr(lvl) == (1.0, 1.0) and _pr(tol) == (1.0, 1.0)
    strict, lvl, tol = _tol([{"slug": "t1", "level_alternatives": ["G"]}], ["G"])  # a group as the alternative
    assert _pr(lvl) == (1.0, 1.0) and _pr(tol) == (1.0, 1.0) and strict["precision"] is None


def test_a_predicted_unlinked_group_node_is_a_tolerant_false_positive_only() -> None:
    # Red under: predicted group nodes left out of the tolerant precision side (P 1.0), or put into the strict one.
    strict, lvl, tol = _tol([{"slug": "a"}], ["a", "G"])
    assert _pr(strict) == (1.0, 1.0)
    assert _pr(lvl) == (0.5, 1.0) and _pr(tol) == (0.5, 1.0)


def test_tolerant_ignore_rules_with_dont_care_and_the_high_variant() -> None:
    # Red under: a dont_care slug's exact prediction counted (P 2/3), a prediction linked only to an ignored row
    # counted as an FP under tolerant (P 1/2), the linked-ignore rule applied without partial credit (levelalt P 1),
    # or the high variant not ignoring the outside row's link (P 1/2) / its level alternative (P 1/2).
    strict, lvl, tol = _tol([{"slug": "a"}, {"slug": "dce", "dont_care": True}], ["a", "dce", "dce.cbrn"])
    assert _pr(strict) == (0.5, 1.0)
    assert _pr(tol) == (1.0, 1.0) and tol["predicted"] == 1.0
    assert _pr(lvl) == (0.5, 1.0)
    rows = [{"slug": "a"}, {"slug": "dce", "confidence": "med"}]
    strict, _, tol = _tol(rows, ["a", "dce.cbrn"], "high")
    assert _pr(strict) == (0.5, 1.0) and _pr(tol) == (1.0, 1.0)
    strict, _, tol = _tol(rows, ["a", "dce.cbrn"], "full")
    assert _pr(tol) == (0.75, 0.75)  # (1 + 0.5) / 2 both sides
    rows = [{"slug": "a"}, {"slug": "dce", "confidence": "med", "level_alternatives": ["b"]}]
    strict, lvl, tol = _tol(rows, ["a", "b"], "high")
    assert strict["precision"] == 0.5 and lvl["precision"] == 1.0 and tol["precision"] == 1.0


def test_tolerant_blocks_in_strata_weighted_markdown_table_and_bootstrap() -> None:
    # Red under: a stratum, the weighted block, a markdown row or the table column missing the tolerant metrics,
    # the bootstrap not resampling them (or with another draw than the strict metrics: the strict CIs move).
    ctx = _ctx()
    ctx = ts.Context(gold=ctx.gold, items=ctx.items, kind=ctx.kind, groups=ctx.groups, shares=ctx.shares,
                     shares_name=ctx.shares_name, links=frozenset({("d", "a")}))
    a = ts.Source("A", [ts.obs_of(_row("X", techniques=[_t("d", mentioned=2)])),
                        ts.obs_of(_row("Y", status="failed_permanent", error_class="truncated"))], [])
    b = ts.Source("B", [ts.obs_of(_row("X", techniques=[_t("a", mentioned=2), _t("g", mentioned=1)])),
                        ts.obs_of(_row("Y", techniques=[_t("c", mentioned=2)]))], [])
    s = ts.score_source(a.obs, ctx)
    full = s["variants"]["full"]
    # A: X gold a, b (+ group g), predicts d (linked to a); Y gold c, failed
    assert full["slugs_tolerant"]["recall"] == pytest.approx(0.5 / 3) and full["slugs_tolerant"]["precision"] == 0.5
    assert full["weighted_tolerant"]["recall"] == pytest.approx(0.2 * 0.5 / (0.2 * 2 + 0.8 * 1))
    assert full["weighted_tolerant"]["precision"] == 0.5
    assert full["by_length"]["<20k"]["slugs_tolerant"]["recall"] == 0.25
    assert full["by_channel_group"]["url"]["slugs_levelalt"]["recall"] == 0.0
    assert full["by_doc_kind"]["paper"]["slugs_tolerant"]["precision"] == 0.5
    md = "\n".join(ts.source_markdown("A", s))
    assert "tolerant P / R / F1" in md and "level alternatives P / R / F1" in md and "0.5" in md
    assert "tolerant P / R / F1" in ts.TABLE_HEAD
    assert ts.table_row("A", s)[ts.TABLE_HEAD.index("tolerant P / R / F1")] == ts._prf_cell(full["slugs_tolerant"])
    c = ts.compare(a, b, ctx, resamples=400)
    m = c["metrics"]
    assert set(m) >= {"tolerant_recall", "tolerant_precision", "tolerant_f1", "tolerant_weighted_recall"}
    sb = ts.score_source(b.obs, ctx)["variants"]["full"]
    assert m["tolerant_recall"]["a"] == pytest.approx(full["slugs_tolerant"]["recall"])
    assert m["tolerant_recall"]["b"] == pytest.approx(sb["slugs_tolerant"]["recall"])
    assert m["tolerant_precision"]["b"] == pytest.approx(sb["slugs_tolerant"]["precision"])  # g: an FP, a hit
    assert m["tolerant_weighted_recall"]["b"] == pytest.approx(sb["weighted_tolerant"]["recall"])
    assert m["tolerant_recall"]["resamples"] == 400 and m["tolerant_recall"]["ci95"] is not None
    assert c["tolerant_partial"] == 0.5 and "tolerant_recall" in ts.compare_markdown(c)
    # the strict metrics' intervals as the scorer gave them before the tolerant metrics existed (same draws)
    assert {k: [v["ci95"], v["share_b_better"]] for k, v in m.items() if not k.startswith("tolerant")} == {
        "slug_recall": [[0.5, 1.0], 1.0], "slug_precision": [[1.0, 1.0], 1.0], "slug_f1": [[2 / 3, 0.8], 1.0],
        "weighted_recall": [[0.5, 1.0], 1.0], "cells_ge2_f1": [[2 / 3, 2 / 3], 1.0]}


def test_tolerant_equals_strict_without_links_alternatives_or_predicted_groups_on_the_real_set() -> None:
    # Red under: the tolerant code reading another universe than the strict primary one (group rows in recall,
    # candidate or dont_care rows counted) — with no links, no level alternatives and no predicted group node the
    # two must agree on every observation and variant.
    ctx = ts.load_context(SET_B_DIR)
    plain = ts.Context(gold=ctx.gold, items=ctx.items, kind=ctx.kind, groups=ctx.groups, shares=ctx.shares,
                       shares_name=ctx.shares_name)
    obs = []
    for k, g in enumerate(read_jsonl(SET_B_DIR / "gold.jsonl")):
        slugs = [t["slug"] for t in g["label"]["techniques"] if not t.get("candidate") and t["slug"] not in ctx.groups]
        obs.append(ts.obs_of(_row(g["item_id"], techniques=[_t(s, mentioned=2) for s in slugs[k % 3::2]]
                                  + [_t("prompt-injection-defense-training", mentioned=1)])))
    s = ts.score_source(obs, plain)
    for variant in ts.GOLD_VARIANTS:
        v = s["variants"][variant]
        for key in ("slugs_tolerant", "slugs_levelalt"):
            assert v[key]["precision"] == pytest.approx(v["slugs"]["precision"]), (variant, key)
            assert v[key]["recall"] == pytest.approx(v["slugs"]["recall"]), (variant, key)
    linked = ts.score_source(obs, ctx)["variants"]["full"]  # the real links only add credit here
    assert linked["slugs_tolerant"]["recall"] >= linked["slugs"]["recall"]
    assert len(ctx.links) == 4 + 49 and ("red-teaming", "human-red-teaming") not in ctx.links
    assert ("human-red-teaming", "red-teaming") in ctx.links


def test_real_taxonomy_234_links_and_level_alternatives_checked_against_the_taxonomy(tmp_path: Path) -> None:
    # Red under: parent links or group-children links missing, or an unknown level alternative accepted silently.
    _kind, groups, links = ts.taxonomy_structure(REPO / "taxonomy" / "taxonomy-v0.9-234.yaml")
    assert ("dangerous-capability-evaluation.cbrn", "dangerous-capability-evaluation") in links
    assert ("task-adherence-monitoring", "prompt-injection-defense-agentic") in links
    assert ("task-adherence-monitoring", "agent-runtime-monitoring-intervention") in links
    assert len(links) == 53 and len(groups) == 9
    d = tmp_path / "set"
    d.mkdir()
    for f in ("items.jsonl", "manifest.json"):
        shutil.copyfile(SET_B_DIR / f, d / f)
    gold = read_jsonl(SET_B_DIR / "gold.jsonl")
    gold[0]["label"]["techniques"][0]["level_alternatives"] = ["no-such-slug"]
    write_jsonl(d / "gold.jsonl", gold)
    with pytest.raises(ts.AnalysisError, match="level_alternatives.*no-such-slug"):
        ts.load_context(d)


# ═════════════════════════════════════ gold v2: the set 2026-10-18b (D630) ═════════════════════════════════

TAX_234 = "taxonomy/taxonomy-v0.9-234.yaml"
#: items.jsonl as the first trial runs used it (built before the final labels): a re-freeze must not move it
ITEMS_18B_SHA256 = "fa4912e8a1a0a7dca03451de01c57d9504579c3cbdf0cdfeaea5e2ec4559ce78"


def _freeze_18b(monkeypatch: pytest.MonkeyPatch) -> Any:
    require_eval_texts(SET_18B_DIR)
    monkeypatch.chdir(REPO)
    spec = importlib.util.spec_from_file_location("_ts_freeze_18b_under_test", SET_18B_DIR / "scripts" / "freeze.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _copy_18b(tmp: Path) -> Path:
    d = tmp / "set"
    shutil.copytree(SET_18B_DIR, d, ignore=shutil.ignore_patterns("__pycache__"))
    return d


@pytest.mark.slow
def test_freeze_18b_reproduces_the_set_byte_for_byte(monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: any change to freeze_info / the v3 single user message / the split rule / the gold conversion
    # without a re-freeze, a hand edit of items.jsonl, gold.jsonl or manifest.json, or items.jsonl moving away from
    # the build the trial runs used.
    fz = _freeze_18b(monkeypatch)
    assert fz.main(False) == 0
    out = capsys.readouterr().out
    assert "reproduced: items.jsonl, gold.jsonl, manifest.json byte for byte, 120 texts checked" in out
    assert hashlib.sha256((SET_18B_DIR / "items.jsonl").read_bytes()).hexdigest() == ITEMS_18B_SHA256
    man = json.loads((SET_18B_DIR / "manifest.json").read_text("utf-8"))
    assert "dry_run" not in man and man["items_sha256"] == ITEMS_18B_SHA256
    assert man["frozen_with"]["taxonomy_path"] == TAX_234 and man["frozen_with"]["prompt_set"] == "technique-staged-v3"
    g = man["gold"]
    assert (g["items"], g["rows"], g["scored_rows"], g["dont_care_rows"], g["verified_items"]) == (120, 1189, 1082, 41,
                                                                                                 30)
    assert g["adjudicators"] == {"astra-reconciler": 25, "merge-rule": 95} and man["splits"] == {"dev": 60, "test": 60}


@pytest.mark.slow
def test_freeze_18b_refuses_moved_text_bad_slugs_sources_adjudicator_and_partial_staging(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: texts_and_meta not checking the text against the store digest, build_gold accepting a slug or level
    # alternative outside the 234-entry taxonomy, sources outside X / Y / reconciler / opus / verification, an
    # adjudicator that contradicts the reconciled-items list, or a partial staging frozen without --allow-partial.
    fz = _freeze_18b(monkeypatch)
    d = _copy_18b(tmp_path)
    src = eval_text_file(SET_18B_DIR, "g2-0001")
    p = d / "texts" / "g2-0001.txt.gz"  # the copy's own file is read before the cache's
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(gzip.decompress(src.read_bytes()) + b" ", compresslevel=9, mtime=0))
    assert fz.main(False, out=d) == 1
    assert "g2-0001: staged text does not hash to the store's text_digest" in capsys.readouterr().out
    shutil.copyfile(src, p)
    final = d / "adjudication" / "final.jsonl"
    rec = read_jsonl(final)
    rec[0]["techniques"][0]["slug"] = "no-such-technique"
    rec[0]["techniques"][1]["level_alternatives"] = ["candidate:x"]
    rec[1]["techniques"][0]["sources"] = ["X", "Z"]
    rec[2]["adjudicator"] = "merge-rule" if rec[2]["item_id"] in json.loads(
        (d / "adjudication" / "STAGED.json").read_text("utf-8"))["reconciled_items"] else "astra-reconciler"
    write_jsonl(final, rec)
    assert fz.main(False, out=d) == 1
    out = capsys.readouterr().out
    assert "no-such-technique (slug)" in out and "(level alternative candidate:x)" in out
    assert "(sources ['Z'])" in out and f"{rec[2]['item_id']} (adjudicator" in out
    shutil.copyfile(SET_18B_DIR / "adjudication" / "final.jsonl", final)
    staged = json.loads((d / "adjudication" / "STAGED.json").read_text("utf-8"))
    (d / "adjudication" / "STAGED.json").write_text(json.dumps({**staged, "partial": True}), "utf-8")
    assert fz.main(False, out=d) == 1
    assert "pass --allow-partial for a dry run" in capsys.readouterr().out


def test_18b_gold_sources_verification_rows_and_variants() -> None:
    # Red under: X / Y not mapped to A / B (the both variant would be empty), verification rows (opus, dont_care)
    # dropped or not flagged, or the verified items not naming their verifiers.
    gold = read_jsonl(SET_18B_DIR / "gold.jsonl")
    rows = [t for g in gold for t in g["label"]["techniques"]]
    assert {s for t in rows for s in t["sources"]} <= {"A", "B", "reconciler", "opus", "verification"}
    assert sum(t["sources"] == ["opus"] for t in rows) == 29 and sum(bool(t.get("dont_care")) for t in rows) == 41
    verified = {x["item_id"] for x in json.loads((SET_18B_DIR / "adjudication" / "verify-sample.json").read_text())}
    for g in gold:
        assert g["labelers"] == ["astra-L1", "astra-L2"] + (["opus", "sonnet-adjudicator"]
                                                            if g["item_id"] in verified else [])
        assert ("verification" in g["adjudication"]) == (g["item_id"] in verified)
    keep, ignore = ts.gold_rows(next(g for g in gold if any(t.get("dont_care") for t in g["label"]["techniques"])),
                                "both")
    assert keep and ignore and all({"A", "B"} <= set(t["sources"]) for t in keep)


@pytest.mark.parametrize("mode", ["single", "sweep", "staged", "lexical"])
@pytest.mark.slow
def test_input_check_passes_on_18b_with_the_234_taxonomy_under_v3(mode: str) -> None:
    # Red under: items frozen against another taxonomy or prompt set than the manifest names (the staged / lexical
    # digests, the v3 single user message), or a gold slug outside the pinned taxonomy.
    from stack.annotate.stage import stack_config
    from stack.evals import score

    require_eval_texts(SET_18B_DIR)
    a = harness.make_adapter("technique_staged", stack_config(), None, prompt="technique-staged-v3", models=[LUNA],
                             pin_overrides={"mode": mode, "taxonomy": TAX_234})
    a.set_dir = SET_18B_DIR
    items = harness.select_items(harness.load_set(REPO / "evals", "technique_staged", "2026-10-18b").items, "all",
                                 None, None)
    assert len(items) == 120 and a.checks_user_message()
    harness.check_inputs(a, items)
    assert a.gold_problems(score.load_gold(SET_18B_DIR, None)[1]) == []


def test_scorer_and_length_weights_work_on_18b() -> None:
    # Red under: a stratum or band the scorer cannot read, the gold not loadable with the set's taxonomy, or
    # dont_care rows scored (a perfect prediction of the scored rows would lose recall).
    ctx = ts.load_context(SET_18B_DIR)
    assert len(ctx.links) == 53 and len(ctx.items) == len(ctx.gold) == 120
    w = ctx.weights()
    assert sum(w.values()) == pytest.approx(1.0, abs=2e-4)  # the production shares sum to 1.0001 (rounding)
    obs = []
    for iid, g in ctx.gold.items():
        slugs = [t["slug"] for t in g["label"]["techniques"] if not t.get("dont_care")]
        obs.append(ts.obs_of(_row(iid, techniques=[_t(s, mentioned=2) for s in slugs])))
    full = ts.score_source(obs, ctx)["variants"]["full"]
    assert full["slugs"]["recall"] == 1.0 and full["slugs"]["precision"] == 1.0
    assert full["slugs"]["tp"] == 1082 and full["slugs_tolerant"]["recall"] == 1.0
    assert full["weighted"]["recall"] == 1.0 and set(full["by_channel_group"]) == {"paper", "forum", "url"}


# ═════════════════════════════════════ gold v2, revision 2026-10-18c (D632) ════════════════════════════════


def _derive_18c(monkeypatch: pytest.MonkeyPatch) -> Any:
    require_eval_texts(SET_18B_DIR, SET_18C_DIR)
    monkeypatch.chdir(REPO)
    spec = importlib.util.spec_from_file_location("_ts_derive_18c_under_test", SET_18C_DIR / "scripts" / "derive.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_18c_is_derived_byte_for_byte_on_18b_items(monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: a hand edit of 18c's gold / manifest, items or texts drifting from 18b's (every 18b run must score
    # here), an item without the Astra adjudicator or its reconciliation round, or the verifiers missing.
    dv = _derive_18c(monkeypatch)
    assert dv.main(False) == 0
    assert "reproduced: items.jsonl, gold.jsonl, manifest.json byte for byte, 120 texts identical" in \
        capsys.readouterr().out
    assert (hashlib.sha256((SET_18C_DIR / "items.jsonl").read_bytes()).hexdigest()
            == hashlib.sha256((SET_18B_DIR / "items.jsonl").read_bytes()).hexdigest() == ITEMS_18B_SHA256)
    man = json.loads((SET_18C_DIR / "manifest.json").read_text("utf-8"))
    g = man["gold"]
    assert (g["rows"], g["scored_rows"], g["scored_high"], g["dont_care_rows"], g["scored_both_labellers"],
            g["level_alternative_rows"]) == (1130, 1064, 829, 3, 947, 8)
    assert g["adjudicators"] == {"astra-reconciler": 120} and man["frozen_with"]["taxonomy_path"] == TAX_234
    m2a = man["derivation"]["merge_to_astra_round2"]
    assert (m2a["items"], m2a["rows_removed"], m2a["rows_added"], m2a["confidence_changed"],
            m2a["rows_roles_changed"], m2a["slug_f1_merge_vs_astra_no_group_nodes"]) == (95, 49, 10, 76, 158, 0.97)
    assert man["derivation"]["verification_round2"]["to_codex"] == 63
    gold = read_jsonl(SET_18C_DIR / "gold.jsonl")
    assert {x["adjudication"]["reconciliation_round"] for x in gold} == {1, 2}
    assert sum(x["labelers"] == ["astra-L1", "astra-L2", "opus", "sonnet-adjudicator", "codex-sol-adjudicator"]
               for x in gold) == 30
    ctx = ts.load_context(SET_18C_DIR)
    assert len(ctx.gold) == 120 and len(ctx.links) == 53


def test_revision_18c_refuses_other_items_and_bad_gold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: derive accepting an 18b items.jsonl with another sha256, a slug outside the 234-entry taxonomy, or an
    # adjudicator other than the Astra reconciler.
    dv = _derive_18c(monkeypatch)
    src = tmp_path / "18b"
    shutil.copytree(SET_18B_DIR, src, ignore=shutil.ignore_patterns("__pycache__"))
    (src / "items.jsonl").write_text((SET_18B_DIR / "items.jsonl").read_text("utf-8") + "\n", "utf-8")
    monkeypatch.setattr(dv, "SRC", src)
    assert dv.main(False) == 1
    assert "items.jsonl hashes to" in capsys.readouterr().out
    monkeypatch.setattr(dv, "SRC", SET_18B_DIR)
    out = tmp_path / "18c"
    shutil.copytree(SET_18C_DIR, out, ignore=shutil.ignore_patterns("__pycache__"))
    rec = read_jsonl(out / "adjudication" / "final.jsonl")
    rec[0]["techniques"][0]["slug"] = "no-such-technique"
    rec[1]["adjudicator"] = "merge-rule"
    write_jsonl(out / "adjudication" / "final.jsonl", rec)
    assert dv.main(False, out=out) == 1
    msg = capsys.readouterr().out
    assert "no-such-technique (slug)" in msg and f"{rec[1]['item_id']} (adjudicator 'merge-rule')" in msg
