"""arXiv: URL construction, the LaTeXML-HTML markup rungs, and the Atom API's version answer.

Used by ``acquire``'s arXiv lane (``workflows/acquire/lanes/arxiv.py``: ids, the versioned PDF URL,
the batched Atom version sweep, and with ``options.structured.arxiv_html`` the versioned LaTeXML HTML
URL, its soft-404 acceptance test :func:`markup_is_acceptable` and :data:`ARXIV_HTML_RECHECK_DAYS`;
round 5, T-RUN B8). The lane keeps the "no HTML" memory on its ``html`` role.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from stack.mediatype import sniff_kind

# ─────────────────────────────────────────────────────────────────────────────
# Identifiers and URLs
# ─────────────────────────────────────────────────────────────────────────────
# New-style ids (2007-04 on: YYMM.NNNNN) and old-style ones (archive/YYMMNNN), each with an
# optional version suffix. The old form keeps its archive prefix — it IS the id.
_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(?:v(\d+))?")
_ARXIV_OLD = re.compile(r"([a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?")


def normalize_arxiv_id(text: str) -> str:
    """The bare arXiv id inside ``text`` — a URL, an ``arXiv:`` prefix, a ``.pdf`` suffix and any
    ``vN`` version are all stripped. Empty string when nothing parses."""
    m = _ARXIV_NEW.search(text or "") or _ARXIV_OLD.search(text or "")
    return m.group(1) if m else ""


def arxiv_version_of(text: str) -> int | None:
    """The ``vN`` version carried by ``text`` (an Atom ``<id>``, a URL), if any."""
    m = _ARXIV_NEW.search(text or "") or _ARXIV_OLD.search(text or "")
    return int(m.group(2)) if m and m.group(2) else None


def arxiv_pdf_url(external_id: str, version: int | None = None) -> str:
    """The arXiv direct-PDF URL for a bare arXiv id (v1 download_fulltext: arxiv.org/pdf/<id>).
    With ``version`` the URL is pinned (``…/pdf/<id>v<N>``) — for fetch.arxiv, which has just
    resolved the current version from the Atom API and records it on the snapshot."""
    if version:
        return f"https://arxiv.org/pdf/{external_id}v{version}"
    return f"https://arxiv.org/pdf/{external_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Markup (HTML/JATS) rungs — tried BEFORE the standard rung, as bare probes
# ─────────────────────────────────────────────────────────────────────────────
# arXiv renders LaTeX submissions to HTML via LaTeXML. That HTML carries real structure —
# sectioning, tables as tables, math as MathML — where the PDF only carries page layout for
# convert to reconstruct. convert.docling already has the matching backends and even LaTeXML-
# specific table logic; nothing was feeding them.
#
# NO metadata API exposes these URLs (checked: OpenAlex gives only landing_page_url + pdf_url;
# DataCite gives url=abs, contentUrl=null; Crossref has no arXiv record at all because 10.48550
# is a DataCite prefix; the Atom API's two <link>s are abs + pdf). Like the PDF URL, it is
# constructed. robots.txt explicitly allows it: `Allow: /html` sits beside /abs and /pdf.
#
# NOT version-pinned, deliberately. The bare URL serves the CURRENT version; pinning to a version
# recorded in metadata would silently serve an OLDER revision than the PDF rung does whenever that
# metadata lags. Provenance is preserved anyway — snapshots.fetch_meta.url records what we asked
# for and observed_at records when.
ARXIV_HTML_MIN_BYTES = 20_000   # real LaTeXML pages measured 282KB–1.7MB; the miss page is ~7.7KB
_ARXIV_NO_HTML_RE = re.compile(rb"No HTML for|HTML is not available for the source", re.I)

# How long a recorded "this paper has no HTML" verdict is trusted before we look again. arXiv
# generates HTML from Dec 2023 on, keyed on the LATEST VERSION's submission — so an OLD paper
# GAINS HTML when it is revised (measured: arxiv.org/html/2201.05000v3, a 2022 id revised in
# 2026, returns 200 with 667KB). Any gate derived from the id's month or the original pub_date is
# therefore WRONG — it would permanently skip exactly the actively-maintained papers. A TTL
# recheck is the cheap correct alternative: it bounds the wasted probes to ~2/year for a source
# that genuinely has no HTML, while still picking up papers that acquire it on revision.
ARXIV_HTML_RECHECK_DAYS = 180


def arxiv_html_url(external_id: str, version: int | None = None) -> str:
    """arXiv's own LaTeXML HTML rendering. Bare id → the current version (see note above);
    ``version`` pins it, for a caller that has just resolved the current version itself."""
    if version:
        return f"https://arxiv.org/html/{external_id}v{version}"
    return f"https://arxiv.org/html/{external_id}"


def markup_is_acceptable(body: bytes, kind: str, status: int | None) -> str | None:
    """``None`` when the body is a usable markup document, else a short miss-reason string.

    The reason arXiv needs this at all: a missing rendering is a SOFT 404 — status 404 with a
    complete, real HTML page reading "No HTML for '<id>'. HTML is not available for the source."
    Minting that as the paper would be silently catastrophic, so it is rejected three ways (status,
    size floor, marker text) rather than trusting any single signal.

    Deliberately cheap and byte-level: fetch does not judge conversion quality. A partially broken
    LaTeXML page may still beat the PDF, and convert already emits a ``text_quality`` verdict that
    ``corpus`` filters on. This gate only answers "is this the document at all?"."""
    if status != 200:
        return f"http_{status}"
    if len(body) < ARXIV_HTML_MIN_BYTES:
        return "too_small"
    if _ARXIV_NO_HTML_RE.search(body[:8192]):
        return "soft_404"
    got = sniff_kind(body)
    if got != kind:
        return f"not_{kind}"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The Atom API's version answer (export.arxiv.org/api/query?id_list=…)
# ─────────────────────────────────────────────────────────────────────────────
# One request answers for up to a few hundred ids at once, which is how a sweep learns whether
# a paper has a new version WITHOUT downloading it: 50k ids at 100 per request is ~500 paced
# requests (~25 min at arXiv's 1 req / 3 s), against 50k paced PDF downloads (~42 h).
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ATOM_BATCH = 100


@dataclass(frozen=True)
class AtomVersion:
    """What the feed says about one id: its current version and the two dates."""

    arxiv_id: str
    version: int
    published: str   # v1 submission date, ISO date (YYYY-MM-DD) or ""
    updated: str     # latest version's date, ISO date or ""


def parse_atom_versions(xml_text: str) -> dict[str, AtomVersion] | None:
    """Parse an Atom ``id_list`` response → ``{arxiv_id: AtomVersion}``.

    ``None`` for a body that is not an Atom feed — a parse error, or a well-formed interstitial
    (a proxy's ``<html>403</html>``, a CDN error page) served in place of the feed. That case
    must not read as "these ids have no entries" (``collect.arxiv_sweep`` learned this the hard
    way: arXiv throttles by serving garbage). Entries whose ``<id>`` does not parse — arXiv's own
    "Error" entry for an unknown id — are skipped, not fatal: the caller sees the id missing."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != f"{{{ATOM_NS['atom']}}}feed":
        return None
    out: dict[str, AtomVersion] = {}
    for e in root.findall("atom:entry", ATOM_NS):
        idel = e.find("atom:id", ATOM_NS)
        id_text = (idel.text or "").strip() if idel is not None else ""
        aid = normalize_arxiv_id(id_text)
        if not aid:
            continue
        pub = e.find("atom:published", ATOM_NS)
        upd = e.find("atom:updated", ATOM_NS)
        out[aid] = AtomVersion(
            arxiv_id=aid,
            version=arxiv_version_of(id_text) or 1,
            published=((pub.text or "")[:10] if pub is not None else ""),
            updated=((upd.text or "")[:10] if upd is not None else ""),
        )
    return out


def atom_id_list_url(endpoint: str, ids: Sequence[str]) -> str:
    """The batched query URL. ``endpoint`` is the channel's ``location`` (channels.yaml)."""
    q = urllib.parse.urlencode({"id_list": ",".join(ids), "max_results": len(ids)})
    return f"{endpoint}?{q}"


def batched(seq: Sequence[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i:i + n])


__all__ = [
    "ARXIV_HTML_MIN_BYTES", "ARXIV_HTML_RECHECK_DAYS", "ATOM_BATCH", "ATOM_NS", "AtomVersion",
    "arxiv_html_url", "arxiv_pdf_url", "arxiv_version_of", "atom_id_list_url", "batched",
    "markup_is_acceptable", "normalize_arxiv_id", "parse_atom_versions",
]
