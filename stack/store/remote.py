"""A store's local root and, for an S3 store, its remote (D321; ``docs/OPERATE.md``,
"Stores and the mirror").

A store is a directory. A **local-only** store is any directory without ``.stack-mirror.json``. A
**mirror** is the local copy of one S3 store ``s3://<bucket>/<prefix>/``: the same keys under
``$STACK_MIRROR/<bucket>/<prefix>/``, bound by ``.stack-mirror.json`` = ``{"uri": "s3://bucket/prefix"}``
(written once by :func:`resolve_store`, never changed, never uploaded). A key present locally was
downloaded or written here; an absent one is fetched on read (:meth:`Store.fetch`).

* :class:`Remote`: one store prefix in one bucket, over :class:`stack.store.backend.S3Backend` (the
  one S3 implementation): ``put_bytes``, ``put_file``, ``get_bytes``, ``download`` (atomic),
  ``head``, ``list`` (paginated), ``delete``.
* :class:`Store`: ``root`` plus ``remote`` (``None`` for a local-only store): ``key``, ``path``,
  ``fetch`` (pull on read), ``push`` (PUT the local file).
* :func:`open_store`: the :class:`Store` of a root, one per root per process; the client has explicit
  keys (:func:`store_config`), never boto3's ambient chain; tests inject a fake with
  :func:`using_s3_client`.
* :func:`resolve_store`: a ``--store`` value (a path or an ``s3://`` URI) to the local root.

No locks, no eviction, no prefetch. Nothing here prints or raises a credential value, and no message
names the bucket (D540): every bucket :func:`parse_store_uri` has seen is shown as ``<bucket>``
(:func:`mask_buckets`, :func:`shown_uri`; the CLI masks its output with them too).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stack.store.backend import BackendKeyError, PreconditionFailed, S3Backend, check_key
from stack.store.layout import MIRROR_FILE, StoreLayout
from stack.store.s3config import (
    ENV_FILE_ENV,
    READ_ONLY_LOCATIONS,
    S3Config,
    S3ConfigError,
    env_file_values,
    is_s3_transient,
)

#: The settings a store's client needs, each as ``(name, older name still accepted)`` (D726); the bucket and
#: prefix come from the store URI. Per setting, the new name in either source before the old name in either
#: (environment, then the ``$STACK_ENV_FILE`` file), so an unrelated exported ``AWS_*`` key cannot override a
#: ``STACK_S3_*`` one.
STORE_S3_SETTINGS = (("STACK_S3_ENDPOINT_URL", "STACK_S3_ENDPOINT"),
                     ("STACK_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
                     ("STACK_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
                     ("STACK_S3_DEFAULT_REGION", "STACK_S3_REGION"))
#: The older name each store setting is still accepted under.
STORE_S3_FALLBACK = dict(STORE_S3_SETTINGS)
#: Every name the store settings are read under, new and old.
STORE_S3_NAMES = tuple(n for pair in STORE_S3_SETTINGS for n in pair)
#: Written empty beside ``.stack-mirror.json``: a mirror is a cache of S3, not something to back up.
EXCLUDE_FROM_BACKUP = ".exclude_from_backup"
_CHUNK = 1 << 20


#: What a message or an output shows in place of a bucket name (D540).
BUCKET_MASK = "<bucket>"
_buckets: set[str] = set()


def mask_buckets(text: str) -> str:
    """``text`` with every bucket name this process parsed (:func:`parse_store_uri`) shown as
    :data:`BUCKET_MASK`: after ``s3://`` and as a path component (a mirror root is
    ``$STACK_MIRROR/<bucket>/<prefix>``). The bucket name is kept out of every output (D540)."""
    for b in sorted(_buckets, key=len, reverse=True):
        e = re.escape(b)
        text = re.sub(rf"(?<=s3://){e}(?![A-Za-z0-9.-])", BUCKET_MASK, text)
        text = re.sub(rf"(?<=[/\\]){e}(?=[/\\])", BUCKET_MASK, text)
    return text


def mask_s3_text(text: str) -> str:
    """Text from an S3 client (a botocore message) as the CLI may print it: :func:`mask_buckets`, and
    every http(s) URL (the endpoint, a presigned or virtual-host URL that holds the bucket) shown as
    ``<url>`` (D544)."""
    return re.sub(r"https?://[^\s'\"]+", "<url>", mask_buckets(text))


def shown_uri(uri: str) -> str:
    """``s3://<bucket>/<prefix>`` of a store URI (the bucket masked, D540)."""
    return mask_buckets(uri)


class StoreError(RuntimeError):
    """A bad store URI or mirror binding (a path naming a mirror, a changed URI, a bad file)."""


class ObjectConflict(RuntimeError):  # noqa: N818
    """A create-only PUT found other bytes under the key on S3 (D372)."""


class RemoteKeyError(BackendKeyError):
    """``get_bytes``/``download`` of a key the remote does not hold."""


@dataclass(frozen=True)
class Obj:
    """One remote object: its key (relative to the store prefix), size and ``LastModified``."""

    key: str
    size: int
    last_modified: dt.datetime | None


def parse_store_uri(uri: str) -> tuple[str, str]:
    """``(bucket, prefix)`` of ``s3://<bucket>/<prefix>`` (a store always has a prefix; trailing
    slashes are dropped); :class:`StoreError` otherwise. Remembers the bucket for
    :func:`mask_buckets` (also of a URI it refuses), so no message shows it."""
    if not uri.startswith("s3://"):
        raise StoreError(f"store URI {uri!r} is not s3://<bucket>/<prefix>")
    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    if bucket:
        _buckets.add(bucket)
    prefix = prefix.strip("/")
    if not bucket or "/" in bucket or not prefix:
        raise StoreError(mask_buckets(f"store URI {uri!r} is not s3://<bucket>/<prefix> (a store is one "
                                      "prefix of a bucket)"))
    try:
        check_key(prefix)
    except ValueError:
        raise StoreError(mask_buckets(f"store URI {uri!r}: bad prefix {prefix!r}")) from None
    return bucket, prefix


#: Waits (s) before each repeat of a download whose body was cut short (:meth:`Remote.download`).
DOWNLOAD_RETRY_S: tuple[float, ...] = (1.0, 4.0, 15.0)


def _transient_read(exc: BaseException) -> bool:
    """A streamed GET that may pass when asked again: a truncated body or a transient S3 failure."""
    try:
        from botocore.exceptions import IncompleteReadError
    except ImportError:  # pragma: no cover - botocore is a dependency
        return False
    return isinstance(exc, IncompleteReadError) or is_s3_transient(exc)


class Remote:
    """One S3 store: ``s3://<bucket>/<prefix>/``. Keys are relative to the prefix."""

    def __init__(self, bucket: str, prefix: str, client: Any) -> None:
        self.s3 = S3Backend(bucket, prefix, client=client)

    @property
    def bucket(self) -> str:
        return self.s3.bucket

    @property
    def prefix(self) -> str:
        return self.s3.prefix

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def put_bytes(self, key: str, data: bytes) -> None:
        self.s3.put(key, data)

    def put_file(self, key: str, path: Path, *, create_only: bool = False) -> None:
        self.s3.put_file(key, path, create_only=create_only)

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.s3.get(key)
        except BackendKeyError:
            raise RemoteKeyError(key) from None

    def download(self, key: str, dst: Path) -> Path:
        """GET ``key`` into ``dst``: written to a temp file in ``dst``'s directory, then
        ``os.replace``d, so a reader never sees a partial file. :class:`RemoteKeyError` when absent
        (``dst`` untouched). A body cut short in transit (botocore's ``IncompleteReadError``, or any
        :func:`is_s3_transient` failure of the streamed read, which the client's request retries do not
        cover) is asked again after each of :data:`DOWNLOAD_RETRY_S`; then it raises."""
        for wait in (*DOWNLOAD_RETRY_S, None):
            try:
                return self._download_once(key, dst)
            except Exception as exc:
                if wait is None or not _transient_read(exc):
                    raise
                time.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def _download_once(self, key: str, dst: Path) -> Path:
        try:
            body = self.s3.body(key)
        except BackendKeyError:
            raise RemoteKeyError(key) from None
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / f".{dst.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        try:
            with open(tmp, "wb") as f:
                while chunk := body.read(_CHUNK):
                    f.write(chunk)
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return dst

    def head(self, key: str) -> Obj | None:
        hit = self.s3.head(key)
        return None if hit is None else Obj(key, hit[0], hit[1])

    def list(self, prefix: str = "") -> Iterator[Obj]:
        """Every object whose key starts with ``prefix`` (paginated LIST)."""
        for key, size, mtime in self.s3.entries(prefix):
            yield Obj(key, size, mtime)

    def delete(self, key: str) -> None:
        self.s3.delete(key)


@dataclass(frozen=True)
class Store:
    """A store root and, for a mirror, its :class:`Remote` (``None``: a local-only store)."""

    root: Path
    remote: Remote | None = None

    @property
    def layout(self) -> StoreLayout:
        return StoreLayout(self.root)

    def key(self, path: str | Path) -> str:
        """The key of ``path`` under the root (:meth:`StoreLayout.key`)."""
        return self.layout.key(path)

    def path(self, key: str) -> Path:
        """The local path of ``key``."""
        return self.root / check_key(key)

    def fetch(self, key: str, *, refresh: bool = False) -> Path | None:
        """The local path of ``key``, downloading it when absent (or always, with ``refresh``); ``None``
        when neither side has it. A local-only store only checks the file. When ``refresh`` finds
        the remote without the key, a local file is still returned (it may be a no-upload job's
        own file); the caller that needs the remote's answer asks ``remote.head``."""
        path = self.path(key)
        if self.remote is None or (path.is_file() and not refresh):
            return path if path.is_file() else None
        try:
            return self.remote.download(key, path)
        except RemoteKeyError:
            return path if path.is_file() else None

    def push(self, key: str, *, create_only: bool = False) -> None:
        """PUT the local file of ``key`` (``FileNotFoundError`` when it is absent); a no-op on a
        local-only store. ``create_only`` (data and release files, D372; ``docs/OPERATE.md``, "Upload"): PUT with
        ``If-None-Match: *``; when S3 holds the key already, GET it and compare sha256 with the
        local file: equal is an earlier upload of the same bytes (a retry), anything else raises
        :class:`ObjectConflict`. Nothing is ever overwritten."""
        path = self.path(key)
        if not path.is_file():
            raise FileNotFoundError(f"push {key}: no local file {path}")
        if self.remote is None:
            return
        try:
            self.remote.put_file(key, path, create_only=create_only)
        except PreconditionFailed:
            import hashlib

            from stack.runs import file_digest

            there = hashlib.sha256(self.remote.get_bytes(key)).hexdigest()
            if there != file_digest(path):
                raise ObjectConflict(f"push {key}: S3 holds other bytes (sha256 {there[:12]}…) than the local "
                                     "file; not overwritten") from None


# ---------------------------------------------------------------- credentials and the client
def store_config(uri: str, env: Mapping[str, str] | None = None) -> S3Config:
    """The S3 identity of the store at ``uri``: the bucket and prefix from the URI, the endpoint and
    keys from ``env`` (default ``os.environ``), else by name from the file ``$STACK_ENV_FILE`` names
    (:data:`STORE_S3_SETTINGS`: each under its name in either source, else its older name in either).
    Refuses (:class:`S3ConfigError`) missing keys and a location in :data:`READ_ONLY_LOCATIONS` (checked
    for the URI and for its bucket root)."""
    bucket, prefix = parse_store_uri(uri)
    e = os.environ if env is None else env
    path = e.get(ENV_FILE_ENV)

    def get(src: Mapping[str, str], n: str) -> str:
        return (src.get(n) or "").strip()

    file_vals: Mapping[str, str] = {}
    if path and not all(get(e, new) for new, _ in STORE_S3_SETTINGS):
        file_vals = env_file_values(path, STORE_S3_NAMES)
    vals = {new: next((v for v in (get(e, new), get(file_vals, new), get(e, old), get(file_vals, old)) if v), "")
            for new, old in STORE_S3_SETTINGS}
    where = f"environment or ${ENV_FILE_ENV}" + (f"={path}" if path else " (unset)")
    missing = [f"{n} (or {STORE_S3_FALLBACK[n]})" for n in ("STACK_S3_ACCESS_KEY_ID", "STACK_S3_SECRET_ACCESS_KEY")
               if not vals[n]]
    if missing:
        raise S3ConfigError(f"store {shown_uri(uri)}: {', '.join(missing)} not set ({where})")
    cfg = S3Config(label="store", base_uri=f"s3://{bucket}/{prefix}",
                   endpoint=vals["STACK_S3_ENDPOINT_URL"] or None,
                   access_key_id=vals["STACK_S3_ACCESS_KEY_ID"],
                   secret_access_key=vals["STACK_S3_SECRET_ACCESS_KEY"],
                   region=vals["STACK_S3_DEFAULT_REGION"] or None)
    root_cfg = S3Config(label="store", base_uri=f"s3://{bucket}", endpoint=cfg.endpoint,
                        access_key_id="", secret_access_key="")
    for loc in (cfg.location_digest(), root_cfg.location_digest()):
        if loc in READ_ONLY_LOCATIONS:
            raise S3ConfigError(f"store {shown_uri(uri)} resolves to the read-only v2 archive bucket "
                                f"(location {loc}; {where}); refusing to use it as a store")
    return cfg


_client_override: Any = None
_lock = threading.Lock()
_stores: dict[Path, Store] = {}


def client_override() -> Any:
    """The client :func:`using_s3_client` installed, or ``None``."""
    return _client_override


@contextlib.contextmanager
def using_s3_client(client: Any) -> Iterator[Any]:
    """TEST HOOK: every S3 client this process builds for a store (:func:`open_store`), and so for its
    blobs (:mod:`stack.store.blobs`, D360), is ``client`` (an in-memory fake) inside the block. Clears :func:`open_store`'s cache on entry and exit. Credentials must still
    resolve (the read-only location check is not bypassed)."""
    global _client_override
    with _lock:
        prev, _client_override = _client_override, client
        _stores.clear()
    try:
        yield client
    finally:
        with _lock:
            _client_override = prev
            _stores.clear()


def read_mirror_uri(root: Path) -> str | None:
    """The URI in ``<root>/.stack-mirror.json``, or ``None`` when the file is absent."""
    path = Path(root) / MIRROR_FILE
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise StoreError(mask_buckets(f"{path}: unreadable mirror binding ({type(exc).__name__})")) from None
    uri = data.get("uri") if isinstance(data, dict) else None
    if not isinstance(uri, str):
        raise StoreError(mask_buckets(f"{path}: no \"uri\" string"))
    bucket, prefix = parse_store_uri(uri)
    return f"s3://{bucket}/{prefix}"


def open_store(root: str | Path) -> Store:
    """The :class:`Store` at ``root`` (one per resolved root per process): a mirror when
    ``.stack-mirror.json`` is there (its client from :func:`store_config`, or the
    :func:`using_s3_client` fake), else local-only."""
    path = Path(root)
    ck = path.resolve()
    with _lock:
        hit = _stores.get(ck)
        if hit is not None:
            return hit
        uri = read_mirror_uri(path)
        remote = None
        if uri is not None:
            cfg = store_config(uri)
            client = _client_override if _client_override is not None else cfg.client()
            remote = Remote(cfg.bucket, cfg.base_path, client)
        store = Store(path, remote)
        _stores[ck] = store
        return store


def resolve_store(arg: str | Path, *, mirror_base: Path) -> Path:
    """The local root a ``--store`` value names. ``s3://<bucket>/<prefix>`` is the mirror
    ``<mirror_base>/<bucket>/<prefix>/``, created with its ``.stack-mirror.json`` (and an empty
    ``.exclude_from_backup``) on first use; an existing binding to another URI is refused. A path is
    the root itself (absolute), refused when it holds ``.stack-mirror.json`` (a mirror is named by
    its URI, so nothing writes into it as a local-only store)."""
    s = str(arg)
    if s.startswith("s3://"):
        bucket, prefix = parse_store_uri(s)
        uri = f"s3://{bucket}/{prefix}"
        root = Path(mirror_base).absolute() / bucket / prefix
        have = read_mirror_uri(root)
        if have is None:
            root.mkdir(parents=True, exist_ok=True)
            try:
                with open(root / MIRROR_FILE, "x", encoding="utf-8") as f:
                    f.write(json.dumps({"uri": uri}) + "\n")
            except FileExistsError:
                have = read_mirror_uri(root)  # another process bound it meanwhile
            (root / EXCLUDE_FROM_BACKUP).touch()
        if have is not None and have != uri:
            raise StoreError(mask_buckets(f"{root / MIRROR_FILE} binds this directory to {have}, not {uri}; "
                                          "a mirror binding never changes"))
        return root
    root = Path(s).absolute()
    have = read_mirror_uri(root)
    if have is not None:
        raise StoreError(mask_buckets(f"{root} is the mirror of {have}: use --store {have}, not its path"))
    return root
