"""Run liveness after T-STORE A2 (D352-D355): a run's job
(:class:`stack.store.job.RunJob`) writes the json, the log and the ``-running.json`` marker; its
tick rewrites them; the store index derives liveness from the marker. No locks, no pid liveness,
no writer intent, no gc (all removed).

* tick faults are retried, counted (``counts.tick_errors``) and logged; the run still publishes;
* the index sees a run in progress ``live`` and a finished one ``finished``;
* R2-7 no rewrite or rollback after the final record, the tick alive through staging; R2-8
  SIGTERM → ``cancelled``; D345 a data file no record names is in no series; R2-5 unknown record
  keys kept; R2-9 orphan guard (an ``excludes`` record).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import zstandard

from stack import stage_runner
from stack.cli import _on_sigterm
from stack.plan import load_plan
from stack.runs import (
    RunRecord,
    RunRecordWarning,
    files_without_ok_record,
    find_run_record,
    has_ok_record,
    read_run_record,
    read_run_records,
    records_without_file,
    running_records,
    write_final_record,
)
from stack.stage_runner import run_stage
from stack.store import chain as chain_mod
from stack.store import job as job_mod
from stack.store.chain import (
    HeaderFields,
    head_of,
    orphan,
)
from stack.store.index import store_index
from tests.synth.records import record_files, sdir, write_series
from tests.synth.rows import AT, dg, document_row, rid, table
from tests.test_stage_runner import seed_documents

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
FAST = dt.timedelta(milliseconds=50)
DOCS = sdir("documents")  # data/documents/main/


# ---------------------------------------------------------------- helpers
@pytest.fixture
def plan() -> Any:
    return load_plan(DEV_PLAN)


@pytest.fixture
def stage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workflows"
    root.mkdir()
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", root)
    return root


@pytest.fixture
def fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run's job ticks every 50 ms (read at the job's construction)."""
    monkeypatch.setattr(job_mod, "TICK", FAST)


def toy(root: Path, name: str, body: str) -> None:
    d = root.joinpath(*name.split("."))
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(textwrap.dedent(body))


def run_log(store: Path, run_id: str) -> str:
    """A run's whole log (``<stem>.log.zst`` beside its json)."""
    (p,) = (store / "runs").glob(f"*_{run_id}*.log.zst")
    return zstandard.ZstdDecompressor().decompress(p.read_bytes()).decode()


EMIT_TWO = 'ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")'


# ---------------------------------------------------------------- tick faults (D354)
def test_transient_tick_fault_run_still_publishes(tmp_path: Path, plan: Any, stage_dir: Path,
                                                  fast_tick: None) -> None:
    """The R6-1 repro over the job's tick: the run directory turns unwritable for a while mid-run
    (EACCES stands in for ENOSPC: both are OSError on the running json's temp file). The tick
    retries (well under SELF_CANCEL_AFTER), counts the failures and the run publishes."""
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, "w_fault", f"""
        import os, time
        from stack.stage_runner import StageResult
        def run(ctx):
            d = ctx._prov.json_path.parent
            os.chmod(d, 0o500)
            try:
                time.sleep(0.5)
            finally:
                os.chmod(d, 0o755)
            time.sleep(0.25)
            {EMIT_TWO}
            return StageResult()
    """)
    rec = run_stage(store, plan, "w_fault", [DOCS], kind="part")
    assert rec.status == "ok", rec.error
    assert rec.counts.get("tick_errors", 0) >= 2, rec.counts
    log = run_log(store, rec.run_id)
    assert "tick failed" in log and "retrying" in log, log
    assert head_of(store, DOCS) == Path(rec.outputs["documents"]["path"]).name
    on_disk = find_run_record(store, rec.run_id)
    assert on_disk is not None and on_disk.status == "ok" and on_disk.counts["tick_errors"] >= 2
    assert files_without_ok_record(store) == [] and running_records(store) == []
    assert not list((store / "runs").glob("*-running.json"))  # the marker went at the end


def test_index_sees_a_run_live_then_finished(tmp_path: Path, plan: Any, stage_dir: Path) -> None:
    """The store index's liveness over a real run: ``live`` from inside the run (the marker the
    job's start wrote), ``finished`` once its final record is written."""
    store = tmp_path / "store"
    seed_documents(store)
    seen = tmp_path / "seen.json"
    toy(stage_dir, "w_live", f"""
        import json, pathlib
        from stack.stage_runner import StageResult
        from stack.store.index import store_index
        def run(ctx):
            e = store_index(ctx.store_root).run(ctx.run_id)
            pathlib.Path({str(seen)!r}).write_text(json.dumps([e.liveness, e.record.status]))
            {EMIT_TWO}
            return StageResult()
    """)
    rec = run_stage(store, plan, "w_live", [DOCS], kind="part")
    assert rec.status == "ok", rec.error
    assert json.loads(seen.read_text()) == ["live", "running"]
    e = store_index(store).run(rec.run_id)
    assert e is not None and (e.liveness, e.record.status) == ("finished", "ok")


# ---------------------------------------------------------------- R2-7: after the final record
def test_error_after_the_ok_record_keeps_it(tmp_path: Path, plan: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    seed_documents(store)
    real = stage_runner.publish

    def publish_then_fail(*a: Any, **k: Any) -> Any:
        real(*a, **k)  # the final ok record is written by its on_published hook
        raise chain_mod.ChainError("after the record (injected)")

    monkeypatch.setattr(stage_runner, "publish", publish_then_fail)
    said: list[str] = []
    rec = run_stage(store, plan, "echo_test", [DOCS], warn=said.append, kind="part")
    assert rec.status == "ok" and any("record is kept" in m for m in said), said
    on_disk = find_run_record(store, rec.run_id)
    assert on_disk is not None and on_disk.status == "ok"
    assert head_of(store, DOCS) == Path(rec.outputs["documents"]["path"]).name
    assert running_records(store) == [] and records_without_file(store) == []


def test_interrupt_after_the_ok_record_does_not_roll_back(tmp_path: Path, plan: Any,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    seed_documents(store)

    def die(point: str) -> None:
        if point == "after_final_local":  # the final ok json is local; the log not yet written
            raise KeyboardInterrupt

    monkeypatch.setattr(job_mod, "maybe_die", die)
    with pytest.raises(KeyboardInterrupt):
        run_stage(store, plan, "echo_test", [DOCS], kind="part")
    (rec,) = [r for r in read_run_records(store) if r.stage == "echo_test"]
    assert rec.status == "ok"
    assert head_of(store, DOCS) == Path(rec.outputs["documents"]["path"]).name  # not moved back to staging
    assert records_without_file(store) == [] and files_without_ok_record(store) == []
    assert running_records(store) == []


def test_tick_runs_through_staging(tmp_path: Path, plan: Any, monkeypatch: pytest.MonkeyPatch,
                                   fast_tick: None) -> None:
    store = tmp_path / "store"
    seed_documents(store)
    beats: list[dt.datetime] = []
    real = stage_runner.stage_file

    def slow_stage_file(root: Any, *a: Any, **k: Any) -> Any:
        (prov,) = running_records(root)
        beats.append(prov.heartbeat_at)
        time.sleep(0.4)
        (prov,) = running_records(root)
        beats.append(prov.heartbeat_at)
        return real(root, *a, **k)

    monkeypatch.setattr(stage_runner, "stage_file", slow_stage_file)
    rec = run_stage(store, plan, "echo_test", [DOCS], kind="part")
    assert rec.status == "ok" and beats[1] > beats[0]


# ---------------------------------------------------------------- R2-8: SIGTERM (D570: the second one)
def test_sigterm_handler_ends_the_run_cancelled(tmp_path: Path, plan: Any, stage_dir: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """D570 changed R2-8: the first SIGTERM drains (``tests/test_drain.py``); the second (past the
    repeat window, here 0) is the cancel this test pinned for the first."""
    monkeypatch.setattr(job_mod, "SIGTERM_REPEAT_S", 0.0)
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, "w_term", """
        import os, signal, time
        def run(ctx):
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
            raise AssertionError("not interrupted")
    """)
    prev = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_stage(store, plan, "w_term", [DOCS])
    finally:
        signal.signal(signal.SIGTERM, prev)
    (rec,) = [r for r in read_run_records(store) if r.stage == "w_term"]
    assert (rec.status, rec.error_class) == ("cancelled", "operator")
    assert running_records(store) == []


@pytest.mark.slow
def test_sigterm_to_stack_run_subprocess_drains_then_cancels(tmp_path: Path) -> None:
    """The real CLI process: one SIGTERM drains (a stage polling ``ctx.drain`` publishes and the run
    ends ``ok``, scope incomplete, exit 0); a second SIGTERM to a run that ignores the drain cancels it."""
    store = tmp_path / "store"
    seed_documents(store)
    wf = tmp_path / "wf"
    toy(wf, "w_poll", """
        import time
        from stack.stage_runner import StageResult
        def run(ctx):
            deadline = time.monotonic() + 60
            while not ctx.drain.stopping() and time.monotonic() < deadline:
                time.sleep(0.05)
            ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")
            return StageResult()
    """)
    env = {**os.environ, stage_runner.WORKFLOWS_ROOT_ENV: str(wf)}
    proc = subprocess.Popen([sys.executable, "-m", "stack.cli", "run", "w_poll", "--in", DOCS, "--part",
                             "--store", str(store), "--plan", str(DEV_PLAN)],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not running_records(store) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert running_records(store), "the run never started"
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert json.loads(out)["scope_complete"] is False and "draining" in err
    (rec,) = [r for r in read_run_records(store) if r.stage == "w_poll"]
    assert (rec.status, rec.scope_complete, rec.counts["stopped.drain"]) == ("ok", False, 1)
    assert running_records(store) == []

    toy(wf, "w_sleep", """
        import time
        def run(ctx):
            time.sleep(60)
    """)
    env = {**os.environ, stage_runner.WORKFLOWS_ROOT_ENV: str(wf)}
    proc = subprocess.Popen([sys.executable, "-m", "stack.cli", "run", "w_sleep", "--in", DOCS,
                             "--store", str(store), "--plan", str(DEV_PLAN)],
                            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not running_records(store) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert running_records(store), "the run never started"
    proc.send_signal(signal.SIGTERM)  # drains; the stage does not look
    time.sleep(job_mod.SIGTERM_REPEAT_S + 0.5)  # a second one sooner would be the same stop
    proc.send_signal(signal.SIGTERM)  # the hard cancel
    proc.communicate(timeout=60)
    assert proc.returncode != 0
    (rec,) = [r for r in read_run_records(store) if r.stage == "w_sleep"]
    assert (rec.status, rec.error_class) == ("cancelled", "operator")
    assert running_records(store) == []


# ---------------------------------------------------------------- R2-2: an unrecorded file
def test_unrecorded_file_is_in_no_series_until_recorded(tmp_path: Path) -> None:
    """D345: a file renamed into ``data/`` without its ok record (a crash after rename) is in no
    series: the head stays the last recorded file; once a record names the file it is the head.
    (``stack gc``'s half of the old test went with gc.)"""
    store = tmp_path / "store"
    base = seed_documents(store)
    later = AT + dt.timedelta(days=1)
    fields = HeaderFields(stage="gather", run_id=rid(50), config_digest=dg("c"), code_digest=dg("code"),
                          plan="dev-v2", blocks_written=["identity", "metadata", "inband"])
    stray = write_series(store, "documents", "documents", table("documents", [document_row(0, 50)]), fields,
                         kind="full", finished_at=later)  # no ok record: a crash after rename
    assert not has_ok_record(store, stray.path) and files_without_ok_record(store) == [stray.path]
    assert head_of(store, DOCS) == base.path.name
    # the same file with an ok record: it is the head
    record_files(store, [stray.path], at=later)
    assert head_of(store, DOCS) == stray.path.name and files_without_ok_record(store) == []


# ---------------------------------------------------------------- R2-5: unknown record keys
def test_unknown_record_keys_are_kept_and_warned(tmp_path: Path) -> None:
    store = tmp_path / "store"
    base = seed_documents(store)
    (path,) = (store / "runs").glob("*.json.zst")
    data = json.loads(zstandard.ZstdDecompressor().decompress(path.read_bytes()))
    data["future_field"] = {"x": 1}
    path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(data).encode()))
    with pytest.warns(RunRecordWarning, match="future_field"):
        (rec,) = read_run_records(store)
    assert rec.extra == {"future_field": {"x": 1}} and rec.to_json()["future_field"] == {"x": 1}
    with pytest.warns(RunRecordWarning):
        assert has_ok_record(store, base.path)


def test_unknown_error_class_is_kept_on_read_strict_in_code(tmp_path: Path) -> None:
    """S25-37: a failed record whose ``error_class`` this code does not know (written by newer
    code) is read with a warning and the label kept; the index, ``status``, the X1 basis and
    ``verify`` read the store; a record built in code with that class still raises."""
    from stack.checks.verify import run_checks
    from stack.cli.status import collect_status

    store = tmp_path / "store"
    base = seed_documents(store)
    at = AT + dt.timedelta(minutes=5)
    path = write_final_record(store, RunRecord(
        run_id=rid(9), stage="triage", writer="triage", plan="dev-v2", status="failed", started_at=at,
        heartbeat_at=at, finished_at=at, code_digest=dg("code"), config_digest=dg(("config", "triage")),
        error_class="code_bug", error="boom"))
    data = json.loads(zstandard.ZstdDecompressor().decompress(path.read_bytes()))
    data["error_class"] = "future_class"
    path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(data).encode()))
    with pytest.warns(RunRecordWarning, match="future_class"):
        recs = read_run_records(store)
    assert {(r.status, r.error_class) for r in recs} == {("ok", None), ("failed", "future_class")}
    with pytest.warns(RunRecordWarning):
        assert read_run_record(path).to_json()["error_class"] == "future_class"
    with pytest.warns(RunRecordWarning):
        assert has_ok_record(store, base.path) and files_without_ok_record(store) == []
    with pytest.warns(RunRecordWarning):
        report = collect_status(store)
    assert "failed[future_class]" in json.dumps(report)
    with pytest.warns(RunRecordWarning):
        assert run_checks(store)
    with pytest.raises(ValueError, match="not a run error class"):
        RunRecord(run_id=rid(10), stage="triage", writer="triage", plan="dev-v2", status="failed",
                  started_at=at, heartbeat_at=at, code_digest=dg("code"), config_digest=dg("c"),
                  error_class="future_class")


# ---------------------------------------------------------------- R2-9: orphan guard
def test_orphan_refuses_a_recorded_middle_file(tmp_path: Path) -> None:
    """D345: ``orphan`` writes a standalone ``chain.orphan`` ok record excluding the file and moves
    nothing; the guard refuses a middle file without ``force``, an excluded file, and a file no
    ok record names."""
    store = tmp_path / "store"
    base = seed_documents(store)
    part = seed_documents(store, run=2, part_of=base.file_id)
    with pytest.raises(chain_mod.ChainError, match="not the last file"):
        orphan(store, base.staged.dest.key)
    excl = orphan(store, part.staged.dest.key)  # the last file is allowed
    assert excl.parent == store / "runs" and excl.name.endswith(".chain.orphan.json.zst")
    rec = read_run_record(excl)
    assert (rec.stage, rec.status, rec.excludes, rec.outputs) == ("chain.orphan", "ok", [part.file_id], {})
    assert part.path.exists() and head_of(store, DOCS) == base.path.name
    with pytest.raises(chain_mod.ChainError, match="excluded already"):
        orphan(store, part.staged.dest.key)
    seed_documents(store, run=3, part_of=base.file_id)
    assert read_run_record(orphan(store, base.staged.dest.key, force=True)).excludes == [base.file_id]
    assert base.path.exists()  # nothing moved
    with pytest.raises(chain_mod.ChainError, match="no ok run record lists"):
        orphan(store, f"{DOCS}{base.staged.dest.utc}_{rid(99)}.part.parquet")
