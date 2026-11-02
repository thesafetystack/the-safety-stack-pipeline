"""The organisation vocabulary (``taxonomy/orgs-<version>.yaml``) and its deterministic matcher: affiliation
surfaces → canonical organisation ids.

**Status (D736, D738, D778):** the vocabulary is version 0.x (ids may still change); 0.2.0 and 0.2.2 are files of the
taxonomies data repository (``data-repo/taxonomies/orgs/``, vendored: ``taxonomy/orgs-<version>.yaml`` is a symlink
into the subtree, ``taxonomy/vendored.lock.yaml``), 0.1.0 and 0.2.1 regular files here; 0.2.2 is the stage's. The models catalogue (1.1.0 on) refers to its entries by
id (makers and labs, ``meta.references.orgs``). The affiliation payload's ``org_slugs`` still come from ``configs/registry.yaml`` through
:mod:`stack.canon`; this module answers "which organisations does this affiliation name" AFTER
``annotate.affiliation``, as a pure function of its verbatim surfaces and one vocabulary file (no LLM, no I/O beyond
reading that file), so a vocabulary edit re-resolves every surface without re-asking the affiliation model. Readers:
the stage ``annotate.affiliation_orgs`` (stores :func:`affiliation_orgs` per document; plan ``batch-2026-10a``
revision 8), whose records the dataset release ships (the kind ``affiliation_orgs``, and each affiliation's
``org_ids`` and ``org_status`` copied into ``affiliation``: :mod:`stack.fold.dataset`), and
``tools/org_vocab.py report`` (coverage on a corpus or release parquet).

**The file** (:class:`OrgVocabFile`, strict): ``version``, ``meta`` (sources, the type and tag glosses, and the
lexicons the matcher reads: country names, generic words, non-organisation patterns) and ``orgs``: one entry per
canonical organisation with a stable ``id`` (slug; the registry's and the models catalogue's maker slugs where those
exist), ``name`` (display only; matched only when also listed in ``aliases``), ``type`` (:data:`ORG_TYPES`), ``country`` (ISO 3166-1 alpha-2), ``parent`` (another entry's id, for
a sub-unit: ``deepmind`` → ``google``), ``ror`` (ROR ids it consolidates), ``other_ids`` (the slugs other
vocabularies use for the same organisation), ``tags`` (project classes: ``frontier-lab``, ``ai-safety-org``,
``model-maker``), and what matches it: ``aliases`` (whole names, compared after :func:`normalise`), ``cased`` (token
sequences compared with case kept: acronyms such as ``MIT``, and names that are also common words such as ``Scale
AI``), ``patterns`` (regexes over the normalised text, for what an alias cannot say), ``domains`` (email domains,
suffix match), ``segment_aliases`` (names matched only when they are a whole comma-separated segment of the
surface: one-word names that also occur inside other names, "Kaggle", "RIKEN"), ``local_names`` (below).

**Matching one surface** (:meth:`OrgMatcher.match`):

1. e-mail addresses in the surface → their domains → ``domains`` (longest suffix wins);
2. the rest is cut at hard separators (``;``, ``|``, newline, bullet) into parts; each part is tokenised
   (NFKC, accents stripped, the Latin letters without a decomposition spelled out: ``ß`` → ``ss``, ``ø`` → ``o``,
   ``ł`` → ``l``, ``æ`` → ``ae``, ...; ``&`` → ``and``, every other non-alphanumeric run a break; each Han or kana
   character a token, Hangul runs kept) — commas are breaks, not boundaries, so ``University of California,
   Berkeley`` is one four-token name;
3. every token span of every part is looked up: as an alias (casefolded), as a cased alias (case kept), against
   the patterns; the candidates of one span are pooled, and the spans are then taken longest first, never
   overlapping a span already taken: ``Google DeepMind`` is ``deepmind`` and not also ``google``,
   ``Department of Computer Science, Stanford University`` is ``stanford`` (the department names nothing);
4. a hit naming several entries (``Northeastern University``) is resolved by the other hits of the surface (exactly
   one candidate has an ancestor among them: ``Institute of X, Chinese Academy of Sciences``), else by the country
   names the surface mentions outside its hits (exactly one candidate in that country), else, when the surface
   names no country, by the countries of its other resolved hits ("Key Laboratory ..., Ministry of Education,
   Peking University" is China's ministry); what stays open is *ambiguous*;
5. a comma segment that is an entry's ``segment_aliases`` names it, unless a hit lies inside that segment or covers
   it ("FAIR, Meta" is the alias "FAIR Meta" across the comma, not also the segment "FAIR"), or it is a place name
   (``meta.places``: Berkeley, Columbia, Princeton, Stanford) after the first segment and an earlier segment named an
   organisation: there it is that organisation's address ("Redwood Research, Berkeley, CA", "University of South
   Carolina, Columbia, SC"), while "Berkeley, Department of EECS" and "Department of EECS, Berkeley" still name it;
   a comma segment that is an entry's ``local_names`` (a sub-unit's own generic name: "Institute of Automation"
   under the Chinese Academy of Sciences) names that entry when exactly one entry with that local name has an
   ancestor among the surface's other hits;
6. a one-token cased hit inside parentheses is dropped when the part has another hit: "Name (ACR)" spells out
   the preceding name's abbreviation (``Vietnam National University Ho Chi Minh City (VNU-HCM)`` is not also the
   Hausdorff Center for Mathematics, HCM);
7. an entry whose descendant was also found in the surface is dropped (the specific one implies it).

Status: ``matched`` (at least one entry, nothing left open), ``ambiguous`` (some hit left open; the resolved ones are
still reported), ``non_org`` (nothing matched and the surface is a non-organisation such as "Independent
researcher", ``meta.non_org``), ``generic`` (nothing matched and the surface is only a unit without its organisation:
generic words headed by a ``meta.unit_words`` word with more after it, "Department of Computer Science", or a
sub-unit's local name whose parent is not named; D737), ``unmatched`` (everything else, including a name the
vocabulary does not know and junk such as "Centre for" or a bare "University").

**One author's affiliations** (:meth:`OrgMatcher.match_author`): each surface as above, then a surface that named
nothing but is a local name is resolved against the organisations the same author's OTHER affiliations named (the
extractor often splits "Institute of Automation; Chinese Academy of Sciences" into two affiliations). Never across
authors.

The loader lints what would make the matcher silently wrong (:func:`lint`): duplicate ids, a dangling or cyclic
parent, an alias made only of generic words or naming a country, a pattern that does not compile, a cased alias that
is not a token sequence. Lint failures raise :class:`OrgVocabError` at load: loud, never mid-run.
"""

from __future__ import annotations

import functools
import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stack import yamlio

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The vocabulary the stage pin defaults to (vendored from the data repository; see the module docstring).
DEFAULT_PATH = REPO_ROOT / "taxonomy" / "orgs-0.2.2.yaml"
#: The matcher's own behaviour version: a change of what a given file resolves to (D737: unit words, regions, the
#: longest country name; 3: a place-name segment after an organisation is its address, ``ß``/``ø``/``ł``/... spelled
#: out; 4: Han and kana are tokens, one per character, Hangul runs too) bumps it; ``annotate.affiliation_orgs`` pins it
#: beside the file's sha256, so a matcher change recomputes every document as a vocabulary edit does. Version 3 was
#: two different matchers on two branches (the place-name rule with the spelled-out letters, and the Han/kana tokens);
#: 4 is both, so no pin of either branch's 3 reads as this one.
MATCHER_VERSION = "4"

OrgType = Literal["university", "company", "institute", "government", "nonprofit", "hospital", "other"]
ORG_TYPES: tuple[str, ...] = OrgType.__args__  # type: ignore[attr-defined]
Status = Literal["matched", "ambiguous", "non_org", "generic", "unmatched"]
STATUSES: tuple[str, ...] = Status.__args__  # type: ignore[attr-defined]


class OrgVocabError(ValueError):
    """The vocabulary file failed validation or lint (raised at load)."""


# ── the file ─────────────────────────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OrgEntry(_Strict):
    """One canonical organisation (module docstring)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    type: OrgType
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    parent: str | None = None
    ror: list[str] = []
    other_ids: list[str] = []
    tags: list[str] = []
    aliases: list[str] = []
    cased: list[str] = []
    segment_aliases: list[str] = []
    local_names: list[str] = []
    patterns: list[str] = []
    domains: list[str] = []
    note: str | None = None


class OrgRegion(_Strict):
    """``meta.regions.<ISO code>``: first-level divisions whose name, or whose cased postal code in an address
    position (a whole comma segment after the first, optionally with a postcode: "Boston, MA 02115"), states the
    country (US states)."""

    names: list[str] = []
    codes: list[str] = []


class OrgVocabMeta(_Strict):
    """``meta``: provenance, glosses and the matcher's lexicons. A catalogue envelope file (0.2.0 on;
    :mod:`stack.catalogue`) names its JSON Schema in ``schema`` and carries ``status``; a 0.1 file has
    ``schema_version``."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: str | None = Field(default=None, alias="schema")
    schema_version: str | None = None
    status: str | None = None
    updated: str
    description: str = ""
    sources: dict[str, str] = {}
    types: dict[str, str]
    tags: dict[str, str] = {}
    #: ISO code → names as written (normalised at load): used to disambiguate and refused as aliases.
    countries: dict[str, list[str]] = {}
    #: Words that name no organisation on their own; an alias made only of these is refused.
    generic_words: list[str] = []
    #: Regexes over the normalised whole surface: a surface that matched nothing and fits one is ``non_org``.
    non_org: list[str] = []
    #: Words that head a unit of an organisation (department, school, laboratory, ...): a surface that matched
    #: nothing is ``generic`` only when it starts with one and has more after it, all generic words (0.2.0 on;
    #: without the list, any all-generic surface is ``generic``, as in 0.1).
    unit_words: list[str] = []
    #: ISO code → first-level divisions that state that country (:class:`OrgRegion`).
    regions: dict[str, OrgRegion] = {}
    #: Place names (cities) that are also segment aliases: after the first segment and an organisation's name they
    #: are its address, not another organisation (module docstring, step 5; 0.2.1 on).
    places: list[str] = []


class OrgVocabFile(_Strict):
    version: str
    meta: OrgVocabMeta
    orgs: list[OrgEntry]


# ── normalisation ────────────────────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_URL = re.compile(r"(?i)\bhttps?://\S+|\bwww\.\S+")
_HARD_SEP = re.compile(r"[;|\n\r•·]+")
_SEGMENT_SEP = re.compile(r"[,;|\n\r•·]+")
#: Latin letters that NFKD does not decompose to a base letter, spelled out (matcher 3): without it each was a
#: token break ("Gießen" → "gie en", "Wrocław" → "wroc aw"), so the ASCII spelling of the same name did not match.
_SPELL_OUT = str.maketrans({"ß": "ss", "ẞ": "SS", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "æ": "ae", "Æ": "AE",
                            "œ": "oe", "Œ": "OE", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th",
                            "ı": "i"})
#: Han and kana characters (matcher version 4): written without spaces, so each is one token and a Chinese alias
#: ("清华大学", four tokens) matches inside a longer unspaced surface ("清华大学计算机系").
_CJK_CHAR = re.compile("([぀-ヺー-ヿ㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f])")
#: Token breaks: every run of characters other than ASCII letters and digits, Han, kana and Hangul (Hangul words
#: stay whole runs: Korean spaces its words). Other scripts are breaks: the vocabulary has no aliases in them.
_NON_ALNUM = re.compile(
    "[^0-9a-zA-Z぀-ヺー-ヿ㐀-䶿一-鿿가-힯豈-﫿"
    "\U00020000-\U0003134f]+")


def _fold(s: str) -> str:
    """NFKC, the letters of :data:`_SPELL_OUT` spelled out, accents stripped (NFKD minus combining marks; Hangul
    recomposed to syllables), ``&`` → `` and ``, apostrophes dropped (``King's`` → ``Kings``). Case is kept: cased
    aliases compare with it."""
    s = unicodedata.normalize("NFKC", s).translate(_SPELL_OUT)
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    if not s.isascii():
        s = unicodedata.normalize("NFC", s)
    s = s.replace("&", " and ")
    return re.sub(r"['’`´]", "", s)


def tokens(s: str) -> list[str]:
    """The case-kept tokens of ``s`` (:func:`_fold`, every run of other characters than ASCII alphanumerics, Han,
    kana and Hangul a break; each Han or kana character its own token, so Chinese aliases match inside unspaced
    surfaces; matcher version 4)."""
    f = _fold(s)
    if not f.isascii():
        f = _CJK_CHAR.sub(r" \1 ", f)
    return [t for t in _NON_ALNUM.split(f) if t]


def _paren_flags(s: str) -> list[bool]:
    """Per token of :func:`tokens` (``s``), whether it sits inside parentheses."""
    flags: list[bool] = []
    depth = 0
    for piece in re.split(r"([()])", s):
        if piece == "(":
            depth += 1
        elif piece == ")":
            depth = max(0, depth - 1)
        else:
            flags.extend([depth > 0] * len(tokens(piece)))
    return flags


def normalise(s: str) -> str:
    """The alias key: :func:`tokens` casefolded and joined with single spaces; a leading ``the`` dropped."""
    toks = [t.casefold() for t in tokens(s)]
    if len(toks) > 1 and toks[0] == "the":
        toks = toks[1:]
    return " ".join(toks)


# ── results ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    """One span of a surface that named vocabulary entries: ``candidates`` (one when resolved), how it matched
    (``alias`` | ``cased`` | ``pattern`` | ``domain``) and the matched text."""

    candidates: tuple[str, ...]
    via: str
    text: str
    resolved_by: str | None = None


@dataclass(frozen=True)
class SurfaceMatch:
    """The matcher's answer for one surface (module docstring)."""

    status: Status
    orgs: tuple[str, ...]
    ambiguous: tuple[tuple[str, ...], ...] = ()
    hits: tuple[Hit, ...] = field(default=(), compare=False)
    #: Entries whose ``local_names`` the surface is, left unresolved (for :meth:`OrgMatcher.match_author`).
    local: tuple[str, ...] = field(default=(), compare=False)


# ── lint ─────────────────────────────────────────────────────────────────────────────────────


def lint(vf: OrgVocabFile) -> list[str]:
    """Problems that would make the matcher silently wrong (module docstring); [] when clean."""
    problems: list[str] = []
    ids = [o.id for o in vf.orgs]
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            problems.append(f"duplicate id {i!r}")
        seen.add(i)
    by_id = {o.id: o for o in vf.orgs}
    generic = {normalise(w) for w in vf.meta.generic_words}
    countries = {normalise(n) for names in vf.meta.countries.values() for n in names}
    for o in vf.orgs:
        if o.parent is not None:
            if o.parent not in by_id:
                problems.append(f"{o.id}: parent {o.parent!r} is not an entry")
            else:
                cur: str | None = o.parent
                hops = 0
                while cur is not None and hops <= len(by_id):
                    if cur == o.id:
                        problems.append(f"{o.id}: parent chain is a cycle")
                        break
                    cur = by_id[cur].parent if cur in by_id else None
                    hops += 1
        for kind, names in (("alias", o.aliases), ("segment alias", o.segment_aliases)):
            for a in names:
                key = normalise(a)
                if not key:
                    problems.append(f"{o.id}: {kind} {a!r} normalises to nothing")
                elif all(t in generic for t in key.split()):
                    problems.append(f"{o.id}: {kind} {a!r} is made only of generic words")
                elif key in countries:
                    problems.append(f"{o.id}: {kind} {a!r} is a country name")
        for a in o.cased:
            if normalise(a) in countries:
                problems.append(f"{o.id}: cased alias {a!r} is a country name")
        if o.local_names and o.parent is None:
            problems.append(f"{o.id}: local_names without a parent")
        for a in o.local_names:
            if not normalise(a):
                problems.append(f"{o.id}: local name {a!r} normalises to nothing")
        for a in o.cased:
            if " ".join(tokens(a)) != a:
                problems.append(f"{o.id}: cased alias {a!r} is not alphanumeric tokens joined by single spaces")
        for p in o.patterns:
            try:
                re.compile(p)
            except re.error as exc:
                problems.append(f"{o.id}: pattern {p!r} does not compile: {exc}")
    for p in vf.meta.non_org:
        try:
            re.compile(p)
        except re.error as exc:
            problems.append(f"meta.non_org {p!r} does not compile: {exc}")
    return problems


# ── the matcher ──────────────────────────────────────────────────────────────────────────────

_VIA_RANK = {"alias": 0, "pattern": 1, "cased": 2}


class OrgMatcher:
    """The compiled vocabulary (module docstring); pure, safe to share, :meth:`match` cached per surface."""

    def __init__(self, vf: OrgVocabFile, *, check: bool = True) -> None:
        if check:
            problems = lint(vf)
            if problems:
                raise OrgVocabError(f"org vocabulary {vf.version}: {len(problems)} problem(s): "
                                    + "; ".join(problems[:20]))
        self.file = vf
        self.entries: dict[str, OrgEntry] = {o.id: o for o in vf.orgs}
        self.parent: dict[str, str | None] = {o.id: o.parent for o in vf.orgs}
        # ``name`` is NOT an alias by itself: an entry whose name is also a phrase ("Scale AI" in "large-scale AI")
        # says so by leaving it out of ``aliases``. Unlinted (bootstrap) matchers drop what lint refuses.
        generic = {normalise(w) for w in vf.meta.generic_words}
        self._country_of_name = {normalise(n): code for code, names in vf.meta.countries.items() for n in names}
        alias: dict[str, set[str]] = {}
        for o in vf.orgs:
            for a in o.aliases:
                key = normalise(a)
                if key and not all(t in generic for t in key.split()) and key not in self._country_of_name:
                    alias.setdefault(key, set()).add(o.id)
        self._alias = {k: tuple(sorted(v)) for k, v in alias.items()}
        self._max_len = max((len(k.split()) for k in self._alias), default=1)
        cased: dict[str, set[str]] = {}
        for o in vf.orgs:
            for a in o.cased:
                if a.casefold() not in self._country_of_name:
                    cased.setdefault(a, set()).add(o.id)
        self._cased = {k: tuple(sorted(v)) for k, v in cased.items()}
        self._max_len = max(self._max_len, *(len(k.split()) for k in self._cased), 1)
        self._patterns = [(re.compile(p), o.id) for o in vf.orgs for p in o.patterns]
        segment: dict[str, set[str]] = {}
        for o in vf.orgs:
            for a in o.segment_aliases:
                key = normalise(a)
                if key and key not in self._country_of_name and not all(t in generic for t in key.split()):
                    segment.setdefault(key, set()).add(o.id)
        self._segment = {k: tuple(sorted(v)) for k, v in segment.items()}
        self._places = {normalise(p) for p in vf.meta.places} - {""}
        local: dict[str, set[str]] = {}
        for o in vf.orgs:
            for a in o.local_names:
                local.setdefault(normalise(a), set()).add(o.id)
        self._local = {k: tuple(sorted(v)) for k, v in local.items()}
        self._generic = generic
        doms = [(d.strip().lower().lstrip("@."), o.id) for o in vf.orgs for d in o.domains]
        self._domains = sorted(doms, key=lambda p: len(p[0]), reverse=True)
        self._country_max = max((len(k.split()) for k in self._country_of_name), default=1)
        self._non_org = [re.compile(p) for p in vf.meta.non_org]
        self._unit_words = {normalise(w) for w in vf.meta.unit_words}
        for code, reg in vf.meta.regions.items():
            for n in reg.names:
                self._country_of_name.setdefault(normalise(n), code)
        self._region_codes = {c: code for code, reg in vf.meta.regions.items() for c in reg.codes}
        self._country_max = max((len(k.split()) for k in self._country_of_name), default=1)
        self._cache: dict[str, SurfaceMatch] = {}

    # -- hierarchy --

    def ancestors(self, org_id: str) -> list[str]:
        """The parent chain of ``org_id``, nearest first."""
        out, cur = [], self.parent.get(org_id)
        while cur is not None and cur not in out:
            out.append(cur)
            cur = self.parent.get(cur)
        return out

    def root(self, org_id: str) -> str:
        """The top of ``org_id``'s parent chain (itself when it has no parent)."""
        chain = self.ancestors(org_id)
        return chain[-1] if chain else org_id

    # -- one surface --

    def domain_org(self, domain: str) -> str | None:
        d = (domain or "").strip().lower().rstrip(".")
        for registered, oid in self._domains:
            if d == registered or d.endswith("." + registered):
                return oid
        return None

    def _spans(self, part: str) -> tuple[list[tuple[int, int, int, Hit]], list[str]]:
        """Candidate hits of one part as ``(start, end, rank, hit)`` over its tokens, and its casefolded tokens."""
        toks_cs = tokens(part)
        toks = [t.casefold() for t in toks_cs]
        out: list[tuple[int, int, int, Hit]] = []
        n_tok = len(toks)
        for i in range(n_tok):
            for n in range(1, min(self._max_len, n_tok - i) + 1):
                key = " ".join(toks[i:i + n])
                cands = self._alias.get(key)
                if cands:
                    out.append((i, i + n, _VIA_RANK["alias"], Hit(cands, "alias", key)))
                cands = self._cased.get(" ".join(toks_cs[i:i + n]))
                if cands:
                    out.append((i, i + n, _VIA_RANK["cased"], Hit(cands, "cased", " ".join(toks_cs[i:i + n]))))
        if self._patterns and toks:
            text = " ".join(toks)
            starts, pos = [], 0
            for t in toks:
                starts.append(pos)
                pos += len(t) + 1
            for rx, oid in self._patterns:
                for m in rx.finditer(text):
                    if m.start() not in starts or (m.end() != len(text) and m.end() + 1 not in starts):
                        continue  # a pattern hit must sit on token boundaries
                    a = starts.index(m.start())
                    b = a + len(text[m.start():m.end()].split())
                    out.append((a, b, _VIA_RANK["pattern"], Hit((oid,), "pattern", m.group(0))))
        return out, toks

    def _countries(self, toks: Sequence[str], covered: set[int]) -> set[str]:
        """The countries ``toks`` names outside ``covered``, longest name first ("New Mexico" is not also
        Mexico, "Papua New Guinea" not also Guinea)."""
        found: set[str] = set()
        i = 0
        while i < len(toks):
            step = 1
            for n in range(min(self._country_max, len(toks) - i), 0, -1):
                if any(j in covered for j in range(i, i + n)):
                    continue
                code = self._country_of_name.get(" ".join(toks[i:i + n]))
                if code:
                    found.add(code)
                    step = n
                    break
            i += step
        return found

    def _region_code_countries(self, surface: str) -> set[str]:
        """Countries stated by a cased postal code of :attr:`OrgVocabMeta.regions` in an address position: a
        whole comma segment after the first, the code alone or followed by a postcode ("Boston, MA 02115"); the
        caller drops them when the surface names another country (Brazil's MA is Maranhão)."""
        if not self._region_codes:
            return set()
        out: set[str] = set()
        for seg in _SEGMENT_SEP.split(surface)[1:]:
            m = re.fullmatch(r"\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*\.?\s*", seg)
            if m and m.group(1) in self._region_codes:
                out.add(self._region_codes[m.group(1)])
        return out

    def _match(self, surface: str) -> SurfaceMatch:
        surface = surface or ""
        hits: list[Hit] = []
        for m in _EMAIL.finditer(surface):
            oid = self.domain_org(m.group(1))
            if oid:
                hits.append(Hit((oid,), "domain", m.group(1).lower()))
        rest = _URL.sub(" ", _EMAIL.sub(" ", surface))
        countries: set[str] = set()
        segs = [normalise(seg) for seg in _SEGMENT_SEP.split(rest)]
        local_cands = [(k, self._local[k]) for k in segs if k in self._local]
        for part in _HARD_SEP.split(rest):
            spans, toks = self._spans(part)
            pooled: dict[tuple[int, int], tuple[int, Hit]] = {}
            for a, b, rank, hit in spans:  # one span, several ways of naming it: pool the candidates
                if (a, b) in pooled:
                    r0, h0 = pooled[(a, b)]
                    cands = tuple(sorted(set(h0.candidates) | set(hit.candidates)))
                    best = h0 if r0 <= rank else hit
                    pooled[(a, b)] = (min(r0, rank), Hit(cands, best.via, best.text))
                else:
                    pooled[(a, b)] = (rank, hit)
            ordered = sorted(pooled.items(), key=lambda kv: (-(kv[0][1] - kv[0][0]), kv[1][0], kv[0][0]))
            covered: set[int] = set()
            taken: list[tuple[int, int, Hit]] = []
            for (a, b), (_rank, hit) in ordered:
                if any(j in covered for j in range(a, b)):
                    continue
                covered.update(range(a, b))
                taken.append((a, b, hit))
            if len(taken) > 1 and "(" in part:
                inparen = _paren_flags(part)
                abbrev = [t for t in taken if t[2].via == "cased" and t[1] - t[0] == 1 and inparen[t[0]]]
                if abbrev and len(abbrev) < len(taken):
                    taken = [t for t in taken if t not in abbrev]
            hits.extend(h for _a, _b, h in sorted(taken, key=lambda t: t[0]))
            countries |= self._countries(toks, covered)
        coded = self._region_code_countries(rest)
        if coded and not countries - coded:  # a postal code yields to a named other country ("São Luís, MA, Brazil")
            countries |= coded
        if self._segment:
            named = [normalise(h.text) for h in hits if h.via != "domain"]
            raw_segs = _SEGMENT_SEP.split(rest)  # aligned with ``segs``
            seg_hit_before = False
            for i, k in enumerate(segs):
                if k not in self._segment or any(n and (f" {n} " in f" {k} " or f" {k} " in f" {n} ") for n in named):
                    continue
                if i > 0 and k in self._places:  # a city after an organisation's name is its address (step 5)
                    before = f" {normalise(' '.join(raw_segs[:i]))} "
                    if seg_hit_before or any(n and f" {n} " in before for n in named):
                        continue
                hits.append(Hit(self._segment[k], "segment", k))
                seg_hit_before = True
        sure = {h.candidates[0] for h in hits if len(h.candidates) == 1}
        open_local: list[str] = []
        near_units = sure | {a for c in sure for a in self.ancestors(c)}  # a sibling unit's parent counts (D737)
        for key, cands in local_cands:
            by_parent = [c for c in cands if set(self.ancestors(c)) & near_units]
            if len(by_parent) == 1:
                hits.append(Hit((by_parent[0],), "local", key, "parent"))
            else:
                open_local.extend(cands)
        local = tuple(dict.fromkeys(open_local))
        if not hits:
            key = normalise(surface)
            status: Status = "unmatched"
            if key and any(rx.search(key) for rx in self._non_org):
                status = "non_org"
            elif key and (local or self._is_unit(key)):
                status = "generic"
            return SurfaceMatch(status, (), local=local)
        resolved: list[Hit] = []
        for h in hits:
            if len(h.candidates) == 1:
                resolved.append(h)
                continue
            by_parent = [c for c in h.candidates if set(self.ancestors(c)) & sure]
            if len(by_parent) == 1:
                resolved.append(Hit((by_parent[0],), h.via, h.text, "parent"))
                continue
            by_country = [c for c in h.candidates if self.entries[c].country in countries]
            if len(by_country) == 1:
                resolved.append(Hit((by_country[0],), h.via, h.text, "country"))
                continue
            near = {self.entries[c].country for c in sure} - {None}
            by_near = [c for c in h.candidates if self.entries[c].country in near] if not countries else []
            if len(by_near) == 1:
                resolved.append(Hit((by_near[0],), h.via, h.text, "country_of_others"))
                continue
            resolved.append(h)
        chosen = list(dict.fromkeys(h.candidates[0] for h in resolved if len(h.candidates) == 1))
        implied = {a for c in chosen for a in self.ancestors(c)}
        orgs = tuple(c for c in chosen if c not in implied)
        open_ = tuple(dict.fromkeys(h.candidates for h in resolved if len(h.candidates) > 1))
        return SurfaceMatch("ambiguous" if open_ else "matched", orgs, open_, tuple(resolved), local)

    def _is_unit(self, key: str) -> bool:
        """Whether the normalised surface ``key`` is only a unit without its organisation: all generic words, and
        (with :attr:`OrgVocabMeta.unit_words`) headed by a unit word with more after it ("Department of
        Mathematics"); a bare "University" or "Centre for" is not a unit, it is ``unmatched``."""
        toks = key.split()
        if not all(t in self._generic or t in self._unit_words for t in toks):
            return False
        if not self._unit_words:
            return True
        return len(toks) > 1 and toks[0] in self._unit_words and any(t not in ("of", "for", "and", "the", "in")
                                                                        for t in toks[1:])

    def match(self, surface: str) -> SurfaceMatch:
        """The answer for ``surface`` (module docstring), memoised: a corpus repeats its surfaces."""
        got = self._cache.get(surface)
        if got is None:
            got = self._cache[surface] = self._match(surface)
        return got

    def match_author(self, surfaces: Sequence[str]) -> list[SurfaceMatch]:
        """The answers for one author's affiliation surfaces (module docstring, "One author's affiliations")."""
        got = [self.match(s or "") for s in surfaces]
        if not any(g.local and not g.orgs for g in got):
            return got
        out = []
        for i, g in enumerate(got):
            if g.orgs or not g.local:
                out.append(g)
                continue
            context = {o for j, h in enumerate(got) if j != i for o in h.orgs}
            context |= {a for o in context for a in self.ancestors(o)}
            cands = [c for c in g.local if set(self.ancestors(c)) & context]
            if len(cands) == 1:
                hit = Hit((cands[0],), "local", normalise(surfaces[i] or ""), "sibling")
                out.append(SurfaceMatch("matched", (cands[0],), (), (hit,)))
            else:
                out.append(g)
        return out


# ── the derivation a pipeline step would write ───────────────────────────────────────────────


def affiliation_orgs(payload: Mapping[str, Any] | None, matcher: OrgMatcher) -> dict[str, Any]:
    """The organisations of one document's ``affiliation`` payload (A.4 ``AFFILIATION``): per affiliation of each
    item, the matcher's status, ids and open candidate sets, plus the document's distinct ids. The
    payload of ``annotate.affiliation_orgs`` (without ``vocab_version``) and the source of the dataset release's
    ``org_ids`` / ``org_status``; a pure function of the payload and the vocabulary."""
    items_out: list[dict[str, Any]] = []
    doc_orgs: dict[str, None] = {}
    for item in (payload or {}).get("items") or []:
        affs = []
        given = item.get("affiliations") or []
        for a, m in zip(given, matcher.match_author([a.get("surface") or "" for a in given])):
            affs.append({"status": m.status, "org_ids": list(m.orgs),
                         "ambiguous": [list(c) for c in m.ambiguous]})
            doc_orgs.update(dict.fromkeys(m.orgs))
        items_out.append({"item_key": item.get("item_key"), "byline_index": item.get("byline_index"),
                          "affiliations": affs})
    return {"items": items_out, "org_ids": list(doc_orgs)}


# ── loading ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrgVocabulary:
    """A loaded vocabulary: the path, its version, the full sha256 of the bytes and the compiled matcher."""

    path: Path
    version: str
    digest: str
    matcher: OrgMatcher


#: The JSON Schema of the catalogue-envelope vocabulary files (``meta.schema``; :mod:`stack.catalogue`).
SCHEMA = "orgs-0"


def from_envelope(data: Mapping[str, Any]) -> dict[str, Any]:
    """A catalogue-envelope vocabulary (``entries``, core fields ``external_ids.ror``) in the shape of
    :class:`OrgVocabFile` (``orgs``, ``ror``); validated against its JSON Schema first."""
    from stack import catalogue

    catalogue.validate(data, SCHEMA)
    orgs = []
    for e in data["entries"]:
        e = dict(e)
        ext = e.pop("external_ids", None) or {}
        if ext.get("ror"):
            e["ror"] = list(ext["ror"])
        orgs.append(e)
    return {"version": data["version"], "meta": data["meta"], "orgs": orgs}


def parse(raw: bytes | str, *, check: bool = True) -> OrgVocabFile:
    """Validate the YAML text of a vocabulary file, 0.1 (``orgs``) or a catalogue envelope (``entries``, its
    JSON Schema checked); no lint (:class:`OrgMatcher` lints)."""
    data = yamlio.load(raw if isinstance(raw, str) else raw.decode("utf-8"))
    try:
        if isinstance(data, Mapping) and "entries" in data:
            data = from_envelope(data)
        return OrgVocabFile.model_validate(data)
    except Exception as exc:  # pydantic's ValidationError: re-raised with the file's role in the message
        raise OrgVocabError(f"org vocabulary: invalid file: {exc}") from exc


def load(path: str | Path | None = None) -> OrgVocabulary:
    """Load, validate, lint and compile a vocabulary file (cached per path and content)."""
    p = Path(path) if path is not None else DEFAULT_PATH
    raw = p.read_bytes()
    return _load(str(p.resolve()), hashlib.sha256(raw).hexdigest())


@functools.lru_cache(maxsize=4)
def _load(path_str: str, digest: str) -> OrgVocabulary:
    raw = Path(path_str).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise OrgVocabError(f"{path_str}: changed while loading")
    vf = parse(raw)
    return OrgVocabulary(path=Path(path_str), version=vf.version, digest=digest, matcher=OrgMatcher(vf))


def matcher_from_entries(entries: Iterable[Mapping[str, Any]], meta: Mapping[str, Any] | None = None,
                         *, check: bool = True) -> OrgMatcher:
    """A matcher over in-memory entries (tests, the bootstrap tool)."""
    m = {"schema_version": "0", "updated": "", "types": {t: t for t in ORG_TYPES}, **(meta or {})}
    return OrgMatcher(OrgVocabFile.model_validate({"version": "0.0.0", "meta": m, "orgs": list(entries)}),
                      check=check)


__all__ = ["DEFAULT_PATH", "MATCHER_VERSION", "ORG_TYPES", "SCHEMA", "STATUSES", "Hit", "OrgEntry", "OrgMatcher",
           "OrgRegion", "OrgVocabError",
           "OrgVocabFile", "OrgVocabMeta", "OrgVocabulary", "SurfaceMatch", "affiliation_orgs", "lint", "load",
           "matcher_from_entries", "normalise", "parse", "tokens"]
