"""Crash/restart (T1.3 slot 2; D16, D38, D60, D61; T-STORE A2:
D352-D355): a subprocess ``stack run echo_test`` over a local-only store is SIGKILLed at each
injected point of a run's end (env ``STACK_CRASH_AT``, :data:`stack.store.job.CRASH_POINTS`), the
damage is read through the library functions the verbs wrap (``status`` →
:func:`stack.cli.status.collect_status` and the store index's liveness,
``runs.files_without_ok_record``), and a re-run publishes. There are no locks to break.

| point               | data/ after the kill            | record             |
|---------------------|---------------------------------|--------------------|
| `after_claim`       | unchanged (`<dir>/.<name>.tmp`) | running → dead     |
| `after_rename`      | + the part, no ok record        | running → dead     |
| `after_data_put`    | + the part, no ok record        | running → dead     |
| `after_final_local` | + the part, ok record           | ok (finished)      |

Death is the index's (D344): a ``running`` json whose ``-running.json`` marker is older than
``DEAD_AFTER``; right after the kill the marker is fresh (``live``), so each case ages it with
``os.utime``. Series resolve from ok run records (D345): after ``after_rename``/``after_data_put``
the renamed part is named by no record, so it is in no series (garbage, ``files_without_ok_record``),
the head is still the last recorded file, and a re-run publishes on that head; the stray part
stays garbage. ``after_final_put`` needs an uploading store (a remote), so it is not reached here.
A series the stage publishes to resolves the same way (the publish rule,
:func:`stack.store.chain.claim`: ``head_moved``, ``name_order``), in-process.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from stack import stage_runner
from stack.cli.status import collect_status
from stack.plan import load_plan
from stack.runs import (
    files_without_ok_record,
    find_run_record,
    read_run_records,
    records_without_file,
    running_records,
)
from stack.stage_runner import run_stage
from stack.store.chain import (
    HeaderFields,
    head_of,
    resolve,
)
from stack.store.index import DEAD_AFTER, store_index
from stack.store.job import CRASH_POINTS
from stack.store.layout import DATA
from tests.synth.records import sdir, write_series
from tests.synth.rows import AT, dg, document_row, rid, table
from tests.test_stage_runner import seed_documents

#: subprocess crash/restart runs: skipped while iterating (`-m "not slow"`), run at milestone gates (D90; `docs/SETUP.md` §7).
pytestmark = [pytest.mark.slow, pytest.mark.e2e]

REPO = Path(__file__).resolve().parents[1]
DOCS = sdir("documents")  # data/documents/main/
#: the points a local-only store reaches (``after_final_put`` follows the final json's PUT); derived,
#: so a new crash point is killed here without a test to keep a copy in step
LOCAL_POINTS = tuple(p for p in CRASH_POINTS if p != "after_final_put")


@pytest.fixture
def plan_path() -> Path:
    return REPO / "configs" / "plans" / "dev-v2.yaml"


def stack_run(store: Path, plan: Path, crash_at: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "STACK_CRASH_AT"}
    if crash_at is not None:
        env["STACK_CRASH_AT"] = crash_at
    return subprocess.run(
        ["uv", "run", "--no-sync", "stack", "run", "echo_test", "--in", DOCS, "--part",
         "--store", str(store), "--plan", str(plan)],
        check=False, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


def data_files(store: Path) -> list[str]:
    return sorted(p.name for p in (store / DATA).rglob("*.parquet"))


def staged_files(store: Path, run_id: str) -> list[Path]:
    """The run's staged (unpublished) outputs ``<dir>/.<name>.tmp``."""
    return sorted((store / DOCS).glob(f".*_{run_id}*.tmp"))


def marker_of(store: Path, run_id: str) -> Path:
    (m,) = (store / "runs").glob(f"*_{run_id}*-running.json")
    return m


@pytest.mark.parametrize("point", LOCAL_POINTS)
def test_kill_rerun(tmp_path: Path, plan_path: Path, point: str) -> None:
    store = tmp_path / "store"
    base = seed_documents(store)

    proc = stack_run(store, plan_path, point)
    assert proc.returncode != 0 and f"STACK_CRASH_AT={point}: SIGKILL" in proc.stderr, proc.stderr
    # the killed run's one record (D344): still running, or final once it got past the final json
    (killed,) = [r for r in running_records(store) + read_run_records(store) if r.stage == "echo_test"]
    rid = killed.run_id
    final = find_run_record(store, rid)
    assert final is not None

    def liveness() -> str:
        e = store_index(store).run(rid)
        assert e is not None
        return e.liveness

    if point == "after_final_local":  # the file is legitimately in the series
        assert final.status == "ok" and liveness() == "finished"
        assert files_without_ok_record(store) == []
        assert resolve(store, DOCS).head == f"{rid}.documents"
        assert collect_status(store)["by_liveness"]["dead"] == 0
    else:
        assert final.status == "running"
        # death is the marker's age, not the kill: the marker the run's tick left is fresh
        assert liveness() == "live"
        assert [(j["id"], j["liveness"]) for j in collect_status(store)["jobs"] if j["status"] == "running"] == [
            (rid, "live")]
        old = (dt.datetime.now(dt.UTC) - DEAD_AFTER - dt.timedelta(minutes=1)).timestamp()
        os.utime(marker_of(store, rid), (old, old))
        assert liveness() == "dead"
        st = collect_status(store)
        assert [(j["id"], j["liveness"]) for j in st["jobs"] if j["status"] == "running"] == [(rid, "dead")]
        assert head_of(store, DOCS) == base.path.name  # the head is the last recorded file
        if point == "after_claim":
            assert data_files(store) == [base.path.name]  # nothing renamed
            assert len(staged_files(store, rid)) == 1  # staged as <dir>/.<name>.tmp: garbage
            assert files_without_ok_record(store) == []
        else:  # after_rename, after_data_put (no upload on a local-only store: the same state)
            stray = [n for n in data_files(store) if rid in n]
            assert len(stray) == 1 and staged_files(store, rid) == []  # renamed, never recorded
            assert [p.name for p in files_without_ok_record(store)] == stray  # garbage: in no series (D345)

    rerun = stack_run(store, plan_path)
    assert rerun.returncode == 0 and '"status": "ok"' in rerun.stdout, rerun.stdout + rerun.stderr
    ok = [r for r in read_run_records(store) if r.stage == "echo_test" and r.status == "ok"]
    ch = resolve(store, DOCS)  # linear chain (V11 is the resolver's check), head = the re-run
    assert ch.head == ok[-1].outputs["documents"]["file_id"]
    expected = [base.file_id] + ([f"{rid}.documents"] if point == "after_final_local" else []) + [ch.head]
    assert list(ch.version_chain) == expected
    garbage = [n for n in data_files(store) if rid in n] if point in ("after_rename", "after_data_put") else []
    assert [p.name for p in files_without_ok_record(store)] == garbage and records_without_file(store) == []


def test_series_with_an_unrecorded_file_publishes_on_the_recorded_head(
        tmp_path: Path, plan_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D345 (was D88's backstop): a stage publishing to a series is checked by the publish rule
    (:func:`stack.store.chain.claim`), which resolves the series from the records: a part renamed into the series
    directory without a record is in no series, so the run publishes on the last recorded head
    and the stray part stays garbage. (The in-flight ``lock_held`` case went with the chain locks.)"""
    store = tmp_path / "store"
    base = seed_documents(store)
    fields = HeaderFields(stage="triage", run_id=rid(2), config_digest=dg("c"), code_digest=dg("code"),
                          plan="dev-v2", blocks_written=["annotations"])
    tail = write_series(store, "documents", "documents", table("documents", [document_row(0, 2)]), fields,
                        kind="part", finished_at=AT + dt.timedelta(minutes=2))  # no record
    assert files_without_ok_record(store) == [tail.path]
    plan = load_plan(plan_path)
    workflows = tmp_path / "workflows"
    (workflows / "writer").mkdir(parents=True)
    (workflows / "writer" / "main.py").write_text(textwrap.dedent("""
        from stack.stage_runner import StageResult
        from stack.store.chain import read_view, resolve
        def run(ctx):
            ctx.emit_part("documents", read_view(resolve(ctx.store_root, "data/documents/main/")).slice(0, 1), "*")
            return StageResult()
    """))
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", workflows)
    assert head_of(store, DOCS) == base.path.name
    rec = run_stage(store, plan, "writer", [DOCS], kind="part")
    assert (rec.status, rec.error_class) == ("ok", None), rec.error
    new = rec.outputs["documents"]["file_id"]
    ch = resolve(store, DOCS)
    assert (ch.head, list(ch.version_chain)) == (new, [base.file_id, new])
    assert files_without_ok_record(store) == [tail.path]
    assert staged_files(store, rec.run_id) == [] and records_without_file(store) == []
