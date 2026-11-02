"""Rule-only triage (T2.9; §3.1 gate table, §3.3; D19, D32, D37): the dev plan's ``sql:`` gate
(arms 1 and 2), a ``question:`` gate with no asker (arm 6, ``stack/gate.py`` argues why), the
asker seam with a fake asker (arms 1-5, ``not_attempted`` → ``scope_complete = false``), a
``gate:`` edit re-selected with ``--only select`` and zero asker calls (X14 green on the new head,
red on the stale one), a fields change resetting a row to ``identified`` and re-triage restoring
it (X19, X20), masked and retired rows untouched, and ``stack run triage`` from the CLI.

Every case runs the real stages through ``stack.stage_runner.run_stage`` over a store gathered
from synthetic ``sources`` rows (the ``Store`` helper of ``tests/test_gather_identity.py``, rows
from ``tests/synth/rows.py``, D46); the checks run through ``stack.checks.verify.run_checks``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from stack import gate
from stack.checks.verify import run_checks
from stack.plan import Plan, load_plan
from stack.runs import RunRecord
from stack.stage_runner import StageContext
from stack.store.chain import head_of, resolve
from stack.store.parquet import read_family
from tests.synth.records import run_line, sdir
from tests.synth.rows import AT, blob, dg, document_row, sid
from tests.test_gather_identity import DEV_PLAN, Store, mask, merge, src

REPO = Path(__file__).resolve().parents[1]
TRIAGE_CHECKS = ["V-annotations", "gate_obeyed", "gate_union", "state_monotone", "triage_current",
                 "block_ownership", "created_preserved", "state_blocks", "digest_agreements"]

Row = dict[str, Any]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


def paper(i: int, *, abstract: str | None = "An abstract", inband: bool = False, **over: Any) -> Row:
    """An arXiv source ``sid(i)`` (channel group ``paper``) with or without abstract / in-band text."""
    row = src(i, [("arxiv", f"2610.{i:05d}")], **over)
    row["metadata"]["abstract"] = abstract
    if inband:
        row["inband"]["entries"] = [{"source_id": sid(i), "content_role": "post", "raw": None,
                                     "text": blob(dg(("text", i)), 1000, "text/markdown"),
                                     "captured_at": AT}]
    return row


def blog(i: int) -> Row:
    """A blog post ``sid(i)`` (channel group ``url``) with no abstract and no in-band text."""
    row = src(i, [("url", f"https://openai.com/blog/{i}")], channel="openai_blog")
    row["metadata"]["abstract"] = None
    return row


def triage(store: Store, plan: Plan | None = None, only: list[str] | None = None) -> RunRecord:
    rec = run_line(store.root, plan or store.plan, "triage", only=only)
    assert rec is not None
    assert rec.status == "ok", rec.error
    return rec


def fid(store: Store) -> str:
    """The ``file_id`` of the ``documents`` head: what a build record's ``files`` entry names."""
    return resolve(store.root, sdir("documents")).files[-1].file_id


def ann(doc: Row) -> Row:
    t = doc["annotations"]["triage"]
    assert t is not None
    return t


def checks(root: Path, build: dict[str, Any] | None = None) -> dict[str, tuple[str, list[str]]]:
    res = run_checks(root, names=TRIAGE_CHECKS, build=build)
    return {r.check: (r.status, r.items) for r in res}


def assert_green(root: Path, build: dict[str, Any] | None = None) -> None:
    bad = {k: v for k, v in checks(root, build).items() if v[0] not in ("ok", "skipped")}
    assert not bad, bad


def plan_with(tmp: Path, name: str, gate_: dict[str, Any]) -> tuple[Plan, Path]:
    """The dev plan with ``gate_`` as its ``gate:``, written to ``tmp/<name>.yaml``."""
    data = yaml.safe_load(DEV_PLAN.read_text())
    data["gate"] = gate_
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_plan(path), path


def output(store: Store, rec: RunRecord, family: str) -> tuple[list[Row], dict[str, str]]:
    path = Path(rec.outputs[family]["path"])
    tbl, h = read_family(path if path.is_absolute() else store.root / path)
    return tbl.to_pylist(), h


# ---------------------------------------------------------------- arms 1 and 2, the dev gate
@pytest.mark.slow
def test_dev_gate_fetches_abstract_inband_and_url_rows_and_skips_the_rest(store: Store) -> None:
    store.add("sources.arxiv_sweep", [paper(1), paper(2, abstract=None, inband=True),
                                      paper(3, abstract=None)])
    store.add("sources.blogs", [blog(4)])
    store.gather()
    rec = triage(store)
    docs = store.docs()
    want = {sid(1): "fetch", sid(2): "fetch", sid(3): "skip", sid(4): "fetch"}
    assert {d: ann(r)["payload"]["decision"] for d, r in docs.items()} == want
    for d, r in docs.items():
        a = ann(r)
        assert r["state"] == "triaged"
        assert (a["status"], a["error_class"], a["origin"], a["basis_kind"]) == ("ok", None, "rule", "fields")
        assert a["basis_digest"] == r["metadata"]["fields_digest"]  # S9
        assert a["config_digest"] == rec.config_digest
        assert a["payload"]["wanted_by"] == (["screen"] if want[d] == "fetch" else [])
        assert a["payload"]["questions"] == []
    assert rec.counts["arm1"] == 3 and rec.counts["arm2"] == 1
    rows, h = output(store, rec, "documents")
    assert (h["kind"], h["blocks_written"], len(rows)) == ("part", '["annotations"]', 4)
    ev, eh = output(store, rec, "triage_evidence")
    assert ev == [] and (eh["kind"], eh["base"]) == ("full", "")
    assert_green(store.root, {"plan": str(DEV_PLAN), "files": {"documents": {"file_id": fid(store)}}})


@pytest.mark.slow
def test_rerun_on_a_triaged_head_touches_nothing(store: Store) -> None:
    """A rerun with nothing in scope writes no ``documents`` part (S25-52: an empty part only
    counted toward ``max_partials``), still its ``triage_evidence`` file, and the checks stay
    green; without ``--part`` the run still writes the full the (empty) part makes."""
    from stack.stage_runner import run_stage

    store.add("sources.arxiv_sweep", [paper(1), paper(2, abstract=None)])
    store.gather()
    triage(store)
    n_files = len(resolve(store.root, sdir("documents")).files)
    rec = triage(store)
    assert rec.counts["scoped"] == 0 and rec.counts["out_of_scope"] == 2
    assert "documents" not in rec.outputs and "triage_evidence" in rec.outputs, rec.outputs
    assert len(resolve(store.root, sdir("documents")).files) == n_files
    assert_green(store.root, {"plan": str(DEV_PLAN), "files": {"documents": {"file_id": fid(store)}}})
    full = run_stage(store.root, store.plan, "triage", [sdir("documents"), sdir("triage_evidence")])
    assert full.status == "ok", full.error
    rows, h = output(store, full, "documents")
    assert (h["kind"], len(rows)) == ("full", 2)


def test_compaction_due_counts_only_parts_with_rows(tmp_path: Path) -> None:
    """S25-52: zero-row parts (older triage runs wrote one per run) do not count toward
    ``max_partials``; a part with rows does."""
    import datetime as dt

    from stack.build import compaction_due
    from stack.store.chain import HeaderFields
    from tests.synth.records import record_files, write_series
    from tests.synth.rows import rid, table
    from tests.test_stage_runner import seed_documents

    root = tmp_path / "store"
    seed_documents(root)
    plan = load_plan(DEV_PLAN)
    plan = plan.model_copy(update={"versions": plan.versions.model_copy(update={
        "compaction": plan.versions.compaction.model_copy(update={"max_partials": 1})})})

    def part(run: int, rows: list[Row]) -> None:
        at = AT + dt.timedelta(minutes=10 + run)
        fields = HeaderFields(stage="triage", run_id=rid(run), config_digest=dg("c"), code_digest=dg("code"),
                              plan="dev-v2", blocks_written=["annotations"])
        pub = write_series(root, "documents", "documents", table("documents", rows), fields, kind="part",
                           at=at, finished_at=at)
        record_files(root, [pub.path], at=at)

    part(20, [])
    part(21, [])
    assert compaction_due(plan, resolve(root, sdir("documents"))) is None
    part(22, [document_row(0, 22)])
    assert compaction_due(plan, resolve(root, sdir("documents"))) is None
    part(23, [document_row(1, 23)])
    assert compaction_due(plan, resolve(root, sdir("documents"))) == "2 part files > max_partials 1"


# ---------------------------------------------------------------- questions, no asker (D37)
def test_question_gate_without_asker_lands_in_arm_6(store: Store, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    # Since T4.2 (D157) `asker_for` builds the LLM asker when the gate has questions; a hook
    # returning None keeps this M2 case (no asker) as it was.
    monkeypatch.setattr(gate, "ASKER_HOOK", lambda plan, line: None)
    plan, _ = plan_with(tmp_path, "q", {"screen": [
        {"sql": "metadata.abstract IS NOT NULL"},
        {"question": "safety_relevance", "prompt": "triage.safety-relevance-v1", "fetch_on": ["relevant"]}]})
    store.add("sources.arxiv_sweep", [paper(1), paper(2, abstract=None)])
    store.gather()
    rec = triage(store, plan)
    docs = store.docs()
    a1, a2 = ann(docs[sid(1)]), ann(docs[sid(2)])
    assert (a1["payload"]["decision"], a1["status"], a1["origin"]) == ("fetch", "ok", "rule")
    assert (a2["payload"]["decision"], a2["status"], a2["error_class"]) == ("pending", "skipped", "not_needed")
    for a in (a1, a2):
        assert [(q["question"], q["status"], q["error_class"]) for q in a["payload"]["questions"]] == [
            ("safety_relevance", "skipped", "not_requested")]
    assert docs[sid(2)]["state"] == "triaged"  # decided on current fields; pending is no fetch permission
    assert (rec.counts["arm1"], rec.counts["arm6"], rec.counts["arm3"]) == (1, 1, 0)


# ---------------------------------------------------------------- the asker seam
class FakeAsker:
    """Answers by doc: 1 relevant, 2 irrelevant, 3 failed_permanent, 4 no_basis, 5 left out."""

    name = "fake:v1"
    origin = "ai_check"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def ask(self, rows: list[Row], questions: list[gate.Question], ctx: StageContext) -> gate.AskResult:
        assert all("channel_group" in r and "inband_text_sha256" in r for r in rows)
        self.calls.append(sorted(r["doc_id"] for r in rows))
        res = gate.AskResult()
        for r in rows:
            for q in questions:
                a = gate.question_row(q, "ok")
                n = int(r["identity"]["members"][0]["key_value"].split(".")[1])
                if n == 1:
                    a.update(verdict="relevant", confidence=0.9)
                elif n == 2:
                    a.update(verdict="irrelevant", confidence=0.8)
                elif n == 3:
                    a.update(status="failed_permanent", error_class="parse_error")
                elif n == 4:
                    a.update(status="skipped", error_class="no_basis")
                else:
                    continue
                res.answers[(r["doc_id"], q.name)] = a
        return res


@pytest.fixture
def asker(monkeypatch: pytest.MonkeyPatch) -> FakeAsker:
    fake = FakeAsker()
    monkeypatch.setattr(gate, "ASKER_HOOK", lambda plan, line: fake)
    return fake


@pytest.mark.slow
def test_asker_seam_arms_and_gate_edit_reselects_with_zero_calls(
        store: Store, tmp_path: Path, asker: FakeAsker) -> None:
    q = {"question": "safety_relevance", "prompt": "triage.safety-relevance-v1", "fetch_on": ["relevant"]}
    plan_a, path_a = plan_with(tmp_path, "a", {"screen": [q]})
    store.add("sources.arxiv_sweep", [paper(i) for i in range(1, 6)])
    store.gather()
    rec = triage(store, plan_a)
    assert len(asker.calls) == 1 and len(asker.calls[0]) == 5
    assert rec.scope_complete is False and rec.counts["not_attempted"] == 1
    got = {d: (ann(r)["payload"]["decision"], ann(r)["status"], ann(r)["error_class"], ann(r)["origin"])
           for d, r in store.docs().items()}
    assert got == {sid(1): ("fetch", "ok", None, "ai_check"), sid(2): ("skip", "ok", None, "ai_check"),
                   sid(3): ("pending", "skipped", "gate_failed", "rule"),
                   sid(4): ("pending", "skipped", "no_basis", "rule"),
                   sid(5): ("pending", "not_attempted", "limit", "rule")}
    stale = fid(store)
    assert_green(store.root, {"plan": str(path_a), "files": {"documents": {"file_id": stale}}})

    # A gate: edit: a new consumer with a sql: clause and a wider fetch_on; --only select.
    plan_b, path_b = plan_with(tmp_path, "b", {
        "screen": [{**q, "fetch_on": ["relevant", "irrelevant"]}],
        "papers": [{"sql": "metadata.title LIKE 'Paper 3%'"}]})
    rec_b = triage(store, plan_b, only=["select"])
    assert len(asker.calls) == 1  # zero calls
    assert rec_b.substages["ask"]["ran"] is False and rec_b.counts["scoped"] == 5
    docs = store.docs()
    assert ann(docs[sid(2)])["payload"]["decision"] == "fetch"
    assert ann(docs[sid(3)])["payload"]["wanted_by"] == ["papers"]
    assert ann(docs[sid(5)])["status"] == "not_attempted"  # carried: still due for an ask
    fresh = fid(store)
    assert_green(store.root, {"plan": str(path_b), "files": {"documents": {"file_id": fresh}}})
    x14 = checks(store.root, {"plan": str(path_b), "files": {"documents": {"file_id": stale}}})["gate_union"]
    assert x14[0] == "failed" and any(sid(3) in i for i in x14[1]), x14
    # The gate a build records by gate_digest is found in the store (the triage run put it in
    # the derived blobs), whatever the plan file now says; an unknown gate is skipped, not passed.
    by_digest = {"plan": "no-such-plan", "gate_digest": plan_b.gate_digest}
    assert checks(store.root, {**by_digest, "files": {"documents": {"file_id": fresh}}})["gate_union"][0] == "ok"
    assert checks(store.root, {**by_digest, "files": {"documents": {"file_id": stale}}})["gate_union"][0] == "failed"
    unknown = {"plan": str(path_b), "gate_digest": dg("another gate"), "files": {"documents": {"file_id": fresh}}}
    res = {r.check: r for r in run_checks(store.root, names=["gate_union"], build=unknown)}["gate_union"]
    assert (res.status, res.error_class) == ("skipped", "no_gate")


# ---------------------------------------------------------------- fields change, X19, X20
@pytest.mark.slow
def test_fields_change_resets_to_identified_and_retriage_restores(store: Store) -> None:
    store.add("sources.arxiv_sweep", [paper(1), paper(2)])
    store.gather()
    triage(store)
    store.add("sources.arxiv_sweep", [paper(1, abstract="A new abstract", t=30)])
    store.gather()
    docs = store.docs()
    assert docs[sid(1)]["state"] == "identified" and docs[sid(2)]["state"] == "triaged"
    assert ann(docs[sid(1)])["basis_digest"] != docs[sid(1)]["metadata"]["fields_digest"]  # carried, stale
    assert_green(store.root)
    rec = triage(store)
    rows, _ = output(store, rec, "documents")
    assert [r["doc_id"] for r in rows] == [sid(1)]  # untouched rows are not in the part
    d1 = store.docs()[sid(1)]
    assert d1["state"] == "triaged" and ann(d1)["basis_digest"] == d1["metadata"]["fields_digest"]
    assert_green(store.root)


@pytest.mark.slow
def test_masked_and_retired_rows_are_untouched(store: Store) -> None:
    store.add("sources.arxiv_sweep", [paper(1), paper(2), paper(3), paper(4)])
    store.gather()
    store.overrides([merge("o1", sid(1), sid(2)), mask("o2", sid(3))])
    store.gather()
    docs = store.docs()
    assert docs[sid(2)]["state"] == "retired" and docs[sid(3)]["identity"]["is_masked"]
    rec = triage(store)
    rows, _ = output(store, rec, "documents")
    assert sorted(r["doc_id"] for r in rows) == [sid(1), sid(4)]
    docs = store.docs()
    assert docs[sid(2)]["annotations"]["triage"] is None and docs[sid(3)]["state"] == "identified"
    assert_green(store.root)


def test_scope_table_over_synthetic_rows() -> None:
    """§3.1's scope on factory rows: S9 and gather's reset make a stale basis on a ``triaged``
    row unreachable in a valid store, so the rule is pinned here on the rows themselves."""
    cfg = dg("triage-config")

    def row(state: str = "triaged", **edit: Any) -> Row:
        r = document_row(1, state=state)
        if r["annotations"]["triage"] is not None:
            r["annotations"]["triage"]["config_digest"] = cfg
        for path, v in edit.items():
            *head, last = path.split("__")
            tgt = r
            for k in head:
                tgt = tgt[k]
            tgt[last] = v
        return r

    table_ = {
        "identified": (row("identified"), True),
        "triaged, current": (row(), False),
        "triaged, stale basis": (row(annotations__triage__basis_digest=dg("old fields")), True),
        "triaged, stale config": (row(annotations__triage__config_digest=dg("old gate")), True),
        "acquired, current": (row("acquired"), False),
        "masked identified": (row("identified", identity__is_masked=True), False),
        "retired": (row("retired"), False),
    }
    assert {k: gate.in_scope(r, cfg) for k, (r, _) in table_.items()} == {k: v for k, (_, v) in table_.items()}


# ---------------------------------------------------------------- the CLI
@pytest.mark.slow
def test_stack_run_triage_cli_writes_part_and_empty_evidence(store: Store) -> None:
    store.add("sources.arxiv_sweep", [paper(1), paper(2, abstract=None)])
    store.gather()
    base = head_of(store.root, "data/documents/main")
    proc = subprocess.run(
        ["uv", "run", "--no-sync", "stack", "run", "triage", "--in", sdir("documents"),
         "--series", "main", "--part", "--store", str(store.root), "--plan", str(DEV_PLAN)],
        check=False, cwd=REPO, env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    files = resolve(store.root, sdir("documents")).files
    tbl, h = read_family(files[-1].path)
    assert (h["stage"], h["kind"], h["base"], tbl.num_rows) == ("triage", "part", base, 2)
    ev = sorted((store.root / sdir("triage_evidence")).glob("*.parquet"))
    assert len(ev) == 1 and read_family(ev[0])[0].num_rows == 0
    assert {r["doc_id"]: ann(r)["payload"]["decision"] for r in tbl.to_pylist()} == {
        sid(1): "fetch", sid(2): "skip"}
