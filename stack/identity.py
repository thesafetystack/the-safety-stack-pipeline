"""``stack.identity`` — the duplicate detectors: every rule that says two ``sources`` records are copies of
one work, declared in one place (round 5, track T-ID): alternate URLs are stored rather than a specialised
``same_as``; papers match on first author + normalised title within a one-year window; every duplicate detector
(producer) is declared in this module so that they are visibly grouped.

Pure, stdlib only, no network, no store access. Gather's own rule is unchanged: a document is a connected
component of sources over shared merging keys within a channel group (DESIGN §6,
:mod:`workflows.gather.union`). The detectors below only add to what joins.

**1. Key adders** (:data:`KEY_ADDERS`; per record). Each reads one source's own record payload and returns extra
typed keys for it. They run where the record is: :meth:`stack.collect.base.Collector.observe` (every collector,
at collect time) and ``stack migrate rekey`` (stored rows, from their ``provider_record`` in-band entry, D524). Their keys are stored in
``sources.identity.keys`` like any key and gather merges on them unchanged; the joining key is the member's
``MEMBER.key_kind``/``key_value``. A rekey adds adder keys and never removes them.

**2. Cross-group keys** (:data:`CROSS_GROUP_KEYS`). An adder may give a source a key of another group's kind: a
LessWrong linkpost (``forum``) gets the ``url`` key of the page it links. Such a key joins the named group's edges
(:func:`bridge_keys`); every other edge stays within its group. A bridge key carried by two or more sources of the
bridging group (two linkposts of one page) joins nothing: two posts linking one page are not one work
(:mod:`workflows.gather.union`, counted ``bridge_ambiguous``).

**3. Matchers** (:data:`MATCHERS`; cross record, deterministic). Gather's union calls :func:`match_pairs` over
every observation and adds an edge ``match:<name>`` per matched pair (``MEMBER.edge_kind``,
``edge_to_source_id``). A matcher compares sources of one match class only (:func:`match_class`: ``paper`` = the
``paper`` group; ``post`` = the ``forum`` group and blog-like ``url`` rows), so a paper never joins the blog post
summarising it. Titles are compared normalised (:func:`norm_title`); generic titles (:data:`GENERIC_TITLES`), short
titles (:data:`MIN_TITLE_TOKENS`) and crowded title buckets (:data:`MAX_BUCKET`) are skipped. A bucket's crowding
counts works, not sources: the sources key edges already join (gather's key clusters) count once, so a well-covered
paper (arXiv, OpenAlex, S2, lookups) does not crowd its own bucket and dissolve its match in a later build.

**Undo.** the ``identity`` overrides (``docs/howto/overrides.md``): ``separate`` removes every computed edge (key or match) between the named
sources; ``merge`` joins sources no detector joins (:mod:`workflows.gather.overrides`).

URL-level alternates (the same page under two URL forms) are :mod:`stack.urls` rules
(:func:`stack.urls.alternate_url_keys`); the ``url_alternates`` adder applies them to a ``url`` row's keys.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import urllib.parse
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from stack import urls

Pair = tuple[str, str]


# ================================================================ 1. key adders (per record)
def _lw_linkpost(record: Mapping[str, Any], keys: Sequence[Pair]) -> list[Pair]:
    """A ForumMagnum post's ``url`` (the target of a linkpost; NULL on a plain post) → the target's key when the
    target is a post-like page or another forum post. A paper target (arXiv, DOI, a paper host, a PDF) gives
    nothing: a linkpost to a paper is a post about the paper; nor does a site's home page. The post's own URL
    gives nothing new."""
    target = record.get("url")
    if not isinstance(target, str) or not target.strip():
        return []
    t = urls.link_keys(target)
    if t is None:
        return []
    if t.group == "forum":
        return [k for k in t.keys if k not in keys]
    if t.group == "url" and t.kind == "other" and urllib.parse.urlsplit(t.keys[0][1]).path.strip("/"):
        return list(t.keys)  # a site's home page is no post
    return []


def _feed_guid(record: Mapping[str, Any], keys: Sequence[Pair]) -> list[Pair]:
    """A feed item whose ``guid`` is a web URL other than its ``link`` (a Substack custom domain and its
    ``*.substack.com`` form, a WordPress ``?p=`` permalink) → the guid's ``url`` key."""
    guid, link = record.get("guid"), record.get("link")
    if not isinstance(guid, str) or not isinstance(link, str):
        return []
    g = urls.url_key(guid)
    return [("url", g)] if g and g != urls.url_key(link) else []


def _openalex_arxiv_location(record: Mapping[str, Any], keys: Sequence[Pair]) -> list[Pair]:
    """An OpenAlex work's ``locations[]`` (requested by ``collect.openalex``) → the arXiv id of an arXiv location
    (landing page or PDF), so a journal or conference version joins its preprint. Other locations' DOIs are not
    taken: a repository copy's DOI may name a dataset or a supplement."""
    out: list[Pair] = []
    for loc in record.get("locations") or []:
        if not isinstance(loc, Mapping):
            continue
        for f in ("landing_page_url", "pdf_url"):
            v = loc.get(f)
            a = urls.arxiv_of_url(v) if isinstance(v, str) else None
            if a is None:
                continue
            from stack.scholarly_records import keys_for  # lazy: scholarly_records imports stack.urls

            out += [(k["kind"], k["value"]) for k in keys_for([("arxiv", a[0])])]
    return out


def _url_alternates(record: Mapping[str, Any], keys: Sequence[Pair]) -> list[Pair]:
    """Every ``url`` key's URL-rule alternates (:func:`stack.urls.alternate_url_keys`)."""
    return [("url", alt) for kind, value in keys if kind == "url" for alt in urls.alternate_url_keys(value)]


@dataclass(frozen=True)
class KeyAdder:
    """One per-record detector: ``fn(record, keys) -> extra (kind, value) pairs`` for a source of a channel in
    ``groups`` (``channels`` narrows it when set). ``keys`` are the record's keys from the collector."""

    name: str
    groups: frozenset[str]
    fn: Callable[[Mapping[str, Any], Sequence[Pair]], list[Pair]]
    channels: frozenset[str] | None = None


#: The key adders, in application order (module docstring, 1).
KEY_ADDERS: tuple[KeyAdder, ...] = (
    KeyAdder("lw_linkpost", frozenset({"forum"}), _lw_linkpost,
             frozenset({"lesswrong", "alignmentforum"})),
    KeyAdder("feed_guid", frozenset({"url"}), _feed_guid),
    KeyAdder("openalex_arxiv_location", frozenset({"paper"}), _openalex_arxiv_location, frozenset({"openalex"})),
    KeyAdder("url_alternates", frozenset({"url"}), _url_alternates),
)


def added_keys(channel: str, group: str, record: Mapping[str, Any] | None,
               keys: Iterable[Pair] = ()) -> list[Pair]:
    """The keys :data:`KEY_ADDERS` give a record of ``channel`` (in ``group``) beyond ``keys``, typed and in
    adder order, each once."""
    have = list(keys)
    out: list[Pair] = []
    if not isinstance(record, Mapping):
        record = {}
    for a in KEY_ADDERS:
        if group not in a.groups or (a.channels is not None and channel not in a.channels):
            continue
        for k in a.fn(record, have):
            if k[1] and k not in have and k not in out:
                out.append(k)
    return out


# ================================================================ 2. cross-group keys
#: group → {key kind → the group whose edges that key joins}. A ``url`` key on a forum source exists only through
#: an adder (``lw_linkpost``); it joins the ``url`` group's ``url`` edges.
CROSS_GROUP_KEYS: Mapping[str, Mapping[str, str]] = {"forum": {"url": "url"}}


def bridge_keys(group: str, keys: Iterable[Pair], non_merging: Iterable[str] = ()) -> tuple[tuple[str, str, str], ...]:
    """``(target group, kind, value)`` of the keys of a ``group`` source that join another group
    (:data:`CROSS_GROUP_KEYS`), minus the channel's non-merging kinds."""
    table = CROSS_GROUP_KEYS.get(group, {})
    skip = set(non_merging)
    return tuple(dict.fromkeys((table[k], k, v) for k, v in keys if k in table and k not in skip))


# ================================================================ 3. matchers (cross record)
class Observed(Protocol):
    """What a matcher reads of one source (:class:`workflows.gather.normalise.Observation` satisfies it)."""

    source_id: str
    channel: str
    group: str
    keys: tuple[Pair, ...]
    row: Mapping[str, Any]


#: Normalised titles never matched (a stoplist; titles below :data:`MIN_TITLE_TOKENS` are skipped anyway).
GENERIC_TITLES: frozenset[str] = frozenset({
    "table of contents", "front matter", "back matter", "letter to the editor", "book review", "book reviews",
    "call for papers", "supplementary material", "supplementary information", "supplementary materials",
    "frequently asked questions", "editorial board", "list of reviewers", "author index", "subject index",
    "message from the general chair", "message from the program chairs", "conference program",
    "ai safety newsletter", "alignment newsletter", "weekly open thread", "monthly open thread",
    "open thread", "links for the week", "quick takes", "shortform", "announcing our new website",
    "a note on terminology", "year in review", "annual report", "research update", "progress update",
    "frequently asked questions faq", "terms of service", "privacy policy",
})
#: Minimum normalised title tokens per match class.
MIN_TITLE_TOKENS: Mapping[str, int] = {"paper": 3, "post": 4}
#: A (class, title) bucket with more key clusters (works) than this is generic: none of it is matched (counted).
MAX_BUCKET = 8
#: Post doc types of the ``url`` group that are posts (a card, a policy or an untyped link is not).
POST_DOC_TYPES: frozenset[str] = frozenset({"lab_blog", "forum_post", "critique"})
#: Windows: a paper's versions within a year; a crosspost within days.
PAPER_WINDOW = dt.timedelta(days=366)
POST_WINDOW = dt.timedelta(days=7)

_BRACKET_TAG = re.compile(r"^\s*(?:[\[(][^\])]{1,24}[\])]\s*)+")
#: Separators: every run of characters other than ASCII letters and digits and the letters of the non-Latin,
#: non-Greek scripts (Cyrillic, Armenian, Hebrew, Arabic, the Indic scripts, Thai, Georgian, Hangul, kana, CJK;
#: the Chinese-coverage work). Until then every non-ASCII letter left after accent folding was a separator, so a Chinese or Russian
#: title normalised to '' and took no part in matching. Latin letters accent folding keeps (``ß``, ``ø``) and Greek
#: letters stay separators: in titles Greek is mostly mathematics (``$μ$J`` and ``µJ`` of one paper), and keeping it
#: split such pairs on the store (a measurement of the Chinese-coverage work).
_NON_WORD = re.compile(
    "[^a-z0-9\u0400-\u052f\u0530-\u06ff\u0900-\u0dff\u0e00-\u0e7f\u10a0-\u10ff\u1100-\u11ff"
    "\u3040-\u30fa\u30fc-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
    "\U00020000-\U0003134f]+")
#: Characters of scripts written without word spaces (Han, kana): each is one title token, so a Chinese title has
#: as many tokens as characters and :data:`MIN_TITLE_TOKENS` applies to it sensibly. Names keep their runs.
_CJK_CHAR = re.compile("([\u3040-\u30fa\u30fc-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f])")
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md"})


def _fold(text: str, *, split_cjk: bool = False) -> str:
    """NFKD, combining marks dropped, lower case, word-character tokens joined by one space (``split_cjk``: each
    Han or kana character its own token)."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    if not s.isascii():
        s = unicodedata.normalize("NFC", s)  # Hangul jamo back to syllables (no combining mark is left to compose)
        if split_cjk:
            s = _CJK_CHAR.sub(r" \1 ", s)
    return _NON_WORD.sub(" ", s).strip()


def norm_title(title: str | None) -> str:
    """Lower case accent-folded word tokens joined by one space, each Han or kana character a token; leading
    bracket tags (``[Paper]``, ``[Linkpost]``, ``(AN #12)``) dropped. A title in Latin or Greek script normalises
    exactly as before the Chinese-coverage work (:data:`_NON_WORD`)."""
    if not title:
        return ""
    return _fold(_BRACKET_TAG.sub("", title), split_cjk=True)


_ASCII_LETTER = re.compile("[a-z]")


def _one_script(name: str) -> str:
    """The name as :func:`surname` and :func:`author_key` read it. A byline in Latin letters and another script
    (``曹东兴 Cao Dongxing``, ``Lei Zhang (张磊)``, ``Jixuan Cai, 蔡纪烜``) keeps only its tokens with an ASCII
    letter, so it keys as its Latin-only rendering, as before the non-Latin letters were kept (D744); with them a
    leading Han name took the initial (``('dongxing', '曹')``) and matched no Latin-only record. A comma part left
    empty is dropped (the comma separated the two renderings). Beside a non-Latin token, the token ``null`` (a
    missing name part written out) is dropped too, so ``null 章成志`` keys as ``章成志``. A name in one script is
    returned as it is; a token mixing Cyrillic look-alike letters into Latin ones (``Мikhail``) counts as Latin."""
    parts = [_fold(p).split() for p in name.split(",", 1)]
    toks = [t for p in parts for t in p]
    latin = any(_ASCII_LETTER.search(t) and t != "null" for t in toks)
    if not any(not t.isascii() and not _ASCII_LETTER.search(t) for t in toks) or not latin and "null" not in toks:
        return name
    kept = [[t for t in p if t != "null" and (not latin or t.isascii() or _ASCII_LETTER.search(t))] for p in parts]
    return ", ".join(" ".join(p) for p in kept if p)


def surname(name: str | None) -> str:
    """The folded surname of an author name: before the comma of ``Surname, Given``, else the last token
    (name suffixes such as ``Jr`` skipped); ``''`` for a one-letter result. A name in two scripts is read by
    its Latin tokens (:func:`_one_script`)."""
    if not name:
        return ""
    name = _one_script(name)
    if "," in name:
        toks = _fold(name.split(",", 1)[0]).split()
    else:
        toks = [t for t in _fold(name).split() if t not in _NAME_SUFFIXES]
    s = toks[-1] if toks else ""
    return s if len(s) > 1 else ""


def author_key(name: str | None) -> tuple[str, str]:
    """``(surname, first initial)`` of an author name: :func:`surname`, and the first letter of the given
    names (after the comma of ``Surname, Given``, else the first token; ``''`` for a surname-only name), so
    ``Wei Wang``, ``W. Wang`` and ``Wang, Wei`` share a key and ``Lei Wang`` does not (as
    :func:`workflows.gather.merge.author_key`, which takes the last token as the surname)."""
    s = surname(name)
    if not s or not name:
        return ("", "")
    name = _one_script(name)
    if "," in name:
        given = _fold(name.split(",", 1)[1])
    else:
        toks = [t for t in _fold(name).split() if t not in _NAME_SUFFIXES]
        given = toks[0] if len(toks) > 1 else ""
    return (s, given[:1])


def _name_tokens(name: str | None) -> frozenset[str]:
    """The folded tokens of a name, both scripts of a two-script byline kept (unlike :func:`author_key`):
    :func:`same_author` is a subset test, so ``曹东兴 Cao Dongxing`` matches ``Cao Dongxing`` and ``曹东兴``."""
    return frozenset(t for t in _fold(name or "").split() if t not in _NAME_SUFFIXES)


def same_author(a: Iterable[str | None], b: Iterable[str | None]) -> bool:
    """Some author of ``a`` and some of ``b`` name the same person loosely: one name's tokens are a subset of the
    other's (``Zvi`` ⊂ ``Zvi Mowshowitz``; a LessWrong ``Stuart_Armstrong`` = ``Stuart Armstrong``)."""
    ta = [t for t in map(_name_tokens, a) if t and max(map(len, t)) > 2]
    tb = [t for t in map(_name_tokens, b) if t and max(map(len, t)) > 2]
    return any(x <= y or y <= x for x in ta for y in tb)


def match_class(o: Observed) -> str | None:
    """``paper`` for the ``paper`` group, ``post`` for the ``forum`` group and ``url`` rows of a post doc type
    (:data:`POST_DOC_TYPES`), else ``None`` (never matched)."""
    if o.group == "paper":
        return "paper"
    if o.group == "forum":
        return "post"
    if o.group == "url" and ((o.row.get("metadata") or {}).get("doc_type")) in POST_DOC_TYPES:
        return "post"
    return None


def _md(o: Observed) -> Mapping[str, Any]:
    return o.row.get("metadata") or {}


def _authors(o: Observed) -> list[str | None]:
    return [a.get("name") for a in (_md(o).get("authors") or [])]


def _within(a: Observed, b: Observed, window: dt.timedelta) -> bool:
    pa, pb = _md(a).get("published_at"), _md(b).get("published_at")
    return pa is not None and pb is not None and abs(pa - pb) <= window


def _arxiv_conflict(a: Observed, b: Observed) -> bool:
    xa = {v for k, v in a.keys if k == "arxiv"}
    xb = {v for k, v in b.keys if k == "arxiv"}
    return bool(xa and xb and not xa & xb)


def _paper_title_author(a: Observed, b: Observed) -> bool:
    """Papers: equal normalised title (the bucket), equal first-author :func:`author_key` (surname and first
    initial), published within :data:`PAPER_WINDOW`; two distinct arXiv ids never match (two deposits)."""
    ka, kb = (author_key((_authors(o) or [None])[0]) for o in (a, b))
    return bool(ka[0]) and ka == kb and _within(a, b, PAPER_WINDOW) and not _arxiv_conflict(a, b)


def _post_title_author(a: Observed, b: Observed) -> bool:
    """Posts: equal normalised title (the bucket), published within :data:`POST_WINDOW`, and the same author
    (:func:`same_author`) when both have a byline, else the same channel (a channel stands in for a missing
    byline, never for a different one)."""
    if not _within(a, b, POST_WINDOW):
        return False
    aa, ab = _authors(a), _authors(b)
    if any(aa) and any(ab):
        return same_author(aa, ab)
    return a.channel == b.channel


@dataclass(frozen=True)
class Matcher:
    """One cross-record detector over sources of match class ``klass`` sharing a normalised title."""

    name: str
    klass: str
    fn: Callable[[Observed, Observed], bool]


#: The matchers (module docstring, 3); the edge kind is ``match:<name>``.
MATCHERS: tuple[Matcher, ...] = (
    Matcher("paper_title_author", "paper", _paper_title_author),
    Matcher("post_title_author", "post", _post_title_author),
)


@dataclass
class MatchStats:
    """Per matcher: pairs matched; per class: buckets skipped as crowded (:data:`MAX_BUCKET` key clusters)."""

    pairs: dict[str, int]
    crowded: dict[str, int]


def match_pairs(obs: Mapping[str, Observed],
                order: Callable[[str], tuple[object, ...]],
                cluster: Mapping[str, str] | None = None) -> tuple[list[tuple[str, str, str]], MatchStats]:
    """``(a, b, matcher name)`` for every matched pair, ``a`` before ``b`` in ``order``; sorted, so gather stays
    deterministic. Sources without a title, a class, or with a generic or short title take no part. ``cluster``
    (source → its key cluster, gather's components over key edges; default: every source its own) sets what a
    bucket's crowding counts (:data:`MAX_BUCKET`); every pair of an uncrowded bucket is compared."""
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in sorted(obs, key=order):
        o = obs[s]
        klass = match_class(o)
        if klass is None:
            continue
        t = norm_title(_md(o).get("title"))
        if not t or t in GENERIC_TITLES or len(t.split()) < MIN_TITLE_TOKENS[klass]:
            continue
        buckets[(klass, t)].append(s)
    by_class = {m.klass: m for m in MATCHERS}
    out: list[tuple[str, str, str]] = []
    stats = MatchStats({m.name: 0 for m in MATCHERS}, {k: 0 for k in by_class})
    for (klass, _), ss in sorted(buckets.items()):
        if len(ss) < 2:
            continue
        if len({cluster.get(s, s) for s in ss} if cluster is not None else ss) > MAX_BUCKET:
            stats.crowded[klass] += 1
            continue
        m = by_class.get(klass)
        if m is None:
            continue
        for i, a in enumerate(ss):
            for b in ss[i + 1:]:
                if m.fn(obs[a], obs[b]):
                    out.append((a, b, m.name))
                    stats.pairs[m.name] += 1
    return out, stats


__all__ = ["CROSS_GROUP_KEYS", "GENERIC_TITLES", "KEY_ADDERS", "MATCHERS", "MAX_BUCKET", "MIN_TITLE_TOKENS",
           "KeyAdder", "MatchStats", "Matcher", "added_keys", "bridge_keys", "match_class", "match_pairs",
           "norm_title", "same_author", "surname"]
