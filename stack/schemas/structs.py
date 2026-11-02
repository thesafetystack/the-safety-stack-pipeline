"""Appendix A.1 notation and the A.2 structs as pyarrow types (frozen, D43).

Notation (A.1): ``S`` string, ``B`` bool, ``I`` int32, ``L`` int64, ``D`` float64, ``TS``
timestamp(us, UTC), ``LS(t)`` list, ``ST(...)`` struct. A keyword ending in ``_`` declares the
field non-nullable (A.1's ``!``); pyarrow does not enforce that on write, the S2 check does.

Kept apart from ``blocks.py`` so the payload modules can use the structs without importing the
``annotations`` block, which is itself built from the payload registry.

Amendments to A.2 as written: ``SNAPSHOT`` drops ``converted_from`` and gains
``text_error_class`` next to ``text_status`` (D9); ``ROLE`` gains ``lane`` at its end (D11);
``SNAPSHOT`` gains ``quality`` at its end (round 3, Q: the LLM-free quality of its text,
``stack/conversion/quality.py``; nullable, so stored files upcast with NULL).

Blob references (D690, documents/corpus v6, sources v4): a reference to a blob is a ``BLOB`` struct
``{sha256, bytes, media_type}`` named by its role (``raw``, ``text``), all three non-null (the size is
recorded when the blob is written); a flat string reference ends in ``_sha256``; ``*_digest`` is a
fingerprint, always. ``SNAPSHOT.digest``/``bytes``/``media_type`` are ``raw``, ``text_digest`` is
``text`` (:func:`text_blob`), ``convert_config_digest`` is ``convert_pin``; an ``INBAND_ENTRY``'s
``raw_digest``/``text_digest`` (+ ``media_type``) are ``raw``/``text``; ``MEMBER.inband_digests`` is
gone (derivable from the entries). ``QUALITY.tier`` is the converter's ladder tier (the stamp's
``+tier<k>``) and ``ROLE.rule`` names the acquire rule that set a role's status (``arxiv_pdf``), so
no rule reads a marker inside a string.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pyarrow as pa

S = pa.string()
B = pa.bool_()
I = pa.int32()
L = pa.int64()
D = pa.float64()
TS = pa.timestamp("us", tz="UTC")


def LS(t: pa.DataType) -> pa.DataType:
    """``LS(t)``: a list of ``t`` (items nullable; the list itself per its field)."""
    return pa.list_(t)


def fields(**kw: pa.DataType) -> list[pa.Field]:
    """Fields in keyword order; a name ending in ``_`` is non-nullable (A.1's ``!``)."""
    return [pa.field(k.rstrip("_"), t, nullable=not k.endswith("_")) for k, t in kw.items()]


def ST(**kw: pa.DataType) -> pa.StructType:
    """``ST(...)``: a struct of :func:`fields`."""
    return pa.struct(fields(**kw))


# ---------------------------------------------------------------- A.1 tiers
T0 = fields(status_=S, run_id_=S)
T1 = T0 + fields(error_class=S, error=S)
T2 = T1 + fields(attempts_=I, first_attempt_at=TS, last_attempt_at=TS, retry_after=TS)

# ---------------------------------------------------------------- A.2 structs
AUTHOR = ST(name_=S, orcid=S, affiliation_raw=S)
KEY = ST(kind_=S, value_=S)
KEYSRC = ST(kind_=S, value_=S, source_id_=S)
MEMBER = ST(
    source_id_=S,
    channel_=S,
    key_kind_=S,
    key_value_=S,
    canonical_url=S,
    title=S,
    published_at=TS,
    doc_type=S,
    license_class=S,
    is_live_=B,
    edge_kind_=S,
    edge_to_source_id=S,
    override_id=S,
    first_observed_at_=TS,
    last_observed_at_=TS,
    observation_count_=I,
    withdrawn_at=TS,
    withdrawn_reason=S,
)
#: A blob reference (D690): the blob's sha256 (its key ``blobs/<sha256>``), its size and the media
#: type of these bytes.
BLOB = ST(sha256_=S, bytes_=L, media_type_=S)
#: The media type of a text that is not its raw bytes: every converter and the X collector write
#: Markdown (:func:`text_blob`).
TEXT_MEDIA_TYPE = "text/markdown"
INBAND_ENTRY = ST(
    source_id_=S,
    content_role_=S,
    raw=BLOB,
    text=BLOB,
    captured_at_=TS,
)
#: The measured metrics of a text (``stack.conversion.quality.METRIC_FIELDS``, same order).
QUALITY_METRICS = ST(
    chars=L,
    words=L,
    lines=L,
    pages=I,
    raw_bytes=L,
    chars_per_page=D,
    text_raw_ratio=D,
    truncated=B,
    replacement_per_1k=D,
    mojibake_per_1k=D,
    binary_ratio=D,
    pua_ratio=D,
    link_density=D,
    link_line_ratio=D,
    markup_ratio=D,
    html_tags_per_1k=D,
    table_rows=I,
    table_bad_ratio=D,
    latex_per_1k=D,
    glyph_line_ratio=D,
    glyph_runs_per_1k=D,
    figure_text_ratio=D,
    stub_hits=LS(S),
    repeated_line_ratio=D,
    abstract_ratio=D,
    body_sections=I,
    landing_hits=I,
    ref_line_ratio=D,
    ref_start_ratio=D,
    en_ratio=D,
    non_latin_ratio=D,
)
#: A text's quality verdict (round 3, Q): ``version`` = the checks version that measured
#: ``metrics``; ``bucket`` ``ok``/``flagged``/``failed``; ``flags`` every check that fired,
#: ``fails`` those at fail level; ``gate_digest`` the gate that judged; ``converter`` the stamp
#: of the tool that made the text (``<tool>@<version>+tier<k>``, ``inband`` for a collector's);
#: ``tier`` that stamp's tier ``<k>`` (D690; NULL without a tier, e.g. ``inband``).
QUALITY = ST(
    version_=I,
    bucket_=S,
    flags_=LS(S),
    fails_=LS(S),
    gate_digest_=S,
    converter=S,
    tier=S,
    metrics=QUALITY_METRICS,
)
SNAPSHOT = ST(
    snapshot_id_=S,
    source_id_=S,
    kind_=S,
    content_role_=S,
    via_=S,
    url=S,
    final_url=S,
    http_status=I,
    etag=S,
    last_modified=S,
    captured_at_=TS,
    as_of=TS,
    revision=S,
    raw_=BLOB,
    text=BLOB,
    text_status=S,
    text_error_class=S,  # D9
    convert_attempts_=I,
    convert_pin=S,
    quality=QUALITY,  # round 3, Q
)
ROLE = ST(
    content_role_=S,
    source_id=S,
    status_=S,
    error_class=S,
    error=S,
    attempts_=I,
    first_attempt_at=TS,
    last_attempt_at=TS,
    last_outcome=S,
    retry_after=TS,
    tried_urls_=LS(S),
    last_source_id=S,
    etag=S,
    last_modified=S,
    validator_url=S,
    snapshot_id=S,
    lane=S,  # D11
    rule=S,  # D690: the acquire rule that set the status (``arxiv_pdf``), NULL for a lane's outcome
)
REVISION = ST(revision_=S, published_at=TS, title=S)
QUOTE = ST(text_=S, offset=I)
AFFIL = ST(surface_=S, org_slugs_=LS(S))
QUESTION = ST(
    question_=S,
    status_=S,
    error_class=S,
    verdict=S,
    confidence=D,
    evidence_id=S,
    config_digest_=S,
)


#: The ``BLOB`` parts of a snapshot and of an in-band entry.
BLOB_PARTS: tuple[str, ...] = ("raw", "text")


def blob_refs(row: Mapping[str, Any]) -> Iterator[tuple[str, int, str, dict[str, Any]]]:
    """Every ``BLOB`` a ``documents``/``corpus`` or ``sources`` row (as ``to_pylist`` yields it) references:
    ``(holder, index, part, blob)`` with ``holder`` ``snapshot`` (``content.snapshots[index]``) or ``inband``
    (``inband.entries[index]``) and ``part`` ``raw``/``text``; NULL parts are skipped. The one place that
    knows where a row's blob references are (D690)."""
    holders = (("snapshot", (row.get("content") or {}).get("snapshots")),
               ("inband", (row.get("inband") or {}).get("entries")))
    for holder, items in holders:
        for i, h in enumerate(items or []):
            for part in BLOB_PARTS:
                if (b := h.get(part)) is not None:
                    yield holder, i, part, b


def text_blob(sha256: str, size: int, raw: dict[str, object] | None) -> dict[str, object]:
    """The ``text`` BLOB of a snapshot whose text is ``sha256`` (``size`` bytes) over ``raw`` (its
    ``raw`` BLOB): the raw's media type when the text is the raw bytes and those are ``text/*`` (a
    collector's text, a plain-text page that converts to itself), else :data:`TEXT_MEDIA_TYPE` (D690;
    the D647 release rule)."""
    raw_media = str((raw or {}).get("media_type") or "")
    same = raw is not None and raw.get("sha256") == sha256
    return {"sha256": sha256, "bytes": size,
            "media_type": raw_media if same and raw_media.startswith("text/") else TEXT_MEDIA_TYPE}
