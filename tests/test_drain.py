"""The drain (D570-D573): a graceful stop of a run or build that publishes what it completed.

Core (a toy stage over a seeded ``documents`` series, ``stack.stage_runner.run_stage``):

* a drain mid-run: the stage stops admitting rows, the run publishes the rows it did and ends ``ok`` with
  ``scope_complete = false`` and ``counts["stopped.drain"]``; the next run does only the rest;
* SIGTERM under the CLI handler: the first drains, a second (or one with no job running) is the hard cancel,
  ``cancelled[operator]``, nothing published;
* stop requests (``stack stop``, :func:`stack.store.job.request_stop`): read by the job's tick on a local store
  and on the fake S3 store (the local file removed, so the S3 object is what the job reads; the job's end
  deletes it), ``now`` hard-cancels, refusals for no live job, a finished job and a dead one;
* builds (the synthetic M5 world and its stand-in stages): a drain ends the build ``incomplete`` with
  ``stopped`` (no later line, no repeat, no later pass), verify run; the next build resumes and ends ``ok``.

The stage-specific drains (acquire, the LLM stages, the collectors) are in ``tests/test_drain_stages.py``.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
import zstandard
from typer.testing import CliRunner

from stack import build as build_mod
from stack.build import incomplete_reasons, run_build, run_passes
from stack.cli import _on_sigterm, app
from stack.plan import Plan, load_plan
from stack.runs import find_run_record, read_run_records, running_records
from stack.stage_runner import WORKFLOWS_ROOT_ENV, run_stage
from stack.store import job as J
from stack.store.chain import read_view, resolve
from stack.store.index import store_index
from stack.store.layout import MIRROR_FILE
from stack.store.remote import resolve_store, using_s3_client
from tests.fakes.s3 import FAKE_ENV, FakeS3
from tests.synth.records import sdir
from tests.synth.world import build_world
from tests.test_stage_runner import seed_documents, stage_dir  # noqa: F401 — fixtures

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
STAND_INS = REPO / "tests" / "synth" / "stages"
M5_PLAN = REPO / "tests" / "synth" / "plans" / "synth-m5.yaml"
DOCS = sdir("documents")
BUCKET, PREFIX = "stack-data", "stack-v4"
FAST = dt.timedelta(milliseconds=50)
runner = CliRunner()

#: A toy stage: every row not yet stamped ``DONE`` (``updated_at``) is stamped, one at a time, admitting none once
#: the process drains. ``options`` (the dev plan has no line for it, so a module constant per test):
#: ``DRAIN_AT`` rows done before the stage itself triggers the stop (``signal``: SIGTERM to itself;
#: ``request``/``now``: a stop request for its own run, then it waits for the tick to read it).
TOY = '''
import datetime as dt, os, signal, time
from stack.stage_runner import StageResult
from stack.store import job as J

DRAIN_AT = {drain_at}
HOW = {how!r}


def run(ctx):
    view = ctx.view("documents")
    done_at = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    rows = [r for r in view.to_pylist() if r["updated_at"] != done_at]
    done = []
    for r in rows:
        if len(done) == DRAIN_AT:
            if HOW == "signal":
                os.kill(os.getpid(), signal.SIGTERM)
            elif HOW == "pair":
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
            elif HOW == "twice":
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(10)
            elif HOW in ("request", "now"):
                J.request_stop(ctx.store_root, run=ctx.run_id, mode="drain" if HOW == "request" else "now")
                if {local_gone}:
                    for p in ctx.store_root.glob("runs/*-stop.json"):
                        p.unlink()
                deadline = time.monotonic() + 10
                while not ctx.drain.stopping() and time.monotonic() < deadline:
                    time.sleep(0.02)
            elif HOW == "call":
                J.request_drain("test")
        if ctx.drain.stopping():
            break
        done.append(dict(r, updated_at=done_at))
    if done:
        ctx.emit_part("documents", view.from_pylist(done, schema=view.schema), "*")
    return StageResult(scope_complete=True)
'''


def toy(root: Path, drain_at: int | None, how: str = "call", *, local_gone: bool = False) -> None:
    d = root / "w_drain"
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(textwrap.dedent(TOY.format(drain_at=drain_at, how=how, local_gone=local_gone)))


def done(store: Path) -> int:
    """Rows the toy stamped."""
    at = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    return sum(1 for v in read_view(resolve(store, DOCS)).column("updated_at").to_pylist() if v == at)


def run_log(path: Path) -> str:
    return zstandard.ZstdDecompressor().decompress(path.read_bytes()).decode()


@pytest.fixture
def plan() -> Plan:
    return load_plan(DEV_PLAN)


@pytest.fixture
def fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(J, "TICK", FAST)


@pytest.fixture(autouse=True)
def no_drain_left() -> Iterator[None]:
    J.clear_drain()
    yield
    assert J.drain_state() is None, "the outermost job's end clears the drain"
    J.clear_drain()


@pytest.fixture
def sigterm() -> Iterator[None]:
    """The CLI's SIGTERM handler (``stack.cli.main`` installs it)."""
    prev = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, prev)


# ---------------------------------------------------------------- the run
def test_drained_run_publishes_what_it_did_and_the_next_run_does_the_rest(
        tmp_path: Path, plan: Plan, stage_dir: Path) -> None:  # noqa: F811
    store = tmp_path / "store"
    seed_documents(store)  # 12 rows
    toy(stage_dir, drain_at=3)
    rec = run_stage(store, plan, "w_drain", [DOCS], kind="part")
    assert (rec.status, rec.scope_complete, rec.counts.get("stopped.drain")) == ("ok", False, 1), rec.error
    assert incomplete_reasons(rec.counts) == ["stopped"]
    assert rec.outputs["documents"]["row_count"] == 3 and done(store) == 3
    toy(stage_dir, drain_at=None)
    again = run_stage(store, plan, "w_drain", [DOCS], kind="part")
    assert (again.status, again.scope_complete, again.counts.get("stopped.drain")) == ("ok", True, None)
    assert again.outputs["documents"]["row_count"] == 9 and done(store) == 12
    assert running_records(store) == []


def test_first_sigterm_drains(tmp_path: Path, plan: Plan, stage_dir: Path, sigterm: None) -> None:  # noqa: F811
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, drain_at=2, how="signal")
    rec = run_stage(store, plan, "w_drain", [DOCS], kind="part")
    assert (rec.status, rec.scope_complete, rec.counts.get("stopped.drain")) == ("ok", False, 1), rec.error
    assert rec.outputs["documents"]["row_count"] == 2


def test_second_sigterm_cancels(tmp_path: Path, plan: Plan, stage_dir: Path, sigterm: None,  # noqa: F811
                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(J, "SIGTERM_REPEAT_S", 0.0)  # no repeat window: the second SIGTERM at once
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, drain_at=2, how="twice")
    with pytest.raises(KeyboardInterrupt):
        run_stage(store, plan, "w_drain", [DOCS], kind="part")
    (rec,) = [r for r in read_run_records(store) if r.stage == "w_drain"]
    assert (rec.status, rec.error_class, rec.outputs) == ("cancelled", "operator", {})
    assert done(store) == 0  # nothing published


def test_a_repeated_sigterm_within_the_window_is_the_same_stop(
        tmp_path: Path, plan: Plan, stage_dir: Path, sigterm: None) -> None:  # noqa: F811
    """A SIGTERM to the process group reaches ``stack`` twice under ``uv run`` (it forwards it):
    two within :data:`stack.store.job.SIGTERM_REPEAT_S` drain, they do not cancel."""
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, drain_at=2, how="pair")
    rec = run_stage(store, plan, "w_drain", [DOCS], kind="part")
    assert (rec.status, rec.scope_complete, rec.counts.get("stopped.drain")) == ("ok", False, 1), rec.error
    assert rec.outputs["documents"]["row_count"] == 2


def test_sigterm_without_a_job_is_the_plain_interrupt(sigterm: None) -> None:
    with pytest.raises(KeyboardInterrupt):
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(100):  # the handler runs at the next bytecode boundary
            pass
    assert J.drain_state() is None


# ---------------------------------------------------------------- stop requests
def test_stop_request_on_a_local_store(tmp_path: Path, plan: Plan, stage_dir: Path,  # noqa: F811
                                       fast_tick: None) -> None:
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, drain_at=2, how="request")
    rec = run_stage(store, plan, "w_drain", [DOCS], kind="part")
    assert (rec.status, rec.scope_complete, rec.counts.get("stopped.drain")) == ("ok", False, 1), rec.error
    assert rec.outputs["documents"]["row_count"] == 2
    assert list(store.glob("runs/*-stop.json")) == []  # the job's end removed its request
    (log,) = store.glob(f"runs/*_{rec.run_id}*.log.zst")
    assert "stop request (drain)" in run_log(log)


def test_stop_now_request_cancels(tmp_path: Path, plan: Plan, stage_dir: Path,  # noqa: F811
                                  fast_tick: None, sigterm: None) -> None:
    store = tmp_path / "store"
    seed_documents(store)
    toy(stage_dir, drain_at=2, how="now")
    with pytest.raises(KeyboardInterrupt):
        run_stage(store, plan, "w_drain", [DOCS], kind="part")
    (rec,) = [r for r in read_run_records(store) if r.stage == "w_drain"]
    assert (rec.status, rec.error_class) == ("cancelled", "operator")
    assert done(store) == 0 and list(store.glob("runs/*-stop.json")) == []


@pytest.fixture
def s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, FakeS3]]:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    monkeypatch.setenv("STACK_MIRROR", str(tmp_path / "mirror"))
    fake = FakeS3(page=50)
    with using_s3_client(fake):
        yield resolve_store(f"s3://{BUCKET}/{PREFIX}", mirror_base=tmp_path / "mirror"), fake


def test_stop_request_on_the_fake_s3_store(s3: tuple[Path, FakeS3], plan: Plan, stage_dir: Path,  # noqa: F811
                                           fast_tick: None) -> None:
    """The request is PUT on S3; the local copy is removed, as on a machine other than the job's, so
    the job reads the S3 object (HEAD, GET). The job's end deletes it: the only DELETEs are the
    marker's and the request's."""
    root, fake = s3
    seed_documents(root)
    for p in sorted(root.rglob("*")):  # the seeded series, as an uploading job would have left it
        if p.is_file() and p.name != MIRROR_FILE and not p.name.startswith(".") and "cache" not in p.parts:
            fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(root).as_posix()}", Body=p.read_bytes())
    store_index(root, refresh=True)
    toy(stage_dir, drain_at=2, how="request", local_gone=True)
    n0 = len(fake.calls)
    rec = run_stage(root, plan, "w_drain", [DOCS], kind="part")
    assert (rec.status, rec.scope_complete, rec.counts.get("stopped.drain")) == ("ok", False, 1), rec.error
    calls = [(op, k.removeprefix(PREFIX + "/")) for op, k in fake.calls[n0:]]
    stops = [c for c in calls if c[1].endswith("-stop.json")]
    key = stops[0][1]
    assert {k for _, k in stops} == {key} and [op for op, _ in stops].count("put") == 1
    put = stops.index(("put", key))
    assert ("get", key) in stops[put:] and stops[-1] == ("delete", key)  # read on S3 after the PUT; deleted last
    deletes = [k for op, k in calls if op == "delete"]
    assert len(deletes) == 2 and deletes[0].endswith("-running.json") and deletes[1].endswith("-stop.json")
    assert not any(k.endswith("-stop.json") for _, k in fake.objects)
    assert find_run_record(root, rec.run_id) is not None


def test_stop_refusals(tmp_path: Path, plan: Plan, stage_dir: Path) -> None:  # noqa: F811
    store = tmp_path / "store"
    seed_documents(store)
    out = runner.invoke(app, ["stop", "--store", str(store)])
    assert out.exit_code == 1 and "no live build or run" in out.output
    toy(stage_dir, drain_at=None)
    rec = run_stage(store, plan, "w_drain", [DOCS], kind="part")
    out = runner.invoke(app, ["stop", "--store", str(store), "--run", rec.run_id])
    assert out.exit_code == 1 and "already ended (ok)" in out.output
    out = runner.invoke(app, ["stop", "--store", str(store), "--run", "r-nope"])
    assert out.exit_code == 1 and "no run named" in out.output
    # a running record whose marker is old: dead, nothing would read the request
    import json
    (path,) = store.glob(f"runs/*_{rec.run_id}*.json.zst")
    data = json.loads(zstandard.ZstdDecompressor().decompress(path.read_bytes()))
    data.update(status="running", finished_at=None, outputs={})
    dead = path.with_name(path.name.replace(rec.run_id, "r0000000000d9"))
    data["run_id"] = "r0000000000d9"
    dead.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(data).encode()))
    marker = Path(str(dead).removesuffix(".json.zst") + "-running.json")
    marker.write_text('{"tick": "2020-01-01T00:00:00Z"}\n')
    old = (dt.datetime.now() - dt.timedelta(hours=1)).timestamp()
    os.utime(marker, (old, old))
    out = runner.invoke(app, ["stop", "--store", str(store), "--run", "r0000000000d9"])
    assert out.exit_code == 1 and "is dead" in out.output
    assert list(store.glob("runs/*-stop.json")) == []


# ---------------------------------------------------------------- builds
def m5_plan(name: str = "synth-m5") -> Plan:
    data = yaml.safe_load(M5_PLAN.read_text("utf-8"))
    data["plan"] = name
    return Plan.model_validate(data)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    root = tmp_path / "store"
    build_world(root)
    return root


def drain_after(monkeypatch: pytest.MonkeyPatch, stage: str) -> list[str]:
    """Request a drain as soon as ``stage``'s first run of a build has ended (the build's job still
    running); returns the stages run."""
    real = build_mod.run_stage
    ran: list[str] = []

    def wrapped(root: Any, plan: Any, st: str, *a: Any, **k: Any) -> Any:
        ran.append(st)
        rec = real(root, plan, st, *a, **k)
        if st == stage:
            J.request_drain("test")
        return rec

    monkeypatch.setattr(build_mod, "run_stage", wrapped)
    return ran


@pytest.mark.slow
def test_build_stops_after_a_drain_and_the_next_build_resumes(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = drain_after(monkeypatch, "gather")
    b = run_build(world, m5_plan())
    assert b.status == "incomplete", b.data.get("error")
    assert ran == ["collect.arxiv", "gather"]
    stopped = b.data["stopped"]
    assert stopped["by"] == "test" and stopped["runs_drained"] == []
    assert stopped["not_run"] == ["triage", "acquire", "fold.corpus", "annotate.screen", "fold.build"]
    assert b.data["verify"]["status"] == "ok"
    out = runner.invoke(app, ["status", "--store", str(world)])
    assert "incomplete[stopped]" in out.output
    monkeypatch.setattr(build_mod, "run_stage", run_stage)
    again = run_build(world, m5_plan())
    assert again.status in ("ok", "incomplete") and "stopped" not in again.data, again.data.get("error")
    stages = {r.stage for r in read_run_records(world) if r.run_id in again.data["runs"]}
    assert {"triage", "acquire", "fold.corpus", "annotate.screen", "fold.build"} <= stages


@pytest.mark.slow
def test_drain_stops_a_repeat_and_the_passes(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """acquire's line repeats until scope_complete (the stand-in needs 2 runs): a drain during its first
    run starts no second one, and ``--passes 3`` stops after the stopped pass."""
    ran = drain_after(monkeypatch, "acquire")
    res = run_passes(world, m5_plan(), 3)
    assert res.stopped == "stopped" and len(res.builds) == 1
    b = res.builds[0]
    assert b.status == "incomplete" and ran.count("acquire") == 1
    assert b.data["stopped"]["not_run"] == ["fold.corpus", "annotate.screen", "fold.build"]
    assert [e["stage"] for e in b.data["incomplete"]] == ["acquire"]
