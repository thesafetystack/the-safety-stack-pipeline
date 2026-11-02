"""M5 scenario, part 1 (PLAN T5.1a): ``stack build`` over the synthetic world, v4e steps 1 and 10
in v4f terms, as far as the build machinery goes.

The real ``collect.*``, ``gather``, ``triage``, ``acquire`` and ``annotate.*`` stages belong to
tracks A, B and C and are changing; the builds here run the stand-ins of ``tests/synth/stages/``
(selected by ``STACK_WORKFLOWS_ROOT``) under the plan ``tests/synth/plans/synth-m5.yaml``, on top
of ``tests/synth/world.py``'s store. ``fold.*`` stages are the real fold adapter. Assertions go
through the build record, the run records, ``run_checks`` (inside the build) and the CLI
(``stack build``, ``stack head``, ``stack release``, ``stack chain orphan``; typer's
``CliRunner``, no subprocess). There is no prior build (D373): every build continues
from the heads of the ``main`` series directories, and its ``files`` list its own outputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
import zstandard
from typer.testing import CliRunner

from stack.build import HALTED_REPEAT_MIN_OK, BuildRecord, run_build
from stack.cli import app
from stack.plan import Plan
from stack.runs import (
    RunRecord,
    newest_ok_build,
    read_build_record,
    read_run_records,
    write_final_record,
)
from stack.stage_runner import WORKFLOWS_ROOT_ENV
from stack.store.chain import resolve
from stack.store.layout import BLOBS, DATA, RELEASES, parse_data_key
from stack.store.parquet import read_family, read_header
from tests.synth.records import sdir
from tests.synth.rows import AT
from tests.synth.world import build_world

#: scenario world: skipped while iterating (`-m "not slow"`), run at milestone gates (D90; `docs/SETUP.md` §7).
pytestmark = [pytest.mark.slow, pytest.mark.e2e]

REPO = Path(__file__).resolve().parents[1]
STAND_INS = REPO / "tests" / "synth" / "stages"
PLAN_PATH = REPO / "tests" / "synth" / "plans" / "synth-m5.yaml"
runner = CliRunner()


def plan_with(**stage_options: dict[str, Any]) -> Plan:
    """The synthetic plan with ``options`` merged into the named stage lines; a key not in the
    plan appends a bare stage line before ``fold.build``."""
    data = yaml.safe_load(PLAN_PATH.read_text("utf-8"))
    lines = {s["stage"]: s for s in data["stages"]}
    for stage, opts in stage_options.items():
        if stage not in lines:
            data["stages"].insert(len(data["stages"]) - 1, {"stage": stage})
            continue
        for key in ("repeat", "require_complete"):  # line fields, not options
            if key in opts:
                lines[stage][key] = opts.pop(key)
        lines[stage]["options"] = {**lines[stage].get("options", {}), **opts}
    return Plan.model_validate(data)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    root = tmp_path / "store"
    build_world(root)
    return root


def cli(store: Path, *args: str) -> Any:
    return runner.invoke(app, ["--store", str(store), "--plan", str(PLAN_PATH), *args])


def build(store: Path, plan: Plan | None = None) -> BuildRecord:
    return run_build(store, plan or plan_with())


def runs_of(store: Path, b: BuildRecord) -> list[Any]:
    recs = {r.run_id: r for r in read_run_records(store)}
    return [recs[r] for r in b.data["runs"]]


def assert_head_pins(store: Path, b: BuildRecord, start_head: str) -> None:
    """Per-run head pins: each run reading ``documents`` pinned the head current at its start,
    i.e. the previous ``documents`` writer's output in this build (or the build's start head)."""
    head = start_head
    for rec in runs_of(store, b):
        if "documents" in rec.inputs:
            assert rec.inputs["documents"] == head, (rec.stage, rec.run_id)
        if "documents" in rec.outputs:
            head = rec.outputs["documents"]["file_id"]
    assert b.files["documents"]["file_id"] == head


def ok_line(b: BuildRecord) -> None:
    assert b.status == "ok", b.data.get("error")
    assert b.data["verify"]["status"] == "ok"
    ran = [c for c in b.data["verify"]["checks"] if c["error_class"] != "not_implemented"]
    # a check may skip for its own reason (X14 `no_gate` on the stand-ins' rows, track A); none
    # fails or warns, and T5.1b's nine are asserted ok by name (`nine_ok`)
    assert ran and all(c["status"] in ("ok", "skipped") for c in ran), [c for c in ran if c["status"] != "ok"]


# ---------------------------------------------------------------- v4e step 1
def test_fresh_build_ok_head_release(store: Path) -> None:
    start = resolve(store, sdir("documents")).head
    res = cli(store, "build", str(PLAN_PATH), "--json")
    assert res.exit_code == 0, res.output
    rec = json.loads(res.stdout)
    hits = sorted((store / "builds").glob(f"*_{rec['build_id']}.synth-m5/build.json.zst"))  # D344
    assert len(hits) == 1 and read_build_record(hits[0]) == rec
    b = BuildRecord(hits[0], rec)
    ok_line(b)
    assert rec["build_id"].startswith("w") and "prior_build_id" not in rec and rec["series"] == "main"
    assert rec["inputs_pinned_at"] == rec["started_at"] and "compacted_from" not in rec
    # version_chain: the walk of the build's documents file (newest full file to it)
    assert rec["version_chain"] == list(resolve(store, rec["files"]["documents"]["path"]).version_chain)
    # files: this build's own outputs: the three corpus keys, documents, the collector's sources
    # chain, this build's <wf> files (the stand-in collect.arxiv writes no coverage file)
    for k in ("corpus", "corpus_input", "corpus_everything", "documents", "screen", "screen_evidence",
              "triage_evidence", "sources/sources.arxiv"):
        assert {"file_id", "path", "file_digest", "run_id"} <= set(rec["files"][k]), k
    assert "coverage/coverage.arxiv" not in rec["files"]
    # each corpus key into its own family directory of the series (D374)
    assert {k: rec["files"][k]["path"].rsplit("/", 1)[0] for k in ("corpus", "corpus_input", "corpus_everything")} == {
        "corpus": "data/corpus.build/main", "corpus_input": "data/corpus/main",
        "corpus_everything": "data/corpus.everything/main"}
    corpus, corpus_in, everything = (read_header(store / rec["files"][k]["path"])
                                     for k in ("corpus", "corpus_input", "corpus_everything"))
    assert (corpus["fold_config"], corpus_in["fold_config"], everything["fold_config"]) == ("build", "corpus", "corpus")
    for k, h in (("corpus", corpus), ("corpus_input", corpus_in), ("corpus_everything", everything)):
        assert rec["files"][k]["schema_digest"] == h["schema_digest"], k  # R1-2
    assert json.loads(corpus["inputs"]) == {"corpus": rec["files"]["corpus_input"]["file_id"],
                                            "screen": rec["files"]["screen"]["file_id"]}
    assert rec["files"]["corpus"]["fold"]["joined"] == {"screen": rec["files"]["screen"]["file_id"]}
    assert int(everything["row_count"]) == 15 and int(corpus_in["row_count"]) == 7  # everything vs has_text
    # every run is recorded ok, in execution order; acquire repeated until scope_complete
    recs = runs_of(store, b)
    assert [r.status for r in recs] == ["ok"] * len(recs)
    assert [r.stage for r in recs] == ["collect.arxiv", "gather", "triage", "acquire", "acquire", "fold.corpus",
                                       "fold.corpus", "annotate.screen", "fold.build"]
    assert [r.scope_complete for r in recs if r.stage == "acquire"] == [False, True]
    assert [r.filter for r in recs if r.stage == "fold.corpus"][1] == "everything"
    assert_head_pins(store, b, start)
    # stack head names the build's corpus as the last file of corpus.build/main/ (stack latest is
    # gone, D375); stack release --in that directory releases it
    res = cli(store, "head", sdir("corpus.build"), "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["path"] == rec["files"]["corpus"]["path"]
    res = cli(store, "head", sdir("corpus.build"))
    assert f"head\t{rec['files']['corpus']['path']}" in res.stdout.splitlines()
    res = cli(store, "release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "t1")
    assert res.exit_code == 0, res.output
    rel = json.loads((store / RELEASES / "t1" / "release.json").read_text("utf-8"))
    assert rel["build_id"] == rec["build_id"] and rel["corpus_file_id"] == rec["files"]["corpus"]["file_id"]


# ---------------------------------------------------------------- require_complete (round 5: incomplete)
def test_acquire_incomplete_build_runs_on_and_the_next_build_continues_from_it(store: Path) -> None:
    """R6-4: acquire ends ``scope_complete = false`` after its three runs; the build runs every later
    line and verify, and ends ``incomplete`` (not ``failed``). ``newest_ok_build`` keeps naming build
    1; ``stack head`` names build 2's corpus (the directory's last file, whatever the build's
    status); releasing it needs ``--allow-incomplete``; build 1's corpus is released only by its
    exact path; the next build continues from the series heads build 2 left. (gc's keeping build
    2's files went with ``stack gc``; the prior build went with D373.)"""
    b1 = build(store)
    ok_line(b1)
    b2 = build(store, plan_with(acquire={"complete_after": 99, "repeat": {"max_runs": 3}}))
    assert (b2.status, b2.error_class) == ("incomplete", None), b2.data.get("error")
    stages = [r.stage for r in runs_of(store, b2)]
    assert stages.count("acquire") == 3
    assert {"fold.corpus", "annotate.screen", "fold.build"} <= set(stages)  # the later lines ran
    acq = [r for r in runs_of(store, b2) if r.stage == "acquire"][-1]
    assert b2.data["incomplete"] == [{"stage": "acquire", "run_id": acq.run_id, "reasons": ["unspecified"]}]
    assert b2.data["verify"]["status"] == "ok"
    exp = next(c for c in b2.data["verify"]["checks"] if c["name"] == "expected_scope")
    assert exp["status"] == "ok", exp
    assert any("acquire run" in w and "ends incomplete" in w for w in b2.data["warnings"])
    assert read_build_record(b2.path)["status"] == "incomplete"
    assert newest_ok_build(store, "synth-m5")[1]["build_id"] == b1.build_id
    # removed with newest_prior_build, `build --dry-run`'s prior_build_id and `stack latest [--any]`
    # (D373, D375): the series directory's head is build 2's corpus
    res = cli(store, "head", sdir("corpus.build"))
    assert f"head\t{b2.files['corpus']['path']}" in res.stdout.splitlines()
    # release: refused without the flag, allowed with it; build 1's corpus only by its exact path
    res = cli(store, "release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "inc")
    assert res.exit_code == 1 and f"is the corpus of build {b2.build_id!r}, which is incomplete" in res.output
    assert "--allow-incomplete" in res.output and not (store / RELEASES / "inc").exists()
    corpus2 = b2.files["corpus"]["file_id"]
    res = cli(store, "fold", "release", "--input", f"corpus={corpus2}", "--release", "public-v1", "--tag", "inc0")
    assert res.exit_code != 0 and not (store / RELEASES / "inc0").exists()
    res = cli(store, "release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "inc",
              "--allow-incomplete")
    assert res.exit_code == 0, res.output
    assert json.loads((store / RELEASES / "inc" / "release.json").read_text("utf-8"))["build_id"] == b2.build_id
    res = cli(store, "release", "--in", b1.files["corpus"]["path"], "--config", "public-v1", "--tag", "lat")
    assert res.exit_code == 0, res.output
    assert "NOT its directory's last file" in res.output
    assert json.loads((store / RELEASES / "lat" / "release.json").read_text("utf-8"))["build_id"] == b1.build_id
    # the next build continues from the heads build 2 left: its screen files, and acquire completes
    b3 = build(store)
    ok_line(b3)
    by_stage = {r.stage: r for r in runs_of(store, b3)}
    assert by_stage["annotate.screen"].prior == [b2.files["screen"]["file_id"], b2.files["screen_evidence"]["file_id"]]
    assert newest_ok_build(store, "synth-m5")[1]["build_id"] == b3.build_id


def test_a_halted_acquire_run_ends_the_repeat_unless_it_made_progress(store: Path) -> None:
    """A run that ended ``halted`` (a lane-wide provider halt) with fewer than ``HALTED_REPEAT_MIN_OK``
    roles ok ends the line's ``repeat``: run 2 of 6 halts with 0 ok, no run 3; the build runs every
    later line and ends ``incomplete`` (acquire, ``halted``) with a note. A halted run that still
    completed ``HALTED_REPEAT_MIN_OK`` roles is repeated as before (build wtwv0wpnr8hfv ran 4 halted runs of
    0-3 ok roles after the halt; ``stack.build.HALTED_REPEAT_MIN_OK``)."""
    b1 = build(store)
    ok_line(b1)
    b2 = build(store, plan_with(acquire={"complete_after": 99, "halt_from": 2, "repeat": {"max_runs": 6}}))
    assert (b2.status, b2.error_class) == ("incomplete", None), b2.data.get("error")
    acq = [r for r in runs_of(store, b2) if r.stage == "acquire"]
    assert len(acq) == 2 and "halted" not in acq[0].counts and acq[1].counts["halted.oa"] == 1
    assert b2.data["incomplete"] == [{"stage": "acquire", "run_id": acq[-1].run_id, "reasons": ["halted"]}]
    assert any(f"acquire run {acq[-1].run_id} halted (oa) with 0 item(s) ok (< {HALTED_REPEAT_MIN_OK}): not "
               "repeated" in w for w in b2.data["warnings"]), b2.data["warnings"]
    assert {"fold.corpus", "annotate.screen", "fold.build"} <= {r.stage for r in runs_of(store, b2)}
    # progress: a halted run with HALTED_REPEAT_MIN_OK roles ok is repeated up to max_runs
    b3 = build(store, plan_with(acquire={"complete_after": 99, "halt_from": 1, "halted_ok": HALTED_REPEAT_MIN_OK,
                                         "repeat": {"max_runs": 3}}))
    acq3 = [r for r in runs_of(store, b3) if r.stage == "acquire"]
    assert len(acq3) == 3 and all(r.counts["halted"] == 1 for r in acq3)
    assert not any("not repeated" in w for w in b3.data["warnings"])


def test_a_failed_run_still_fails_the_build_at_once(store: Path) -> None:
    """A run that ends ``failed`` fails the build at once (the later lines do not run), whatever
    stopped short before it; the record still lists the stage that did; it is never the newest ok
    build."""
    b1 = build(store)
    ok_line(b1)
    data = yaml.safe_load(PLAN_PATH.read_text("utf-8"))
    lines = {s["stage"]: s for s in data["stages"]}
    lines["annotate.screen"]["options"] = {"max_rows": 0}  # decides nothing: scope incomplete
    lines["acquire"]["options"] = {"fail": True}
    data["stages"] = [lines[s] for s in ("collect.arxiv", "gather", "triage", "fold.corpus", "annotate.screen",
                                         "acquire", "fold.build")]
    data["require_complete"] = [*data["require_complete"], "screen"]
    b2 = build(store, Plan.model_validate(data))
    assert (b2.status, b2.error_class) == ("failed", "code_bug"), b2.data.get("error")
    stages = [r.stage for r in runs_of(store, b2)]
    assert stages[-2:] == ["annotate.screen", "acquire"] and "fold.build" not in stages
    assert [e["stage"] for e in b2.data["incomplete"]] == ["annotate.screen"]
    # newest_prior_build is gone (D373)
    assert newest_ok_build(store, "synth-m5")[1]["build_id"] == b1.build_id


def test_a_line_not_required_complete_leaves_the_build_ok(store: Path) -> None:
    """R6 item 1: acquire ends short (``complete_after`` never reached) on a line with ``require_complete:
    false``: the build ends ``ok`` (the newest ok build), the shortfall is in ``incomplete_allowed``
    with a warning, X3 passes. The same line without the flag ends the build ``incomplete``. The flag
    changes the plan digest, not the line's digest (the runs' ``config_digest``)."""
    short = {"complete_after": 99, "repeat": {"max_runs": 2}}
    flagged = plan_with(acquire={**short, "require_complete": False})
    plain = plan_with(acquire=dict(short, repeat={"max_runs": 2}))
    assert flagged.plan_digest != plain.plan_digest
    assert flagged.stage_line("acquire").digest() == plain.stage_line("acquire").digest()
    assert "require_complete" not in plain.to_json()["stages"][3]
    b1 = build(store, flagged)
    ok_line(b1)
    acq = [r for r in runs_of(store, b1) if r.stage == "acquire"]
    assert len(acq) == 2 and not acq[-1].scope_complete
    assert "incomplete" not in b1.data
    assert b1.data["incomplete_allowed"] == [{"stage": "acquire", "run_id": acq[-1].run_id, "reasons": ["unspecified"]}]
    assert any("acquire run" in w and "require_complete: false" in w and "retries" in w for w in b1.data["warnings"])
    exp = next(c for c in b1.data["verify"]["checks"] if c["name"] == "expected_scope")
    assert exp["status"] == "ok", exp
    assert newest_ok_build(store, "synth-m5")[1]["build_id"] == b1.build_id
    b2 = build(store, plain)
    assert b2.status == "incomplete", b2.data.get("error")
    acq2 = [r for r in runs_of(store, b2) if r.stage == "acquire"][-1]
    assert b2.data["incomplete"] == [{"stage": "acquire", "run_id": acq2.run_id, "reasons": ["unspecified"]}]
    assert "incomplete_allowed" not in b2.data


def test_a_failed_run_of_a_line_not_required_complete_still_fails_the_build(store: Path) -> None:
    """``require_complete: false`` covers a short ``ok`` run only: a failed run fails the build."""
    b = build(store, plan_with(acquire={"fail": True, "require_complete": False}))
    assert (b.status, b.error_class) == ("failed", "code_bug"), b.data.get("error")
    assert "fold.build" not in [r.stage for r in runs_of(store, b)]


# ---------------------------------------------------------------- v4e step 10, compaction
def test_second_build_continues_from_heads_and_compacts(store: Path) -> None:
    b1 = build(store)
    ok_line(b1)
    b2 = build(store)
    ok_line(b2)
    # fold.compact at build start (D373): 3 parts after the full file > max_partials 2;
    # an ordinary run writing a full on the head into documents/main/
    head1 = b1.files["documents"]["file_id"]
    assert b2.data["compacted_from"] == b1.files["documents"]["path"]
    recs = runs_of(store, b2)
    assert recs[0].stage == "fold.compact"
    out = recs[0].outputs["documents"]
    compact = read_header(store / out["path"])
    assert (compact["kind"], compact["base"]) == ("full", Path(b1.files["documents"]["path"]).name)
    assert out["path"].startswith("data/documents/main/") and out["base"] == compact["base"]
    assert_head_pins(store, b2, head1)
    # priors: the series heads, i.e. build 1's files
    by_stage = {r.stage: r for r in recs}
    assert by_stage["annotate.screen"].prior == [b1.files["screen"]["file_id"], b1.files["screen_evidence"]["file_id"]]
    assert by_stage["annotate.screen"].inputs == {"corpus": b2.files["corpus_input"]["file_id"]}
    assert by_stage["triage"].prior == [b1.files["triage_evidence"]["file_id"]]
    assert by_stage["triage"].counts["prior_evidence"] == 1
    # the row build 1 left not_attempted is done in build 2 and reaches the corpus as ok
    s1 = read_family(store / b1.files["screen"]["path"])[0].to_pylist()
    left = [r["doc_id"] for r in s1 if r["status"] == "not_attempted"]
    assert len(left) == 1
    s2 = {r["doc_id"]: r for r in read_family(store / b2.files["screen"]["path"])[0].to_pylist()}
    assert s2[left[0]]["status"] == "ok" and s2[left[0]]["run_id"] == by_stage["annotate.screen"].run_id
    assert all(r["status"] == "ok" for r in s2.values())
    corpus = {r["doc_id"]: r for r in read_family(store / b2.files["corpus"]["path"])[0].to_pylist()}
    assert corpus[left[0]]["annotations"]["screen"]["status"] == "ok"
    assert "triage_evidence" not in b2.files  # nothing left to triage: no output this build


# ---------------------------------------------------------------- failures leave the latest ok build
def test_failing_stages(store: Path) -> None:
    b1 = build(store)
    ok_line(b1)
    # a stage raising mid-build: the run is failed[code_bug], the build too, with a record
    b2 = build(store, plan_with(acquire={"fail": True}))
    assert (b2.status, b2.error_class) == ("failed", "code_bug")
    assert "stage acquire run" in b2.data["error"] and "injected failure" in b2.data["error"]
    failed = runs_of(store, b2)[-1]
    assert (failed.stage, failed.status) == ("acquire", "failed")
    assert not [n for n in (store / DATA).rglob("*.parquet") if parse_data_key(n).run_id == failed.run_id]
    # a stage whose module does not exist: refused before dispatch
    b3 = build(store, plan_with(**{"annotate.missing": {}}))
    assert (b3.status, b3.error_class) == ("failed", "stage_refused")
    assert "annotate.missing" in b3.data["error"]
    assert newest_ok_build(store, "synth-m5")[1]["build_id"] == b1.build_id


def test_orphaned_tail_next_build_continues_from_the_file_before(store: Path) -> None:
    # The build-start X23 against the prior build is gone (D373, D375): an
    # orphaned tail is no longer a lost head; the next build continues from the directory's new last file.
    b1 = build(store)
    ok_line(b1)
    tail = b1.files["documents"]["path"]
    walk = resolve(store, sdir("documents")).paths
    before = walk[-2].relative_to(store).as_posix()
    res = cli(store, "chain", "orphan", tail)
    assert res.exit_code == 0, res.output
    assert "chain.orphan" in res.output or "excluded by" in res.output
    res = cli(store, "head", sdir("documents"))
    assert f"head\t{before}" in res.stdout.splitlines()
    b4 = build(store)
    ok_line(b4)
    gather = next(r for r in runs_of(store, b4) if r.stage == "gather")
    first = runs_of(store, b4)[0]
    pinned = first.inputs["documents"] if first.stage == "fold.compact" else gather.inputs["documents"]
    assert pinned == read_header(store / before)["file_id"]


# ================================================================ part 2 (T5.1b): cross-build checks
#: The checks T5.1b implements (Appendix B X2, X3, X5, X9, X11, X12, X18, X21, X25).
T51B = ("lineage_equality", "expected_scope", "blob_resolution", "fold_recompute", "dead_runs_seen",
        "state_propagation", "tombstone_closure", "carry_regrouped", "same_raw_reported")


def verify_json(store: Path, record: dict[str, Any] | None, tmp: Path, *flags: str) -> dict[str, dict[str, Any]]:
    """``stack verify --json [flags]`` against ``record`` (written outside ``runs/builds/``, so it is not
    a build of the store), as ``name -> check entry``."""
    args = ["verify", "--json", *flags]
    if record is not None:
        path = tmp / "probe.build.json.zst"
        path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(record).encode()))
        args += ["--build", str(path)]
    res = cli(store, *args)
    return {c["name"]: c for c in json.loads(res.stdout)["checks"]}


def nine_ok(b: BuildRecord) -> None:
    by = {c["name"]: c for c in b.data["verify"]["checks"]}
    assert {n: by[n]["status"] for n in T51B} == dict.fromkeys(T51B, "ok"), {n: by[n] for n in T51B}


# ---------------------------------------------------------------- v4e step 9: broken lineage
def test_x2_fold_build_on_another_corpus_or_a_newer_head(store: Path, tmp_path: Path) -> None:
    b1 = build(store)
    ok_line(b1)
    nine_ok(b1)
    assert verify_json(store, b1.data, tmp_path)["lineage_equality"]["status"] == "ok"
    # the record claims the diagnostic fold.corpus file as corpus_input: annotate.screen and
    # fold.build read another corpus file than the build's
    files = dict(b1.data["files"], corpus_input=b1.data["files"]["corpus_everything"])
    x2 = verify_json(store, dict(b1.data, files=files), tmp_path)["lineage_equality"]
    assert x2["status"] == "failed", x2
    want = b1.data["files"]["corpus_input"]["file_id"]
    assert any(f"fold.build read corpus {want}, not the build's corpus_input" in i for i in x2["items"]), x2
    assert any("annotate.screen read corpus" in i for i in x2["items"]), x2
    # the record pins an older documents head: runs of the build pinned newer heads (acquire's
    # outputs) that are not on its walk
    recs = runs_of(store, b1)
    gather_out = next(r for r in recs if r.stage == "gather").outputs["documents"]["file_id"]
    files = dict(b1.data["files"], documents=dict(b1.data["files"]["documents"], file_id=gather_out))
    x2 = verify_json(store, dict(b1.data, files=files), tmp_path)["lineage_equality"]
    assert x2["status"] == "failed"
    acq = [r.outputs["documents"]["file_id"] for r in recs if r.stage == "acquire"]
    assert any(f"pinned {acq[-1]}, not on the walk of the build's head {gather_out}" in i for i in x2["items"]), x2


def test_hard_check_fails_the_build_warn_does_not(store: Path) -> None:
    # X11 is a warning: two crashed runs sharing an inflight key leave the build ok
    for n in (90, 91):
        write_final_record(store, RunRecord(
            run_id=f"r0000000000{n}", stage="acquire", writer="acquire", plan="synth-m5", status="failed",
            error_class="crashed", started_at=AT, heartbeat_at=AT, finished_at=AT, code_digest="0" * 64,
            config_digest="0" * 64, inflight=["s1:full_text"]))
    b1 = build(store)
    assert b1.status == "ok", b1.data.get("error")
    x11 = next(c for c in b1.data["verify"]["checks"] if c["name"] == "dead_runs_seen")
    assert (x11["status"], x11["level"]) == ("warn", "warn") and "s1:full_text" in x11["items"][0]
    # X5: a text blob the documents view points at is gone → the next build fails verify
    rows = read_family(store / b1.files["corpus"]["path"])[0].to_pylist()
    digest = rows[0]["content"]["current_text_sha256"]
    (store / BLOBS / digest).unlink()
    b2 = build(store)
    assert (b2.status, b2.error_class) == ("failed", "verify"), b2.data.get("error")
    assert "blob_resolution" in b2.data["error"] and digest[:12] in b2.data["error"]
    assert newest_ok_build(store, "synth-m5")[1]["build_id"] == b1.build_id


# ---------------------------------------------------------------- v4e step 11: lost tail, clean rebuild
def test_lost_tail_part_file_fails_x23_clean_second_build_verifies(store: Path, tmp_path: Path) -> None:
    b1 = build(store)
    ok_line(b1)
    b2 = build(store)  # a clean second build: all nine run and are ok, not skipped
    ok_line(b2)
    nine_ok(b2)
    # the tail part file of documents (b2's head, an acquire part) is removed after the build
    head = b2.files["documents"]
    assert read_header(store / head["path"])["kind"] == "part"
    (store / head["path"]).unlink()
    # (plain `stack verify` would take the world's own dev-v2 build record as the reference: it
    # finishes later on the synthetic clock; the build's record is passed instead)
    # D345: the records still name the lost tail as the head, so the chain does not resolve (V11)
    # and X1 names the lost file; X23 leaves an unresolvable chain to V11
    got = verify_json(store, b2.data, tmp_path, "--full")  # V11 and X1 are structural: not in the default
    assert got["V-chain-linear"]["status"] == "failed" and Path(head["path"]).name in " ".join(got["V-chain-linear"]["items"])
    assert got["finished_files"]["status"] == "failed" and head["file_id"] in " ".join(got["finished_files"]["items"])
    b3 = build(store)
    assert (b3.status, b3.error_class, b3.data["runs"]) == ("failed", "chain_error", [])
    assert "does not resolve" in b3.data["error"] and Path(head["path"]).name in b3.data["error"]


# ---------------------------------------------------------------- D554: S2 off the build's critical path
def test_a_skipped_collector_published_by_a_standalone_run_during_the_build_leaves_it_ok(
        store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D554 (the full fetch's S2 collect runs standalone beside build 1, whose ``--skip`` leaves its line
    out): a ``--skip``ped collector line makes no run, so it is no ``incomplete`` entry under
    ``require_complete: [sources, coverage, documents]`` (only a warning), and a standalone run of that
    collector publishing its ``sources``/``coverage`` parts while the build runs, once before gather and
    once between the last stage and verify, causes no ``head_moved`` and no verify failure: the build
    writes neither directory, and ``claim`` checks only the destination's head."""
    import stack.build as sb
    from stack.build import BuildOverrides
    from tests.synth.rows import coverage_row
    from tests.synth.world import sources_row, write_on_head

    published: list[str] = []

    def publish(n: int) -> None:  # what `stack run collect.openalex` would leave: an ok record and its parts
        write_on_head(store, "sources.openalex", "sources", [sources_row("O2", 40 + n)],
                      stage="collect.openalex", run=40 + n)
        write_on_head(store, "coverage.openalex", "coverage", [coverage_row(5 + n, 40 + n, channel="openalex")],
                      stage="collect.openalex", run=50 + n)
        published.append(f"publish {n}")

    real_run_stage, real_checks = sb.run_stage, sb.run_checks

    def run_stage(root: Any, plan: Any, stage: str, *a: Any, **k: Any) -> Any:
        if stage == "gather" and not published:
            publish(1)
        return real_run_stage(root, plan, stage, *a, **k)

    def run_checks(*a: Any, **k: Any) -> Any:
        if "build" in k:  # the finish's verify (the start's structural one names no build)
            publish(2)
        return real_checks(*a, **k)

    monkeypatch.setattr(sb, "run_stage", run_stage)
    monkeypatch.setattr(sb, "run_checks", run_checks)
    plan = plan_with(**{"collect.openalex": {}})  # the line a build skips (no stand-in module: never loaded)
    b = run_build(store, plan, overrides=BuildOverrides(skip=("collect.openalex",)))
    assert published == ["publish 1", "publish 2"]
    ok_line(b)
    assert "incomplete" not in b.data and "incomplete_allowed" not in b.data
    assert "skipped by --skip: collect.openalex" in b.data["warnings"]
    assert all(r.status == "ok" and r.error_class is None for r in runs_of(store, b))
    assert "sources/sources.openalex" not in b.files and "coverage/coverage.openalex" not in b.files
