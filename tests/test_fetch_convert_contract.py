"""The fetch↔convert media-type contract.

The ``http`` lane (v3's ``fetch.http``) stamps ``snapshots.raw.media_type``; ``acquire``'s ``convert``
substage dispatches on that stamp BEFORE it re-sniffs the bytes. The two therefore have to agree
about what a body is — and they cannot share code, because the convert child imports no
``stack.*`` module. So the precedence is deliberately duplicated (``stack.mediatype.sniff_kind``,
the lane's, mirrors ``stack.conversion.ladder.classify``, which the convert child runs) and pinned
here.

This is the test that would have caught the bug it was written for: ``looks_like_html`` answers
True for ``<?xml``, so JATS full text used to be stamped ``text/html``, and convert — trusting the
stamp — routed the publisher's own structural markup to the generic HTML backend instead of the
JATS one. Snapshots are write-once, so that mistake could only ever be repaired by re-fetching.

Fixtures below are trimmed from real bytes: an Europe PMC ``fullTextXML`` response, an
``arxiv.org/html/<id>`` LaTeXML rendering, and arXiv's "no HTML" miss page.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

# The stamp is the http lane's (``workflows/acquire/lanes/http.py``, v3's fetch.http moved there
# at T3.2): it stamps with ``stack.mediatype.media_type_for`` and re-exports the names used here.
from workflows.acquire.lanes import http as fh

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The convert child runs stack/conversion/ladder.py (the workflows/convert/docling copy was a dead
# duplicate, retired at T1.8; its main.py is now workflows/acquire/convert.py).
ladder = _load("_ladder_contract", REPO / "stack" / "conversion" / "ladder.py")


# ─────────────────────────────────────────────────────────────────────────────
# fixtures (trimmed from real responses)
# ─────────────────────────────────────────────────────────────────────────────
# Europe PMC serves JATS with a leading newline, then the NLM doctype — note there is NO <?xml
# prolog and no <html anywhere, so the ONLY signal that this is JATS is the doctype.
JATS_BYTES = (
    b"\n<!DOCTYPE article\n"
    b'  PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD with '
    b'MathML3 v1.4 20241031//EN" "JATS-archivearticle1-4-mathml3.dtd">\n'
    b'<article xml:lang="en" article-type="research-article">'
    b"<front><article-meta><title-group><article-title>A paper</article-title>"
    b"</title-group></article-meta></front><body><sec><p>Body text.</p></sec></body>"
    b"</article>"
)

# arXiv's LaTeXML output: an ordinary HTML document whose article body carries ltx_ classes.
ARXIV_HTML_BYTES = (
    b'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    b"<title>A paper</title></head><body>"
    b'<article class="ltx_document"><h1 class="ltx_title">A paper</h1>'
    b'<section class="ltx_section"><p class="ltx_p">Body text.</p></section>'
    b"</article></body></html>"
)

# A generic XML payload that is NOT JATS — must still reach an XML backend, not the HTML one.
PLAIN_XML_BYTES = b'<?xml version="1.0" encoding="UTF-8"?><records><record id="1"/></records>'

PDF_BYTES = b"%PDF-1.7\n" + b"x" * 20_000
PLAIN_HTML_BYTES = b"<!doctype html><html><body>a landing page</body></html>"
TEXT_BYTES = b"just some plain prose with no markup at all, at all"


@pytest.mark.parametrize(
    "name, body, expect_kind, expect_media_type",
    [
        ("jats", JATS_BYTES, "xml", "application/xml"),
        ("plain-xml", PLAIN_XML_BYTES, "xml", "application/xml"),
        ("arxiv-latexml", ARXIV_HTML_BYTES, "html", "text/html"),
        ("plain-html", PLAIN_HTML_BYTES, "html", "text/html"),
        ("pdf", PDF_BYTES, "pdf", "application/pdf"),
        ("text", TEXT_BYTES, "text", "application/octet-stream"),
    ],
)
def test_fetch_sniff_and_convert_classify_agree(
    name: str, body: bytes, expect_kind: str, expect_media_type: str
) -> None:
    """What fetch stamps must be what convert dispatches on. Asserted against the REAL
    ``classify()``, not a hand-copied expectation — a copy would drift with the thing it mirrors."""
    assert fh.sniff_kind(body) == expect_kind, name
    media_type = fh.media_type_for(body)
    assert media_type == expect_media_type, name
    assert ladder.classify(media_type, body) == expect_kind, name
    # …and with NO stamp to dispatch on, classify falls through to its OWN magic-byte sniff — the
    # copy of `sniff_kind` that the cardinal rule forces to exist. The assertion above cannot reach
    # it (every fixture here carries a media_type, so classify returns from its string-match
    # prefix), which left the one duplicated-precedence surface this file exists to pin unpinned.
    # Matters for real rows: a snapshot with a NULL media_type is dispatched by this branch alone.
    assert ladder.classify(None, body) == expect_kind, name


def test_jats_is_not_stamped_as_html() -> None:
    """The specific regression. ``looks_like_html`` still answers True for JATS (it is used by
    landing-page resolution, where the looseness is harmless) — the point is that the media-type
    stamp no longer follows it."""
    assert fh.looks_like_html(JATS_BYTES) is True
    assert fh.media_type_for(JATS_BYTES) == "application/xml"
    # And the old stamp really would have mis-routed it:
    assert ladder.classify("text/html", JATS_BYTES) == "html"
    assert ladder.classify("application/xml", JATS_BYTES) == "xml"


def test_declared_content_type_only_breaks_ties() -> None:
    """A declared type refines an inconclusive sniff; it never overrides a confident one. Servers
    mislabel far more often than magic bytes lie (the v1 Cloudflare/publisher experience)."""
    # Inconclusive bytes + a specific header → the header decides.
    assert fh.media_type_for(TEXT_BYTES, "application/xml") == "application/xml"
    assert fh.media_type_for(TEXT_BYTES, "application/jats+xml; charset=utf-8") == "application/xml"
    assert fh.media_type_for(TEXT_BYTES, "application/pdf") == "application/pdf"
    # Confident bytes + a WRONG header → the bytes win.
    assert fh.media_type_for(PDF_BYTES, "text/html") == "application/pdf"
    assert fh.media_type_for(JATS_BYTES, "text/html") == "application/xml"
    assert fh.media_type_for(PLAIN_HTML_BYTES, "application/xml") == "text/html"
    # No header at all → unchanged behaviour.
    assert fh.media_type_for(TEXT_BYTES) == "application/octet-stream"
