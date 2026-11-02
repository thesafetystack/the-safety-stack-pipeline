"""Explicit S3 credentials (round 5, T-S3; R6-9): :class:`S3Config`, the env-file reader, the
read-only guard, :func:`redact_error` and the S3-transient classification (:func:`is_s3_transient`,
:class:`S3TransientError`, S25-47).

A store's own credentials are resolved by :func:`stack.store.remote.store_config` (``STACK_S3_ENDPOINT_URL``,
``STACK_S3_ACCESS_KEY_ID``, ``STACK_S3_SECRET_ACCESS_KEY``, optional ``STACK_S3_DEFAULT_REGION``; the older
names ``STACK_S3_ENDPOINT``, ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``STACK_S3_REGION`` are still
accepted, D726; the bucket and prefix come from the store URI). The separate blob bucket (``STACK_S3_BASE_URI``) is gone: blobs live in each
store's own prefix (T-STORE D360). No store may resolve to a location in :data:`READ_ONLY_LOCATIONS`
(the old v2 bucket, whose keys are read-write). The v2/v3 archive lane that read that bucket is gone
(T-STORE D329).

Nothing here puts a value into ``os.environ``; boto3 clients are built with explicit keys, never
boto3's ambient chain. No message, ``repr`` or exception raised here contains a value: errors name the
variable and the file, and :func:`redact_error` reduces a provider exception to its class and S3
error code (an endpoint URL or a DSN host can sit in a botocore or libpq message).

``S3Config.client(read_only=True)`` refuses every operation outside :data:`READ_OPS` in a
``before-call`` hook, so a write cannot be sent through that client.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ENV_FILE_ENV = "STACK_ENV_FILE"
#: The only S3 operations a ``read_only`` client may send.
READ_OPS = frozenset({"GetObject", "HeadObject", "ListObjectsV2", "ListObjects", "GetObjectAttributes"})
#: Location digests (:meth:`S3Config.location_digest`) a store may never resolve to: the v2
#: archive bucket; the archive keys are read-write, so treat them with care.
#: A digest, not the endpoint or bucket name.
READ_ONLY_LOCATIONS = frozenset({"60b03b8ee1685bde"})


class S3ConfigError(RuntimeError):
    """A missing or malformed credential setting (the message names variables, never values)."""


class ReadOnlyViolation(RuntimeError):
    """An operation outside :data:`READ_OPS` was attempted on a read-only client."""


def env_file_values(path: str | Path, names: tuple[str, ...]) -> dict[str, str]:
    """``KEY=VALUE`` lines of ``path`` for ``names`` only (optional ``export``, one pair of matching
    quotes stripped, ``#`` comment lines and `` #`` trailing comments; no interpolation). A missing
    or unreadable file raises :class:`S3ConfigError` naming the path."""
    try:
        text = Path(path).read_text("utf-8")
    except OSError as exc:
        raise S3ConfigError(f"env file {path}: cannot read ({type(exc).__name__})") from None
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, sep, v = line.partition("=")
        k = k.strip()
        if not sep or k not in names:
            continue
        v = v.strip()
        if v[:1] in ("'", '"') and v.find(v[0], 1) > 0:
            v = v[1:v.find(v[0], 1)]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if v:
            out[k] = v
    return out


def redact_error(exc: BaseException) -> str:
    """A provider exception as ``<Class>[<S3 code>]``: nothing of its message, which can carry an
    endpoint URL, a bucket name or a DSN host."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, Mapping):
        code = str((resp.get("Error") or {}).get("Code") or "")
    return f"{type(exc).__name__}[{code}]" if code else type(exc).__name__


#: S3 error codes of a request that may succeed when repeated (server side or throttling); with
#: any HTTP status >= 500 or 429 (S25-47).
TRANSIENT_CODES = frozenset({"InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout",
                             "Throttling", "ThrottlingException", "RequestLimitExceeded"})


class S3TransientError(RuntimeError):
    """A transient S3 failure (:func:`is_s3_transient`) where no run record exists yet (a job's
    :func:`stack.store.job.prepare`); the message is redacted (:func:`redact_error`). A build
    ends ``failed[s3_transient]`` (S25-47)."""


def is_s3_transient(exc: BaseException) -> bool:
    """An S3 transport failure that the client's own retries did not get past and that may pass:
    :class:`S3TransientError`, a botocore connection or HTTP-client error (connect, read timeout,
    closed connection), or a ``ClientError`` with a :data:`TRANSIENT_CODES` code or HTTP status
    >= 500 or 429. Anything else (``AccessDenied``, ``NoSuchBucket``, a precondition) is not."""
    if isinstance(exc, S3TransientError):
        return True
    try:
        from botocore.exceptions import ClientError, HTTPClientError
        from botocore.exceptions import ConnectionError as BotoConnectionError
    except ImportError:  # pragma: no cover - botocore is a dependency
        return False
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return True
    if not isinstance(exc, ClientError):
        return False
    code = str((exc.response.get("Error") or {}).get("Code") or "")
    status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in TRANSIENT_CODES or (isinstance(status, int) and (status >= 500 or status == 429))


def redacted_trace(exc: BaseException) -> str:
    """``exc``'s traceback frames (file, line, source) and :func:`redact_error` in place of its
    message (an S3 message can name the endpoint URL, the bucket and the key): a record's
    ``error`` for an S3 failure."""
    import traceback

    return "".join(traceback.format_tb(exc.__traceback__)) + (
        str(exc) if isinstance(exc, S3TransientError) else redact_error(exc))


@dataclass(frozen=True)
class S3Config:
    """One bucket identity. ``repr`` shows the label only."""

    label: str
    base_uri: str = field(repr=False)
    endpoint: str | None = field(repr=False)
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    region: str | None = field(default=None, repr=False)

    @property
    def bucket(self) -> str:
        return urlparse(self.base_uri).netloc

    @property
    def base_path(self) -> str:
        return urlparse(self.base_uri).path.strip("/")

    def location_digest(self) -> str:
        """``sha256(endpoint NUL base_uri)[:16]``: identifies the bucket location in a store's
        settings without writing the endpoint or bucket name down."""
        h = hashlib.sha256(f"{self.endpoint or ''}\0{self.base_uri}".encode()).hexdigest()
        return h[:16]

    def client(self, *, read_only: bool = False) -> Any:
        """A boto3 S3 client with this identity's explicit keys (never the ambient chain). With a
        custom endpoint the signing region is ``region`` or ``auto`` (R2). ``read_only`` installs
        the :data:`READ_OPS` guard."""
        import boto3
        from botocore.config import Config

        region = self.region or ("auto" if self.endpoint else None)
        session = boto3.session.Session(aws_access_key_id=self.access_key_id,
                                        aws_secret_access_key=self.secret_access_key,
                                        region_name=region)
        cfg = Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=20,
                     read_timeout=120, max_pool_connections=32)
        client = session.client("s3", endpoint_url=self.endpoint, config=cfg)
        if read_only:
            guard_read_only(client)
        return client


def guard_read_only(client: Any) -> None:
    """Refuse every operation outside :data:`READ_OPS` before it is signed or sent."""
    client.meta.events.register("before-call.s3.*", refuse_writes)


def refuse_writes(model: Any = None, **_: Any) -> None:
    """The ``before-call`` hook of :func:`guard_read_only`: raise on any operation outside :data:`READ_OPS`."""
    name = getattr(model, "name", None)
    if name not in READ_OPS:
        raise ReadOnlyViolation(f"read-only client: operation {name} refused")

