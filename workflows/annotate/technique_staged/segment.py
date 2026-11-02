"""The segmenter of ``annotate.technique_staged`` (no LLM; :data:`SEGMENTER_VERSION`).

Turns a document's stored text into numbered paragraphs (``p1..pN``, character offsets into the
text), section labels, a references span, and the chunks the menu-free pre-pass reads. Pure and
deterministic: the same text gives the same paragraphs, chunks and rendered bytes, so the
pre-pass requests (and their response-cache keys) depend on the text digest alone.

Steps:

1. **Cap.** Only the first :data:`DOC_CHAR_CAP` characters are read (``truncated``, ``read_chars``;
   :func:`segment`'s ``cap`` sets another limit).
2. **Headings**, line by line (never inside a ``` / ``~~~`` fence or on a table row): markdown
   ``#``..``######`` (level = number of ``#``); numbered ``3`` / ``3.2`` / ``A.1`` / ``IV.`` + a
   capitalised title of <= 12 words without terminal punctuation, only as the first line of a
   blank-line block and never when the line looks like a reference (level = depth, roman 1);
   keyword lines (:data:`KEYWORDS`, optionally numbered, bold or with a colon; ``Appendix ...`` /
   ``Supplementary ...`` lines too; level = the markdown or numbered level, else 1).
3. **Paragraphs** = blank-line-separated blocks; a heading line is a paragraph of its own; a block
   longer than :data:`MAX_PARA_CHARS` is split at line breaks, a still longer line at sentence ends,
   a still longer sentence at whitespace (greedy packing up to the limit).
4. **Sections.** Each paragraph carries the label of the last heading at or before it (the
   heading's text, markup stripped, numbering kept, <= 80 characters); before any heading,
   ``front``.
5. **References** (paragraph kind ``references``; all others ``body``). From a heading whose
   keyword is references / bibliography / works cited / literature cited, to the next heading that
   is appendix-like (``Appendix``, ``Supplementary``, ``A.1``-style) or of the same or a higher
   level. Without such a heading, a fallback: in the last 40% of the text, the first run of
   :data:`REF_RUN` consecutive non-empty lines of which at least half look like references (a year
   in parentheses, ``et al.``, ``arXiv:``, ``doi:``, a URL; and at most :data:`REF_LINE_MAX`
   characters, since a longer line is a prose paragraph citing work) starts the span at its first
   reference-like line (a paragraph is split there) and runs to the next heading or the end.
6. **Chunks** of body paragraphs only (references never reach the pre-pass): one chunk if the body
   is <= :data:`SINGLE_CHUNK_MAX` characters; else packed in order to :data:`CHUNK_TARGET`, never
   splitting a paragraph, a remainder that fits under :data:`CHUNK_MAX` (with its overlap) forming
   the last chunk rather than two (never for the first chunk: a body over the single-chunk limit
   always gets at least two),
   and every later chunk starting with the previous chunk's last paragraph(s) (up to
   :data:`OVERLAP_CHARS`, at least one paragraph) marked as overlap. Character counts here are
   paragraph text lengths (markers and separators excluded).

:func:`render_chunk` gives the text the pre-pass sees (``[p17] `` before each paragraph);
:func:`opening` the document opening (first :data:`OPENING_CHARS` of the body), which the prompt
shows before every chunk after the first. :meth:`Segmentation.summary` is the JSON-able record for
the payload and debugging.

Imports nothing from ``stack`` or other workflows; loaded by path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SEGMENTER_VERSION = "seg-v1"

DOC_CHAR_CAP = 300_000
MAX_PARA_CHARS = 2_000
SINGLE_CHUNK_MAX = 40_000
CHUNK_TARGET = 30_000
CHUNK_MAX = 45_000
OVERLAP_CHARS = 1_000
OPENING_CHARS = 1_500
LABEL_CHARS = 80
REF_TAIL_FRACTION = 0.6  # the fallback looks only at lines starting after 60% of the text
REF_RUN = 10
REF_LINE_MAX = 500  # a longer line is prose with citations, not a reference entry (fallback only)

KEYWORDS: frozenset[str] = frozenset({
    "abstract", "introduction", "background", "related work", "method", "methods", "methodology",
    "approach", "experiments", "experimental setup", "evaluation", "results", "discussion",
    "limitations", "conclusion", "conclusions", "future work", "acknowledgements", "acknowledgments",
    "appendix", "appendices", "references", "bibliography", "works cited", "literature cited",
})
REFERENCE_KEYWORDS: frozenset[str] = frozenset({"references", "bibliography", "works cited", "literature cited"})

_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_NUMBERED = re.compile(
    r"^\s*(?:(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?|(?P<let>[A-Z](?:\.\d{1,2}){1,3})\.?|(?P<rom>[IVX]{1,5})\.)"
    r"\s+(?P<title>\S.*?)\s*$")
_NUM_PREFIX = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2}){0,3}\.?|[ivx]{1,5}\.|[a-z](?:\.\d{1,2})+\.?)\s+")
_APPENDIX_LIKE = re.compile(r"^(?:appendix|appendices|supplementary)\b|^[A-Z](?:\.\d{1,2})*\.?\s+\S")
_APPENDIX_LINE = re.compile(r"^(?:appendix|appendices|supplementary)\b", re.IGNORECASE)
_REF_SIGNAL = re.compile(
    r"\((?:19|20)\d{2}[a-z]?\)|\bet\s+al\b|\barxiv:\s*\d|\bdoi:\s*\S|doi\.org/|https?://", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_EMPHASIS = re.compile(r"^[*_\s]+|[*_\s]+$")


# ── data ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Paragraph:
    """One numbered paragraph: ``text == source[start:end]``."""

    pid: str
    index: int
    start: int
    end: int
    text: str
    section: str
    kind: str  # "body" | "references"
    heading_level: int | None = None  # set iff the paragraph is a heading line

    @property
    def is_heading(self) -> bool:
        return self.heading_level is not None

    @property
    def chars(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Chunk:
    """One pre-pass input: body paragraphs in order, the first ``overlap`` repeated from the previous chunk."""

    index: int  # 1-based
    total: int
    paragraphs: tuple[Paragraph, ...]
    overlap: int = 0

    @property
    def pids(self) -> tuple[str, ...]:
        return tuple(p.pid for p in self.paragraphs)

    @property
    def overlap_pids(self) -> tuple[str, ...]:
        return tuple(p.pid for p in self.paragraphs[: self.overlap])

    @property
    def chars(self) -> int:
        return sum(p.chars for p in self.paragraphs)

    @property
    def start(self) -> int:
        return self.paragraphs[0].start

    @property
    def end(self) -> int:
        return self.paragraphs[-1].end

    @property
    def section_labels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.section for p in self.paragraphs))


@dataclass(frozen=True)
class Segmentation:
    doc_chars: int
    read_chars: int
    truncated: bool
    paragraphs: tuple[Paragraph, ...]
    chunks: tuple[Chunk, ...]
    references_method: str  # "heading" | "fallback" | "none"
    opening: str

    @property
    def body_chars(self) -> int:
        return sum(p.chars for p in self.paragraphs if p.kind == "body")

    @property
    def reference_paragraphs(self) -> tuple[Paragraph, ...]:
        return tuple(p for p in self.paragraphs if p.kind == "references")

    def summary(self) -> dict[str, Any]:
        """JSON-able record of the segmentation (no paragraph texts)."""
        refs = self.reference_paragraphs
        sections: list[dict[str, Any]] = []
        for p in self.paragraphs:
            if not sections or sections[-1]["label"] != p.section or sections[-1]["kind"] != p.kind:
                sections.append({"label": p.section, "kind": p.kind, "first_pid": p.pid, "start": p.start})
        return {
            "segmenter_version": SEGMENTER_VERSION,
            "doc_chars": self.doc_chars,
            "read_chars": self.read_chars,
            "truncated": self.truncated,
            "n_paragraphs": len(self.paragraphs),
            "body_chars": self.body_chars,
            "references": {"method": self.references_method, "paragraphs": len(refs),
                           "chars": sum(p.chars for p in refs),
                           "first_pid": refs[0].pid if refs else None},
            "sections": sections,
            "chunks": [{"index": c.index, "total": c.total, "first_pid": c.pids[0], "last_pid": c.pids[-1],
                        "n_paragraphs": len(c.paragraphs), "overlap_pids": list(c.overlap_pids),
                        "chars": c.chars, "start": c.start, "end": c.end,
                        "section_labels": list(c.section_labels)} for c in self.chunks],
        }


# ── lines and headings ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Line:
    start: int
    end: int  # exclusive, without the newline
    text: str
    first_in_block: bool
    heading_level: int | None
    label: str | None


def _clean_label(s: str) -> str:
    s = " ".join(_EMPHASIS.sub("", s).split())
    return s[:LABEL_CHARS]


def _keyword(label: str) -> str:
    """The label as a keyword candidate: lowercase, numbering and a trailing ``:``/``.`` removed."""
    k = _NUM_PREFIX.sub("", label.lower()).strip().rstrip(":.").strip()
    return k


def looks_like_reference(line: str) -> bool:
    return _REF_SIGNAL.search(line) is not None


def _heading(line: str, first_in_block: bool) -> tuple[int, str] | None:
    """``(level, label)`` if the line is a heading (module docstring, step 2), else None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("|") or len(stripped) > 200:
        return None
    md = _MD_HEADING.match(line)
    if md:
        label = _clean_label(md.group(2))
        return (len(md.group(1)), label) if label else None
    plain = _clean_label(stripped)
    num = _NUMBERED.match(plain)
    kw = _keyword(plain)
    if kw in KEYWORDS or (_APPENDIX_LINE.match(plain) and len(plain) <= LABEL_CHARS and not plain.endswith(".")):
        level = 1
        if num and num.group("num"):
            level = num.group("num").count(".") + 1
        return level, plain
    if num and first_in_block:
        title = num.group("title")
        if (len(plain) <= 100 and title[0].isupper() and len(title.split()) <= 12
                and title[-1] not in ".,;:!?" and not looks_like_reference(plain)):
            if num.group("num"):
                return num.group("num").count(".") + 1, plain
            if num.group("let"):
                return num.group("let").count(".") + 1, plain
            return 1, plain
    return None


def _lines(text: str) -> list[_Line]:
    out: list[_Line] = []
    in_fence = False
    prev_blank = True
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        raw = text[pos:end]
        blank = not raw.strip()
        if not blank:
            fence = _FENCE.match(raw) is not None
            head = None if (in_fence or fence) else _heading(raw, prev_blank)
            out.append(_Line(pos, end, raw, prev_blank, head[0] if head else None, head[1] if head else None))
            if fence:
                in_fence = not in_fence
        prev_blank = blank
        pos = end + 1
    return out


def _is_reference_heading(line: _Line) -> bool:
    return line.label is not None and _keyword(line.label) in REFERENCE_KEYWORDS


def _is_appendix_like(label: str) -> bool:
    return _APPENDIX_LIKE.match(label) is not None or _APPENDIX_LIKE.match(label.lower()) is not None


def _fallback_reference_start(lines: list[_Line], read_chars: int) -> int | None:
    """Offset of the first reference-like line of the first qualifying run in the tail, or None."""
    tail = [ln for ln in lines if ln.start >= REF_TAIL_FRACTION * read_chars]
    flags = [len(ln.text) <= REF_LINE_MAX and looks_like_reference(ln.text) for ln in tail]
    for i in range(len(tail) - REF_RUN + 1):
        window = flags[i:i + REF_RUN]
        if sum(window) * 2 >= REF_RUN:
            return tail[i + window.index(True)].start
    return None


# ── paragraphs ───────────────────────────────────────────────────────────────────────────────


def _trim(text: str, s: int, e: int) -> tuple[int, int] | None:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return (s, e) if e > s else None


def _pack(spans: list[tuple[int, int]], limit: int) -> list[tuple[int, int]]:
    """Merge consecutive contiguous spans greedily while the merged span stays <= ``limit``."""
    out: list[tuple[int, int]] = []
    for s, e in spans:
        if out and e - out[-1][0] <= limit:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _hard_split(text: str, s: int, e: int, limit: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    while e - s > limit:
        cut = text.rfind(" ", s + limit // 2, s + limit)
        cut = cut if cut > s else s + limit
        out.append((s, cut))
        s = cut
    out.append((s, e))
    return out


def _sentence_spans(text: str, s: int, e: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cur = s
    for m in _SENTENCE_END.finditer(text, s, e):
        spans.append((cur, m.end()))
        cur = m.end()
    if cur < e:
        spans.append((cur, e))
    return spans


def _split_long(text: str, s: int, e: int, line_starts: list[int]) -> list[tuple[int, int]]:
    """Split ``text[s:e]`` into pieces <= :data:`MAX_PARA_CHARS` (lines, then sentences, then words)."""
    if e - s <= MAX_PARA_CHARS:
        return [(s, e)]
    cuts = [x for x in line_starts if s < x < e]
    line_spans = list(zip([s, *cuts], [*cuts, e]))
    out: list[tuple[int, int]] = []
    for ls, le in _pack(line_spans, MAX_PARA_CHARS):
        if le - ls <= MAX_PARA_CHARS:
            out.append((ls, le))
            continue
        for ss, se in _pack(_sentence_spans(text, ls, le), MAX_PARA_CHARS):
            out.extend(_hard_split(text, ss, se, MAX_PARA_CHARS) if se - ss > MAX_PARA_CHARS else [(ss, se)])
    return out


def _paragraph_spans(text: str, lines: list[_Line], forced_break: int | None) -> list[tuple[int, int, _Line | None]]:
    """``(start, end, heading_line_or_None)`` for every paragraph, in order."""
    blocks: list[list[_Line]] = []
    for ln in lines:
        if ln.first_in_block or not blocks or ln.heading_level is not None or ln.start == forced_break:
            blocks.append([ln])
        elif blocks[-1][-1].heading_level is not None:
            blocks.append([ln])  # the line after a heading line starts a new paragraph
        else:
            blocks[-1].append(ln)
    out: list[tuple[int, int, _Line | None]] = []
    for block in blocks:
        if block[0].heading_level is not None:
            span = _trim(text, block[0].start, block[0].end)
            if span:
                out.append((span[0], span[1], block[0]))
            continue
        starts = [ln.start for ln in block]
        for s, e in _split_long(text, block[0].start, block[-1].end, starts):
            span = _trim(text, s, e)
            if span:
                out.append((span[0], span[1], None))
    return out


# ── the segmenter ────────────────────────────────────────────────────────────────────────────


def _chunks(body: list[Paragraph], *, single_max: int = SINGLE_CHUNK_MAX, target: int = CHUNK_TARGET,
            cap: int = CHUNK_MAX, overlap_chars: int = OVERLAP_CHARS) -> tuple[Chunk, ...]:
    """Step 6 (module docstring); the defaults are P's chunks, :func:`windows` passes other sizes."""
    if not body:
        return ()
    total_chars = sum(p.chars for p in body)
    if total_chars <= single_max:
        return (Chunk(index=1, total=1, paragraphs=tuple(body)),)
    groups: list[tuple[list[Paragraph], int]] = []  # (paragraphs, overlap count)
    i = 0
    overlap: list[Paragraph] = []
    while i < len(body):
        cur = list(overlap)
        size = sum(p.chars for p in cur)
        rest = sum(p.chars for p in body[i:])
        if groups and size + rest <= cap:  # a body over single_max always gets >= 2 chunks
            groups.append((cur + body[i:], len(overlap)))
            break
        new = 0
        while i < len(body) and (new == 0 or size + body[i].chars <= target):
            cur.append(body[i])
            size += body[i].chars
            i += 1
            new += 1
        if i < len(body) and new > 1 and cur[-1].is_heading:
            cur.pop()  # a heading opens the next chunk rather than closing this one
            i -= 1
        groups.append((cur, len(overlap)))
        # the next chunk repeats the last paragraph, plus earlier ones while the total stays <= overlap_chars
        overlap = [cur[-1]]
        acc = cur[-1].chars
        for p in reversed(cur[:-1]):
            if acc + p.chars > overlap_chars:
                break
            overlap.insert(0, p)
            acc += p.chars
    total = len(groups)
    return tuple(Chunk(index=k + 1, total=total, paragraphs=tuple(ps), overlap=ov)
                 for k, (ps, ov) in enumerate(groups))


def segment(text: str, *, cap: int = DOC_CHAR_CAP) -> Segmentation:
    """Paragraphs, sections, references and chunks of ``text`` (module docstring); ``cap`` replaces
    :data:`DOC_CHAR_CAP` for the windowed single call under the pin key ``doc_cap`` (the default keeps
    ``seg-v1``'s output byte for byte)."""
    doc_chars = len(text)
    src = text[:cap]
    lines = _lines(src)
    has_ref_heading = any(_is_reference_heading(ln) for ln in lines)
    fallback_start = None if has_ref_heading else _fallback_reference_start(lines, len(src))
    spans = _paragraph_spans(src, lines, fallback_start)

    paragraphs: list[Paragraph] = []
    label = "front"
    in_refs = False
    refs_level = 1
    fallback_started = False
    method = "heading" if has_ref_heading else ("fallback" if fallback_start is not None else "none")
    for idx, (s, e, head) in enumerate(spans, start=1):
        level = head.heading_level if head is not None else None
        if head is not None and head.label is not None:
            # a heading ends a references span when appendix-like or of the same or a higher level
            # (fallback spans: any heading)
            if in_refs and (fallback_started or _is_appendix_like(head.label) or (level or 1) <= refs_level):
                in_refs = False
            if _is_reference_heading(head):
                in_refs, refs_level = True, level or 1
            label = head.label
        if fallback_start is not None and not fallback_started and s >= fallback_start:
            in_refs = fallback_started = True
        paragraphs.append(Paragraph(pid=f"p{idx}", index=idx, start=s, end=e, text=src[s:e], section=label,
                                    kind="references" if in_refs else "body", heading_level=level))

    body = [p for p in paragraphs if p.kind == "body"]
    opening_text = "\n\n".join(p.text for p in body)[:OPENING_CHARS]
    return Segmentation(doc_chars=doc_chars, read_chars=len(src), truncated=doc_chars > cap,
                        paragraphs=tuple(paragraphs), chunks=_chunks(body), references_method=method,
                        opening=opening_text)


def windows(seg: Segmentation, *, single_max: int, target: int, cap: int, overlap_chars: int = 0) -> tuple[Chunk, ...]:
    """The body paragraphs of ``seg`` packed like step 6 with other sizes (the windowed single call, D614);
    ``overlap_chars=0`` repeats exactly one paragraph. P's chunks (``seg.chunks``) are unaffected."""
    body = [p for p in seg.paragraphs if p.kind == "body"]
    return _chunks(body, single_max=single_max, target=target, cap=cap, overlap_chars=overlap_chars)


def outline(seg: Segmentation) -> str:
    """Every body heading, one per line, indented two spaces per level below 1 (the windowed call's outline)."""
    return "\n".join("  " * (max(p.heading_level or 1, 1) - 1) + p.section
                     for p in seg.paragraphs if p.kind == "body" and p.is_heading)


def plain_chunk(chunk: Chunk) -> str:
    """The chunk's paragraph texts joined by blank lines, without ``[pN]`` markers (quotes stay verbatim)."""
    return "\n\n".join(p.text for p in chunk.paragraphs)


def render_chunk(chunk: Chunk) -> str:
    """The chunk as the pre-pass sees it: ``[p17] <paragraph text>`` per paragraph, blank-line separated."""
    return "\n\n".join(f"[{p.pid}] {p.text}" for p in chunk.paragraphs)


def opening(text: str) -> str:
    """The first :data:`OPENING_CHARS` characters of the body (paragraph texts joined by blank lines)."""
    return segment(text).opening


__all__ = ["CHUNK_MAX", "CHUNK_TARGET", "DOC_CHAR_CAP", "KEYWORDS", "MAX_PARA_CHARS", "OPENING_CHARS",
           "OVERLAP_CHARS", "REFERENCE_KEYWORDS", "SEGMENTER_VERSION", "SINGLE_CHUNK_MAX", "Chunk", "Paragraph",
           "Segmentation", "looks_like_reference", "opening", "outline", "plain_chunk", "render_chunk", "segment",
           "windows"]
