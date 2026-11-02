"""T-STORE A2 (D352-D359): jobs on an S3 store over the fake
S3 (``tests/fakes/s3.py``, no network). The order of PUTs and DELETEs from the fake's call log
(start, tick, publish, end), a late tick that cannot replace the final json, a stale marker read
as dead, self-cancel with an injected clock and failing PUTs (then recovery at the next start),
the base-head re-check at publish, a no-upload job, ``stack store push`` and ``push --check``,
per-run upload in a build whose later stage fails, and three subprocess crash points
(``STACK_CRASH_AT``; the fake keeps its objects in a directory the subprocess shares)."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
import zstandard
from typer.testing import CliRunner

from stack.cli import app
from stack.plan import Plan
from stack.runs import RunRecord, files_without_ok_record, run_record_path
from stack.stage_runner import WORKFLOWS_ROOT_ENV, run_stage
from stack.store import job as J
from stack.store.chain import head_of, last_file
from stack.store.index import DEAD_AFTER, store_index
from stack.store.job import PushError, RunJob, check_push, push_job
from stack.store.layout import MIRROR_FILE
from stack.store.remote import resolve_store, using_s3_client
from tests.fakes.s3 import FAKE_ENV, FakeS3
from tests.synth.records import sdir
from tests.test_stage_runner import plan, seed_documents, stage_dir, toy_stage  # noqa: F401 — fixtures

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
BUCKET, PREFIX = "stack-data", "stack-v4"
URI = f"s3://{BUCKET}/{PREFIX}"
runner = CliRunner()
DOCS = sdir("documents")  # data/documents/main/


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    monkeypatch.setenv("STACK_MIRROR", str(tmp_path / "mirror"))


@pytest.fixture
def s3(tmp_path: Path, fake_env: None) -> Iterator[tuple[Path, FakeS3]]:
    """A mirror root and its fake bucket (in memory)."""
    fake = FakeS3(page=50)
    with using_s3_client(fake):
        yield resolve_store(URI, mirror_base=tmp_path / "mirror"), fake


# ---------------------------------------------------------------- helpers
def upload_all(root: Path, fake: FakeS3) -> None:
    """PUT every store file (seeded records and data) as is, like a finished uploading job."""
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != MIRROR_FILE and not p.name.startswith(".") and "cache" not in p.parts:
            fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(root).as_posix()}", Body=p.read_bytes())


def rel(key: str) -> str:
    return key.removeprefix(f"{PREFIX}/")


def writes(fake: FakeS3, since: int = 0) -> list[tuple[str, str]]:
    """``(op, key)`` of every PUT and DELETE since call ``since``, blobs left out."""
    return [(op, rel(k)) for op, k in fake.calls[since:] if op in ("put", "delete") and "/blobs/" not in k]


def remote_json(fake: FakeS3, key: str) -> dict[str, Any]:
    raw = fake.objects[(BUCKET, f"{PREFIX}/{key}")]
    return json.loads(zstandard.ZstdDecompressor().decompress(raw))


def has(fake: FakeS3, key: str) -> bool:
    return (BUCKET, f"{PREFIX}/{key}") in fake.objects


def job_keys(root: Path, rec: RunRecord, build: str | None = None) -> tuple[str, str, str]:
    """The json, log and marker keys of a run."""
    stem = run_record_path(root, rec, build).relative_to(root).as_posix().removesuffix(".json.zst")
    return f"{stem}.json.zst", f"{stem}.log.zst", f"{stem}-running.json"


def seeded(root: Path, fake: FakeS3) -> str:
    """A ``documents`` series of one full file, its record and file on S3; the head's file id."""
    base = seed_documents(root)
    upload_all(root, fake)
    store_index(root, refresh=True)
    return base.file_id


def running_rec(run_id: str = "r0000000000a1") -> RunRecord:
    at = dt.datetime.now(dt.UTC)
    return RunRecord(run_id=run_id, stage="echo_test", writer="echo_test", plan="dev-v2", status="running",
                     started_at=at, heartbeat_at=at, code_digest="c" * 64, config_digest="none")


# ---------------------------------------------------------------- order: start, tick, end
def test_tick_and_end_order(s3: tuple[Path, FakeS3]) -> None:
    """Start: json, log, marker; each tick the same; the end: json, log, then the marker DELETE
    (the only runtime delete); a tick after the end writes nothing."""
    root, fake = s3
    rec = running_rec()
    job = RunJob(root, rec)
    jk, lk, mk = job_keys(root, rec)
    n0 = len(fake.calls)
    job.start()
    J.say("a line of the run")
    assert job.tick()
    with job.lock:
        rec.status, rec.finished_at = "ok", dt.datetime.now(dt.UTC)
    assert job.finish_record() == []
    assert writes(fake, n0) == [("put", jk), ("put", lk), ("put", mk),
                                ("put", jk), ("put", lk), ("put", mk),
                                ("put", jk), ("put", lk), ("delete", mk)]
    assert remote_json(fake, jk)["status"] == "ok" and remote_json(fake, jk)["upload"] is True
    log = zstandard.ZstdDecompressor().decompress(fake.objects[(BUCKET, f"{PREFIX}/{lk}")])
    assert b"a line of the run" in log
    assert not (root / mk).exists()
    n1 = len(fake.calls)
    assert job.tick() and writes(fake, n1) == []  # never after the final json


class _BlockingS3(FakeS3):
    """Holds a PUT of a ``running`` json (a tick in flight) until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__(page=50)
        self.armed = False
        self.blocked = threading.Event()
        self.release = threading.Event()

    def put_object(self, *, Bucket: str, Key: str, Body: Any) -> dict[str, Any]:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        if self.armed and Key.endswith(".json.zst"):
            if json.loads(zstandard.ZstdDecompressor().decompress(data)).get("status") == "running":
                self.armed = False
                self.blocked.set()
                self.release.wait(10)
        return super().put_object(Bucket=Bucket, Key=Key, Body=data)


def test_late_tick_cannot_replace_the_final_json(tmp_path: Path, fake_env: None,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick whose json PUT is still in flight when the run ends: the end joins the tick thread
    before writing the final json, so S3 ends with the final json, not the late ``running`` one."""
    fake = _BlockingS3()
    monkeypatch.setattr(J, "TICK", dt.timedelta(milliseconds=20))
    with using_s3_client(fake):
        root = resolve_store(URI, mirror_base=tmp_path / "mirror")
        rec = running_rec()
        job = RunJob(root, rec)
        jk, _, _ = job_keys(root, rec)
        job.start()
        fake.armed = True
        job.start_tick()
        assert fake.blocked.wait(10)
        with job.lock:
            rec.status, rec.finished_at = "failed", dt.datetime.now(dt.UTC)
            rec.error_class, rec.error = "code_bug", "x"
        ticker = job._thread
        assert ticker is not None
        t = threading.Thread(target=job.finish_record)
        t.start()
        t.join(0.3)
        fake.release.set()
        t.join(10)
        ticker.join(10)  # the late tick's PUT has landed (or was joined before the final json)
        assert remote_json(fake, jk)["status"] == "failed"


def test_stale_marker_is_dead(s3: tuple[Path, FakeS3], tmp_path: Path) -> None:
    """A job that stopped ticking (a crash) keeps its ``running`` json and marker; once the
    marker is older than DEAD_AFTER on both sides the index reads it ``dead``; fresh it is ``live``."""
    root, fake = s3
    rec = running_rec()
    job = RunJob(root, rec)
    job.start()
    job.abandon()  # the process "dies": no final record
    assert store_index(root, refresh=True).run(rec.run_id).liveness == "live"
    old = dt.datetime.now(dt.UTC) - DEAD_AFTER - dt.timedelta(minutes=1)
    _, _, mk = job_keys(root, rec)
    fake.mtimes[(BUCKET, f"{PREFIX}/{mk}")] = old
    os.utime(root / mk, (old.timestamp(), old.timestamp()))
    assert store_index(root, refresh=True).run(rec.run_id).liveness == "dead"


# ---------------------------------------------------------------- a run on S3, end to end
def test_run_uploads_in_publish_order(s3: tuple[Path, FakeS3], plan: Any) -> None:  # noqa: F811
    """echo_test on an S3 store: data PUT before the final json, the final json before its log,
    the marker deleted last; S3 then holds the new head and the ok record."""
    root, fake = s3
    seeded(root, fake)
    n0 = len(fake.calls)
    rec = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert rec.status == "ok" and rec.upload is True
    jk, lk, mk = job_keys(root, rec)
    data = rec.outputs["documents"]["path"]
    w = writes(fake, n0)
    assert w[:3] == [("put", jk), ("put", lk), ("put", mk)]
    assert w[-4:] == [("put", data), ("put", jk), ("put", lk), ("delete", mk)]
    assert remote_json(fake, jk)["status"] == "ok" and has(fake, data) and not has(fake, mk)


def test_head_moved_after_the_pin_is_refused_and_nothing_goes_up(
        s3: tuple[Path, FakeS3], plan: Any, stage_dir: Path) -> None:  # noqa: F811
    """Another writer publishes on the series (on S3) after this run pinned its base: the publish's
    publish rule (:func:`stack.store.chain.claim`) over a refreshed index refuses (``failed[head_moved]``), no data file of the run
    is renamed or PUT, and the other writer's head stands."""
    root, fake = s3
    base = seeded(root, fake)
    toy_stage(stage_dir, "racer", f"""
        from stack.stage_runner import StageResult
        from tests.test_stage_runner import seed_documents
        from stack.store.remote import client_override
        from tests.test_store_job import upload_all
        def run(ctx):
            ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")
            seed_documents(ctx.store_root, run=7, part_of={base!r})  # another writer, elsewhere
            upload_all(ctx.store_root, client_override())
            return StageResult()
    """)
    rec = run_stage(root, plan, "racer", [DOCS], kind="part")
    assert (rec.status, rec.error_class, rec.outputs) == ("failed", "head_moved", {}), rec.error
    assert not any(k.startswith("data/") and rec.run_id in k for _, k in writes(fake))
    jk, _, _ = job_keys(root, rec)
    assert remote_json(fake, jk)["status"] == "failed"
    other = store_index(root, refresh=True)
    new = last_file(root, DOCS)
    assert new is not None and new.file_id != base and other.output(new.file_id) is not None


# ---------------------------------------------------------------- self-cancel and recovery
def test_self_cancel_then_recovery(s3: tuple[Path, FakeS3], plan: Any, stage_dir: Path,  # noqa: F811
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """PUTs fail after the start and the job's clock moves a minute per tick: after 2.5 min of
    failing ticks the run ends ``cancelled[tick_failed]`` where it reports progress, publishes
    nothing, and its final json stays local (its PUT fails too). The next uploading job's start
    pushes it (recovery): S3 then holds the cancelled json and no marker."""
    root, fake = s3
    seeded(root, fake)
    toy_stage(stage_dir, "spin", """
        import time
        from stack.stage_runner import StageResult
        def run(ctx):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ctx.heartbeat()
                time.sleep(0.005)
            ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")
            return StageResult()
    """)
    t0 = dt.datetime.now(dt.UTC)
    ticks = {"n": 0}

    def clock() -> dt.datetime:  # each call from a tick thread: a minute on, and PUTs failing
        if threading.current_thread().name.startswith("tick-"):
            ticks["n"] += 1
            fake.fail_ops.add("PutObject")
        return t0 + dt.timedelta(minutes=ticks["n"])

    utc, tick = J._utc, J.TICK
    monkeypatch.setattr(J, "TICK", dt.timedelta(milliseconds=10))
    monkeypatch.setattr(J, "_utc", clock)
    n0 = len(fake.calls)
    rec = run_stage(root, plan, "spin", [DOCS], kind="part")
    monkeypatch.setattr(J, "_utc", utc)
    monkeypatch.setattr(J, "TICK", tick)
    assert (rec.status, rec.error_class) == ("cancelled", "tick_failed"), rec.error
    assert rec.outputs == {} and rec.counts.get("tick_errors", 0) >= 2
    assert not any(k.startswith("data/") for op, k in writes(fake, n0) if op == "put")
    assert not any(rec.run_id in p.name for p in (root / "data").rglob("*.parquet"))
    jk, lk, mk = job_keys(root, rec)
    assert remote_json(fake, jk)["status"] == "running" and has(fake, mk)  # S3 lags: the final PUT failed
    assert check_push(root) != []

    fake.fail_ops.clear()
    n1 = len(fake.calls)
    again = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert again.status == "ok"
    w = writes(fake, n1)
    assert w[:3] == [("put", jk), ("put", lk), ("delete", mk)]  # recovery, before the new run's start
    assert remote_json(fake, jk)["status"] == "cancelled" and not has(fake, mk)
    assert check_push(root) == []


class _Ticker:
    """A bare :class:`J.Job` on a local store with an injected clock and a switch that makes its ticks fail
    (the log write raises, as a full disk or an S3 outage would)."""

    def __init__(self, tmp_path: Path, t0: dt.datetime) -> None:
        from stack.store.remote import open_store

        root = tmp_path / "store"
        root.mkdir()
        self.now, self.failing = t0, False
        self.job = J.Job(open_store(root), json_path=root / "j.json.zst", log_path=root / "j.log.zst",
                         marker_path=root / "j-running.json", upload=False,
                         snapshot=lambda: {"status": "running"}, label="test", clock=lambda: self.now)
        write_log = self.job._write_log

        def flaky() -> None:
            if self.failing:
                raise OSError(28, "No space left on device")
            write_log()

        self.job._write_log = flaky  # type: ignore[method-assign]
        self.job.start()

    def tick(self, at_min: float, *, ok: bool) -> bool:
        self.now = self.t0 + dt.timedelta(minutes=at_min)
        self.failing = not ok
        return self.job.tick()

    @property
    def t0(self) -> dt.datetime:
        return T0


T0 = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def ticker(tmp_path: Path) -> Iterator[_Ticker]:
    t = _Ticker(tmp_path, T0)
    yield t
    t.job.abandon()  # leave the active-job stack: a later publish must not see this job's cancel


def test_one_failed_tick_after_a_suspend_does_not_cancel(ticker: _Ticker) -> None:
    """S25-12 repro (D493): a good tick, a 20-min suspend, one failed tick, then a good one. The failing time
    counts from the first failed tick (not the last good one), so nothing is cancelled."""
    t = ticker
    assert t.tick(0.5, ok=True)
    assert not t.tick(20.5, ok=False) and not t.job.cancel.is_set()
    assert t.tick(21.0, ok=True)
    t.job.check_cancel()  # does not raise
    assert t.job.tick_failures == 1


def test_an_outage_that_recovers_does_not_cancel(ticker: _Ticker) -> None:
    """A 2.5-min outage (ticks failing at 0.5-2.5 min after its start, the next one good) never fails for
    SELF_CANCEL_AFTER from its first failed tick; and a good tick clears a flag no check has raised yet."""
    t = ticker
    for m in (1.0, 1.5, 2.0, 2.5, 3.0):
        assert not t.tick(m, ok=False)
    assert not t.job.cancel.is_set()
    assert t.tick(3.5, ok=True)
    t.job.check_cancel()
    # a streak that reaches the bound sets the flag; a good tick before anything raised it clears it
    for m in (5.0, 6.0, 7.0, 7.5):
        t.tick(m, ok=False)
    assert t.job.cancel.is_set()
    assert t.tick(8.0, ok=True) and not t.job.cancel.is_set() and t.job.cancel_reason is None
    t.job.check_cancel()


def test_continuous_failure_past_the_bound_cancels_for_good(ticker: _Ticker) -> None:
    t = ticker
    assert J.SELF_CANCEL_AFTER == dt.timedelta(minutes=2.5)
    for m in (1.0, 1.5, 2.0, 2.5, 3.0):
        t.tick(m, ok=False)
    assert not t.job.cancel.is_set()  # 2.0 min since the first failure
    t.tick(3.5, ok=False)
    assert t.job.cancel.is_set() and "failing continuously since 2026-10-14T12:01:00" in (t.job.cancel_reason or "")
    with pytest.raises(J.JobCancelled):
        t.job.check_cancel()
    assert t.tick(4.0, ok=True) and t.job.cancel.is_set()  # raised: the cancel stands
    with pytest.raises(J.JobCancelled):
        t.job.check_cancel()


def test_s3_errors_reach_the_record_redacted(s3: tuple[Path, FakeS3], plan: Any,  # noqa: F811
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """S25-93: a botocore message can name the endpoint URL, bucket and key; a job's start-upload
    failure (the run record's ``error``) and its final-upload warning carry the class only
    (``redact_error``)."""
    from botocore.exceptions import EndpointConnectionError

    root, fake = s3
    seeded(root, fake)
    url = f"https://s3.secret-endpoint.example/{BUCKET}/{PREFIX}/runs/x.json.zst"
    fake._error = lambda code, op: EndpointConnectionError(endpoint_url=url)  # type: ignore[method-assign]
    fake.fail_ops.add("PutObject")
    rec = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("failed", "tick_failed"), rec.error
    assert "start upload failed: EndpointConnectionError" in str(rec.error) and url not in str(rec.error)
    err = capsys.readouterr().err
    assert "final upload failed (EndpointConnectionError)" in err and "secret-endpoint" not in err, err


# ---------------------------------------------------------------- S25-47: S3 outages
def _fail_at_publish(monkeypatch: pytest.MonkeyPatch, fake: FakeS3, n: int,
                     when: Any = lambda: True) -> list[float]:
    """Fail the next ``n`` PutObject calls when a run publishes (the data PUT; ``when()`` picks the
    run); the chain's retry waits are recorded, not slept."""
    from stack import stage_runner as SR
    from stack.store import chain as C

    real = SR.publish
    slept: list[float] = []

    def publish(staged: Any, on_published: Any = None) -> Any:
        if when():
            fake.fail_next("PutObject", n)
        return real(staged, on_published)

    monkeypatch.setattr(SR, "publish", publish)
    monkeypatch.setattr(C, "_sleep", slept.append)
    return slept


def test_a_transient_data_put_failure_is_retried(s3: tuple[Path, FakeS3], plan: Any,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-47: the publish's data PUT failing twice (each a failure past the client's own retries)
    is repeated after the chain's waits; the run ends ``ok`` with its data and record on S3."""
    from stack.store.chain import DATA_PUT_RETRY_DELAYS

    root, fake = s3
    seeded(root, fake)
    slept = _fail_at_publish(monkeypatch, fake, 2)
    rec = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("ok", None), rec.error
    assert slept == list(DATA_PUT_RETRY_DELAYS[:2])
    assert has(fake, rec.outputs["documents"]["path"]) and remote_json(fake, job_keys(root, rec)[0])["status"] == "ok"


def test_a_lasting_data_put_failure_ends_the_run_s3_transient(s3: tuple[Path, FakeS3], plan: Any,  # noqa: F811
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """S25-47: a data PUT failing past every repeat ends the run ``failed[s3_transient]`` (not
    ``code_bug``), its error redacted (the endpoint URL of the botocore message is not in it) and
    no ``ok`` record; S3 holds the failed record."""
    from botocore.exceptions import EndpointConnectionError

    from stack.store.chain import DATA_PUT_RETRY_DELAYS

    root, fake = s3
    seeded(root, fake)
    url = f"https://s3.secret-endpoint.example/{BUCKET}/{PREFIX}/data/x.parquet"
    fake._error = lambda code, op: EndpointConnectionError(endpoint_url=url)  # type: ignore[method-assign]
    slept = _fail_at_publish(monkeypatch, fake, len(DATA_PUT_RETRY_DELAYS) + 1)
    rec = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert (rec.status, rec.error_class, rec.outputs) == ("failed", "s3_transient", {}), rec.error
    assert slept == list(DATA_PUT_RETRY_DELAYS)
    assert "EndpointConnectionError" in str(rec.error) and "secret-endpoint" not in str(rec.error), rec.error
    assert remote_json(fake, job_keys(root, rec)[0])["error_class"] == "s3_transient"


def test_a_transient_failure_at_a_runs_prepare_raises_before_any_record(
        s3: tuple[Path, FakeS3], plan: Any) -> None:  # noqa: F811
    """S25-47: the run's prepare LIST failing (past the client's retries) raises
    ``S3TransientError`` (redacted) before any record exists; nothing goes up."""
    from stack.store.s3config import S3TransientError

    root, fake = s3
    seeded(root, fake)
    n0 = len(fake.calls)
    fake.fail_next("ListObjectsV2", 1)
    with pytest.raises(S3TransientError, match=r"prepare .*ClientError\[InternalError\]"):
        run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert writes(fake, n0) == [] and not list((root / "runs").glob("*.echo_test.json.zst"))


@pytest.mark.parametrize("where", ["prepare", "publish"])
def test_an_s3_outage_in_a_builds_run_ends_it_s3_transient(s3: tuple[Path, FakeS3],
                                                           monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """S25-47 (L2O-3's repro): an S3 failure at the second run's prepare LIST, or at its data PUT
    past every repeat, ends the build ``failed[s3_transient]`` (was ``failed[code_bug]`` with a
    traceback); the first run's work stands, ``ok`` on S3."""
    import stack.build as B
    from stack.build import run_build
    from stack.store.chain import DATA_PUT_RETRY_DELAYS
    from tests.synth.world import build_world
    from tests.test_build_overrides import synth_plan

    root, fake = s3
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(REPO / "tests" / "synth" / "stages"))
    build_world(root)
    upload_all(root, fake)
    n = {"runs": 0}
    real_run_stage = B.run_stage

    def run_stage_(*a: Any, **k: Any) -> Any:
        n["runs"] += 1
        if n["runs"] == 2 and where == "prepare":
            fake.fail_next("ListObjectsV2", 1)
        return real_run_stage(*a, **k)

    monkeypatch.setattr(B, "run_stage", run_stage_)
    _fail_at_publish(monkeypatch, fake, len(DATA_PUT_RETRY_DELAYS) + 1, when=lambda: n["runs"] == 2)
    b = run_build(root, synth_plan("synth-a"))
    assert (b.status, b.data.get("error_class")) == ("failed", "s3_transient"), b.data.get("error")
    assert "Traceback" not in str(b.data.get("error"))
    idx = store_index(root, refresh=True)
    recs = {e.record.run_id: e for e in idx.runs}
    e0 = recs[b.data["runs"][0]]
    assert e0.record.status == "ok" and e0.uploaded
    if where == "publish":
        assert recs[b.data["runs"][1]].record.error_class == "s3_transient"
    else:
        assert len(b.data["runs"]) == 1 and "prepare" in str(b.data.get("error"))


def test_publish_checks_the_cancel_flag_before_claim(s3: tuple[Path, FakeS3], plan: Any,  # noqa: F811
                                                     stage_dir: Path) -> None:
    """A cancel flag set while the stage ran (no progress call after it) still stops the publish
    before ``claim`` (the publish rule): ``cancelled[tick_failed]``, nothing renamed."""
    root, fake = s3
    seeded(root, fake)
    toy_stage(stage_dir, "flagger", """
        from stack.stage_runner import StageResult
        from stack.store.job import current_job
        def run(ctx):
            ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")
            j = current_job()
            j.cancel_reason = "test: ticks gave up"
            j.cancel.set()
            return StageResult()
    """)
    rec = run_stage(root, plan, "flagger", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("cancelled", "tick_failed")
    assert files_without_ok_record(root) == []
    assert len(list((root / DOCS).glob(f".*{rec.run_id}*.tmp"))) == 1  # staged, never renamed


# ---------------------------------------------------------------- no-upload, push, push --check
def test_no_upload_job_then_push(s3: tuple[Path, FakeS3], plan: Any) -> None:  # noqa: F811
    """A no-upload run sends no record, log, marker or data PUT; ``push --check`` lists it (exit
    1); ``push`` uploads it in publish order; then the check is clean (exit 0)."""
    root, fake = s3
    seeded(root, fake)
    n0 = len(fake.calls)
    rec = run_stage(root, plan, "echo_test", [DOCS], upload=False, kind="part")
    assert rec.status == "ok" and rec.upload is False
    assert writes(fake, n0) == []
    jk, lk, mk = job_keys(root, rec)
    res = runner.invoke(app, ["--store", URI, "store", "push", "--check"])
    assert res.exit_code == 1 and jk in res.output and "no-upload" in res.output
    n1 = len(fake.calls)
    res = runner.invoke(app, ["--store", URI, "store", "push", rec.run_id])
    assert res.exit_code == 0, res.output
    data = rec.outputs["documents"]["path"]
    assert writes(fake, n1) == [("put", data), ("put", jk), ("put", lk), ("delete", mk)]
    res = runner.invoke(app, ["--store", URI, "store", "push", "--check"])
    assert res.exit_code == 0 and "nothing local is missing" in res.output, res.output


def test_push_refuses_inputs_not_on_s3_and_a_moved_base(s3: tuple[Path, FakeS3], plan: Any) -> None:  # noqa: F811
    """``push`` refuses a run whose input no ok record on S3 names, and a run whose base is no
    longer its series' last file on S3 (the publish rule against the uploaded records,
    :func:`stack.store.job.recheck_outputs`); nothing of the
    refused run goes up."""
    root, fake = s3
    base = seed_documents(root)  # local only: not on S3
    a = run_stage(root, plan, "echo_test", [DOCS], upload=False, kind="part")
    assert a.status == "ok"
    n0 = len(fake.calls)
    with pytest.raises(PushError, match="not named by ok records on S3"):
        push_job(root, a.run_id)
    assert writes(fake, n0) == []
    upload_all_but(root, fake, a)  # the seed goes up (another machine finished it)
    store_index(root, refresh=True, uploaded_only=True)
    b = run_stage(root, plan, "echo_test", [DOCS], kind="part")  # uploads, on the base
    assert b.status == "ok" and b.inputs["documents"] == base.file_id
    n1 = len(fake.calls)
    with pytest.raises(PushError, match="moved on S3"):  # the publish rule again, on the uploaded records
        push_job(root, a.run_id)
    assert not any(a.run_id in k for _, k in writes(fake, n1))


@pytest.mark.slow
def test_push_a_build_by_its_id(s3: tuple[Path, FakeS3], monkeypatch: pytest.MonkeyPatch) -> None:
    """``stack store push <build_id>`` pushes a no-upload build with its runs (S25-53: every run
    record carries the build id, so the id used to name them all and was refused); then
    ``push --check`` is clean. A run id still names its run only."""
    from stack.build import BuildOverrides, run_build
    from tests.synth.world import build_world
    from tests.test_build_overrides import synth_plan

    root, fake = s3
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(REPO / "tests" / "synth" / "stages"))
    build_world(root)
    upload_all(root, fake)
    b = run_build(root, synth_plan("synth-a"), upload=False,
                  overrides=BuildOverrides(skip=("acquire", "fold.*", "annotate.*")))
    assert b.status == "ok" and len(b.data["runs"]) > 1
    assert J._find_job(root, b.data["runs"][0])[1]["run_id"] == b.data["runs"][0]
    pushed = push_job(root, b.build_id)
    assert len(pushed) == len(b.data["runs"]) + 1 and pushed[-1].endswith("/build.json.zst")
    assert check_push(root) == []


def test_push_check_on_a_local_only_store_is_refused(tmp_path: Path) -> None:
    """S25-99: ``push --check`` on a local-only store listed every record as "S3 absent"; there is no
    S3 to compare with, so it is refused like ``push``."""
    root = tmp_path / "store"
    seed_documents(root)
    res = runner.invoke(app, ["--store", str(root), "store", "push", "--check"])
    assert res.exit_code == 1 and "is a local-only store" in res.output and "S3 absent" not in res.output, res.output
    with pytest.raises(PushError, match="local-only store"):
        check_push(root)


def upload_all_but(root: Path, fake: FakeS3, rec: RunRecord) -> None:
    for p in sorted(root.rglob("*")):
        if (p.is_file() and p.name != MIRROR_FILE and not p.name.startswith(".") and rec.run_id not in p.name
                and "cache" not in p.parts):
            fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(root).as_posix()}", Body=p.read_bytes())


# ---------------------------------------------------------------- per-run upload in a build
def test_build_uploads_each_run_although_a_later_stage_fails(s3: tuple[Path, FakeS3],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthetic build on an S3 store with acquire failing: the collect and gather runs that
    finished before are on S3 (data files and final ok jsons, no markers) although the build
    failed; the build's final json and log are on S3 too, with ``upload: true``."""
    from stack.build import run_build
    from tests.synth.world import build_world

    root, fake = s3
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(REPO / "tests" / "synth" / "stages"))
    build_world(root)
    upload_all(root, fake)
    data = yaml.safe_load((REPO / "tests" / "synth" / "plans" / "synth-m5.yaml").read_text("utf-8"))
    for line in data["stages"]:
        if line["stage"] == "acquire":
            line["options"] = {"fail": True}
    b = run_build(root, Plan.model_validate(data))
    assert b.status == "failed" and b.data["upload"] is True
    idx = store_index(root, refresh=True)
    runs = {e.record.stage: e for e in idx.runs if e.record.build_id == b.build_id}
    for stage in ("collect.arxiv", "gather"):
        e = runs[stage]
        assert e.record.status == "ok" and e.uploaded
        assert remote_json(fake, e.key)["status"] == "ok"
        assert not has(fake, J.marker_key(e.key))
        for out in e.record.outputs.values():
            assert has(fake, out["path"]), out["path"]
    assert runs["acquire"].record.status == "failed"
    bk = idx.build(b.build_id).key
    assert remote_json(fake, bk)["status"] == "failed"
    assert has(fake, J.log_key(bk)) and not has(fake, J.marker_key(bk))
    log = zstandard.ZstdDecompressor().decompress(fake.objects[(BUCKET, f"{PREFIX}/{J.log_key(bk)}")])
    assert b"gather run" in log and b"acquire run" in log


# ---------------------------------------------------------------- crashes (subprocess)
CRASH_SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from tests.fakes.s3 import FakeS3
from stack.store.remote import using_s3_client
from stack.cli import main
with using_s3_client(FakeS3(root=Path({bucket_dir!r}), page=50)):
    sys.argv = ["stack", *sys.argv[1:]]
    main()
"""


def crash_run(tmp_path: Path, point: str | None, stage: str = "echo_test",
              env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "crash_stack.py"
    script.write_text(textwrap.dedent(CRASH_SCRIPT.format(repo=str(REPO), bucket_dir=str(tmp_path / "bucket"))))
    env = {k: v for k, v in os.environ.items() if k != "STACK_CRASH_AT"}
    env.update(FAKE_ENV)
    env["STACK_MIRROR"] = str(tmp_path / "mirror")
    env.pop("STACK_ENV_FILE", None)
    if point is not None:
        env["STACK_CRASH_AT"] = point
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(script), "run", stage, "--in", DOCS, "--part",
                           "--store", URI, "--plan", str(REPO / "configs" / "plans" / "dev-v2.yaml")],
                          check=False, cwd=REPO, env=env, capture_output=True, text=True, timeout=180)


@pytest.mark.slow
@pytest.mark.parametrize("point", ["after_rename", "after_data_put", "after_final_local"])
def test_crash_points(tmp_path: Path, fake_env: None, point: str) -> None:
    """SIGKILL at a point of the run's end, then a fresh process on the same bucket and mirror.

    * ``after_rename``: the part is renamed locally, not on S3; the record is ``running`` on both
      sides with a marker: the index reads the run live (dead after 5 min), the head is unchanged
      and the next run publishes on it; the stray part is local garbage.
    * ``after_data_put``: the part is on S3 too, named by no ok record: invisible, garbage.
    * ``after_final_local``: the ok record is final locally, S3 holds the ``running`` json and the
      marker; the next run's start pushes it (recovery), so its part is the head the next run
      extends, and nothing is lost."""
    fake = FakeS3(root=tmp_path / "bucket", page=50)
    with using_s3_client(fake):
        root = resolve_store(URI, mirror_base=tmp_path / "mirror")
        base = seeded(root, fake)
        base_name = head_of(root, DOCS)
    proc = crash_run(tmp_path, point)
    assert proc.returncode == -9, proc.stderr[-2000:]
    with using_s3_client(fake):
        idx = store_index(root, refresh=True, uploaded_only=True)
        crashed = [e for e in idx.runs if e.record.stage == "echo_test"]
        assert len(crashed) == 1
        c = crashed[0]
        local_status = J._status_at(root / c.key)
        mk = J.marker_key(c.key)
        assert remote_json(fake, c.key)["status"] == "running" and has(fake, mk)
        part = [p for p in (root / "data").rglob(f"*{c.run_id}*.parquet")]
        assert len(part) == 1
        on_s3 = has(fake, part[0].relative_to(root).as_posix())
        if point == "after_rename":
            assert local_status == "running" and not on_s3
        elif point == "after_data_put":
            assert local_status == "running" and on_s3
        else:
            assert local_status == "ok" and on_s3
        if point != "after_final_local":
            assert head_of(root, DOCS) == base_name and c.liveness == "live"

    proc = crash_run(tmp_path, None)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    with using_s3_client(fake):
        idx = store_index(root, refresh=True, uploaded_only=True)
        rerun = idx.run(out["run_id"])
        assert rerun is not None and rerun.record.status == "ok"
        crashed_now = idx.run(c.run_id)
        if point == "after_final_local":
            assert remote_json(fake, c.key)["status"] == "ok" and not has(fake, mk)  # recovered
            assert rerun.record.inputs["documents"] == c.record.outputs["documents"]["file_id"]
        else:
            assert crashed_now is not None and crashed_now.record.status == "running"
            assert rerun.record.inputs["documents"] == base  # the crashed part is in no series
        assert head_of(root, DOCS) == Path(rerun.record.outputs["documents"]["path"]).name



@pytest.mark.slow
def test_cli_captures_fd2_into_the_run_log(tmp_path: Path, fake_env: None) -> None:
    """``stack run`` (the console entry point, ``stack.cli.main``): what a child process and a raw
    ``os.write(2, …)`` print during the run is in the run's log on S3, and still reaches stderr."""
    fake = FakeS3(root=tmp_path / "bucket", page=50)
    with using_s3_client(fake):
        root = resolve_store(URI, mirror_base=tmp_path / "mirror")
        seeded(root, fake)
    wf = tmp_path / "workflows"
    toy_stage(wf, "noisy", """
        import os, subprocess, sys
        from stack.stage_runner import StageResult
        def run(ctx):
            subprocess.run([sys.executable, "-c", "import sys; print('CHILD-STDERR-LINE', file=sys.stderr)"],
                           check=True)
            os.write(2, b"RAW-FD2-LINE" + bytes([10]))
            return StageResult()
    """)
    proc = crash_run(tmp_path, None, stage="noisy", env_extra={WORKFLOWS_ROOT_ENV: str(wf)})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "CHILD-STDERR-LINE" in proc.stderr and "RAW-FD2-LINE" in proc.stderr
    run_id = json.loads(proc.stdout.strip().splitlines()[-1])["run_id"]
    with using_s3_client(fake):
        e = store_index(root, refresh=True).run(run_id)
        log = zstandard.ZstdDecompressor().decompress(fake.objects[(BUCKET, f"{PREFIX}/{J.log_key(e.key)}")])
    assert b"CHILD-STDERR-LINE" in log and b"RAW-FD2-LINE" in log


DRAIN_SCRIPT = """
import datetime as dt, os, sys, time
sys.path.insert(0, {repo!r})
from pathlib import Path
import zstandard
from stack.runs import RunRecord
from stack.store import job as J

slow = J._Capture._read_one
def slow_read(self):
    time.sleep(0.3)  # a slow reader: the line is still in the pipe when the run ends
    return slow(self)
J._Capture._read_one = slow_read
at = dt.datetime.now(dt.UTC)
rec = RunRecord(run_id="r0000000000a1", stage="echo_test", writer="echo_test", plan="dev-v2", status="running",
                started_at=at, heartbeat_at=at, code_digest="c" * 64, config_digest="none")
with J.capture_stderr():
    job = J.RunJob(Path({root!r}), rec)
    job.start()
    os.write(2, b"LAST-LINE-BEFORE-THE-END" + bytes([10]))
    rec.status, rec.finished_at = "ok", dt.datetime.now(dt.UTC)
    job.finish_record()
print(zstandard.ZstdDecompressor().decompress(job.log_path.read_bytes()).decode())
"""


def test_the_final_log_is_written_after_the_capture_drained(tmp_path: Path) -> None:
    """A line written to fd 2 just before a run ends is in its final log although the capture's
    reader is slow: ``finish`` drains the pipe first (a subprocess: the capture owns fd 2)."""
    script = tmp_path / "drain.py"
    script.write_text(DRAIN_SCRIPT.format(repo=str(REPO), root=str(tmp_path / "store")))
    proc = subprocess.run([sys.executable, str(script)], cwd=REPO, capture_output=True, text=True, timeout=60,
                          check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "LAST-LINE-BEFORE-THE-END" in proc.stdout and "LAST-LINE-BEFORE-THE-END" in proc.stderr


# ---------------------------------------------------------------- a run's blob uploads (L3-4, D407)
#: Released by the ``minter`` stage after its puts returned; a :class:`SlowBlobS3` blob PUT waits on it.
BLOB_GATE = threading.Event()
#: What the ``minter`` stage measured: ``{"put_s": <seconds of its puts>, "released_after": bool}``.
MINTED: dict[str, Any] = {}
N_BLOBS = 12
BLOCK_S = 0.5


class SlowBlobS3(FakeS3):
    """A blob HEAD sleeps 5 ms; a blob PUT waits for :data:`BLOB_GATE` (at most :data:`BLOCK_S`),
    then sleeps 5 ms, or raises when ``fail_blobs`` (or for the next ``fail_blob_puts`` blob PUTs),
    with ``blob_code``. Other keys are as in :class:`FakeS3`."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.fail_blobs = False
        self.fail_blob_puts = 0
        self.blob_code = "InternalError"

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if "/blobs/" in Key:
            time.sleep(0.005)
        return super().head_object(Bucket=Bucket, Key=Key)

    def put_object(self, *, Bucket: str, Key: str, Body: Any, IfNoneMatch: str | None = None) -> dict[str, Any]:  # noqa: N803
        if "/blobs/" in Key:
            BLOB_GATE.wait(BLOCK_S)
            time.sleep(0.005)
            with self._lock:
                fail = self.fail_blobs or self.fail_blob_puts > 0
                self.fail_blob_puts -= 1 if self.fail_blob_puts > 0 else 0
            if fail:
                self._log("put", Key)
                raise self._error(self.blob_code, "PutObject")
        return super().put_object(Bucket=Bucket, Key=Key, Body=Body, IfNoneMatch=IfNoneMatch)


MINTER = f"""
    import time
    from stack.stage_runner import StageResult
    import sys
    T = sys.modules[{__name__!r}]  # this test module, as pytest imported it
    def run(ctx):
        t0 = time.monotonic()
        for i in range({N_BLOBS}):
            ctx.blobs.put(f"minted blob {{i}}".encode())
        T.MINTED["put_s"] = time.monotonic() - t0
        T.MINTED["released_after"] = not T.BLOB_GATE.is_set()
        T.BLOB_GATE.set()
        assert ctx.blobs.get(ctx.blobs.put(b"minted blob 0")) == b"minted blob 0"  # the local copy serves the run
        if T.MINTED.get("die"):
            raise RuntimeError("the stage died after minting")
        ctx.emit_part("documents", ctx.view("documents").slice(0, 2), "*")
        return StageResult()
"""


def minted_keys() -> set[str]:
    import hashlib
    return {f"{PREFIX}/blobs/{hashlib.sha256(f'minted blob {i}'.encode()).hexdigest()}" for i in range(N_BLOBS)}


@pytest.fixture
def slow_s3(tmp_path: Path, fake_env: None) -> Iterator[tuple[Path, SlowBlobS3]]:
    BLOB_GATE.clear()
    MINTED.clear()
    fake = SlowBlobS3(page=50)
    with using_s3_client(fake):
        yield resolve_store(URI, mirror_base=tmp_path / "mirror"), fake
    BLOB_GATE.set()  # never leave a worker waiting


def test_a_runs_blob_puts_do_not_wait_on_the_remote(slow_s3: tuple[Path, SlowBlobS3], plan: Any,  # noqa: F811
                                                    stage_dir: Path) -> None:
    """(a) A stage minting N blobs on an S3 store whose blob PUTs block until released: every
    ``ctx.blobs.put`` returns before the release (the synchronous path blocks N x the PUT), and the
    run still publishes ok."""
    root, fake = slow_s3
    seeded(root, fake)
    toy_stage(stage_dir, "minter", MINTER)
    rec = run_stage(root, plan, "minter", [DOCS], kind="part")
    assert MINTED["released_after"], "a put returned only after the remote PUT was released"
    assert MINTED["put_s"] < BLOCK_S / 2, MINTED
    assert rec.status == "ok", rec.error


def test_b_minted_blobs_go_up_before_the_data_and_the_record(slow_s3: tuple[Path, SlowBlobS3], plan: Any,  # noqa: F811
                                                             stage_dir: Path) -> None:
    """(b) After the run publishes, every minted blob is on S3, and every blob PUT precedes the data
    PUT, which precedes the final json PUT (blobs, data, record)."""
    root, fake = slow_s3
    seeded(root, fake)
    toy_stage(stage_dir, "minter", MINTER)
    n0 = len(fake.calls)
    rec = run_stage(root, plan, "minter", [DOCS], kind="part")
    assert rec.status == "ok", rec.error
    want = minted_keys()
    assert want <= {k for (_, k) in fake.objects}
    calls = fake.calls[n0:]
    blob_puts = [i for i, (op, k) in enumerate(calls) if op == "put" and k in want]
    data = f"{PREFIX}/{rec.outputs['documents']['path']}"
    jk, _, _ = job_keys(root, rec)
    data_put = calls.index(("put", data))
    final_put = max(i for i, c in enumerate(calls) if c == ("put", f"{PREFIX}/{jk}"))
    assert len(blob_puts) == N_BLOBS and max(blob_puts) < data_put < final_put


def test_c_a_failed_blob_upload_fails_the_run_and_publishes_nothing(
        slow_s3: tuple[Path, SlowBlobS3], plan: Any, stage_dir: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) A blob PUT that fails past every repeat (D789) fails the run (``failed[s3_transient]`` for
    the fake's ``InternalError``, S25-47; ``code_bug`` before); no data file of the run is renamed or PUT."""
    from stack.store import blobs as B

    monkeypatch.setattr(B, "_sleep", lambda _s: None)
    root, fake = slow_s3
    seeded(root, fake)
    fake.fail_blobs = True
    toy_stage(stage_dir, "minter", MINTER)
    rec = run_stage(root, plan, "minter", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("failed", "s3_transient"), rec.error
    assert "InternalError" in (rec.error or "")
    assert not any(k.startswith("data/") and rec.run_id in k for _, k in writes(fake))
    assert list((root / "data").rglob(f"*{rec.run_id}*.parquet")) == []
    assert files_without_ok_record(root) == []
    jk, _, _ = job_keys(root, rec)
    assert remote_json(fake, jk)["status"] == "failed"


@pytest.mark.parametrize("code", ["SlowDown", "InternalError"])
def test_e_throttled_blob_uploads_are_repeated_and_the_run_publishes(
        slow_s3: tuple[Path, SlowBlobS3], plan: Any, stage_dir: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """(e) D789: blob PUTs answered ``SlowDown`` (or a 5xx) past the client's own retries are
    repeated after jittered waits, and the run publishes ``ok`` with every minted blob on S3.
    Before, the first such failure was kept until the flush and failed the whole run
    (``failed[s3_transient]``, no outputs: the 2026-11-01 production re-sweep, 110 min)."""
    from stack.store import blobs as B

    slept: list[float] = []
    monkeypatch.setattr(B, "_sleep", slept.append)
    root, fake = slow_s3
    seeded(root, fake)
    fake.blob_code, fake.fail_blob_puts = code, 5
    toy_stage(stage_dir, "minter", MINTER)
    rec = run_stage(root, plan, "minter", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("ok", None), rec.error
    assert minted_keys() <= {k for (_, k) in fake.objects}
    assert len(slept) == 5 and all(0.5 * B.UPLOAD_RETRY_DELAYS[0] <= w for w in slept), slept


def test_d_a_run_dying_before_publish_leaves_local_blobs_a_later_put_uploads(
        slow_s3: tuple[Path, SlowBlobS3], plan: Any, stage_dir: Path,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) A run that dies between its mints and its publish, its uploads still blocked: nothing
    is published, the run ends without waiting on them (bounded close), and its local blob copies
    are not on S3 (a local copy proves nothing remote). The next run minting the same bytes uploads
    them (its put HEADs the remote, not the local file) before its own data."""
    from stack.store import blobs as B
    monkeypatch.setattr(B, "CLOSE_TIMEOUT", 0.05)
    root, fake = slow_s3
    seeded(root, fake)
    toy_stage(stage_dir, "minter", MINTER)
    MINTED["die"] = True
    real_set = BLOB_GATE.set
    monkeypatch.setattr(BLOB_GATE, "set", lambda: None)  # the dying run never releases its uploads,
    fake.fail_blobs = True  # and they never land (the process is gone)
    rec = run_stage(root, plan, "minter", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("failed", "code_bug")
    assert not any(k.startswith("data/") and rec.run_id in k for _, k in writes(fake))
    want = minted_keys()
    assert not (want & {k for (_, k) in fake.objects})
    blobs = B.open_blobs(root)
    assert all(blobs.local(k.rsplit("/", 1)[1]).is_file() for k in want)
    assert blobs.held([k.rsplit("/", 1)[1] for k in want]) == {}
    time.sleep(BLOCK_S + 0.2)  # the abandoned uploads wake and fail
    fake.fail_blobs = False
    monkeypatch.setattr(BLOB_GATE, "set", real_set)
    real_set()
    MINTED.clear()
    n0 = len(fake.calls)
    again = run_stage(root, plan, "minter", [DOCS], kind="part")
    assert again.status == "ok", again.error
    assert want <= {k for (_, k) in fake.objects}
    calls = fake.calls[n0:]
    data_put = calls.index(("put", f"{PREFIX}/{again.outputs['documents']['path']}"))
    assert max(i for i, (op, k) in enumerate(calls) if op == "put" and k in want) < data_put


def test_blob_store_outside_a_run_is_synchronous(slow_s3: tuple[Path, SlowBlobS3]) -> None:
    """``open_blobs`` (migrate, release, links, freeze, gate) stays synchronous: ``put`` returns
    with the blob on S3."""
    from stack.store.blobs import open_blobs
    root, fake = slow_s3
    BLOB_GATE.set()
    blobs = open_blobs(root)
    d = blobs.put(b"outside a run")
    assert (BUCKET, f"{PREFIX}/blobs/{d}") in fake.objects


def test_deferred_puts_from_threads_upload_each_digest_once(slow_s3: tuple[Path, SlowBlobS3]) -> None:
    """Deferred ``put`` from 8 threads (acquire's workers) of 4 distinct payloads: ``flush`` returns
    with all 4 on S3, each HEADed and PUT once (the queue dedupes by digest)."""
    from stack.store.blobs import BlobStore
    from stack.store.remote import open_store
    root, fake = slow_s3
    BLOB_GATE.set()
    blobs = BlobStore(open_store(root), deferred=True)
    ts = [threading.Thread(target=lambda i=i: [blobs.put(f"p{j}".encode()) for j in range(4)]) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    blobs.flush()
    blobs.close()
    puts = [k for op, k in fake.calls if op == "put" and "/blobs/" in k]
    heads = [k for op, k in fake.calls if op == "head" and "/blobs/" in k]
    assert len(puts) == len(set(puts)) == 4 and len(heads) == 4
