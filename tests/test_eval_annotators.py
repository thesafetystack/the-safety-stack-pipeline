"""``stack eval model_roles | technique_roles | model_mentions`` (track AE, :mod:`stack.evals.annotators`)
with the fake model only (the conftest's ``$STACK_LLM_TRANSPORT`` seam; the ledger is the run's own, in memory).

(a) the input check passes on the current sets (technique_roles 2026-10-13a; model_roles 2026-10-17a, registry
version 3, every item asked) and refuses a moved whole text, a moved user message or menu, and an item frozen as
"no call"; (a2) model_roles set 2026-10-17a: the input check passes 37/37 under the frozen prompt and under other
prompt versions (fills only); its gold is 2026-10-16a's with the documented remap and the new items, its nodes exist,
every menu node has a gold row; the README's command as an estimate; (b) a 2-item run per task through
``stack eval …``: rows, payload, cost, an empty-menu item; (c) the scorer on hand-computed cases, the extras block; (d) the model_mentions scorer incl. the lenient ancestor match, and the
``stack eval model_mentions`` run on a tmp copy; (e) the menu source: the live sets record their
taxonomy / vocabulary digest, a gold id absent from the current file refuses the run before any call, a digest-only
change warns. Each test names the mutation that turns it red.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tests.fakes.llm as fakes
from stack.cli import app
from stack.evals import freeze as F
from stack.evals import harness, score
from stack.modelvocab import load_vocab
from tests.eval_texts import eval_text_file, require_eval_texts
from tests.fakes.llm import FakeLlm, ok

pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
OVERLAY = REPO / "evals" / "egress-overlay.yaml"
SET = "2026-10-13a"
H = "anthropic/claude-haiku-5.5"
ASKS, NO_ASK = "an-0001", "an-0025"  # an-0001: 5 strong mentions; an-0025: none (2026-10-13a production_asks false)
MR_SET = "2026-10-17a"  # model_roles' current set (registry version 3)
#: configs/models.yaml when the live model_roles sets were frozen: models 0.9.0, whose content models 1.0.0 (D707)
#: keeps with only the `version` field changed and models 1.1.0 (the current file, D738) in the catalogue envelope; so
#: their gold ids stay in the menu source, and an eval run on them warns that the digest moved.
MODELS_0_9_0_SHA256 = "ce35fb95729991de3a5cea8d29829d22ed202b236f6a57308140cc23a9643315"
EMPTY = "an-0026"  # 2026-10-17a: a real text naming no AI model (empty menu, still a call)
runner = CliRunner()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows), "utf-8")


def copy_set(root: Path, task: str, ids: list[str] | None = None, set_id: str = SET) -> Path:
    """A tmp copy of the real frozen set (``ids``: only those items), whole texts from the eval-text cache."""
    src = REPO / "evals" / task / set_id
    require_eval_texts(src)
    d = root / "evals" / task / set_id
    items = [i for i in read_jsonl(src / "items.jsonl") if ids is None or i["item_id"] in ids]
    write_jsonl(d / "items.jsonl", items)
    (d / "texts").mkdir(parents=True, exist_ok=True)
    for i in items:
        shutil.copyfile(eval_text_file(src, i["item_id"]), F.text_path(d, i["item_id"]))
    shutil.copyfile(src / "manifest.json", d / "manifest.json")
    return d


def adapter(task: str, set_dir: Path) -> harness.Adapter:
    from stack.annotate.stage import stack_config

    a = harness.make_adapter(task, stack_config(), None, prompt=None, models=[H])
    a.set_dir = set_dir
    return a


def cli(root: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", *args, "--evals", str(root / "evals"),
                              "--out", str(root / "out"), "--overlay", str(OVERLAY)])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    f = FakeLlm()
    monkeypatch.setattr(fakes, "INSTALLED", f)
    return f


# ── (a) the input check ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("task", "set_id", "n"), [pytest.param("model_roles", MR_SET, 37, marks=pytest.mark.slow),
                                                   ("technique_roles", SET, 25)])
def test_input_check_real_set_passes_and_refuses_moved_inputs(tmp_path: Path, task: str, set_id: str, n: int) -> None:
    # Red under: AnnotatorAdapter._ask_args building a different AskItem (e.g. `text[:-1]`), the prompt file or the
    # menu rendering moving without a re-freeze (the real set is then refused), or check_inputs skipping the
    # user-message / fills comparison (the second refusal below does not happen).
    real = REPO / "evals" / task / set_id
    require_eval_texts(real)
    items = harness.select_items(harness.load_set(REPO / "evals", task, set_id).items, "all", None, None)
    assert len(items) == n
    harness.check_inputs(adapter(task, real), items)  # production still sends what was frozen

    d = copy_set(tmp_path, task, [it["item_id"] for it in items], set_id=set_id)
    text = F.read_text_gz(F.text_path(d, ASKS))
    moved = text.replace("regex classifier", "regex classifiers", 1)  # one character
    assert moved != text
    F.text_path(d, ASKS).write_bytes(F.gzip_text(moved))
    tmp_items = harness.load_set(tmp_path / "evals", task, set_id).items
    with pytest.raises(harness.EvalError, match=r"an-0001 \(whole text.*full_text_digest"):
        harness.check_inputs(adapter(task, d), tmp_items)
    # the whole-text digest updated too: now production's user message (and model_roles' fills) differ
    for it in tmp_items:
        if it["item_id"] == ASKS:
            it["input"]["full_text_digest"] = sha(moved)
    write_jsonl(d / "items.jsonl", tmp_items)
    want = r"an-0001 \(fills \(window, menu\) hash to" if task == "model_roles" else r"1 item\(s\) whose input_digest.*an-0001 \(frozen"
    with pytest.raises(harness.EvalError, match=want):
        harness.check_inputs(adapter(task, d), harness.load_set(tmp_path / "evals", task, set_id).items)


def test_input_check_refuses_an_item_frozen_without_a_call(tmp_path: Path) -> None:
    # Registry version 3 asks every document: an item frozen as "no call" (2026-10-16a and older) is refused.
    # Red under: AnnotatorAdapter.frozen_problems not checking production_asks.
    d = copy_set(tmp_path, "model_roles", [ASKS, EMPTY], set_id=MR_SET)
    items = read_jsonl(d / "items.jsonl")
    harness.check_inputs(adapter("model_roles", d), items)
    next(i for i in items if i["item_id"] == EMPTY)["production_asks"] = False
    with pytest.raises(harness.EvalError, match="an-0026 \\(frozen production_asks false, production asks every document"):
        harness.check_inputs(adapter("model_roles", d), items)


# ── (b) two-item runs through the CLI ────────────────────────────────────────────────────────

NULL_ROLES_MR = dict.fromkeys(("mentioned", "cited_work", "discussed", "usage_experience", "built_on", "instrument",
                               "modified", "evaluated_behavioral", "analyzed_internals", "introduced"))


def test_model_roles_run_rows_payload_cost_and_empty_menu_item(tmp_path: Path, fake: FakeLlm) -> None:
    # Red under: run_one mis-reporting no_call, the harness counting the empty-menu item's row as a failure, or the
    # scorer dropping it (n would be 1, J≥2 0.5 not 0.75).
    d = copy_set(tmp_path, "model_roles", [ASKS, EMPTY], set_id=MR_SET)
    text = F.read_text_gz(F.text_path(d, ASKS))
    quote = text[text.index("three translation engines"):text.index("generating 53")].rstrip(" ,")
    reply = {"doc_kind": "paper", "variant_models": [], "extra_models": [], "flags": [], "models": [
        {"node_id": "gpt-5.2", "roles": {**NULL_ROLES_MR, "mentioned": 2, "instrument": 3},
         "evidence": [{"roles": ["instrument"], "quote": quote}], "note": None, "refined_name": None},
        {"node_id": "qwen2.5-72b", "roles": {**NULL_ROLES_MR, "mentioned": 2, "instrument": 2},
         "evidence": [{"roles": ["instrument"], "quote": "a sentence the document never says qwerty"}], "note": None,
         "refined_name": None}]}
    fake.on("53 040 AdvBench", ok(reply, cost=0.003))
    fake.on("Moshe Tennenholtz", ok({"doc_kind": "commentary", "models": [], "variant_models": [], "extra_models": [],
                                     "flags": []}, cost=0.001))
    gold = [{"item_id": ASKS, "doc_id": "x", "contested": False, "labelers": ["t"], "adjudication_note": None,
             "label": {"mentions": [], "mentions_out_of_vocab": [], "off_menu_models": [], "models": [
                 {"node_id": "gpt-5.2", "roles": {"mentioned": 2, "instrument": 3}},
                 {"node_id": "internlm", "roles": {"mentioned": 2, "evaluated_behavioral": 2}}]}},
            {"item_id": EMPTY, "doc_id": "y", "contested": False, "labelers": ["t"], "adjudication_note": None,
             "label": {"mentions": [], "mentions_out_of_vocab": [], "off_menu_models": [], "models": [],
                       "expected_extras": []}}]
    write_jsonl(d / "gold.jsonl", gold)
    out = cli(tmp_path, "model_roles", "--set", MR_SET, "--split", "all", "--models", H, "--workers", "2")
    (run,) = [p for p in (tmp_path / "out" / "model_roles" / MR_SET).iterdir() if p.name != "cache"]
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    assert fake.count() == 2  # registry version 3: the empty-menu item is judged too
    a, b = rows[ASKS], rows[EMPTY]
    assert (a["status"], a["ann_status"], a["no_call"], a["cost_usd"]) == ("ok", "ok", False, pytest.approx(0.003))
    graded = {m["node_id"]: m for m in a["payload"]["models"]}
    # production's repair (judge.normalize), not the raw reply: `mentioned` raised to the strongest role
    assert {k: v for k, v in graded["gpt-5.2"]["roles"].items() if v} == {"mentioned": 3, "instrument": 3}
    assert a["counts"]["quote_unverified"] == 1  # production's verification against the window
    assert (b["status"], b["ann_status"], b["no_call"], b["cost_usd"]) == ("ok", "empty", False, pytest.approx(0.001))
    assert b["payload"]["models"] == [] and b["counts"]["menu_empty"] == 1
    rj = json.loads((run / "run.json").read_text())
    assert rj["settled_usd"] == pytest.approx(0.004) and rj["pins"][H]["name"] == "model-roles-v2"
    assert rj["estimate"]["per_model"][H].get("no_call", 0) == 0 and rj["estimate"]["per_model"][H]["calls"] == 2
    s = json.loads((run / "summary.json").read_text())["per_model"][H]
    mr, g, ex = s["model_roles"], s["general"], s["extras"]
    assert (mr["n"], mr["answered"], mr["no_call"]) == (2, 2, 0)
    # pred ≥2 cells (gpt, m) (gpt, i) (qwen, m) (qwen, i); gold (gpt, m) (gpt, i) (internlm, m) (internlm, e)
    assert (mr["cells_ge2"]["tp"], mr["cells_ge2"]["fp"], mr["cells_ge2"]["fn"]) == (2, 2, 2)
    assert mr["jaccard_ge2"] == pytest.approx((2 / 6 + 1.0) / 2)  # the empty item: both empty = 1
    assert g["cost_per_1000_usd"] == pytest.approx(1000 * 0.004 / 2)
    assert (ex["n"], ex["menu_spurious"]) == (1, 0)  # only the empty item's gold lists expected extras
    assert "## model_roles" in out and "USD / 1,000 docs" in out and "### off-catalogue extras" in out


def test_model_roles_run_records_the_vocabulary_digests(tmp_path: Path, fake: FakeLlm) -> None:
    # D591: the run scans with today's vocabulary; run.json records both digests and nothing warns. Red under:
    # AnnotatorAdapter.provenance returning {} (no vocab_digest).
    d = copy_set(tmp_path, "model_roles", [EMPTY], set_id=MR_SET)
    fake.on("Moshe Tennenholtz", ok({"doc_kind": "commentary", "models": [], "flags": []}))
    current = hashlib.sha256((REPO / "configs" / "models.yaml").read_bytes()).hexdigest()
    # the copy as if frozen on today's file (the real set records models 0.9.0, MODELS_0_9_0_SHA256)
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["frozen_with"]["models_yaml_sha256"] == MODELS_0_9_0_SHA256
    manifest["frozen_with"]["models_yaml_sha256"] = current
    (d / "manifest.json").write_text(json.dumps(manifest))
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", "model_roles", "--set", MR_SET, "--split", "all",
                              "--models", H, "--evals", str(tmp_path / "evals"), "--out", str(tmp_path / "out"),
                              "--overlay", str(OVERLAY)])
    assert res.exit_code == 0, res.output
    (run_dir,) = [p for p in (tmp_path / "out" / "model_roles" / MR_SET).iterdir() if p.name != "cache"]
    rj = json.loads((run_dir / "run.json").read_text())
    assert (rj["vocab_digest"], rj["frozen_vocab_digest"]) == (current, current) and "warnings" not in rj
    assert "warning:" not in res.stderr and "**Warning:**" not in (run_dir / "summary.md").read_text()


# ── (a2) model_roles set 2026-10-17a (registry version 3; prompt model-roles-v2) ─────────────────


@pytest.mark.slow
def test_model_roles_set_passes_the_input_check_for_any_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                fake: FakeLlm) -> None:
    # Red under: a re-freeze needed (production's scan, menu or prompt moved), the adapter comparing the whole user
    # message under another prompt (model-roles-v1 and a v3 copy below refused).
    real = REPO / "evals" / "model_roles" / MR_SET
    require_eval_texts(real)
    items = harness.select_items(harness.load_set(REPO / "evals", "model_roles", MR_SET).items, "all", None, None)
    assert len(items) == 37 and all(i["production_asks"] for i in items)
    a = adapter("model_roles", real)
    assert a.checks_user_message()  # the frozen prompt (model-roles-v2): the whole user message is compared too
    harness.check_inputs(a, items)
    a1 = harness.make_adapter("model_roles", a.config, None, prompt="model-roles-v1", models=[H])
    a1.set_dir = real
    assert not a1.checks_user_message()
    harness.check_inputs(a1, items)
    import workflows.annotate.model_roles.main as mr

    here = tmp_path / "wf"
    shutil.copytree(mr.HERE / "prompts", here / "prompts")
    v2 = (here / "prompts" / "model-roles-v2.md").read_text("utf-8")
    (here / "prompts" / "model-roles-v3.md").write_text(v2.replace("# Role", "# Your role", 1), "utf-8")
    monkeypatch.setattr(mr, "HERE", here)
    a3 = harness.make_adapter("model_roles", a.config, None, prompt="model-roles-v3", models=[H])
    a3.set_dir = real
    harness.check_inputs(a3, items)
    assert fake.count() == 0


def test_model_roles_set_gold_is_the_documented_remap_and_new_items() -> None:
    """``gold.jsonl`` = 2026-10-16a's gold with ``gold-remap.jsonl`` applied plus ``gold-new.jsonl`` (re-derived
    here with the freeze script's own function); every gold node exists in the vocabulary; the new items carry
    ``expected_extras``; the README names every remapped item; every menu node has a gold row. Red under: a silent gold edit, a
    remap entry not applied, or a stale README."""
    import importlib.util

    d = REPO / "evals" / "model_roles" / MR_SET
    spec = importlib.util.spec_from_file_location("_freeze17", d / "scripts" / "freeze.py")
    assert spec is not None and spec.loader is not None
    fz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fz)
    src = REPO / "evals" / "model_roles" / "2026-10-16a"
    gold = {g["item_id"]: g for g in read_jsonl(src / "gold.jsonl")}
    remap = read_jsonl(d / "gold-remap.jsonl")
    require_eval_texts(d)
    texts = {i["item_id"]: F.read_text_gz(eval_text_file(d, i["item_id"])) for i in read_jsonl(d / "items.jsonl")}
    fz.apply_remap(gold, remap, texts)
    for g in read_jsonl(d / "gold-new.jsonl"):
        gold[g["item_id"]] = g
    assert [gold[k] for k in sorted(gold)] == read_jsonl(d / "gold.jsonl")
    nodes = load_vocab(REPO / "configs").nodes
    assert all(m["node_id"] in nodes for g in gold.values() for m in g["label"]["models"])
    sections: dict[str, set[str]] = {}
    for e in remap:
        sections.setdefault(e.get("section", "v0.5"), set()).add(e["item_id"])
    assert sorted(sections["v0.5"]) == ["an-0003", "an-0005", "an-0010", "an-0011", "an-0017", "an-0019", "an-0021",
                                        "an-0023"]
    assert sorted(sections["v0.7"]) == ["an-0001", "an-0017", "an-0019", "an-0021"]  # catalogue v0.7 folds (D595)
    new = {i["item_id"] for i in read_jsonl(d / "items.jsonl") if int(i["item_id"][3:]) > 25}
    assert new == {g["item_id"] for g in gold.values() if "expected_extras" in g["label"]}
    readme = (d / "README.md").read_text("utf-8")
    assert all(k in readme for ids in sections.values() for k in ids)
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["frozen_with"]["models_yaml_sha256"] == MODELS_0_9_0_SHA256
    # every menu node of every item has a gold row (graded or all-zero), except the concretization positives,
    # whose gold sits on the refined node (README "Score risk")
    for it in read_jsonl(d / "items.jsonl"):
        rows = {m["node_id"] for m in gold[it["item_id"]]["label"]["models"]}
        off = [m["node_id"] for m in it["input"]["menu"] if m["node_id"] not in rows]
        assert not off or it["item_id"] in ("an-0035", "an-0036"), (it["item_id"], off)


@pytest.mark.slow
def test_model_roles_set_estimate_through_the_cli(tmp_path: Path, fake: FakeLlm) -> None:
    # The README's command, as an estimate: no call, every item admitted and asked. Red under: the CLI refusing the set.
    require_eval_texts(REPO / "evals" / "model_roles" / MR_SET)
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", "model_roles", "--set", MR_SET, "--split", "all",
                              "--models", H, "--estimate", "--out", str(tmp_path / "out"), "--overlay", str(OVERLAY)])
    assert res.exit_code == 0, res.output
    (run,) = (tmp_path / "out" / "model_roles" / MR_SET).iterdir()
    rj = json.loads((run / "run.json").read_text())
    assert rj["estimate"]["per_model"][H]["calls"] == 37 and rj["estimate"]["per_model"][H].get("no_call", 0) == 0
    assert rj["frozen_vocab_digest"] == MODELS_0_9_0_SHA256 and fake.count() == 0
    assert rj["vocab_digest"] == hashlib.sha256((REPO / "configs" / "models.yaml").read_bytes()).hexdigest()


def test_technique_roles_run_rows_payload_cost(tmp_path: Path, fake: FakeLlm) -> None:
    # Red under: run_one storing the parsed reply instead of production's payload (no `verified` flags);
    # extra techniques per doc averaged over gold rows only (no gold here: n/a, not 0.5).
    d = copy_set(tmp_path, "technique_roles", [ASKS, NO_ASK])
    text = F.read_text_gz(F.text_path(d, ASKS))
    quote = text[text.index("The regex classifier"):text.index(" (Qwen")]
    reply = {"doc_kind": "paper", "flags": [], "techniques": [
        {"slug": "llm-as-judge", "roles": {"mentioned": 2, "used": 3},
         "quotes": [quote, "a fabricated sentence qwerty zxcv"]}],
        "extra_techniques": [{"name_as_written": "Wilson intervals", "roles": {"mentioned": 1},
                              "quotes": ["we report Wilson 95% confidence intervals"]}]}
    fake.on("53 040 AdvBench", ok(reply, cost=0.002))
    fake.on(None, ok({"doc_kind": "paper", "techniques": [], "extra_techniques": [], "flags": []}, cost=0.001))
    cli(tmp_path, "technique_roles", "--set", SET, "--split", "all", "--models", H)
    (run,) = [p for p in (tmp_path / "out" / "technique_roles" / SET).iterdir() if p.name != "cache"]
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    assert fake.count() == 2 and fake.count(contains="53 040 AdvBench") == 1
    a, b = rows[ASKS], rows[NO_ASK]
    assert (a["ann_status"], a["no_call"], a["cost_usd"]) == ("ok", False, pytest.approx(0.002))
    (t,) = a["payload"]["techniques"]
    assert t["slug"] == "llm-as-judge" and [q["verified"] for q in t["quotes"]] == [True, False]
    assert (b["ann_status"], b["no_call"], b["cost_usd"]) == ("empty", False, pytest.approx(0.001))
    s = json.loads((run / "summary.json").read_text())["per_model"][H]
    tr = s["technique_roles"]
    assert (tr["n"], tr["quotes"], tr["quotes_verified"]) == (0, 3, 2)  # no gold file: n 0
    assert tr["extra_per_doc"] == pytest.approx(0.5)
    assert json.loads((run / "run.json").read_text())["settled_usd"] == pytest.approx(0.003)


# ── (c) scorer, hand-computed ────────────────────────────────────────────────────────────────


def _row(item: str, status: str = "ok", payload: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return {"item_id": item, "model": H, "repeat": 1, "status": status, "error_class": kw.pop("error_class", None),
            "payload": payload, "cost_usd": 0.0, "latency_ms": 0, "cache_hit": False, **kw}


def test_model_roles_scorer_hand_case() -> None:
    # Red under (each): threshold `> t` for the ≥ t cells; a failed row dropped instead of scored empty
    # (fn 1 not 2); both-empty Jaccard 0; "truncated" removed from PARSE_FAILURES (parse_schema_failures 0);
    # general_block's latency counting the no-call row's 0 ms (median 500 not 1,000).
    gold = {"A": {"label": {"models": [{"node_id": "n1", "roles": {"mentioned": 2, "built_on": 3}},
                                       {"node_id": "n2", "roles": {"mentioned": 1}},
                                       {"node_id": "n3", "roles": {"mentioned": 0}}]}},
            "B": {"label": {"models": []}},
            "C": {"label": {"models": [{"node_id": "n4", "roles": {"mentioned": 3}}]}}}
    rows = [
        _row("A", latency_ms=1000, payload={"models": [{"node_id": "n1", "roles": {"mentioned": 2, "built_on": 1}},
                                      {"node_id": "n2", "roles": {"mentioned": 2, "instrument": 2}},
                                      {"node_id": "n3", "roles": {"mentioned": 1}}],
                           "repairs": ["quote_unverified:n2", "missing_row:n3"]}),
        _row("B", payload={"models": [], "repairs": []}, no_call=True, production_asks=False),
        _row("C", status="failed_permanent", error_class="truncated"),
    ]
    b = score.model_roles_block(rows, gold)
    assert (b["n"], b["answered"]) == (3, 2)
    assert (b["cells_ge2"]["tp"], b["cells_ge2"]["fp"], b["cells_ge2"]["fn"]) == (1, 2, 2)
    assert b["cells_ge2"]["f1"] == pytest.approx(1 / 3)
    assert (b["cells_ge1"]["precision"], b["cells_ge1"]["recall"]) == (pytest.approx(3 / 5), pytest.approx(3 / 4))
    assert b["jaccard_ge2"] == pytest.approx((1 / 4 + 1 + 0) / 3)
    assert (b["nodes"]["tp"], b["nodes"]["fp"], b["nodes"]["fn"]) == (2, 1, 1)
    assert b["strength_abs_diff"] == pytest.approx((0 + 2 + 1 + 2 + 1 + 3) / 6) and b["strength_cells"] == 6
    assert (b["repairs"], b["quote_unverified"], b["repaired_docs"], b["no_call"]) == (2, 1, 1, 1)
    assert b["called_without_production_ask"] == 0
    g = score.general_block(rows, score.cells_key("model_roles"), categorical=False)
    assert g["parse_schema_failures"] == 1 and g["latency_ms_median"] == 1000.0


def test_model_roles_extras_block_hand_case() -> None:
    # Red under: exact-only name matching (the "the Zephyrine-9B model" spelling then an FP and an FN), an expected name
    # matched twice, items without expected_extras counted, or spurious menu grades not counted.
    gold = {"A": {"label": {"models": [], "expected_extras": [{"name": "Zephyrine-9B", "aliases": ["Zephyrine"]},
                                                               {"name": "Marrowind-3"}]}},
            "B": {"label": {"models": [{"node_id": "gpt", "roles": {"mentioned": 1}}], "expected_extras": []}},
            "C": {"label": {"models": []}}}  # no expected_extras: not in the block
    rows = [_row("A", payload={"models": [], "extra_models": [{"name_as_written": "the Zephyrine-9B model"},
                                                               {"name_as_written": "the Zephyrine model"},
                                                               {"name_as_written": "GPT-4"}]}),
            _row("B", payload={"models": [{"node_id": "gpt", "roles": {"mentioned": 1}},
                                          {"node_id": "gpt-4", "roles": {"evaluated_behavioral": 2}}],
                               "extra_models": []}),
            _row("C", payload={"models": [{"node_id": "x", "roles": {"mentioned": 1}}], "extra_models": []})]
    b = score.extras_block(rows, gold)
    assert b["n"] == 2
    # A: "the Zephyrine-9B model" contains Zephyrine-9B; "the Zephyrine model" contains the alias but that name is taken
    # (one match per expected name) -> unexpected; GPT-4 unexpected; Marrowind-3 missed
    assert (b["extras"]["tp"], b["extras"]["fp"], b["extras"]["fn"]) == (1, 2, 1)
    assert (b["menu_spurious"], b["menu_spurious_items"]) == (1, ["B"])
    a = next(x for x in b["per_item"] if x["item_id"] == "A")
    assert a["matched"] == ["the Zephyrine-9B model"] and a["missed"] == ["Marrowind-3"]


def test_technique_roles_scorer_hand_case() -> None:
    # Red under (each): slug presence read from role strengths instead of row existence (s2 would not be
    # gold-present: fn 1); quotes counted over techniques only (3 quotes, 1 verified).
    gold = {"A": {"label": {"techniques": [{"slug": "s1", "roles": {"mentioned": 2, "used": 3}, "quote": "q"},
                                           {"slug": "s2", "roles": {"mentioned": 0}, "quote": "q"}]}},
            "B": {"label": {"techniques": [{"slug": "s3", "roles": {"mentioned": 3}, "quote": "q"}]}}}
    rows = [
        _row("A", payload={"techniques": [
            {"slug": "s1", "roles": {"mentioned": 2, "used": 2}, "flags": [],
             "quotes": [{"text": "x", "verified": True}, {"text": "y", "verified": False}]},
            {"slug": "s4", "roles": {"mentioned": 2}, "flags": ["quote_unverified"],
             "quotes": [{"text": "z", "verified": False}]}],
            "extra_techniques": [{"name_as_written": "E", "roles": {"mentioned": 1}, "flags": [],
                                  "quotes": [{"text": "w", "verified": True}]}], "repairs": []}),
        _row("B", payload={"techniques": [], "extra_techniques": [], "repairs": []}),
    ]
    b = score.technique_roles_block(rows, gold)
    t = b["techniques"]
    assert (t["tp"], t["fp"], t["fn"]) == (1, 1, 2) and t["f1"] == pytest.approx(0.4)
    c = b["cells_ge2"]
    assert (c["tp"], c["fp"], c["fn"]) == (2, 1, 1) and c["f1"] == pytest.approx(2 / 3)
    assert (b["quotes"], b["quotes_verified"], b["quote_verified_rate"]) == (4, 2, 0.5)
    assert (b["rows"], b["rows_quote_unverified"], b["extra_per_doc"]) == (3, 1, 0.5)


# ── (d) model_mentions ───────────────────────────────────────────────────────────────────────


def test_mentions_scorer_hand_case_with_lenient_ancestor_match() -> None:
    # Red under (each): lenient match in one direction only (`related(p, g)`: Z's llama-3-70b vs gold
    # llama unmatched → lenient strong P 2/4); weak not added to the strong ∪ weak set; oov not counted.
    from workflows.annotate.model_roles.match import is_ancestor

    nodes = {"llama": {}, "llama-3": {"parent": "llama"}, "llama-3-70b": {"parent": "llama-3"}, "gpt-4": {},
             "bert": {}}

    def related(a: str, b: str) -> bool:
        return is_ancestor(nodes, a, b)

    rows = [{"item_id": "X", "strong": ["llama-3", "gpt-4"], "weak": ["bert"], "ambiguous": 1, "frozen_differs": True},
            {"item_id": "Y", "strong": ["gpt-4"], "weak": [], "ambiguous": 0, "frozen_differs": False},
            {"item_id": "Z", "strong": ["llama-3-70b"], "weak": [], "ambiguous": 0, "frozen_differs": False},
            {"item_id": "W", "strong": ["bert"], "weak": [], "ambiguous": 0, "frozen_differs": False}]  # no gold
    gold = {"X": {"label": {"mentions": [{"node_id": "llama-3-70b", "surface": "Llama 3 70B"},
                                         {"node_id": "gpt-4", "surface": "GPT-4"}, {"node_id": "bert", "surface": "BERT"}],
                            "mentions_out_of_vocab": [{"name": "FooLM"}]}},
            "Y": {"label": {"mentions": [], "mentions_out_of_vocab": []}},
            "Z": {"label": {"mentions": [{"node_id": "llama", "surface": "Llama"}], "mentions_out_of_vocab": []}}}
    b = score.mentions_block(rows, gold, related)
    assert (b["n"], b["gold_items"], b["frozen_differs"], b["gold_oov"], b["ambiguous"]) == (4, 3, 1, 1, 1)
    s = b["strong"]
    assert (s["tp"], s["fp"], s["fn"], s["precision"], s["recall"]) == (1, 3, 3, 0.25, 0.25)
    sw = b["strong_weak"]
    assert (sw["tp"], sw["fp"], sw["fn"]) == (2, 3, 2) and sw["precision"] == pytest.approx(0.4)
    sl = b["strong_lenient"]
    assert (sl["pred_matched"], sl["pred"], sl["gold_matched"], sl["gold"]) == (3, 4, 3, 4)
    assert sl["f1"] == pytest.approx(0.75)
    swl = b["strong_weak_lenient"]
    assert (swl["precision"], swl["recall"]) == (pytest.approx(4 / 5), 1.0)
    x = next(i for i in b["per_item"] if i["item_id"] == "X")
    assert (x["fp"], x["fn"], x["oov"]) == (["llama-3"], ["bert", "llama-3-70b"], ["FooLM"])


@pytest.mark.slow
def test_model_mentions_cli_reports_a_moved_frozen_payload(tmp_path: Path) -> None:
    # Red under: mentions_rows comparing against the scan itself (frozen_differs always False), or not
    # reading the frozen whole text (the scan of "" finds nothing: FN on the gold node).
    d = copy_set(tmp_path, "model_roles", [ASKS, NO_ASK])
    items = read_jsonl(d / "items.jsonl")
    items[0]["input"]["model_mentions"]["payload"]["mentions"].pop()  # as if the vocabulary had changed since
    write_jsonl(d / "items.jsonl", items)
    write_jsonl(d / "gold.jsonl", [{"item_id": ASKS, "label": {"mentions": [{"node_id": "gpt-5.2", "surface": "GPT‐5.2"}],
                                                               "mentions_out_of_vocab": [{"name": "Yi"}]}}])
    res = runner.invoke(app, ["eval", "model_mentions", "--set", SET, "--split", "all", "--evals",
                              str(tmp_path / "evals"), "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    (run,) = list((tmp_path / "out" / "model_mentions" / SET).iterdir())
    s = json.loads((run / "summary.json").read_text())["mentions"]
    assert (s["n"], s["frozen_differs"], s["frozen_differs_items"]) == (2, 1, [ASKS])
    # fp 3 (4 before vocabulary 0.9): the NLLB node, one of an-0001's strong hits, was removed
    assert (s["strong"]["tp"], s["strong"]["fn"], s["strong"]["fp"]) == (1, 0, 3) and s["gold_oov"] == 1
    assert "differs from the frozen" in res.output.lower() or "Scan differs" in res.output
    assert len(read_jsonl(run / "rows.jsonl")) == 2


def test_eval_defaults_are_the_repositorys_whatever_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            fake: FakeLlm) -> None:
    # S25-96 (the eval half of S-6). Red under cwd-relative defaults (Path("evals"), Path("v4/evals-store"),
    # Path("evals/egress-overlay.yaml")): from another cwd the set is "no eval set evals/model_roles/…".
    import inspect

    from stack.evals import combine, gold, overlay, sr

    require_eval_texts(REPO / "evals" / "model_roles" / MR_SET, REPO / "evals" / "model_roles" / SET)
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", "model_roles", "--set", MR_SET, "--split", "all",
                              "--items", f"{ASKS},{EMPTY}", "--models", H, "--estimate", "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    (run,) = (tmp_path / "out" / "model_roles" / MR_SET).iterdir()
    rj = json.loads((run / "run.json").read_text())
    assert rj["set_dir"] == str((REPO / "evals" / "model_roles" / MR_SET).resolve())
    assert rj["egress_overlay"]["path"] == str(OVERLAY.resolve())
    assert fake.count() == 0
    res = runner.invoke(app, ["eval", "model_mentions", "--set", SET, "--split", "all", "--items", ASKS,
                              "--out", str(tmp_path / "out")])
    assert res.exit_code == 0, res.output
    assert harness.DEFAULT_OUT == REPO.resolve() / "v4" / "evals-store"
    assert harness.DEFAULT_EVALS == gold.ROOT == REPO.resolve() / "evals"
    assert overlay.DEFAULT_OVERLAY == OVERLAY.resolve()
    assert inspect.signature(combine.combine).parameters["out_root"].default == harness.DEFAULT_OUT
    assert inspect.signature(sr.append).parameters["evals_root"].default == harness.DEFAULT_EVALS
    assert inspect.signature(sr.recall).parameters["out"].default.is_absolute()


# ── (e) the menu source: gold ids must exist, a moved digest warns ───────────────────────────


def _technique_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Any) -> tuple[Path, Any]:
    """A 2-item copy of technique set 2026-10-13a with its gold, and ``main.TAXONOMY_PATH`` pointed at a
    copy of the taxonomy with ``edit`` applied to its text."""
    import importlib

    d = copy_set(tmp_path, "technique_roles", [ASKS, NO_ASK])
    src = REPO / "evals" / "technique_roles" / SET
    write_jsonl(d / "gold.jsonl", [g for g in read_jsonl(src / "gold.jsonl") if g["item_id"] in (ASKS, NO_ASK)])
    main = importlib.import_module("workflows.annotate.technique_roles.main")
    tax = tmp_path / "taxonomy-v0.8.yaml"
    tax.write_text(edit(main.TAXONOMY_PATH.read_text("utf-8")), "utf-8")
    monkeypatch.setattr(main, "TAXONOMY_PATH", tax)
    return d, main


def test_live_sets_record_their_menu_source() -> None:
    # Red under: a live set's manifest losing its menu-source digest (nothing to compare a moved file with).
    tr = json.loads((REPO / "evals" / "technique_roles" / SET / "manifest.json").read_text())["frozen_with"]
    assert tr["taxonomy_path"] == "taxonomy/taxonomy-v0.8.yaml" and tr["taxonomy_version"] == "0.8"
    assert tr["taxonomy_sha256"] == hashlib.sha256((REPO / tr["taxonomy_path"]).read_bytes()).hexdigest()
    mr = json.loads((REPO / "evals" / "model_roles" / MR_SET / "manifest.json").read_text())["frozen_with"]
    assert (mr["models_yaml_sha256"], mr["models_yaml_version"]) == (MODELS_0_9_0_SHA256, "0.9")
    # models 1.1.0 (D738): MODELS_0_9_0_SHA256's nodes and menus in the catalogue envelope
    assert load_vocab(REPO / "configs").validated.version == "1.1.0"
    # every gold id of both live sets is in today's menu source
    for task, set_id in (("technique_roles", SET), ("model_roles", MR_SET)):
        gold = {g["item_id"]: g for g in read_jsonl(REPO / "evals" / task / set_id / "gold.jsonl")}
        assert adapter(task, REPO / "evals" / task / set_id).gold_problems(gold) == [], task


def test_a_renamed_gold_slug_refuses_the_run_before_any_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             fake: FakeLlm) -> None:
    # Red under: run_eval not calling gold_problems (the run goes ahead and llm-as-judge's gold rows become misses
    # while check_inputs passes: the user message does not contain the menu).
    _technique_world(tmp_path, monkeypatch, lambda t: t.replace("- slug: llm-as-judge\n", "- slug: llm-judge\n", 1))
    out = cli(tmp_path, "technique_roles", "--set", SET, "--split", "all", "--models", H, code=1)
    assert "not in the current menu source" in out and f"{ASKS}:llm-as-judge" in out
    assert fake.count() == 0


def test_a_moved_taxonomy_digest_with_every_gold_slug_present_only_warns(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeLlm) -> None:
    # Red under: provenance comparing nothing (no warning), or the digest check refusing instead of warning.
    _technique_world(tmp_path, monkeypatch, lambda t: t + "# an edit the menu does not show\n")
    fake.on(None, ok({"doc_kind": "paper", "techniques": [], "extra_techniques": [], "flags": []}, cost=0.001))
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", "technique_roles", "--set", SET, "--split", "all",
                              "--models", H, "--evals", str(tmp_path / "evals"), "--out", str(tmp_path / "out"),
                              "--overlay", str(OVERLAY)])
    assert res.exit_code == 0, res.output
    assert "warning: technique_roles: the taxonomy file is not the one set 2026-10-13a was frozen with" in res.stderr
    (run_dir,) = [p for p in (tmp_path / "out" / "technique_roles" / SET).iterdir() if p.name != "cache"]
    rj = json.loads((run_dir / "run.json").read_text())
    assert rj["taxonomy_digest"] != rj["frozen_taxonomy_digest"] and rj["taxonomy_version"] == "0.8"
    assert len(rj["warnings"]) == 1 and fake.count() == 2


@pytest.mark.slow
def test_model_roles_gold_nodes_absent_from_the_vocabulary_are_named(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Red under: gold_ids skipping label.mentions / off_menu_models, or gold_problems comparing with the menu
    # of one document instead of the vocabulary.
    a = adapter("model_roles", REPO / "evals" / "model_roles" / MR_SET)
    gold = {"x-1": {"label": {"models": [{"node_id": "gpt-4o"}], "mentions": [{"node_id": "no-such-node"}],
                              "off_menu_models": [{"name": "Foo", "node_id": None}, {"name": "Bar", "node_id": "zz-9"}]}}}
    assert a.gold_problems(gold) == ["x-1:no-such-node", "x-1:zz-9"]
    cfg = tmp_path / "configs"
    shutil.copytree(REPO / "configs", cfg, ignore=shutil.ignore_patterns("plans"))
    with (cfg / "models.yaml").open("a", encoding="utf-8") as fh:
        fh.write("# an edit\n")
    monkeypatch.setenv("STACK_CONFIG_DIR", str(cfg))
    moved = adapter("model_roles", REPO / "evals" / "model_roles" / MR_SET)
    p = moved.provenance()
    assert p["vocab_digest"] != p["frozen_vocab_digest"] and "configs/models.yaml is not the one" in p["warnings"][0]


def test_the_technique_set_freeze_script_reproduces_the_set_at_head(monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    # Red under: the prompt file's user part or the window moving (the frozen input digests no longer reproduce),
    # or the script importing removed code (the 2026-10-13 freeze called render.render_user, removed by D591).
    from stack.stage_runner import load_path_module

    require_eval_texts(REPO / "evals" / "screen" / "2026-10-12a", REPO / "evals" / "technique_roles" / SET)
    monkeypatch.chdir(REPO)  # the script resolves the repo from the cwd, as its docstring says
    script = load_path_module("_tr_freeze_under_test",
                              REPO / "evals" / "technique_roles" / SET / "scripts" / "freeze.py")
    assert script.main(write=False) == 0
    assert "reproduced: items.jsonl byte for byte (25 items" in capsys.readouterr().out


def test_model_roles_eval_takes_doc_cap_by_pin_and_refuses_name_model_and_other_tasks() -> None:
    # Red under: --pin not reaching the model_roles pins, or accepted for technique_roles / name / model.
    from stack.annotate import stage as annotate_stage
    from stack.evals import harness

    cfg = annotate_stage.stack_config()
    a = harness.make_adapter("model_roles", cfg, None, prompt=None, models=["openai/gpt-6-luna"],
                             pin_overrides={"doc_cap": 900_000})
    assert a.plan_pins["openai/gpt-6-luna"]["doc_cap"] == 900_000
    assert not a.checks_user_message()  # the frozen input_digest describes the default pin
    assert len(a.prompt_pins["openai/gpt-6-luna"].template_digests) == len(
        harness.make_adapter("model_roles", cfg, None, prompt=None,
                             models=["openai/gpt-6-luna"]).prompt_pins["openai/gpt-6-luna"].template_digests) + 1
    for task, pins, msg in (("model_roles", {"name": "x"}, "--prompt"), ("model_roles", {"model": "x"}, "--models"),
                            ("model_roles", {"doc_cap": 5}, "doc_cap must be an integer"),
                            ("technique_roles", {"doc_cap": 900_000}, "take plan-pin overrides")):
        with pytest.raises(harness.EvalError, match=msg):
            harness.make_adapter(task, cfg, None, prompt=None, models=["openai/gpt-6-luna"], pin_overrides=pins)
