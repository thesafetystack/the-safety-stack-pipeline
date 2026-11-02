"""``stack eval`` (track E, :mod:`stack.evals.harness` / :mod:`stack.evals.score`) on tiny frozen sets
written in tmp, through ``stack eval …`` (typer's ``CliRunner``) and the fake model only: the
conftest's ``$STACK_LLM_TRANSPORT`` seam hands ``open_client`` the ``FakeLlm`` a test installs; the
ledger is the eval run's own, in memory.

Scenarios: (1) two models x 2 repeats write rows/summary, repeat 2 is not a cache hit, a rerun is
all cache hits at $0; (2) metrics equal hand-computed values (triage recall/precision/skip rate,
κ, repeat agreement, McNemar; screen tier-2 share, voided quotes, what tier 2 changed); (3) the
egress overlay: deepseek on a ``restricted`` item is ``skipped[egress]`` with no call, on an
``open`` item it is sent pinned to DeepInfra; (4) a moved ``input_digest`` refuses the run before
any call; (5) the estimate refuses a too-small ``--max-usd``, and a budget trip mid-run leaves
``not_attempted[budget]`` rows in an ``ok`` run; (6) screen: tier 1 gets the truncated text, tier 2
the frozen whole text, and a whole text that no longer hashes to its digest refuses; (7) affiliation
on a 2-paper set: per-author P/R/F1 (tracked and all orgs), coverage, the broadcast, anchor voids,
repeat agreement and McNemar equal hand-computed values; a moved ``input_digest`` refuses.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tests.fakes.llm as fakes
from stack.cli import app
from stack.evals import freeze as F
from stack.evals.gold import affiliation_runner
from tests.fakes.llm import FakeLlm, ok, schema_invalid
from workflows.triage.ask import document_text

pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
OVERLAY = REPO / "evals" / "egress-overlay.yaml"
SET = "2026-10-12t"
H, L = "anthropic/claude-haiku-5.5", "openai/gpt-6-luna"
DS = "deepseek/deepseek-v4.1-flash"
GEM = "google/gemini-3.5-flash-lite"  # google-vertex: egress.default allows it, `restricted` does not
TERRA = "openai/gpt-5.6-terra"  # llm.yaml role screen_adjudicate (tier 2)
runner = CliRunner()

# (title, abstract or None, gold verdict); the last two are title-only probes (production_asks false).
TRIAGE = [
    ("Alpha paper", "We study reward hacking in RL agents.", "relevant"),
    ("Bravo paper", "A survey of language model evaluation.", "borderline"),
    ("Charlie paper", "Protein folding at scale.", "not_relevant"),
    ("Delta paper", "Interpretability of sparse autoencoders.", "relevant"),
    ("Echo post", None, "not_relevant"),
    ("Foxtrot post", None, "relevant"),
]
SCREEN_TEXT = ("This document reports a red-teaming study of frontier models. The exact phrase from "
               "the text is here. It discusses jailbreak robustness and evaluation protocols.")
#: Juliet's whole text runs past the screen's 8,000-char tier-1 cap; the marker sits beyond it.
TAIL = "TAIL-MARKER only the whole text carries."
LONG_TEXT = SCREEN_TEXT + " filler." * 1100 + " " + TAIL
# (title, gold verdict, gold buckets)
SCREEN = [("Golf study", "relevant", ["paper"]), ("Hotel study", "irrelevant", []),
          ("India study", "relevant", ["eval_report"]), ("Juliet study", "borderline", ["paper"])]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), "utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def triage_set(root: Path, *, license: dict[str, str | None] | None = None) -> Path:
    items, gold = [], []
    for n, (title, abstract, verdict) in enumerate(TRIAGE, 1):
        iid = f"tri-{n:04d}"
        row = {"metadata": {"title": title, "abstract": abstract, "authors": [{"name": "A. Author"}]}}
        record = document_text(row, None)  # the production basis builder, as the freezer calls it
        items.append({"item_id": iid, "doc_id": f"doc-{n}", "split": "dev",
                      "stratum": {"channel_group": "paper" if abstract else "url", "channel": "x", "proxy": "kw_high"},
                      "hard": None, "license_class": (license or {}).get(iid, "open"),
                      "production_asks": abstract is not None,
                      "input": {"title": title, "authors": ["A. Author"], "abstract": abstract, "excerpt": None,
                                "record": record},
                      "input_digest": sha(record)})
        gold.append({"item_id": iid, "doc_id": f"doc-{n}",
                     "label": {"verdict": verdict, "fetch": verdict != "not_relevant"}})
    d = root / "evals" / "triage" / SET
    write_jsonl(d / "items.jsonl", items)
    write_jsonl(d / "gold.jsonl", gold)
    (d / "manifest.json").write_text(json.dumps({"set": SET, "task": "triage"}), "utf-8")
    return d


def screen_set(root: Path) -> Path:
    items, gold = [], []
    d = root / "evals" / "screen" / SET
    for n, (title, verdict, buckets) in enumerate(SCREEN, 1):
        iid = f"scr-{n:04d}"
        full = LONG_TEXT if title.startswith("Juliet") else SCREEN_TEXT
        sent = full if len(full) <= 8_000 else full[:8_000] + "\n…[truncated]"  # the screen's tier-1 cut
        user = f"Title: {title}\n\nScreening text (abstract or document excerpt):\n" + sent
        items.append({"item_id": iid, "doc_id": f"sdoc-{n}", "split": "dev", "stratum": {}, "hard": None,
                      "license_class": "open", "production_asks": True,
                      "input": {"title": title, "text": sent, "text_digest": sha(full + title),
                                "basis_role": "full_text", "full_text_digest": sha(full),
                                "full_text_chars": len(full)},
                      "input_digest": sha(user)})
        F.text_path(d, iid).parent.mkdir(parents=True, exist_ok=True)
        F.text_path(d, iid).write_bytes(F.gzip_text(full))
        gold.append({"item_id": iid, "doc_id": f"sdoc-{n}", "label": {"verdict": verdict, "buckets": buckets}})
    write_jsonl(d / "items.jsonl", items)
    write_jsonl(d / "gold.jsonl", gold)
    return d


def tri(verdict: str, **kw: Any) -> Any:
    return ok({"verdict": verdict, "confidence": 0.8, "reasoning": "r"}, **kw)


def scr(verdict: str, quotes: list[str] | None = None, buckets: list[str] | None = None) -> Any:
    return ok({"verdict": verdict, "rationale": "because", "buckets": buckets or [], "quotes": quotes or []})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    f = FakeLlm()
    monkeypatch.setattr(fakes, "INSTALLED", f)
    return f


def script_triage(fake: FakeLlm, *, bravo_h: Any = None, cost: float = 0.001) -> None:
    h = {"Alpha": "relevant", "Bravo": "borderline", "Charlie": "not_relevant", "Delta": "relevant",
         "Echo": "not_relevant", "Foxtrot": "relevant"}
    lu = {"Alpha": "relevant", "Bravo": "not_relevant", "Charlie": "relevant", "Echo": "not_relevant",
          "Foxtrot": "relevant"}
    for t, v in h.items():
        reply = bravo_h if (t == "Bravo" and bravo_h is not None) else tri(v, cost=cost)
        fake.on(f"Title: {t} ", reply, model=H)
    for t, v in lu.items():
        fake.on(f"Title: {t} ", tri(v, cost=cost), model=L)
    fake.on("Title: Delta ", schema_invalid(cost=cost), model=L)  # three schema-invalid attempts: a failure


def cli(root: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", *args, "--evals", str(root / "evals"),
                              "--out", str(root / "out"), "--overlay", str(OVERLAY)])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


def run_dirs(root: Path, task: str = "triage") -> list[Path]:
    base = root / "out" / task / SET
    return sorted(p for p in base.iterdir() if p.name != "cache") if base.exists() else []


def test_two_models_two_repeats_then_rerun_from_cache(tmp_path: Path, fake: FakeLlm) -> None:
    triage_set(tmp_path)
    script_triage(fake)
    args = ("triage", "--set", SET, "--prompt", "safety-relevance-v1", "--split", "dev", "--models", f"{H},{L}", "--repeat", "2", "--workers", "4")
    cli(tmp_path, *args)
    (run1,) = run_dirs(tmp_path)
    rows = read_jsonl(run1 / "rows.jsonl")
    assert len(rows) == 6 * 2 * 2
    assert {r["status"] for r in rows if not (r["model"] == L and r["item_id"] == "tri-0004")} == {"ok"}
    assert {(r["status"], r["error_class"]) for r in rows if r["model"] == L and r["item_id"] == "tri-0004"} == {
        ("failed_permanent", "schema_invalid")}
    # repeat 2 made its own calls (its own cache): 2 x (6 + 5 ok + 3 schema attempts) requests
    assert not any(r["cache_hit"] for r in rows)
    assert fake.count() == 2 * (6 + 5 + 3)
    assert fake.count(model=H) == 12
    run = json.loads((run1 / "run.json").read_text())
    assert run["status"] == "ok" and run["models"] == [H, L] and run["repeat"] == 2
    assert run["settled_usd"] == pytest.approx(0.001 * 28)
    assert sum(run["spend"]["usd_micro"].values()) == 28_000 and run["spend"]["reserved_open_usd_micro"] == 0
    assert run["pins"][H]["name"] == "safety-relevance-v1" and run["pins"][H]["model"] == H
    assert (run1 / "summary.md").read_text().startswith("# stack eval triage")
    assert set(json.loads((run1 / "summary.json").read_text())["per_model"]) == {H, L}

    cli(tmp_path, *args)  # the same command again: every answered row from the cache, $0, no request
    run2 = next(p for p in run_dirs(tmp_path) if p != run1)
    rows2 = read_jsonl(run2 / "rows.jsonl")
    assert all(r["cache_hit"] and r["cost_usd"] == 0 for r in rows2 if r["status"] == "ok")
    assert fake.count() == 28 + 2 * 3  # only the never-cached schema failure is re-asked
    spend = read_jsonl(tmp_path / "out" / "spend.jsonl")
    assert [s["settled_usd"] for s in spend] == pytest.approx([0.028, 0.006])


def test_metrics_equal_hand_computed_values(tmp_path: Path, fake: FakeLlm) -> None:
    triage_set(tmp_path)
    script_triage(fake, bravo_h=[tri("borderline"), tri("relevant")])  # haiku changes its mind on repeat 2
    cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", f"{H},{L}", "--repeat", "2", "--workers", "1")
    (run,) = run_dirs(tmp_path)
    s = json.loads((run / "summary.json").read_text())["per_model"]
    lt, ht = s[L]["triage"], s[H]["triage"]
    # luna fetches Alpha, Charlie, Foxtrot; gold fetch = Alpha, Bravo, Delta (failed), Foxtrot
    assert lt["all"]["recall_fetch"] == pytest.approx(2 / 4)
    assert lt["all"]["precision_fetch"] == pytest.approx(2 / 3)
    assert lt["all"]["skip_rate"] == pytest.approx(3 / 6)
    assert lt["all"]["accuracy3"] == pytest.approx(3 / 6)
    assert lt["all"]["kappa3"] == pytest.approx(5 / 23)
    assert lt["production_asks"]["recall_fetch"] == pytest.approx(1 / 3)
    assert lt["title_only"]["recall_fetch"] == pytest.approx(1.0)
    assert [x["item_id"] for x in lt["skipped_gold_fetch"]] == ["tri-0002", "tri-0004"]
    # haiku: Bravo borderline then relevant → fetched 7 of 8 gold-fetch rows; skipped 5 of 12 rows
    assert ht["all"]["recall_fetch"] == pytest.approx(7 / 8)
    assert ht["all"]["skip_rate"] == pytest.approx(5 / 12)
    assert ht["all"]["recall_fetch_rb"] == pytest.approx(1.0)
    assert s[H]["general"]["repeat_agreement"] == pytest.approx(5 / 6)
    assert s[H]["general"]["repeat_kappa_1_2"] == pytest.approx(0.7)
    assert s[L]["general"]["parse_schema_failures"] == 2
    mc = s[L]["mcnemar"]  # repeat 1, fetch correctness vs haiku: haiku-only right on Charlie and Delta
    assert (mc["baseline_only_correct"], mc["model_only_correct"], mc["p_value"]) == (2, 0, pytest.approx(0.5))
    assert s[H]["mcnemar"] is None
    md = (run / "summary.md").read_text()
    assert "USD / 1,000 docs" in md and "tri-0004 'Delta paper' [2/2]" in md

    screen_set(tmp_path)
    fake.on("Title: Golf study", scr("relevant", ["The exact phrase from the text is here.",
                                                   "qwerty zxcvb plugh never said"], ["paper"]), model=H)
    fake.on("Title: Hotel study", scr("borderline"), model=H)
    fake.on("Title: India study", scr("irrelevant"), model=H)
    fake.on("Title: Juliet study", scr("borderline"), model=H)
    fake.on("Title: Hotel study", scr("irrelevant"), model=TERRA)
    fake.on("Title: Juliet study", scr("relevant", buckets=["paper"]), model=TERRA)
    cli(tmp_path, "screen", "--set", SET, "--models", H, "--workers", "2")
    (srun,) = run_dirs(tmp_path, "screen")
    rows = {r["item_id"]: r for r in read_jsonl(srun / "rows.jsonl")}
    assert (rows["scr-0001"]["quotes_kept"], rows["scr-0001"]["quotes_voided"]) == (1, 1)
    assert rows["scr-0002"]["tier1_verdict"] == "borderline" and rows["scr-0002"]["verdict"] == "irrelevant"
    assert rows["scr-0002"]["adjudication_tier"] == 2 and rows["scr-0003"]["adjudication_tier"] == 1
    sb = json.loads((srun / "summary.json").read_text())["per_model"][H]["screen"]
    assert sb["tier2_share"] == pytest.approx(2 / 4)
    assert sb["quote_verbatim_rate"] == pytest.approx(1 / 2) and sb["quotes_voided"] == 1
    assert sb["accuracy"] == pytest.approx(2 / 4)
    assert sb["relevant_recall"] == pytest.approx(1 / 2) and sb["relevant_precision"] == pytest.approx(1 / 2)
    assert sb["tier2_changes"] == {"borderline->irrelevant": 1, "borderline->relevant": 1}
    assert (sb["tier2_tier1_correct"], sb["tier2_final_correct"]) == (1, 1)
    assert sb["bucket_jaccard"] == pytest.approx((1 + 0 + 1) / 3)  # gold-kept: Golf, India (no answer bucket), Juliet
    # tier 1 read Juliet's first 8,000 chars; tier 2 the whole frozen text
    assert fake.count(model=H, contains="Title: Juliet") == 1 and fake.count(model=H, contains=TAIL) == 0
    assert fake.count(model=H, contains="…[truncated]") == 1
    assert fake.count(model=TERRA, contains=TAIL) == 1 and fake.count(model=TERRA, contains="Title: Hotel") == 1


def test_egress_overlay_skips_restricted_and_pins_deepinfra(tmp_path: Path, fake: FakeLlm) -> None:
    triage_set(tmp_path, license={"tri-0001": "restricted"})
    fake.on("Title: Bravo ", tri("borderline"), model=DS)
    out = cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", DS, "--items", "tri-0001,tri-0002")
    (run,) = run_dirs(tmp_path)
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    assert (rows["tri-0001"]["status"], rows["tri-0001"]["error_class"]) == ("skipped", "egress")
    assert rows["tri-0002"]["status"] == "ok"
    assert fake.count() == 1 and fake.count(contains="Title: Alpha ") == 0
    assert fake.requests[0].payload["provider"]["only"] == ["deepinfra"]
    assert fake.requests[0].payload["provider"]["data_collection"] == "deny"
    g = json.loads((run / "summary.json").read_text())["per_model"][DS]["general"]
    assert (g["egress_skipped"], g["scored"]) == (1, 1)
    assert "egress-skipped   1" in out
    # production llm.yaml is untouched: no deepseek route there
    assert "deepseek" not in (REPO / "configs" / "llm.yaml").read_text().split("egress:")[1].split("budget:")[0]


def test_a_null_license_class_is_gated_as_production_gates_it(tmp_path: Path, fake: FakeLlm) -> None:
    # S-41: production passes a NULL licence class as `restricted` (stack/annotate/stage.py,
    # workflows/triage/ask.py); `restricted` excludes google-vertex, egress.default allows it. Red under
    # the raw item["license_class"] in the triage adapter / the egress pre-check: the google call is made.
    triage_set(tmp_path, license={"tri-0001": None})
    fake.on("Title: Alpha ", tri("relevant"), model=GEM)
    fake.on("Title: Bravo ", tri("borderline"), model=GEM)
    cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", GEM,
        "--items", "tri-0001,tri-0002")
    (run,) = run_dirs(tmp_path)
    rows = {r["item_id"]: r for r in read_jsonl(run / "rows.jsonl")}
    assert (rows["tri-0001"]["status"], rows["tri-0001"]["error_class"]) == ("skipped", "egress")
    assert rows["tri-0001"]["license_class"] is None  # the row records the frozen value
    assert rows["tri-0002"]["status"] == "ok" and fake.count(contains="Title: Alpha ") == 0
    from stack.evals import harness

    assert harness.license_of({"license_class": None}) == "restricted"
    assert harness.license_of({"license_class": "open"}) == "open"


def test_moved_input_digest_refuses_before_any_call(tmp_path: Path, fake: FakeLlm) -> None:
    d = triage_set(tmp_path)
    items = read_jsonl(d / "items.jsonl")
    items[2]["input"]["abstract"] = "Protein folding at scale, revised."  # production would now send other text
    write_jsonl(d / "items.jsonl", items)
    script_triage(fake)
    out = cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, code=1)
    assert "tri-0003" in out and "input_digest" in out and "new set" in out
    assert fake.count() == 0 and run_dirs(tmp_path) == []


def test_screen_whole_text_moved_refuses(tmp_path: Path, fake: FakeLlm) -> None:
    d = screen_set(tmp_path)
    F.text_path(d, "scr-0004").write_bytes(F.gzip_text(LONG_TEXT + " edited"))  # tier 1's cut is unchanged
    out = cli(tmp_path, "screen", "--set", SET, "--models", H, code=1)
    assert "scr-0004" in out and "full_text_digest" in out and "new set" in out
    assert fake.count() == 0 and run_dirs(tmp_path, "screen") == []


def test_budget_refused_by_estimate_and_tripped_mid_run(tmp_path: Path, fake: FakeLlm) -> None:
    triage_set(tmp_path)
    script_triage(fake, cost=0.01)
    out = cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--max-usd", "0.0001", code=1)
    assert "estimate" in out and "--max-usd" in out
    assert fake.count() == 0 and run_dirs(tmp_path) == []

    # the estimate (~$0.0003 per call reserved) passes; the actuals ($0.01 each) trip the run budget
    out = cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--max-usd", "0.025", "--workers", "1")
    (run,) = run_dirs(tmp_path)
    rj = json.loads((run / "run.json").read_text())
    assert rj["estimate"]["reserve_usd"] < 0.025 and rj["status"] == "ok" and rj["budget_tripped"]
    rows = read_jsonl(run / "rows.jsonl")
    assert [(r["status"], r["error_class"]) for r in rows] == [("ok", None)] * 3 + [("not_attempted", "budget")] * 3
    assert fake.count() == 3
    g = json.loads((run / "summary.json").read_text())["per_model"][H]["general"]
    assert (g["not_attempted"], g["scored"]) == (3, 3)
    assert "budget tripped" in out


def test_a_code_error_in_one_row_keeps_the_other_rows(tmp_path: Path, fake: FakeLlm) -> None:
    """An exception other than ``LlmError`` in one row (a code bug) still fails the command, but
    only after ``rows.jsonl`` (the rows that finished), ``run.json`` (``failed``, the error) and the
    spend line are written (review L5 LOW, D432); before, all three were lost."""
    triage_set(tmp_path)
    fake.on("Title: Charlie ", RuntimeError("adapter bug"), model=H)  # before the script: the first match wins
    script_triage(fake)
    with pytest.raises(RuntimeError, match="adapter bug"):
        cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--workers", "2")
    (run,) = run_dirs(tmp_path)
    rows = read_jsonl(run / "rows.jsonl")
    assert len(rows) == 5 and {r["status"] for r in rows} == {"ok"}
    rj = json.loads((run / "run.json").read_text())
    assert rj["status"] == "failed" and "adapter bug" in rj["error"] and rj["settled_usd"] == pytest.approx(0.005)
    assert [s["run_dir"] for s in read_jsonl(tmp_path / "out" / "spend.jsonl")] == [str(run)]


# ── affiliation ──────────────────────────────────────────────────────────────────────────────

AFF_WINDOWS = {
    "aff-0001": "Paper A\nAnn Lee1, Bo Chen2, Cy Diaz1\n1 Anthropic, San Francisco 2 University of Toronto\n"
                "Abstract. We study A.",
    "aff-0002": "Paper B\nDee Evans, Eli Fox, Fay Gu, Gus Hu\nGoogle DeepMind, London\nMIT\nOpenAI\n"
                "Abstract. We study B.",
}
AFF_META = {"aff-0001": ["Ann Lee", "Bo Chen", "Cy Diaz"], "aff-0002": ["Dee Evans", "Eli Fox", "Fay Gu", "Gus Hu"]}
AFF_GOLD = {
    "aff-0001": [("Ann Lee", ["Anthropic, San Francisco"], ["anthropic"]), ("Bo Chen", ["University of Toronto "], []),
                 ("Cy Diaz", ["Anthropic, San Francisco"], ["anthropic"])],
    "aff-0002": [("Dee Evans", ["Google DeepMind, London"], ["deepmind"]), ("Eli Fox", ["MIT,"], []),
                 ("Fay Gu", ["MIT"], []), ("Gus Hu", ["OpenAI"], ["openai"])],
}


def aff_set(root: Path) -> Path:
    main = affiliation_runner()
    items, gold = [], []
    for iid, window in AFF_WINDOWS.items():
        block = main.ex.build_authors_block(AFF_META[iid])
        user = main._user_message(block, window)  # the runner's glue, as the freezer calls it
        items.append({"item_id": iid, "doc_id": f"adoc-{iid[-1]}", "split": "dev",
                      "stratum": {"channel_group": "paper", "channel": "arxiv", "proxy": "byline"}, "hard": None,
                      "license_class": "open", "production_asks": True, "production_uses_llm": True,
                      "input": {"title": f"Paper {iid[-1]}", "window": window, "authors_meta": AFF_META[iid],
                                "authors_block": block, "user": user, "xml_snippet": None, "xml_byline": None,
                                "roster_entries": []},
                      "input_digest": sha(user)})
        gold.append({"item_id": iid, "doc_id": f"adoc-{iid[-1]}", "label": {"authors": [
            {"name": n, "orgs": o, "roster_ids": r} for n, o, r in AFF_GOLD[iid]]}})
    d = root / "evals" / "affiliation" / SET
    write_jsonl(d / "items.jsonl", items)
    write_jsonl(d / "gold.jsonl", gold)
    return d


def aff(*authors: tuple[str, list[str]]) -> Any:
    return ok({"authors": [{"author_surface": n, "affiliations": [{"surface": s} for s in affs]}
                           for n, affs in authors]})


def test_affiliation_metrics_broadcast_and_digest(tmp_path: Path, fake: FakeLlm) -> None:
    d = aff_set(tmp_path)
    a_ok = aff(("Ann Lee", ["Anthropic, San Francisco"]), ("Bo Chen", ["University of Toronto"]),
               ("Cy Diaz", ["Anthropic, San Francisco"]))
    b_r1 = aff(("Dee Evans", ["Google DeepMind, London"]), ("Eli Fox", ["MIT"]), ("Fay Gu", ["MIT"]),
               ("Gus Hu", ["OpenAI"]))
    b_r2 = aff(("Dee Evans", ["Google DeepMind, London"]), ("Eli Fox", ["MIT"]), ("Fay Gu", []),
               ("Gus Hu", ["OpenAI"]))
    fake.on("Paper A\n", a_ok, model=H)
    fake.on("Paper B\n", [b_r1, b_r2], model=H)  # haiku drops Fay's MIT on repeat 2
    # luna: one hallucinated surface (voided by anchoring), a shared org on Bo, and OpenAI broadcast to all of B
    fake.on("Paper A\n", aff(("Ann Lee", ["Anthropic"]), ("Bo Chen", ["Anthropic", "University of Toronto"]),
                             ("Cy Diaz", ["Imaginary Institute of Nowhere"])), model=L)
    fake.on("Paper B\n", aff(("Dee Evans", ["OpenAI", "Google DeepMind, London"]), ("Eli Fox", ["OpenAI"]),
                             ("Fay Gu", ["OpenAI"]), ("Gus Hu", ["OpenAI"]), ("Zed Extra", ["OpenAI"])), model=L)
    out = cli(tmp_path, "affiliation", "--set", SET, "--models", f"{H},{L}", "--repeat", "2", "--workers", "1")
    (run,) = run_dirs(tmp_path, "affiliation")
    rows = read_jsonl(run / "rows.jsonl")
    assert len(rows) == 2 * 2 * 2 and {r["status"] for r in rows} == {"ok"}
    assert fake.count() == 8
    req = fake.requests[0]  # production's call: the extract.md template, the runner's user message, the model
    assert req.model == H and "```authors\nAnn Lee\nBo Chen\nCy Diaz\n```" in req.payload["messages"][-1]["content"]
    lb = next(r for r in rows if r["model"] == L and r["item_id"] == "aff-0002" and r["repeat"] == 1)
    assert [a["name"] for a in lb["authors"]] == AFF_META["aff-0002"]  # Zed Extra dropped by align_to_roster
    assert lb["authors"][0]["roster_ids"] == ["openai", "deepmind"] and lb["returned_authors"] == 5
    assert lb["roster_mismatch"] == {"roster": 4, "returned": 5, "matched": 4}
    assert lb["production_uses_llm"] is True

    s = json.loads((run / "summary.json").read_text())["per_model"]
    la, ha = s[L]["affiliation"], s[H]["affiliation"]
    # luna, per repeat: tracked tp 3 fp 4 fn 1; all tp 4 fp 5 fn 3 (both repeats alike, so the ratios hold)
    assert (la["tracked"]["tp"], la["tracked"]["fp"], la["tracked"]["fn"]) == (6, 8, 2)
    assert la["tracked"]["precision"] == pytest.approx(3 / 7) and la["tracked"]["recall"] == pytest.approx(3 / 4)
    assert la["tracked"]["f1"] == pytest.approx(6 / 11)
    assert (la["all"]["tp"], la["all"]["fp"], la["all"]["fn"]) == (8, 10, 6)
    assert la["all"]["precision"] == pytest.approx(4 / 9) and la["all"]["recall"] == pytest.approx(4 / 7)
    assert la["all"]["f1"] == pytest.approx(0.5)
    assert la["byline_coverage"] == 1.0 and la["org_coverage"] == 1.0 and la["gold_authors"] == 14
    assert la["broadcast"] == 2 and [(b["item_id"], b["repeat"], b["org"]) for b in la["broadcasts"]] == [
        ("aff-0002", 1, "id:openai"), ("aff-0002", 2, "id:openai")]
    assert la["broadcasts"][0]["pred_share"] == 1.0 and la["broadcasts"][0]["gold_share"] == 0.25
    assert la["anchor_void_rate"] == pytest.approx(2 / 18)  # 4 + 5 surfaces per repeat (Zed's never aligned)
    # haiku: tracked perfect; all misses Fay's MIT on repeat 2 ("MIT," and "MIT" normalise equal)
    assert (ha["tracked"]["precision"], ha["tracked"]["recall"]) == (1.0, 1.0)
    assert (ha["all"]["tp"], ha["all"]["fp"], ha["all"]["fn"]) == (13, 0, 1)
    assert ha["org_coverage"] == pytest.approx(13 / 14) and ha["broadcast"] == 0 and ha["anchor_void_rate"] == 0.0
    assert s[H]["general"]["repeat_agreement"] == pytest.approx(1 / 2)
    assert s[L]["general"]["repeat_agreement"] == 1.0 and s[L]["general"]["verdicts"] is None
    mc = s[L]["mcnemar"]  # repeat 1, per author: haiku right on all 7, luna on Ann and Gus only
    assert (mc["pairs"], mc["baseline_only_correct"], mc["model_only_correct"]) == (7, 5, 0)
    assert mc["p_value"] == pytest.approx(2 / 32)
    assert "aff-0002 r1 id:openai (100% vs gold 25%)" in out

    # a moved input digest refuses before any call
    items = read_jsonl(d / "items.jsonl")
    items[1]["input"]["authors_meta"] = ["Dee Evans", "Eli Fox", "Fay Gu"]  # production would now send other text
    write_jsonl(d / "items.jsonl", items)
    before = fake.count()
    out = cli(tmp_path, "affiliation", "--set", SET, "--models", H, code=1)
    assert "aff-0002" in out and "input_digest" in out and "new set" in out
    assert fake.count() == before and len(run_dirs(tmp_path, "affiliation")) == 1


def test_affiliation_gold_merge_compares_normalised_orgs(tmp_path: Path) -> None:
    from stack.evals import gold

    d = aff_set(tmp_path)
    opus = [{"item_id": "aff-0001", "authors": [
                {"name": "Ann Lee", "orgs": ["Anthropic, San Francisco"], "roster_ids": ["anthropic"]},
                {"name": "Bo Chen", "orgs": ["University of Toronto"], "roster_ids": []}]},
            {"item_id": "aff-0002", "authors": [
                {"name": "Dee Evans", "orgs": ["Google DeepMind, London"], "roster_ids": ["deepmind"]},
                {"name": "Eli Fox", "orgs": ["MIT"], "roster_ids": []}]}]
    codex = [{"item_id": "aff-0001", "authors": [  # case, spacing, edge punctuation, name order: the same label
                {"name": "Lee, Ann", "orgs": ["anthropic,  San Francisco."], "roster_ids": ["anthropic"]},
                {"name": "Bo Chen", "orgs": ["University of Toronto"], "roster_ids": []}]},
             {"item_id": "aff-0002", "authors": [  # Dee's roster id differs: a disagreement
                {"name": "Dee Evans", "orgs": ["Google DeepMind, London"], "roster_ids": []},
                {"name": "Eli Fox", "orgs": ["MIT"], "roster_ids": []}]}]
    write_jsonl(d / "labels" / "opus-01.jsonl", opus)
    write_jsonl(d / "labels" / "codex-01.jsonl", codex)
    rep = gold.merge("affiliation", d)
    assert (rep["agree"], rep["disagree"]) == (1, 1)
    assert [x["item_id"] for x in read_jsonl(d / "labels" / "disagreements.jsonl")] == ["aff-0002"]
    assert rep["per_author"] == {"authors": 4, "authors_matched": 4, "roster_ids_agree": 0.75,
                                 "orgs_agree": 1.0, "both_agree": 0.75}


def test_affiliation_discovery_mode_matching() -> None:
    """No authors_meta: predicted authors are matched to gold by name key, then by production's
    align_to_roster (``A. Lee`` = ``Ann Lee``); an unmatched predicted author's orgs are false
    positives; byline coverage counts gold authors with a matched record."""
    from stack.annotate.stage import stack_config
    from stack.canon import Canon
    from stack.evals import score

    canon = Canon.from_config(stack_config())
    gold = {"aff-0009": {"item_id": "aff-0009", "label": {"authors": [
        {"name": "Ann Lee", "orgs": ["OpenAI"], "roster_ids": ["openai"]},
        {"name": "Bo Chen", "orgs": ["MIT"], "roster_ids": []}]}}}
    row = {"item_id": "aff-0009", "model": H, "repeat": 1, "status": "ok", "authors": [
        {"name": "A. Lee", "affiliations": [{"surface": "OpenAI", "org_slugs": ["openai"]}]},
        {"name": "Zed Extra", "affiliations": [{"surface": "Anthropic", "org_slugs": ["anthropic"]}]}]}
    b = score.affiliation_block([row], gold, canon)
    assert b["byline_coverage"] == 0.5 and b["org_coverage"] == 0.5
    assert (b["tracked"]["tp"], b["tracked"]["fp"], b["tracked"]["fn"]) == (1, 1, 0)
    assert (b["all"]["tp"], b["all"]["fp"], b["all"]["fn"]) == (1, 1, 1)


def test_triage_pin_from_the_plans_gate_question(tmp_path: Path, fake: FakeLlm) -> None:
    """The triage eval takes its pin from the plan's gate question clause (``prompt:`` names the
    ``prompts`` key), ``--prompt`` overrides the name; a plan without one needs ``--prompt``."""
    import yaml

    triage_set(tmp_path)
    data = yaml.safe_load(DEV_PLAN.read_text("utf-8"))
    data["plan"] = "eval-gate-q"
    data["prompts"] = {**data["prompts"], "triage.qx": {"name": "safety-relevance-v2", "role": "classify"}}
    data["gate"]["screen"] = [{"question": "safety_relevance", "prompt": "triage.qx", "fetch_on": ["relevant"]}]
    plan = tmp_path / "eval-gate-q.yaml"
    plan.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    est = ("triage", "--set", SET, "--models", H, "--estimate")

    def pin_of(*extra: str) -> dict[str, Any]:
        before = set(run_dirs(tmp_path))
        cli(tmp_path, *est, "--plan", str(plan), *extra)
        (new,) = set(run_dirs(tmp_path)) - before
        return dict(json.loads((new / "run.json").read_text())["pins"][H])

    p = pin_of()
    assert (p["name"], p["role"], p["prompt_key"]) == ("safety-relevance-v2", "classify", "triage.qx")
    p = pin_of("--prompt", "safety-relevance-v1")
    assert (p["name"], p["role"], p["prompt_key"]) == ("safety-relevance-v1", "classify", "triage.qx")
    # dev-v2's gate has no question clause: --prompt is required (no silent fallback to v1)
    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", *est, "--evals", str(tmp_path / "evals"),
                              "--out", str(tmp_path / "out"), "--overlay", str(OVERLAY)])
    assert res.exit_code == 1 and "no gate question clause" in res.output + str(res.stderr_bytes or b"", "utf-8")
    cli(tmp_path, *est, "--prompt", "safety-relevance-v1")
    assert fake.count() == 0


def test_workers_default_64_and_cap_1024(tmp_path: Path, fake: FakeLlm) -> None:
    """``--workers`` unset is 64 and the bound is 1..1024 (D716; 1..256 in D692, were 8 and 32), the stages' own."""
    triage_set(tmp_path)
    script_triage(fake)
    cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H)
    (run,) = run_dirs(tmp_path)
    assert json.loads((run / "run.json").read_text())["workers"] == 64
    cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--workers", "1024")
    assert "--workers must be 1..1024" in cli(tmp_path, "triage", "--set", SET, "--prompt", "safety-relevance-v1",
                                             "--models", H, "--workers", "1025", code=1)
