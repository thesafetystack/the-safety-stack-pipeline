"""Object backends with one small duck-typed interface (D28): ``put/get/exists/list`` over
``/``-separated keys. :class:`LocalBackend` is a directory; :class:`S3Backend` is one bucket and key prefix via
boto3 (the S3 code of v3 ``core/blobstore.py``).

The S3 backend takes an explicit client (round 5, R6-9): a store's is built by
:meth:`stack.store.s3config.S3Config.client` with explicit keys, never boto3's ambient chain; tests
hand it an in-memory fake. Nothing here reads credentials.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any


class PreconditionFailed(Exception):  # noqa: N818 - S3's error code
    """A create-only PUT (``If-None-Match: *``) of a key the bucket holds already (HTTP 412)."""


class BackendKeyError(KeyError):
    """``get`` on a key the backend does not hold."""


def check_key(key: str) -> str:
    """A relative ``/``-separated key with no empty, ``.`` or ``..`` part (else ``ValueError``)."""
    parts = PurePosixPath(key).parts
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts) or "\\" in key:
        raise ValueError(f"bad object key {key!r}")
    return key


class LocalBackend:
    """Objects as files under ``root``. ``put`` is atomic (temp file + ``os.replace`` in the
    target directory) and overwrites; temp files are dot-prefixed and never listed."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / check_key(key)

    def put(self, key: str, data: bytes) -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as exc:
            raise BackendKeyError(key) from exc

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def list(self, prefix: str = "") -> Iterator[str]:
        if not self.root.is_dir():
            return
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield key


_MISSING = ("404", "NoSuchKey", "NotFound")


def _is_missing(exc: Any) -> bool:
    return str(exc.response.get("Error", {}).get("Code", "")) in _MISSING


class S3Backend:
    """Objects in one S3 bucket under an optional key prefix. ``client`` is any object with
    boto3's ``put_object/get_object/head_object/list_objects_v2/delete_object`` (required: no
    ambient-chain client is ever built here). The one S3 implementation: the blob store and
    :class:`stack.store.remote.Remote` both go through it."""

    def __init__(self, bucket: str, prefix: str = "", *, client: Any) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client

    def object_key(self, key: str) -> str:
        k = check_key(key)
        return f"{self.prefix}/{k}" if self.prefix else k

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.object_key(key), Body=data)

    def put_file(self, key: str, path: Path, *, create_only: bool = False) -> None:
        """PUT the file at ``path`` (streamed from disk, one request); ``create_only`` sends
        ``If-None-Match: *`` and raises :class:`PreconditionFailed` when the key exists."""
        from botocore.exceptions import ClientError

        extra = {"IfNoneMatch": "*"} if create_only else {}
        with open(path, "rb") as f:
            try:
                self.client.put_object(Bucket=self.bucket, Key=self.object_key(key), Body=f, **extra)
            except ClientError as exc:
                err = exc.response.get("Error", {})
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if create_only and (err.get("Code") == "PreconditionFailed" or status == 412):
                    raise PreconditionFailed(key) from None
                raise

    def body(self, key: str) -> Any:
        """The streaming ``Body`` of a GET (``.read(n)``); :class:`BackendKeyError` when absent."""
        from botocore.exceptions import ClientError

        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self.object_key(key))
        except ClientError as exc:
            if _is_missing(exc):
                raise BackendKeyError(key) from exc
            raise
        return resp["Body"]

    def get(self, key: str) -> bytes:
        data: bytes = self.body(key).read()
        return data

    def head(self, key: str) -> tuple[int, dt.datetime | None] | None:
        """``(size, last_modified)`` of the object, or ``None`` when absent."""
        from botocore.exceptions import ClientError

        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=self.object_key(key))
        except ClientError as exc:
            if _is_missing(exc):
                return None
            raise
        return int(resp.get("ContentLength", 0)), resp.get("LastModified")

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def delete(self, key: str) -> None:
        """DELETE the object (S3: no error when it is absent)."""
        self.client.delete_object(Bucket=self.bucket, Key=self.object_key(key))

    def list(self, prefix: str = "") -> Iterator[str]:
        for key, _, _ in self.entries(prefix):
            yield key

    def items(self, prefix: str = "") -> Iterator[tuple[str, int]]:
        """``(key, size)`` under ``prefix``, one paginated LIST."""
        for key, size, _ in self.entries(prefix):
            yield key, size

    def entries(self, prefix: str = "") -> Iterator[tuple[str, int, dt.datetime | None]]:
        """``(key, size, last_modified)`` under ``prefix`` (keys relative to the backend's
        prefix), one paginated LIST."""
        base = f"{self.prefix}/" if self.prefix else ""
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": base + prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                yield str(obj["Key"])[len(base):], int(obj.get("Size", 0)), obj.get("LastModified")
            if not resp.get("IsTruncated"):
                return
            token = resp["NextContinuationToken"]
