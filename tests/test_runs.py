"""Run records (T1.3 slice of ``stack/runs.py``): running and final records round trip, a run's
job tick (:class:`stack.store.job.RunJob`) with the inflight cap, X11's ``repeated_inflight`` on two
index-dead records sharing a key (``stack status``). ``files_without_ok_record`` on a file whose run
never finished is tests/test_run_liveness.py's (and tests/test_runner_crash.py's for a staged file).
Dead-run derivation is the store index's (markers; tests/test_store_index.py).
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pytest

from stack.cli.status import collect_status
from stack.runs import (
    INFLIGHT_CAP,
    RunRecord,
    build_record_path,
    find_run_record,
    read_run_record,
    read_run_records,
    run_record_path,
    write_build_record,
    write_final_record,
    write_running_record,
)
from stack.store.index import DEAD_AFTER
from stack.store.job import RunJob
from tests.synth.rows import dg, rid

NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)


def running(n: int, heartbeat_age: dt.timedelta, inflight: list[str] | None = None) -> RunRecord:
    started = NOW - heartbeat_age - dt.timedelta(minutes=1)
    return RunRecord(run_id=rid(n), stage="acquire", writer="acquire", plan="dev-v2", status="running",
                     started_at=started, heartbeat_at=NOW - heartbeat_age, code_digest=dg("code"),
                     config_digest=dg("config"), inflight=inflight or [],
                     substages={"fetch": {"ran": True, "scope_complete": False}}, counts={"fetched": 3})


def test_running_and_final_round_trip(tmp_path: Path) -> None:
    """D344: one json per run at runs/<head>.<stage>.json.zst (builds/<build>/runs/ in a build),
    written running, then replaced once by the final record."""
    rec = running(1, dt.timedelta(seconds=5), ["s1|full_text"])
    rec.comment = "probe"
    p = write_running_record(tmp_path, rec)
    head = f"{rec.started_at:%Y%m%dT%H%MZ}_{rec.run_id}_probe"
    assert p == run_record_path(tmp_path, rec) == tmp_path / "runs" / f"{head}.acquire.json.zst"
    assert read_run_record(p) == rec and find_run_record(tmp_path, rec.run_id) == rec
    assert read_run_records(tmp_path) == []  # final records only
    with pytest.raises(ValueError, match="a running record"):
        write_running_record(tmp_path, RunRecord(**{**rec.__dict__, "status": "ok"}))
    final = RunRecord(**{**rec.__dict__, "status": "failed", "error_class": "head_moved", "finished_at": NOW})
    assert write_final_record(tmp_path, final) == p  # replaces the running json
    got = find_run_record(tmp_path, rec.run_id)
    assert got is not None and got.status == "failed" and got == final
    assert read_run_records(tmp_path) == [got]
    with pytest.raises(FileExistsError, match="final failed record already"):
        write_final_record(tmp_path, final)  # immutable once final
    in_build = write_running_record(tmp_path, running(2, dt.timedelta(0)), build="20261012T1100Z_w1.dev-v2")
    assert in_build.parent == tmp_path / "builds" / "20261012T1100Z_w1.dev-v2" / "runs"
    with pytest.raises(ValueError, match="not a run error class"):
        RunRecord(**{**rec.__dict__, "status": "failed", "error_class": "oops"})


def test_build_record_written_running_then_final(tmp_path: Path) -> None:
    rec = {"build_id": "w000000000001", "plan": "dev-v2", "status": "running", "comment": "p1",
           "started_at": NOW.isoformat(), "finished_at": None}
    p = write_build_record(tmp_path, rec)
    assert p == build_record_path(tmp_path, rec)
    assert p == tmp_path / "builds" / "20261012T1200Z_w000000000001_p1.dev-v2" / "build.json.zst"
    write_build_record(tmp_path, {**rec, "status": "ok", "finished_at": NOW.isoformat()})  # rewritten
    with pytest.raises(FileExistsError):
        write_build_record(tmp_path, {**rec, "status": "failed"})  # immutable once final


def _marker(path: Path, age: dt.timedelta | None) -> None:
    """The ``-running.json`` marker beside a run json, its mtime ``age`` ago (``None``: no marker)."""
    m = path.with_name(path.name.removesuffix(".json.zst") + "-running.json")
    m.unlink(missing_ok=True)
    if age is not None:
        m.write_text('{"tick": "x"}')
        at = (dt.datetime.now(dt.UTC) - age).timestamp()
        os.utime(m, (at, at))


def test_repeated_inflight_two_dead_records_one_key(tmp_path: Path) -> None:
    """X11 (``stack status``'s ``repeated_inflight``): a key inflight in two index-dead runs (a
    marker older than DEAD_AFTER); a live run (fresh marker) or an unknown one (no marker) with
    the same key does not count."""
    key = "s0000000000001|full_text"
    stale = DEAD_AFTER + dt.timedelta(minutes=1)
    _marker(write_running_record(tmp_path, running(1, dt.timedelta(hours=2), [key, "s2|post"])), stale)
    _marker(write_running_record(tmp_path, running(2, dt.timedelta(hours=1), [key])), stale)
    _marker(write_running_record(tmp_path, running(3, dt.timedelta(minutes=1), [key, "s2|post"])),
            dt.timedelta(0))  # alive
    p4 = write_running_record(tmp_path, running(5, dt.timedelta(hours=1), [key, "s2|post"]))  # unknown
    assert collect_status(tmp_path)["repeated_inflight"] == [{"key": key, "runs": [rid(1), rid(2)]}]
    _marker(run_record_path(tmp_path, running(2, dt.timedelta(hours=1))), dt.timedelta(0))  # run 2 alive again
    assert collect_status(tmp_path)["repeated_inflight"] == []
    assert p4.is_file()


def test_tick_thread_inflight_cap_and_final(tmp_path: Path) -> None:
    """A run's job (local-only store): start writes the running json, the log and the marker; the
    tick thread rewrites the json (``heartbeat_at``); the inflight cap (D19); the final record
    replaces the running json and the marker goes."""
    rec = running(4, dt.timedelta(0))
    rec.heartbeat_at = rec.started_at
    job = RunJob(tmp_path, rec, interval=dt.timedelta(seconds=0.05))
    job.start()
    path = job.json_path
    assert path == run_record_path(tmp_path, rec) and job.marker_path.is_file() and job.log_path.is_file()
    assert rec.upload is False  # a local-only store's default
    first = read_run_record(path).heartbeat_at
    job.start_tick()
    time.sleep(0.3)
    job.stop_tick()
    assert read_run_record(path).heartbeat_at > first
    job.set_inflight([f"k{i}" for i in range(INFLIGHT_CAP + 5)], INFLIGHT_CAP)
    on_disk = read_run_record(path)
    assert len(on_disk.inflight) == INFLIGHT_CAP and on_disk.counts["inflight_dropped"] == 5
    job.set_substage("fetch", ran=True, scope_complete=True)
    assert read_run_record(path).substages["fetch"] == {"ran": True, "scope_complete": True}
    with job.lock:
        rec.status, rec.finished_at = "ok", NOW
    assert job.finish_record() == []
    assert read_run_record(path).status == "ok"
    assert sorted(p.name for p in path.parent.iterdir()) == sorted([path.name, job.log_path.name])  # no temp, no marker
    with pytest.raises(FileExistsError, match="final ok record already"):
        RunJob(tmp_path, rec).finish_record()  # immutable once final




def test_selection_field_round_trips_and_is_omitted_when_none(tmp_path: Path) -> None:
    """D758: an annotate or triage run's ``selection`` (target, accepted list, selection digest) is kept by the record."""
    rec = running(3, dt.timedelta(seconds=5))
    assert "selection" not in rec.to_json()
    rec.selection = {"digest": dg("sel"), "target": dg("b"), "accepted": [
        {"config": dg("a"), "name": "screen-v7", "decision": "D1"}]}
    back = read_run_record(write_running_record(tmp_path, rec))
    assert back.selection == rec.selection and back.extra == {}
