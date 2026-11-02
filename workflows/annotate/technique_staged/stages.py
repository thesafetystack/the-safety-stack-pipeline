"""The pure pieces of ``annotate.technique_staged``'s calls (no LLM, no I/O): the strict schemas of the
pre-pass P and the selection S, the grading schema, the user-message fills, the post-processing of P's
notes and S's answers, the sub-menu assembly and the sweep call's reading notes. ``main.py`` makes the
calls and owns the flow; everything here is a function of its arguments, so it is unit-tested directly
and the user messages are deterministic (the eval freeze reads them).

**P notes** (:func:`clean_mentions`, :func:`dedupe_overlap`, :func:`assign_ids`). Per P answer: a
``paras`` number that is not a paragraph of the part read is dropped (repair
``para_out_of_chunk:<part>:<n>``) and at most :data:`MAX_PARAS` are kept; a note left with no paragraph
is kept with surface and gloss only (no section, no snippet); the surface is cut to
:data:`SURFACE_CHARS` characters and the gloss to :data:`GLOSS_WORDS` words, whitespace collapsed; an
empty surface drops the note (``empty_surface:<part>``). The **snippet** (the pointer the later calls
see) is cut by code, so it is verbatim: a window of about :data:`SNIPPET_CHARS` characters centred on
the surface in the first cited paragraph that contains it (case- and whitespace-insensitive), else the
first cited paragraph's head, whitespace collapsed. **Dedupe** removes only the exact duplicates the
chunk overlap creates: a note of chunk k > 1 that cites only paragraphs of that chunk's overlap and
matches an earlier note of chunk k-1 (same normalised surface, same stance hint, overlapping
paragraphs) is dropped and its paragraphs merged into the earlier one (count ``mentions_deduped``).
Two notes of one chunk are never merged (occurrence-level notes, the prompt's rule). **Ids** ``m1..``
in text order (first cited paragraph; a note without one sorts at its part's first paragraph).

**S answers** (:func:`apply_selection`): unknown slugs dropped (``unknown_slug:<id>:<slug>``), at most
:data:`MAX_CANDIDATES` candidates per mention (``candidates_cut:<id>``), a repeated candidate slug once;
an answer for an id not asked dropped (``unknown_mention_id:<id>``), a repeated id keeps the first
(``duplicate_mention_id:<id>``); ``none`` beside candidates is ignored (``none_with_candidates:<id>``);
``proposed_name`` / ``proposed_gloss`` of ``""`` are read as null (C3), and kept only for
``off_taxonomy``; ``doc_level`` validated the same way, at most :data:`MAX_DOC_LEVEL`. The ids the
answer left out are returned for the caller's one re-ask.

**Sub-menu** (:func:`assemble_submenu`): the union of S's candidates (every confidence), ``doc_level``
and the lexical body hits (``staged``), or the lexical body hits alone (``lexical``). Each slug gets a
class: ``protected`` (an S ``high`` candidate or ``doc_level``), else S's best ``med`` / ``low``, else
``lexical`` (body hits >= 2) / ``lexical_one``. Over :data:`SUBMENU_CAP` slugs, the classes are dropped
in the order ``lexical_one``, ``low``, ``lexical``, ``med`` (latest-ranked first within a class), each
drop recorded as ``dropped_by_cap:<class>:<slug>``; ``protected`` slugs are never dropped (more than
the cap of them: ``submenu_over_cap:<n>``). Order: class rank (protected, med, lexical, low,
lexical_one), then first appearance (``doc_level``, then mentions in id order, then lexical hits in
text order). Pointers per slug: up to :data:`MAX_POINTERS` notes (distinct stance hints first, then text
order) and ``(+k more mentions)``, the ``doc_level`` reason, the lexical count, and for a slug no note
proposed the matcher's first body hit.

**Inventory accounting** (:func:`inventory_accounting`, the single call of an inventory contract, prompt
set ``technique-staged-v3``): the answer's ``sweeps.passages`` and top-level ``not_graded`` are counted,
never turned into rows. Slugs a passage or a §7 sweep lists that are menu slugs are the *listed* set; a
``not_graded`` entry needs a menu slug (else ``bad_not_graded``; an unknown reason is recorded as
``not_graded_reason_unknown:<slug>``); a ``not_graded`` slug that also has a kept row keeps the row
(``not_graded_with_row:<slug>``). Counts: ``inv_passages`` (passage lines), ``inv_passage_slugs``
(distinct menu slugs in passages), ``inv_not_graded``, ``inv_unaccounted`` (listed, neither a kept row
nor ``not_graded``) and ``inv_not_graded_with_row``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: P's stance hints (the role names plus ``cited`` for ``cited_work``), the schema enum.
STANCES: tuple[str, ...] = ("introduced", "used", "evaluated", "modified", "analyzed", "attested_use",
                            "suspected_use", "considered", "critiqued", "discussed", "cited", "mentioned")
CONFIDENCES: tuple[str, ...] = ("high", "med", "low")
NONE_REASONS: tuple[str, ...] = ("not_a_technique", "ml_method_only", "too_vague", "off_taxonomy")
REJECT_REASONS: tuple[str, ...] = ("not_in_text", "wrong_sibling", "generic_only", "ml_method_only", "not_a_technique")
#: ``not_graded[].why`` of the inventory contract (single-v2).
NOT_GRADED_REASONS: tuple[str, ...] = ("not_in_text", "parent_of_graded_child", "bibliography_only")
#: The §7 sweeps whose entries name a ``technique`` (technique_roles' judge checks the same lists).
SWEEPS_WITH_TECHNIQUE: tuple[str, ...] = ("deployment_claims", "recommendations", "controls_discussed")

#: P's mention cap (the prompt's limit; more, or ``overflow``, splits the part).
MENTION_CAP = 120
SURFACE_CHARS = 80
GLOSS_WORDS = 30
MAX_PARAS = 3
SNIPPET_CHARS = 240
MAX_CANDIDATES = 5
MAX_DOC_LEVEL = 5
PROPOSED_NAME_CHARS = 80
PROPOSED_GLOSS_CHARS = 300
WHY_CHARS = 200
#: Mentions per S call (a longer list goes in batches on the same system part).
SELECT_BATCH = 150
SUBMENU_CAP = 100
MAX_POINTERS = 3
#: The ``candidates`` fill of an empty sub-menu (the grading call still runs, C1).
NO_CANDIDATES = "(none)"
#: The ``reading_notes`` fill when P found nothing (P5).
NO_NOTES = "(no reading notes)"
OPENING_HEADER = "# DOCUMENT OPENING (context only)\n\n"

#: Class ranks of the sub-menu (lower is kept longer; module docstring).
CLASS_RANK: dict[str, int] = {"protected": 0, "med": 1, "lexical": 2, "low": 3, "lexical_one": 4}


# ── schemas ──────────────────────────────────────────────────────────────────────────────────


def prepass_schema() -> dict[str, Any]:
    """P's answer shape (before ``strict_schema``): fixed keys and enums, no length keywords
    (lengths are cut in code)."""
    mention = {
        "type": "object",
        "required": ["paras", "surface", "gloss", "stance_hint", "implicit"],
        "properties": {
            "paras": {"type": "array", "items": {"type": "integer"}},
            "surface": {"type": "string"},
            "gloss": {"type": "string"},
            "stance_hint": {"enum": list(STANCES)},
            "implicit": {"type": "boolean"},
        },
    }
    return {"type": "object", "required": ["mentions", "overflow"],
            "properties": {"mentions": {"type": "array", "items": mention}, "overflow": {"type": "boolean"}}}


def select_schema() -> dict[str, Any]:
    """S's answer shape (before ``strict_schema``): per mention ``{id, candidates, none}``, ``none``
    null or ``{reason, proposed_name, proposed_gloss}`` (the two nullable), plus ``doc_level``."""
    candidate = {"type": "object", "required": ["slug", "confidence"],
                 "properties": {"slug": {"type": "string"}, "confidence": {"enum": list(CONFIDENCES)}}}
    none = {"anyOf": [
        {"type": "object", "required": ["reason", "proposed_name", "proposed_gloss"],
         "properties": {"reason": {"enum": list(NONE_REASONS)}, "proposed_name": {"type": ["string", "null"]},
                        "proposed_gloss": {"type": ["string", "null"]}}},
        {"type": "null"}]}
    mention = {"type": "object", "required": ["id", "candidates", "none"],
               "properties": {"id": {"type": "string"}, "candidates": {"type": "array", "items": candidate},
                              "none": none}}
    doc_level = {"type": "object", "required": ["slug", "confidence", "why"],
                 "properties": {"slug": {"type": "string"}, "confidence": {"enum": list(CONFIDENCES)},
                                "why": {"type": "string"}}}
    return {"type": "object", "required": ["mentions", "doc_level"],
            "properties": {"mentions": {"type": "array", "items": mention},
                           "doc_level": {"type": "array", "items": doc_level}}}


def grading_schema(base: Mapping[str, Any]) -> dict[str, Any]:
    """The grading call's schema: technique_roles' permissive ``output_schema()`` (``base``, which
    carries the optional ``sweeps``) plus an optional ``rejected`` list (P3): the degrade fold sends
    this object as the JSON Schema to conform to, so it must name what the prompt asks for."""
    out = dict(base)
    props = dict(base["properties"])
    props["rejected"] = {"type": "array", "items": {"type": "object", "properties": {
        "slug": {"type": "string"}, "reason": {"type": "string"}}}}
    out["properties"] = props
    return out


def inventory_schema(base: Mapping[str, Any]) -> dict[str, Any]:
    """The schema of a single call with the inventory contract (single-v2): ``base`` (technique_roles'
    permissive ``output_schema()``) plus the optional ``sweeps.passages`` and top-level ``not_graded``.
    As for :func:`grading_schema`, the degrade fold sends this object as the schema to conform to, so it
    names what the prompt asks for (with ``base`` alone the model leaves both keys out)."""
    out = dict(base)
    props = dict(base["properties"])
    sweeps = dict(props["sweeps"])
    sweep_props = dict(sweeps["properties"])
    sweep_props["passages"] = {
        "type": "array", "description": "the inventory, written first: every technique-bearing passage in document order",
        "items": {"type": "object", "properties": {"where": {"type": "string"}, "what": {"type": "string"},
                                                   "slugs": {"type": "array", "items": {"type": "string"}},
                                                   "none": {"type": "string"}}}}
    sweeps["properties"] = sweep_props
    props["sweeps"] = sweeps
    props["not_graded"] = {"type": "array", "items": {"type": "object", "properties": {
        "slug": {"type": "string"}, "why": {"type": "string"}}}}
    out["properties"] = props
    return out


# ── parts read by P ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Part:
    """One P input: a segmenter chunk, or a half of one after a split. ``label`` names it in the
    payload (``"2"``, ``"2a"``); ``index_text`` is the prompt's part number (``"2 (first half)"``);
    ``chunk`` the chunk's number; ``overlap`` the paragraph numbers repeated from the previous chunk."""

    label: str
    index_text: str
    total: int
    chunk: int
    paragraphs: tuple[Any, ...]
    overlap: frozenset[int] = frozenset()
    first_body: bool = False  # starts at the first body paragraph (no opening shown)

    @property
    def numbers(self) -> frozenset[int]:
        return frozenset(p.index for p in self.paragraphs)

    @property
    def section_labels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.section for p in self.paragraphs))


def parts_of(seg: Any) -> list[Part]:
    """The segmentation's chunks as :class:`Part` s."""
    body0 = next((p.index for p in seg.paragraphs if p.kind == "body"), None)
    out = []
    for c in seg.chunks:
        out.append(Part(label=str(c.index), index_text=str(c.index), total=c.total, chunk=c.index,
                        paragraphs=tuple(c.paragraphs), overlap=frozenset(p.index for p in c.paragraphs[:c.overlap]),
                        first_body=bool(c.paragraphs) and c.paragraphs[0].index == body0))
    return out


def split_part(part: Part) -> tuple[Part, Part] | None:
    """The two halves of ``part``, cut at the paragraph boundary nearest half its characters (each half
    at least one paragraph); None for a one-paragraph part."""
    paras = part.paragraphs
    if len(paras) < 2:
        return None
    total = sum(len(p.text) for p in paras)
    acc, cut, best = 0, 1, None
    for i in range(1, len(paras)):
        acc += len(paras[i - 1].text)
        d = abs(2 * acc - total)
        if best is None or d < best:
            best, cut = d, i
    a, b = paras[:cut], paras[cut:]
    first = Part(label=f"{part.label}a", index_text=f"{part.index_text} (first half)", total=part.total,
                 chunk=part.chunk, paragraphs=a, overlap=part.overlap & frozenset(p.index for p in a),
                 first_body=part.first_body)
    second = Part(label=f"{part.label}b", index_text=f"{part.index_text} (second half)", total=part.total,
                  chunk=part.chunk, paragraphs=b, overlap=part.overlap & frozenset(p.index for p in b))
    return first, second


def render_part(part: Part) -> str:
    """The part as P reads it: ``[pN] <paragraph>`` per paragraph, blank-line separated (as
    ``segment.render_chunk``)."""
    return "\n\n".join(f"[{p.pid}] {p.text}" for p in part.paragraphs)


def prepass_fills(part: Part, opening: str) -> dict[str, str]:
    """The prepass prompt's user-part fills for ``part`` (the opening only for a part that does not
    start the body)."""
    return {"opening": "" if part.first_body or not opening else f"{OPENING_HEADER}{opening}\n\n",
            "chunk_index": part.index_text, "chunk_total": str(part.total),
            "section_labels": "; ".join(part.section_labels), "chunk": render_part(part)}


# ── P notes ──────────────────────────────────────────────────────────────────────────────────


@dataclass(eq=False)
class Mention:
    """One P note after code post-processing (module docstring), and S's answer for it. Compared by
    identity (two notes with equal fields are two occurrences)."""

    part: str
    chunk: int
    position: int
    paras: list[int]
    surface: str
    gloss: str
    stance: str
    implicit: bool
    section: str | None
    snippet: str | None
    sort_at: int
    id: str = ""
    candidates: list[tuple[str, str]] = field(default_factory=list)  # (slug, confidence)
    none_reason: str | None = None
    proposed_name: str | None = None
    proposed_gloss: str | None = None
    answered: bool = False


def one_line(s: str) -> str:
    return " ".join(s.split())


def cut_words(s: str, n: int) -> str:
    words = s.split()
    return " ".join(words[:n])


def snippet(para_text: str, surface: str) -> str | None:
    """A ~:data:`SNIPPET_CHARS` window of ``para_text`` centred on ``surface`` (case- and
    whitespace-insensitive), whitespace collapsed; None when the surface is not in it."""
    tokens = surface.split()
    if not tokens:
        return None
    m = re.search(r"\s+".join(re.escape(t) for t in tokens), para_text, re.IGNORECASE)
    if m is None:
        return None
    center = (m.start() + m.end()) // 2
    lo = max(0, center - SNIPPET_CHARS // 2)
    hi = min(len(para_text), lo + SNIPPET_CHARS)
    lo = max(0, hi - SNIPPET_CHARS)
    return one_line(para_text[lo:hi])


def head(para_text: str) -> str:
    return one_line(para_text[:SNIPPET_CHARS])


def clean_mentions(parsed: Mapping[str, Any], part: Part, repairs: list[str]) -> list[Mention]:
    """P's answer for ``part`` as :class:`Mention` s (module docstring), in answer order."""
    by_number = {p.index: p for p in part.paragraphs}
    out: list[Mention] = []
    for pos, raw in enumerate(parsed.get("mentions") or []):
        if not isinstance(raw, Mapping):
            repairs.append(f"bad_mention:{part.label}")
            continue
        surface = one_line(str(raw.get("surface") or ""))[:SURFACE_CHARS]
        if not surface:
            repairs.append(f"empty_surface:{part.label}")
            continue
        paras: list[int] = []
        for n in raw.get("paras") or []:
            if isinstance(n, bool) or not isinstance(n, int) or n not in by_number:
                repairs.append(f"para_out_of_chunk:{part.label}:{str(n)[:12]}")
            elif n not in paras:
                paras.append(n)
        paras = paras[:MAX_PARAS]
        cited = [by_number[n] for n in paras]
        snip = next((s for s in (snippet(p.text, surface) for p in cited) if s), None)
        if snip is None and cited:
            snip = head(cited[0].text)
        stance = raw.get("stance_hint")
        out.append(Mention(
            part=part.label, chunk=part.chunk, position=pos, paras=paras, surface=surface,
            gloss=cut_words(one_line(str(raw.get("gloss") or "")), GLOSS_WORDS),
            stance=stance if stance in STANCES else "mentioned", implicit=bool(raw.get("implicit")),
            section=cited[0].section if cited else None, snippet=snip,
            sort_at=paras[0] if paras else min(by_number) if by_number else 0))
    return out


_DASHES = "-‐‑‒–—―−"


def norm_surface(s: str) -> str:
    """Case, whitespace and hyphens folded (the overlap dedupe's key)."""
    s = unicodedata.normalize("NFKC", s).casefold()
    for d in _DASHES:
        s = s.replace(d, " ")
    return " ".join(s.split())


def dedupe_overlap(mentions: list[Mention], parts: Sequence[Part]) -> tuple[list[Mention], int]:
    """``mentions`` without the chunk-overlap double reads (module docstring); the number dropped."""
    overlap = {p.chunk: p.overlap for p in parts}
    kept: list[Mention] = []
    dropped = 0
    for m in mentions:
        ov = overlap.get(m.chunk, frozenset())
        if m.chunk > 1 and m.paras and set(m.paras) <= ov:
            twin = next((k for k in kept if k.chunk == m.chunk - 1 and k.stance == m.stance
                         and norm_surface(k.surface) == norm_surface(m.surface) and set(k.paras) & set(m.paras)),
                        None)
            if twin is not None:
                twin.paras = sorted(set(twin.paras) | set(m.paras))[:MAX_PARAS]
                dropped += 1
                continue
        kept.append(m)
    return kept, dropped


def assign_ids(mentions: list[Mention], part_order: Mapping[str, int]) -> list[Mention]:
    """``mentions`` sorted in text order and numbered ``m1..`` (module docstring)."""
    ordered = sorted(mentions, key=lambda m: (m.sort_at, part_order[m.part], m.position))
    for i, m in enumerate(ordered, start=1):
        m.id = f"m{i}"
    return ordered


# ── S ────────────────────────────────────────────────────────────────────────────────────────


def mention_line(m: Mention) -> str:
    """One line of S's mention list (DESIGN §3.4 shape)."""
    parts = [m.id, f'"{m.surface}"', f"gloss: {m.gloss or 'none'}", f"stance: {m.stance}",
             f"implicit: {'yes' if m.implicit else 'no'}", f"section: {m.section or 'unknown'}"]
    if m.paras and m.snippet:
        parts.append(f'[p{m.paras[0]}] "{m.snippet}"')
    return " | ".join(parts)


def select_fills(opening: str, batch: Sequence[Mention], note: str) -> dict[str, str]:
    """The selection prompt's user-part fills for one batch."""
    return {"opening": opening or "(empty)", "mention_count": str(len(batch)), "batch_note": note,
            "mentions": "\n".join(mention_line(m) for m in batch)}


def batch_note(k: int, n: int, *, reask: bool = False) -> str:
    """The ``batch_note`` fill: empty for the first of one batch; later batches and re-asks leave
    ``doc_level`` empty (the prompt's §2 says when the message says so)."""
    if reask:
        return "; a re-ask of mentions the previous answer left out: leave doc_level empty"
    if k == 1:
        return "" if n == 1 else f"; batch 1 of {n}"
    return f"; batch {k} of {n}: leave doc_level empty"


def batches(mentions: Sequence[Mention]) -> list[list[Mention]]:
    return [list(mentions[i:i + SELECT_BATCH]) for i in range(0, len(mentions), SELECT_BATCH)]


def _null_if_blank(v: Any, cap: int) -> str | None:
    if not isinstance(v, str):
        return None
    v = one_line(v)[:cap]
    return v or None


def apply_selection(parsed: Mapping[str, Any], asked: Mapping[str, Mention], valid: frozenset[str],
                    repairs: list[str], *, read_doc_level: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Record S's answer on the asked mentions (module docstring); returns ``(doc_level, missing ids)``."""
    seen: set[str] = set()
    for raw in parsed.get("mentions") or []:
        if not isinstance(raw, Mapping):
            repairs.append("bad_selection")
            continue
        mid = str(raw.get("id") or "")
        if mid not in asked:
            repairs.append(f"unknown_mention_id:{mid[:20]}")
            continue
        if mid in seen:
            repairs.append(f"duplicate_mention_id:{mid}")
            continue
        seen.add(mid)
        m = asked[mid]
        m.answered = True
        cands: list[tuple[str, str]] = []
        for c in raw.get("candidates") or []:
            if not isinstance(c, Mapping):
                continue
            slug, conf = str(c.get("slug") or "").strip(), c.get("confidence")
            if slug not in valid:
                repairs.append(f"unknown_slug:{mid}:{slug[:60]}")
                continue
            if any(s == slug for s, _ in cands):
                continue
            cands.append((slug, conf if conf in CONFIDENCES else "low"))
        if len(cands) > MAX_CANDIDATES:
            repairs.append(f"candidates_cut:{mid}")
            cands = cands[:MAX_CANDIDATES]
        m.candidates = cands
        none = raw.get("none")
        if isinstance(none, Mapping):
            if cands:
                repairs.append(f"none_with_candidates:{mid}")
            else:
                reason = none.get("reason")
                m.none_reason = reason if reason in NONE_REASONS else None
                if m.none_reason == "off_taxonomy":
                    m.proposed_name = _null_if_blank(none.get("proposed_name"), PROPOSED_NAME_CHARS)
                    m.proposed_gloss = _null_if_blank(none.get("proposed_gloss"), PROPOSED_GLOSS_CHARS)
    doc_level: list[dict[str, Any]] = []
    if read_doc_level:
        for d in parsed.get("doc_level") or []:
            if not isinstance(d, Mapping):
                continue
            slug = str(d.get("slug") or "").strip()
            if slug not in valid:
                repairs.append(f"unknown_slug:doc_level:{slug[:60]}")
                continue
            if any(x["slug"] == slug for x in doc_level):
                continue
            conf = d.get("confidence")
            doc_level.append({"slug": slug, "confidence": conf if conf in CONFIDENCES else "low",
                              "why": _null_if_blank(d.get("why"), WHY_CHARS)})
        if len(doc_level) > MAX_DOC_LEVEL:
            repairs.append("doc_level_cut")
            doc_level = doc_level[:MAX_DOC_LEVEL]
    missing = [mid for mid in asked if mid not in seen]
    return doc_level, missing


# ── sweep reading notes ──────────────────────────────────────────────────────────────────────


def note_line(m: Mention) -> str:
    """``[<section>, pN] "surface" - gloss (stance hint: x): "snippet"`` (DESIGN §3.7)."""
    where = f"[{m.section}, p{m.paras[0]}]" if m.paras else "[section unknown]"
    tail = f': "{m.snippet}"' if m.snippet else ""
    return f'{where} "{m.surface}" - {m.gloss or "no gloss"} (stance hint: {m.stance}){tail}'


def reading_notes(mentions: Sequence[Mention]) -> str:
    """The sweep call's ``reading_notes`` fill: one line per note, :data:`NO_NOTES` when empty (P5)."""
    return "\n".join(note_line(m) for m in mentions) if mentions else NO_NOTES


# ── the sub-menu ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Submenu:
    slugs: tuple[str, ...]
    pointers: dict[str, list[dict[str, Any]]]
    classes: dict[str, str]  # every candidate slug (dropped ones too) -> its class
    dropped: tuple[str, ...]


def _class_of(slug: str, s_conf: Mapping[str, str], doc_level: frozenset[str], lex: Mapping[str, Any]) -> str:
    if slug in doc_level or s_conf.get(slug) == "high":
        return "protected"
    if slug in s_conf:
        return s_conf[slug]
    return "lexical" if int(lex[slug]["hits_body"]) >= 2 else "lexical_one"


def assemble_submenu(mentions: Sequence[Mention], doc_level: Sequence[Mapping[str, Any]],
                     lex: Mapping[str, Mapping[str, Any]], section_of: Mapping[str, str],
                     repairs: list[str]) -> Submenu:
    """The graded candidate list (module docstring). ``lex`` = the lexical matcher's hits with
    ``hits_body >= 1`` (slug -> hit); ``section_of`` = paragraph id -> section label."""
    best = {c: i for i, c in enumerate(CONFIDENCES)}
    s_conf: dict[str, str] = {}
    seen: dict[str, int] = {}
    for d in doc_level:
        seen.setdefault(d["slug"], len(seen))
    for m in mentions:
        for slug, conf in m.candidates:
            if slug not in s_conf or best[conf] < best[s_conf[slug]]:
                s_conf[slug] = conf
            seen.setdefault(slug, len(seen))
    for slug in sorted(lex, key=lambda s: (_pnum(lex[s].get("first_para")), s)):
        seen.setdefault(slug, len(seen))
    dl = frozenset(d["slug"] for d in doc_level)
    classes = {s: _class_of(s, s_conf, dl, lex) for s in seen}
    ranked = sorted(seen, key=lambda s: (CLASS_RANK[classes[s]], seen[s], s))
    dropped: list[str] = []
    protected = sum(classes[s] == "protected" for s in ranked)
    if len(ranked) > SUBMENU_CAP:
        for cls in ("lexical_one", "low", "lexical", "med"):
            for s in reversed([x for x in ranked if classes[x] == cls]):
                if len(ranked) <= SUBMENU_CAP:
                    break
                ranked.remove(s)
                dropped.append(s)
                repairs.append(f"dropped_by_cap:{cls}:{s}")
        if protected > SUBMENU_CAP:
            repairs.append(f"submenu_over_cap:{protected}")
    pointers: dict[str, list[dict[str, Any]]] = {}
    why = {d["slug"]: d.get("why") for d in doc_level}
    for s in ranked:
        ps: list[dict[str, Any]] = []
        hits = [m for m in mentions if any(c == s for c, _ in m.candidates)]
        picked: list[Mention] = []
        stances: set[str] = set()
        for m in hits:  # distinct stance hints first, then text order
            if m.stance not in stances and len(picked) < MAX_POINTERS:
                picked.append(m)
                stances.add(m.stance)
        for m in hits:
            if len(picked) >= MAX_POINTERS:
                break
            if m not in picked:
                picked.append(m)
        picked.sort(key=lambda m: hits.index(m))
        for m in picked:
            ps.append({"source": f"p{m.paras[0]}" if m.paras else None, "section": m.section,
                       "surface": m.surface, "snippet": m.snippet, "stance": m.stance})
        if len(hits) > len(picked):
            ps.append({"note": f"(+{len(hits) - len(picked)} more mentions)"})
        if s in why:
            ps.append({"note": f"document-level candidate (from the opening): {why[s] or 'no reason given'}"})
        hit = lex.get(s)
        if hit is not None:
            if not hits:
                fp = hit.get("first_para")
                ps.append({"source": fp, "section": section_of.get(fp or ""), "surface": (hit.get("surfaces") or [None])[0],
                           "snippet": hit.get("snippet"), "stance": None})
            n = int(hit["hits_body"])
            ps.append({"note": f"name or aliases occur {n} time{'s' if n != 1 else ''} in the body"})
        pointers[s] = ps
    return Submenu(slugs=tuple(ranked), pointers=pointers, classes=classes, dropped=tuple(dropped))


def inventory_accounting(parsed: Mapping[str, Any], valid: frozenset[str], kept: frozenset[str],
                         repairs: list[str]) -> dict[str, int]:
    """The inventory contract's counts (module docstring); ``kept`` = the slugs of the kept rows. Pure."""
    sweeps = parsed.get("sweeps")
    sweeps = sweeps if isinstance(sweeps, Mapping) else {}
    passages = sweeps.get("passages")
    passages = passages if isinstance(passages, list) else []
    in_passages: set[str] = set()
    for line in passages:
        for slug in (line.get("slugs") if isinstance(line, Mapping) else None) or []:
            if isinstance(slug, str) and slug.strip() in valid:
                in_passages.add(slug.strip())
    listed = set(in_passages)
    for name in SWEEPS_WITH_TECHNIQUE:
        entries = sweeps.get(name)
        for e in entries if isinstance(entries, list) else []:
            slug = e.get("technique") if isinstance(e, Mapping) else None
            if isinstance(slug, str) and slug.strip() in valid:
                listed.add(slug.strip())
    not_graded: set[str] = set()
    raw = parsed.get("not_graded")
    for e in raw if isinstance(raw, list) else []:
        slug = e.get("slug") if isinstance(e, Mapping) else None
        if not isinstance(slug, str) or slug.strip() not in valid:
            repairs.append("bad_not_graded")
            continue
        slug = slug.strip()
        if str(e.get("why") or "").strip() not in NOT_GRADED_REASONS:
            repairs.append(f"not_graded_reason_unknown:{slug}")
        not_graded.add(slug)
    with_row = sorted(not_graded & kept)
    repairs.extend(f"not_graded_with_row:{s}" for s in with_row)
    return {"inv_passages": len(passages), "inv_passage_slugs": len(in_passages), "inv_not_graded": len(not_graded),
            "inv_unaccounted": len(listed - kept - not_graded), "inv_not_graded_with_row": len(with_row)}


def _pnum(pid: Any) -> int:
    return int(pid[1:]) if isinstance(pid, str) and pid[1:].isdigit() else 10**9


__all__ = ["CLASS_RANK", "CONFIDENCES", "GLOSS_WORDS", "MAX_CANDIDATES", "MAX_DOC_LEVEL", "MAX_PARAS",
           "MAX_POINTERS", "MENTION_CAP", "NONE_REASONS", "NOT_GRADED_REASONS", "NO_CANDIDATES", "NO_NOTES",
           "OPENING_HEADER", "REJECT_REASONS", "SWEEPS_WITH_TECHNIQUE", "SELECT_BATCH", "SNIPPET_CHARS", "STANCES", "SUBMENU_CAP", "SURFACE_CHARS", "Mention",
           "Part", "Submenu", "apply_selection", "assemble_submenu", "assign_ids", "batch_note", "batches",
           "clean_mentions", "dedupe_overlap", "grading_schema", "inventory_accounting", "inventory_schema", "mention_line", "norm_surface", "note_line",
           "parts_of", "prepass_fills", "prepass_schema", "reading_notes", "render_part", "select_fills",
           "select_schema", "snippet", "split_part"]
