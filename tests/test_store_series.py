"""The T-STORE "series" package's new behaviour (D372-D375) that no other test file asserts:
the publish rule's own-output pass, racing restarts read as a fork, the restart guard (runner and
``stack fold``), ``If-None-Match`` on data PUTs at the run level, the tie guard end to end, the
pair check, a file without a series, ``run_line`` = the build line, and X2 under the conductor's
ruling (pins of directories the build did not write; ``in_files`` digests).

Covered elsewhere (not repeated here): ``head_moved`` and ``name_order`` at publish
(``test_store_chain::test_two_writers``, ``test_lost_update_full_and_restart``,
``test_name_order``); two writers with one ``after`` -> ``ForkError`` naming both runs
(``test_store_chain::test_two_writers``); ``tie_guard`` itself
(``test_store_chain::test_names_and_references``); ``Store.push(create_only=True)``
(``test_store_remote::test_store_push_create_only``); ``--in data/<family>/`` refused
(``test_stage_runner::test_exact_file_input_pins_that_version``); ``stack fold corpus --out`` into a
non-empty ``corpus/main/`` without ``--restart`` and fold.build's exemption
(``test_fold_build::test_cli_fold_build_run_fold_build_fold_release_and_release``).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from stack import stage_runner
from stack.cli import app
from stack.plan import Plan
from stack.runs import find_run_record, read_run_records
from stack.stage_runner import WORKFLOWS_ROOT_ENV, StageError, run_stage
from stack.store.chain import (
    ForkError,
    HeaderFields,
    HeadMovedError,
    StagedFile,
    claim,
    head_of,
    publish,
    resolve,
    stage_file,
)
from stack.store.index import store_index
from stack.store.job import recheck_outputs
from stack.store.layout import (
    DATA,
    MIRROR_FILE,
    make_data_file,
    parse_data_key,
    tie_guard,
)
from stack.store.parquet import read_header
from stack.store.remote import open_store, resolve_store, using_s3_client
from tests.fakes.s3 import FAKE_ENV, FakeS3
from tests.synth.records import (
    record_files,
    record_hook,
    rewrite_record,
    sdir,
    write_series,
)
from tests.synth.rows import AT, dg, document_row, rid, table, valid_file
from tests.test_stage_runner import plan, seed_documents, stage_dir, toy_stage  # noqa: F401 — fixtures

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
M5_PLAN = REPO / "tests" / "synth" / "plans" / "synth-m5.yaml"
STAND_INS = REPO / "tests" / "synth" / "stages"
DOCS = sdir("documents")  # data/documents/main/
D = DOCS.rstrip("/")
GATHER_BLOCKS = ["identity", "metadata", "inband"]
BUCKET, PREFIX = "stack-data", "stack-v4"
runner = CliRunner()


# ---------------------------------------------------------------- helpers
def fields(stage: str, run: int, **kw: Any) -> HeaderFields:
    return HeaderFields(stage=stage, run_id=rid(run), config_digest=dg(("config", stage)),
                        code_digest=dg("code"), plan="dev-v2", **kw)


def docs_full(store: Path, run: int, at: dt.datetime, *, series: str = "main", base: str = "",
              after: str = "") -> StagedFile:
    """A staged ``documents`` full of run ``run`` stamped ``at`` in ``data/documents/<series>/``."""
    dest = make_data_file("documents", series, at, rid(run), "full")
    return stage_file(store, dest, table("documents", [document_row(i, run) for i in range(3)]),
                      fields("gather", run, blocks_written=GATHER_BLOCKS), base=base, after=after, finished_at=at)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def force_publish(staged: StagedFile) -> Path:
    """Publish ``staged`` past the publish rule (rename + ``ok`` record), as a second writer that
    passed the check before the first one committed would."""
    os.rename(staged.path, staged.data_path)
    record_files(staged.store_root, [staged.data_path])
    return staged.data_path


def data_names(store: Path) -> list[str]:
    return sorted(p.relative_to(store).as_posix() for p in (store / DATA).rglob("*.parquet"))


def run_ids(store: Path) -> list[str]:
    return [r.run_id for r in read_run_records(store)]


# ---------------------------------------------------------------- 1. publish rule: own output
def test_publish_rule_own_output_is_done(store: Path) -> None:
    """A lost acknowledgement (``docs/OPERATE.md``, "Data files: series and chains"): the directory's
    last file is already this run's own output, so the publish (a retry, or ``push``'s re-check) is
    done instead of ``head_moved``."""
    first = write_series(store, "documents", "documents", table("documents", [document_row(0, 1)]),
                         fields("gather", 1, blocks_written=GATHER_BLOCKS), at=AT, on_published=record_hook(store))
    head = first.staged.dest.name
    s = docs_full(store, 2, AT + dt.timedelta(hours=1), base=head, after=head)
    (pub,) = publish(s)
    rec = record_files(store, [pub.path])  # the commit
    assert head_of(store, D) == s.name
    assert claim(store, s, own_committed=True) is False  # committed already: nothing more to publish
    with pytest.raises(HeadMovedError):  # without the own-output pass the retry would look moved
        claim(store, s)
    recheck_outputs(open_store(store), "runs/x.json.zst", rec.to_json())  # push's re-check passes too


# ---------------------------------------------------------------- 2. reader: racing restarts
def test_racing_restarts_are_a_fork(store: Path) -> None:
    """Two base-less fulls written after the same last file (two restarts racing; both passed the
    publish rule before either committed): the reader's ``after`` walk raises ``ForkError`` naming
    both runs. The same for two first files of an empty directory."""
    first = write_series(store, "documents", "documents", table("documents", [document_row(0, 1)]),
                         fields("gather", 1, blocks_written=GATHER_BLOCKS), at=AT, on_published=record_hook(store))
    head = first.staged.dest.name
    a = docs_full(store, 2, AT + dt.timedelta(hours=1), after=head)
    b = docs_full(store, 3, AT + dt.timedelta(hours=2), after=head)
    publish(a, record_hook(store))
    with pytest.raises(HeadMovedError):  # the later check sees the first restart
        publish(b)
    force_publish(b)
    with pytest.raises(ForkError) as exc:
        resolve(store, DOCS)
    assert rid(2) in str(exc.value) and rid(3) in str(exc.value), exc.value
    # two first files of one empty directory (both after = '')
    c = docs_full(store, 4, AT + dt.timedelta(hours=3), series="fresh")
    d = docs_full(store, 5, AT + dt.timedelta(hours=4), series="fresh")
    publish(c, record_hook(store))
    force_publish(d)
    with pytest.raises(ForkError) as exc2:
        resolve(store, sdir("documents", "fresh"))
    assert rid(4) in str(exc2.value) and rid(5) in str(exc2.value), exc2.value


# ---------------------------------------------------------------- 3. restart guard
FRESH = """
    from stack.stage_runner import StageResult
    from tests.synth.rows import document_row, table
    def run(ctx):
        ctx.emit_full("documents", table("documents", [document_row(i, 50) for i in range(3)]))
        return StageResult()
"""


def test_restart_guard(store: Path, plan: Any, stage_dir: Path) -> None:  # noqa: F811
    """A full without a base into a non-empty directory the run did not read is refused before
    the run (no record) and passes with ``restart``; an empty directory needs no flag;
    fold.compact into another non-empty documents directory needs ``restart`` too."""
    toy_stage(stage_dir, "fresh", FRESH)
    seed_documents(store)
    head, names, runs = head_of(store, D), data_names(store), run_ids(store)
    with pytest.raises(StageError, match="not empty and not read by this run"):
        run_stage(store, plan, "fresh", outs=[DOCS])
    assert data_names(store) == names and run_ids(store) == runs  # refused before any run
    rec = run_stage(store, plan, "fresh", outs=[DOCS], restart=True)
    assert rec.status == "ok", rec.error
    out = rec.outputs["documents"]
    assert (out["kind"], out["base"], out["after"]) == ("full", "", head), rec.error
    # the first file of an empty directory: no flag
    rec2 = run_stage(store, plan, "fresh", outs=[sdir("documents", "empty")])
    o2 = rec2.outputs["documents"] if rec2.outputs else {}
    assert rec2.status == "ok" and (o2["base"], o2["after"], o2["series"]) == ("", "", "empty"), rec2.error
    # fold.compact reads documents: --out into another non-empty documents directory is a restart
    other = seed_other(store)
    runs = run_ids(store)
    with pytest.raises(StageError, match="not empty and not read by this run"):
        run_stage(store, plan, "fold.compact", [DOCS], [sdir("documents", "other")])
    assert run_ids(store) == runs
    rec3 = run_stage(store, plan, "fold.compact", [DOCS], [sdir("documents", "other")], restart=True)
    o3 = rec3.outputs["documents"] if rec3.outputs else {}
    assert rec3.status == "ok" and (o3["base"], o3["after"]) == ("", other), rec3.error


def seed_other(store: Path) -> str:
    """``data/documents/other/``: one old full file (stamped ``AT``, so a run now sorts after it);
    its name."""
    pub = write_series(store, "documents", "documents", table("documents", [document_row(0, 70)]),
                       fields("gather", 70, blocks_written=GATHER_BLOCKS), series="other", at=AT,
                       on_published=record_hook(store))
    return pub.staged.dest.name


# ---------------------------------------------------------------- 4. `stack fold` CLI guard
def cli(store: Path, *args: str, code: int = 0) -> str:
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), *args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    print(f"$ stack {' '.join(args)}  -> {res.exit_code}\n{res.output}")
    assert res.exit_code == code, res.output
    return res.output


@pytest.mark.slow
def test_cli_fold_restart_guard(tmp_path: Path) -> None:
    """Conductor ruling: ``stack fold compact --out`` into another non-empty documents directory
    is a usage error (exit 1, ``error:``, no run record) without ``--restart`` and a run with it;
    ``stack run`` agrees, and ``stack run fold.corpus`` into a non-empty ``corpus/main/`` needs no
    flag (fold.corpus never reads its output family)."""
    from tests.fixtures.checks import build_store

    store = tmp_path / "store"
    build_store(store)  # documents/main F1-F5, corpus/main (run 8), ok records
    other, other_last = sdir("documents", "other"), seed_other(store)
    runs, names = run_ids(store), data_names(store)
    msg = cli(store, "fold", "compact", "--input", f"documents={DOCS}", "--out", other, code=1)
    assert "error:" in msg and "--restart" in msg
    assert run_ids(store) == runs and data_names(store) == names  # no run record, no file
    msg = cli(store, "run", "fold.compact", "--in", DOCS, "--out", other, code=1)
    assert "error:" in msg and "--restart" in msg and run_ids(store) == runs  # both CLI paths agree
    again = json.loads(cli(store, "fold", "compact", "--input", f"documents={DOCS}", "--out", other, "--restart"))
    assert again["status"] == "ok"
    h = read_header(store / again["outputs"]["documents"])
    assert (h["base"], h["after"]) == ("", other_last)
    empty = json.loads(cli(store, "fold", "compact", "--input", f"documents={DOCS}", "--out", sdir("documents", "e")))
    assert empty["status"] == "ok" and empty["outputs"]["documents"].startswith(sdir("documents", "e"))  # no flag
    corpus = sdir("corpus")
    assert head_of(store, corpus.rstrip("/"))  # non-empty
    out = json.loads(cli(store, "run", "fold.corpus", "--in", DOCS, "--out", corpus).splitlines()[-1])  # no flag
    assert out["status"] == "ok" and read_header(store / out["outputs"]["corpus"])["base"] == ""


# ---------------------------------------------------------------- 5. If-None-Match on data PUTs
class _PrePutS3(FakeS3):
    """Places an object at the next data file's key just before its create-only PUT: the same
    bytes (``same``: an earlier attempt's upload) or other bytes (``other``)."""

    def __init__(self) -> None:
        super().__init__(page=50)
        self.mode: str | None = None

    def put_object(self, *, Bucket: str, Key: str, Body: Any, IfNoneMatch: str | None = None) -> dict[str, Any]:
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        if self.mode is not None and IfNoneMatch == "*" and f"/{DATA}/" in f"/{Key}" and Key.endswith(".parquet"):
            with self._lock:
                self.objects[(Bucket, Key)] = data if self.mode == "same" else b"other bytes"
            self.mode = None
        return super().put_object(Bucket=Bucket, Key=Key, Body=data, IfNoneMatch=IfNoneMatch)


@pytest.fixture
def s3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, _PrePutS3]]:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    monkeypatch.setenv("STACK_MIRROR", str(tmp_path / "mirror"))
    fake = _PrePutS3()
    with using_s3_client(fake):
        yield resolve_store(f"s3://{BUCKET}/{PREFIX}", mirror_base=tmp_path / "mirror"), fake


def test_data_put_if_none_match(s3: tuple[Path, _PrePutS3], plan: Any) -> None:  # noqa: F811
    """A data PUT finding its key taken (412): the same bytes are an earlier attempt's upload and
    the run ends ``ok``; other bytes fail the run and are never overwritten."""
    root, fake = s3
    seed_documents(root)
    for p in sorted(root.rglob("*")):  # the seed on S3, as a finished uploading job leaves it
        if p.is_file() and p.name != MIRROR_FILE and not p.name.startswith(".") and "cache" not in p.parts:
            fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(root).as_posix()}", Body=p.read_bytes())
    store_index(root, refresh=True)
    fake.mode = "same"
    ok = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    key = f"{PREFIX}/{ok.outputs['documents']['path']}" if ok.outputs else ""
    assert ok.status == "ok" and ok.upload is True, ok.error
    assert fake.ops("put_refused") == [key]  # the 412, accepted on an equal digest
    fake.mode = "other"
    bad = run_stage(root, plan, "echo_test", [DOCS], kind="part")
    assert bad.status == "failed" and bad.outputs == {} and "S3 holds other bytes" in (bad.error or ""), bad.error
    taken = [k for k in fake.ops("put_refused") if k != key]
    assert len(taken) == 1 and bad.run_id in taken[0] and fake.objects[(BUCKET, taken[0])] == b"other bytes"
    assert resolve(root, DOCS).head == ok.outputs["documents"]["file_id"]


# ---------------------------------------------------------------- 6. tie guard end to end
def test_tie_guard_end_to_end(store: Path, plan: Any) -> None:  # noqa: F811
    """A run that starts before (or in the same second as) its newest input file's stamp names its
    outputs 1 s after that stamp; ``started_at`` stays the true start."""
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).replace(microsecond=0)
    seed = write_series(store, "documents", "documents", table("documents", [document_row(i, 1) for i in range(3)]),
                        fields("gather", 1, blocks_written=GATHER_BLOCKS), at=future, on_published=record_hook(store))
    stamp_in = seed.staged.dest.name[:16]
    rec = run_stage(store, plan, "echo_test", [DOCS], kind="part")
    assert rec.status == "ok", rec.error
    name = Path(rec.outputs["documents"]["path"]).name
    one_after = f"{future + dt.timedelta(seconds=1):%Y%m%dT%H%M%SZ}"
    assert rec.started_at < future and name[:16] == tie_guard(future, stamp_in) == one_after
    # a run in the same second as its input: the stamp is still after it
    rec2 = run_stage(store, plan, "echo_test", [DOCS], kind="part")
    name2 = Path(rec2.outputs["documents"]["path"]).name
    assert rec2.status == "ok" and name2[:16] == tie_guard(rec2.started_at, name[:16]) > name[:16], rec2.error


# ---------------------------------------------------------------- 7. pair check
PAIRS = """
    from stack.stage_runner import StageResult
    def run(ctx):
        return StageResult()
"""


def test_pair_inputs_from_one_run(store: Path, plan: Any, stage_dir: Path) -> None:  # noqa: F811
    """``screen`` + ``screen_evidence`` read together must come from one run (``docs/OPERATE.md``,
    "Runs stand alone"): from two runs (series directories or exact files alike) they are refused
    before dispatch; from one run they are accepted."""
    toy_stage(stage_dir, "pairs", PAIRS)

    def staged(fam: str, run: int, at: dt.datetime) -> Any:
        tbl, _ = valid_file(fam)
        kw = {"basis_kind": "text"} if fam == "screen" else {}
        return stage_file(store, make_data_file(fam, "main", at, rid(run), "full"), tbl,
                          fields("annotate.screen", run, **kw), after=head_of(store, f"data/{fam}/main"), finished_at=at)

    a = publish([staged("screen", 1, AT), staged("screen_evidence", 1, AT)], record_hook(store))
    b = publish(staged("screen", 2, AT + dt.timedelta(hours=1)), record_hook(store))[0]
    a_screen, a_ev = (p.staged.dest.key for p in a)
    runs = run_ids(store)
    for ins in ([sdir("screen"), sdir("screen_evidence")], [b.staged.dest.key, a_ev]):
        with pytest.raises(StageError, match="are not from one run"):
            run_stage(store, plan, "pairs", ins)
    assert run_ids(store) == runs  # refused before any run
    rec = run_stage(store, plan, "pairs", [a_screen, a_ev])
    assert rec.status == "ok" and rec.inputs == {"screen": f"{rid(1)}.screen",
                                                 "screen_evidence": f"{rid(1)}.screen_evidence"}, rec.error


def first_pair(store: Path) -> tuple[str, str]:
    """The series' first ``screen`` + ``screen_evidence`` pair, one run, published; its keys."""
    def staged(fam: str) -> StagedFile:
        kw = {"basis_kind": "text"} if fam == "screen" else {}
        return stage_file(store, make_data_file(fam, "main", AT, rid(1), "full"), valid_file(fam)[0],
                          fields("annotate.screen", 1, **kw), after="", finished_at=AT)

    a_screen, a_ev = (p.staged.dest.key for p in publish([staged("screen"), staged("screen_evidence")],
                                                          record_hook(store)))
    return a_screen, a_ev


def test_pair_with_one_half_orphaned_is_refused(store: Path) -> None:
    """S25-49: the series' only pair with its evidence (or its answers) orphaned leaves that prior
    directory empty; an empty directory is a requested prior too, so the pair is refused (the run
    would re-ask everything against kept answers); both halves empty is a first run."""
    from stack.plan import StageLine
    from stack.store.chain import orphan

    line, refs = StageLine(stage="annotate.screen"), [sdir("screen"), sdir("screen_evidence")]
    a_screen, a_ev = first_pair(store)
    orphan(store, a_ev, note="test: only the evidence")
    with pytest.raises(StageError, match="data/screen_evidence/main/, which has no published file"):
        stage_runner._pin_all(store, "annotate.screen", line, refs)
    orphan(store, a_screen, note="test: now the answers too")
    _, prior, empty = stage_runner._pin_all(store, "annotate.screen", line, refs)
    assert prior == {} and {e.family for e in empty} == {"screen", "screen_evidence"}


def test_pair_with_only_the_answers_orphaned_is_refused(store: Path) -> None:
    from stack.plan import StageLine
    from stack.store.chain import orphan

    a_screen, _ = first_pair(store)
    orphan(store, a_screen, note="test: only the answers")
    with pytest.raises(StageError, match="data/screen/main/, which has no published file"):
        stage_runner._pin_all(store, "annotate.screen", StageLine(stage="annotate.screen"),
                              [sdir("screen"), sdir("screen_evidence")])


# ---------------------------------------------------------------- 8. a file without a series
def test_file_without_series_read_by_exact_path_only(store: Path, plan: Any) -> None:  # noqa: F811
    """``--out data/documents/`` writes ``data/documents/<name>.full.parquet``: in no series
    directory (the ``main`` head is unchanged), read only by its exact path."""
    base = seed_documents(store)
    rec = run_stage(store, plan, "echo_test", [DOCS], ["data/documents/"])
    out = rec.outputs["documents"]
    f = parse_data_key(out["path"])
    assert rec.status == "ok" and (f.series, f.kind, f.dir) == (None, "full", "data/documents"), rec.error
    assert out["path"] == f"data/documents/{f.name}" and f.name.endswith(f"_{rec.run_id}.full.parquet")
    assert (out["series"], out["base"], out["after"]) == ("", "", "")
    assert resolve(store, DOCS).head == base.file_id  # not on the main series
    assert store_index(store, refresh=True).series_dirs("documents") == [D]
    assert resolve(store, out["path"]).version_chain == (out["file_id"],)
    again = run_stage(store, plan, "echo_test", [out["path"]], [sdir("documents", "copy")])
    assert again.status == "ok" and again.inputs == {"documents": out["file_id"]}, again.error


# ---------------------------------------------------------------- 10. X2 (conductor ruling, option A)
@pytest.mark.slow  # a whole synthetic build
def test_x2_unbuilt_dir_ok_and_pin_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gather reads a ``sources`` series directory no line of the plan collects: the build ends
    ``ok`` and X2 is ``ok`` (a pin of a directory the build did not write is never a lineage
    fault). A run pin (``in_files``) whose digest is not the publishing record's is an X2 finding."""
    from stack.build import run_build
    from stack.checks.verify import run_checks
    from tests.synth.world import build_world

    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    store = tmp_path / "store"
    build_world(store)
    tbl, _ = valid_file("sources")
    extra = write_series(store, "sources.extra", "sources", tbl, fields("collect.extra", 60),
                         at=AT, on_published=record_hook(store))
    extra_key = extra.staged.dest.key
    p = Plan.model_validate(yaml.safe_load(M5_PLAN.read_text("utf-8")))
    assert all(not s.stage.endswith(".extra") for s in p.stages)
    b = run_build(store, p)
    assert b.status == "ok", b.data.get("error")
    x2 = {c["name"]: c for c in b.data["verify"]["checks"]}["lineage_equality"]
    assert x2["status"] == "ok", x2
    gather = next(r for r in (find_run_record(store, i) for i in b.data["runs"]) if r is not None and r.stage == "gather")
    assert extra_key in [f["path"] for f in gather.in_files]
    assert "sources/sources.extra" not in b.files  # not a file of this build
    # a pin whose digest differs from the publishing record's
    gather.in_files = [dict(f, file_digest="0" * 64) if f["path"] == extra_key else f  # type: ignore[misc]
                       for f in gather.in_files]
    rewrite_record(store, gather)
    (res,) = run_checks(store, names=["lineage_equality"], build=b.path)
    assert res.status == "failed", res
    items = [i for f in res.findings for i in f.items]
    assert any(extra_key in i and "at digest 000000000000" in i for i in items), items
