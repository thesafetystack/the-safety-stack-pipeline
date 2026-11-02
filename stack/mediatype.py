"""What media type to stamp on a snapshot, from the bytes.

Shared by every workflow that acquires bytes, because the stamp is write-once. fetch stamps
``snapshots.media_type`` and ``convert.docling`` dispatches on that stamp BEFORE it re-sniffs, so
two acquisition workflows disagreeing here is silent and permanent: a JATS body stamped
``text/html`` is routed to the HTML backend forever and can only be repaired by re-fetching.

The byte-level precedence below MIRRORS ``convert.docling``'s ``classify()``
(stack/conversion/ladder.py) on purpose. That copy stays duplicated and cannot import
this module: the ladder is pure conversion logic that runs in a subprocess with no ``stack.*``
imports at all. ``tests/test_fetch_convert_contract.py`` pins the two together — if you change the
order here, that test fails, which is the point.
"""

from __future__ import annotations

# NOTE for readers arriving from fetch.http: that module's ``looks_like_html`` is deliberately
# NOT reused here. It matches ``<?xml`` (so it answers True for JATS) and it also drives
# landing-page PDF resolution, where that looseness is harmless. Narrowing it is a separate
# behavioural change.

_JATS_MARKERS: tuple[bytes, ...] = (b"//nlm//dtd jats", b"<article")

# kind → the media type stamped on the snapshot. ``text`` keeps the historical
# ``application/octet-stream`` (convert re-sniffs it and reaches the same verdict); changing it
# would churn every existing non-markup snapshot's stamp for no gain.
_KIND_MEDIA_TYPE: dict[str, str] = {
    "pdf": "application/pdf",
    "html": "text/html",
    "xml": "application/xml",
    "text": "application/octet-stream",
}

# Declared Content-Type → kind, consulted ONLY when the sniff is inconclusive (see
# :func:`media_type_for`). Servers mislabel far more often than magic bytes lie — sniffing is
# primary precisely because of the v1 Cloudflare/publisher experience — so this is a tiebreaker,
# never an override.
_DECLARED_KIND: tuple[tuple[str, str], ...] = (
    ("application/pdf", "pdf"),
    ("+xml", "xml"),          # application/jats+xml, application/xhtml+xml → XML backends
    ("application/xml", "xml"),
    ("text/xml", "xml"),
    ("text/html", "html"),
)


def sniff_kind(data: bytes) -> str:
    """``"pdf" | "html" | "xml" | "text"`` from magic bytes, in convert ``classify()`` order.

    The JATS check sits BELOW the generic HTML test (an XHTML document declaring ``<html`` is
    HTML) but ABOVE the bare ``<?xml`` prolog, so a JATS payload claims the JATS backend rather
    than falling into the generic-XML bucket."""
    if data[:5] == b"%PDF-" or data[:4] == b"%PDF":
        return "pdf"
    low = data[:2048].lstrip().lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html") or b"<html" in low:
        return "html"
    if any(marker in low for marker in _JATS_MARKERS):
        return "xml"
    if low.startswith(b"<?xml"):
        return "xml"
    return "text"


def media_type_for(body: bytes, declared: str | None = None) -> str:
    """The media type to stamp on a snapshot: the magic-byte sniff, with a specific declared
    ``Content-Type`` breaking the tie only when the sniff is inconclusive.

    Europe PMC labels its JATS ``application/xml`` correctly, but plenty of hosts serve a PDF as
    ``text/html`` or everything as ``octet-stream``; trusting the header over the bytes would be
    a regression. So ``declared`` is consulted only where the bytes say nothing useful."""
    kind = sniff_kind(body)
    if kind == "text" and declared:
        d = declared.split(";", 1)[0].strip().lower()
        for needle, dk in _DECLARED_KIND:
            if needle in d:
                kind = dk
                break
    return _KIND_MEDIA_TYPE[kind]


__all__ = ["media_type_for", "sniff_kind"]
