"""T-STORE WP0: the job-name grammar and the S3 layout (stack/store/layout.py, D320), Remote/Store,
open_store and resolve_store (stack/store/remote.py, D321), the fake S3's knobs, and the plan/stage
name checks. No network: S3 is tests/fakes/s3.py."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from stack.plan import Plan
from stack.store import layout as L
from stack.store import remote as R
from stack.store.s3config import S3ConfigError
from tests.fakes.s3 import FAKE_ENV, FAKE_ENV_OLD, SECRET, FakeS3

AT = dt.datetime(2026, 10, 14, 8, 0, 31, tzinfo=dt.UTC)
BUCKET = "stack-data"
URI = f"s3://{BUCKET}/stack-v4"
DIGEST = "3f5a9c" + "0" * 54 + "e9c1"


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)


# ---------------------------------------------------------------- grammar
@pytest.mark.parametrize("id_,comment", [("rw4e1dw22b8kk", None), ("w2wbnyc2kcyg2", "backfill-p1"),
                                         ("rq8d0v2k6m3hx", "add-masks-2"), ("r1", "x")])
def test_head_round_trip(id_: str, comment: str | None) -> None:
    head = L.make_head(AT, id_, comment)
    assert head == f"20261014T0800Z_{id_}" + (f"_{comment}" if comment else "")
    assert L.parse_head(head) == L.Head("20261014T0800Z", id_, comment)
    assert str(L.parse_head(head)) == head
    assert "." not in head


@pytest.mark.parametrize("bad", [("r1", "a.b"), ("r1", "a b"), ("r_1", None), ("R1", None),
                                 ("r1", "X"), ("r1", "a_b")])  # --comment is [a-z0-9-]+ (D372)
def test_head_refuses(bad: tuple[str, str | None]) -> None:
    with pytest.raises(ValueError):
        L.make_head(AT, *bad)


def test_head_refuses_naive_and_bad_stamp() -> None:
    with pytest.raises(ValueError):
        L.make_head(dt.datetime(2026, 10, 14), "r1")
    with pytest.raises(ValueError):
        L.parse_head("2026-10-14_r1")
    # a head written before the comment grammar narrowed still parses
    assert L.parse_head("20261014T0800Z_r1_X_y") == L.Head("20261014T0800Z", "r1", "X_y")


@pytest.mark.parametrize("stage", ["gather", "collect.openalex", "collect.arxiv_sweep", "annotate.screen",
                                   "fold.corpus", "overrides.add", "fold.release"])
def test_run_stem_round_trip(stage: str) -> None:
    head = L.make_head(AT, "rw4e1dw22b8kk", "c-1")
    stem = L.run_stem(head, stage)
    assert stem == f"{head}.{stage}"
    job = L.parse_job_name(stem)
    assert (str(job.head), job.tail, str(job)) == (head, stage, stem)
    for suf in L.RESERVED_SUFFIXES:
        assert L.split_job_file(stem + suf) == (stem, suf)


@pytest.mark.parametrize("plan", ["std-v3", "std-2026-10-13a", "test-plan", "p"])
def test_build_name_round_trip(plan: str) -> None:
    head = L.make_head(AT, "w2wbnyc2kcyg2", "backfill-p1")
    name = L.build_name(head, plan)
    job = L.parse_job_name(name)
    assert (str(job.head), job.tail) == (head, plan)


@pytest.mark.parametrize("stage", ["Gather", "collect.a.b", "x.json.zst", "collect.x-running.json", ".gather", "gather."])
def test_run_stem_refuses(stage: str) -> None:
    with pytest.raises(ValueError):
        L.run_stem("20261014T0800Z_r1", stage)


@pytest.mark.parametrize("plan", ["std_v3", "Std", "std.v3", "x-running.json", ""])
def test_build_name_refuses(plan: str) -> None:
    with pytest.raises(ValueError):
        L.build_name("20261014T0800Z_w1", plan)


@pytest.mark.parametrize("name", ["20261014T0800Z_r1", "20261014T0800Z_r1.", "nohead.gather",
                                  "20261014T0800Z_r1.gather.json.zst", "20261014T0800Z_r1.A"])
def test_parse_job_name_refuses(name: str) -> None:
    with pytest.raises(ValueError):
        L.parse_job_name(name)


def test_split_job_file_refuses_other_names() -> None:
    with pytest.raises(ValueError):
        L.split_job_file("20261014T0800Z_r1.gather.parquet")
    with pytest.raises(ValueError):
        L.split_job_file("junk.json.zst")


# ---------------------------------------------------------------- layout paths
def test_layout_paths(tmp_path: Path) -> None:
    lay = L.StoreLayout(tmp_path)
    build = L.build_name(L.make_head(AT, "w2wbnyc2kcyg2", "backfill-p1"), "std-2026-10-13a")
    stem = L.run_stem(L.make_head("20261014T0801Z", "rw4e1dw22b8kk"), "collect.openalex")
    bdir = f"builds/{build}"
    assert lay.key(lay.build_dir(build)) == bdir
    assert lay.key(lay.build_json(build)) == f"{bdir}/build.json.zst"
    assert lay.key(lay.build_log(build)) == f"{bdir}/build.log.zst"
    assert lay.key(lay.build_marker(build)) == f"{bdir}/build-running.json"
    assert lay.key(lay.run_json(stem, build)) == f"{bdir}/runs/{stem}.json.zst"
    assert lay.key(lay.run_log(stem, build)) == f"{bdir}/runs/{stem}.log.zst"
    assert lay.key(lay.run_marker(stem, build)) == f"{bdir}/runs/{stem}-running.json"
    assert lay.key(lay.run_json(stem)) == f"runs/{stem}.json.zst"
    assert lay.key(lay.run_log(stem)) == f"runs/{stem}.log.zst"
    assert lay.key(lay.run_marker(stem)) == f"runs/{stem}-running.json"
    assert lay.key(lay.flat_blob(DIGEST)) == f"blobs/{DIGEST}"
    assert lay.mirror_file == tmp_path / ".stack-mirror.json"
    assert lay.builds_dir == tmp_path / "builds"
    f = L.make_data_file("documents", "main", "20261014T080102Z", "r1", "full")
    assert lay.staged_file(f) == tmp_path / "data" / "documents" / "main" / f".{f.name}.tmp"
    assert lay.data_file(f) == tmp_path / "data" / "documents" / "main" / "20261014T080102Z_r1.full.parquet"


def test_layout_refuses(tmp_path: Path) -> None:
    lay = L.StoreLayout(tmp_path)
    stem = L.run_stem("20261014T0801Z_r1", "gather")
    with pytest.raises(ValueError):
        lay.flat_blob(DIGEST.upper())
    with pytest.raises(ValueError):
        lay.flat_blob(DIGEST[:-1])
    with pytest.raises(ValueError):
        lay.build_dir(stem.replace(".gather", ".collect.x"))  # a stage, not a plan
    with pytest.raises(ValueError):
        lay.run_json("20261014T0801Z_r1.std-v3")  # a plan, not a stage


def test_layout_key(tmp_path: Path) -> None:
    lay = L.StoreLayout(tmp_path / "st")
    assert lay.key(tmp_path / "st" / "data" / "x.parquet") == "data/x.parquet"
    assert lay.key("data/x.parquet") == "data/x.parquet"
    assert lay.key(tmp_path / "st" / "a" / ".." / "b") == "b"
    for bad in (tmp_path / "other" / "x", tmp_path / "st" / ".." / "x", tmp_path / "st", "../x"):
        with pytest.raises(ValueError):
            lay.key(bad)


# ---------------------------------------------------------------- Remote over the fake
def _remote(fake: FakeS3, prefix: str = "stack-v4") -> R.Remote:
    return R.Remote(BUCKET, prefix, fake)


def test_remote_put_get_head_delete() -> None:
    t = [dt.datetime(2026, 10, 14, 9, tzinfo=dt.UTC)]
    fake = FakeS3(clock=lambda: t[0])
    r = _remote(fake)
    assert r.uri == URI
    r.put_bytes("runs/a.json.zst", b"abc")
    assert fake.objects[(BUCKET, "stack-v4/runs/a.json.zst")] == b"abc"
    assert r.get_bytes("runs/a.json.zst") == b"abc"
    assert r.head("runs/a.json.zst") == R.Obj("runs/a.json.zst", 3, t[0])
    assert r.head("runs/b.json.zst") is None
    with pytest.raises(R.RemoteKeyError):
        r.get_bytes("runs/b.json.zst")
    r.delete("runs/a.json.zst")
    r.delete("runs/a.json.zst")  # S3: deleting an absent key is no error
    assert r.head("runs/a.json.zst") is None
    with pytest.raises(ValueError):
        r.put_bytes("../escape", b"x")


def test_remote_put_file(tmp_path: Path) -> None:
    fake = FakeS3()
    src = tmp_path / "f.parquet"
    src.write_bytes(b"\x00" * 5000)
    _remote(fake).put_file("data/f.parquet", src)
    assert fake.objects[(BUCKET, "stack-v4/data/f.parquet")] == b"\x00" * 5000


def test_remote_list_paginates_and_stays_in_prefix() -> None:
    clock = iter(dt.datetime(2026, 10, 14, 9, i, tzinfo=dt.UTC) for i in range(20))
    fake = FakeS3(page=2, clock=lambda: next(clock))
    r = _remote(fake)
    for i in range(7):
        r.put_bytes(f"runs/r{i}.json.zst", b"x" * i)
    _remote(fake, "stack-v4-other").put_bytes("runs/zz.json.zst", b"other store")
    r.put_bytes("data/d.parquet", b"d")
    got = list(r.list("runs/"))
    assert [o.key for o in got] == [f"runs/r{i}.json.zst" for i in range(7)]
    assert [o.size for o in got] == list(range(7))
    assert got[3].last_modified == dt.datetime(2026, 10, 14, 9, 3, tzinfo=dt.UTC)
    assert len(fake.ops("list")) == 4  # 7 keys at 2 per page
    assert {o.key for o in r.list()} == {o.key for o in got} | {"data/d.parquet"}


def test_remote_download_atomic(tmp_path: Path) -> None:
    fake = FakeS3()
    r = _remote(fake)
    data = os.urandom(3 * (1 << 20) + 17)  # several chunks
    r.put_bytes("blobs/x", data)
    dst = tmp_path / "m" / "blobs" / "x"
    assert r.download("blobs/x", dst) == dst
    assert dst.read_bytes() == data
    assert [p.name for p in dst.parent.iterdir()] == ["x"]  # no temp file left


def test_remote_download_missing_and_failure_leave_nothing(tmp_path: Path) -> None:
    fake = FakeS3()
    r = _remote(fake)
    dst = tmp_path / "blobs" / "x"
    with pytest.raises(R.RemoteKeyError):
        r.download("blobs/x", dst)
    assert not dst.exists()
    r.put_bytes("blobs/x", b"new")
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"old")

    class Torn:
        def __init__(self) -> None:
            self.n = 0

        def read(self, n: int) -> bytes:
            self.n += 1
            if self.n > 1:
                raise ConnectionError("torn")
            return b"ne"

    real = fake.get_object
    fake.get_object = lambda **kw: {**real(**kw), "Body": Torn()}  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        r.download("blobs/x", dst)
    assert dst.read_bytes() == b"old"  # the old file stands; no partial write
    assert [p.name for p in dst.parent.iterdir()] == ["x"]


def test_remote_raises_other_errors() -> None:
    fake = FakeS3()
    r = _remote(fake)
    fake.fail_next("HeadObject")
    with pytest.raises(ClientError):
        r.head("x")
    assert r.head("x") is None  # only the next one failed


# ---------------------------------------------------------------- Store.fetch / push
def test_store_local_only(tmp_path: Path) -> None:
    st = R.Store(tmp_path)
    assert st.fetch("data/x.parquet") is None
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.parquet").write_bytes(b"x")
    assert st.fetch("data/x.parquet") == tmp_path / "data" / "x.parquet"
    assert st.fetch("data/x.parquet", refresh=True) == tmp_path / "data" / "x.parquet"
    st.push("data/x.parquet")  # no remote: nothing to do
    with pytest.raises(FileNotFoundError):
        st.push("data/missing.parquet")
    assert st.key(tmp_path / "data" / "x.parquet") == "data/x.parquet"


def test_store_fetch_pulls_on_read(tmp_path: Path) -> None:
    fake = FakeS3()
    st = R.Store(tmp_path, _remote(fake))
    st.remote.put_bytes("runs/a.json.zst", b"v1")  # type: ignore[union-attr]
    p = st.fetch("runs/a.json.zst")
    assert p == tmp_path / "runs" / "a.json.zst" and p.read_bytes() == b"v1"
    st.remote.put_bytes("runs/a.json.zst", b"v2")  # type: ignore[union-attr]
    assert st.fetch("runs/a.json.zst").read_bytes() == b"v1"  # type: ignore[union-attr]  # present: no GET
    assert fake.ops("get") == ["stack-v4/runs/a.json.zst"]
    assert st.fetch("runs/a.json.zst", refresh=True).read_bytes() == b"v2"  # type: ignore[union-attr]
    assert st.fetch("runs/none.json.zst") is None
    assert not (tmp_path / "runs" / "none.json.zst").exists()
    # refresh with the remote key gone: the local file stays and is returned
    st.remote.delete("runs/a.json.zst")  # type: ignore[union-attr]
    assert st.fetch("runs/a.json.zst", refresh=True).read_bytes() == b"v2"  # type: ignore[union-attr]


def test_store_push(tmp_path: Path) -> None:
    fake = FakeS3()
    st = R.Store(tmp_path, _remote(fake))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "d.parquet").write_bytes(b"d")
    st.push("data/d.parquet")
    assert fake.objects[(BUCKET, "stack-v4/data/d.parquet")] == b"d"
    with pytest.raises(FileNotFoundError):
        st.push("data/e.parquet")
    fake.fail_ops = {"PutObject"}
    with pytest.raises(ClientError):
        st.push("data/d.parquet")


def test_store_push_create_only(tmp_path: Path) -> None:
    """D372: data PUTs carry ``If-None-Match: *``; the fake refuses an existing key
    with 412; a retry of the same bytes passes after a GET and sha256 compare; other bytes are
    never overwritten."""
    fake = FakeS3()
    st = R.Store(tmp_path, _remote(fake))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "d.parquet").write_bytes(b"d")
    st.push("data/d.parquet", create_only=True)
    st.push("data/d.parquet", create_only=True)  # a retry: same bytes, passes
    assert fake.ops("put_refused") == ["stack-v4/data/d.parquet"]  # the retry's 412
    (tmp_path / "data" / "d.parquet").write_bytes(b"other")
    with pytest.raises(R.ObjectConflict, match="not overwritten"):
        st.push("data/d.parquet", create_only=True)
    assert fake.objects[(BUCKET, "stack-v4/data/d.parquet")] == b"d"
    st.push("data/d.parquet")  # an unconditional PUT (records) still overwrites
    assert fake.objects[(BUCKET, "stack-v4/data/d.parquet")] == b"other"


# ---------------------------------------------------------------- open_store / resolve_store
def test_resolve_store_uri_creates_mirror(tmp_path: Path) -> None:
    base = tmp_path / "mirror"
    root = R.resolve_store(URI + "/", mirror_base=base)
    assert root == base / BUCKET / "stack-v4"
    assert json.loads((root / ".stack-mirror.json").read_text()) == {"uri": URI}
    assert (root / ".exclude_from_backup").read_bytes() == b""
    assert R.resolve_store(URI, mirror_base=base) == root  # again: unchanged
    nested = R.resolve_store(f"s3://{BUCKET}/a/b", mirror_base=base)
    assert nested == base / BUCKET / "a" / "b"


def test_resolve_store_refuses_changed_uri(tmp_path: Path) -> None:
    base = tmp_path / "mirror"
    root = R.resolve_store(URI, mirror_base=base)
    (root / ".stack-mirror.json").write_text(json.dumps({"uri": f"s3://{BUCKET}/elsewhere"}))
    with pytest.raises(R.StoreError, match="never changes"):
        R.resolve_store(URI, mirror_base=base)
    assert json.loads((root / ".stack-mirror.json").read_text()) == {"uri": f"s3://{BUCKET}/elsewhere"}


def test_resolve_store_refuses_mirror_path(tmp_path: Path) -> None:
    root = R.resolve_store(URI, mirror_base=tmp_path)
    with pytest.raises(R.StoreError, match="use --store s3://"):
        R.resolve_store(str(root), mirror_base=tmp_path)
    plain = tmp_path / "plain"
    assert R.resolve_store(str(plain), mirror_base=tmp_path) == plain
    assert not plain.exists()  # a local path is never created here
    rel = R.resolve_store("rel/store", mirror_base=tmp_path)
    assert rel.is_absolute()


@pytest.mark.parametrize("uri", ["s3://", "s3://bucket", "s3://bucket/", "s3:///p", "s3://b/a/../x"])
def test_resolve_store_refuses_bad_uri(tmp_path: Path, uri: str) -> None:
    with pytest.raises(R.StoreError):
        R.resolve_store(uri, mirror_base=tmp_path)


def test_open_store_local_and_mirror(tmp_path: Path, fake_env: None) -> None:
    fake = FakeS3()
    local = tmp_path / "local"
    local.mkdir()
    with R.using_s3_client(fake):
        st = R.open_store(local)
        assert st.remote is None and st.root == local
        assert R.open_store(local) is st  # cached per root
        root = R.resolve_store(URI, mirror_base=tmp_path / "mirror")
        m = R.open_store(root)
        assert m.remote is not None and m.remote.uri == URI and m.remote.s3.client is fake
        m.remote.put_bytes("runs/x.json.zst", b"x")
        assert fake.keys() == {"stack-v4/runs/x.json.zst"}
        assert R.open_store(root) is m
    with R.using_s3_client(FakeS3()):
        assert R.open_store(root) is not m  # the hook clears the cache


def test_open_store_needs_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in R.STORE_S3_NAMES:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    root = R.resolve_store(URI, mirror_base=tmp_path)
    with R.using_s3_client(FakeS3()):
        with pytest.raises(S3ConfigError, match="STACK_S3_ACCESS_KEY_ID") as ei:
            R.open_store(root)
    assert SECRET not in str(ei.value)


def test_store_config_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in R.STORE_S3_NAMES:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in FAKE_ENV.items()))
    cfg = R.store_config(URI, {"STACK_ENV_FILE": str(env)})
    assert (cfg.bucket, cfg.base_path, cfg.endpoint) == (BUCKET, "stack-v4", FAKE_ENV["STACK_S3_ENDPOINT_URL"])
    assert SECRET not in repr(cfg)


def _creds(cfg: R.S3Config) -> tuple[str | None, str, str, str | None]:
    return cfg.endpoint, cfg.access_key_id, cfg.secret_access_key, cfg.region


WANT = ("https://fake-endpoint.invalid", "FAKE-KEY-ID", SECRET, "auto")


def test_store_config_new_names_only() -> None:
    assert _creds(R.store_config(URI, dict(FAKE_ENV))) == WANT


def test_store_config_old_names_only() -> None:
    """D726: the older names (a laptop's or the box's .env) are still accepted."""
    assert _creds(R.store_config(URI, dict(FAKE_ENV_OLD))) == WANT


def test_store_config_new_name_wins_per_setting() -> None:
    other = {k: f"old-{v}" for k, v in FAKE_ENV_OLD.items()}
    assert _creds(R.store_config(URI, {**other, **FAKE_ENV})) == WANT
    # per setting: a new name set alone wins its setting; the others fall back to the old names
    mixed = R.store_config(URI, {**other, "STACK_S3_SECRET_ACCESS_KEY": SECRET})
    assert _creds(mixed) == ("old-https://fake-endpoint.invalid", "old-FAKE-KEY-ID", SECRET, "old-auto")
    # an empty or blank new name counts as unset
    assert _creds(R.store_config(URI, {**FAKE_ENV_OLD, "STACK_S3_ACCESS_KEY_ID": " "})) == WANT


def test_store_config_env_file_fallback(tmp_path: Path) -> None:
    """D726: per setting, the new name in either source (environment, then the $STACK_ENV_FILE file) before the
    old name in either."""
    f = tmp_path / ".env"
    f.write_text("".join(f"{k}=old-{v}\n" for k, v in FAKE_ENV_OLD.items())
                 + f"STACK_S3_SECRET_ACCESS_KEY={SECRET}\n")
    got = R.store_config(URI, {"STACK_ENV_FILE": str(f)})
    assert _creds(got) == ("old-https://fake-endpoint.invalid", "old-FAKE-KEY-ID", SECRET, "old-auto")
    f.write_text("".join(f"{k}={v}\n" for k, v in FAKE_ENV_OLD.items()))
    assert _creds(R.store_config(URI, {"STACK_ENV_FILE": str(f)})) == WANT
    # a new name in the file wins over an old name in the environment (an unrelated exported AWS_* key)
    f.write_text("".join(f"{k}={v}\n" for k, v in FAKE_ENV.items()))
    got = R.store_config(URI, {"STACK_ENV_FILE": str(f), **{k: f"env-{v}" for k, v in FAKE_ENV_OLD.items()}})
    assert _creds(got) == WANT
    # a new name in the environment wins over one in the file; with old names only, the environment wins
    got = R.store_config(URI, {"STACK_ENV_FILE": str(f), "STACK_S3_ACCESS_KEY_ID": "env-key"})
    assert _creds(got) == ("https://fake-endpoint.invalid", "env-key", SECRET, "auto")
    f.write_text("".join(f"{k}=file-{v}\n" for k, v in FAKE_ENV_OLD.items()))
    got = R.store_config(URI, {"STACK_ENV_FILE": str(f), "AWS_ACCESS_KEY_ID": "env-key"})
    assert _creds(got) == ("file-https://fake-endpoint.invalid", "env-key", f"file-{SECRET}", "file-auto")


def test_store_config_missing_key_message(tmp_path: Path) -> None:
    with pytest.raises(S3ConfigError) as ei:
        R.store_config(URI, {"STACK_S3_ENDPOINT_URL": "https://e.invalid", "AWS_ACCESS_KEY_ID": "k"})
    assert str(ei.value) == ("store s3://<bucket>/stack-v4: STACK_S3_SECRET_ACCESS_KEY (or AWS_SECRET_ACCESS_KEY) "
                             "not set (environment or $STACK_ENV_FILE (unset))")
    f = tmp_path / ".env"
    f.write_text("STACK_S3_REGION=auto\n")
    with pytest.raises(S3ConfigError) as ei:
        R.store_config(URI, {"STACK_ENV_FILE": str(f)})
    assert str(ei.value) == (f"store s3://<bucket>/stack-v4: STACK_S3_ACCESS_KEY_ID (or AWS_ACCESS_KEY_ID), "
                             f"STACK_S3_SECRET_ACCESS_KEY (or AWS_SECRET_ACCESS_KEY) not set "
                             f"(environment or $STACK_ENV_FILE={f})")


def test_store_config_read_only_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    env = dict(FAKE_ENV)
    whole = R.store_config(URI, env).location_digest()
    bucket_root = R.S3Config("x", f"s3://{BUCKET}", FAKE_ENV["STACK_S3_ENDPOINT_URL"], "", "").location_digest()
    for loc in (whole, bucket_root):
        monkeypatch.setattr(R, "READ_ONLY_LOCATIONS", frozenset({loc}))
        with pytest.raises(S3ConfigError, match="read-only"):
            R.store_config(URI, env)


def test_open_store_bad_binding(tmp_path: Path, fake_env: None) -> None:
    (tmp_path / ".stack-mirror.json").write_text("{not json")
    with pytest.raises(R.StoreError, match="unreadable"):
        R.open_store(tmp_path)
    (tmp_path / ".stack-mirror.json").write_text(json.dumps({"uri": "s3://bucketonly"}))
    with pytest.raises(R.StoreError):
        R.open_store(tmp_path)


# ---------------------------------------------------------------- plan and stage names
def _plan(**kw: object) -> dict[str, object]:
    return {"plan": "p-1", "schema_version": 2, "stages": [{"stage": "gather"}], **kw}


def test_plan_names() -> None:
    assert Plan.model_validate(_plan()).plan == "p-1"
    for bad in ("Std", "std_v3", "std.v3", "x-running.json"):
        with pytest.raises(ValueError):
            Plan.model_validate(_plan(plan=bad))
    with pytest.raises(ValueError):
        Plan.model_validate(_plan(stages=[{"stage": "gather.log.zst"}]))


def _load_check_configs() -> object:
    spec = importlib.util.spec_from_file_location("check_configs_wp0", Path(__file__).resolve().parents[1] / "tools" / "check_configs.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_check_configs_plan_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cc = _load_check_configs()
    assert cc.RESERVED_SUFFIXES == L.RESERVED_SUFFIXES  # type: ignore[attr-defined]
    assert cc.PLAN_NAME_RE == L.PLAN_RE.pattern and cc.STAGE_NAME_RE == L.STAGE_RE.pattern  # type: ignore[attr-defined]
    cc.plan_name_checks()  # type: ignore[attr-defined]
    assert cc.errors == []  # type: ignore[attr-defined]  # the repository's plans pass
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "a.yaml").write_text("plan: a-running.json\nstages:\n  - {stage: gather}\n")
    (tmp_path / "plans" / "b.yaml").write_text("plan: b\nstages:\n  - {stage: collect.x.log.zst}\n")
    (tmp_path / "plans" / "c.yaml").write_text("plan: C_1\nstages: []\n")
    monkeypatch.setattr(cc, "CONFIGS", tmp_path)
    cc.plan_name_checks()  # type: ignore[attr-defined]
    errs = cc.errors  # type: ignore[attr-defined]
    assert len(errs) == 3, errs
    assert "reserved suffix" in errs[0] and "reserved suffix" in errs[1] and "does not match" in errs[2]


# ---------------------------------------------------------------- CLI wiring (--store, --comment)
def _cli(*args: str, mirror: Path) -> object:
    from typer.testing import CliRunner

    from stack.cli import app

    return CliRunner().invoke(app, list(args), env={"STACK_PLAN": None, "STACK_STORE": None,
                                                     "STACK_MIRROR": str(mirror)})


def test_cli_store_uri_resolves_to_the_mirror(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    res = _cli("--store", URI, "status", mirror=mirror)
    root = mirror / BUCKET / "stack-v4"
    assert json.loads((root / ".stack-mirror.json").read_text()) == {"uri": URI}, res.output  # type: ignore[attr-defined]
    res = _cli("status", "--store", f"s3://{BUCKET}/other", mirror=mirror)  # the verb's option
    assert (mirror / BUCKET / "other" / ".stack-mirror.json").is_file(), res.output  # type: ignore[attr-defined]
    res = _cli("--store", str(root), "status", mirror=mirror)
    # D540: the refusal names the URI to use, the bucket masked
    assert res.exit_code == 1 and "use --store s3://<bucket>/stack-v4" in res.output  # type: ignore[attr-defined]
    assert BUCKET not in res.output  # type: ignore[attr-defined]


def test_cli_never_prints_the_bucket(tmp_path: Path, fake_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """D540: ``ls`` (its empty-store line and its refusal), ``head``, ``status`` (text and JSON), a
    refused URI and the missing-credentials error show ``<bucket>``, never the bucket name, also
    where the mirror root (``$STACK_MIRROR/<bucket>/<prefix>``) is printed."""
    mirror = tmp_path / "mirror"
    outs = []
    with R.using_s3_client(FakeS3()):
        for args in (["ls"], ["ls", "data/documents/main/"], ["head", "data/documents/main/"], ["status"],
                     ["status", "--json"]):
            res = _cli("--store", URI, *args, mirror=mirror)
            outs.append(res.output)  # type: ignore[attr-defined]
            assert res.exception is None or isinstance(res.exception, SystemExit), (args, res.output)  # type: ignore[attr-defined]
    assert "no published series directory in " in outs[0] and "/<bucket>/stack-v4" in outs[0], outs[0]
    assert outs[1].startswith("error: data/documents/main/: no published file in ") and "/<bucket>/" in outs[1]
    assert "error:" in outs[2] and "/<bucket>/" in outs[2], outs[2]
    assert "store s3://<bucket>/stack-v4 (mirror " in outs[3], outs[3]
    s = json.loads(outs[4])
    assert s["store"] == "s3://<bucket>/stack-v4" and s["root"].endswith("/<bucket>/stack-v4"), s
    bad = _cli("--store", f"s3://{BUCKET}", "ls", mirror=mirror)  # no prefix: refused
    outs.append(bad.output)  # type: ignore[attr-defined]
    assert bad.exit_code == 1 and "s3://<bucket>" in bad.output  # type: ignore[attr-defined]
    broken = FakeS3()
    broken.fail_ops = {"ListObjectsV2"}  # an S3 client error no verb handles (D544)
    with R.using_s3_client(broken):
        s3err = _cli("--store", f"s3://{BUCKET}/third", "status", mirror=mirror)
    outs.append(s3err.output)  # type: ignore[attr-defined]
    assert s3err.exit_code == 1 and s3err.output.startswith("error: S3: "), s3err.output  # type: ignore[attr-defined]
    monkeypatch.delenv("STACK_S3_SECRET_ACCESS_KEY")
    with R.using_s3_client(FakeS3()):
        nokey = _cli("--store", f"s3://{BUCKET}/other", "ls", mirror=mirror)
    outs.append(nokey.output)  # type: ignore[attr-defined]
    # D544: a missing key is an error line at the CLI boundary, exit 1, not a traceback
    assert nokey.exit_code == 1 and nokey.output == (  # type: ignore[attr-defined]
        "error: store s3://<bucket>/other: STACK_S3_SECRET_ACCESS_KEY (or AWS_SECRET_ACCESS_KEY) not set "
        "(environment or $STACK_ENV_FILE (unset))\n")
    for out in outs:
        assert BUCKET not in out and SECRET not in out, out


def test_mask_s3_text_masks_buckets_and_urls() -> None:
    """D544: botocore text as the CLI prints it: the bucket and every URL (the endpoint, a
    virtual-host URL) masked; the rest kept."""
    R.parse_store_uri(f"s3://{BUCKET}/p")
    msg = (f"Could not connect to the endpoint URL: \"https://{BUCKET}.fake-endpoint.invalid/p?list-type=2\"; "
           f"An error occurred (NoSuchBucket) when calling ListObjectsV2: bucket s3://{BUCKET}/p")
    out = R.mask_s3_text(msg)
    assert out == ('Could not connect to the endpoint URL: "<url>"; An error occurred (NoSuchBucket) when calling '
                   "ListObjectsV2: bucket s3://<bucket>/p"), out


def test_cli_store_uri_refused_when_rebound(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    root = R.resolve_store(URI, mirror_base=mirror)
    (root / ".stack-mirror.json").write_text(json.dumps({"uri": f"s3://{BUCKET}/x"}))
    res = _cli("--store", URI, "status", mirror=mirror)
    assert res.exit_code == 1 and "never changes" in res.output  # type: ignore[attr-defined]


def test_cli_run_comment_and_record_fields(tmp_path: Path) -> None:
    from stack.runs import read_run_records
    from stack.store.parquet import read_header
    from tests.fixtures.checks import build_store

    plan = str(Path(__file__).resolve().parents[1] / "configs" / "plans" / "dev-v2.yaml")
    gone = tmp_path / "gone"
    bad = _cli("run", "echo_test", "--store", str(gone), "--plan", plan, "--create", "--comment", "a.b",
               mirror=tmp_path)
    assert bad.exit_code == 2 and not gone.exists()  # type: ignore[attr-defined]
    res = _cli("run", "echo_test", "--store", str(gone), "--plan", plan, "--create", mirror=tmp_path)
    assert gone.is_dir() and not (gone / "store.json").exists()  # --create: just the directory
    st = tmp_path / "st"
    build_store(st)
    before = {r.run_id for r in read_run_records(st)}
    res = _cli("run", "fold.compact", "--in", "data/documents/main/", "--store", str(st), "--plan", plan,
               "--comment", "try-1", mirror=tmp_path)
    assert res.exit_code == 0, res.output  # type: ignore[attr-defined]
    (rec,) = [r for r in read_run_records(st) if r.run_id not in before]
    assert (rec.comment, rec.upload, rec.excludes) == ("try-1", False, [])
    assert rec.outputs, rec
    for out in rec.outputs.values():
        h = read_header(st / out["path"])
        assert (out["chain_id"], out["series"], out["kind"], out["base"], out["after"], out["comment"]) == (
            h["chain_id"], h["series"], h["kind"], h["base"], h["after"], h["comment"])
        # D322, D372: the data file carries the run's start to the second, not its finish
        assert L.parse_data_key(out["path"]).utc == L.data_stamp(rec.started_at)
        assert L.parse_data_key(out["path"]).comment == "try-1"
    assert any(o["base"] for o in rec.outputs.values())  # compaction sits on a head


def test_run_record_new_fields_round_trip() -> None:
    from stack.runs import RunRecord

    rec = RunRecord(run_id="r1", stage="gather", writer="gather", plan="p", status="ok", started_at=AT,
                    heartbeat_at=AT, code_digest="c", config_digest="d", upload=True, comment="x",
                    excludes=["r2.documents"])
    data = rec.to_json()
    assert (data["upload"], data["comment"], data["excludes"]) == (True, "x", ["r2.documents"])
    assert RunRecord.from_json(data) == rec
    for k in ("upload", "comment", "excludes"):
        del data[k]
    old = RunRecord.from_json(data)  # a record written before D322
    assert (old.upload, old.comment, old.excludes) == (False, None, [])


def test_build_record_comment(tmp_path: Path) -> None:
    from stack.build import run_build

    plan = Plan.model_validate({"plan": "empty-p", "schema_version": 2, "stages": []})
    b = run_build(tmp_path, plan, comment="backfill-p1")
    assert b.data["comment"] == "backfill-p1"
    assert run_build(tmp_path, plan).data["comment"] is None


# ---------------------------------------------------------------- stack status on a mirror (C2, D365)
def test_status_on_a_mirror_reads_only_record_jsons(tmp_path: Path, fake_env: None) -> None:
    """``stack --store s3://… status`` refreshes the index (one LIST of builds/ and runs/), GETs
    record jsons only, and shows live, dead, unknown and finished jobs with their last tick's age,
    spend, and the S3 state: on S3, local-only (a no-upload job), push pending (S3 still holds the
    running json and marker of a job whose final json is only local)."""
    from typer.testing import CliRunner

    from stack.cli import app
    from stack.runs import RunRecord, write_build_record, write_final_record, write_running_record
    from stack.store.index import forget_indexes

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    stamp = [now]
    fake = FakeS3(page=1000, clock=lambda: stamp[0])
    src, mirror = tmp_path / "src", tmp_path / "mirror"
    prefix = "stack-v4"

    def up(p: Path, at: dt.datetime) -> None:
        stamp[0] = at
        fake.put_object(Bucket=BUCKET, Key=f"{prefix}/{p.relative_to(src).as_posix()}", Body=p.read_bytes())

    def marker(p: Path, at: dt.datetime) -> None:
        m = p.with_name(p.name.removesuffix(".json.zst") + "-running.json")
        m.write_text('{"tick": "x"}')
        up(m, at)

    def rec(n: int, status: str, **kw: object) -> RunRecord:
        start = AT + dt.timedelta(hours=n)
        return RunRecord(run_id=f"r{n:012d}", stage="gather", writer="gather", plan="dev-v2", status=status,
                         started_at=start, heartbeat_at=start, finished_at=start if status != "running" else None,
                         code_digest="c" * 64, config_digest="d" * 64, **{"upload": True, **kw})  # type: ignore[arg-type]

    spend = {"usd_micro": {"openrouter/m": 700_000}, "reserved_open_usd_micro": 300_000, "batch_ids": [],
             "abandoned_batch_ids": []}
    up(write_final_record(src, rec(1, "ok", spend=spend)), now - dt.timedelta(hours=3))
    p = write_running_record(src, rec(2, "running", spend=spend))
    up(p, now - dt.timedelta(seconds=30))
    marker(p, now - dt.timedelta(seconds=30))
    p = write_running_record(src, rec(3, "running"))
    up(p, now - dt.timedelta(minutes=10))
    marker(p, now - dt.timedelta(minutes=10))
    up(write_running_record(src, rec(4, "running")), now - dt.timedelta(minutes=1))  # no marker: unknown
    b = {"build_id": "w000000000005", "plan": "dev-v2", "status": "running",
         "started_at": (AT + dt.timedelta(hours=5)).isoformat(), "upload": True}
    bp = write_build_record(src, b)
    up(bp, now - dt.timedelta(seconds=20))
    bp.parent.joinpath("build-running.json").write_text("{}")
    up(bp.parent.joinpath("build-running.json"), now - dt.timedelta(seconds=20))
    p = write_running_record(src, rec(6, "running", spend=spend, build_id="w000000000005"), build=bp.parent.name)
    up(p, now - dt.timedelta(seconds=20))
    marker(p, now - dt.timedelta(seconds=20))
    # a crash after the local final json: S3 holds the running json and the marker
    p = write_running_record(src, rec(7, "running"))
    up(p, now - dt.timedelta(minutes=30))
    marker(p, now - dt.timedelta(minutes=30))
    # data and blobs on S3: status never touches them
    fake.put_object(Bucket=BUCKET, Key=f"{prefix}/data/x.parquet", Body=b"x")
    fake.put_object(Bucket=BUCKET, Key=f"{prefix}/blobs/{DIGEST}", Body=b"x")
    root = R.resolve_store(URI, mirror_base=mirror)
    write_final_record(root, rec(7, "ok"))  # the local final json (never PUT)
    write_final_record(root, rec(8, "ok", upload=False))  # a no-upload job: local-only
    fake.calls.clear()
    forget_indexes()
    with R.using_s3_client(fake):
        res = CliRunner().invoke(app, ["--store", URI, "status", "--json"],
                                 env={"STACK_PLAN": None, "STACK_STORE": None, "STACK_MIRROR": str(mirror)})
    assert res.exit_code == 0, res.output
    s = json.loads(res.stdout)
    assert sorted(fake.ops("list")) == [f"{prefix}/builds/", f"{prefix}/runs/"]
    gets = fake.ops("get")
    assert gets and all(k.endswith(".json.zst") for k in gets), gets
    assert not [k for k in fake.ops("get") + fake.ops("head") if "/data/" in k or "/blobs/" in k]
    by = {j["id"]: j for j in s["jobs"]}
    shown = URI.replace(BUCKET, "<bucket>")  # D540: never the bucket
    assert (s["store"], s["mirror"], s["jobs_total"]) == (shown, True, 7)  # r6 is the build's
    assert {k: (j["liveness"], j["s3"]) for k, j in by.items()} == {
        "r000000000001": ("finished", "s3"), "r000000000002": ("live", "s3"), "r000000000003": ("dead", "s3"),
        "r000000000004": ("unknown", "s3"), "w000000000005": ("live", "s3"), "r000000000007": ("finished", "push pending"),
        "r000000000008": ("finished", "local-only")}
    assert re.fullmatch(r"running, live (3\d|4\d)s", by["r000000000002"]["state"]), by["r000000000002"]
    assert by["r000000000003"]["state"] == "running, DEAD 10m"
    assert (by["w000000000005"]["spend_usd_micro"], by["w000000000005"]["open_usd_micro"]) == (700_000, 300_000)
    assert [r["id"] for r in by["w000000000005"]["active_runs"]] == ["r000000000006"]
    assert (s["local_only"], s["push_pending"]) == (1, 1)
    with R.using_s3_client(fake):
        out = CliRunner().invoke(app, ["--store", URI, "status"], env={
            "STACK_PLAN": None, "STACK_STORE": None, "STACK_MIRROR": str(mirror)}).output
    masked_root = str(root).replace(f"/{BUCKET}/", "/<bucket>/")
    assert out.startswith(f"store {shown} (mirror {masked_root})\n") and "1 local-only, 1 push pending" in out, out
    assert "running, DEAD 10m" in out and "$0.70 +$0.30 open" in out


def test_read_verbs_on_a_fresh_mirror_download_only_what_they_show(tmp_path: Path, fake_env: None) -> None:
    """C2 (D365): ``ls``, ``ls <dir>`` and ``head <dir>`` on a fresh mirror of the check store read the
    index (LIST + record jsons); no data file and no blob is fetched. ``diff`` and ``verify`` read
    the files they compare or check."""
    from typer.testing import CliRunner

    from stack.cli import app
    from stack.store.index import forget_indexes
    from tests.fixtures.checks import build_store

    src = tmp_path / "src"
    build_store(src)
    fake = FakeS3(page=1000)
    for p in sorted(src.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            fake.put_object(Bucket=BUCKET, Key=f"stack-v4/{p.relative_to(src).as_posix()}", Body=p.read_bytes())
    env = {"STACK_PLAN": None, "STACK_STORE": None, "STACK_MIRROR": str(tmp_path / "mirror")}
    for args in (["ls"], ["ls", "data/documents/main/"], ["head", "data/documents/main/"]):
        forget_indexes()
        fake.calls.clear()
        with R.using_s3_client(fake):
            res = CliRunner().invoke(app, ["--store", URI, *args], env=env)
        assert res.exit_code == 0, (args, res.output)
        fetched = fake.ops("get") + fake.ops("head")
        assert fake.ops("list") and (args != ["ls"] or fetched), (args, fake.calls)  # later verbs: jsons synced
        assert not [k for k in fetched if not k.endswith(".json.zst")], (args, fetched)
    assert "data/documents/main/: 5 file(s)" in CliRunner().invoke(app, ["--store", str(src), "ls"], env=env).output


def test_remote_download_repeats_a_truncated_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A body cut short in transit (botocore IncompleteReadError; seen reading text blobs into a fresh mirror) is
    asked again; one that stays short raises after the last repeat, leaving nothing behind."""
    from botocore.exceptions import IncompleteReadError

    monkeypatch.setattr(R, "DOWNLOAD_RETRY_S", (0.0, 0.0))
    fake = FakeS3()
    r = _remote(fake)
    r.put_bytes("blobs/x", b"payload")
    cuts = {"n": 1}

    class Short:
        def read(self, n: int) -> bytes:
            raise IncompleteReadError(actual_bytes=0, expected_bytes=7)

    real = fake.get_object

    def get(**kw: Any) -> dict[str, Any]:
        if cuts["n"] > 0:
            cuts["n"] -= 1
            return {**real(**kw), "Body": Short()}
        return real(**kw)

    fake.get_object = get  # type: ignore[method-assign]
    dst = tmp_path / "blobs" / "x"
    assert r.download("blobs/x", dst).read_bytes() == b"payload"
    cuts["n"] = 3
    dst.unlink()
    with pytest.raises(IncompleteReadError):
        r.download("blobs/x", dst)
    assert not dst.exists() and list(dst.parent.iterdir()) == []
