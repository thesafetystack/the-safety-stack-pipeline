"""S3 credentials (:mod:`stack.store.s3config`; round 5, T-S3, D248): the read-only client hook
(a store's own credentials and its read-only location guard: ``tests/test_store_remote.py``). Moved from ``tests/test_v3archive.py`` when the archive lane was removed (T-STORE D329). No
network: the client is never asked to send."""

from __future__ import annotations

import pytest

from stack.store.s3config import (
    READ_OPS,
    ReadOnlyViolation,
    S3Config,
    refuse_writes,
)

SECRET = "sEcReT-sentinel-value"


def test_read_only_client_refuses_writes_before_sending() -> None:
    cfg = S3Config(label="v2", base_uri="s3://b", endpoint="http://127.0.0.1:9", access_key_id="a",
                   secret_access_key=SECRET)
    client = cfg.client(read_only=True)
    for call in (lambda: client.put_object(Bucket="b", Key="k", Body=b"x"),
                 lambda: client.delete_object(Bucket="b", Key="k"),
                 lambda: client.delete_objects(Bucket="b", Delete={"Objects": [{"Key": "k"}]}),
                 lambda: client.copy_object(Bucket="b", Key="k", CopySource={"Bucket": "b", "Key": "j"}),
                 lambda: client.create_multipart_upload(Bucket="b", Key="k"),
                 lambda: client.put_bucket_policy(Bucket="b", Policy="{}")):
        with pytest.raises(ReadOnlyViolation):
            call()

    class Op:
        def __init__(self, name: str) -> None:
            self.name = name

    for op in READ_OPS:  # the allowed operations pass the hook (called directly: no network)
        refuse_writes(model=Op(op))
    for op in ("PutObject", "DeleteObject", "CopyObject", "UploadPart", "PutObjectAcl", None):
        with pytest.raises(ReadOnlyViolation):
            refuse_writes(model=Op(op) if op else None)
