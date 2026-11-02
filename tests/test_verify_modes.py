"""What a verify runs (:mod:`stack.checks.verify`): three **parts** or **full**.

* ``structural``: store-wide file damage, S1 from footers, V11 (chains resolve, keys unique), X1
  without a re-hash, X5 with raw sizes, X23; every build at its start and its finish,
  ``stack verify --structural``;
* ``files``: every check on the files a build's (or a run's) records name;
* ``build_set``: the build checks on that build;
* full: every check on every file, data files re-hashed (``--full`` only).

``stack verify`` runs ``files`` + ``build_set`` on the newest ``ok`` build. Nothing is carried between
verifies: a build record's ``verify`` and ``verify_start`` hold ``status``, ``mode`` and ``checks``.

The worlds are the per-check fixture stores (``tests.fixtures.checks``): runs 1-5 write the
``documents`` chain F1-F5, run 6 the collector files, run 7 ``screen`` and its evidence, run 8 the
corpus. A fault planted in run 5's file is caught by the ``files`` part scoped to run 5 and by a full
verify; a fault in an older file (run 4's compaction) only by the full one.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from stack.build import run_build
from stack.checks.verify import (
    BUILD_FINISH_PARTS,
    CHECK_PARTS,
    FILES,
    STRUCTURAL,
    STRUCTURAL_CHECKS,
    mode_line,
    run_checks,
    written_files,
)
from stack.cli import app
from stack.runs import read_build_record, write_build_record
from stack.stage_runner import WORKFLOWS_ROOT_ENV
from stack.store import parquet
from stack.store.layout import MIRROR_FILE
from stack.store.parquet import footer_cache
from stack.store.remote import using_s3_client
from tests.fakes.s3 import FAKE_ENV, FakeS3
from tests.fixtures.checks import FIXTURES, World, clean_world, write_world
from tests.synth.rows import mint, rid
from tests.synth.world import build_world
from tests.test_scenario_m5 import STAND_INS, plan_with
from tests.test_store_parquet import count_footer_parses, rewrite_header_in_place

pytestmark = [pytest.mark.e2e, pytest.mark.checks]  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

AT = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
runner = CliRunner()
STRUCT = {STRUCTURAL}


def by_name(res: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in res}


def store(tmp_path: Path, fixture: str | None = None, edit: Any = None) -> Path:
    world = clean_world()
    if fixture is not None:
        FIXTURES[fixture](world)
    if edit is not None:
        edit(world)
    return write_world(world, tmp_path / "store")


def failed(res: list[Any]) -> set[str]:
    return {r.name for r in res if r.status == "failed"}


def with_build(root: Path, runs: list[str], n: int = 101, **kw: Any) -> dict[str, Any]:
    """Write a build record of the fixture world naming ``runs``, finished after the world's own."""
    at = AT + dt.timedelta(days=30, hours=n)
    rec = {**(clean_world().build or {}), "build_id": mint("b", n), "runs": runs, "started_at": at.isoformat(),
           "finished_at": (at + dt.timedelta(minutes=1)).isoformat(), **kw}
    write_build_record(root, rec)
    return rec


# ---------------------------------------------------------------- files: what a build or run wrote
@pytest.mark.parametrize(("check", "run"), [("created_preserved", 5), ("V-digests", 5), ("key_unique", 7),
                                          ("V-wf", 7), ("V-evidence", 7)])
@pytest.mark.slow
def test_files_part_catches_a_fault_in_a_file_the_run_wrote(tmp_path: Path, check: str, run: int) -> None:
    """X17 (a pair check: F5 resets a ``created_at``), V4 (a file check: F5's stale
    ``fields_digest``), S3 (``screen``, a chain of one, holds a key twice), V7 (a ``screen`` row
    without evidence) and V8 (a stale evidence ``result_digest``): per-file store checks."""
    root = store(tmp_path, check)
    assert check in failed(run_checks(root))  # full
    scoped = run_checks(root, parts={FILES}, runs=[rid(run)])
    assert check in failed(scoped), by_name(scoped)[check]
    assert scoped.mode == "files" and scoped.scope == written_files(root, [rid(run)])
    other = by_name(run_checks(root, parts={FILES}, runs=[rid(3)]))[check]  # run 3 wrote F3 only
    assert other.status == "ok", other
    bare = by_name(run_checks(root, parts=STRUCT))[check]  # structural only: the per-file check does not run
    assert (bare.status, bare.error_class) == ("skipped", "files")


def test_default_parts_check_the_newest_builds_files_and_build_set(tmp_path: Path) -> None:
    root = store(tmp_path, "created_preserved")
    with_build(root, [rid(5)])  # now the newest ok build
    res = run_checks(root, parts=CHECK_PARTS)
    by = by_name(res)
    assert res.mode == "files+build_set" and res.scope == written_files(root, [rid(5)])
    assert by["created_preserved"].status == "failed"
    # the build checks run on that build (X10 on its version_chain; X12 on its set) ...
    assert by["ordering_pinned"].status == "ok" and by["state_propagation"].status == "ok"
    # ... and the structural checks do not
    assert {r.code for r in res if r.error_class == "structural"} == set(STRUCTURAL_CHECKS) - {"S1"}
    assert by["schema_equal"].status == "ok"  # S1 is also a file check: on the build's files
    # without the build set
    no_set = by_name(run_checks(root, parts={FILES}))
    assert no_set["created_preserved"].status == "failed"
    assert (no_set["ordering_pinned"].status, no_set["ordering_pinned"].error_class) == ("skipped", "build_set")


@pytest.mark.slow
def test_files_part_without_a_build_is_skipped(tmp_path: Path) -> None:
    def no_build(w: World) -> None:
        w.build = None

    res = by_name(run_checks(store(tmp_path, edit=no_build), parts=CHECK_PARTS))
    assert (res["created_preserved"].status, res["created_preserved"].error_class) == ("skipped", "no_builds")
    assert (res["V-digests"].status, res["V-digests"].error_class) == ("skipped", "no_builds")


def test_written_files_names_the_run_outputs_and_refuses_an_unknown_run(tmp_path: Path) -> None:
    root = store(tmp_path)
    assert {Path(k).parent.as_posix() for k in written_files(root, [rid(7)])} == {
        "data/screen/main", "data/screen_evidence/main"}
    with pytest.raises(KeyError, match="no run record of run 'rnope'"):
        run_checks(root, parts={FILES}, runs=["rnope"])
    with pytest.raises(ValueError, match="files part only"):
        run_checks(root, runs=[rid(5)])
    with pytest.raises(ValueError, match="unknown verify part"):
        run_checks(root, parts={"fast"})


# ---------------------------------------------------------------- full catches what the parts do not see
@pytest.mark.slow
def test_full_catches_a_fault_in_an_old_file_the_parts_do_not(tmp_path: Path) -> None:
    """V12 (F4, run 4's compaction, is not the union of F1-F3): outside a build scoped to run 5."""
    root = store(tmp_path, "V-compaction-equal")
    rec = with_build(root, [rid(5)])
    assert "V-compaction-equal" in failed(run_checks(root))
    assert not failed(run_checks(root, parts=STRUCT))
    assert not failed(run_checks(root, parts=BUILD_FINISH_PARTS, build=rec))


@pytest.mark.slow
def test_full_rehashes_a_same_size_edit_structural_does_not(tmp_path: Path) -> None:
    root = store(tmp_path)
    path = root / next(iter(written_files(root, [rid(1)])))
    data = bytearray(path.read_bytes())
    # one byte inside the data pages, so the footer still reads: the middle of the file's largest column chunk,
    # located from its metadata (the file's middle byte can be footer once the footer outgrows the pages)
    meta = pq.ParquetFile(path).metadata
    chunks = [meta.row_group(g).column(c) for g in range(meta.num_row_groups) for c in range(meta.num_columns)]
    chunk = max(chunks, key=lambda ch: ch.total_compressed_size)
    start = chunk.dictionary_page_offset if chunk.has_dictionary_page else chunk.data_page_offset
    offset = start + chunk.total_compressed_size // 2
    footer_start = len(data) - 8 - int.from_bytes(data[-8:-4], "little")
    assert 4 <= offset < footer_start, (offset, footer_start)
    data[offset] ^= 0xFF
    path.write_bytes(bytes(data))
    full = by_name(run_checks(root, names=["finished_files"]))["finished_files"]
    assert full.status == "failed" and "does not have its record's file_digest" in full.items[0]
    assert by_name(run_checks(root, parts=STRUCT, names=["finished_files"]))["finished_files"].status == "ok"


# ---------------------------------------------------------------- the structural part
def test_structural_x1_reads_footers_against_the_records(tmp_path: Path) -> None:
    root = store(tmp_path)
    path = root / next(iter(written_files(root, [rid(1)])))
    path.write_bytes(path.read_bytes()[:-200])  # truncated: the footer is gone
    res = by_name(run_checks(root, parts=STRUCT, names=["finished_files", "schema_equal"]))
    assert res["finished_files"].status == "failed" and "footer does not read" in res["finished_files"].items[0]


def test_structural_x1_compares_a_mirror_copy_with_the_store_object(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    root = store(tmp_path)
    bucket, prefix = "fake-store-bucket", "stores/t1"
    (root / MIRROR_FILE).write_text(json.dumps({"uri": f"s3://{bucket}/{prefix}"}))
    fake = FakeS3(page=4)
    keys = sorted(written_files(root, [rid(n) for n in range(1, 9)]))
    for key in keys:
        body = (root / key).read_bytes()
        fake.put_object(Bucket=bucket, Key=f"{prefix}/{key}", Body=body + (b"x" if key == keys[0] else b""))
    with using_s3_client(fake):
        res = by_name(run_checks(root, parts=STRUCT, names=["finished_files"]))["finished_files"]
    assert res.status == "failed" and res.n == 1, res
    size = (root / keys[0]).stat().st_size
    assert res.items[0].endswith(f"{keys[0]} is {size} bytes, the store's object {size + 1}")


def test_structural_s1_reads_the_footer(tmp_path: Path) -> None:
    root = store(tmp_path, "schema_equal")  # screen's `attempts` int64
    s1 = by_name(run_checks(root, parts=STRUCT))["schema_equal"]
    assert s1.status == "failed" and "SchemaMismatch" in s1.findings[0].message
    assert "is not the file's own schema's" in s1.items[0]


def test_structural_v11_catches_a_key_twice_in_a_chain_head(tmp_path: Path) -> None:
    def twice(w: World) -> None:  # F5, the documents head, holds doc 6 twice
        w.raw_ok = True
        w.rows("F5").append(copy.deepcopy(w.rows("F5")[0]))

    root = store(tmp_path, edit=twice)
    res = by_name(run_checks(root, parts=STRUCT))
    assert res["V-chain-linear"].status == "failed" and "1 duplicate doc_id rows" in res["V-chain-linear"].items[0]
    assert res["key_unique"].error_class == "files"  # S3 itself is a per-file check


# ---------------------------------------------------------------- the footer cache (D731)
def _results(res: list[Any]) -> list[tuple[Any, ...]]:
    return [(r.name, r.status, r.error_class, r.n, tuple(r.all_items)) for r in res]


def _screen_file(root: Path) -> Path:
    return root / next(k for k in written_files(root, [rid(7)]) if k.startswith("data/screen/"))


def test_a_footer_changed_between_verifies_is_found(tmp_path: Path) -> None:
    """Nothing a verify caches outlives it: a footer changed in place (size, mtime, inode kept) after a clean
    verify is the next verify's finding."""
    root = store(tmp_path)
    assert not failed(run_checks(root, parts=STRUCT))
    rewrite_header_in_place(_screen_file(root), "schema_digest", "0" * 64)
    s1 = by_name(run_checks(root, parts=STRUCT))["schema_equal"]
    assert s1.status == "failed" and "is not the file's own schema's" in s1.items[0]


@pytest.mark.slow
def test_a_footer_changed_within_one_cache_block_is_found_as_without_it(tmp_path: Path) -> None:
    """A build's verifies share one footer cache (its start and its finish): a file changed in place between
    them reads as it now is, and the second verify's results are those of a verify with no cache before it."""
    root = store(tmp_path)
    with footer_cache():
        assert not failed(run_checks(root))  # every footer and the files' metadata now cached
        rewrite_header_in_place(_screen_file(root), "schema_digest", "0" * 64)
        inside_struct, inside_full = run_checks(root, parts=STRUCT), run_checks(root)
    assert "schema_equal" in failed(inside_struct) and "schema_equal" in failed(inside_full)
    assert _results(inside_struct) == _results(run_checks(root, parts=STRUCT))
    assert _results(inside_full) == _results(run_checks(root))


@pytest.mark.slow
def test_a_verify_parses_each_footer_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = store(tmp_path)
    files = sorted((root / "data").rglob("*.parquet"))
    parses = count_footer_parses(monkeypatch)  # a footer parse per file, none for a data read (< METADATA_ENTRIES files)
    digests: list[str] = []
    real_digest = parquet.schema_digest
    monkeypatch.setattr(parquet, "schema_digest", lambda s: digests.append("") or real_digest(s))
    for parts in (None, STRUCT, BUILD_FINISH_PARTS):
        parses.clear()
        digests.clear()
        assert not failed(run_checks(root, parts=parts))
        assert sorted(parses) == files, parts
        assert len(digests) == len(files), parts


def _raw_digest(root: Path) -> tuple[str, int]:
    rows = pq.read_table(root / next(iter(written_files(root, [rid(3)])))).to_pylist()
    snap = rows[0]["content"]["snapshots"][0]
    return snap["raw"]["sha256"], snap["raw"]["bytes"]


def test_structural_x5_catches_a_missing_blob(tmp_path: Path) -> None:
    root = store(tmp_path)
    assert by_name(run_checks(root, parts=STRUCT))["blob_resolution"].status == "ok"
    digest, _ = _raw_digest(root)
    (root / "blobs" / digest).unlink()
    x5 = by_name(run_checks(root, parts=STRUCT))["blob_resolution"]
    assert x5.status == "failed" and all("not under blobs/" in i for i in x5.items), x5
    assert digest[:12] in x5.items[0]
    assert by_name(run_checks(root, parts=CHECK_PARTS))["blob_resolution"].error_class == "structural"


@pytest.mark.slow
def test_structural_and_full_x5_catch_a_raw_size_mismatch(tmp_path: Path) -> None:
    root = store(tmp_path)
    digest, size = _raw_digest(root)
    (root / "blobs" / digest).write_bytes(b"." * (size - 1))
    for parts in (STRUCT, None):
        x5 = by_name(run_checks(root, parts=parts))["blob_resolution"]
        assert x5.status == "failed", x5
        assert x5.items[0].endswith(f"{digest[:12]}… is {size - 1} bytes under blobs/, the row says {size}")


@pytest.mark.slow
def test_x5_checks_every_blobs_size(tmp_path: Path) -> None:
    """D690: every BLOB records its size, so X5 compares text and in-band blobs too, not only a raw's."""
    root = store(tmp_path)
    rows = pq.read_table(root / next(iter(written_files(root, [rid(3)])))).to_pylist()
    text = next(s["text"] for r in rows for s in (r["content"] or {}).get("snapshots") or []
                if s["text"] and s["text"]["sha256"] != s["raw"]["sha256"])
    (root / "blobs" / text["sha256"]).write_bytes(b"." * (text["bytes"] + 3))
    x5 = by_name(run_checks(root))["blob_resolution"]  # full: every part
    assert x5.status == "failed", x5
    assert any(i.endswith(f"{text['sha256'][:12]}… is {text['bytes'] + 3} bytes under blobs/, the row says "
                          f"{text['bytes']}") and " text " in i for i in x5.items), x5.items


def test_structural_runs_only_its_checks(tmp_path: Path) -> None:
    res = run_checks(store(tmp_path), parts=STRUCT)
    ran = {r.code for r in res if r.status != "skipped"}
    assert ran == set(STRUCTURAL_CHECKS), ran
    assert all(r.status == "ok" for r in res if r.code in STRUCTURAL_CHECKS)
    assert res.scope is None and mode_line(res).startswith("mode: structural (structural: footers")


# ---------------------------------------------------------------- the CLI
@pytest.mark.slow
def test_cli_defaults_and_flags(tmp_path: Path) -> None:
    root = store(tmp_path, "created_preserved")
    with_build(root, [rid(3)], n=100, series="main", plan="plan-main")
    rec = with_build(root, [rid(5)], series="exp", plan="plan-exp")  # the newest ok build
    n5 = len(written_files(root, [rid(5)]))

    def cli(*args: str) -> Any:
        return runner.invoke(app, ["--store", str(root), "verify", *args])

    out = cli()  # the newest ok build
    assert out.exit_code == 1 and "created_preserved" in out.output
    assert out.output.splitlines()[-1] == (f"mode: files+build_set (files: {n5} file(s) the build or run(s) wrote; "
                                           "build set: the build checks; --full checks every file)")
    j = json.loads(cli("--json").output)
    assert (j["mode"], j["scope_files"]) == ("files+build_set", n5)
    assert {c["name"]: c["error_class"] for c in j["checks"]}["blob_resolution"] == "structural"
    assert "pairs" not in j
    out = cli("--no-build-set")
    assert out.exit_code == 1 and out.output.splitlines()[-1].startswith("mode: files (")
    out = cli("--structural")
    assert out.exit_code == 0 and out.output.splitlines()[-1].startswith("mode: structural (")
    out = cli("--full")
    assert out.exit_code == 1 and out.output.splitlines()[-1] == (
        "mode: full (every check on every file, data files re-hashed)")
    # --series picks that series' newest finished build
    j = json.loads(cli("--series", "main", "--json").output)
    assert j["scope_files"] == 1 and {c["name"]: c["status"] for c in j["checks"]}["created_preserved"] == "ok"
    assert json.loads(cli("--series", "exp", "--json").output)["scope_files"] == n5
    assert "no ok or incomplete build of that series" in cli("--series", "nope").output
    assert json.loads(cli("--build", rec["build_id"], "--json").output)["scope_files"] == n5
    j = json.loads(cli("--run", rid(3), "--json").output)
    assert j["scope_files"] == 1 and {c["name"]: c["status"] for c in j["checks"]}["created_preserved"] == "ok"
    for bad in (("--full", "--structural"), ("--full", "--run", rid(5)), ("--structural", "--no-build-set"),
                ("--build", rec["build_id"], "--series", "exp")):
        out = cli(*bad)
        assert out.exit_code == 1 and "error" in out.output, (bad, out.output)
    out = cli("--build", "bnope")
    assert out.exit_code == 1 and "--build" in out.output


# ---------------------------------------------------------------- builds
@pytest.mark.slow
def test_builds_verify_structural_at_start_and_three_parts_at_finish(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKFLOWS_ROOT_ENV, str(STAND_INS))
    root = tmp_path / "store"
    build_world(root)
    b1 = run_build(root, plan_with())
    assert b1.status == "ok", b1.data.get("error")
    b2 = run_build(root, plan_with())
    assert b2.status == "ok", b2.data.get("error")
    for b in (b1, b2):
        start, v = b.data["verify_start"], b.data["verify"]
        assert set(start) == set(v) == {"status", "mode", "checks"}
        assert (start["status"], start["mode"]) == ("ok", "structural"), start
        assert (v["status"], v["mode"]) == ("ok", "structural+files+build_set"), v
        assert not [c for c in v["checks"] if c["error_class"] in ("structural", "files", "build_set")]
    # the files part was exactly build 2's files: those its runs' records name
    again = run_checks(root, parts=BUILD_FINISH_PARTS, build=read_build_record(b2.path))
    assert again.scope == written_files(root, b2.data["runs"]) and again.scope
    assert [(r.name, r.status) for r in again] == [(c["name"], c["status"]) for c in b2.data["verify"]["checks"]]
    b3 = run_build(root, plan_with(), verify_full=True)
    assert (b3.status, b3.data["verify"]["mode"], b3.data["verify_start"]["mode"]) == ("ok", "full", "structural")
    # damage before a build: the structural verify at its start fails it before any run
    snap = next(s for r in pq.read_table(root / next(iter(sorted(written_files(root, b3.data["runs"])))))
                .to_pylist() if (r.get("content") or {}).get("snapshots") for s in r["content"]["snapshots"])
    (root / "blobs" / snap["raw"]["sha256"]).unlink()
    b4 = run_build(root, plan_with())
    assert (b4.status, b4.data["error_class"]) == ("failed", "verify"), b4.data.get("error")
    assert b4.data["verify_start"]["status"] == "failed" and b4.data["runs"] == []
    assert "verify_start: hard check(s) failed: blob_resolution" in b4.data["error"]


def test_reference_build_is_the_newest_ok_build_of_the_in_series(tmp_path: Path) -> None:
    """S25-39: standalone verify anchored the build checks (X2, X10, X14) on the newest ok build of
    any series. With ``--in`` the default is the newest ok build of those directories' series;
    directories of several series are refused (no one reference); without ``--in`` the newest of all."""
    from stack.checks.chain import MixedSeriesError, StoreView

    root = store(tmp_path)
    world = clean_world().build or {}
    ids = {}
    for n, series in ((1, "main"), (2, "exp")):  # exp's build finished last
        at = AT + dt.timedelta(hours=n)
        rec = {**world, "build_id": mint("b", 200 + n), "plan": f"plan-{series}", "series": series,
               "started_at": at.isoformat(), "finished_at": (at + dt.timedelta(minutes=1)).isoformat()}
        write_build_record(root, rec)
        ids[series] = rec["build_id"]
    assert StoreView(root, dirs=["data/documents/main"]).build["build_id"] == ids["main"]  # type: ignore[index]
    assert StoreView(root).build["build_id"] == ids["exp"]  # type: ignore[index]
    assert StoreView(root, dirs=["data/documents/other"]).build is None  # no build of that series: skipped
    with pytest.raises(MixedSeriesError, match="several series"):
        StoreView(root, dirs=["data/documents/main", "data/documents/exp"])
    res = runner.invoke(app, ["--store", str(root), "verify", "--in", "data/documents/main/", "--in",
                              "data/documents/exp/"])
    assert res.exit_code == 1 and "several series ['exp', 'main']" in res.output, res.output


@pytest.mark.slow
def test_the_default_build_is_the_newest_finished_one_ok_or_incomplete(tmp_path: Path) -> None:
    """D652: with no build given, ``stack verify`` (and ``--series``) checks the newest ``ok`` or
    ``incomplete`` build, never a ``failed`` one; X10 judges that build's ``version_chain``."""
    from stack.checks.chain import StoreView

    root = store(tmp_path)
    world = clean_world().build or {}
    good = with_build(root, [rid(5)], n=101, series="main")
    vc = world["version_chain"]
    broken = with_build(root, [rid(3)], n=102, series="main", status="incomplete", version_chain=[vc[1], vc[0], *vc[2:]])
    with_build(root, [rid(1)], n=103, series="main", status="failed", version_chain=["nope"])
    assert StoreView(root).build["build_id"] == broken["build_id"]  # type: ignore[index]
    res = run_checks(root, parts=CHECK_PARTS)
    assert res.scope == written_files(root, [rid(3)])
    assert by_name(res)["ordering_pinned"].status == "failed"  # the incomplete build's chain is out of order
    j = json.loads(runner.invoke(app, ["--store", str(root), "verify", "--series", "main", "--json"]).output)
    assert {c["name"]: c["status"] for c in j["checks"]}["ordering_pinned"] == "failed"
    assert j["scope_files"] == 1
    # given explicitly, the ok build is checked
    assert by_name(run_checks(root, parts=CHECK_PARTS, build=good))["ordering_pinned"].status == "ok"
    # D703: `--build latest` is the same build as the default (was: the newest ok one)
    j = json.loads(runner.invoke(app, ["--store", str(root), "verify", "--build", "latest", "--json"]).output)
    assert {c["name"]: c["status"] for c in j["checks"]}["ordering_pinned"] == "failed"

