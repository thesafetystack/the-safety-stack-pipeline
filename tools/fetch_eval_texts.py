"""Fetch the frozen eval sets' whole texts into the eval-text cache (D790), read-only from the archive.

    STACK_ENV_FILE=.env uv run --no-sync python tools/fetch_eval_texts.py [--store s3://<bucket>/<prefix>]
        [--archive KEY] [--dest DIR]
    uv run --no-sync python tools/fetch_eval_texts.py --from-tar evals.tar.zst [--dest DIR]   # a local copy
    uv run --no-sync python tools/fetch_eval_texts.py --check [--dest DIR]                    # no network

The whole texts of the older eval sets (``evals/<task>/<set_id>/texts/<item_id>.txt.gz``) are third-party
documents and are not in the repository. Readers (:func:`stack.evals.freeze.find_text`: the eval harness, the
tests) look in a set's own ``texts/`` and then in the cache, ``<dest>/<task>/<set_id>/<item_id>.txt.gz``
(``--dest`` defaults to :func:`stack.evals.freeze.texts_cache_root`: ``$STACK_EVAL_TEXTS_DIR``, else
``$XDG_CACHE_HOME/stack/eval-texts``, else ``~/.cache/stack/eval-texts``). Fetching is never part of a test.

**Source.** A ``tar.zst`` of an ``evals/`` tree in the store's bucket (``--archive``, a key from the bucket
root, repeatable; default :data:`DEFAULT_ARCHIVES`), checked against the ``SHA256SUMS`` beside it before it is
read. Every member ``.../<task>/<set_id>/texts/<item_id>.txt.gz`` is a candidate. The bucket comes from
``--store``, else ``STACK_STORE``, else ``STACK_S3_BASE_URI`` (each from the environment, else by name from the
file ``$STACK_ENV_FILE`` names); credentials as for a store (:func:`stack.store.remote.store_config`), on a
read-only client (only GET, HEAD and LIST can be sent). Nothing is written to S3. The bucket, the endpoint and
any credential are never printed; a provider error is shown as its class and S3 code.

**Pins.** A text is written only when its item is in this checkout's ``evals/<task>/<set_id>/items.jsonl`` and
the sha256 of the decompressed text equals the item's frozen digest (``input.full_text_digest``, or the
top-level ``text_digest`` of a set frozen before it); anything else is counted and refused. Written files are
the archive's bytes (deterministic gzip, mtime 0). ``--check`` verifies the cache against the same pins.

**Archived eval files** (D793): eval files with long third-party excerpts (labeller disagreement dumps, the
calibration slice) are not in the repository either; ``evals/archived.sha256`` pins each by sha256 and path under
``evals/``. The tool fetches them from ``archived/2026-11-02-eval-excerpts/evals/<path>`` into ``<dest>/<path>``
(:func:`stack.evals.freeze.find_eval_file` reads them there), each checked against its pin; ``--check`` verifies them.
They include eight sets' ``items.jsonl`` (D795), fetched before the texts because the texts' pins are in them.

Exit status: 0 when every set the source (or, with ``--check``, the cache) has texts for is complete and every
text matches its pin; 1 otherwise. Sets whose texts the source does not hold are listed, not failed (the sets
frozen after 2026-10-21 never had their texts in the repository).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stack.evals.freeze import TEXTS_CACHE_ENV, archived_pins, texts_cache_root  # noqa: E402
from stack.store.remote import store_config  # noqa: E402
from stack.store.s3config import ENV_FILE_ENV, env_file_values, redact_error  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"
#: The archive of the repository's ``evals/`` tree taken before the texts left it (a key from the bucket root).
DEFAULT_ARCHIVES = ("archived/2026-10-31-pipeline-evals-at-38330fc5/evals.tar.zst",)
#: The settings that name the bucket, in order (environment, else the env file).
BUCKET_SETTINGS = ("STACK_STORE", "STACK_S3_BASE_URI")
#: The archives of the eval files kept out of the repository, tried in order: ``<prefix>evals/<path>``. Long
#: excerpts and items (D793, D795); a source file carrying links to link-shared documents (D796).
EXCERPTS_PREFIXES = ("archived/2026-11-02-eval-excerpts/", "archived/2026-11-02-eval-links/")
EXCERPTS_PREFIX = EXCERPTS_PREFIXES[0]
MEMBER_RE = re.compile(r"(?:^|/)([^/]+)/([^/]+)/texts/([^/]+)\.txt\.gz$")

Key = tuple[str, str]  # (task, set_id)


def pins(evals: Path = EVALS, dest: Path | None = None) -> dict[Key, dict[str, str]]:
    """``(task, set_id) -> {item_id: sha256 of the text}`` for every item of the checkout's sets that has one; a set
    whose ``items.jsonl`` is archived (D795) is read from ``dest`` (the cache), so fetch the archived files first."""
    out: dict[Key, dict[str, str]] = {}
    dirs = {p.parent for p in evals.glob("*/*/items.jsonl")}
    dirs |= {evals / rel.rsplit("/", 1)[0] for rel in archived_pins(evals) if rel.endswith("/items.jsonl")}
    for d in sorted(dirs):
        items = d / "items.jsonl"
        if not items.is_file() and dest is not None:
            items = dest / d.parent.name / d.name / "items.jsonl"
        if not items.is_file():
            continue
        got: dict[str, str] = {}
        for line in items.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            inp = row.get("input") if isinstance(row.get("input"), dict) else {}
            digest = inp.get("full_text_digest") or row.get("text_digest")
            if digest:
                got[str(row["item_id"])] = str(digest)
        if got:
            out[(d.parent.name, d.name)] = got
    return out


def tar_getter(tars: list[bytes]) -> Callable[[str], bytes]:
    """The archived eval files (by path under ``evals/``) as members of local ``evals/`` tarballs."""
    import zstandard

    found: dict[str, bytes] = {}
    want = set(archived_pins(EVALS))
    for b in tars:
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(b)) as zr, tarfile.open(fileobj=zr, mode="r|") as tar:
            for m in tar:
                rel = m.name.split("evals/", 1)[-1]
                if rel in want and m.isfile() and (f := tar.extractfile(m)) is not None:
                    found[rel] = f.read()

    def get(rel: str) -> bytes:
        if rel not in found:
            raise SystemExit(f"error: {rel} is not in the given tarball(s)")
        return found[rel]
    return get


@dataclass
class Tally:
    written: int = 0
    present: int = 0  # already in the cache with the pinned digest
    bad: list[str] = field(default_factory=list)  # digest mismatch, unknown item, unreadable


def text_digest(data: bytes) -> str:
    return hashlib.sha256(gzip.decompress(data)).hexdigest()


def put(dest: Path, key: Key, item: str, data: bytes) -> bool:
    """Write ``data`` to the cache unless an identical file is there; True when written."""
    p = dest / key[0] / key[1] / f"{item}.txt.gz"
    if p.is_file() and p.read_bytes() == data:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(p)
    return True


def ingest(tar_bytes: bytes, pinned: dict[Key, dict[str, str]], dest: Path,
           tally: dict[Key, Tally]) -> None:
    """Every text member of the ``tar.zst`` in ``tar_bytes``, checked against ``pinned`` and written to ``dest``."""
    import zstandard

    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(tar_bytes)) as zr, \
            tarfile.open(fileobj=zr, mode="r|") as tar:
        for m in tar:
            hit = MEMBER_RE.search(m.name)
            if not hit or not m.isfile():
                continue
            key, item = (hit.group(1), hit.group(2)), hit.group(3)
            t = tally[key]
            f = tar.extractfile(m)
            data = f.read() if f is not None else b""
            want = pinned.get(key, {}).get(item)
            if want is None:
                t.bad.append(f"{item}: no such item with a text digest in this checkout")
                continue
            try:
                got = text_digest(data)
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                t.bad.append(f"{item}: unreadable gzip ({type(exc).__name__})")
                continue
            if got != want:
                t.bad.append(f"{item}: text hashes to {got[:12]}, pinned {want[:12]}")
                continue
            if put(dest, key, item, data):
                t.written += 1
            else:
                t.present += 1


def setting(name: str) -> str:
    """``name`` from the environment, else by name from ``$STACK_ENV_FILE`` (never printed)."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    path = os.environ.get(ENV_FILE_ENV, "").strip()
    return env_file_values(path, (name,)).get(name, "") if path else ""


def bucket_of(store: str | None) -> str:
    for v in ([store] if store else [setting(n) for n in BUCKET_SETTINGS]):
        if v and v.startswith("s3://") and v.removeprefix("s3://").split("/", 1)[0]:
            return v.removeprefix("s3://").split("/", 1)[0]
    raise SystemExit(f"error: no bucket: pass --store s3://<bucket>/<prefix> or set {' or '.join(BUCKET_SETTINGS)} "
                     f"(environment or ${ENV_FILE_ENV})")


def download(bucket: str, key: str) -> bytes:
    """The archive at ``key``, checked against its folder's ``SHA256SUMS`` (read-only client)."""
    folder, _, name = key.rpartition("/")
    cfg = store_config(f"s3://{bucket}/{folder}")
    client = cfg.client(read_only=True)
    try:
        sums = client.get_object(Bucket=bucket, Key=f"{folder}/SHA256SUMS")["Body"].read().decode("utf-8")
        data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — reported redacted
        raise SystemExit(f"error: reading {key} or its SHA256SUMS: {redact_error(exc)}") from None
    want = {ln.split()[1]: ln.split()[0] for ln in sums.splitlines() if len(ln.split()) == 2}
    got = hashlib.sha256(data).hexdigest()
    if want.get(name) != got:
        raise SystemExit(f"error: {key}: sha256 {got[:12]} does not match its SHA256SUMS "
                         f"({(want.get(name) or 'no entry')[:12]})")
    print(f"{key}: {len(data):,} bytes, sha256 {got[:12]} = SHA256SUMS")
    return data


def excerpts(get: Callable[[str], bytes] | None, pins_: dict[str, str], dest: Path) -> int:
    """The archived eval files (``evals/archived.sha256``, D793) into ``dest/<path>``: fetched with ``get`` (a key
    under ``evals/`` -> bytes; None: verify the cache only), each checked against its pin. Prints one line per
    file; returns the number missing or refused."""
    failed = 0
    for rel, want in sorted(pins_.items()):
        p = dest / rel
        if get is not None and not (p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == want):
            data = get(rel)
            if hashlib.sha256(data).hexdigest() != want:
                print(f"  {rel}: refused, sha256 is not its pin")
                failed += 1
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.with_name(p.name + ".part").write_bytes(data)
            p.with_name(p.name + ".part").replace(p)
            print(f"  {rel}: written")
            continue
        ok = p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == want
        failed += not ok
        print(f"  {rel}: {'ok' if ok else 'MISSING' if not p.is_file() else 'does not match its pin'}")
    return failed


def s3_getter(bucket: str) -> Callable[[str], bytes]:
    """GET ``<prefix>evals/<rel>`` from the first of :data:`EXCERPTS_PREFIXES` that holds it, on a read-only
    client; errors redacted."""
    cfg = store_config(f"s3://{bucket}/{EXCERPTS_PREFIX.rstrip('/')}")
    client = cfg.client(read_only=True)

    def get(rel: str) -> bytes:
        last = ""
        for prefix in EXCERPTS_PREFIXES:
            try:
                return client.get_object(Bucket=bucket, Key=f"{prefix}evals/{rel}")["Body"].read()
            except Exception as exc:  # noqa: BLE001 — reported redacted; the next prefix is tried
                last = redact_error(exc)
        raise SystemExit(f"error: evals/{rel} is in none of {', '.join(EXCERPTS_PREFIXES)} ({last})")
    return get


def check(pinned: dict[Key, dict[str, str]], dest: Path, tally: dict[Key, Tally]) -> None:
    """Every cached text of a pinned set against its pin (no network)."""
    for key, items in pinned.items():
        for item, want in items.items():
            p = dest / key[0] / key[1] / f"{item}.txt.gz"
            if not p.is_file():
                continue
            try:
                ok = text_digest(p.read_bytes()) == want
            except (OSError, EOFError, gzip.BadGzipFile):
                ok = False
            if ok:
                tally[key].present += 1
            else:
                tally[key].bad.append(f"{item}: cached text does not match its pin")


def report(pinned: dict[Key, dict[str, str]], dest: Path, tally: dict[Key, Tally]) -> int:
    failed = 0
    for key in sorted(set(pinned) | set(tally)):
        need = pinned.get(key, {})
        t = tally.get(key)
        name = f"{key[0]}/{key[1]}"
        if t is None or (t.written + t.present == 0 and not t.bad):
            print(f"  {name}: {len(need)} items, no texts found")
            continue
        have = t.written + t.present
        missing = len(need) - have
        status = "ok" if not missing and not t.bad else "INCOMPLETE"
        failed += status != "ok"
        print(f"  {name}: {have}/{len(need)} verified ({t.written} written, {t.present} already there"
              f"{f', {len(t.bad)} refused' if t.bad else ''}) {status}")
        for b in t.bad[:5]:
            print(f"    refused {b}")
    total = sum(len(v) for v in pinned.values())
    have = sum(t.written + t.present for t in tally.values())
    print(f"cache {dest}: {have} of {total} pinned texts verified; {failed} set(s) incomplete")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--store", help="s3://<bucket>/<prefix> (only the bucket is used); default $STACK_STORE, "
                                    "else $STACK_S3_BASE_URI")
    ap.add_argument("--archive", action="append", help=f"archive key from the bucket root (repeatable; default "
                                                       f"{', '.join(DEFAULT_ARCHIVES)})")
    ap.add_argument("--from-tar", type=Path, action="append", help="a local tar.zst instead of S3 (repeatable)")
    ap.add_argument("--dest", type=Path, help=f"the cache root (default ${TEXTS_CACHE_ENV} or the XDG cache)")
    ap.add_argument("--check", action="store_true", help="verify the cache only (no network)")
    args = ap.parse_args()
    dest = (args.dest or texts_cache_root()).expanduser()
    tars = [p.read_bytes() for p in args.from_tar or []]
    bucket = None if args.check or tars else bucket_of(args.store)
    get: Callable[[str], bytes] | None = None if args.check else tar_getter(tars) if tars else s3_getter(bucket or "")
    print(f"archived eval files (D793, D795, D796; {', '.join(EXCERPTS_PREFIXES)}):")
    bad = excerpts(get, archived_pins(EVALS), dest)
    pinned = pins(EVALS, dest)
    tally: dict[Key, Tally] = defaultdict(Tally)
    if args.check:
        check(pinned, dest, tally)
    elif tars:
        for b in tars:
            ingest(b, pinned, dest, tally)
    else:
        for key in args.archive or DEFAULT_ARCHIVES:
            ingest(download(bucket or "", key), pinned, dest, tally)
    code = report(pinned, dest, tally)
    return 1 if code or bad else 0

if __name__ == "__main__":
    raise SystemExit(main())
