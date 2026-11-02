"""``stack.scholarly_records`` — the one key-typing module, plus the pure provider-record normalisers.

Identity keys (design §6, App. A.2 ``KEY = (kind! S, value! S)``): every collector, every lookup and
gather's ``union`` turn provider identifiers into typed keys here and nowhere else. This module only
types and normalises; which kinds merge documents is gather's job (``configs/channels.yaml``
``channel_groups``, mirrored by :data:`MERGING_KEY_KINDS` and pinned against the config in
``tests/test_scholarly_records.py``).

API
---
* :class:`Key` — the ``KEY`` dict ``{"kind", "value"}``; ``value`` is in the kind's normal form.
* :data:`KEY_KINDS` (stable order), :data:`MERGING_KEY_KINDS` (group -> kinds),
  :data:`NON_MERGING_KEY_KINDS` (D56: recorded, never merged).
* :func:`normalize_key` ``(kind, raw) -> Key | None`` — one value; ``None`` for junk; an unknown kind
  raises ``ValueError`` (a typo in a collector must not silently drop keys).
* :func:`keys_for` ``(pairs) -> list[Key]`` — many ``(kind, raw)`` pairs (or a ``{kind: raw}``
  mapping) of one record: normalised, bridged, de-duplicated, sorted by ``(KEY_KINDS order, value)``.
* :func:`keys_for_source` ``(channel_group, channel, external_id, canonical_url) -> list[Key]`` —
  the keys of one ``sources`` row from its identity columns.
* :func:`classify_external_id` ``(channel, external_id) -> ExternalId`` — v3's lookup routing
  (``kind`` ∈ arxiv/doi/pmid/other with ``arxiv_id``/``doi``/``pmid``/``zenodo_id``), unchanged in
  signature; its caller is ``workflows/fetch/metadata/main.py``.
* Per-kind normalisers ``normalize_<kind>`` (each ``str -> str``, ``''`` = not a valid id) and v3's
  ``_arxiv_id_of`` (kept for its callers; same as :func:`normalize_arxiv`).

Normal forms (one per kind)
---------------------------
=============== ==================================================================================
``doi``         lower case, resolver prefix stripped (``https://doi.org/``, ``dx.doi.org``,
                ``doi.org/``, ``doi:``; URL forms percent-decoded), must match ``10.NNNN…/suffix``.
                An arXiv DataCite DOI is rewritten to ``10.48550/arxiv.<arxiv normal form>``.
                Also from a DOI-in-path publisher URL (``stack.urls.doi_of_url``: ACM, Springer,
                Wiley, T&F, Science, PNAS and other ``/doi/`` sites, Frontiers, PLOS ``?id=``,
                ``nature.com/articles/<suffix>`` -> ``10.1038/<suffix>``, bioRxiv/medRxiv).
``arxiv``       bare id, no version, lower case: new style ``2409.15747``; old style
                ``archive/YYMMNNN`` (``cs/9901001``), the subject class dropped
                (``math.GT/0309136`` -> ``math/0309136``). From a bare id, ``arXiv:`` id, an arXiv
                URL (``stack.urls.ARXIV_URL_RE``: ``abs|pdf|html|format|ps`` on any ``arxiv.org``
                host, ar5iv, the Hugging Face / alphaXiv mirrors) or the DataCite DOI.
``openalex``    work id ``W<digits>`` (upper-case W), from a bare id or an ``openalex.org`` URL.
``s2``          Semantic Scholar ``paperId``, 40 lower-case hex (v3 keyed S2 sources on it); a
                ``semanticscholar.org`` paper URL ending in it is accepted. Corpus ids are not keys.
``pmid``        decimal digits without leading zeros, from ``pmid:N``, ``PMID: N``, a bare number or
                a ``pubmed.ncbi.nlm.nih.gov`` / ``ncbi.nlm.nih.gov/pubmed/`` URL.
``zenodo``      the record number (digits), from a bare number, a ``zenodo.org/records/N`` URL or a
                Zenodo DOI ``10.5281/zenodo.N``.
``fm_post``     ForumMagnum post ``_id``, case kept, from a bare id or a LessWrong / Alignment Forum
                / GreaterWrong / EA Forum post URL (``/posts/<id>``, ``/s/<seq>/p/<id>``).
``url``         ``stack.urls.url_key`` (https, host without ``www.``, no fragment or trailing slash,
                tracking dropped, identity parameters kept per host, domain canonical forms);
                only absolute http(s) URLs.
``x_post``      the post's decimal id, from a bare id or a status URL (``stack.urls.x_status_of_url``:
                ``x.com`` / ``twitter.com`` / ``mobile.`` / mirrors, ``/i/web/status/<id>``).
``registry_id`` the registry's id, whitespace-collapsed, case kept.
non-merging     ``conversation_id``, ``reply_to_id``, ``quote_of``, ``repost_of`` as ``x_post``;
                ``related``, ``relay`` as ``url``; ``flag`` (a manual source's flag, values
                :data:`FLAGS`: ``collect.manual``, round 4 (L)) lower case, else ''.
=============== ==================================================================================

The arXiv–DOI bridge: :func:`keys_for` gives an arXiv DataCite DOI **both** keys ``doi`` and
``arxiv``, and a bare arXiv id both as well, so gather merges an arXiv source with its OpenAlex/S2
record inside the paper group. A Zenodo DOI also gives ``zenodo`` (v3's ``zenodo_id``). Nothing else
is bridged.

The rest of the module is v3's pure provider-record normalisers (venue type, dates, OpenAlex/arXiv/S2
fragments, Zenodo versions).
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypedDict

from stack import urls


_OA_SOURCE_TYPE_MAP = {
    "journal": "journal", "conference": "conference", "repository": "preprint",
    "book series": "book", "ebook platform": "book",
}

_CROSSREF_TYPE_MAP = {
    "journal-article": "journal", "proceedings-article": "conference",
    "posted-content": "preprint", "report": "report",
    "book-chapter": "book", "book": "book", "book-part": "book",
}

_WORK_TYPE_MAP = {
    "preprint": "preprint", "posted-content": "preprint", "report": "report", "conference-paper": "conference",
    "book-chapter": "book", "book": "book", "post": "blog",
    "system_card": "website", "policy": "website",
}

def venue_type_from(
    *,
    is_arxiv: bool = False,
    rec_type: str = "",
    oa_source_type: str | None = None,
    crossref_type: str | None = None,
    s2_types: Any | None = None,
) -> str:
    """Classify a paper's venue KIND from the best available signal (ported priority)."""
    if is_arxiv:
        return "preprint"
    oa = (oa_source_type or "").lower()
    if oa in _OA_SOURCE_TYPE_MAP:
        return _OA_SOURCE_TYPE_MAP[oa]
    st = {str(t).lower() for t in (s2_types or [])}
    if st & {"conference", "conferenceproceeding"}:
        return "conference"
    if "journalarticle" in st:
        return "journal"
    cr = (crossref_type or "").lower()
    if cr in _CROSSREF_TYPE_MAP:
        return _CROSSREF_TYPE_MAP[cr]
    return _WORK_TYPE_MAP.get((rec_type or "").lower(), "")

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

_YEAR_RE = re.compile(r"(\d{4})")

def year_from(date: str) -> int | None:
    m = _YEAR_RE.search(date or "")
    return int(m.group(1)) if m else None

def month_from(date: str, source: str = "") -> int | None:
    """Month 1-12 from an ISO date, or None. An OpenAlex Jan-1 date is treated as month-unknown
    (OpenAlex fabricates YYYY-01-01 when it only has the year)."""
    m = _ISO_DATE.match(date or "")
    if not m:
        return None
    mo, day = int(m.group(2)), int(m.group(3))
    if source == "openalex" and mo == 1 and day == 1:
        return None
    return mo

def pub_date_from(date: str, year: Any = None) -> str | None:
    """A ``YYYY-MM-DD`` string (MetadataCommon.pub_date's prefix-anchored pattern): the provider's
    date, else Jan-1 of a bare year, else None."""
    if date:
        m = _ISO_DATE.match(date.strip())
        if m is not None:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    y = year_from(str(year) if year is not None else "")
    if y:
        return f"{y:04d}-01-01"
    return None

def reconstruct_inverted_index(idx: dict[str, list[int]] | None) -> str:
    """OpenAlex ``abstract_inverted_index`` -> plain text (ported)."""
    if not idx:
        return ""
    pairs = [(pos, word) for word, positions in idx.items() for pos in positions]
    pairs.sort()
    return " ".join(w for _, w in pairs)

def pages_str(first: Any, last: Any) -> str:
    """'392'/'407' -> '392–407'; single -> '392'; nothing -> '' (ported)."""
    first = str(first or "").strip()
    last = str(last or "").strip()
    if first and last and first != last:
        return f"{first}–{last}"
    return str(first or last)

# ─────────────────────────────────────────────────────────────────────────────
# Key typing (design §6, App. A.2 KEY)
# ─────────────────────────────────────────────────────────────────────────────
class Key(TypedDict):
    """App. A.2 ``KEY = (kind! S, value! S)``; ``value`` in the kind's normal form."""

    kind: str
    value: str


# Identity groups -> merging key kinds. Mirrors `channel_groups` in configs/channels.yaml;
# tests/test_scholarly_records.py asserts equality with the loaded config.
MERGING_KEY_KINDS: dict[str, tuple[str, ...]] = {
    "paper": ("doi", "arxiv", "openalex", "s2", "pmid", "zenodo"),
    "forum": ("fm_post",),
    "url": ("url",),
    "social": ("x_post",),
    "record": ("registry_id",),
}
# D56: recorded on a source, never merge identities. Mirrors tools/check_configs.py (pinned by test).
NON_MERGING_KEY_KINDS: tuple[str, ...] = (
    "related", "relay", "conversation_id", "reply_to_id", "quote_of", "repost_of", "flag",
    "v3_meta_id",  # D524: the v3 metadata row a `collect.v3_import` row came from (was metadata.record_ref)
)
#: The values of a ``flag`` key (a ``manual_sources`` override's ``flags``, ``collect.manual``):
#: ``collect_links`` = harvest the document's outbound links; ``accept`` = triage fetches it.
FLAGS: tuple[str, ...] = ("collect_links", "accept")
KEY_KINDS: tuple[str, ...] = (
    tuple(k for kinds in MERGING_KEY_KINDS.values() for k in kinds) + NON_MERGING_KEY_KINDS
)
_KIND_RANK = {k: i for i, k in enumerate(KEY_KINDS)}

# arXiv ids: new style `YYMM.NNNN(N)`, old style `archive(.SC)/YYMMNNN` (subject class optional);
# the grammar and the URL forms live in stack.urls.
_ARXIV_ID = urls.ARXIV_ID
_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\." + _ARXIV_ID + r"(?:v\d+)?$", re.IGNORECASE)
_ARXIV_URL_RE = urls.ARXIV_URL_RE
_ARXIV_BARE_RE = re.compile(r"^(?:arxiv:\s*)?" + _ARXIV_ID + r"(?:v\d+)?$", re.IGNORECASE)
_ARXIV_SUBJECT_CLASS_RE = re.compile(r"^([a-z\-]+)\.[a-z]{2}/")
_ZENODO_RE = re.compile(r"^10\.5281/zenodo\.(\d+)$", re.IGNORECASE)

_DOI_PREFIX_RE = re.compile(r"^(?:(?:https?://)?(?:dx\.|www\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_DOI_RE = re.compile(r"^10\.\d{4,9}(?:\.\d+)*/\S+$")
_OPENALEX_RE = re.compile(r"^(?:https?://(?:api\.)?openalex\.org/(?:works/)?)?(w\d+)/?$", re.IGNORECASE)
_S2_RE = re.compile(
    r"^(?:https?://(?:www\.|api\.)?semanticscholar\.org/(?:\S*/)?)?([0-9a-f]{40})/?$", re.IGNORECASE)
_PMID_RE = re.compile(
    r"^(?:pmid:?\s*|https?://pubmed\.ncbi\.nlm\.nih\.gov/)?(\d{1,9})/?$", re.IGNORECASE)
_ZENODO_ID_RE = re.compile(
    r"^(?:https?://(?:www\.)?zenodo\.org/(?:records?|deposit)/)?(\d+)/?$", re.IGNORECASE)
_FM_POST_BARE_RE = re.compile(r"^[A-Za-z0-9]+$")
_X_POST_RE = re.compile(
    r"^(?:https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/\s]+/status(?:es)?/)?(\d{1,25})"
    r"(?:[/?#]\S*)?$", re.IGNORECASE)


def _canon_arxiv(aid: str) -> str:
    """Lower case; an old-style id drops its subject class (``math.gt/0309136`` -> ``math/0309136``)."""
    return _ARXIV_SUBJECT_CLASS_RE.sub(r"\1/", aid.lower())


def normalize_doi(raw: str) -> str:
    """A DOI in normal form (module docstring), else ''."""
    s = (raw or "").strip()
    if s[:4].lower() == "http" and not _DOI_PREFIX_RE.match(s):
        s = urls.doi_of_url(s)  # a DOI-in-path publisher URL ('' for any other URL)
    m = _DOI_PREFIX_RE.match(s)
    if m is not None:
        s = s[m.end():]
        if not m.group(0).lower().startswith("doi:"):
            s = urllib.parse.unquote(s)
    s = s.strip().lower()
    if not _DOI_RE.match(s):
        return ""
    am = _ARXIV_DOI_RE.match(s)
    return f"10.48550/arxiv.{_canon_arxiv(am.group(1))}" if am else s


def _arxiv_id_of(text: str) -> str:
    """A bare arXiv id from an arXiv DOI, an arxiv.org abs/pdf/html URL, or a bare id, else ''.

    ANCHORED — never matches a ``\\d{4}\\.\\d{4,5}`` substring inside an unrelated DOI (both id
    schemes; ported from corpus.arxiv_id, which keyed on the literal ``arxiv.`` prefix)."""
    s = (text or "").strip()
    doi = normalize_doi(s)
    if doi:
        m = _ARXIV_DOI_RE.match(doi)
        return _canon_arxiv(m.group(1)) if m else ""
    m = _ARXIV_URL_RE.search(s) or _ARXIV_BARE_RE.match(s)
    return _canon_arxiv(m.group(1)) if m else ""


def normalize_arxiv(raw: str) -> str:
    """An arXiv id in normal form (module docstring), else ''."""
    return _arxiv_id_of(raw)


def normalize_openalex_id(raw: str) -> str:
    """An OpenAlex work id ``W…``, else ''."""
    m = _OPENALEX_RE.match((raw or "").strip())
    return m.group(1).upper() if m else ""


def normalize_s2_id(raw: str) -> str:
    """A Semantic Scholar paperId (40 lower-case hex), else ''."""
    m = _S2_RE.match((raw or "").strip())
    return m.group(1).lower() if m else ""


def normalize_pmid(raw: str) -> str:
    """A PubMed id (digits, no leading zeros), else ''."""
    s = (raw or "").strip()
    if s[:4].lower() == "http":
        s = urls.pmid_of_url(s)
    m = _PMID_RE.match(s)
    return str(int(m.group(1))) if m and int(m.group(1)) else ""


def normalize_zenodo_id(raw: str) -> str:
    """A Zenodo record number, from the number, a record URL or a Zenodo DOI, else ''."""
    s = (raw or "").strip()
    zm = _ZENODO_RE.match(normalize_doi(s))
    m = zm or _ZENODO_ID_RE.match(s)
    return str(int(m.group(1))) if m and int(m.group(1)) else ""


def normalize_url(raw: str) -> str:
    """An absolute http(s) URL in the ``url`` key form (:func:`stack.urls.url_key`), else ''."""
    s = (raw or "").strip()
    if not s or any(c.isspace() for c in s):
        return ""
    return urls.url_key(s)


def normalize_fm_post(raw: str) -> str:
    """A ForumMagnum post ``_id`` (case kept), from the id or a forum post URL, else ''."""
    s = (raw or "").strip()
    if s[:4].lower() == "http":
        return urls.fm_post_of_url(s)
    return s if _FM_POST_BARE_RE.match(s) else ""


def normalize_x_post(raw: str) -> str:
    """An X post id (digits), from the id or a status URL, else ''."""
    s = (raw or "").strip()
    if s[:4].lower() == "http":
        return urls.x_status_of_url(s)
    m = _X_POST_RE.match(s)
    return m.group(1) if m else ""


def normalize_registry_id(raw: str) -> str:
    """A registry id, whitespace-collapsed, case kept, else ''."""
    return " ".join((raw or "").split())


def normalize_flag(raw: str) -> str:
    """A manual-source flag (:data:`FLAGS`), trimmed and lower-cased, else ''."""
    s = (raw or "").strip().lower()
    return s if s in FLAGS else ""


_NORMALISERS: dict[str, Callable[[str], str]] = {
    "doi": normalize_doi,
    "arxiv": normalize_arxiv,
    "openalex": normalize_openalex_id,
    "s2": normalize_s2_id,
    "pmid": normalize_pmid,
    "zenodo": normalize_zenodo_id,
    "fm_post": normalize_fm_post,
    "url": normalize_url,
    "x_post": normalize_x_post,
    "registry_id": normalize_registry_id,
    "related": normalize_url,
    "relay": normalize_url,
    "conversation_id": normalize_x_post,
    "reply_to_id": normalize_x_post,
    "quote_of": normalize_x_post,
    "repost_of": normalize_x_post,
    "flag": normalize_flag,
    "v3_meta_id": normalize_registry_id,
}
if set(_NORMALISERS) != set(KEY_KINDS):  # pragma: no cover - import-time consistency guard
    raise RuntimeError(f"normaliser table {sorted(_NORMALISERS)} != KEY_KINDS {sorted(KEY_KINDS)}")


def normalize_key(kind: str, raw: Any) -> Key | None:
    """One typed key in normal form, or ``None`` when ``raw`` is not a valid id of ``kind``.

    Raises ``ValueError`` for a kind outside :data:`KEY_KINDS`."""
    norm = _NORMALISERS.get(kind)
    if norm is None:
        raise ValueError(f"unknown key kind {kind!r} (known: {', '.join(KEY_KINDS)})")
    value = norm(str(raw)) if raw is not None else ""
    return Key(kind=kind, value=value) if value else None


def _bridged(key: Key) -> list[Key]:
    """The key plus what the arXiv–DOI bridge (and v3's Zenodo DOI rule) add to it."""
    out = [key]
    if key["kind"] == "arxiv":
        out.append(Key(kind="doi", value=f"10.48550/arxiv.{key['value']}"))
    elif key["kind"] == "doi":
        am = _ARXIV_DOI_RE.match(key["value"])
        if am:
            out.append(Key(kind="arxiv", value=_canon_arxiv(am.group(1))))
        zm = _ZENODO_RE.match(key["value"])
        if zm:
            out.append(Key(kind="zenodo", value=str(int(zm.group(1)))))
    return out


def keys_for(ids: Mapping[str, Any] | Iterable[tuple[str, Any]]) -> list[Key]:
    """The typed keys of one record from its ``(kind, raw)`` pairs (or a ``{kind: raw}`` mapping).

    Junk and empty values give no key; the bridge adds ``doi``/``arxiv``/``zenodo`` (module
    docstring); duplicates collapse; the order is ``(KEY_KINDS position, value)``."""
    pairs = ids.items() if isinstance(ids, Mapping) else ids
    seen: set[tuple[str, str]] = set()
    out: list[Key] = []
    for kind, raw in pairs:
        key = normalize_key(kind, raw)
        if key is None:
            continue
        for k in _bridged(key):
            if (k["kind"], k["value"]) not in seen:
                seen.add((k["kind"], k["value"]))
                out.append(k)
    out.sort(key=lambda k: (_KIND_RANK[k["kind"]], k["value"]))
    return out


def keys_for_source(
    channel_group: str, channel: str, external_id: str | None, canonical_url: str | None = None,
) -> list[Key]:
    """The keys of one ``sources`` row from its identity columns, by the channel's group.

    ``paper``: :func:`classify_external_id` (arXiv id / DOI / PMID), else an OpenAlex work id or an
    S2 paperId; ``forum``: ``fm_post``; ``social``: ``x_post``; ``record``: ``registry_id``;
    ``url``: the canonical URL (else the external id when it is a URL). Raises ``ValueError`` for
    an unknown group."""
    raw = (external_id or "").strip()
    if channel_group == "paper":
        ext = classify_external_id(channel, raw)
        if ext.kind == "arxiv":
            return keys_for([("arxiv", ext.arxiv_id)])
        if ext.kind == "doi":
            return keys_for([("doi", ext.doi)])
        if ext.kind == "pmid":
            return keys_for([("pmid", ext.pmid)])
        return keys_for([("openalex", raw), ("s2", raw)])
    if channel_group == "url":
        return keys_for([("url", canonical_url or raw)])
    kind = {"forum": "fm_post", "social": "x_post", "record": "registry_id"}.get(channel_group)
    if kind is None:
        raise ValueError(f"unknown channel group {channel_group!r} (known: {', '.join(MERGING_KEY_KINDS)})")
    return keys_for([(kind, raw)])


class ExternalId:
    """The classified lookup keys for one source's ``external_id`` (10-design §3 identity)."""

    def __init__(
        self,
        channel: str,
        raw: str,
        kind: str,  # 'arxiv' | 'doi' | 'pmid' | 'other'
        *,
        arxiv_id: str = "",
        doi: str = "",       # canonical DOI ('/' form), lowercased
        pmid: str = "",
        zenodo_id: str = "",
    ) -> None:
        self.channel = channel
        self.raw = raw
        self.kind = kind
        self.arxiv_id = arxiv_id
        self.doi = doi
        self.pmid = pmid
        self.zenodo_id = zenodo_id

    @property
    def is_arxiv(self) -> bool:
        return self.kind == "arxiv"

    @property
    def is_zenodo(self) -> bool:
        return bool(self.zenodo_id)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"ExternalId(channel={self.channel!r}, kind={self.kind!r}, "
                f"arxiv_id={self.arxiv_id!r}, doi={self.doi!r}, pmid={self.pmid!r})")


def classify_external_id(channel: str, external_id: str) -> ExternalId:
    """Map a source's ``(channel, external_id)`` onto its scholarly lookup keys.

    ``kind='other'`` is a non-scholarly id (a blog/forum/card slug, an OpenAlex ``W…`` or an S2
    paperId) — not looked up here. Values are in this module's normal forms."""
    raw = (external_id or "").strip()

    # PMID synthetic id (`pmid:N`, the S2/EuropePMC fallback external_id).
    if raw.lower().startswith("pmid:"):
        pmid = normalize_pmid(raw)
        if pmid:
            return ExternalId(channel, raw, "pmid", pmid=pmid)

    # arXiv: bare id (channel 'arxiv' carries the bare id), an arXiv URL, or an arXiv DOI.
    aid = _arxiv_id_of(raw)
    if aid:
        return ExternalId(channel, raw, "arxiv", arxiv_id=aid, doi=f"10.48550/arxiv.{aid}")

    # A generic DOI (incl. Zenodo).
    doi = normalize_doi(raw)
    if doi:
        zm = _ZENODO_RE.match(doi)
        return ExternalId(channel, raw, "doi", doi=doi, zenodo_id=str(int(zm.group(1))) if zm else "")

    return ExternalId(channel, raw, "other")


def normalize_openalex(work: dict[str, Any], *, is_arxiv: bool) -> dict[str, Any]:
    """OpenAlex work -> fragment (title/authors/pub_date/venue/venue_type/citations/oa/biblio,
    plus an ``abstract`` candidate from the inverted index)."""
    loc = work.get("primary_location") or {}
    src = (loc.get("source") or {}) if loc else {}
    bib = work.get("biblio") or {}
    date = work.get("publication_date") or ""
    ships = work.get("authorships") or []
    authors = [((a.get("author") or {}).get("display_name") or "").strip() for a in ships]
    authors = [a for a in authors if a]
    frag: dict[str, Any] = {
        "title": (work.get("title") or "").strip() or None,
        "authors": authors or None,
        "pub_date": pub_date_from(date, work.get("publication_year")),
        "month": month_from(date, "openalex"),
        "venue": ("arXiv" if is_arxiv else (src.get("display_name") or "").strip()) or None,
        "work_type": (work.get("type") or "").strip() or None,
        "venue_type": venue_type_from(
            is_arxiv=is_arxiv, rec_type=work.get("type") or "", oa_source_type=src.get("type")),
        "citations": work.get("cited_by_count"),
        "oa": (work.get("open_access") or {}).get("is_oa"),
        "url": loc.get("landing_page_url") or None,
        "abstract": reconstruct_inverted_index(work.get("abstract_inverted_index")) or "",
    }
    if not is_arxiv:
        frag["volume"] = (bib.get("volume") or "").strip() or None
        frag["issue"] = (bib.get("issue") or "").strip() or None
        frag["pages"] = pages_str(bib.get("first_page"), bib.get("last_page")) or None
    return {k: v for k, v in frag.items() if v is not None}

def normalize_s2(paper: dict[str, Any]) -> dict[str, Any]:
    """Semantic Scholar paper (batch or single) -> fragment. S2 carries authoritative author
    lists (no affiliations — those stay LLM-extracted), publication venue/types, and an abstract."""
    date = paper.get("publicationDate") or ""
    authors = [(a.get("name") or "").strip() for a in (paper.get("authors") or [])]
    authors = [a for a in authors if a]
    pub_venue = (paper.get("publicationVenue") or {}) or {}
    frag: dict[str, Any] = {
        "title": (paper.get("title") or "").strip() or None,
        "authors": authors or None,
        "pub_date": pub_date_from(date, paper.get("year")),
        "abstract": (paper.get("abstract") or "").strip(),
        "s2_types": list(paper.get("publicationTypes") or []) or None,
        "s2_venue": (pub_venue.get("name") or paper.get("venue") or "").strip() or None,
        "s2_venue_type": pub_venue.get("type"),
        "published_doi": ((paper.get("externalIds") or {}).get("DOI") or "").strip() or None,
    }
    return {k: v for k, v in frag.items() if v is not None}
