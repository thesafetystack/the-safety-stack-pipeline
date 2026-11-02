"""``annotate.affiliation`` — the PURE extraction core (no DB, no ``Services``, no network).

Everything here is a function of its arguments: the document text, the authoritative author
list, a ``canon`` matcher, and a caller-supplied ``llm_call``. ``main.py`` supplies the real
ones; the eval harness (``stack/evals``), v3's ``tools/eval_affiliations.py`` and ``tests/test_affiliation.py`` supply stubs or a bare
LLM client. That split is deliberate — accuracy/precision/recall work needs to run this stage
on real documents hundreds of times without standing up Postgres or a sweep.

The pipeline this module implements:

0. :func:`parse_xml_byline` — the deterministic tier: when the snapshot's raw bytes are
   publisher XML (JATS / Elsevier CE), read the author→affiliation linkage straight from the
   markup and skip the LLM entirely (D-RAW1). Fires only when ``main.py`` passes ``raw_xml``.
1. :func:`header_window` — the front-matter slice sent to the model (v1's tuned 8k head +
   2k tail; affiliations sometimes print at the END, which is why the tail exists).
2. :func:`parse_authors` — defensive read of the model's JSON into :class:`RawAuthor`.
3. :func:`align_to_roster` — when metadata supplied an authoritative list, map the model's
   records back onto it so the model cannot add, drop, or rename an author.
4. :func:`resolve` — fill ``org_slugs`` from each verbatim ``surface`` via ``canon.orgs_in``,
   then the OpenAlex and email-domain fallback tiers for authors the text did not resolve.

Tier order REVERSES v1. v1 ran OpenAlex first and called the LLM only for what was left; its
shipped ``paper_affiliations`` shows the result — 2,199 of 27,923 author affiliations (7.9%)
came from OpenAlex with first claim on every author, against 90.6% from the LLM. Text is
therefore primary here and OpenAlex is a fallback: for an author the text left unresolved, the
OpenAlex institutions (``metadata.authors[].affiliation_raw``) become the item's affiliations with
``resolution = "openalex"``, not anchored in the text (:func:`anchor_payload`).

The model never names an organisation — it copies strings, and ``canon_org`` resolves slugs
afterwards (see ``extract.md``). That is what lets a registry edit cost a re-resolve instead of
a re-extraction of the corpus.
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Any, Callable, Iterator, Mapping, Sequence

from stack.canon import Canon, email_domain
from stack.llm import strict_schema

# Front-matter window (v1 ``extract_affiliations.header_window``, tuned on the real corpus).
HEAD_CHARS = 8_000
TAIL_CHARS = 2_000

# v1's cap on names sent to / accepted from the model; guards a 900-author physics byline.
MAX_AUTHORS = 60

# Fraction of authors left with no affiliation before the page-image fallback is worth paying
# for. A chosen value, not a spec mandate: below this, a partial byline is normal (senior
# authors often carry no marker); above it, the converted text is probably mangled.
VISION_TRIGGER_UNRESOLVED = 0.5

_DASHES = dict.fromkeys(
    [0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D], "-"
)
_SYNTHETIC_PREFIX_RE = re.compile(r"\A#\s.*?\n(\*\*DOI:\*\*.*?\n)?")

# Generational/honorific suffixes that follow a comma but are NOT a given name, so the
# "Family, Given" un-inversion must leave them alone.
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"})


def norm_author_name(raw: str) -> str:
    """The H-S12 name key: fold diacritics, unify dashes, un-invert "Last, First", lowercase,
    collapse whitespace.

    v3's ``stack.types.normalize_item_key`` (NFC + strip + lower) is deliberately NOT reused: it
    preserves combining marks, so "José" and "Jose" would key as two different authors. Real
    corpus names also arrive with typographic dashes — v1's own data holds "Ying–Dar Lin" (en
    dash) beside "Ying-Dar Lin" — so dash unification is doing real work, not cosmetics.

    **Inverted names are the big one.** Bibliographic metadata (Crossref, OpenAlex) emits
    ``Family, Given``, while a paper's byline prints ``Given Family`` — so the roster says
    "Hayes, Jamie" where the document says "Jamie Hayes". Measured on 200 papers, that single
    mismatch caused **85% of the addressable recall gap**: the model read the affiliation
    correctly and alignment then threw it away, because a token reordering scores below the
    fuzzy threshold. Un-inverting here fixes it once for every tier.

    Local to this workflow by the layout rule (30-stack-module §0: duplicate first, graduate
    deliberately). It graduates to ``stack`` when a second workflow needs the same key.
    """
    folded = unicodedata.normalize("NFD", raw or "").translate(_DASHES)
    stripped = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    key = " ".join(stripped.lower().split())
    head, sep, tail = key.partition(",")
    if sep and head.strip() and tail.strip() and "," not in tail:
        # "Family, Given" -> "given family". A generational/honorific suffix is NOT a given name,
        # so those keep their order ("king, jr." must not become "jr. king").
        tail_clean = tail.strip().rstrip(".")
        if tail_clean not in _NAME_SUFFIXES:
            return f"{tail.strip()} {head.strip()}".strip()
    return key


def header_window(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    """The front-matter slice shown to the model: head + tail, minus converter boilerplate.

    Trimming is safe for grounding: the anchorer searches the FULL stored text, so a quote
    found in this window still anchors at its true offset. The window only bounds prompt cost.
    """
    body = _SYNTHETIC_PREFIX_RE.sub("", text or "", count=1)
    if len(body) <= head + tail:
        return body
    return body[:head] + "\n...\n" + (body[-tail:] if tail else "")


def build_authors_block(names: Sequence[str]) -> str:
    """The ``{{input:authors}}`` fill: one name per line, or '' to trigger discovery mode."""
    return "\n".join(n.strip() for n in names[:MAX_AUTHORS] if n and n.strip())


class RawAffiliation:
    """One affiliation as the model reported it, before org resolution.

    A plain class, not a ``@dataclass``: written when workflow modules were loaded via
    ``spec_from_file_location`` with no ``sys.modules`` entry, which ``@dataclass`` + future
    annotations cannot tolerate (the stage loader registers them since D435).
    """

    __slots__ = ("surface", "marker")

    def __init__(self, surface: str, marker: str | None = None) -> None:
        self.surface = surface
        self.marker = marker

    def __repr__(self) -> str:  # test failure output
        return f"RawAffiliation({self.surface!r}, {self.marker!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RawAffiliation):
            return NotImplemented
        return (self.surface, self.marker) == (other.surface, other.marker)


class RawAuthor:
    """One author as the model reported them, before roster alignment and org resolution.

    Plain class for the same reason as :class:`RawAffiliation`.
    """

    __slots__ = ("author_surface", "affiliations", "email", "orcid")

    def __init__(
        self,
        author_surface: str,
        affiliations: list[RawAffiliation] | None = None,
        email: str = "",
        orcid: str = "",
    ) -> None:
        self.author_surface = author_surface
        self.affiliations = affiliations if affiliations is not None else []
        self.email = email
        self.orcid = orcid  # only the XML tier fills this; the model is never asked for it

    def __repr__(self) -> str:  # test failure output
        return f"RawAuthor({self.author_surface!r}, {self.affiliations!r}, {self.email!r})"


def parse_authors(parsed: Mapping[str, Any] | None) -> list[RawAuthor]:
    """Read the model's JSON into :class:`RawAuthor`, tolerating shape drift.

    Deliberately permissive about SHAPE and strict about CONTENT: a missing key or a stray type
    yields fewer records, never an exception, because one malformed reply must cost one unit's
    receipt rather than the run. Blank surfaces are dropped here so they cannot reach the
    anchorer as empty quotes. A ``null`` ``email`` or ``marker`` (the strict schema's "absent")
    reads as absent.
    """
    rows = (parsed or {}).get("authors")
    if not isinstance(rows, list):
        return []
    out: list[RawAuthor] = []
    for row in rows[:MAX_AUTHORS]:
        if not isinstance(row, Mapping):
            continue
        surface = str(row.get("author_surface") or row.get("name") or "").strip()
        if not surface:
            continue
        affs: list[RawAffiliation] = []
        raw_affs = row.get("affiliations")
        if isinstance(raw_affs, list):
            for a in raw_affs:
                if isinstance(a, Mapping):
                    text = str(a.get("surface") or "").strip()
                    marker = str(a.get("marker") or "").strip() or None
                elif isinstance(a, str):  # a model that flattened to plain strings
                    text, marker = a.strip(), None
                else:
                    continue
                if text and text.lower() != "unknown":  # v1's sentinel for "not found"
                    affs.append(RawAffiliation(surface=text, marker=marker))
        email = str(row.get("email") or "").strip()
        out.append(RawAuthor(author_surface=surface, affiliations=affs, email=email))
    return out


# ── the XML byline tier (tier-0, D-RAW1) ──────────────────────────────────────────────────────
#
# Deterministic parse of the raw markup's OWN author→affiliation linkage. The point: docling's
# JATS backend computes exactly this mapping and then flattens it to a comma-joined author line
# plus a deduplicated affiliation line — the `ambiguous_assignment` failure mode, manufactured
# at convert time. The raw XML still holds the linkage, so when the snapshot's media_type is
# application/xml this tier reads it directly and the LLM is never called.
#
# Two vocabularies cover what the fetch rungs actually deliver as application/xml:
#   * JATS (Europe PMC fullTextXML):  <contrib-group><contrib contrib-type="author">
#       with <xref ref-type="aff" rid=...> resolved against <aff id=...>.
#   * Elsevier CE (Crossref TDM):     <ce:author-group><ce:author> with
#       <ce:cross-ref refid=...> resolved against <ce:affiliation id=...>/<ce:textfn>.
#
# Namespace handling is by LOCALNAME throughout (Elsevier is heavily namespaced, JATS is not).
# Anything unparseable or byline-less returns None and the caller falls through to the LLM.

# Schema caps (author_affiliation.schema.json) enforced at the parser edge so a pathological
# document degrades a field instead of failing the unit's payload validation.
_XML_SURFACE_MAX = 300
_XML_NAME_MAX = 200
_XML_MARKER_MAX = 8
_XML_ORCID_MAX = 40
_XML_EMAIL_MAX = 200


def _local(el: ET.Element) -> str:
    """The tag's localname — '{http://ns}surname' and 'ce:surname' both read as 'surname'."""
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _children(el: ET.Element, name: str) -> Iterator[ET.Element]:
    return (c for c in el if _local(c) == name)


def _find(el: ET.Element, name: str) -> ET.Element | None:
    return next(_children(el, name), None)


def _descendants(el: ET.Element, name: str) -> Iterator[ET.Element]:
    return (d for d in el.iter() if _local(d) == name)


def _text_of(el: ET.Element | None, *, skip: frozenset[str] = frozenset()) -> str:
    """Whitespace-collapsed text content, excluding subtrees whose localname is in ``skip``
    (a JATS ``<aff>`` starts with its ``<label>``, which is the marker, not the institution)."""
    if el is None:
        return ""
    parts: list[str] = [el.text or ""]
    for child in el:
        if _local(child) not in skip:
            parts.append(_text_of(child))
        parts.append(child.tail or "")
    return " ".join(" ".join(parts).split())


def _person_name(el: ET.Element, given: str, family: str) -> str:
    """'Given Family' from the two child elements, tolerating either being absent."""
    g, f = _text_of(_find(el, given)), _text_of(_find(el, family))
    return f"{g} {f}".strip()[:_XML_NAME_MAX]


def _jats_authors(root: ET.Element) -> list[RawAuthor]:
    # The legend: every <aff id=...> in the document (JATS puts them inside OR after the
    # contrib-group, and article-meta-level affs serve every group).
    affs: dict[str, RawAffiliation] = {}
    for aff in _descendants(root, "aff"):
        aff_id = aff.get("id") or ""
        surface = _text_of(aff, skip=frozenset({"label"}))[:_XML_SURFACE_MAX]
        if not aff_id or not surface:
            continue
        label = _text_of(_find(aff, "label"))[:_XML_MARKER_MAX] or None
        affs[aff_id] = RawAffiliation(surface=surface, marker=label)

    out: list[RawAuthor] = []
    for group in _descendants(root, "contrib-group"):
        for contrib in _children(group, "contrib"):
            if contrib.get("contrib-type") not in (None, "author"):
                continue  # editors, translators
            if _find(contrib, "collab") is not None:
                continue  # a consortium is not a person; the roster never carries it
            name_el = _find(contrib, "name")
            name = (
                _person_name(name_el, "given-names", "surname")
                if name_el is not None
                else _text_of(_find(contrib, "string-name"))[:_XML_NAME_MAX]
            )
            if not name:
                continue
            linked: list[RawAffiliation] = []
            for xref in _descendants(contrib, "xref"):
                if xref.get("ref-type") != "aff":
                    continue
                for rid in (xref.get("rid") or "").split():
                    hit = affs.get(rid)
                    if hit is not None and hit not in linked:
                        marker = _text_of(xref)[:_XML_MARKER_MAX] or hit.marker
                        linked.append(RawAffiliation(surface=hit.surface, marker=marker))
            for inline in _children(contrib, "aff"):  # affs printed inside the contrib itself
                surface = _text_of(inline, skip=frozenset({"label"}))[:_XML_SURFACE_MAX]
                if surface:
                    linked.append(RawAffiliation(surface=surface))
            orcid = ""
            for cid in _children(contrib, "contrib-id"):
                if cid.get("contrib-id-type") == "orcid":
                    orcid = _text_of(cid).rsplit("/", 1)[-1][:_XML_ORCID_MAX]
            out.append(
                RawAuthor(
                    author_surface=name,
                    affiliations=linked,
                    email=_text_of(_find(contrib, "email"))[:_XML_EMAIL_MAX],
                    orcid=orcid,
                )
            )
    # The shared-affiliation layout: markers only exist when there is something to distinguish.
    # One <aff> in the whole document and zero links ⇒ it belongs to every author (the same
    # rule the prompt teaches the model as byline shape 1).
    if out and len(affs) == 1 and not any(a.affiliations for a in out):
        only = next(iter(affs.values()))
        for author in out:
            author.affiliations = [RawAffiliation(surface=only.surface, marker=only.marker)]
    return out[:MAX_AUTHORS]


def _elsevier_authors(root: ET.Element) -> list[RawAuthor]:
    out: list[RawAuthor] = []
    for group in _descendants(root, "author-group"):
        # <ce:affiliation> siblings are this group's legend; <ce:textfn> is the display string.
        affs: dict[str, RawAffiliation] = {}
        for aff in _children(group, "affiliation"):
            aff_id = aff.get("id") or ""
            surface = _text_of(_find(aff, "textfn"))[:_XML_SURFACE_MAX]
            if not aff_id or not surface:
                continue
            label = _text_of(_find(aff, "label"))[:_XML_MARKER_MAX] or None
            affs[aff_id] = RawAffiliation(surface=surface, marker=label)
        for author in _children(group, "author"):
            name = _person_name(author, "given-name", "surname")
            if not name:
                continue
            linked: list[RawAffiliation] = []
            for xref in _children(author, "cross-ref"):
                hit = affs.get(xref.get("refid") or "")
                if hit is not None and hit not in linked:
                    marker = _text_of(_find(xref, "sup"))[:_XML_MARKER_MAX] or hit.marker
                    linked.append(RawAffiliation(surface=hit.surface, marker=marker))
            if not linked and len(affs) == 1:
                # CE convention: a group with one unreferenced affiliation applies it to the
                # whole group — same shared-affiliation rule as the JATS branch.
                linked = [next(iter(affs.values()))]
            out.append(
                RawAuthor(
                    author_surface=name,
                    affiliations=linked,
                    email=_text_of(_find(author, "e-address"))[:_XML_EMAIL_MAX],
                    orcid=(author.get("orcid") or "")[:_XML_ORCID_MAX],
                )
            )
    return out[:MAX_AUTHORS]


def parse_xml_byline(raw: bytes) -> list[RawAuthor] | None:
    """Authors + linked affiliations from raw publisher XML, or ``None`` to fall through.

    ``None`` and ``[]`` are deliberately the same answer at the call site (use the LLM):
    a document that IS parseable XML but carries no recognisable byline gives the text tier
    its chance rather than asserting "no authors" from silence.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None  # not well-formed (or DOCTYPE-entity-dependent) — the LLM path handles it
    authors = _jats_authors(root)
    if not authors:
        authors = _elsevier_authors(root)
    return authors or None


def _name_variants(name: str) -> tuple[str, str, str]:
    """Three progressively looser keys for one name: exact, first+last token, last+first initial.

    Metadata and bylines disagree constantly in ways that carry no information — the roster says
    "Martin J. Chadwick" where the paper prints "Martin Chadwick", or the roster expands an
    initial the byline abbreviates. Dropping the middle tokens is what makes those the same
    person.
    """
    key = norm_author_name(name)
    parts = key.split()
    if len(parts) < 2:
        return key, key, key
    first, last = parts[0], parts[-1]
    return key, f"{first} {last}", f"{first[0]} {last}"


def align_to_roster(
    raw: Sequence[RawAuthor], roster: Sequence[str]
) -> tuple[list[RawAuthor], dict[str, Any]]:
    """Map the model's records onto the AUTHORITATIVE author list (when metadata supplied one).

    The roster is the truth about WHO the authors are; the model is only trusted for what the
    document prints about them. So every roster name yields exactly one record — in roster order,
    under the roster's spelling — and a name the model invented is dropped rather than stored.
    This is v1's completeness check without v1's all-or-nothing retry: a partial reply keeps the
    authors it did resolve, and the discrepancy is reported for ``flags``.

    Matching runs in four tiers, loosest last: exact key → first+last token → first-initial+last →
    close string similarity (≥0.90). Exact-only matching silently produced EMPTY records — measured
    on 200 papers, "Martin J. Chadwick" vs "Martin Chadwick" and "Lucy Campbell-Gillingham" vs the
    byline's misspelled "Campbell-Gillingam" both failed, and the author looked unaffiliated rather
    than unmatched.

    Every tier requires a UNIQUE surviving candidate: with "J. Smith" and "Jane Smith" both on a
    paper, an ambiguous loose key claims neither. A wrong merge would attach one author's
    employer to another — exactly the error v1 makes — so ambiguity must lose.

    Returns (records, anomalies). An empty roster means discovery mode: pass raw through.
    """
    if not roster:
        return list(raw), {}

    unclaimed = list(raw)
    names = list(roster[:MAX_AUTHORS])
    hits: dict[int, RawAuthor] = {}
    fuzzy = 0

    def _matches(name: str, cand: RawAuthor, tier: int) -> bool:
        if tier < 3:
            return _name_variants(name)[tier] == _name_variants(cand.author_surface)[tier]
        return (
            SequenceMatcher(
                None, norm_author_name(name), norm_author_name(cand.author_surface)
            ).ratio()
            >= 0.90
        )

    for tier in range(4):
        # MUTUAL uniqueness: a pair is accepted only when this roster name matches exactly one
        # candidate AND that candidate matches exactly one roster name. Checking only the
        # candidate side is not enough — "J. Smith" is a unique candidate for "Jane Smith" while
        # also matching "John Smith", and claiming it would attach one author's employer to
        # another (v1's characteristic error).
        pending = [i for i in range(len(names)) if i not in hits]
        cand_for = {i: [c for c in unclaimed if _matches(names[i], c, tier)] for i in pending}
        for i in pending:
            cands = cand_for[i]
            if len(cands) != 1:
                continue
            cand = cands[0]
            if sum(1 for j in pending if cand in cand_for[j]) != 1:
                continue  # this candidate is claimed by more than one roster name — ambiguous
            hits[i] = cand
            unclaimed.remove(cand)
            fuzzy += tier > 0

    out = [
        RawAuthor(
            author_surface=name,
            affiliations=hits[i].affiliations if i in hits else [],
            email=hits[i].email if i in hits else "",
            orcid=hits[i].orcid if i in hits else "",
        )
        for i, name in enumerate(names)
    ]
    anomalies: dict[str, Any] = {}
    if len(hits) != len(raw) or len(hits) != len(out):
        anomalies["roster_mismatch"] = {
            "roster": len(out),
            "returned": len(raw),
            "matched": len(hits),
        }
    if fuzzy:
        anomalies["fuzzy_name_matches"] = fuzzy
    return out, anomalies


def resolve(
    raw: Sequence[RawAuthor],
    canon: Canon,
    *,
    openalex: Mapping[str, Sequence[str]] | None = None,
    tier: str = "text",
) -> list[dict[str, Any]]:
    """Turn :class:`RawAuthor` records into ``author_affiliation`` payload dicts.

    Org slugs come from ``canon.orgs_in`` over each verbatim surface — plural, because
    "MIT ; Anthropic" is one printed affiliation naming two orgs and a scalar answer would
    silently drop half of it.

    ``tier`` names the channel the primary records came from — ``"text"`` for the LLM read of
    the converted markdown, ``"xml"`` for :func:`parse_xml_byline` — and becomes those records'
    ``resolution``. Fallback tiers fire ONLY for an author the primary tier left unresolved, in
    order: OpenAlex ``authors_detail`` institutions (keyed by normalized name), then the email
    domain. Each records which tier answered in ``resolution``, so precision can be measured
    per tier instead of over an undifferentiated blob.
    """
    oa = {norm_author_name(k): v for k, v in (openalex or {}).items()}
    out: list[dict[str, Any]] = []
    for index, author in enumerate(raw):
        key = norm_author_name(author.author_surface)
        payload: dict[str, Any] = {
            "author": key,
            "author_surface": author.author_surface,
            "affiliations": [],
            "byline_index": index,
        }
        if author.email:
            payload["email"] = author.email
        if author.orcid:
            payload["orcid"] = author.orcid

        if author.affiliations:
            payload["affiliations"] = [
                {
                    "surface": a.surface,
                    **({"marker": a.marker} if a.marker else {}),
                    "org_slugs": [r.slug for r in canon.orgs_in(a.surface) if r.slug],
                }
                for a in author.affiliations
            ]
            payload["resolution"] = tier
        elif oa.get(key):
            payload["affiliations"] = [
                {"surface": inst, "org_slugs": [r.slug for r in canon.orgs_in(inst) if r.slug]}
                for inst in oa[key]
                if inst
            ]
            payload["resolution"] = "openalex"
        elif author.email:
            hit = canon.org_for_domain(email_domain(author.email))
            if hit.matched and hit.slug:
                # No `surface`-bearing element: the domain is evidence ABOUT an org, not an
                # affiliation string the document printed, so there is nothing to anchor.
                payload["affiliations"] = [{"surface": author.email, "org_slugs": [hit.slug]}]
                payload["resolution"] = "email_domain"
            else:
                payload["resolution"] = "unknown"
        else:
            payload["resolution"] = "unknown"
        out.append(payload)
    return out


# The model's REPLY shape — deliberately not the payload schema. The reply wraps a list of
# authors, and each author's payload is assembled here (normalized key, org slugs, byline index),
# so the two shapes are related but not equal. Kept beside the code that parses it.
#
# NO `maxItems` / `maxLength`: Anthropic's strict structured-output mode REJECTS them outright
# ("For 'array' type, property 'maxItems' is not supported" — a live 400, found by the eval
# harness). The caps are enforced in :func:`parse_authors` instead (``rows[:MAX_AUTHORS]``,
# per-affiliation truncation), which is where they belong anyway: a cap the provider silently
# does not enforce would otherwise be a cap we only *believe* we have.
#
# Sent in its strict form (``stack.llm.strict_schema``, D185 (C)): OpenAI's strict mode refuses an
# optional key, so ``email`` and ``marker`` are required and nullable; the model answers ``null``
# where it would have omitted them, and :func:`parse_authors` reads ``null`` as absent.
RESPONSE_SCHEMA: dict[str, Any] = strict_schema({
    "type": "object",
    "additionalProperties": False,
    "required": ["authors"],
    "properties": {
        "authors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["author_surface", "affiliations"],
                "properties": {
                    "author_surface": {"type": "string"},
                    "email": {"type": "string"},
                    "affiliations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["surface"],
                            "properties": {
                                "surface": {"type": "string"},
                                "marker": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
})


def anchor_payload(
    payload: dict[str, Any], anchorer: Any, text: str, text_digest: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify one author's affiliation surfaces against the stored text; apply the §6.1 void
    recipe. Returns ``(payload, envelope, voided)``.

    ONLY the ``text`` tier is anchored. An ``openalex`` or ``email_domain`` affiliation is not a
    string the document printed — there is nothing in this text for it to match — so anchoring it
    would manufacture failures and void perfectly good fallback data. The ``xml`` tier is the
    same for a different reason: its surfaces come from the raw markup, and docling reflows or
    drops them in the converted markdown, so the record asserts what the SOURCE says, not what
    the converted text prints. These tiers write no grounding, which is the honest record.

    A surface that fails to anchor is REMOVED from ``affiliations`` and recorded in ``voided``
    (never stored as-if-grounded, §6.2). Because grounding is per-element, one hallucinated half
    of a dual affiliation voids alone and the real half survives. If every element voids, the
    author is left with ``affiliations: []`` — the empty-legal "no affiliation findable" answer,
    NOT a whole-record void: this payload has no schema-required scalar, so §6.1's
    whole-record branch does not apply.
    """
    if payload.get("resolution") != "text":
        return payload, [], []
    envelope: list[dict[str, Any]] = []
    voided: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for aff in payload.get("affiliations", []):
        quote = aff.get("surface", "")
        outcome = anchorer.anchor_detail(quote, text)
        if outcome.anchor is None:
            voided.append(
                {"field": "affiliations", "quote": quote, "reason": outcome.reason or "no_match"}
            )
            continue
        anchor = outcome.anchor
        aff = {**aff, "grounding_idx": [len(envelope)]}
        envelope.append(
            {
                "quote": quote,
                "span": [anchor.span[0], anchor.span[1]],
                "text_digest": text_digest,
                "method": anchor.method,
                "score": anchor.score,
                "occurrences": anchor.occurrences,
            }
        )
        kept.append(aff)
    payload = {**payload, "affiliations": kept}
    if not kept:
        # Every quote failed: the tier claim "the text says so" is no longer true.
        payload["resolution"] = "unknown"
    return payload, envelope, voided


def ambiguous_assignment(payloads: Sequence[Mapping[str, Any]]) -> bool:
    """Whether this document's author→affiliation mapping was INFERRED FROM LAYOUT, not read.

    Fires when several authors share a byline carrying several distinct affiliations and the model
    reported no marker on any of them — i.e. the conversion dropped the superscripts, so nothing in
    the text actually ties a given author to a given org. Measured case: docling flattened
    ``arxiv.2510.13900`` to 7 names and 6 unlinked affiliation strings, and two authors were
    confidently assigned the wrong lab.

    Recorded as a FLAG, never a filter or a fallback trigger. The tempting move is to treat this
    class as low-quality wholesale, but on a 200-paper sample its disagreement rate was 1.9%
    against 3.3% elsewhere — LOWER, because most marker-less bylines are the easy single-shared-
    affiliation case. So the honest thing is to mark the records queryable and let a consumer
    (or a reviewer sampling for the graded set) decide, rather than act on a hunch the data
    contradicts.
    """
    if len(payloads) < 2:
        return False
    surfaces: set[str] = set()
    for p in payloads:
        for a in p.get("affiliations") or []:
            if a.get("marker"):
                return False  # at least one explicit tie exists
            if a.get("surface"):
                surfaces.add(str(a["surface"]))
    return len(surfaces) > 1


def unresolved_fraction(payloads: Sequence[Mapping[str, Any]]) -> float:
    """Share of authors carrying no affiliation at all (0.0 when there are no authors)."""
    if not payloads:
        return 0.0
    empty = sum(1 for p in payloads if not p.get("affiliations"))
    return empty / len(payloads)


def needs_vision_fallback(
    payloads: Sequence[Mapping[str, Any]], *, voided: int = 0, quoted: int = 0
) -> bool:
    """Whether to escalate this document to the page-image pass (step 5).

    Three triggers, all meaning "the converted text failed us, not the document":
    zero authors found in discovery mode; most authors left unaffiliated; or most of the quotes
    the model did emit failed to anchor (the text it read is not the text we stored).
    """
    if not payloads:
        return True
    if unresolved_fraction(payloads) >= VISION_TRIGGER_UNRESOLVED:
        return True
    return quoted > 0 and voided / quoted >= VISION_TRIGGER_UNRESOLVED


def extract(
    *,
    text: str,
    roster: Sequence[str],
    canon: Canon,
    llm_call: Callable[[str, str], Mapping[str, Any] | None],
    openalex: Mapping[str, Sequence[str]] | None = None,
    raw_xml: bytes | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the whole extraction for one document. THE isolation entry point.

    When ``raw_xml`` (the snapshot's raw bytes, supplied by ``main.py`` via ``read.raw`` for
    ``application/xml`` snapshots only) parses to a byline, that IS the answer — publisher
    markup is the publisher's own record of the author→affiliation linkage, so the LLM is
    never called and the document costs nothing. A parse miss falls through to the text tier
    unchanged.

    ``llm_call(authors_block, document_window) -> parsed JSON`` is injected, so a test passes a
    stub, the eval harness passes a cached/replaying client, and ``main.py`` passes the real
    ``services.llm.complete``. Returns (payloads, flags).
    """
    if raw_xml is not None:
        xml_raw = parse_xml_byline(raw_xml)
        if xml_raw:
            aligned, flags = align_to_roster(xml_raw, roster)
            payloads = resolve(aligned, canon, openalex=openalex, tier="xml")
            flags["xml_byline"] = True  # lets the eval and aggregate slice the tier
            return payloads, flags
    window = header_window(text)
    parsed = llm_call(build_authors_block(roster), window)
    raw = parse_authors(parsed)
    aligned, flags = align_to_roster(raw, roster)
    payloads = resolve(aligned, canon, openalex=openalex)
    if not roster and not raw:
        flags["discovery_failed"] = True
    return payloads, flags
