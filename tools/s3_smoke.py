"""Smoke-test an S3 store's bucket: put, head, get (sha256-checked), list and delete under a test prefix.

    STACK_ENV_FILE=.env uv run --no-sync python tools/s3_smoke.py s3://<bucket>/<prefix> [--sizes 1024,1048576]

Credentials as for the store (:func:`stack.store.remote.store_config`). Objects go to
``<prefix>/_smoke/<utc>-<rand>/<n>`` and are deleted at the end (only under that prefix). Prints operation, size, latency and the error class
and S3 code; never the endpoint, the bucket, a key's base path or any credential.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stack.store.remote import store_config  # noqa: E402
from stack.store.s3config import redact_error  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("store", help="s3://<bucket>/<prefix>")
    ap.add_argument("--sizes", default="1024,1048576,10485760")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    cfg = store_config(args.store)
    client = cfg.client()
    run = f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}-{os.urandom(3).hex()}"
    prefix = "/".join(p for p in (cfg.base_path, "_smoke", run) if p) + "/"
    print(f"smoke run {run}; location digest {cfg.location_digest()}")
    errors = 0
    keys: list[str] = []

    def op(name: str, size: int, fn):  # type: ignore[no-untyped-def]
        nonlocal errors
        t = time.perf_counter()
        try:
            out = fn()
            print(f"  {name:<8} {size:>10} B  {1000 * (time.perf_counter() - t):8.1f} ms  ok")
            return out
        except Exception as exc:  # noqa: BLE001 — reported redacted, counted
            errors += 1
            print(f"  {name:<8} {size:>10} B  {1000 * (time.perf_counter() - t):8.1f} ms  "
                  f"ERROR {redact_error(exc)}")
            return None

    for i, size in enumerate(sizes):
        data = os.urandom(size)
        digest = hashlib.sha256(data).hexdigest()
        key = f"{prefix}{i}"
        keys.append(key)
        op("put", size, lambda: client.put_object(Bucket=cfg.bucket, Key=key, Body=data))
        op("put-again", size, lambda: client.put_object(Bucket=cfg.bucket, Key=key, Body=data))
        h = op("head", size, lambda: client.head_object(Bucket=cfg.bucket, Key=key))
        if h is not None and h.get("ContentLength") != size:
            errors += 1
            print(f"  head size mismatch: {h.get('ContentLength')} != {size}")
        body = op("get", size, lambda: client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read())
        if body is not None and hashlib.sha256(body).hexdigest() != digest:
            errors += 1
            print("  get: sha256 mismatch")
    listed = op("list", 0, lambda: client.list_objects_v2(Bucket=cfg.bucket, Prefix=prefix))
    if listed is not None:
        got = sorted(o["Key"] for o in listed.get("Contents", []))
        print(f"  list returned {len(got)} of {len(keys)} keys; match: {got == sorted(keys)}")
        errors += got != sorted(keys)
    for key, size in zip(keys, sizes, strict=True):
        op("delete", size, lambda: client.delete_object(Bucket=cfg.bucket, Key=key))
    from botocore.exceptions import ClientError

    for key in keys:
        try:
            client.head_object(Bucket=cfg.bucket, Key=key)
            errors += 1
            print("  head after delete: still present")
        except ClientError as exc:
            code = str((exc.response.get("Error") or {}).get("Code", ""))
            if code not in ("404", "NoSuchKey", "NotFound"):
                errors += 1
                print(f"  head after delete: {redact_error(exc)}")
    big = os.urandom(8 << 20)
    key = f"{prefix}throughput"
    t = time.perf_counter()
    op("put-8M", len(big), lambda: client.put_object(Bucket=cfg.bucket, Key=key, Body=big))
    up = time.perf_counter() - t
    t = time.perf_counter()
    op("get-8M", len(big), lambda: client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read())
    down = time.perf_counter() - t
    op("delete", len(big), lambda: client.delete_object(Bucket=cfg.bucket, Key=key))
    print(f"  throughput: up {8 / up:.1f} MiB/s, down {8 / down:.1f} MiB/s (single stream)")
    print(f"errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
