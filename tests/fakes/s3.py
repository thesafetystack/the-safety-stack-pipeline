"""TEST-ONLY in-memory S3 client (round 5, T-S3; T-STORE): boto3's ``put_object`` (``Body`` bytes
or a binary file), ``get_object`` (``Body`` with ``.read()``), ``head_object``, ``delete_object``
``copy_object`` (server-side) and paginated ``list_objects_v2`` over a dict. A missing key raises
``botocore.exceptions.ClientError`` with code ``404`` (HEAD) or ``NoSuchKey`` (GET), as S3 does; a
DELETE of a missing key succeeds, as on S3. No network, no credentials: hand it to
``stack.store.remote.using_s3_client`` or to a backend's ``client=``.

Knobs:
- ``page``: LIST page size, small so pagination runs;
- ``clock``: ``() -> datetime`` stamping each PUT's ``LastModified`` (HEAD and LIST report it);
  default the wall clock, UTC (an object set in ``objects`` directly reports :data:`EPOCH`);
- ``fail_ops``: operation names (``PutObject``, ``GetObject``, ``HeadObject``, ``DeleteObject``,
  ``ListObjectsV2``) that raise ``ClientError`` (code ``fail_code``, default ``InternalError``) while
  listed; ``fail_next(op, n)`` fails only the next ``n`` calls of ``op``;
- ``fail_code``: the S3 error code of those failures (``SlowDown``: a throttled request, D789);
- ``fail_puts``: the older spelling of ``"PutObject" in fail_ops`` (kept);
- ``calls``: ``(op, key)`` in order (``op`` = ``put``, ``get``, ``head``, ``delete``, ``list``),
  thread-safe; a failed call is logged too;
- ``root`` (T-STORE A2): keep the objects in that directory instead of memory
  (``<root>/<bucket>/<key>``, ``LastModified`` = the file's mtime, set from ``clock``), so a
  subprocess and the test share one bucket (the crash tests); each PUT is a temp file + rename.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import threading
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

#: Fake store settings for ``stack.store.remote.store_config`` (the current names, D726); the secret is a
#: sentinel that tests assert never appears in output or messages.
SECRET = "FAKE-SECRET-sentinel-0f1e2d"
FAKE_ENV = {
    "STACK_S3_ENDPOINT_URL": "https://fake-endpoint.invalid",
    "STACK_S3_ACCESS_KEY_ID": "FAKE-KEY-ID",
    "STACK_S3_SECRET_ACCESS_KEY": SECRET,
    "STACK_S3_DEFAULT_REGION": "auto",
}
#: The same settings under their older names, still accepted (D726).
FAKE_ENV_OLD = {
    "STACK_S3_ENDPOINT": FAKE_ENV["STACK_S3_ENDPOINT_URL"],
    "AWS_ACCESS_KEY_ID": FAKE_ENV["STACK_S3_ACCESS_KEY_ID"],
    "AWS_SECRET_ACCESS_KEY": SECRET,
    "STACK_S3_REGION": "auto",
}


#: ``LastModified`` of an object a test placed in ``objects`` directly (no PUT, so no clock stamp).
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class _DirObjects(MutableMapping[tuple[str, str], bytes]):
    """``(bucket, key) -> bytes`` as files under ``root`` (see the module docstring)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, k: tuple[str, str]) -> Path:
        return self.root / k[0] / k[1]

    def __getitem__(self, k: tuple[str, str]) -> bytes:
        try:
            return self._path(k).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise KeyError(k) from None

    def __setitem__(self, k: tuple[str, str], v: bytes) -> None:
        p = self._path(k)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".fake-s3.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_bytes(v)
        os.replace(tmp, p)

    def __delitem__(self, k: tuple[str, str]) -> None:
        try:
            self._path(k).unlink()
        except FileNotFoundError:
            raise KeyError(k) from None

    def __iter__(self) -> Iterator[tuple[str, str]]:
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith(".fake-s3."):
                rel = p.relative_to(self.root).as_posix()
                bucket, _, key = rel.partition("/")
                yield bucket, key

    def __len__(self) -> int:
        return sum(1 for _ in self)


class _DirMtimes(MutableMapping[tuple[str, str], dt.datetime]):
    """``LastModified`` of a :class:`_DirObjects` entry: its file's mtime."""

    def __init__(self, objects: _DirObjects) -> None:
        self.objects = objects

    def __getitem__(self, k: tuple[str, str]) -> dt.datetime:
        try:
            return dt.datetime.fromtimestamp(self.objects._path(k).stat().st_mtime, dt.UTC)
        except FileNotFoundError:
            raise KeyError(k) from None

    def __setitem__(self, k: tuple[str, str], v: dt.datetime) -> None:
        ts = v.timestamp()
        os.utime(self.objects._path(k), (ts, ts))

    def __delitem__(self, k: tuple[str, str]) -> None:
        pass  # goes with the object

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self.objects)

    def __len__(self) -> int:
        return len(self.objects)


class FakeS3:
    def __init__(self, *, page: int = 3, clock: Callable[[], dt.datetime] = _now, root: Path | None = None) -> None:
        self.objects: MutableMapping[tuple[str, str], bytes] = {} if root is None else _DirObjects(root)
        self.mtimes: MutableMapping[tuple[str, str], dt.datetime] = \
            {} if root is None else _DirMtimes(self.objects)  # type: ignore[arg-type]
        self.page = page
        self.clock = clock
        self.fail_puts = False
        self.fail_ops: set[str] = set()
        self._fail_next: dict[str, int] = {}
        self.fail_code = "InternalError"
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def _log(self, op: str, key: str) -> None:
        with self._lock:
            self.calls.append((op, key))

    def ops(self, op: str) -> list[str]:
        """The keys of every ``op`` call so far."""
        with self._lock:
            return [k for o, k in self.calls if o == op]

    def fail_next(self, op: str, n: int = 1) -> None:
        """Fail the next ``n`` calls of ``op`` (an S3 operation name, e.g. ``PutObject``)."""
        with self._lock:
            self._fail_next[op] = self._fail_next.get(op, 0) + n

    @staticmethod
    def _error(code: str, op: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "fake"}}, op)

    def _maybe_fail(self, op: str) -> None:
        with self._lock:
            hit = op in self.fail_ops or (op == "PutObject" and self.fail_puts)
            if not hit and self._fail_next.get(op, 0) > 0:
                self._fail_next[op] -= 1
                hit = True
        if hit:
            raise self._error(self.fail_code, op)

    def put_object(self, *, Bucket: str, Key: str, Body: Any, IfNoneMatch: str | None = None) -> dict[str, Any]:  # noqa: N803
        """A PUT; ``IfNoneMatch="*"`` refuses an existing key with 412 ``PreconditionFailed`` (as S3
        and Tigris do)."""
        self._log("put", Key)
        self._maybe_fail("PutObject")
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        if IfNoneMatch is not None and IfNoneMatch != "*":
            raise ValueError(f"fake S3: IfNoneMatch {IfNoneMatch!r} (only '*')")
        with self._lock:
            refused = IfNoneMatch is not None and (Bucket, Key) in self.objects
            if not refused:
                self.objects[(Bucket, Key)] = bytes(data)
                self.mtimes[(Bucket, Key)] = self.clock()
        if refused:
            self._log("put_refused", Key)
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "fake"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._log("get", Key)
        self._maybe_fail("GetObject")
        with self._lock:
            body = self.objects.get((Bucket, Key))
            mtime = self.mtimes.get((Bucket, Key), EPOCH)
        if body is None:
            raise self._error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(body), "ContentLength": len(body), "LastModified": mtime}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._log("head", Key)
        self._maybe_fail("HeadObject")
        with self._lock:
            body = self.objects.get((Bucket, Key))
            mtime = self.mtimes.get((Bucket, Key), EPOCH)
        if body is None:
            raise self._error("404", "HeadObject")
        return {"ContentLength": len(body), "LastModified": mtime}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict[str, str]) -> dict[str, Any]:  # noqa: N803
        """A server-side copy (``CopySource = {Bucket, Key}``); a missing source raises ``NoSuchKey``."""
        self._log("copy", Key)
        self._maybe_fail("CopyObject")
        with self._lock:
            body = self.objects.get((CopySource["Bucket"], CopySource["Key"]))
            if body is not None:
                self.objects[(Bucket, Key)] = body
                self.mtimes[(Bucket, Key)] = self.clock()
        if body is None:
            raise self._error("NoSuchKey", "CopyObject")
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self._log("delete", Key)
        self._maybe_fail("DeleteObject")
        with self._lock:
            self.objects.pop((Bucket, Key), None)
            self.mtimes.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "",  # noqa: N803
                        ContinuationToken: str | None = None, MaxKeys: int | None = None,  # noqa: N803
                        ) -> dict[str, Any]:
        self._log("list", Prefix)
        self._maybe_fail("ListObjectsV2")
        with self._lock:
            keys = sorted((k, len(v), self.mtimes.get((b, k), EPOCH)) for (b, k), v in self.objects.items()
                          if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        n = min(self.page, MaxKeys or self.page)
        chunk = keys[start:start + n]
        more = start + n < len(keys)
        return {"Contents": [{"Key": k, "Size": s, "LastModified": m} for k, s, m in chunk],
                "IsTruncated": more, **({"NextContinuationToken": str(start + n)} if more else {})}

    def keys(self) -> set[str]:
        with self._lock:
            return {k for _, k in self.objects}
