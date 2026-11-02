"""The LLM response cache: ``<store>/cache/llm-responses/<key>.json.zst`` (D23, DESIGN §3.5).

``key = sha256(jcs([model, rendered_digest, basis_digest]))``. Only schema-valid responses are
stored; an entry holds the response body, its usage and the parsed JSON, so a hit rebuilds the
result without the provider. Writes are atomic (temp file + ``os.replace``); two writers of one
key write the same logical answer, the last rename wins. No lock: entries are immutable by key.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import zstandard

from stack.core.hashes import jcs
from stack.store.layout import StoreLayout

CACHE_DIR = "llm-responses"


def cache_key(model: str, rendered_digest: str, basis_digest: str) -> str:
    """``H([model, rendered_digest, basis_digest])`` (D23)."""
    return hashlib.sha256(jcs([model, rendered_digest, basis_digest]).encode("utf-8")).hexdigest()


class ResponseCache:
    """File cache under one directory (normally ``<store>/cache/llm-responses``)."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    @classmethod
    def for_store(cls, store_root: str | Path) -> ResponseCache:
        return cls(StoreLayout(Path(store_root)).cache / CACHE_DIR)

    def path(self, key: str) -> Path:
        if len(key) != 64 or not all(c in "0123456789abcdef" for c in key):
            raise ValueError(f"bad cache key {key!r}")
        return self.dir / f"{key}.json.zst"

    def get(self, key: str) -> dict[str, Any] | None:
        """The stored entry, or None. An unreadable entry raises (never silently re-asked)."""
        p = self.path(key)
        try:
            raw = p.read_bytes()
        except FileNotFoundError:
            return None
        try:
            entry = json.loads(zstandard.ZstdDecompressor().decompress(raw))
        except (zstandard.ZstdError, ValueError) as exc:
            raise ValueError(f"{p}: unreadable cache entry ({exc}); delete it to re-ask") from exc
        if not isinstance(entry, dict) or entry.get("key") != key:
            raise ValueError(f"{p}: cache entry does not carry its own key")
        return entry

    def put(self, key: str, entry: dict[str, Any]) -> Path:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({**entry, "key": key}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        tmp = p.parent / f".{p.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        try:
            tmp.write_bytes(zstandard.ZstdCompressor().compress(data))
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)
        return p
