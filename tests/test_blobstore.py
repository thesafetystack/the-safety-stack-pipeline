"""Flat blobs per store (T-STORE D360-D362).

``blobs/<sha256>`` locally and, for an S3 store (a mirror), at ``<prefix>/blobs/<sha256>`` in the
store's own bucket. ``put`` HEADs the remote key and PUTs when absent (a local copy proves nothing
remote), then writes the local copy; ``get``/``path`` download on a miss, sha256-checked; X5 asks
one LIST. Every S3 test runs on the in-memory fake (``tests/fakes/s3.py``): no network.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from stack.store.blobs import BlobCorrupt, BlobNotFound, BlobStore, blob_key, open_blobs
from stack.store.layout import MIRROR_FILE
from stack.store.remote import using_s3_client
from tests.fakes.s3 import FAKE_ENV, FakeS3

_DATA = b"stack v4 blob payload"
_DIGEST = hashlib.sha256(_DATA).hexdigest()
BUCKET, PREFIX = "fake-store-bucket", "stores/t1"


def _rkey(digest: str) -> str:
    return f"{PREFIX}/blobs/{digest}"


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)


@pytest.fixture
def mirror(tmp_path: Path, fake_env: None) -> Iterator[tuple[Path, FakeS3]]:
    """A mirror root bound to ``s3://fake-store-bucket/stores/t1`` and the fake that holds it."""
    root = tmp_path / "mirror"
    root.mkdir()
    (root / MIRROR_FILE).write_text(json.dumps({"uri": f"s3://{BUCKET}/{PREFIX}"}))
    fake = FakeS3(page=4)
    with using_s3_client(fake):
        yield root, fake


# ---------------------------------------------------------------- local-only store
def test_local_put_get_exists_and_flat_layout(tmp_path: Path) -> None:
    blobs = open_blobs(tmp_path)
    assert blobs.store.remote is None
    assert not blobs.exists(_DIGEST)
    assert blobs.put(_DATA) == _DIGEST
    assert (tmp_path / "blobs" / _DIGEST).read_bytes() == _DATA  # flat: no kind directory
    assert blobs.get(_DIGEST) == _DATA and blobs.exists(_DIGEST)
    assert blobs.path(_DIGEST) == tmp_path / "blobs" / _DIGEST
    assert blobs.put(_DATA) == _DIGEST  # idempotent
    assert sorted(p.name for p in (tmp_path / "blobs").iterdir()) == [_DIGEST]  # no temp files left


def test_local_held_and_errors(tmp_path: Path) -> None:
    blobs = open_blobs(tmp_path)
    other = "0" * 64
    blobs.put(_DATA)
    assert blobs.held([_DIGEST, other]) == {_DIGEST: len(_DATA)}
    assert blobs.held([]) == {}
    with pytest.raises(BlobNotFound) as ei:
        blobs.get(other)
    assert isinstance(ei.value, KeyError)  # callers that treat a miss as "no data" catch KeyError
    for bad in ("", "ABC", "a" * 63, "g" * 64, "../" + "a" * 61):
        with pytest.raises(ValueError):
            blobs.get(bad)
        with pytest.raises(ValueError):
            blob_key(bad)
    with pytest.raises(ValueError):
        blobs.held(["nope"])


# ---------------------------------------------------------------- S3 store (mirror)
def test_put_on_a_fresh_mirror_heads_then_puts(mirror: tuple[Path, FakeS3]) -> None:
    root, fake = mirror
    blobs = open_blobs(root)
    assert blobs.store.remote is not None
    assert blobs.put(_DATA) == _DIGEST
    assert fake.calls == [("head", _rkey(_DIGEST)), ("put", _rkey(_DIGEST))]
    assert fake.objects[(BUCKET, _rkey(_DIGEST))] == _DATA
    assert (root / "blobs" / _DIGEST).read_bytes() == _DATA


def test_second_put_heads_and_skips(mirror: tuple[Path, FakeS3]) -> None:
    root, fake = mirror
    blobs = open_blobs(root)
    blobs.put(_DATA)
    fake.calls.clear()
    assert blobs.put(_DATA) == _DIGEST
    assert fake.calls == [("head", _rkey(_DIGEST))]


def test_a_local_blob_absent_remotely_is_put(mirror: tuple[Path, FakeS3]) -> None:
    """D361: a local copy does not prove a remote one (today's shortcut is gone)."""
    root, fake = mirror
    (root / "blobs").mkdir()
    (root / "blobs" / _DIGEST).write_bytes(_DATA)
    open_blobs(root).put(_DATA)
    assert fake.ops("put") == [_rkey(_DIGEST)]
    assert fake.objects[(BUCKET, _rkey(_DIGEST))] == _DATA


def test_a_remote_object_of_the_wrong_size_is_put_again(mirror: tuple[Path, FakeS3]) -> None:
    """F1: HEAD finding the key is not enough; a size other than ``len(data)`` (a truncated or
    damaged object) gets a fresh PUT of the right bytes."""
    root, fake = mirror
    fake.objects[(BUCKET, _rkey(_DIGEST))] = _DATA[:-1]  # truncated
    open_blobs(root).put(_DATA)
    assert fake.calls == [("head", _rkey(_DIGEST)), ("put", _rkey(_DIGEST))]
    assert fake.objects[(BUCKET, _rkey(_DIGEST))] == _DATA


def test_a_no_upload_jobs_put_still_puts(mirror: tuple[Path, FakeS3], monkeypatch: pytest.MonkeyPatch) -> None:
    """D361: blobs go up from every job on an S3 store, upload or not. A no-upload job reaches its
    blobs through the same ``ctx.blobs`` (the runner's :func:`open_blobs`); the stage below runs
    with ``upload=False`` (``--no-upload``, D352) and its blob is in the bucket, nothing else."""
    import textwrap

    from stack import stage_runner
    from stack.plan import load_plan
    from stack.stage_runner import run_stage
    from tests.synth.records import sdir
    from tests.test_stage_runner import DEV_PLAN, seed_documents

    root, fake = mirror
    seed_documents(root)
    wf = root.parent / "workflows"
    (wf / "blobby").mkdir(parents=True)
    (wf / "blobby" / "main.py").write_text(textwrap.dedent("""
        from stack.stage_runner import StageResult
        def run(ctx):
            ctx.blobs.put(b"raw from a stage")
            return StageResult(scope_complete=True, rows_touched=0)
    """))
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", wf)
    rec = run_stage(root, load_plan(DEV_PLAN), "blobby", [sdir("documents")], upload=False)
    assert rec.status == "ok", rec.error
    assert rec.upload is False
    d = hashlib.sha256(b"raw from a stage").hexdigest()
    assert fake.ops("put") == [_rkey(d)]
    assert (root / "blobs" / d).is_file()


def test_failed_put_raises_and_leaves_no_local_file(mirror: tuple[Path, FakeS3], slept: list[float]) -> None:
    """A PUT failing past every repeat (D789) raises; no local file is written."""
    from stack.store.blobs import UPLOAD_RETRY_DELAYS

    root, fake = mirror
    fake.fail_ops = {"PutObject"}
    with pytest.raises(Exception, match="InternalError"):
        open_blobs(root).put(_DATA)
    assert not (root / "blobs" / _DIGEST).exists()
    assert len(fake.ops("put")) == len(UPLOAD_RETRY_DELAYS) + 1 and len(slept) == len(UPLOAD_RETRY_DELAYS)


# ---------------------------------------------------------------- transient failures (D789)
@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The blob store's repeat waits, recorded instead of slept."""
    from stack.store import blobs as B

    waits: list[float] = []
    monkeypatch.setattr(B, "_sleep", waits.append)
    return waits


@pytest.mark.parametrize("deferred", [False, True])
def test_a_throttled_upload_is_repeated_with_jittered_backoff(
        mirror: tuple[Path, FakeS3], slept: list[float], deferred: bool) -> None:
    """``SlowDown`` past the client's own retries (three times on one blob): the HEAD-then-PUT is
    repeated after each of the first three :data:`UPLOAD_RETRY_DELAYS`, each scaled into [0.5, 1.5),
    and the blob lands. Before D789 the first ``SlowDown`` raised (the deferred store kept it for
    ``flush``, which failed the run)."""
    from stack.store.blobs import UPLOAD_RETRY_DELAYS

    root, fake = mirror
    fake.fail_code = "SlowDown"
    fake.fail_next("PutObject", 3)
    blobs = open_blobs(root, deferred=deferred)
    assert blobs.put(_DATA) == _DIGEST
    blobs.flush()
    blobs.close()
    assert fake.objects[(BUCKET, _rkey(_DIGEST))] == _DATA
    assert fake.calls == [("head", _rkey(_DIGEST)), ("put", _rkey(_DIGEST))] * 4
    assert len(slept) == 3
    assert all(0.5 * d <= w < 1.5 * d for w, d in zip(slept, UPLOAD_RETRY_DELAYS, strict=False)), slept


def test_a_throttled_head_is_repeated_too(mirror: tuple[Path, FakeS3], slept: list[float]) -> None:
    root, fake = mirror
    fake.fail_code = "SlowDown"
    fake.fail_next("HeadObject", 2)
    blobs = open_blobs(root, deferred=True)
    blobs.put(_DATA)
    blobs.flush()
    blobs.close()
    assert fake.objects[(BUCKET, _rkey(_DIGEST))] == _DATA and len(slept) == 2


def test_many_deferred_uploads_ride_out_a_throttling_burst(mirror: tuple[Path, FakeS3], slept: list[float]) -> None:
    """40 blobs on the deferred store's workers, as many PUTs answered ``SlowDown`` as a blob has
    repeats (however the workers share them, none runs out): every blob lands, ``flush`` does not raise."""
    from stack.store.blobs import UPLOAD_RETRY_DELAYS

    root, fake = mirror
    fake.fail_code = "SlowDown"
    fake.fail_next("PutObject", len(UPLOAD_RETRY_DELAYS))
    blobs = open_blobs(root, deferred=True)
    digests = [blobs.put(f"burst {i}".encode()) for i in range(40)]
    blobs.flush()
    blobs.close()
    assert all((BUCKET, _rkey(d)) in fake.objects for d in digests)
    assert len(slept) == len(UPLOAD_RETRY_DELAYS)


def test_a_lasting_throttle_fails_the_flush_after_every_repeat(mirror: tuple[Path, FakeS3], slept: list[float]) -> None:
    from botocore.exceptions import ClientError

    from stack.store.blobs import UPLOAD_RETRY_DELAYS
    from stack.store.s3config import is_s3_transient

    root, fake = mirror
    fake.fail_code = "SlowDown"
    fake.fail_ops = {"PutObject"}
    blobs = open_blobs(root, deferred=True)
    blobs.put(_DATA)
    with pytest.raises(ClientError, match="SlowDown") as ei:
        blobs.flush()
    blobs.close()
    assert is_s3_transient(ei.value)  # the run ends failed[s3_transient], as before
    assert len(fake.ops("put")) == len(UPLOAD_RETRY_DELAYS) + 1 and len(slept) == len(UPLOAD_RETRY_DELAYS)


@pytest.mark.parametrize("deferred", [False, True])
def test_a_non_transient_failure_is_not_repeated(mirror: tuple[Path, FakeS3], slept: list[float],
                                                 deferred: bool) -> None:
    from botocore.exceptions import ClientError

    root, fake = mirror
    fake.fail_code = "AccessDenied"
    fake.fail_next("PutObject", 1)
    blobs = open_blobs(root, deferred=deferred)
    with pytest.raises(ClientError, match="AccessDenied"):
        blobs.put(_DATA)
        blobs.flush()
    blobs.close()
    assert fake.ops("put") == [_rkey(_DIGEST)] and slept == []


def test_a_closed_store_does_not_repeat(mirror: tuple[Path, FakeS3], slept: list[float]) -> None:
    """After ``close`` (a run that failed or was cancelled) a worker's failed upload is not repeated."""
    root, fake = mirror
    blobs = open_blobs(root, deferred=True)
    blobs._write_local(_DIGEST, _DATA)
    blobs.close()
    fake.fail_code = "SlowDown"
    fake.fail_next("PutObject", 1)
    with pytest.raises(Exception, match="SlowDown"):
        blobs._upload(_DIGEST)  # what a worker still running after close does
    assert slept == [] and len(fake.ops("put")) == 1


def test_get_downloads_checks_and_keeps(mirror: tuple[Path, FakeS3]) -> None:
    root, fake = mirror
    fake.objects[(BUCKET, _rkey(_DIGEST))] = _DATA
    blobs = open_blobs(root)
    assert blobs.exists(_DIGEST)  # a HEAD, no download
    assert fake.ops("get") == [] and not (root / "blobs" / _DIGEST).exists()
    assert blobs.get(_DIGEST) == _DATA
    assert (root / "blobs" / _DIGEST).read_bytes() == _DATA
    fake.calls.clear()
    assert blobs.get(_DIGEST) == _DATA and fake.calls == []  # served locally now


def test_a_corrupt_remote_body_is_refused_and_not_kept(mirror: tuple[Path, FakeS3]) -> None:
    root, fake = mirror
    fake.objects[(BUCKET, _rkey(_DIGEST))] = _DATA[:-1]  # truncated
    blobs = open_blobs(root)
    with pytest.raises(BlobCorrupt) as ei:
        blobs.get(_DIGEST)
    assert not isinstance(ei.value, (KeyError, OSError, ValueError))
    assert not (root / "blobs").exists() or list((root / "blobs").iterdir()) == []


def test_get_absent_on_both_sides_is_blob_not_found(mirror: tuple[Path, FakeS3]) -> None:
    root, _ = mirror
    blobs = open_blobs(root)
    with pytest.raises(BlobNotFound):
        blobs.get(_DIGEST)
    assert not blobs.exists(_DIGEST)
    assert not (root / "blobs").exists() or list((root / "blobs").iterdir()) == []


def test_held_is_one_list_of_the_remote(mirror: tuple[Path, FakeS3]) -> None:
    """X5's question on an S3 store: the remote is the store, so a local-only file is not held."""
    root, fake = mirror
    blobs = open_blobs(root)
    have = [blobs.put(f"blob {i}".encode()) for i in range(10)]  # > page size: LIST paginates
    local_only = hashlib.sha256(b"local only").hexdigest()
    (root / "blobs" / local_only).write_bytes(b"local only")
    absent = "f" * 64
    fake.calls.clear()
    assert set(blobs.held([*have, local_only, absent])) == set(have)
    assert {op for op, _ in fake.calls} == {"list"}
    assert all(k == f"{PREFIX}/blobs/" for k in fake.ops("list"))
    # X5's sizes come from the same LIST: the remote object's, a damaged one's included
    fake.put_object(Bucket=BUCKET, Key=_rkey(have[0]), Body=b"damaged")
    fake.calls.clear()
    assert blobs.held([*have, local_only, absent]) == {
        d: len(b"damaged" if i == 0 else f"blob {i}".encode()) for i, d in enumerate(have)}
    assert {op for op, _ in fake.calls} == {"list"}


def test_parallel_puts(mirror: tuple[Path, FakeS3]) -> None:
    """8 threads put overlapping blobs: every digest ends up remote and local, no temp file left."""
    root, fake = mirror
    blobs = open_blobs(root)
    errors: list[BaseException] = []

    def work(t: int) -> None:
        try:
            for i in range(30):
                blobs.put(f"blob {(t * 30 + i) % 50}".encode())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    want = {hashlib.sha256(f"blob {i}".encode()).hexdigest() for i in range(50)}
    assert {k.rsplit("/", 1)[1] for k in fake.keys()} == want
    assert {p.name for p in (root / "blobs").iterdir()} == want


def test_blob_store_over_an_explicit_store(tmp_path: Path) -> None:
    from stack.store.remote import Store

    blobs = BlobStore(Store(tmp_path))
    assert blobs.get(blobs.put(_DATA)) == _DATA


# ---------------------------------------------------------------- X5 on an S3 store
def test_x5_against_a_remote_listing(tmp_path: Path, fake_env: None) -> None:
    """A store whose blobs are only remote passes X5 without downloading; a digest the remote
    lacks fails it, even when the file is still local."""
    from stack.checks.verify import run_checks
    from tests.fixtures.checks import build_store

    root = tmp_path / "store"
    build_store(root)
    fake = FakeS3(page=50)
    files = sorted((root / "blobs").iterdir())
    assert len(files) > 3
    for p in files:
        fake.objects[(BUCKET, _rkey(p.name))] = p.read_bytes()
        p.unlink()
    (root / MIRROR_FILE).write_text(json.dumps({"uri": f"s3://{BUCKET}/{PREFIX}"}))
    with using_s3_client(fake):
        (res,) = run_checks(root, names=["blob_resolution"])
        assert res.status == "ok", res.items
        assert fake.ops("get") == [] and fake.ops("head") == []  # one LIST, no download
        gone = files[0]
        gone.write_bytes(fake.objects.pop((BUCKET, _rkey(gone.name))))  # local, not remote
        (res,) = run_checks(root, names=["blob_resolution"])
    assert res.status == "failed"
    assert any(gone.name[:12] in str(i) for i in res.items), res.items


@pytest.mark.parametrize("allowed", [0, 1])
def test_a_deferred_store_uploads_everything_when_an_upload_thread_cannot_start(
        mirror: tuple[Path, FakeS3], monkeypatch: pytest.MonkeyPatch, allowed: int) -> None:
    """D786: a container at its task limit refuses ``Thread.start``. A worker that did not start is not kept
    (``close`` would join it, a later put count it); with no worker at all the caller uploads, as a store
    without the queue does. Before, the failed start raised out of ``put`` (a run's ``code_bug``)."""
    from stack.store.remote import open_store
    from tests.test_stage_runner import failing_thread_starts

    root, fake = mirror
    started = failing_thread_starts(monkeypatch, "blob-upload", allowed=allowed)
    gate, log = threading.Event(), fake._log

    def held(op: str, key: str) -> None:  # an upload worker waits until every put is queued: the queue grows
        if threading.current_thread().name.startswith("blob-upload"):
            gate.wait(5)
        log(op, key)

    monkeypatch.setattr(fake, "_log", held)
    blobs = BlobStore(open_store(root), deferred=True)
    data = [f"blob {i}".encode() for i in range(6)]
    digests = [blobs.put(d) for d in data]
    gate.set()
    blobs.flush()
    blobs.close()
    assert len(blobs._workers) == len(started) == allowed
    assert all(t.is_alive() is False for t in blobs._workers)
    assert {fake.objects[(BUCKET, _rkey(g))] for g in digests} == set(data)
