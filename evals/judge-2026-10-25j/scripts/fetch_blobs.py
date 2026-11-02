"""Fetch the text blobs of sampled items that the local mirror lacks, read only, into EVAL104_PRIVATE/blobs-extra/.

    fetch_blobs.py SET [SET ...]

The store URI is the mirror's own binding (``.stack-mirror.json`` beside the blob dir EVAL104_BLOBS_GLOB resolves to;
the mirror's relative layout under the store prefix is the bucket's: ``<prefix>/blobs/<sha256>``). Credentials come
from ``STACK_ENV_FILE`` through ``stack.store.remote.store_config`` (this script never reads that file itself); the
boto3 client is ``read_only`` (``stack.store.s3config.guard_read_only``: anything but a read is refused before it is
signed). Each blob is GET, its sha256 checked against its name, and written as ``blobs-extra/<sha256>``. Neither the
URI nor the bucket is printed; errors are reduced to ``redact_error``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    from stack.store.remote import Remote, store_config
    from stack.store.s3config import redact_error

    blobs = C.Blobs()
    want = []
    for set_name in argv:
        for s in C.read_jsonl(C.SET_ROOT / set_name / "sample.jsonl"):
            if not blobs.exists(s["text_sha256"]):
                want.append(s["text_sha256"])
    want = sorted(set(want))
    print(f"missing locally: {len(want)}")
    if not want:
        return 0
    uri = json.loads((blobs._dir.parent / ".stack-mirror.json").read_text("utf-8"))["uri"]
    cfg = store_config(uri)
    remote = Remote(cfg.bucket, cfg.base_path, cfg.client(read_only=True))
    out = C.extra_blob_dir()
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for d in want:
        try:
            data = remote.get_bytes(f"blobs/{d}")
        except Exception as exc:  # noqa: BLE001 - reported redacted, counted
            print(f"FAILED {d}: {redact_error(exc)}")
            bad += 1
            continue
        if C.sha256_bytes(data) != d:
            print(f"FAILED {d}: sha256 mismatch")
            bad += 1
            continue
        (out / d).write_bytes(data)
    print(f"fetched {len(want) - bad}, failed {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
