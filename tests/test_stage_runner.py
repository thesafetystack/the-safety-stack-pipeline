"""The stage runner (T1.3): topological order, and
``run_stage`` end to end over a synthetic ``documents`` series (``data/documents/main/``): the
``echo_test`` part file and its default full, a failing stage, a head-moved race, an exact-file
``--in`` that is not the last file, ``--only`` and the plan loader's refusals.

``seed_documents`` builds the series through ``write_series`` with an ``ok`` final record (as a
``gather`` run would leave it); the crash test (``tests/test_runner_crash.py``) reuses it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from stack import stage_runner
from stack.plan import PlanError, StageLine, load_plan
from stack.runs import (
    RunRecord,
    files_without_ok_record,
    find_run_record,
    read_run_records,
    run_record_path,
    running_records,
    write_final_record,
)
from stack.stage_runner import (
    StageError,
    plan_order,
    run_stage,
    topo_order,
)
from stack.store.chain import (
    HeaderFields,
    Published,
    head_of,
    last_file,
    read_view,
    resolve,
)
from stack.store.layout import DATA, parse_data_key
from tests.synth.records import sdir, write_series
from tests.synth.rows import AT, dg, document_row, rid, table

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
GATHER_BLOCKS = ["identity", "metadata", "inband"]
DOCS = sdir("documents")  # data/documents/main/


# ---------------------------------------------------------------- helpers
def seed_documents(store: Path, n: int = 12, run: int = 1, *, part_of: str | None = None) -> Published:
    """A ``documents`` full file of ``n`` rows starting ``data/documents/main/`` (or, with
    ``part_of``, a part on that directory's last file, named by its file id or name) published
    with an ``ok`` final record, as ``gather``/``triage`` would leave it."""
    states = ["identified", "triaged", "acquired"]
    rows = [document_row(i, run, state=states[i % 3]) for i in range(n)]
    stage = "gather" if part_of is None else "triage"
    fields = HeaderFields(stage=stage, run_id=rid(run), config_digest=dg(("config", stage)),
                          code_digest=dg("code"), plan="dev-v2",
                          blocks_written=GATHER_BLOCKS if part_of is None else ["annotations"])
    at = AT + dt.timedelta(minutes=run)

    def hook(pubs: list[Published]) -> None:
        write_final_record(store, RunRecord(
            run_id=rid(run), stage=stage, writer=stage, plan="dev-v2", status="ok", started_at=at,
            heartbeat_at=at, finished_at=at, code_digest=dg("code"), config_digest=dg(("config", stage)),
            scope_complete=True, version={"rows_touched": n}, outputs={"documents": pubs[0].as_output()}))

    last = last_file(store, DOCS)
    if part_of is None:
        assert last is None, f"{DOCS} is not empty"
    else:
        assert last is not None and part_of in (last.file_id, last.name), f"{part_of} is not the last file of {DOCS}"
    return write_series(store, "documents", "documents", table("documents", rows), fields,
                        kind="full" if part_of is None else "part", at=at, finished_at=at, on_published=hook)


def data_names(store: Path) -> list[str]:
    return sorted(p.name for p in (store / DATA).rglob("*.parquet"))


@pytest.fixture
def plan() -> Any:
    return load_plan(DEV_PLAN)


@pytest.fixture
def stage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private workflows root for toy stages written by a test (echo_test stays the repo's)."""
    root = tmp_path / "workflows"
    (root / "echo_test").mkdir(parents=True)
    (root / "echo_test" / "main.py").write_text((REPO / "workflows" / "echo_test" / "main.py").read_text())
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", root)
    return root


def toy_stage(root: Path, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(textwrap.dedent(body))


# ---------------------------------------------------------------- order
def test_topo_order_phase_tiebreak_and_requires(plan: Any) -> None:
    shuffled = ["fold.build", "annotate.screen", "acquire", "gather", "collect.arxiv", "triage", "fold.corpus"]
    assert topo_order(shuffled) == ["collect.arxiv", "gather", "triage", "acquire", "fold.corpus",
                                    "annotate.screen", "fold.build"]
    # a requires edge wins over phase order; edges to stages outside the set are ignored
    assert topo_order(["annotate.affiliation", "annotate.screen"],
                      {"annotate.screen": ["annotate.affiliation", "collect.x"]}) == [
        "annotate.affiliation", "annotate.screen"]
    with pytest.raises(StageError, match="cycle"):
        topo_order(["triage", "acquire"], {"triage": ["acquire"], "acquire": ["triage"]})
    assert plan_order(plan) == [s.stage for s in plan.stages]  # dev-v2 is listed in build order


# ---------------------------------------------------------------- plan loader
def test_plan_refusals(tmp_path: Path) -> None:
    base = DEV_PLAN.read_text()
    cases = {
        "input_rule": base.replace("schema_version: 2", "schema_version: 2\ninput_rule: newest_finished_same_plan"),
        "compact_after": base.replace("  compaction:", "  compact_after: 12\n  compaction:"),
        "per-stage kinds": base.replace("  compaction:", "  gather: full\n  compaction:"),  # D378: out: {kind:}
        "not implemented in v4 M1-M5": base.replace("dead_run_charge: false", "dead_run_charge: true"),
        "exactly one of `sql:`, `question:` or `rule:`": base.replace(
            "    - {sql: ", "    - {question: q, prompt: p, fetch_on: [x], sql: "),
        "listed twice": base.replace("  - {stage: gather}", "  - {stage: gather}\n  - {stage: gather}"),
        "unknown content roles": base.replace("repos: [readme]", "repos: [readme, tarball]"),
        # D437 (S25-81): nothing compares config stamps, so a bump_on would silently do nothing
        "is read by nothing (no config stamp is compared, D437": base.replace(
            "  - {stage: gather}", "  - {stage: gather, bump_on: [channels]}"),
    }
    for needle, text in cases.items():
        assert text != base, needle
        p = tmp_path / "p.yaml"
        p.write_text(text)
        with pytest.raises(PlanError, match=needle.replace("(", r"\(").replace("`", "`")):
            load_plan(p)
    pl = load_plan(DEV_PLAN)
    assert pl.gate.is_sql_only
    assert all(s.budget is None or s.budget.max_usd is None or s.budget.max_usd <= 3 for s in pl.stages)
    std = load_plan(REPO / "configs" / "plans" / "std-v2.yaml")
    assert std.stage_line("triage").prior == ["triage_evidence"]  # type: ignore[union-attr]
    assert std.plan_digest != pl.plan_digest and len(std.gate_digest) == 64


def test_empty_bump_on_loads_and_keeps_the_line_digest() -> None:
    """D437: only a non-empty ``bump_on`` is refused; ``bump_on: []`` is the default, so it loads
    and the line digest (which still dumps the field) does not move."""
    assert StageLine(stage="gather", bump_on=[]).digest() == StageLine(stage="gather").digest()
    assert "bump_on" in StageLine(stage="gather").model_dump()


# ---------------------------------------------------------------- run_stage end to end
def test_echo_end_to_end(tmp_path: Path, plan: Any) -> None:
    store = tmp_path / "store"
    base = seed_documents(store)
    before = read_view(resolve(store, DOCS))
    rec = run_stage(store, plan, "echo_test", [DOCS], kind="part")
    assert rec.status == "ok" and rec.error_class is None and rec.scope_complete
    assert rec.inputs == {"documents": base.file_id}
    out = rec.outputs["documents"]
    f = parse_data_key(out["path"])
    assert (f.dir, f.run_id, f.kind, out["row_count"]) == (DOCS.rstrip("/"), rec.run_id, "part", 10)
    base_name = base.staged.dest.name
    assert (out["kind"], out["base"], out["after"], out["series"]) == ("part", base_name, base_name, "main")
    assert rec.version == {"rows_touched": 0}
    assert rec.substages == {"echo": {"ran": True, "scope_complete": True},
                             "noop": {"ran": True, "scope_complete": True}}
    assert rec.counts["echoed"] == 10 and rec.inflight == []
    ch = resolve(store, DOCS)
    assert ch.head == out["file_id"] and ch.version_chain == (base.file_id, out["file_id"])
    assert read_view(ch).equals(before)  # rows re-emitted unchanged
    assert files_without_ok_record(store) == []
    assert [r.run_id for r in read_run_records(store)][-1] == rec.run_id
    assert running_records(store) == []  # the final record replaced the running json (D344)
    assert run_record_path(store, rec) == store / "runs" / (
        f"{rec.started_at.strftime('%Y%m%dT%H%MZ')}_{rec.run_id}.echo_test.json.zst")
    assert run_record_path(store, rec).is_file()
    # the default kind is full: the part the stage emits is applied to the version it read
    full = run_stage(store, plan, "echo_test", [DOCS])
    o2 = full.outputs["documents"]
    assert full.status == "ok" and (o2["kind"], o2["base"], o2["row_count"]) == ("full", f.name, 12)
    assert full.counts["materialized.documents"] == 12
    ch2 = resolve(store, DOCS)
    assert ch2.version_chain == (o2["file_id"],) and read_view(ch2).equals(before)


def test_an_option_the_stage_does_not_read_is_refused_before_any_run_record(tmp_path: Path, plan: Any) -> None:
    """D434, D472: a stage module's ``OPTIONS`` are the only keys its line may carry; a misspelt one
    is refused before a run record exists (nothing echoed, nothing written, no ``failed`` record),
    naming the closest known key, as ``stack build`` refuses at its start."""
    store = tmp_path / "store"
    seed_documents(store)
    names, runs = data_names(store), [r.run_id for r in read_run_records(store)]
    bad = plan.model_copy(update={"stages": [*plan.stages, StageLine(stage="echo_test", options={"nn": 3})]})
    with pytest.raises(StageError, match=re.escape(
            "echo_test: unknown options 'nn' (did you mean 'n'?); known: ['n']")):
        run_stage(store, bad, "echo_test", [DOCS], kind="part")
    assert [r.run_id for r in read_run_records(store)] == runs and running_records(store) == []
    assert data_names(store) == names
    good = plan.model_copy(update={"stages": [*plan.stages, StageLine(stage="echo_test", options={"n": 3})]})
    assert run_stage(store, good, "echo_test", [DOCS], kind="part").status == "ok"


def test_a_stage_module_may_define_a_dataclass_under_future_annotations(stage_dir: Path) -> None:
    """L1-8 (S25-92): the loader registers the module in ``sys.modules`` before running it, as an
    import would; ``@dataclass`` with stringized annotations looks its module up there."""
    toy_stage(stage_dir, "dc_test", """
        from __future__ import annotations

        from dataclasses import dataclass


        @dataclass(frozen=True)
        class Point:
            x: int
            y: int = 0


        def run(ctx):
            raise AssertionError("not run")
        """)
    module = stage_runner.load_stage_module("dc_test")
    assert module.Point(1).x == 1 and module.Point(1, 2) == module.Point(1, 2)
    assert sys.modules[module.__name__] is module


def test_only_noop_emits_nothing_and_bad_only_refused(tmp_path: Path, plan: Any) -> None:
    store = tmp_path / "store"
    seed_documents(store)
    names = data_names(store)
    rec = run_stage(store, plan, "echo_test", [DOCS], only=["noop"])
    assert rec.status == "ok" and rec.outputs == {} and rec.version is None and rec.only == ["noop"]
    assert rec.substages["echo"] == {"ran": False, "scope_complete": False}
    assert data_names(store) == names
    with pytest.raises(StageError, match="not in its substages"):
        run_stage(store, plan, "echo_test", [DOCS], only=["fetch"])


def test_failing_stage_leaves_data_untouched(tmp_path: Path, plan: Any, stage_dir: Path) -> None:
    store = tmp_path / "store"
    seed_documents(store)
    toy_stage(stage_dir, "echo_boom", """
        from stack.stage_runner import StageResult
        def run(ctx):
            ctx.emit_part("documents", ctx.view("documents").slice(0, 3), "*")
            ctx.heartbeat(inflight_keys=["k1", "k2"])
            raise RuntimeError("echo_boom: deliberate failure")
    """)
    names, head = data_names(store), head_of(store, DOCS)
    rec = run_stage(store, plan, "echo_boom", [DOCS])
    assert rec.status == "failed" and rec.error_class == "code_bug" and "deliberate failure" in (rec.error or "")
    assert rec.outputs == {} and rec.inflight == ["k1", "k2"]
    on_disk = find_run_record(store, rec.run_id)
    assert on_disk is not None and on_disk.status == "failed" and on_disk.error_class == "code_bug"
    assert data_names(store) == names and head_of(store, DOCS) == head
    assert files_without_ok_record(store) == []


def test_head_moved_race_fails_and_publishes_nothing(tmp_path: Path, plan: Any, stage_dir: Path) -> None:
    store = tmp_path / "store"
    base = seed_documents(store)
    toy_stage(stage_dir, "racer", f"""
        from stack.stage_runner import StageResult
        from tests.test_stage_runner import seed_documents
        def run(ctx):
            ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")
            seed_documents(ctx.store_root, run=7, part_of={base.file_id!r})  # another run publishes
            return StageResult()
    """)
    rec = run_stage(store, plan, "racer", [DOCS], kind="part")
    assert rec.status == "failed" and rec.error_class == "head_moved" and rec.outputs == {}
    on_disk = find_run_record(store, rec.run_id)
    assert on_disk is not None and on_disk.status == "failed" and on_disk.error_class == "head_moved"
    assert len(data_names(store)) == 2  # the base and the other run's part; nothing of ours
    assert not any(rec.run_id in n for n in data_names(store))
    staged = list((store / DOCS).glob(f".*_{rec.run_id}.*.parquet.tmp"))
    assert len(staged) == 1  # the refused output stays staged as <dir>/.<name>.tmp (garbage)
    assert files_without_ok_record(store) == []


def test_exact_file_input_pins_that_version(tmp_path: Path, plan: Any) -> None:
    """An exact-file ``--in`` pins that file's version (D373, D381): written back into its
    directory on a file that is not the last one it would fork it (refused before any run); into another series it is a full without a base there. A family
    directory without a series and an unpublished file are refused before any run."""
    store = tmp_path / "store"
    base = seed_documents(store)
    part = seed_documents(store, run=2, part_of=base.file_id)
    names = data_names(store)
    with pytest.raises(StageError, match="without a series"):
        run_stage(store, plan, "echo_test", ["data/documents/"])
    with pytest.raises(StageError, match="not published"):
        run_stage(store, plan, "echo_test", [f"{DOCS}{base.staged.dest.utc}_{rid(9)}.part.parquet"])
    assert data_names(store) == names and running_records(store) == []
    assert [r.run_id for r in read_run_records(store)] == [rid(1), rid(2)]  # no run started
    with pytest.raises(StageError, match="would fork the directory"):  # refused before any run (D381)
        run_stage(store, plan, "echo_test", [base.staged.dest.key])
    assert data_names(store) == names and [r.run_id for r in read_run_records(store)] == [rid(1), rid(2)]
    repro = run_stage(store, plan, "echo_test", [str(base.path)], ["data/documents/repro/"])
    out = repro.outputs["documents"]
    assert repro.status == "ok" and (out["kind"], out["base"], out["series"]) == ("full", "", "repro")
    assert read_view(resolve(store, sdir("documents", "repro"))).equals(read_view(resolve(store, base.staged.dest.key)))
    rec = run_stage(store, plan, "echo_test", [DOCS], kind="part")  # the last file is fine
    assert rec.status == "ok" and rec.inputs == {"documents": part.file_id}


def test_context_protocol_errors(tmp_path: Path, plan: Any, stage_dir: Path) -> None:
    """Two outputs for one family, and a non-StageResult return, end ``failed[code_bug]``; a
    planned stop is ``ok`` with ``scope_complete = false`` and the header says so."""
    store = tmp_path / "store"
    seed_documents(store)
    toy_stage(stage_dir, "twice", """
        def run(ctx):
            t = ctx.view("documents").slice(0, 1)
            ctx.emit_part("documents", t, "*")
            ctx.emit_part("documents", t, "*")
    """)
    rec = run_stage(store, plan, "twice", [DOCS])
    assert rec.status == "failed" and "second documents output" in (rec.error or "")
    toy_stage(stage_dir, "halts", """
        from stack.stage_runner import StageResult
        def run(ctx):
            ctx.emit_part("documents", ctx.view("documents").slice(0, 1), "*")
            return StageResult(scope_complete=False, counts={"not_attempted": 4})
    """)
    rec = run_stage(store, plan, "halts", [DOCS])
    assert rec.status == "ok" and rec.scope_complete is False and rec.counts["not_attempted"] == 4
    from stack.store.parquet import read_header
    h = read_header(store / rec.outputs["documents"]["path"])
    assert h["scope_complete"] == "false" and json.loads(h["inputs"]) == rec.inputs
    assert h["code_digest"] == rec.code_digest and h["plan"] == "dev-v2" and h["stage"] == "halts"


def test_fold_stages_refused_before_dispatch(tmp_path: Path, plan: Any) -> None:
    # T1.6 wired fold.corpus/fold.compact (tests/test_cli.py runs them), T5.2 fold.build;
    # fold.release (not a stage) and unknown configs are refused before any run record exists.
    with pytest.raises(StageError, match="not a stage"):
        run_stage(tmp_path, plan, "fold.release", [])
    with pytest.raises(StageError, match="no fold config"):
        run_stage(tmp_path, plan, "fold.nope", [])
    assert not (tmp_path / "runs").exists()


# ---------------------------------------------------------------- D786: pools bounded by the container's task limit
def _tasks(d: Path, top: str, cur: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pids.max").write_text(f"{top}\n")
    (d / "pids.current").write_text(f"{cur}\n")


def test_thread_room_reads_the_containers_cgroup_or_the_tightest_ancestor_on_a_host(tmp_path: Path) -> None:
    """A container's ``/sys/fs/cgroup`` is its own cgroup; on a host the process's cgroup and its ancestors
    each limit (the least room counts); ``max`` and missing files are no limit (the conftest's empty dir)."""
    root = stage_runner.CGROUP_ROOT
    assert stage_runner.thread_room() is None
    _tasks(root, "1000", 212)
    assert stage_runner.thread_room() == 788
    _tasks(root, "max", 212)
    assert stage_runner.thread_room() is None
    stage_runner.PROC_CGROUP.write_text("0::/user.slice/u.slice/app.scope\n")
    _tasks(root / "user.slice" / "u.slice", "500", 450)
    _tasks(root / "user.slice" / "u.slice" / "app.scope", "4000", 30)
    assert stage_runner.thread_room() == 50


def test_a_drain_pool_is_capped_at_the_task_room_less_the_reserve(tmp_path: Path) -> None:
    """D786: Railway's ``pids.max`` is 1000; a 1024-worker line keeps ``THREAD_RESERVE`` tasks for the rest of
    the container. Without a readable limit the pool is what the line asks; never below one thread."""
    from stack.stage_runner import THREAD_RESERVE, DrainPool

    with DrainPool(max_workers=1024, thread_name_prefix="t") as pool:
        assert (pool.workers, pool.requested) == (1024, 1024)
    _tasks(stage_runner.CGROUP_ROOT, "1000", 200)
    with DrainPool(max_workers=1024, thread_name_prefix="t") as pool:
        assert (pool.workers, pool.requested) == (800 - THREAD_RESERVE, 1024)
        assert pool._max_workers == pool.workers
    with DrainPool(max_workers=64, thread_name_prefix="t") as pool:
        assert pool.workers == 64
    _tasks(stage_runner.CGROUP_ROOT, "1000", 990)
    with DrainPool(max_workers=64, thread_name_prefix="t") as pool:
        assert pool.workers == 1


def failing_thread_starts(monkeypatch: pytest.MonkeyPatch, prefix: str, allowed: int) -> list[str]:
    """``threading.Thread.start`` raises CPython's ``can't start new thread`` for threads named ``prefix*``
    once ``allowed`` of them started (what a container's ``pids.max`` does); other threads start."""
    import threading

    started: list[str] = []
    real = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name.startswith(prefix):
            if len(started) >= allowed:
                raise RuntimeError("can't start new thread")
            started.append(self.name)
        real(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    return started


def test_a_drain_pool_goes_on_with_the_threads_it_has_when_another_cannot_start(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """D786: the 2026-11-01 cycle's ``annotate.technique_staged`` (1024 workers) failed ``code_bug`` on
    ``pool.submit``: ``RuntimeError: can't start new thread`` at about 800 threads. Now the call is
    queued, the pool caps itself at the threads it started, and every call runs on them."""
    import threading

    from stack.stage_runner import DrainPool

    started = failing_thread_starts(monkeypatch, "cap", allowed=3)
    gate = threading.Event()
    with DrainPool(max_workers=16, thread_name_prefix="cap") as pool:
        futs = [pool.submit(lambda i=i: gate.wait(5) and i) for i in range(10)]
        assert (pool.workers, pool.requested, len(started)) == (3, 16, 3)
        gate.set()
        assert [f.result(timeout=5) for f in futs] == list(range(10))
    started.clear()
    with pytest.raises(RuntimeError, match="can't start new thread"):  # no thread at all: as before
        failing_thread_starts(monkeypatch, "none", allowed=0)
        with DrainPool(max_workers=4, thread_name_prefix="none") as pool:
            pool.submit(lambda: None)


def test_stack_pool_max_threads_is_an_absolute_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """D786: where the container cannot read its task limit (a parent cgroup's ``pids.max``, its own ``max``), the
    operator's ``STACK_POOL_MAX_THREADS`` bounds every pool; with a readable room the lower of the two wins. Unset or
    empty is no ceiling; anything else but a positive integer refuses the pool."""
    from stack.stage_runner import POOL_MAX_THREADS_ENV, THREAD_RESERVE, DrainPool, StageError

    assert POOL_MAX_THREADS_ENV == "STACK_POOL_MAX_THREADS"
    monkeypatch.setenv(POOL_MAX_THREADS_ENV, "")
    assert DrainPool(max_workers=1024).workers == 1024
    monkeypatch.setenv(POOL_MAX_THREADS_ENV, "600")
    _tasks(stage_runner.CGROUP_ROOT, "max", 200)
    assert DrainPool(max_workers=1024).workers == 600
    assert DrainPool(max_workers=64).workers == 64
    _tasks(stage_runner.CGROUP_ROOT, "1000", 400)
    assert DrainPool(max_workers=1024).workers == 600 - THREAD_RESERVE
    for bad in ("0", "-5", "many", "6.5"):
        monkeypatch.setenv(POOL_MAX_THREADS_ENV, bad)
        with pytest.raises(StageError, match="STACK_POOL_MAX_THREADS must be a positive integer"):
            DrainPool(max_workers=8)


def test_the_cap_line_comes_when_the_pool_starts_threads_and_started_counts_them(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """D786: a capped pool that never runs anything (an empty queue) says nothing; one that does says its cap once.
    ``started`` is the threads started, not the cap."""
    from stack.stage_runner import DrainPool

    monkeypatch.setenv("STACK_POOL_MAX_THREADS", "4")
    with DrainPool(max_workers=64, thread_name_prefix="quiet") as pool:
        assert (pool.workers, pool.started) == (4, 0)
    assert "quiet" not in capsys.readouterr().err
    with DrainPool(max_workers=64, thread_name_prefix="loud") as pool:
        for f in [pool.submit(lambda: None) for _ in range(2)]:
            f.result(timeout=5)
        assert 1 <= pool.started <= 2
    err = capsys.readouterr().err
    assert err.count("loud: at most 4 worker threads, not 64: STACK_POOL_MAX_THREADS=4 (D786)") == 1
