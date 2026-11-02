"""In-band content capture for collectors (§5.4, D8, C20, C33; v3 D-INBAND1).

A discovery response often carries usable prose *inside* the bytes collect already paid for: an
arXiv Atom entry ships its ``<summary>`` abstract, an OpenAlex hit ships a (de-invertible)
abstract, a forum search ships the post body. Collect makes no *content* fetches, but persisting
what arrived in-band breaks no invariant: no extra request is made. This module is the one place
that mints it, so every channel mints the same way.

**What a mint is (v4).** The bytes go to the content-addressed blob store (``ctx.blobs``): a body
as delivered (HTML, JSON) as the ``raw`` BLOB; prose that already is text (an
abstract, a de-inverted index) as the ``text`` BLOB after :func:`normalize` (``raw``
NULL, C33); both in the flat ``blobs/`` (D360), each BLOB with its size and media type (D690). The mint
returns one ``INBAND_ENTRY`` (A.2 as amended by D690: ``source_id, content_role, raw, text, captured_at``),
which the collector puts on
the ``sources`` row's ``inband.entries[]``; ``gather`` lists it on the document, ``acquire``
makes it an ``inband`` snapshot (C20) and converts a raw-only entry. v3 minted a raw-less
``partial`` snapshot through ``Writer.import_snapshot``; that DB path is gone.

**The mint rule** (v3 ``store_inband_text``, the guard at v3 ``:144-151``: "never mint onto a
source that already has a snapshot", so an abstract never regressed a full text). In v4 the
pointer rule ranks by role before currency (§5.5) and a ``sources`` row carries its in-band
entries **per role**, so the guard becomes: a mint only ever replaces the entry of its own role,
never another role's; an abstract minted after a body leaves the body standing, and ``acquire``
ranks the body first. Per offered ``(source_id, content_role)`` against the entries the source
already carries (its latest row in the collector's own chain, plus this run):

1. shorter than :data:`MIN_CHARS` after normalisation (text) or empty (raw): not minted (noise);
2. the same role with the same ``sha256`` pair: not minted again; the carried entry (its first
   ``captured_at``) stands, so re-sweeps are idempotent;
3. otherwise minted: a first entry for the role, or a changed body (§5.6: new body = new
   ``inband`` snapshot) replaces that role's entry.

Entries of roles the observation did not offer are carried onto the new row (a re-observation
without a body keeps the body). :class:`InbandSink` applies the rule and keeps v3's tallies and
circuit breaker.

**Provider records (D522).** The role ``provider_record`` (:data:`~stack.schemas.vocab.RECORD_ROLES`)
is not content: its raw blob is the source's record of the item as received, a JSON item as
:func:`record_bytes` (canonical JSON, ``application/json``) or arXiv's Atom ``<entry>``
(``application/atom+xml``); no ``text``; ``captured_at`` = the observation time. It goes
through the same mint rule (an unchanged record keeps its first ``captured_at``), on a sink of its
own in the harness (:meth:`stack.collect.base.Collector.add_record`, counted ``record_*``), and
``acquire`` never mints it. **Freshness:** a collector that observes with ``skip_unchanged`` writes no
row (so no record) while its ``record_projection`` digest is unchanged, so a record is the snapshot
from when the projected fields last changed; counters such as citation counts go stale between
those changes, and ``captured_at`` says when the record was taken.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import json

from stack.core.hashes import jcs, utf8_safe
from stack.core.hashes import raw_digest as _raw_digest
from stack.core.hashes import text_digest as _text_digest
from stack.schemas.vocab import INBAND_ROLES, RECORD_ROLES

#: The ``provider_record`` role (D522) and the media types of its blob.
RECORD_ROLE = RECORD_ROLES[0]
JSON_MEDIA_TYPE = "application/json"
ATOM_MEDIA_TYPE = "application/atom+xml"

MIN_CHARS = 30  # shorter than this is noise, not an abstract

# After this many CONSECUTIVE blob-store errors the sink trips its breaker and no-ops for the rest
# of the run: a broken blob store must not turn discovery (the primary product) into thousands of
# exceptions; in-band capture is bonus.
_BREAKER_THRESHOLD = 3

# 3+ consecutive newlines → a single blank line: paragraph structure kept, blank runs bounded.
_BLANK_RUN = re.compile(r"\n{3,}")

Entry = dict[str, Any]


class BlobSink(Protocol):
    """What the mint needs of ``ctx.blobs`` (:class:`stack.store.blobs.BlobStore`)."""

    def put(self, data: bytes) -> str: ...


def normalize(text: str) -> str:
    """Gentle normalisation that PRESERVES paragraph structure (forum bodies are multi-paragraph):
    line endings to ``\\n``, trailing whitespace stripped per line, runs of 3+ newlines collapsed
    to one blank line, outer whitespace stripped. Interior whitespace is NOT collapsed (v3). Surrogate
    code points (a provider's lone ``\\ud83d`` escape) are repaired first, a lone one to U+FFFD
    (:func:`~stack.core.hashes.utf8_safe`, D586), so the text encodes as UTF-8."""
    t = utf8_safe(text).replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = _BLANK_RUN.sub("\n\n", t)
    return t.strip()


def parse_captured_at(value: str | dt.datetime) -> dt.datetime:
    """An aware UTC datetime from an ISO-8601 date/timestamp or a datetime; naive = UTC. Raises
    ``ValueError`` on a malformed string (a caller bug)."""
    d = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value.strip())
    return d.astimezone(dt.UTC) if d.tzinfo is not None else d.replace(tzinfo=dt.UTC)


def blob(sha256: str, data: bytes, media_type: str) -> dict[str, Any]:
    """A ``BLOB`` reference (D690) to ``data`` stored under ``sha256``."""
    return {"sha256": sha256, "bytes": len(data), "media_type": media_type}


def mint_entry(blobs: BlobSink, source_id: str, content_role: str, *, text: str | None = None,
               raw: bytes | None = None, media_type: str, text_media_type: str | None = None,
               captured_at: str | dt.datetime) -> Entry | None:
    """Mint one in-band body into the blob store and return its ``INBAND_ENTRY``, or ``None`` when
    the body is noise (text under :data:`MIN_CHARS` after :func:`normalize`, or empty raw bytes).
    ``raw`` → the ``raw`` BLOB (``media_type``); ``text`` → the ``text`` BLOB (normalised; ``media_type``,
    or ``text_media_type``, which an offer of both must give: D690); both may be given. No rule
    beyond the noise floor: :class:`InbandSink` applies the mint rule."""
    if content_role not in INBAND_ROLES:
        raise ValueError(f"in-band content_role {content_role!r} not in {INBAND_ROLES}")
    if content_role in RECORD_ROLES and text is not None:
        raise ValueError(f"in-band {content_role!r} is a raw record: no text (D522)")
    norm = normalize(text) if text else None
    if norm is not None and len(norm) < MIN_CHARS:
        norm = None
    raw_ = raw or None
    if text is not None and raw_ is not None and text_media_type is None:
        raise ValueError(f"in-band {content_role!r}: an offer of raw bytes and text names the text's media type "
                         "(text_media_type; D690)")
    if norm is None and raw_ is None:
        return None
    body = norm.encode("utf-8") if norm is not None else None
    return {"source_id": source_id, "content_role": content_role,
            "raw": blob(blobs.put(raw_), raw_, media_type) if raw_ is not None else None,
            "text": blob(blobs.put(body), body, text_media_type or media_type) if body is not None else None,
            "captured_at": parse_captured_at(captured_at)}


def record_bytes(record: Any) -> bytes:
    """A JSON provider record as its ``provider_record`` blob (D522): canonical JSON (JCS: sorted
    keys, no whitespace, UTF-8), so the same record always has the same digest and dedupes. Non-JSON
    leaves (datetimes) are rendered with ``str`` first; surrogate code points in its strings are
    repaired (:func:`~stack.core.hashes.utf8_safe`, D586)."""
    return utf8_safe(jcs(json.loads(json.dumps(record, default=str, ensure_ascii=False)))).encode("utf-8")


def record_entry(row: Mapping[str, Any]) -> Entry | None:
    """The ``provider_record`` entry of a ``sources`` row (D522), or ``None``."""
    entries = (row.get("inband") or {}).get("entries") or []
    return next((e for e in entries if e["content_role"] == RECORD_ROLE), None)


def json_record(blobs: Any, row: Mapping[str, Any]) -> Any | None:
    """The row's ``provider_record`` as parsed JSON (D524: where ``metadata.record_ref`` was read), or
    ``None`` when the row has none, its blob is missing, or it is not JSON (arXiv's Atom ``<entry>``).
    ``blobs``: a store with ``get(digest) -> bytes``."""
    e = record_entry(row)
    if e is None or e["raw"] is None or e["raw"]["media_type"] != JSON_MEDIA_TYPE:
        return None
    try:
        return json.loads(blobs.get(e["raw"]["sha256"]))
    except (KeyError, OSError, ValueError):
        return None


def entry_sha256s(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(raw.sha256, text.sha256)`` of an ``INBAND_ENTRY`` (NULL parts ``None``)."""
    return ((entry.get("raw") or {}).get("sha256"), (entry.get("text") or {}).get("sha256"))


def offer_digests(text: str | None, raw: bytes | None) -> tuple[str | None, str | None]:
    """``(raw sha256, text sha256)`` an offer would mint, without writing (the unchanged test)."""
    norm = normalize(text) if text else None
    td = _text_digest(norm) if norm is not None and len(norm) >= MIN_CHARS else None
    rd = _raw_digest(raw) if raw else None
    return rd, td


class InbandSink:
    """Per-run mint rule, tallies and circuit breaker around :func:`mint_entry` (v3 semantics).

    :meth:`offer` returns the entry the source's row should carry for the role (a fresh mint or
    the carried prior one), or ``None``. Counting (v3): disabled ⇒ no-op, nothing counted; no
    text and no raw ⇒ ``absent``; minted ⇒ ``minted`` (+ ``backfill`` when the caller says the
    source existed before this run); noise or an unchanged body ⇒ ``skipped``; a
    blob-store exception ⇒ ``errors`` (the FIRST message kept; the 3rd in a row disables the
    sink). A non-error outcome resets the consecutive-error run."""

    def __init__(self, blobs: BlobSink, *, now: Callable[[], dt.datetime] | None = None) -> None:
        self._blobs = blobs
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self.minted = 0
        self.backfill = 0
        self.skipped = 0
        self.absent = 0
        self.errors = 0
        self._consecutive = 0
        self.disabled = False
        self.first_error: str | None = None

    def offer(self, source_id: str, content_role: str, *, text: str | None = None,
              raw: bytes | None = None, media_type: str, text_media_type: str | None = None,
              carried: Mapping[str, Entry] | None = None,
              captured_at: str | dt.datetime | None = None, backfill: bool = False) -> Entry | None:
        """Apply the mint rule (module docstring) for one offer against ``carried`` (role → the
        entry the source already carries)."""
        if self.disabled:
            return None
        if not text and not raw:
            self.absent += 1
            return None
        if text and raw and text_media_type is None:  # a caller's bug, not a blob-store error: loud
            raise ValueError(f"in-band {content_role!r}: an offer of raw bytes and text names the text's media "
                             "type (text_media_type; D690)")
        prior = (carried or {}).get(content_role)
        if prior is not None:
            rd, td = offer_digests(text, raw)
            if (rd, td) == entry_sha256s(prior):
                self._ok()
                self.skipped += 1
                return prior
        try:
            entry = mint_entry(self._blobs, source_id, content_role, text=text, raw=raw, media_type=media_type,
                               text_media_type=text_media_type, captured_at=captured_at or self._now())
        except Exception as exc:  # noqa: BLE001 — a broken blob store must not kill discovery
            self.errors += 1
            self._consecutive += 1
            if self.first_error is None:
                self.first_error = str(exc)
            if self._consecutive >= _BREAKER_THRESHOLD:
                self.disabled = True
            return None
        self._ok()
        if entry is None:
            self.skipped += 1
            return None
        self.minted += 1
        if backfill:
            self.backfill += 1
        return entry

    def _ok(self) -> None:
        self._consecutive = 0

    @property
    def counts(self) -> dict[str, int]:
        """``inband_*`` tallies for ``ctx.counts`` (ints only; ``inband_disabled`` = 1 once the
        breaker tripped; the first error is in :attr:`first_error`)."""
        return {"inband_minted": self.minted, "inband_backfill": self.backfill,
                "inband_skipped": self.skipped, "inband_absent": self.absent,
                "inband_errors": self.errors, "inband_disabled": int(self.disabled)}


__all__ = ["ATOM_MEDIA_TYPE", "JSON_MEDIA_TYPE", "MIN_CHARS", "RECORD_ROLE", "BlobSink", "Entry",
           "InbandSink", "blob", "entry_sha256s", "json_record", "mint_entry", "normalize", "offer_digests", "parse_captured_at",
           "record_bytes", "record_entry"]
