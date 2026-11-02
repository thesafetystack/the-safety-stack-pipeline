"""M4 scenario, screen slice (PLAN T4.3b): the M1 synthetic world (``tests/synth/world.py``) →
``stack run fold.corpus`` → ``stack run annotate.screen`` (the real ``workflows/annotate/screen``)
→ ``stack verify``, all through the ``stack`` CLI (typer's ``CliRunner``, no subprocess).

The model is the fake (``tests/fakes/llm.py``) installed through the production seam
``$STACK_LLM_TRANSPORT=tests.fakes.llm:installed`` (``stack.llm.env_transport``); the provider
key is unset, so no network path exists even if the seam were bypassed. The ledger is a tmp file.
The world records labelled text digests only, so the texts screen reads are planted as ``text``
blobs under those digests (as ``tests/test_scenario_m3.py`` plants the in-band blobs).

X6-X8 check the ``<wf>`` files a reference build names; the world's own build names none, so
verify is also run against a build record that names this corpus and this screen run (the shape
``stack build`` writes, ``files.corpus_input`` / ``screen`` / ``screen_evidence``), and a hand
edit of one screen row turns X8 red there (the checks do read these files).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import tests.fakes.llm as fake_llm
from stack.cli import app
from stack.runs import find_run_record, write_build_record
from stack.store.backend import LocalBackend
from stack.store.parquet import read_family
from tests.fakes.annotate import hand_edit
from tests.fakes.llm import FakeLlm, ok
from tests.synth.records import sdir
from tests.synth.world import SOURCES, World, build_world

pytestmark = [pytest.mark.slow, pytest.mark.e2e]

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"  # the world's plan; its pin: tier 1 = gpt-6-luna
LUNA = "openai/gpt-6-luna"
TERRA = "openai/gpt-5.6-terra"  # llm.yaml role screen_adjudicate (D170 (C))
QUOTE = "a reward-model ensemble against reward hacking"
TEXTS = {
    "A1": f"We propose {QUOTE}. It works on three benchmarks. MARK_REL",
    "A2": "Sequence alignment of protein families. MARK_IRR",
    "A4": "A thin abstract on scalable oversight. MARK_BORD",
    "LW": "A forum post, hard to place. MARK_BORD MARK_STILL",
    "W1": "Red-teaming results for a deployed model. MARK_REL",
    "W2": "A blog post on dangerous-capability evals. MARK_REL",  # W3 shares W2's text digest
}
ANNOTATE_CHECKS = ("V-wf", "V-evidence", "policy_pins", "basis_currency", "selection_recompute")
runner = CliRunner()


def label_of(doc_id: str) -> str:
    return next(k for k, v in SOURCES.items() if v.source_id == doc_id)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)
    monkeypatch.setenv("STACK_LLM_TRANSPORT", "tests.fakes.llm:installed")
    monkeypatch.setenv("STACK_PROGRESS", "0")
    return build_world(tmp_path / "store")


def fake_model() -> FakeLlm:
    fake = FakeLlm()
    fake.on("MARK_STILL", ok({"verdict": "borderline", "rationale": "still unsure"}, cost=0.02), model=TERRA)
    fake.on(model=TERRA, reply=ok({"verdict": "relevant", "rationale": "strong reader decided"}, cost=0.02))
    fake.on("MARK_REL", ok({"verdict": "relevant", "rationale": "a technique", "buckets": ["paper"],
                            "quotes": [QUOTE, "words the text does not contain at all"]}), model=LUNA)
    fake.on("MARK_IRR", ok({"verdict": "irrelevant", "rationale": "genomics",
                            "off_topic_reason": "vocab_collision — sequences"}), model=LUNA)
    fake.on("MARK_BORD", ok({"verdict": "borderline", "rationale": "thin"}), model=LUNA)
    return fake


def cli(store: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--store", str(store), "--plan", str(PLAN), *args])
    assert res.exit_code == code, res.output
    return res.output


def run_json(store: Path, *args: str) -> dict[str, Any]:
    out = cli(store, "run", *args).strip().splitlines()  # stderr is mixed in: the seam's notice
    return json.loads(next(line for line in reversed(out) if line.startswith("{")))


def verify(store: Path, *args: str, code: int = 0) -> dict[str, dict[str, Any]]:
    out = json.loads(cli(store, "verify", "--json", *args, code=code))
    return {c["name"]: c for c in out["checks"]}


def reference_build(w: World, corpus_run: str, screen_run: str) -> Path:
    """A build record naming this corpus and screen run (the entries ``stack build`` writes)."""
    def entry(run_id: str, family: str) -> dict[str, Any]:
        rec = find_run_record(w.root, run_id)
        assert rec is not None
        o = rec.outputs[family]
        return {"file_id": o["file_id"], "path": o["path"], "file_digest": o["file_digest"], "run_id": run_id}

    b = copy.deepcopy(w.build)
    now = dt.datetime.now(dt.UTC).isoformat()
    b |= {"build_id": "bm4screen0000", "started_at": now, "finished_at": now, "inputs_pinned_at": now,
          "runs": [*b["runs"], corpus_run, screen_run]}
    b["files"] = {**b["files"], "corpus_input": entry(corpus_run, "corpus"),
                  "screen": entry(screen_run, "screen"), "screen_evidence": entry(screen_run, "screen_evidence")}
    return write_build_record(w.root, b)


def test_screen_over_the_world_corpus(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = world.root
    fake = fake_model()
    monkeypatch.setattr(fake_llm, "INSTALLED", fake)

    corpus = run_json(store, "fold.corpus", "--in", sdir("documents"), "--out", sdir("corpus"))
    rows, _ = read_family(store / corpus["outputs"]["corpus"])
    blobs = LocalBackend(store / "blobs")
    texts = {}
    for r in rows.to_pylist():
        digest = (r["content"] or {}).get("current_text_sha256")
        if digest is not None:
            texts[label_of(r["doc_id"])] = TEXTS["W2" if label_of(r["doc_id"]) == "W3" else label_of(r["doc_id"])]
            size = next(s["text"]["bytes"] for s in r["content"]["snapshots"]
                        if s["snapshot_id"] == r["content"]["current_snapshot_id"])
            body = texts[label_of(r["doc_id"])].encode()
            blobs.put(digest, body + b" " * (size - len(body)))  # at the size the row records (X5, D690)
    assert sorted(texts) == ["A1", "A2", "A4", "LW", "W1", "W2", "W3"]

    # the build line's --in (D373): the corpus file and the (still empty) screen series dirs
    prior = ("--in", sdir("screen"), "--in", sdir("screen_evidence"))
    s1 = run_json(store, "annotate.screen", "--in", corpus["outputs"]["corpus"], *prior)
    assert s1["status"] == "ok" and s1["scope_complete"]
    screen = {label_of(r["doc_id"]): r for r in read_family(store / s1["outputs"]["screen"])[0].to_pylist()}
    assert {k: (r["status"], r["verdict"], r["adjudication_tier"], r["tier1_verdict"]) for k, r in screen.items()} == {
        "A1": ("ok", "relevant", 1, "relevant"), "A2": ("ok", "irrelevant", 1, "irrelevant"),
        "A4": ("ok", "relevant", 2, "borderline"), "LW": ("ok", "borderline", 2, "borderline"),
        "W1": ("ok", "relevant", 1, "relevant"), "W2": ("ok", "relevant", 1, "relevant"),
        "W3": ("ok", "relevant", 1, "relevant")}
    a1 = screen["A1"]
    assert a1["quotes"] == [{"text": QUOTE, "offset": TEXTS["A1"].index(QUOTE)}]
    assert TEXTS["A1"][a1["quotes"][0]["offset"]:][:len(QUOTE)] == QUOTE
    assert screen["A2"]["off_topic_reason"].startswith("vocab_collision")
    assert all(r["basis_kind"] == "text" and r["is_basis_current"] and r["tier1_model"] == LUNA
               for r in screen.values())
    evidence = read_family(store / s1["outputs"]["screen_evidence"])[0].to_pylist()
    assert len(evidence) == 9 and sorted(e["model"] for e in evidence).count(TERRA) == 2
    assert fake.count() == 9 and fake.count(model=TERRA) == 2
    rec = find_run_record(store, s1["run_id"])  # the run's spend is in its record (T-STORE)
    assert rec is not None and rec.spend == {"usd_micro": {f"openrouter/{LUNA}": 7000, f"openrouter/{TERRA}": 40_000},
                                             "reserved_open_usd_micro": 0, "batch_ids": [],
                                             "abandoned_batch_ids": []}

    checks = verify(store)
    assert {n: checks[n]["status"] for n in ANNOTATE_CHECKS} == dict.fromkeys(ANNOTATE_CHECKS, "ok")
    assert {c["status"] for c in checks.values()} <= {"ok", "skipped"}
    build = reference_build(world, corpus["run_id"], s1["run_id"])
    checks = verify(store, "--build", str(build))
    assert {n: checks[n]["status"] for n in ANNOTATE_CHECKS} == dict.fromkeys(ANNOTATE_CHECKS, "ok")
    assert {c["status"] for c in checks.values()} <= {"ok", "skipped"}, {
        n: c["items"] for n, c in checks.items() if c["status"] not in ("ok", "skipped")}

    # rerun on the series heads (s1's files): zero transport calls, the same screen rows
    s2 = run_json(store, "annotate.screen", "--in", corpus["outputs"]["corpus"], *prior)
    assert s2["status"] == "ok" and fake.count() == 9
    assert s2["outputs"]["screen"].startswith("data/screen/main/") and s2["outputs"]["screen"] != s1["outputs"]["screen"]
    again = read_family(store / s2["outputs"]["screen"])[0].to_pylist()
    assert again == read_family(store / s1["outputs"]["screen"])[0].to_pylist()

    # the X checks do read these files: a hand-edited verdict is red under the reference build
    rec = find_run_record(store, s1["run_id"])
    assert rec is not None

    def flip(rows: list[dict[str, Any]]) -> None:
        for r in rows:
            if label_of(r["doc_id"]) == "A2":
                r["verdict"] = "relevant"

    hand_edit(store, rec, "screen", flip)
    checks = verify(store, "--build", str(build), code=1)
    assert checks["selection_recompute"]["status"] == "failed"
    assert any("differs in" in i and "'verdict'" in i for i in checks["selection_recompute"]["items"])
