"""Content-addressed blobs: one flat namespace per store (D360).

A store's blobs are ``blobs/<sha256>`` under its root (``StoreLayout.flat_blob``) and, for an S3
store (a mirror, :mod:`stack.store.remote`), under the same key in the store's own prefix,
``s3://<bucket>/<prefix>/blobs/<sha256>``. Fetched bytes, converted texts, collector record
payloads and gate JSONs all share it; the media type and role live in the row that names a blob.

* ``put(data) -> digest``: on an S3 store, HEAD the remote key and PUT when absent or when its size
  is not ``len(data)`` (a damaged object; a same-size corruption is left to ``get``), by every job,
  upload or not (a blob is immutable and harmless while unreferenced); a local copy does not prove
  a remote one. Then the local copy (temp file + rename). The PUT comes first, so a blob is on S3
  before any row names it; a failed PUT raises and leaves no local file.
* **Transient S3 failures** (D789): a blob's HEAD-then-PUT that fails with
  :func:`~stack.store.s3config.is_s3_transient` (``SlowDown``, a 5xx, a connection error; each attempt
  already carries the client's own retries) is repeated after each of :data:`UPLOAD_RETRY_DELAYS`,
  each scaled by a random factor in [0.5, 1.5) (jitter: upload workers throttled together do not come
  back together); then the failure stands. Both modes, as the publish's data PUT
  (:data:`stack.store.chain.DATA_PUT_RETRY_DELAYS`); a blob is immutable under its digest, so a repeat
  is safe.
* **Deferred** (``BlobStore(store, deferred=True)``, the run's store, :class:`StageContext.blobs
  <stack.stage_runner.StageContext>`; D407, review finding L3-4: a synchronous HEAD + PUT per blob
  cost ~0.13 s each inside a collector's page loop): ``put`` writes the local copy at once (``path``
  and ``get`` in the same run work) and queues the remote HEAD-then-PUT, deduped by digest within
  the process, for :data:`UPLOAD_THREADS` daemon workers that read the bytes back from the local
  file. :meth:`BlobStore.flush` waits for every queued upload and raises the first failure; the
  runner calls it before its publish (before the rename of ``data/.<name>.tmp``), so the order stays
  blobs, data, record and no renamed data file names a blob that is not on S3.
  :meth:`BlobStore.close` (every outcome) stops the workers, joining at most :data:`CLOSE_TIMEOUT`.
  Here a local file may exist with no remote copy (a failed upload, a run that died before its
  flush): a local file never proves an upload, and a later ``put`` of the same bytes HEADs the
  remote again. Every other store (``open_blobs``: migrate, release, links, freeze, gate) is
  synchronous, as above.
* ``path(digest)`` / ``get(digest)``: the local file, else a download from the remote, checked
  against the digest before it is renamed into place (:class:`BlobCorrupt`, nothing kept).
* ``exists(digest)``: local file, else HEAD (whether it can be read here, not whether it is on S3).
  ``held(digests)``: X5's question (which the store holds, and their sizes), one LIST of
  ``blobs/`` on an S3 store, the local files otherwise. ``sizes(digests)``:
  a release's pre-flight (D647), whether each can be read here and its size: the local file's, else one
  LIST of the remote.
* :func:`resolve` (D647): the bytes a release row's digest names, from the row's inline field, else
  the stores the caller names, in order (an export directory's ``blobs/`` first, then e.g. a mirror).

Objects are write-once; nothing here deletes, locally or remotely. No kinds, no settings file, no
LRU or eviction: a container's local blobs stay until it is discarded (D360;
``docs/howto/container-lifecycle.md``, "5. Disk").
"""

from __future__ import annotations

import hashlib
import os
import queue
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from stack.store.remote import RemoteKeyError, Store, open_store
from stack.store.s3config import is_s3_transient, redact_error

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20

#: Remote upload workers of a deferred :class:`BlobStore` (a run's; D407).
UPLOAD_THREADS = 16
#: How long :meth:`BlobStore.close` joins the workers of a run that did not flush (failed or
#: cancelled); they are daemon threads, so an upload still hanging then does not hold process exit.
CLOSE_TIMEOUT = 30.0
#: Waits (s) before each repeat of a blob's HEAD-then-PUT after a transient S3 failure (D789), each
#: scaled by a random factor in [0.5, 1.5); about 4 min in all. The 2026-11-01 production re-sweep
#: minted 79,403 blobs in 110 min on 16 workers and lost the run to one ``SlowDown`` past the client's
#: 4 attempts (botocore's standard mode: at most ~7 s of backoff, less once its retry quota is spent).
UPLOAD_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 120.0)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _jittered(delay: float) -> float:
    return delay * (0.5 + random.random())


class BlobNotFound(KeyError):
    """Raised by ``get``/``path`` when neither the local store nor its remote holds a digest (a
    ``KeyError`` subclass)."""


class BlobCorrupt(RuntimeError):
    """Downloaded bytes whose sha256 is not the digest asked for. Nothing is kept. Deliberately not
    a ``KeyError``/``OSError``/``ValueError``: callers that treat a missing blob as "no data" must
    not swallow a corrupt one."""


def blob_key(digest: str) -> str:
    """``digest`` itself when it is 64 lowercase hex characters (a sha256 hex digest), else
    :class:`ValueError`. Blobs are flat: the digest is the name (D-BLOB1)."""
    if _HEX64.match(digest) is None:
        raise ValueError(f"not a sha256 hex digest (64 lowercase hex chars): {digest!r}")
    return digest


def _key(digest: str) -> str:
    return f"blobs/{blob_key(digest)}"


def _tmp(path: Path) -> Path:
    return path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class BlobStore:
    """The blobs of one :class:`~stack.store.remote.Store` (module docstring). Safe across threads.
    Synchronous: stateless beyond the store; two threads putting one digest both HEAD and may both
    PUT the same bytes, which is harmless. ``deferred``: the upload queue (module docstring)."""

    def __init__(self, store: Store, *, deferred: bool = False) -> None:
        self.store = store
        self.deferred = deferred
        self._cond = threading.Condition()
        self._queued: set[str] = set()  # every digest ever queued by this store (the dedupe)
        self._pending = 0
        self._errors: list[BaseException] = []
        self._q: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._workers: list[threading.Thread] = []
        self._closed = False
        self._repeats = 0  # transient-failure repeats of uploads (D789)

    def local(self, digest: str) -> Path:
        """Where ``digest`` lives locally, present or not."""
        return self.store.path(_key(digest))

    def put(self, data: bytes) -> str:
        """Store ``data``; returns its sha256 hex digest."""
        digest = hashlib.sha256(data).hexdigest()
        key = _key(digest)
        remote = self.store.remote
        if remote is not None and self.deferred:
            self._write_local(digest, data)
            self._enqueue(digest)
            return digest
        if remote is not None:
            def up() -> None:
                hit = remote.head(key)
                if hit is None or hit.size != len(data):  # absent, or a damaged object under the key
                    remote.put_bytes(key, data)  # raises on failure: no local file is written

            self._repeating(digest, up)
        self._write_local(digest, data)
        return digest

    def _repeating(self, digest: str, up: Callable[[], None]) -> None:
        """``up()``, repeated after a transient S3 failure (:data:`UPLOAD_RETRY_DELAYS`, jittered; D789);
        the last failure, any other, or one after :meth:`close` raises. The first repeat and every
        power-of-two-th of the store's repeats are said on stderr."""
        for delay in (*UPLOAD_RETRY_DELAYS, None):
            try:
                up()
                return
            except Exception as exc:
                if delay is None or self._closed or not is_s3_transient(exc):
                    raise
                wait = _jittered(delay)
                with self._cond:
                    self._repeats += 1
                    n = self._repeats
                if n & (n - 1) == 0:
                    from stack.store.job import say

                    say(f"warning: blob {digest[:12]} upload failed ({redact_error(exc)}); retrying in {wait:.1f} s "
                        f"({n} blob upload repeats so far)")
                _sleep(wait)

    def _write_local(self, digest: str, data: bytes) -> None:
        path = self.local(digest)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = _tmp(path)
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

    def _enqueue(self, digest: str) -> None:
        with self._cond:
            if self._closed:
                raise RuntimeError("blob store closed: no further uploads")
            if digest in self._queued:
                return
            self._queued.add(digest)
            self._pending += 1
            inline = False
            if len(self._workers) < UPLOAD_THREADS and len(self._workers) < self._pending:
                t = threading.Thread(target=self._work, name=f"blob-upload-{len(self._workers)}", daemon=True)
                try:
                    t.start()
                except RuntimeError:  # can't start new thread (D786): the workers there take it, if any
                    if not self._workers:  # none: this caller uploads it, as a store without the queue does
                        self._pending -= 1
                        inline = True
                else:
                    self._workers.append(t)  # only a started thread: close() joins and wakes these
        if inline:
            try:
                self._upload(digest)
            except BaseException:
                with self._cond:
                    self._queued.discard(digest)  # not uploaded: a later put of it tries again
                raise
            return
        self._q.put(digest)

    def _upload(self, digest: str) -> None:
        remote = self.store.remote
        assert remote is not None
        key, path = _key(digest), self.local(digest)
        size = path.stat().st_size

        def up() -> None:
            hit = remote.head(key)
            if hit is None or hit.size != size:  # absent, or a damaged object under the key
                remote.put_file(key, path)  # streamed from the local copy: memory stays bounded

        self._repeating(digest, up)

    def _work(self) -> None:
        while (digest := self._q.get()) is not None:
            try:
                if not self._closed:
                    self._upload(digest)
            except BaseException as exc:  # noqa: BLE001 — reported by flush()
                with self._cond:
                    self._errors.append(exc)
            finally:
                with self._cond:
                    self._pending -= 1
                    self._cond.notify_all()

    def flush(self) -> None:
        """Wait for every queued upload (a deferred store; a no-op otherwise); raise the first
        failure (again on every later call: a run whose upload failed must not publish)."""
        with self._cond:
            while self._pending:
                self._cond.wait()
            if self._errors:
                raise self._errors[0]

    def close(self, timeout: float | None = None) -> None:
        """Stop the upload workers (a deferred store): queued uploads not started are dropped, and
        the workers are joined for at most ``timeout`` (default :data:`CLOSE_TIMEOUT`) in all.
        Called on every outcome; after a :meth:`flush` there is nothing left to wait for."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers)
        for _ in workers:
            self._q.put(None)
        deadline = time.monotonic() + (CLOSE_TIMEOUT if timeout is None else timeout)
        for t in workers:
            t.join(max(0.0, deadline - time.monotonic()))

    def path(self, digest: str) -> Path:
        """The local file of ``digest``, downloaded first when absent (sha256-checked)."""
        path = self.local(digest)
        if path.is_file():
            return path
        remote = self.store.remote
        if remote is None:
            raise BlobNotFound(digest)
        tmp = _tmp(path)
        try:
            remote.download(_key(digest), tmp)
        except RemoteKeyError:
            raise BlobNotFound(digest) from None
        try:
            got = _file_sha256(tmp)
            if got != digest:
                raise BlobCorrupt(f"blob {digest}: the downloaded {tmp.stat().st_size} bytes hash to {got}")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    def get(self, digest: str) -> bytes:
        """The bytes of ``digest`` (:meth:`path`)."""
        return self.path(digest).read_bytes()

    def exists(self, digest: str) -> bool:
        """Whether ``digest`` is here or on the remote (no download)."""
        if self.local(digest).is_file():
            return True
        remote = self.store.remote
        return remote is not None and remote.head(_key(digest)) is not None

    def held(self, digests: Iterable[str]) -> dict[str, int]:
        """``{digest: size}`` of the digests of ``digests`` the store holds: on an S3 store the sizes
        one LIST of ``blobs/`` returns (the remote is the store; a local-only file does not count),
        else the local files' sizes. A digest the store does not hold is absent. A malformed digest
        raises :class:`ValueError`."""
        want = {blob_key(d) for d in digests}
        remote = self.store.remote
        if not want:
            return {}
        if remote is None:
            return {d: p.stat().st_size for d in want if (p := self.local(d)).is_file()}
        return {d: o.size for o in remote.list("blobs/") if (d := o.key.removeprefix("blobs/")) in want}

    def sizes(self, digests: Iterable[str]) -> tuple[dict[str, int], set[str]]:
        """``({digest: size}, missing)`` over the distinct ``digests``: a local file gives its size
        (a local-only copy counts: it can be read here, unlike :meth:`held`'s remote view); the rest
        come from one LIST of the remote's ``blobs/``, when there is a remote; what neither holds is
        missing. A LIST failure raises (it is not "absent")."""
        want = {blob_key(d) for d in digests}
        sizes: dict[str, int] = {}
        rest: set[str] = set()
        for d in want:
            p = self.local(d)
            if p.is_file():
                sizes[d] = p.stat().st_size
            else:
                rest.add(d)
        remote = self.store.remote
        if rest and remote is not None:
            for o in remote.list("blobs/"):
                d = o.key.removeprefix("blobs/")
                if d in rest:
                    sizes[d] = o.size
        return sizes, want - set(sizes)


def _inline_of(row: Mapping[str, Any], digest: str) -> bytes | None:
    holders = [*((row.get("content") or {}).get("snapshots") or []), *((row.get("inband") or {}).get("entries") or [])]
    for h in holders:
        for part in ("text", "raw"):
            ref = h.get(part)
            if isinstance(ref, Mapping) and ref.get("sha256") == digest and ref.get("inline") is not None:
                return bytes(ref["inline"])
    return None


def resolve(row: Mapping[str, Any], sha256: str | None, stores: Sequence[Path | str] = ()) -> bytes | None:
    """The bytes ``sha256`` names for a release row (a dict as read from ``corpus.parquet``; D647):
    the ``inline`` of a ``raw``/``text`` BLOB of the row's snapshots or in-band entries whose ``sha256`` it
    is, else each store root of ``stores`` in order through
    :func:`open_blobs` (an export directory is a local store: its ``blobs/<sha256>``; a mirror
    downloads on read), else ``None``. ``None`` for a NULL reference. The bytes are checked against
    ``sha256`` (:class:`BlobCorrupt`). ``release.json`` names no store (D540): the caller does."""
    if sha256 is None:
        return None
    data = _inline_of(row, sha256)
    if data is None:
        for root in stores:
            try:
                data = open_blobs(root).get(sha256)
                break
            except BlobNotFound:
                continue
    if data is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise BlobCorrupt(f"blob {sha256}: the {len(data)} bytes found hash to {hashlib.sha256(data).hexdigest()}")
    return data


def open_blobs(store_root: Path | str, *, deferred: bool = False) -> BlobStore:
    """The blobs of the store at ``store_root`` (:func:`stack.store.remote.open_store`: a mirror's
    blobs go to its S3 prefix, a local-only store's stay in ``blobs/``). ``deferred``: a run's
    upload queue (module docstring); only the stage runner asks for it."""
    return BlobStore(open_store(store_root), deferred=deferred)
