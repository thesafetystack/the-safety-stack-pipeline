"""Track E task E11a: provider prompt-cache usage per row (``stack eval triage`` + ``score``), its
backfill from response-cache entries for rows written without it, and ``stack eval combine``
(vote / escalate / single) on hand-made rows. Fake model only (the conftest seam), no network.

(1) Usage bodies with cached / cache-write / reasoning tokens give the row fields, the per-model
cached-token share and both USD per 1,000 docs (settled; at the ``llm.yaml`` table price); a
rerun replays every row from the response cache and still reports the original calls' cost.
(2) Rows stripped of the fields are backfilled by ``stack eval score --run`` from the cache
entries, each run to its own prompt's entry when two prompts share a cache directory.
(3) ``combine`` on hand-made triage and screen rows equals hand-computed metrics, coverage,
escalation share and costs; bad specs are refused by name; the gold is the set's default
(``gold-v2.jsonl`` when present) or ``--gold``, and a run of another ``items.jsonl`` refuses (S-39).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import tests.fakes.llm as fakes
from stack.evals import combine as C
from stack.evals import score as S
from stack.llm import LlmResponse
from tests.fakes.llm import FakeLlm
from tests.test_eval_harness import (
    DEV_PLAN,
    SET,
    cli,
    read_jsonl,
    run_dirs,
    runner,
    triage_set,
    write_jsonl,
)

H = "anthropic/claude-haiku-5.5"  # llm.yaml: $0.1 / $0.5 per 1M
L, SON = "openai/gpt-6-luna", "anthropic/claude-sonnet-5.5"


def bare(*args: str, code: int = 0) -> str:
    """``stack eval <args>`` without the run options :func:`cli` appends (score, combine)."""
    from stack.cli import app

    res = runner.invoke(app, ["--plan", str(DEV_PLAN), "eval", *args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    assert res.exit_code == code, res.output
    return res.output


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLlm:
    f = FakeLlm()
    monkeypatch.setattr(fakes, "INSTALLED", f)
    return f


def usage_reply(verdict: str, *, prompt: int, cached: int, write: int, completion: int, reasoning: int,
                cost: float) -> LlmResponse:
    """A 200 with the usage block OpenRouter returns (the shape of the real cached bodies)."""
    body = {"choices": [{"message": {"content": json.dumps(
                {"verdict": verdict, "confidence": 0.8, "reasoning": "r"})}}],
            "provider": "Anthropic",
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
                      "cost": cost, "is_byok": False,
                      "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write,
                                                "audio_tokens": 0, "video_tokens": 0},
                      "completion_tokens_details": {"reasoning_tokens": reasoning, "audio_tokens": 0,
                                                    "image_tokens": 0}}}
    return LlmResponse(200, {}, body)


A: dict[str, Any] = {"prompt": 1000, "cached": 800, "write": 0, "completion": 100, "reasoning": 40, "cost": 0.00005}
B: dict[str, Any] = {"prompt": 2000, "cached": 0, "write": 1500, "completion": 50, "reasoning": 0, "cost": 0.0003}
NOCACHE_A = (1000 * 0.1 + 100 * 0.5) / 1e6  # 0.00015
NOCACHE_B = (2000 * 0.1 + 50 * 0.5) / 1e6  # 0.000225


def test_cache_usage_fields_share_and_cost_per_1000(tmp_path: Path, fake: FakeLlm) -> None:
    triage_set(tmp_path)
    fake.on("Title: Alpha ", usage_reply("relevant", **A), model=H)
    fake.on("Title: Bravo ", usage_reply("borderline", **B), model=H)
    args = ("triage", "--set", SET, "--prompt", "safety-relevance-v1", "--models", H, "--items", "tri-0001,tri-0002", "--workers", "1")
    cli(tmp_path, *args)
    (run1,) = run_dirs(tmp_path)
    rows = {r["item_id"]: r for r in read_jsonl(run1 / "rows.jsonl")}
    a, b = rows["tri-0001"], rows["tri-0002"]
    assert (a["prompt_tokens"], a["cached_tokens"], a["cache_write_tokens"], a["completion_tokens"],
            a["reasoning_tokens"]) == (1000, 800, 0, 100, 40)
    assert (b["cached_tokens"], b["cache_write_tokens"]) == (0, 1500)
    assert a["call_cost_usd"] == pytest.approx(0.00005) and b["call_cost_usd"] == pytest.approx(0.0003)
    assert a["cost_nocache_usd"] == pytest.approx(NOCACHE_A) and b["cost_nocache_usd"] == pytest.approx(NOCACHE_B)
    assert a["usage_source"] == "response" and a["usage_missing"] == 0
    c = json.loads((run1 / "summary.json").read_text())["per_model"][H]["cache"]
    assert c["cached_share"] == pytest.approx(800 / 3000)
    assert c["usd_per_1000_settled"] == pytest.approx(1000 * (0.00005 + 0.0003) / 2)
    assert c["usd_per_1000_nocache"] == pytest.approx(1000 * (NOCACHE_A + NOCACHE_B) / 2)
    assert c["reasoning_tokens_per_row"] == pytest.approx(20) and c["cache_write_tokens"] == 1500
    assert "USD/1k settled (provider cache)" in (run1 / "summary.md").read_text()

    cli(tmp_path, *args)  # replayed from the response cache: $0 settled, the original calls' numbers kept
    run2 = next(p for p in run_dirs(tmp_path) if p != run1)
    rows2 = {r["item_id"]: r for r in read_jsonl(run2 / "rows.jsonl")}
    assert all(r["cache_hit"] and r["cost_usd"] == 0 for r in rows2.values())
    assert rows2["tri-0001"]["usage_source"] == "cache_entry"
    assert rows2["tri-0002"]["call_cost_usd"] == pytest.approx(0.0003)
    c2 = json.loads((run2 / "summary.json").read_text())["per_model"][H]["cache"]
    assert (c2["usd_per_1000_settled"], c2["usd_per_1000_nocache"], c2["cached_share"]) == pytest.approx(
        (c["usd_per_1000_settled"], c["usd_per_1000_nocache"], c["cached_share"]))
    assert c2["rows_replayed"] == 2


def _strip_usage(run: Path) -> None:
    rows = read_jsonl(run / "rows.jsonl")
    write_jsonl(run / "rows.jsonl", [{k: v for k, v in r.items() if k not in S.USAGE_FIELDS} for r in rows])


def test_score_backfills_usage_from_cache_entries_per_prompt(tmp_path: Path, fake: FakeLlm) -> None:
    triage_set(tmp_path)
    # the two prompts share the cache directory <cache>/<haiku>/r1; each reply is told apart by its system text
    fake.on("wider research community", usage_reply("relevant", **A), model=H)   # safety-relevance-v1
    fake.on("Bias toward recall", usage_reply("borderline", **B), model=H)       # safety-relevance-v2
    base = ("triage", "--set", SET, "--models", H, "--items", "tri-0001", "--workers", "1")
    cli(tmp_path, *base, "--prompt", "safety-relevance-v1")
    cli(tmp_path, *base, "--prompt", "safety-relevance-v2")
    by_prompt = {json.loads((p / "run.json").read_text())["prompt"]: p for p in run_dirs(tmp_path)}
    run_v1, run_v2 = by_prompt["safety-relevance-v1"], by_prompt["safety-relevance-v2"]
    for run, want in ((run_v1, A), (run_v2, B)):
        _strip_usage(run)
        out = bare("score", "--run", str(run))
        assert "Usage backfilled from cache entries: {'backfilled': 1}" in out
        s = json.loads((run / "summary.json").read_text())
        assert s["usage"]["backfill"] == {"backfilled": 1}
        c = s["per_model"][H]["cache"]
        assert (c["prompt_tokens"], c["cached_tokens"], c["cache_write_tokens"]) == (
            want["prompt"], want["cached"], want["write"])
        assert c["usd_per_1000_settled"] == pytest.approx(1000 * want["cost"])
        assert c["rows_usage_missing"] == 0
    assert "call_cost_usd" not in read_jsonl(run_v1 / "rows.jsonl")[0]  # rows.jsonl is not rewritten


# ── combine on hand-made rows ────────────────────────────────────────────────────────────────

GOLD_T = ["relevant", "borderline", "not_relevant", "relevant", "not_relevant", "borderline"]
# luna, repeats 1-3 per item: (verdict or None = failed, confidence)
LUNA: list[list[tuple[str | None, float | None]]] = [
    [("relevant", 0.9), ("relevant", 0.9), ("borderline", 0.9)],
    [("borderline", 0.8), ("not_relevant", 0.9), ("relevant", 0.9)],   # a three-way tie
    [("not_relevant", 0.9), ("not_relevant", 0.9), ("relevant", 0.9)],
    [("borderline", 0.6), (None, None), ("borderline", 0.6)],
    [("not_relevant", 0.9), (None, None), (None, None)],              # failed repeats cast no vote
    [(None, None), (None, None), (None, None)],                        # no answer at all
]
SONNET = {2: "borderline", 4: "relevant", 6: "borderline"}  # sonnet ran on three items only


def _row(iid: str, model: str, k: int, verdict: str | None, conf: float | None, settled: float, nocache: float,
         **extra: Any) -> dict[str, Any]:
    return {"item_id": iid, "model": model, "repeat": k, "status": "ok" if verdict else "failed_permanent",
            "error_class": None if verdict else "schema_invalid", "verdict": verdict, "confidence": conf,
            "cache_hit": False, "cost_usd": settled, "prompt_tokens": 100, "cached_tokens": 0,
            "cache_write_tokens": 0, "completion_tokens": 10, "reasoning_tokens": 0, "call_cost_usd": settled,
            "cost_nocache_usd": nocache, "usage_source": "response" if verdict else "none", "usage_missing": 0,
            **extra}


def _cost(verdict: str | None, big: bool = False) -> tuple[float, float]:
    if big:
        return 0.01, 0.02
    return (0.001, 0.002) if verdict else (0.0005, 0.0005)


def _write_run(root: Path, task: str, name: str, set_dir: Path, models: list[str], prompt: str,
               rows: list[dict[str, Any]], *, tier2: bool = False, split: str = "dev") -> Path:
    d = root / "out" / task / SET / name
    d.mkdir(parents=True)
    pins = {m: {"name": prompt, "model": m, **({"tier2": tier2} if task == "screen" else {})} for m in models}
    (d / "run.json").write_text(json.dumps({  # set_items_digest: every harness run records it (S-39)
        "run_id": f"eval-{task}-{name}", "task": task, "set": SET, "split": split, "models": models,
        "prompt": prompt, "pins": pins, "set_dir": str(set_dir), "status": "ok",
        "set_items_digest": hashlib.sha256((set_dir / "items.jsonl").read_bytes()).hexdigest(),
        "cache_root": str(root / "out" / task / SET / "cache")}), "utf-8")
    write_jsonl(d / "rows.jsonl", rows)
    return d


def _triage_pool(tmp_path: Path) -> Path:
    d = tmp_path / "evals" / "triage" / SET
    write_jsonl(d / "items.jsonl", [{"item_id": f"t{n}", "doc_id": f"d{n}", "split": "dev", "production_asks": True,
                                     "input": {"title": f"T{n}"}} for n in range(1, 7)]
                + [{"item_id": "t9", "doc_id": "d9", "split": "test", "production_asks": True, "input": {}}])
    write_jsonl(d / "gold.jsonl", [{"item_id": f"t{n}", "label": {"verdict": v, "fetch": v != "not_relevant"}}
                                   for n, v in enumerate(GOLD_T, 1)] + [
        {"item_id": "t9", "label": {"verdict": "relevant", "fetch": True}}])
    luna = [_row(f"t{n}", L, k, v, c, *_cost(v)) for n, reps in enumerate(LUNA, 1) for k, (v, c) in enumerate(reps, 1)]
    _write_run(tmp_path, "triage", "20261012T100000Z_dev_aaaa", d, [L], "safety-relevance-v2", luna)
    son = [_row(f"t{n}", SON, 1, v, 0.9, *_cost(v, big=True)) for n, v in SONNET.items()]
    _write_run(tmp_path, "triage", "20261012T110000Z_dev_bbbb", d, [SON], "safety-relevance-v2", son)
    # a test-split run is not pooled with dev
    _write_run(tmp_path, "triage", "20261012T120000Z_test_cccc", d, [L], "safety-relevance-v2",
               [_row("t9", L, 1, "relevant", 0.9, 0.001, 0.002)], split="test")
    return d


def test_combine_vote_escalate_single_triage(tmp_path: Path) -> None:
    _triage_pool(tmp_path)
    specs = ["vote:gpt-6-luna:3", "escalate:gpt-6-luna:claude-sonnet-5.5:borderline",
             "escalate:gpt-6-luna:claude-sonnet-5.5:conf<0.7", "single:gpt-6-luna:safety-relevance-v2",
             f"single:{SON}"]
    out = bare("combine", "triage", "--set", SET, "--split", "dev", "--out", str(tmp_path / "out"),
               *[x for s in specs for x in ("--spec", s)])
    res = {r["spec"]: r for r in C.combine("triage", SET, "dev", specs, out_root=tmp_path / "out", write=False,
                                          price_for=lambda m: None)["results"]}
    v = res["vote:gpt-6-luna:3"]
    # final: relevant, relevant (tie → most inclusive), not_relevant, borderline, not_relevant, no answer
    assert (v["covered"], v["items"]) == (6, 6)
    assert v["recall_fetch"] == pytest.approx(2 / 4) and v["precision_fetch"] == pytest.approx(1.0)
    assert v["recall_fetch_rb"] == pytest.approx(3 / 4) and v["precision_fetch_rb"] == pytest.approx(1.0)
    assert v["accuracy3"] == pytest.approx(3 / 6) and v["kappa3"] == pytest.approx(4 / 13)
    assert v["usd_per_1000_settled"] == pytest.approx(1000 * 0.015 / 6)  # every call of the 3 repeats
    assert v["usd_per_1000_nocache"] == pytest.approx(1000 * 0.027 / 6)
    assert v["calls_per_item"] == pytest.approx(3.0)

    e = res["escalate:gpt-6-luna:claude-sonnet-5.5:borderline"]  # escalates t2, t4 (borderline) and t6 (no answer)
    assert e["escalated"] == 3 and e["escalation_share"] == pytest.approx(0.5)
    assert e["accuracy3"] == pytest.approx(1.0) and e["kappa3"] == pytest.approx(1.0)
    assert e["recall_fetch"] == pytest.approx(2 / 4) and e["recall_fetch_rb"] == pytest.approx(1.0)
    assert e["usd_per_1000_settled"] == pytest.approx(1000 * (0.0055 + 3 * 0.01) / 6)
    assert e["usd_per_1000_nocache"] == pytest.approx(1000 * (0.0105 + 3 * 0.02) / 6)

    ec = res["escalate:gpt-6-luna:claude-sonnet-5.5:conf<0.7"]  # t4 (0.6) and t6 (no answer); t2 stays borderline
    assert ec["escalated"] == 2 and ec["escalation_share"] == pytest.approx(2 / 6)
    assert ec["accuracy3"] == pytest.approx(1.0)
    assert ec["usd_per_1000_settled"] == pytest.approx(1000 * (0.0055 + 2 * 0.01) / 6)

    s1 = res["single:gpt-6-luna:safety-relevance-v2"]
    assert s1["accuracy3"] == pytest.approx(4 / 6) and s1["usd_per_1000_settled"] == pytest.approx(1000 * 0.0055 / 6)
    son = res[f"single:{SON}"]
    assert (son["covered"], son["items"]) == (3, 6) and son["accuracy3"] == pytest.approx(1.0)

    assert "| vote:gpt-6-luna:3 | 6/6 |" in out and "written:" in out
    assert list((tmp_path / "out" / "triage" / SET).glob("combine-*.md"))


def test_combine_screen_vote_quotes_and_refusals(tmp_path: Path) -> None:
    d = tmp_path / "evals" / "screen" / SET
    gold = ["relevant", "borderline", "irrelevant"]
    write_jsonl(d / "items.jsonl", [{"item_id": f"s{n}", "doc_id": f"d{n}", "split": "dev", "input": {}}
                                    for n in range(1, 4)])
    write_jsonl(d / "gold.jsonl", [{"item_id": f"s{n}", "label": {"verdict": v, "buckets": []}}
                                   for n, v in enumerate(gold, 1)])
    reps = {1: [("relevant", 0), ("relevant", 2)], 2: [("irrelevant", 0), ("borderline", 1)],
            3: [("borderline", 1), ("borderline", 0)]}
    rows = [_row(f"s{n}", H, k, v, None, 0.001, 0.002, quotes_kept=q, adjudication_tier=1, tier1_verdict=v)
            for n, rr in reps.items() for k, (v, q) in enumerate(rr, 1)]
    _write_run(tmp_path, "screen", "20261012T100000Z_dev_dddd", d, [H], "screen-v8", rows)
    r = C.combine("screen", SET, "dev", ["vote:claude-haiku-5.5:2"], out_root=tmp_path / "out", write=False,
                  price_for=lambda m: None)["results"][0]
    # final: s1 relevant (repeat 1: no quote), s2 borderline (tie → inclusive; repeat 2: 1 quote), s3 borderline (r1, quoted)
    assert r["accuracy"] == pytest.approx(2 / 3) and r["quoted_share"] == pytest.approx(2 / 3)
    assert (r["relevant_recall"], r["relevant_precision"]) == (1.0, 1.0)
    assert r["usd_per_1000_settled"] == pytest.approx(2.0)

    for bad, msg in (("escalate:claude-haiku-5.5:claude-haiku-5.5:conf<0.5", "triage only"),
                     ("vote:nope:2", "not in the rows"), ("vote:claude-haiku-5.5:x", "not an integer"),
                     ("single:claude-haiku-5.5:screen-v7", "no rows of variant"), ("best:claude-haiku-5.5", "spec")):
        out = bare("combine", "screen", "--set", SET, "--spec", bad, "--out", str(tmp_path / "out"), code=1)
        assert msg in out, (bad, out)


def test_combine_takes_the_sets_gold_and_refuses_runs_of_another_item_set(tmp_path: Path) -> None:
    # S-39. Red under the old load_pool: gold.jsonl hard-coded (items 6, not gold-v2's 3) and no
    # set_items_digest check (the moved items.jsonl combines: DID NOT RAISE).
    d = _triage_pool(tmp_path)
    write_jsonl(d / "gold-v2.jsonl", [{"item_id": f"t{n}", "label": {"verdict": v, "fetch": v != "not_relevant"}}
                                      for n, v in enumerate(GOLD_T[:3], 1)])

    def single(**kw: Any) -> dict[str, Any]:
        return C.combine("triage", SET, "dev", ["single:gpt-6-luna"], out_root=tmp_path / "out", write=False,
                         price_for=lambda m: None, **kw)

    res = single()
    assert res["results"][0]["items"] == 3 and res["gold"] == "gold-v2.jsonl"  # the harness's default rule
    res = single(gold="gold.jsonl")
    assert (res["gold"], res["results"][0]["items"]) == ("gold.jsonl", 6)
    with pytest.raises(C.CombineError, match="gold file .*gold-v3.jsonl does not exist"):
        single(gold="gold-v3.jsonl")
    out = bare("combine", "triage", "--set", SET, "--spec", "single:gpt-6-luna", "--out", str(tmp_path / "out"),
               "--gold", "gold.jsonl")
    assert "Gold: gold.jsonl." in out

    before = hashlib.sha256((d / "items.jsonl").read_bytes()).hexdigest()
    items = read_jsonl(d / "items.jsonl")
    items[0]["input"]["title"] = "T1, re-frozen"
    write_jsonl(d / "items.jsonl", items)
    now = hashlib.sha256((d / "items.jsonl").read_bytes()).hexdigest()
    with pytest.raises(C.CombineError, match=f"20261012T100000Z_dev_aaaa: set_items_digest {before[:12]} .*"
                                             f"differs from .*items.jsonl's {now[:12]}"):
        single()


def test_pair_screens_and_compare_refuse_runs_of_another_item_set(tmp_path: Path) -> None:
    # The same check on combine's siblings that join runs with the set's current items (S-39). Red under:
    # _load_run / _load_screen_run not calling score.items_digest_problem.
    d = tmp_path / "evals" / "screen" / SET
    write_jsonl(d / "items.jsonl", [{"item_id": "s1", "doc_id": "d1", "split": "dev", "input": {}}])
    rows = [_row("s1", H, 1, "relevant", None, 0.001, 0.002, question_mode="stack")]
    a = _write_run(tmp_path, "screen", "20261012T100000Z_dev_a", d, [H], "screen-stack-v1", rows)
    b = _write_run(tmp_path, "screen", "20261012T100000Z_dev_b", d, [H], "screen-stack-v1", rows)
    run_a = json.loads((a / "run.json").read_text())
    del run_a["set_items_digest"]  # a run that recorded none
    (a / "run.json").write_text(json.dumps(run_a))
    with pytest.raises(C.CombineError, match="set_items_digest unrecorded"):
        C._load_run(a, "stack", lambda m: None)
    with pytest.raises(ValueError, match="_dev_a: set_items_digest unrecorded"):
        S.compare_runs(a, b, price_for=lambda m: None, write=False)
    C._load_run(b, "stack", lambda m: None)  # b ran on the current items.jsonl
    write_jsonl(d / "items.jsonl", [{"item_id": "s1", "doc_id": "d1", "split": "dev", "input": {"moved": 1}}])
    with pytest.raises(C.CombineError, match="_dev_b: set_items_digest .* differs from"):
        C._load_run(b, "stack", lambda m: None)
