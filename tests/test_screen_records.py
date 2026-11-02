"""Screen answers as annotation records (track S, S1): one ``screen-v10`` call yields the
``screen`` record (the call-level summary) plus ``stack_relevance`` and ``sr_relevance``, each its
own typed ``annotations.<record>`` field and document-grain family, all referencing the one
``screen_evidence`` row per call (whose record struct columns carry the per-question payloads).

The scenario: four documents through ``stack build`` (``fold.corpus`` → ``annotate.screen`` →
``fold.build``) with the real screen workflow on the fake model under a plan pinning
``screen-v10``: doc 0 relevant to the Stack and not to SR (with a quote longer than the release
cap), doc 1 escalated to tier 2, doc 2 a failed call, doc 3 irrelevant on both questions with a
per-record override (target ``stack_relevance`` of the ``overrides`` family). Then: the record files, the evidence
struct columns, ``fold.build``'s typed fields, the build's verify, a release (record quotes
capped), the reader helpers (:mod:`stack.annotations`) and the duckdb macros
(:data:`stack.query.MACROS`), a record found in ``other[]`` by digest, the refused plan (folding a
record the pinned prompt does not produce), and one failing fixture per check branch that is
record-specific (V7, V8, X6, X8).

Plans are written to ``<tmp>/configs/plans/<name>.yaml`` and the test runs from ``<tmp>``, so
X6 finds the plan by the name in the file headers (``stack.build.find_plan``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq
import pytest
import yaml

import stack.annotate.stage as annotate_stage
from stack.annotations import ann_expr, annotation, pinned_digest
from stack.build import BuildFailed, dry_run, run_build
from stack.checks.verify import run_checks
from stack.config import load
from stack.core import ids
from stack.fold import payload_json
from stack.fold.release import load_release_config, resolve_destination, run_release
from stack.plan import Plan, load_plan
from stack.query import register_macros
from stack.store.layout import RELEASES
from stack.store.parquet import read_family
from tests.fakes.annotate import (
    AT_RUN,
    annotate,
    doc_with_text,
    fold_corpus,
    hand_edit,
    write_documents,
)
from tests.fakes.llm import FakeLlm, ok, transport_error
from tests.fakes.overrides import add_overrides
from tests.synth.records import SERIES, rewrite_record
from tests.synth.rows import sid

pytestmark = [pytest.mark.slow, pytest.mark.e2e]  # runner-driven builds

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
CONFIG = load()
LUNA = "openai/gpt-6-luna"
TERRA = CONFIG.llm.roles["screen_adjudicate"].model
LONG = "This sentence about reward hacking in reinforcement learning agents is long " * 5  # 375 chars
TEXTS = [
    f"DOC_BOTH. {LONG}And an SR aside on interpretability.",
    "DOC_ESCALATE. A thin abstract about embedded agency.",
    "DOC_FAILS. Whatever this says.",
    "DOC_NEITHER. A recipe for sourdough bread.",
]


def dual(verdict: str, stack: dict[str, Any], sr: dict[str, Any], cost: float = 0.001) -> Any:
    return ok({"verdict": verdict, "rationale": f"overall {verdict}", "buckets": ["paper"], "off_topic_reason": None,
               "stack": {"quotes": [], "confidence": 0.8, **stack},
               "sr": {"quotes": [], "confidence": 0.6, "category": "none", "agenda": None, **sr}}, cost=cost)


def script(fake: FakeLlm) -> FakeLlm:
    fake.on(model=TERRA, reply=dual("relevant", {"verdict": "relevant", "quotes": ["embedded agency"]},
                                    {"verdict": "irrelevant"}, cost=0.02))
    fake.on("DOC_BOTH", dual("relevant", {"verdict": "relevant", "quotes": [LONG.strip(), "never said"]},
                             {"verdict": "irrelevant", "quotes": ["interpretability"]}), model=LUNA)
    fake.on("DOC_ESCALATE", dual("borderline", {"verdict": "borderline", "confidence": 0.4},
                                 {"verdict": "irrelevant", "category": "theory", "agenda": "new:agency"}),
            model=LUNA)
    fake.on("DOC_FAILS", transport_error("connection reset"), model=LUNA)
    fake.on("DOC_NEITHER", dual("irrelevant", {"verdict": "irrelevant"}, {"verdict": "irrelevant"}), model=LUNA)
    return fake


def write_plan(cwd: Path, name: str, *, pin: str, folded: list[str]) -> Plan:
    data = yaml.safe_load(DEV_PLAN.read_text("utf-8"))
    data["plan"] = name
    data["series"] = SERIES  # the test world's series
    data["prompts"]["annotate.screen"] = {"name": pin, "model": LUNA}
    data["fold"]["build"]["annotations_folded"] = folded
    data["fold"]["diagnostic"] = False
    data["stages"] = [{"stage": "fold.corpus"},
                      {"stage": "annotate.screen", "prior": ["screen", "screen_evidence"]},
                      {"stage": "fold.build"}]
    data["require_complete"] = ["screen", "screen_evidence", "stack_relevance"]
    path = cwd / "configs" / "plans" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    return load_plan(path)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fake = script(FakeLlm())
    monkeypatch.delenv("STACK_WORKFLOWS_ROOT", raising=False)  # the real workflows/annotate/screen
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("STACK_PROGRESS", "0")
    monkeypatch.setattr(annotate_stage, "TRANSPORT_HOOK", lambda: fake)
    monkeypatch.setattr(annotate_stage, "SLEEP", lambda _s: None)
    monkeypatch.setattr(annotate_stage, "CLOCK", lambda: AT_RUN)
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "store"
    write_documents(store, [doc_with_text(store, i, t) for i, t in enumerate(TEXTS)], run=1)
    return {"store": store, "fake": fake, "cwd": tmp_path}


def rows(store: Path, entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["doc_id"]: r for r in read_family(store / entry["path"])[0].to_pylist()}


def red(store: Path) -> dict[str, tuple[str, ...]]:
    return {r.check: tuple(r.all_items) for r in run_checks(store) if r.status not in ("ok", "skipped")}


def test_screen_v10_records_through_build_release_and_readers(env: dict[str, Any]) -> None:
    store, cwd = env["store"], env["cwd"]
    p = write_plan(cwd, "s1-records", pin="screen-v10", folded=["screen", "stack_relevance", "sr_relevance"])
    add_overrides(store, "stack_relevance", [{"override_id": "o-rec", "key": {"doc_id": sid(3)},
                                               "scope": {"members": [sid(3)]}, "field": "verdict",
                                               "value": "relevant"}])
    b = run_build(store, p)
    assert b.status == "ok", b.data.get("error")
    files = b.files
    assert {"screen", "stack_relevance", "sr_relevance", "screen_evidence"} <= set(files)
    assert "text_usability" not in files  # screen-v10 does not produce it

    # evidence: one row per call (tier 2 a second row), the record struct columns filled
    ev = read_family(store / files["screen_evidence"]["path"])[0].to_pylist()
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for e in ev:
        by_doc.setdefault(e["doc_id"], []).append(e)
    assert [len(by_doc[sid(i)]) for i in range(4)] == [1, 2, 1, 1]
    e0 = by_doc[sid(0)][0]
    assert e0["stack_relevance"]["verdict"] == "relevant" and e0["sr_relevance"]["verdict"] == "irrelevant"
    assert e0["text_usability"] is None
    t1, t2 = sorted(by_doc[sid(1)], key=lambda e: e["supersedes"] is not None)
    assert t2["supersedes"] == t1["evidence_id"]
    assert (t1["stack_relevance"]["verdict"], t1["stack_relevance"]["adjudication_tier"]) == ("borderline", 1)
    assert (t2["stack_relevance"]["verdict"], t2["stack_relevance"]["tier1_verdict"],
            t2["stack_relevance"]["adjudication_tier"], t2["stack_relevance"]["model"]) == (
        "relevant", "borderline", 2, TERRA)
    assert t2["sr_relevance"]["tier1_verdict"] == "irrelevant" and t1["sr_relevance"]["agenda"] == "new:agency"
    fail = by_doc[sid(2)][0]
    assert fail["status"] == "failed_transient" and fail["stack_relevance"] is None

    # record files: the same selected evidence row as screen's; status and T2 from it
    scr, stk, srr = rows(store, files["screen"]), rows(store, files["stack_relevance"]), rows(store, files["sr_relevance"])
    for d in map(sid, range(4)):
        assert stk[d]["evidence_id"] == srr[d]["evidence_id"] == scr[d]["evidence_id"]
        assert stk[d]["config_digest"] == scr[d]["config_digest"]
        assert (stk[d]["workflow"], stk[d]["version"], srr[d]["workflow"]) == ("stack_relevance", 1, "sr_relevance")
    r0 = stk[sid(0)]
    assert (r0["status"], r0["verdict"], r0["confidence"], r0["prompt_name"], r0["model"]) == (
        "ok", "relevant", 0.8, "screen-v10", LUNA)
    assert [q["text"] for q in r0["quotes"]] == [LONG.strip()]  # the invented quote is voided
    assert [q["text"] for q in srr[sid(0)]["quotes"]] == ["interpretability"]  # anchored separately
    assert (stk[sid(1)]["verdict"], stk[sid(1)]["adjudication_tier"], stk[sid(1)]["evidence_id"]) == (
        "relevant", 2, t2["evidence_id"])
    assert (stk[sid(2)]["status"], stk[sid(2)]["error_class"], stk[sid(2)]["verdict"]) == (
        "failed_transient", "network", None)
    # the per-record override: stack_relevance only, screen keeps the model's verdict
    assert (stk[sid(3)]["verdict"], stk[sid(3)]["origin"], stk[sid(3)]["override_ids"]) == ("relevant", "human", ["o-rec"])
    assert (scr[sid(3)]["verdict"], scr[sid(3)]["override_ids"], srr[sid(3)]["override_ids"]) == ("irrelevant", [], [])

    # fold.build: typed fields; verify inside the build and over the store green
    corpus = rows(store, files["corpus"])
    a0 = corpus[sid(0)]["annotations"]
    assert a0["stack_relevance"]["payload"]["verdict"] == "relevant" and a0["text_usability"] is None
    assert a0["sr_relevance"]["is_basis_current"] is True
    assert b.data["verify"]["status"] == "ok"
    assert red(store) == {}

    # readers: the plan-pinned version, another config, other[]
    digest = pinned_digest(p, "stack_relevance")
    assert digest == r0["config_digest"] == pinned_digest("s1-records", "sr_relevance")
    rec = annotation(corpus[sid(0)], "stack_relevance", config_digest=digest)
    assert rec is not None and rec["payload"]["verdict"] == "relevant" and rec["workflow"] == "stack_relevance"
    assert annotation(corpus[sid(0)], "stack_relevance", config_digest="0" * 64) is None
    v8 = write_plan(cwd, "s1-v8", pin="screen-v8", folded=["screen"])
    assert pinned_digest(v8, "screen") != pinned_digest(p, "screen")
    assert annotation(corpus[sid(0)], "screen", config_digest=pinned_digest(v8, "screen")) is None
    with pytest.raises(ValueError, match="does not produce"):
        pinned_digest(v8, "stack_relevance")
    moved = copy.deepcopy(corpus[sid(0)])
    typed = moved["annotations"].pop("stack_relevance")
    moved["annotations"]["other"] = [{**{k: v for k, v in typed.items() if k != "payload"},
                                      "payload": {"item_key": "", "payload_json": payload_json(typed["payload"])}}]
    assert annotation(moved, "stack_relevance", config_digest="0" * 64) is None
    found = annotation(moved, "stack_relevance", config_digest=digest)
    assert found is not None and found["payload"] == typed["payload"] and found["config_digest"] == digest

    # duckdb macros over the build's corpus file
    con = register_macros(duckdb.connect())
    con.register("corpus", pq.read_table(store / files["corpus"]["path"]))
    got = con.execute(
        "SELECT doc_id, ann_ok(annotations, 'stack_relevance'), ann_verdict(annotations, 'stack_relevance'),"
        " ann_verdict(annotations, 'sr_relevance'), ann_pinned(annotations, 'stack_relevance', $d) IS NOT NULL,"
        " ann_pinned(annotations, 'stack_relevance', $o) IS NULL, ann_ok(annotations, 'text_usability')"
        " FROM corpus ORDER BY doc_id", {"d": digest, "o": "0" * 64}).fetchall()
    assert got == [(sid(0), True, "relevant", "irrelevant", True, True, False),
                   (sid(1), True, "relevant", "irrelevant", True, True, False),
                   (sid(2), False, None, None, True, True, False),
                   (sid(3), True, "relevant", "irrelevant", True, True, False)]

    # release: record quotes capped (open: 300 chars), the usability note column dropped
    rel = run_release(store, run_id=ids.mint("run"), inputs={"corpus": files["corpus"]["file_id"]},
                      release="public-v1", dest=resolve_destination(store, load_release_config("public-v1"), tag="s1"),
                      allow_unbuilt=True)
    out = {r["doc_id"]: r for r in pq.read_table(store / RELEASES / "s1" / "corpus.parquet").to_pylist()}
    q = out[sid(0)]["annotations"]["stack_relevance"]["payload"]["quotes"]
    assert len(LONG.strip()) > 300 and [len(x["text"]) for x in q] == [300]
    assert "note" not in pq.read_schema(rel.corpus_path).field("annotations").type.field(
        "text_usability").type.field("payload").type.names
    assert red(store) == {}


def test_ann_expr_polars(env: dict[str, Any]) -> None:
    pl = pytest.importorskip("polars")
    frame = pl.DataFrame({"annotations": [{"stack_relevance": {"status": "ok", "payload": {"verdict": "relevant"}}}]})
    assert frame.select(ann_expr("stack_relevance", "payload", "verdict")).item() == "relevant"


def test_plan_folding_an_unproduced_record_is_refused(env: dict[str, Any]) -> None:
    store, cwd = env["store"], env["cwd"]
    p = write_plan(cwd, "s1-refused", pin="screen-v8", folded=["screen", "stack_relevance"])
    with pytest.raises(BuildFailed, match="'stack_relevance', a record of annotate.screen"):
        dry_run(store, p)
    b = run_build(store, p)
    assert (b.status, b.error_class, b.data["runs"]) == ("failed", "missing_input", [])
    assert "produces no records" in b.data["error"] and env["fake"].count() == 0
    # the single-question pins produce no records and write no record files
    ok_plan = write_plan(cwd, "s1-v8-ok", pin="screen-v8", folded=["screen"])
    fake = env["fake"]
    fake.on(model=LUNA, reply=ok({"verdict": "relevant", "rationale": "r", "buckets": None,
                                   "off_topic_reason": None, "grounding_idx": None, "quotes": None}))
    del fake._rules[:5]  # drop the screen-v10 script: every v8 call answers the line above
    b2 = run_build(store, ok_plan)
    assert b2.status == "ok", b2.data.get("error")
    assert not {"stack_relevance", "sr_relevance", "text_usability"} & set(b2.files)
    ev = read_family(store / b2.files["screen_evidence"]["path"])[0]
    assert {"stack_relevance", "sr_relevance", "text_usability"} <= set(ev.column_names)
    assert ev.column("stack_relevance").null_count == ev.num_rows


# ---------------------------------------------------------------- record-specific check branches
@pytest.fixture
def two_runs(env: dict[str, Any]) -> tuple[Path, Any, Any, Plan]:
    """Two ``annotate.screen`` runs (the second a rerun with the first's outputs as its prior,
    ``--in``) without a build: V faults go
    into run 1's files (outside the X checks' scope, the newest file per workflow), X faults into
    run 2's (``tests/test_checks_annotate.py``'s layout)."""
    store, cwd = env["store"], env["cwd"]
    p = write_plan(cwd, "s1-checks", pin="screen-v10", folded=["screen", "stack_relevance", "sr_relevance"])
    corpus = fold_corpus(store, p)
    r1 = annotate(store, p, "screen", corpus)
    r2 = annotate(store, p, "screen", corpus, prior=r1)
    assert {"stack_relevance", "sr_relevance"} <= set(r2.outputs)
    assert red(store) == {}
    return store, r1, r2, p


def _first_ok(rows_: list[dict[str, Any]]) -> int:
    return next(i for i, r in enumerate(rows_) if r["status"] == "ok")


def test_v7_record_evidence_resolves_in_the_producers_evidence(two_runs: Any) -> None:
    store, r1, _, _ = two_runs

    def edit(rs: list[dict[str, Any]]) -> None:
        rs[0]["evidence_id"] = ids.mint("annotation")
    hand_edit(store, r1, "stack_relevance", edit)
    got = red(store)
    assert set(got) == {"V-wf"} and "screen_evidence" in got["V-wf"][0], got


def test_v8_digest_covers_the_record_columns(two_runs: Any) -> None:
    store, r1, _, _ = two_runs

    def edit(rs: list[dict[str, Any]]) -> None:
        i = _first_ok(rs)
        rs[i]["sr_relevance"] = {**rs[i]["sr_relevance"], "verdict": "relevant"}
    hand_edit(store, r1, "screen_evidence", edit)
    got = red(store)
    assert set(got) == {"V-evidence"} and "result_digest" in got["V-evidence"][0], got


def _plan_pins(name: str) -> None:
    """The plan the files name (``s1-checks``) now pins ``annotate.screen`` to ``name``."""
    path = Path("configs/plans/s1-checks.yaml")
    data = yaml.safe_load(path.read_text("utf-8"))
    data["prompts"]["annotate.screen"]["name"] = name
    path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")


def _edit_run_record(store: Path, run_id: str, change: Any) -> None:
    from stack.runs import find_run_record

    rec = find_run_record(store, run_id)
    assert rec is not None and rec.is_final
    change(rec)
    rewrite_record(store, rec)


def _x6(store: Path) -> Any:
    return next(r for r in run_checks(store) if r.check == "policy_pins")


def test_x6_judges_by_the_runs_recorded_pin(two_runs: Any) -> None:
    """R1-1 / R2-4: the plan the files name pins ``screen-v8`` after the run. X6 judges the
    evidence against the pin run 2 recorded (``prompt_pin``), so nothing is off it; it warns that
    the plan's pin moved since the run (X6 ``warn``, not ``failed``)."""
    from stack.runs import find_run_record

    store, _, r2, _ = two_runs
    pin = find_run_record(store, r2.run_id).prompt_pin
    assert pin["prompt_name"] == "screen-v10" and set(pin["produced"]) == {"stack_relevance", "sr_relevance"}
    _plan_pins("screen-v8")
    x6 = _x6(store)
    assert x6.status == "warn" and {f.level for f in x6.findings} == {"warn"}, x6
    assert all("pin differs from the one this build's run asked under" in i for i in x6.all_items), x6
    assert set(red(store)) == {"policy_pins"}


def test_x6_record_pin_is_its_producers(two_runs: Any) -> None:
    """The run's recorded pin produces no ``stack_relevance`` (``produced`` hand-edited; the config
    digest does not cover it): that record file is a record the pin does not produce (hard)."""
    store, _, r2, _ = two_runs
    _edit_run_record(store, r2.run_id, lambda r: r.prompt_pin.update(produced=["sr_relevance"]))
    x6 = _x6(store)
    assert x6.status == "failed", x6
    assert any("record stack_relevance written" in i for i in x6.all_items), x6
    assert not any("record sr_relevance written" in i for i in x6.all_items), x6


def test_x6_recorded_pin_must_hash_to_the_header(two_runs: Any) -> None:
    """A recorded pin that is not the one the header's ``config_digest`` covers is a hard finding
    (and the evidence is then off that pin's name)."""
    store, _, r2, _ = two_runs
    _edit_run_record(store, r2.run_id, lambda r: r.prompt_pin.update(prompt_name="screen-v8"))
    x6 = _x6(store)
    assert x6.status == "failed", x6
    assert any("not the header's config_digest" in i for i in x6.all_items), x6


def test_x6_old_run_without_a_recorded_pin(two_runs: Any) -> None:
    """A run from before R1-1 (no ``prompt_pin``): today's pin is used when its config digest is
    the header's (ok); after the plan's pin moved, the evidence is not judged against a pin and a
    warn finding says so (no false hard failure, the dev store's case)."""
    store, _, r2, _ = two_runs
    _edit_run_record(store, r2.run_id, lambda r: setattr(r, "prompt_pin", None))
    assert _x6(store).status == "ok"
    _plan_pins("screen-v8")
    x6 = _x6(store)
    assert x6.status == "warn", x6
    assert any("recorded no prompt_pin" in i and "not checked against a pin" in i for i in x6.all_items), x6


def test_x8_reselects_the_record(two_runs: Any) -> None:
    store, _, r2, _ = two_runs

    def edit(rs: list[dict[str, Any]]) -> None:
        rs[_first_ok(rs)]["verdict"] = "borderline"
    hand_edit(store, r2, "sr_relevance", edit)
    got = red(store)
    assert set(got) == {"selection_recompute"} and "sr_relevance" in got["selection_recompute"][0], got


def test_x8_record_missing_from_decided_evidence(two_runs: Any) -> None:
    store, _, r2, _ = two_runs
    sel = read_family(store / r2.outputs["stack_relevance"]["path"])[0].to_pylist()
    chosen = next(r["evidence_id"] for r in sel if r["status"] == "ok")

    def edit(rs: list[dict[str, Any]]) -> None:
        next(r for r in rs if r["evidence_id"] == chosen)["stack_relevance"] = None
    hand_edit(store, r2, "screen_evidence", edit)
    got = red(store)
    assert any("has no stack_relevance payload" in i for i in got["selection_recompute"]), got
    assert set(got) <= {"selection_recompute", "V-evidence"}, got


def test_payload_json_round_trip_keeps_types() -> None:
    """``annotation`` parses an ``other[]`` payload back into the registry type (ints, floats,
    nested quotes)."""
    payload = {"verdict": "relevant", "confidence": 0.5, "quotes": [{"text": "q", "offset": 3}], "category": "theory",
               "agenda": None, "prompt_name": "screen-v10", "model": LUNA, "basis_role": "full_text",
               "adjudication_tier": 1, "tier1_verdict": "relevant"}
    row = {"annotations": {"sr_relevance": None, "other": [
        {"workflow": "sr_relevance", "version": 1, "status": "ok", "error_class": None, "basis_kind": "text",
         "basis_digest": "b" * 64, "config_digest": "c" * 64, "decided_at": AT_RUN, "evidence_id": None,
         "origin": "ai_check", "is_basis_current": True,
         "payload": {"item_key": "", "payload_json": json.dumps(payload)}}]}}
    got = annotation(row, "sr_relevance")
    assert got is not None and got["payload"] == payload
    assert annotation(row, "sr_relevance", config_digest="c" * 64) == got
    assert annotation(row, "stack_relevance") is None


def test_accepted_rows_carry_their_records(env: dict[str, Any]) -> None:
    """D758: a build under another screen-v10 config (the same prompt; the pin names its template digest) with an
    ``accepted:`` list naming the first build's config asks only the document without a decided answer; each record
    file selects the same accepted evidence row as ``screen`` (its record columns), labelled with the old config;
    verify is green."""
    from stack.stage_runner import load_stage_module

    store, cwd, fake = env["store"], env["cwd"], env["fake"]
    folded = ["screen", "stack_relevance", "sr_relevance"]
    pa_ = write_plan(cwd, "s1-acc", pin="screen-v10", folded=folded)
    a = run_build(store, pa_)
    assert a.status == "ok", a.data.get("error")
    calls, fails = fake.count(), fake.count(contains="DOC_FAILS")
    wf = load_stage_module("annotate.screen").WORKFLOW
    pin = {"name": "screen-v10", "model": LUNA}
    digests = annotate_stage.resolve_pin(wf, pin, annotate_stage.stack_config).template_digests
    data = yaml.safe_load((cwd / "configs" / "plans" / "s1-acc.yaml").read_text())
    data["prompts"]["annotate.screen"] = {**pin, "template_digest": digests[0]}
    cfg_a = a.files and rows(store, a.files["screen"])[sid(0)]["config_digest"]
    pp = dict(data["prompts"]["annotate.screen"])
    cfg_b = annotate_stage.config_digest(wf.spec, pp, annotate_stage.resolve_pin(wf, pp, annotate_stage.stack_config))
    assert cfg_b != cfg_a
    line = next(s for s in data["stages"] if s["stage"] == "annotate.screen")
    line.update(target=cfg_b, accepted=[{"config": cfg_a, "name": "screen-v10", "decision": "D1"}])
    (cwd / "configs" / "plans" / "s1-acc.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    pb = load_plan(cwd / "configs" / "plans" / "s1-acc.yaml")
    assert pb.plan_digest != pa_.plan_digest
    b = run_build(store, pb)
    assert b.status == "ok", b.data.get("error")
    # DOC_FAILS alone is asked again (a failure is never accepted), and fails again after the client's retries
    assert fake.count() - calls == fails and fake.count(contains="DOC_FAILS") == 2 * fails
    scr_a, scr_b = rows(store, a.files["screen"]), rows(store, b.files["screen"])
    for rec in ("stack_relevance", "sr_relevance"):
        ra_, rb_ = rows(store, a.files[rec]), rows(store, b.files[rec])
        for d in (sid(0), sid(1), sid(3)):
            assert rb_[d]["evidence_id"] == scr_b[d]["evidence_id"] == scr_a[d]["evidence_id"]
            assert rb_[d]["config_digest"] == cfg_a and rb_[d] == ra_[d], (rec, d)
    assert scr_b[sid(2)]["config_digest"] == cfg_b and scr_b[sid(2)]["status"] == "failed_transient"
    assert b.data["verify"]["status"] == "ok" and red(store) == {}
