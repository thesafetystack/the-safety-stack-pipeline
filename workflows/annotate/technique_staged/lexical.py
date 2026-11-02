"""Lexical candidates for ``annotate.technique_staged`` (no LLM; :data:`LEXICAL_VERSION`).

A matcher over the taxonomy's own names, compiled once per taxonomy digest (as ``model_roles``
compiles its vocabulary): the ``lexical`` mode's retrieval and a safety net for names the pre-pass
skipped. It proposes; the grading call disposes, so it errs on the side of recall.

Surfaces per technique and group node:

* the display name without its parenthetical parts (``Detection (MAD / OOD)`` -> ``Detection``),
  as one surface: a name is not split at `` / `` (the fragments, ``Age`` from ``Age / minor-content
  classifier``, were generic words; the entries' synonym and instance aliases carry the
  alternative names);
* ``synonym`` aliases and (techniques only) ``instance`` aliases as written; ``legacy`` aliases
  never.

Normalisation: NFKC (text and surfaces) and whitespace collapsed in surfaces. A surface whose
normalised form is shorter than :data:`MIN_SURFACE_CHARS`, or a single word on :data:`STOPLIST`
(generic single-word aliases), is dropped and reported in :attr:`Lexicon.dropped`.

Matching rules, per surface:

* tokens are separated by whitespace or a hyphen/dash; where the surface has a space the text may
  have 1-3 whitespace/hyphen/dash characters, where it has a hyphen or dash it may also have none
  (``fine-tuning`` matches ``fine tuning``, ``fine‑tuning`` and ``finetuning``);
* the last token's plural ``s`` is optional (``guard model`` matches ``guard models`` and
  ``Guard models`` matches ``guard model``; not after ``ss``/``us``/``is``);
* word boundaries at both ends (when the surface starts / ends with a word character);
* surfaces of <= :data:`CASE_SENSITIVE_MAX` characters or all in capitals (``RMU``, ``DP-SGD``) are
  case-sensitive; the others are case-insensitive.

Every slug matches independently: a surface shared by several entries, or nested in another
entry's surface, credits all of them. A slug's regex is tried only at the offsets where one of its
surfaces' literal first tokens (:func:`literal_key`) occurs in the lowercased text, skipping offsets
inside the previous match, which reproduces ``finditer`` exactly (every alternative begins with its
token; ``Pattern.match(text, pos)`` still sees the characters before ``pos`` for the leading
boundary) at a fraction of the cost; when lowercasing changes the text's length, plain
``finditer`` is used. Hits are attributed to the paragraph (from the segmenter) their start falls in: ``hits_body`` / ``hits_refs`` by the paragraph's kind; text past the last
paragraph (beyond the segmenter's cap) is not searched.

Imports nothing from ``stack`` or other workflows (the taxonomy is taken by attribute access);
loaded by path.
"""

from __future__ import annotations

import bisect
import re
import threading
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

LEXICAL_VERSION = "lex-v2"

MIN_SURFACE_CHARS = 3
CASE_SENSITIVE_MAX = 5
SNIPPET_CHARS = 240

#: Single-word aliases too generic to stand for a technique on their own (compared case-insensitively):
#: ``cascade`` (an instance alias of constitutional classifiers) and ``debate`` (a synonym of AI
#: safety via debate). lex-v1's split-fragment entries (``age``, ``keyword``, ...) went with the split.
STOPLIST: frozenset[str] = frozenset({"cascade", "debate"})

_DASHES = "\\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_SEP_SPLIT = re.compile(rf"([\s{_DASHES}]+)")
_HAS_DASH = re.compile(rf"[{_DASHES}]")
_SEP_REQUIRED = rf"[\s{_DASHES}]{{1,3}}"
_SEP_OPTIONAL = rf"[\s{_DASHES}]{{0,3}}"
_PAREN = re.compile(r"\s*\([^()]*\)")


class _Entry(Protocol):
    @property
    def slug(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def synonyms(self) -> tuple[str, ...]: ...


class _Taxonomy(Protocol):
    @property
    def techniques(self) -> Sequence[_Entry]: ...
    @property
    def group_nodes(self) -> Sequence[_Entry]: ...


class _Para(Protocol):
    @property
    def pid(self) -> str: ...
    @property
    def start(self) -> int: ...
    @property
    def end(self) -> int: ...
    @property
    def kind(self) -> str: ...


# ── surfaces ─────────────────────────────────────────────────────────────────────────────────


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).split())


def _deparen(s: str) -> str:
    return " ".join(_PAREN.sub("", s).split())


def name_surfaces(name: str) -> list[str]:
    """The display name without its parenthetical parts, unsplit (module docstring); ``[]`` when nothing is left."""
    surface = _deparen(name).strip()
    return [surface] if surface else []


def entry_surfaces(e: _Entry) -> list[str]:
    """Every candidate surface of an entry, normalised, before the drop rules."""
    raw = [*name_surfaces(e.name), *e.synonyms, *getattr(e, "instances", ())]
    return [s for s in dict.fromkeys(_norm(x) for x in raw) if s]


def drop_reason(surface: str) -> str | None:
    if len(surface) < MIN_SURFACE_CHARS:
        return "short"
    if _SEP_SPLIT.search(surface) is None and surface.lower() in STOPLIST:
        return "stoplist"
    return None


def _last_token(tok: str) -> str:
    esc = re.escape
    if not tok[-1:].isalpha():
        return esc(tok)
    low = tok.lower()
    if low.endswith("s") and not low.endswith(("ss", "us", "is")) and len(tok) > 3:
        return esc(tok[:-1]) + "(?:" + esc(tok[-1]) + ")?"
    return esc(tok) + "s?"


def literal_key(surface: str) -> str:
    """The lowercased literal every match of ``surface`` starts with: its first token (minus an
    optional plural ``s`` when the first token is also the last)."""
    tokens = _SEP_SPLIT.split(surface)[0::2]
    first = tokens[0].lower()
    if len(tokens) == 1 and first.endswith("s") and not first.endswith(("ss", "us", "is")) and len(first) > 3:
        first = first[:-1]
    return first


def is_case_sensitive(surface: str) -> bool:
    return len(surface) <= CASE_SENSITIVE_MAX or (surface.upper() == surface and any(c.isalpha() for c in surface))


def surface_pattern(surface: str) -> str:
    """The regex fragment for one normalised surface (module docstring rules)."""
    parts = _SEP_SPLIT.split(surface)
    tokens, seps = parts[0::2], parts[1::2]
    body = []
    for i, tok in enumerate(tokens):
        if i:
            body.append(_SEP_OPTIONAL if _HAS_DASH.search(seps[i - 1]) else _SEP_REQUIRED)
        body.append(_last_token(tok) if i == len(tokens) - 1 else re.escape(tok))
    pat = "".join(body)
    lead = r"(?<!\w)" if re.match(r"\w", surface) else ""
    trail = r"(?!\w)" if re.search(r"\w$", surface) else ""
    pat = f"{lead}{pat}{trail}"
    return pat if is_case_sensitive(surface) else f"(?i:{pat})"


# ── the compiled lexicon ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Dropped:
    slug: str
    surface: str
    reason: str  # "short" | "stoplist"


def _nfkc_map(text: str) -> tuple[str, list[int]]:
    """NFKC of ``text`` (character by character) and, per output character, its input offset."""
    if text.isascii():
        return text, list(range(len(text)))
    out: list[str] = []
    idx: list[int] = []
    cache: dict[str, str] = {}
    for i, c in enumerate(text):
        n = cache.get(c)
        if n is None:
            n = cache[c] = unicodedata.normalize("NFKC", c)
        out.append(n)
        idx.extend([i] * len(n))
    return "".join(out), idx


class Lexicon:
    """The compiled matcher of one taxonomy. Pure after construction; build once per taxonomy digest
    (:func:`lexicon_for`)."""

    def __init__(self, tax: _Taxonomy) -> None:
        self.surfaces: dict[str, tuple[str, ...]] = {}
        dropped: list[Dropped] = []
        self._regex: dict[str, re.Pattern[str]] = {}
        self._keys: dict[str, tuple[str, ...]] = {}
        for e in [*tax.techniques, *tax.group_nodes]:
            kept: list[str] = []
            for s in entry_surfaces(e):
                reason = drop_reason(s)
                if reason:
                    dropped.append(Dropped(e.slug, s, reason))
                else:
                    kept.append(s)
            self.surfaces[e.slug] = tuple(kept)
            if kept:
                alts = sorted(kept, key=lambda s: (-len(s), s))
                self._regex[e.slug] = re.compile("|".join(f"(?:{surface_pattern(s)})" for s in alts))
                self._keys[e.slug] = tuple(sorted({literal_key(s) for s in kept}))
        self.dropped: tuple[Dropped, ...] = tuple(dropped)

    def match(self, text: str, paragraphs: Sequence[_Para]) -> dict[str, dict[str, Any]]:
        """Per slug with at least one hit, sorted by slug: ``{slug, hits_body, hits_refs, surfaces,
        first_para, snippet}``. ``surfaces`` = distinct matched strings as they appear in the text,
        in document order; ``first_para`` = the pid of the first body hit (else of the first
        references hit); ``snippet`` = <= :data:`SNIPPET_CHARS` characters of the first body hit's
        paragraph around the hit, whitespace collapsed (None without a body hit)."""
        if not paragraphs:
            return {}
        paras = sorted(paragraphs, key=lambda p: p.start)
        starts = [p.start for p in paras]
        limit = max(p.end for p in paras)
        norm, idx = _nfkc_map(text[:limit])
        lowered = norm.lower()
        positional = len(lowered) == len(norm)
        out: dict[str, dict[str, Any]] = {}
        for slug in sorted(self._regex):
            body = refs = 0
            surfaces: dict[str, None] = {}
            first_body: tuple[int, int, _Para] | None = None
            first_any: _Para | None = None
            for ms, me in self._spans(slug, norm, lowered, positional):
                s, e = idx[ms], idx[me - 1] + 1
                k = bisect.bisect_right(starts, s) - 1
                if k < 0:
                    continue
                para = paras[k]
                surfaces.setdefault(text[s:e], None)
                first_any = first_any or para
                if para.kind == "references":
                    refs += 1
                else:
                    body += 1
                    if first_body is None:
                        first_body = (s, e, para)
            if body or refs:
                assert first_any is not None
                out[slug] = {
                    "slug": slug, "hits_body": body, "hits_refs": refs, "surfaces": list(surfaces),
                    "first_para": first_body[2].pid if first_body else first_any.pid,
                    "snippet": _snippet(text, *first_body) if first_body else None,
                }
        return out

    def _spans(self, slug: str, norm: str, lowered: str, positional: bool) -> list[tuple[int, int]]:
        """Non-empty match spans of ``slug``'s regex in ``norm``, as ``finditer`` gives them (class docstring)."""
        rx = self._regex[slug]
        if not positional:
            return [(m.start(), m.end()) for m in rx.finditer(norm) if m.end() > m.start()]
        positions: set[int] = set()
        for key in self._keys[slug]:
            i = lowered.find(key)
            while i >= 0:
                positions.add(i)
                i = lowered.find(key, i + 1)
        spans: list[tuple[int, int]] = []
        end = 0
        for pos in sorted(positions):
            if pos < end:
                continue
            m = rx.match(norm, pos)
            if m is not None and m.end() > pos:
                spans.append((pos, m.end()))
                end = m.end()
        return spans


def _snippet(text: str, s: int, e: int, para: _Para) -> str:
    center = (s + e) // 2
    lo = max(para.start, center - SNIPPET_CHARS // 2)
    hi = min(para.end, lo + SNIPPET_CHARS)
    lo = max(para.start, hi - SNIPPET_CHARS)
    return " ".join(text[lo:hi].split())


#: One compiled lexicon per taxonomy digest (thread-safe: the stage's workers share it).
_LEXICONS: dict[str, Lexicon] = {}
_LOCK = threading.Lock()


def lexicon_for(tax: _Taxonomy, digest: str) -> Lexicon:
    """The lexicon of ``tax`` (whose file has sha256 ``digest``), compiled once per digest per process."""
    with _LOCK:
        if digest not in _LEXICONS:
            _LEXICONS[digest] = Lexicon(tax)
        return _LEXICONS[digest]


__all__ = ["CASE_SENSITIVE_MAX", "LEXICAL_VERSION", "MIN_SURFACE_CHARS", "SNIPPET_CHARS", "STOPLIST", "Dropped",
           "Lexicon", "drop_reason", "entry_surfaces", "is_case_sensitive", "lexicon_for", "literal_key", "name_surfaces",
           "surface_pattern"]
