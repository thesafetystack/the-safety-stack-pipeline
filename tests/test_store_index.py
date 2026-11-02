"""T-STORE A1 (D344-D346): the store index (records at the S3 layout's paths, liveness from
markers, ``uploaded_only``), chains from records with pull on read over a fake S3 (only the pinned
version's files are fetched, each checked against its record's digest), an excluded head, the
newest ``ok`` build skipping unfinished ones, the release tag refused from a remote ``release.json``,
and X1's garbage report. No network: S3 is ``tests/fakes/s3.py``."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pyarrow as pa
import pytest

from stack.checks.verify import run_checks
from stack.fold import FoldError
from stack.fold.release import load_release_config, release_exists, resolve_destination, write_release_dir
from stack.runs import (
    RunRecord,
    newest_ok_build,
    run_record_path,
    write_build_record,
    write_final_record,
    write_running_record,
)
from stack.store import remote as R
from stack.store.chain import (
    HeaderFields,
    MissedFileError,
    Published,
    head_of,
    orphan,
    read_view,
    resolve,
)
from stack.store.index import DEAD_AFTER, RecordIndexError, store_index
from stack.store.layout import MIRROR_FILE
from tests.fakes.s3 import FAKE_ENV, FakeS3
from tests.synth.records import record_hook, sdir, write_series
from tests.synth.rows import dg, document_row, rid, table

BUCKET, PREFIX = "stack-data", "stack-v4"
URI = f"s3://{BUCKET}/{PREFIX}"
T0 = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
GATHER = ["identity", "metadata", "inband"]
D = "data/documents/main"


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)


def put(store: Path, kind: str, run: int, rows: range) -> Published:
    """A ``documents`` file of ``kind`` on the head of ``data/documents/main/`` with its ``ok`` record."""
    stage = "gather" if kind == "full" else "triage"
    f = HeaderFields(stage=stage, run_id=rid(run), config_digest=dg("c"), code_digest=dg("code"), plan="dev-v2",
                     blocks_written=GATHER if kind == "full" else ["annotations"])
    at = T0 + dt.timedelta(minutes=run)
    return write_series(store, "documents", "documents", table("documents", [document_row(i, run) for i in rows]),
                        f, kind=kind, at=at, finished_at=at, on_published=record_hook(store, at=at))


def four_files(store: Path) -> list[Published]:
    """S0 full, S1 part, S2 full (extends S1, supersedes it), S3 part: the pinned version is S2, S3."""
    return [put(store, "full", 1, range(6)), put(store, "part", 2, range(3)), put(store, "full", 3, range(6)),
            put(store, "part", 4, range(2))]


def upload(store: Path, fake: FakeS3) -> None:
    """PUT every store file (records, data) under the fake's store prefix."""
    for p in sorted(store.rglob("*")):
        if p.is_file() and p.name != MIRROR_FILE and not p.name.startswith("."):
            fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(store).as_posix()}", Body=p.read_bytes())


def data_gets(fake: FakeS3) -> set[str]:
    return {k.rsplit("/", 1)[-1] for op, k in fake.calls if op == "get" and "/data/" in k}


# ---------------------------------------------------------------- chains from records over S3
def test_fresh_mirror_reads_only_the_pinned_version(tmp_path: Path, fake_env: None) -> None:
    local = tmp_path / "local"
    fs = four_files(local)
    want = read_view(resolve(local, sdir("documents")))
    fake = FakeS3(page=2)
    upload(local, fake)
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        assert head_of(m, D) == fs[3].path.name  # from the records alone
        assert data_gets(fake) == set()
        ch = resolve(m, sdir("documents"))
        assert ch.version_chain == (fs[2].file_id, fs[3].file_id)
        got = data_gets(fake)
        assert len(got) == 2 and all(rid(3) in n or rid(4) in n for n in got)  # S0, S1 never fetched
        assert read_view(ch).equals(want)
        assert not any(rid(1) in p.name or rid(2) in p.name for p in (m / "data").rglob("*.parquet"))


def test_download_is_checked_against_the_record_digest(tmp_path: Path, fake_env: None) -> None:
    local = tmp_path / "local"
    fs = four_files(local)
    fake = FakeS3()
    upload(local, fake)
    key = next(k for (_, k) in fake.objects if rid(4) in k and "/data/" in k)
    fake.objects[(BUCKET, key)] = fake.objects[(BUCKET, key)][:-1] + b"\x00"
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        with pytest.raises(MissedFileError, match=key.removeprefix(f"{PREFIX}/")) as exc:
            resolve(m, sdir("documents"))
        assert "sha256" in str(exc.value)
        assert not (m / key.removeprefix(f"{PREFIX}/")).exists()  # the bad download is removed
        assert head_of(m, D) == fs[3].path.name


def test_running_json_refetched_and_uploaded_only(tmp_path: Path, fake_env: None) -> None:
    now = [T0]
    fake = FakeS3(clock=lambda: now[0])
    src = tmp_path / "src"
    rec = RunRecord(run_id=rid(7), stage="gather", writer="gather", plan="dev-v2", status="running",
                    started_at=T0, heartbeat_at=T0, code_digest=dg("code"), config_digest=dg("c"))
    upload_one = lambda p: fake.put_object(
        Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(src).as_posix()}", Body=p.read_bytes())
    upload_one(write_running_record(src, rec))
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        e = store_index(m).run(rid(7))
        assert e is not None and e.record.status == "running" and e.uploaded and e.liveness == "unknown"
        # S3's copy goes final; a refresh re-fetches the local running json
        now[0] = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)
        rec.status, rec.finished_at = "ok", T0
        upload_one(write_final_record(src, rec))
        assert store_index(m).run(rid(7)).record.status == "running"  # no LIST, no re-fetch
        e = store_index(m, refresh=True).run(rid(7))
        assert e is not None and e.record.status == "ok" and e.liveness == "finished"
        # a no-upload job's local-only record: seen by default, ignored by an uploading job
        mine = RunRecord(run_id=rid(8), stage="gather", writer="gather", plan="dev-v2", status="ok",
                         started_at=T0, heartbeat_at=T0, finished_at=T0, code_digest=dg("code"),
                         config_digest=dg("c"))
        write_final_record(m, mine)
        e = store_index(m).run(rid(8))
        assert e is not None and not e.uploaded
        try:
            assert store_index(m, uploaded_only=True).run(rid(8)) is None
            assert store_index(m).run(rid(7)) is not None
        finally:
            store_index(m, uploaded_only=False)


# ---------------------------------------------------------------- liveness
def test_liveness_from_markers(tmp_path: Path) -> None:
    store = tmp_path / "store"
    old = dt.datetime.now(dt.UTC) - DEAD_AFTER - dt.timedelta(minutes=1)

    def run(n: int, status: str = "running") -> Path:
        r = RunRecord(run_id=rid(n), stage="gather", writer="gather", plan="dev-v2", status=status,
                      started_at=T0, heartbeat_at=T0, finished_at=T0 if status != "running" else None,
                      code_digest=dg("code"), config_digest=dg("c"))
        (write_running_record if status == "running" else write_final_record)(store, r)
        return run_record_path(store, r)

    def marker(p: Path, at: dt.datetime | None = None) -> None:
        m = p.with_name(p.name.removesuffix(".json.zst") + "-running.json")
        m.write_text('{"tick": "x"}')
        if at is not None:
            os.utime(m, (at.timestamp(), at.timestamp()))

    marker(run(1))
    marker(run(2), old)
    run(3)
    marker(run(4, "ok"), old)
    got = {e.run_id: e.liveness for e in store_index(store).runs}
    assert got == {rid(1): "live", rid(2): "dead", rid(3): "unknown", rid(4): "finished"}
    b = {"build_id": "w000000000001", "plan": "dev-v2", "status": "running", "started_at": T0.isoformat()}
    p = write_build_record(store, b)
    (p.parent / "build-running.json").write_text("{}")
    (e,) = store_index(store).builds
    assert e.liveness == "live" and e.name == p.parent.name
    # X11 counts index-dead runs (a stale marker), not unknown ones
    (x11,) = run_checks(store, names=["dead_runs_seen"])
    assert x11.status == "ok"  # no inflight keys repeated


def test_record_name_must_match_its_content(tmp_path: Path) -> None:
    store = tmp_path / "store"
    r = RunRecord(run_id=rid(1), stage="gather", writer="gather", plan="dev-v2", status="ok",
                  started_at=T0, heartbeat_at=T0, finished_at=T0, code_digest=dg("code"), config_digest=dg("c"))
    p = write_final_record(store, r)
    os.rename(p, store / "runs" / f"{T0:%Y%m%dT%H%MZ}_{rid(1)}.run.json.zst")  # the old layout's name
    with pytest.raises(RecordIndexError, match="old-layout"):
        store_index(store)
    other = tmp_path / "other"
    (other / "runs" / "builds").mkdir(parents=True)
    (other / "runs" / "builds" / f"{T0:%Y%m%dT%H%MZ}_w000000000001_dev-v2.build.json.zst").write_bytes(b"x")
    with pytest.raises(RecordIndexError, match="old layout"):  # never "no build, no prior"
        store_index(other)


# ---------------------------------------------------------------- excludes, priors
def test_excluded_head(tmp_path: Path) -> None:
    store = tmp_path / "store"
    fs = four_files(store)
    rec = orphan(store, fs[3].staged.dest.key)
    assert resolve(store, sdir("documents")).head == fs[2].file_id
    assert fs[3].path.is_file() and rec.is_file()
    (x1,) = run_checks(store, names=["finished_files"])
    assert x1.status == "ok"  # the excluded file is still recorded and present
    with pytest.raises(Exception, match="excluded already"):
        orphan(store, fs[3].staged.dest.key)
    with pytest.raises(Exception, match="not the last file"):
        orphan(store, fs[0].staged.dest.key)


def test_newest_ok_build_skips_unfinished_builds(tmp_path: Path) -> None:
    store = tmp_path / "store"

    def build(n: int, status: str, finished: int | None) -> None:
        write_build_record(store, {"build_id": f"w00000000000{n}", "plan": "dev-v2", "status": status,
                                   "started_at": (T0 + dt.timedelta(hours=n)).isoformat(),
                                   "finished_at": (T0 + dt.timedelta(hours=finished)).isoformat()
                                   if finished is not None else None})

    build(1, "ok", 2)
    build(2, "incomplete", 3)
    build(3, "failed", 4)
    build(4, "running", None)
    # the prior-build half removed with newest_prior_build (D373): priors are --in paths
    ok = newest_ok_build(store, "dev-v2")
    assert ok is not None and ok[1]["build_id"] == "w000000000001" and ok[0].name == "build.json.zst"


# ---------------------------------------------------------------- release tag
def test_release_tag_refused_from_a_remote_release_json(tmp_path: Path, fake_env: None) -> None:
    fake = FakeS3()
    fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/releases/public-1/release.json", Body=b"{}")
    doc = {"tag": "x", "build_id": "none", "corpus_file_id": "none", "config_digest": "none", "run_id": rid(9),
           "schema_digest": "none", "row_count": 0, "config": {"name": "public"}}
    tbl = pa.table({"doc_id": pa.array([], pa.string())})
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        assert release_exists(m, "public-1") and not (m / "releases" / "public-1").exists()
        with pytest.raises(FoldError, match="exists"):  # the remote's release.json (D346)
            resolve_destination(m, load_release_config("public-v1"), tag="public-1")
        # an incomplete tag directory (no release.json) is not a release, and a retry replaces it
        (m / "releases" / "public-2").mkdir(parents=True)
        (m / "releases" / "public-2" / "corpus.parquet").write_bytes(b"half")
        assert not release_exists(m, "public-2")
        dest = resolve_destination(m, load_release_config("public-v1"), tag="public-2")
        out = write_release_dir(m, dest, tbl, {**doc, "tag": "public-2"})
        assert (out / "release.json").is_file() and (out / "corpus.parquet").read_bytes() != b"half"
        assert release_exists(m, "public-2")


# ---------------------------------------------------------------- X1
def test_x1_garbage_is_reported_not_failed(tmp_path: Path) -> None:
    store = tmp_path / "store"
    fs = four_files(store)
    (x1,) = run_checks(store, names=["finished_files"])
    assert x1.status == "ok"
    f = HeaderFields(stage="triage", run_id=rid(9), config_digest=dg("c"), code_digest=dg("code"), plan="dev-v2",
                     blocks_written=["annotations"])
    stray = write_series(store, "documents", "documents", table("documents", [document_row(1, 9)]), f,
                         kind="part", finished_at=T0)  # published, no record: garbage
    assert resolve(store, sdir("documents")).head == fs[3].file_id
    (x1,) = run_checks(store, names=["finished_files"])
    assert x1.status == "warn" and any(stray.path.name in i and "garbage" in i for i in x1.all_items)
    lost = next((store / "data").rglob(f"*{rid(2)}*.parquet"))
    lost.unlink()  # a recorded (superseded) file gone: a failure
    (x1,) = run_checks(store, names=["finished_files"])
    assert x1.status == "failed" and any(lost.name in i for i in x1.all_items)


def test_x1_reports_a_corrupt_remote_data_file(tmp_path: Path, fake_env: None) -> None:
    """F1: a recorded data file whose S3 body is not its record's ``file_digest`` (the superseded
    full file S0, never local on a fresh mirror) fails X1, naming the key; the view pass that met
    it while downloading must not drop it silently."""
    local = tmp_path / "local"
    fs = four_files(local)
    fake = FakeS3()
    upload(local, fake)
    key = next(k for (_, k) in fake.objects if rid(1) in k and "/data/" in k)
    fake.objects[(BUCKET, key)] = fake.objects[(BUCKET, key)][:-1] + b"\x00"
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        (x1,) = run_checks(m, names=["finished_files"])
    rel = key.removeprefix(PREFIX + "/")
    assert x1.status == "failed"
    assert any(fs[0].file_id in i and rel in i and "file_digest" in i for i in x1.all_items), x1.all_items
# ---------------------------------------------------------------- D359: re-fetch by the LIST's stamp, temp downloads
def _rec7(status: str = "running", **kw: object) -> RunRecord:
    return RunRecord(run_id=rid(7), stage="gather", writer="gather", plan="dev-v2", status=status,
                     started_at=T0, heartbeat_at=T0, code_digest=dg("code"), config_digest=dg("c"),
                     finished_at=None if status == "running" else T0, **kw)  # type: ignore[arg-type]


def test_final_put_in_the_same_second_is_fetched(tmp_path: Path, fake_env: None) -> None:
    """S3's ``LastModified`` is server time at whole seconds: a final json PUT in the same second as
    the ``running`` one the mirror downloaded (here: both stamped T0, long before the local download)
    is re-fetched on the next refresh because the LIST's stamp differs from the local copy's, never
    by comparing with the local mtime as a clock (critic finding 1)."""
    fake = FakeS3(clock=lambda: T0)
    src = tmp_path / "src"
    up = lambda p: fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(src).as_posix()}",  # noqa: E731
                                   Body=p.read_bytes())
    up(write_running_record(src, _rec7()))
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        assert store_index(m).run(rid(7)).record.status == "running"
        up(write_final_record(src, _rec7("ok")))
        assert store_index(m, refresh=True).run(rid(7)).record.status == "ok"


def test_a_same_size_final_json_in_the_same_second_is_not_read_from_the_parse_cache(
        tmp_path: Path, fake_env: None) -> None:
    """S25-93 (Astra L2-2): the parse cache is keyed by ``(mtime_ns, size)`` and a download takes
    S3's ``LastModified`` as its mtime, so a final json of the cached ``running`` one's compressed
    size, PUT in the same second, read as ``running`` again. A download drops the cache entry."""
    import hashlib

    def pad(n: int) -> str:  # incompressible text, n characters
        return "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(n // 64 + 1))[:n]

    running_size = write_running_record(tmp_path / "r", _rec7()).stat().st_size
    n = next(n for n in range(400)
             if write_final_record(tmp_path / f"f{n}", _rec7("ok", note=pad(n))).stat().st_size == running_size)
    fake = FakeS3(clock=lambda: T0)
    src = tmp_path / "src"
    up = lambda p: fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(src).as_posix()}",  # noqa: E731
                                   Body=p.read_bytes())
    up(write_running_record(src, _rec7()))
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        assert store_index(m).run(rid(7)).record.status == "running"
        up(write_final_record(src, _rec7("ok", note=pad(n))))
        assert store_index(m, refresh=True).run(rid(7)).record.status == "ok"


def test_a_changed_final_json_is_fetched(tmp_path: Path, fake_env: None) -> None:
    """A final json whose S3 copy changed (another LIST stamp) is re-fetched too (critic finding 3):
    the mirror holds S3's copy, not its older local one."""
    fake = FakeS3()
    src = tmp_path / "src"
    up = lambda p: fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(src).as_posix()}",  # noqa: E731
                                   Body=p.read_bytes())
    up(write_final_record(src, _rec7("ok", counts={"x": 1})))
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        assert store_index(m).run(rid(7)).record.counts == {"x": 1}
        (src / store_index(m).run(rid(7)).key).unlink()
        up(write_final_record(src, _rec7("ok", counts={"x": 22})))
        assert store_index(m, refresh=True).run(rid(7)).record.counts == {"x": 22}


def test_a_local_final_json_is_never_replaced_by_a_remote_running_one(
        tmp_path: Path, fake_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check-then-act (critic finding 4): while the mirror downloads S3's ``running`` copy, the job
    on this machine writes its final json locally; the download must not replace it."""
    fake = FakeS3(clock=lambda: T0 + dt.timedelta(hours=1))
    src = tmp_path / "src"
    up = lambda p: fake.put_object(Bucket=BUCKET, Key=f"{PREFIX}/{p.relative_to(src).as_posix()}",  # noqa: E731
                                   Body=p.read_bytes())
    up(write_running_record(src, _rec7(counts={"tick": 1})))
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        write_running_record(m, _rec7())  # the job's own running json, older than S3's tick
        orig = R.Remote.download

        def download_while_the_job_ends(self: R.Remote, key: str, dst: Path) -> Path:
            if key.endswith(".json.zst"):
                write_final_record(m, _rec7("ok"))  # the job ends meanwhile
            return orig(self, key, dst)

        monkeypatch.setattr(R.Remote, "download", download_while_the_job_ends)
        e = store_index(m, refresh=True).run(rid(7))
        assert e.record.status == "ok" and e.liveness == "finished"


def test_a_corrupt_download_never_sits_at_the_output_path(
        tmp_path: Path, fake_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A data download is checked before it is renamed into place (critic finding 2): a process
    killed during the check leaves nothing at the output's path, so the next read downloads and
    checks again instead of trusting a corrupt local file."""
    import stack.store.index as I

    local = tmp_path / "local"
    four_files(local)
    fake = FakeS3()
    upload(local, fake)
    key = next(k for (_, k) in fake.objects if rid(4) in k and "/data/" in k)
    fake.objects[(BUCKET, key)] = fake.objects[(BUCKET, key)][:-1] + b"\x00"
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        idx = store_index(m, refresh=True)
        out = next(o for _, _, o in idx.outputs() if o["path"] == key.removeprefix(f"{PREFIX}/"))

        def killed(_p: object) -> str:
            raise KeyboardInterrupt("killed during the digest check")

        digest = I.file_digest
        monkeypatch.setattr(I, "file_digest", killed)
        with pytest.raises(KeyboardInterrupt):
            idx.fetch_output(out)
        monkeypatch.setattr(I, "file_digest", digest)
        assert not (m / out["path"]).exists()
        with pytest.raises(I.OutputDigestError):
            idx.fetch_output(out)


def _altered(p: Path) -> bytes:
    """``p``'s bytes with one byte in the middle flipped: same size, the parquet footer intact."""
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0xFF
    return bytes(b)


def test_an_altered_mirror_file_is_replaced_by_the_store_copy(tmp_path: Path, fake_env: None) -> None:
    """G2 (D371): a local mirror file whose bytes are not its record's ``file_digest`` (a stale or
    altered copy, footer intact) is moved aside and the S3 copy downloaded when a job pins it."""
    import stack.store.index as I

    local = tmp_path / "local"
    fs = four_files(local)
    fake = FakeS3()
    upload(local, fake)
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        out = next(o for _, _, o in store_index(m, refresh=True).outputs() if o["file_id"] == fs[3].file_id)
        good = (local / out["path"]).read_bytes()
        bad = _altered(local / out["path"])
        (m / out["path"]).parent.mkdir(parents=True, exist_ok=True)
        (m / out["path"]).write_bytes(bad)
        with pytest.warns(UserWarning, match="moved aside"):
            ch = resolve(m, sdir("documents"))
        assert ch.version_chain == (fs[2].file_id, fs[3].file_id)
        assert (m / out["path"]).read_bytes() == good
        aside = [p for p in (m / "data").rglob("*") if ".mismatch-" in p.name]
        assert len(aside) == 1 and aside[0].name.startswith(".") and aside[0].read_bytes() == bad
        assert I.file_digest(m / out["path"]) == out["file_digest"]


def test_an_altered_mirror_file_with_no_good_copy_fails_loudly(tmp_path: Path, fake_env: None) -> None:
    """G2 (D371): when S3's copy is altered too, the pin fails with the digest error (the existing
    loud error) and nothing sits at the output's path; the local bad copy is kept aside."""
    local = tmp_path / "local"
    fs = four_files(local)
    fake = FakeS3()
    upload(local, fake)
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        out = next(o for _, _, o in store_index(m, refresh=True).outputs() if o["file_id"] == fs[3].file_id)
        fake.objects[(BUCKET, f"{PREFIX}/{out['path']}")] = _altered(local / out["path"])
        (m / out["path"]).parent.mkdir(parents=True, exist_ok=True)
        (m / out["path"]).write_bytes(_altered(local / out["path"]))
        with pytest.warns(UserWarning, match="moved aside"), pytest.raises(MissedFileError, match="downloaded sha256"):
            resolve(m, sdir("documents"))
        assert not (m / out["path"]).exists()
        assert len([p for p in (m / "data").rglob("*") if ".mismatch-" in p.name]) == 1


def test_an_altered_local_only_file_fails_loudly_and_stays(tmp_path: Path) -> None:
    """G2 (D371): on a local-only store there is nothing to re-download: the read raises the
    digest error naming the file and leaves it in place."""
    import stack.store.index as I

    local = tmp_path / "local"
    fs = four_files(local)
    out = next(o for _, _, o in store_index(local).outputs() if o["file_id"] == fs[3].file_id)
    p = local / out["path"]
    bad = _altered(p)
    p.write_bytes(bad)
    with pytest.raises(I.OutputDigestError, match=p.name):
        store_index(local).fetch_output(out)
    with pytest.raises(MissedFileError, match="sha256"):
        resolve(local, sdir("documents"))
    assert p.read_bytes() == bad and not [q for q in (local / "data").rglob("*") if ".mismatch-" in q.name]


def test_a_local_file_is_hashed_once_per_process(tmp_path: Path, fake_env: None,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """G2 (D371): a second read of an unchanged local file does not hash it again, nor does the
    first read after a checked download; a changed file is hashed again."""
    import stack.store.index as I

    calls: list[Path] = []
    digest = I.file_digest

    def counting(p: Path) -> str:
        calls.append(Path(p))
        return digest(p)

    local = tmp_path / "local"
    fs = four_files(local)
    monkeypatch.setattr(I, "file_digest", counting)
    idx = store_index(local)
    out = next(o for _, _, o in idx.outputs() if o["file_id"] == fs[3].file_id)
    assert idx.fetch_output(out) == idx.fetch_output(out) == local / out["path"]
    assert len(calls) == 1
    resolve(local, sdir("documents"))
    assert [c.name for c in calls].count((local / out["path"]).name) == 1
    os.utime(local / out["path"], ns=(1, 1))
    idx.fetch_output(out)
    assert [c.name for c in calls].count((local / out["path"]).name) == 2

    fake = FakeS3()
    upload(local, fake)
    calls.clear()
    with R.using_s3_client(fake):
        m = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        mi = store_index(m, refresh=True)
        assert mi.fetch_output(out) == mi.fetch_output(out) == m / out["path"]
        assert len(calls) == 1  # the download's check; the local copy is not hashed again
