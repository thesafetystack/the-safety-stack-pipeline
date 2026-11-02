"""The convert-phase conversion ladder — raw bytes → canonical Markdown + a mechanical
``text_quality`` verdict, with the v1 hard-won guards ported (10-design §5.3; 20-technical §11).

This module is **pure conversion logic**, deliberately free of any ``stack.*`` dependency: it is
imported by the memory-capped subprocess worker (``worker.py``) and unit-tested directly (the
heavy tiers monkeypatched, so the tests never import torch/docling). The orchestration — scope,
the attempt-before-invoke charge, the convert configs and their pin — is ``acquire``'s
``convert`` substage (``workflows/acquire/convert.py``).

The PDF tier ladder (ported from v1 ``pipeline/stages/lib/pdf_to_markdown.py``):

* **tier 1 — docling** (layout-aware; torch): IMPORT-GUARDED and optional. Applies two v1 gates:
  the **silent-truncation gate** (docling page errors on more than ``max(2, 10%)`` of pages ⇒
  reject the plausible-but-incomplete docling output in favour of the complete fallback — the
  2026-07-16 incident) and the **OCR quality gate** (OCR is off on the primary pass and re-run
  only when the no-OCR yield is < ``OCR_MIN_CHARS_PER_PAGE`` per page — the born-digital case
  never pays the per-page tesseract cost).
* **tier 2 — pymupdf4llm** (pure-Python markdown): fires when docling is absent, raises, or is
  rejected.
* **tier 3 — pymupdf raw text** (``fitz``): last resort when tier 2 is near-empty.

Markup takes its own paths. **JATS XML, and publisher HTML that ``fetch.oa`` acquired, go to
fetchpdf's converter first** (``fetchpdf.retrieval.to_markdown``): prose becomes Markdown and
every table is emitted as inline HTML — a ``<th colspan="2">`` header cannot be expressed in a
pipe table, and flattening it shifts every value to its right by a column, which is exactly the
corruption the tier ladder paid to avoid. Inline HTML blocks are CommonMark, so the canonical
text stays Markdown (20-technical §11). arXiv's LaTeXML pages stay on docling's HTML backend,
which carries the equation-table and MathML fixes below; other HTML (blogs, forums) takes
docling's backend, then the light path (``trafilatura`` / ``readability`` if importable, else a
stdlib-only minimal HTML→text extractor — always available, so HTML never depends on the
``convert`` group). Anything else decodes as plain text.

**Charsets** (the Chinese-coverage work): HTML and plain text are decoded by :func:`decode_text`: a byte-order mark, else the
declared charset (a ``charset`` parameter of the media type, the XML prolog, ``<meta charset>`` or
``http-equiv``) with GB2312/GBK read as GB18030 and the other WHATWG supersets (:data:`CHARSET_ALIASES`), else
strict UTF-8, else charset-normalizer's guess when importable, else UTF-8 with replacement characters (until
2026-10 the only decoding). HTML is re-encoded as UTF-8 before any backend sees it (:func:`utf8_markup`).
Snapshots stamp the media type without its parameters (``stack/mediatype.py``), so at convert time the HTTP
header's charset is not known; a page that declares its charset only in the header and is not valid UTF-8 is
decoded by detection. The convert pin does not cover this code: texts converted before keep their decoding until
they are converted again.

**LaTeXML without docling** (round 5, T-RUN B8): when the caller passes ``latexml=True`` (the convert
request of a config with ``options.structured``, so the pin says so; no environment switch), a LaTeXML
page takes :func:`latexml_to_text`
first: :func:`rewrite_mathml` (``$…$``/``$$…$$`` TeX), :func:`prepare_latexml` (LaTeXML's equation tables
become paragraphs, its ``<figure>`` floats plain blocks so fetchpdf keeps their tables and captions,
its ``ltx_ERROR`` spans, undefined-macro names, are dropped), then fetchpdf's HTML converter
(headings, inline-HTML tables); docling's backend and the light path stay the fallbacks. Without the flag nothing changes. Measured on three arXiv pages (2026-10-13):
the fetchpdf path keeps 53-372 inline and 2-85 display formulas and all 9, 21 and 8 data tables (as
inline HTML, with captions), where ``trafilatura`` (the ``pymupdf4llm-v1`` route before) kept no
formula, flattened tables to pipe rows and lost the section headings.

``text_quality`` is a MECHANICAL verdict from body length, replacement-char density, a binary
sniff, and the truncation signal — never optimistic (10-design §5.3). Thresholds are named
constants carrying v1 values.
"""

from __future__ import annotations

import codecs
import html
import importlib.metadata
import importlib.util
import os
import re
import unicodedata
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

# ─────────────────────────────────────────────────────────────────────────────
# Named thresholds (v1 values — 10-design §5.3, 20-technical §11)
# ─────────────────────────────────────────────────────────────────────────────

# Min chars of extracted markdown per page below which the no-OCR docling pass is treated as low
# quality (a scanned/image-only PDF) and retried WITH OCR. Born-digital papers yield thousands of
# chars/page; scans yield ~0. (v1 OCR_MIN_CHARS_PER_PAGE.)
OCR_MIN_CHARS_PER_PAGE = 100

# A fallback tier's output must clear this to be usable at all (v1: pymupdf/rawtext need ≥ 50).
FALLBACK_MIN_CHARS = 50

# docling output below this is treated as empty/trivial → reject, fall through (v1 value).
DOCLING_MIN_CHARS = 20

# text_quality yield thresholds (mechanical). `complete` needs a real body; below the stub cap the
# text is effectively empty (nav/boilerplate). "fulltext min 500 chars" per the phase contract.
FULLTEXT_MIN_CHARS = 500
STUB_EMPTY_MAX_CHARS = 200

# `corrupted` signals. A high U+FFFD (replacement char) density, or a high proportion of
# non-text control bytes (a raw-text extraction of a binary blob), means the conversion failed.
CORRUPTED_REPLACEMENT_DENSITY = 0.05
CORRUPTED_BINARY_DENSITY = 0.10

# Headings that can't be a paper title — a leading generic section means docling found no title.
_GENERIC_HEADINGS = {
    "abstract", "introduction", "contents", "references", "bibliography",
    "acknowledgements", "acknowledgments", "appendix",
}

# Separator joining a multi-level column header into one label ("Fine-pruning · ACC"). Chosen
# over "/" and "|": "/" occurs inside real header text (TPR/FPR) and "|" would need escaping.
HEADER_JOIN = " · "

# A cell whose text is mostly dot-leaders is a table-of-contents row, not data. Measured on the
# corpus: 2 of 8 random "degraded" tables were appendix ToCs (arxiv.2511.05682, .2303.16200).
_DOT_LEADER = re.compile(r"\.\s*\.\s*\.")
DOT_LEADER_REJECT_RATIO = 0.5

# LaTeXML lays *display equations* out as <table class="ltx_equation"> so it can place the
# equation number in a second cell. docling parses that as a table; without this check every
# numbered equation in an arXiv-HTML paper becomes a spurious one-row table.
_EQUATION_NUMBER = re.compile(r"^\(\s*\d+[a-z]?\s*\)$")
_TEX_MARKER = re.compile(r"\\[A-Za-z]{2,}|\\\[|\\\(|\^\{|_\{")


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────


class ConversionResult:
    """One item's conversion outcome. ``status='ok'`` ⇒ the text group is writable (``markdown``
    + a ``quality`` verdict + a ``converter`` stamp); ``status='failed'`` ⇒ nothing extractable
    (a retryable convert failure — the poison-item path when it recurs to the attempt cap).

    A plain class (not a ``@dataclass``), written when this module was loaded by
    ``spec_from_file_location`` without a ``sys.modules`` entry, which ``@dataclass`` + ``from
    __future__ import annotations`` cannot take; it is a registered import now
    (``stack/conversion/worker.py``: ``import ladder``)."""

    __slots__ = ("status", "markdown", "converter", "quality", "m_flags", "error", "error_class", "pages",
                 "truncated")

    def __init__(
        self,
        status: str,
        markdown: str = "",
        converter: str = "",
        quality: str = "",
        m_flags: list[str] | None = None,
        error: str | None = None,
        error_class: str | None = None,
        pages: int | None = None,
        truncated: bool | None = None,
    ) -> None:
        self.status = status  # "ok" | "failed"
        self.markdown = markdown
        self.converter = converter  # "<tool>@<version>+tier<k>" (I9 attribution)
        self.quality = quality  # a text_quality_verdict value (v3's enums.text_quality, retired with v3)
        self.m_flags = list(m_flags) if m_flags else []  # media-transform flags, e.g. ["ocr"] (v3's enums.media_transform, archived)
        self.error = error
        # The §2.6 permanent class of a ``failed`` result: ``too_short`` (every tier came back
        # near-empty: nothing extractable), ``parse_error`` (the ladder raised); the parent maps
        # a ``failed`` result without one to ``parse_error``.
        self.error_class = error_class
        # Facts for acquire's quality checks (round 3, Q): a PDF's page count (None when unknown or
        # not a PDF) and the truncation signal (docling's page-error gate; None when no tier gives one).
        self.pages = pages
        self.truncated = truncated

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "markdown": self.markdown,
            "converter": self.converter,
            "quality": self.quality,
            "m_flags": list(self.m_flags),
            "error": self.error,
            "error_class": self.error_class,
            "pages": self.pages,
            "truncated": self.truncated,
        }


def converter_stamp(tool: str, tier: int | str) -> str:
    """The I9 provenance stamp ``"<tool>@<version>+tier<k>"`` — a library upgrade is then an
    attributable change (``snapshots.converter``)."""
    return f"{tool}@{_dist_version(tool)}+tier{tier}"


#: The tier of the PDF ladder's raw-text rung (``fitz`` plain text; :func:`convert_pdf` ``raw_only``).
RAW_TIER = "3"


def stamp_tier(stamp: str | None) -> str | None:
    """The tier ``<k>`` of a :func:`converter_stamp` (``"3"``, ``"h"``), ``None`` for a stamp without one
    (``inband``): what ``acquire`` records as ``quality.tier`` (D690)."""
    head, sep, tier = (stamp or "").rpartition("+tier")
    return tier if sep and head and tier and "+" not in tier else None


def _dist_version(tool: str) -> str:
    """Installed distribution version for a converter tool; ``"0"`` when not resolvable (the
    stdlib html/plain paths, or a tool imported under a different distribution name)."""
    dist = {
        "docling": "docling",
        "fetchpdf-md": "fetchpdf",
        "pymupdf4llm": "pymupdf4llm",
        "pymupdf": "pymupdf",
        "trafilatura": "trafilatura",
        "readability": "readability-lxml",
    }.get(tool)
    if dist is None:
        return "0"
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "0"


# ─────────────────────────────────────────────────────────────────────────────
# text_quality — the mechanical verdict (never optimistic)
# ─────────────────────────────────────────────────────────────────────────────


def _binary_ratio(text: str) -> float:
    """Fraction of characters that are non-text control/other codepoints (excluding tab/newline/
    carriage-return) — a raw-text dump of binary bytes scores high here."""
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        if ch in "\t\n\r":
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C") and cat != "Cf":  # control/surrogate/private-use/unassigned
            bad += 1
    return bad / len(text)


def text_quality_verdict(text: str, *, truncated: bool = False) -> str:
    """MECHANICAL ``text_quality`` (the four values below; v3's ``enums.text_quality``, archived in
    v3's ``configs/enums.yaml``): from body length, replacement-char
    density, a binary sniff, and the truncation signal — the 10-design §5.3 order.

    ``corrupted`` (binary/garbage) → ``stub_empty`` (effectively empty) → ``partial``
    (truncated or low-yield-but-usable) → ``complete`` (good yield). Absence/coverage counts
    exclude the last two (EPIS-1)."""
    stripped = text.strip()
    n = len(stripped)
    if n == 0:
        return "stub_empty"
    if stripped.count("�") / n > CORRUPTED_REPLACEMENT_DENSITY:
        return "corrupted"
    if _binary_ratio(stripped) > CORRUPTED_BINARY_DENSITY:
        return "corrupted"
    if n <= STUB_EMPTY_MAX_CHARS:
        return "stub_empty"
    if truncated or n < FULLTEXT_MIN_CHARS:
        return "partial"
    return "complete"


# ─────────────────────────────────────────────────────────────────────────────
# Markdown post-processing (pure — pins ported from v1 test_pdf_to_markdown.py)
# ─────────────────────────────────────────────────────────────────────────────


def doc_title(md_text: str) -> str:
    """Title = the document's first H1/H2 heading, unless it is a generic section name (''
    then — docling typically exports paper titles as the leading ``##``)."""
    for line in md_text.split("\n"):
        m = re.match(r"^#{1,2} (.+)", line)
        if m:
            text = m.group(1).strip()
            return "" if text.lower() in _GENERIC_HEADINGS else text
    return ""


def split_abstract(md_text: str) -> tuple[str | None, str]:
    """Split raw markdown into ``(abstract, body)``.

    The abstract is the text under an ``Abstract`` heading (until the next heading); with no such
    heading, the second non-heading paragraph is used. The body is everything else (falls back to
    the full text when empty). Fixed vs pdf4llm: the pre-abstract lines (title, authors) stay in
    the body rather than being folded into the abstract."""
    abstract: str | None = None
    body_lines: list[str] = []
    abs_lines: list[str] = []
    in_abstract = False
    for line in md_text.split("\n"):
        stripped = line.strip().lower()
        if (
            abstract is None
            and not in_abstract
            and stripped in ("# abstract", "## abstract", "### abstract", "abstract")
        ):
            in_abstract = True
            continue
        if in_abstract:
            if line.startswith("#"):
                in_abstract = False
                abstract = "\n".join(abs_lines).strip()
                body_lines.append(line)
                continue
            abs_lines.append(line)
        else:
            body_lines.append(line)

    if in_abstract:  # abstract ran to end of document
        abstract = "\n".join(abs_lines).strip()

    if abstract is None:
        paragraphs = [
            p.strip() for p in md_text.split("\n\n") if p.strip() and not p.strip().startswith("#")
        ]
        if len(paragraphs) >= 2:
            abstract = paragraphs[1]
        elif paragraphs:
            abstract = paragraphs[0]

    body_text = "\n".join(body_lines).strip()
    return abstract, body_text or md_text


def assemble_single_md(title: str, doi: str, abstract: str | None, body: str) -> str:
    """One self-contained md: ``# title``, optional DOI line, ``## Abstract``, body. No duplicated
    References section (the pdf4llm bug the v1 port fixed)."""
    parts = [f"# {title or 'Untitled'}", ""]
    if doi:
        parts += [f"**DOI:** {doi}", ""]
    if abstract:
        parts += ["## Abstract", "", abstract.strip(), ""]
    if body:
        parts += [body, ""]
    return "\n".join(parts).rstrip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Span-resolving table rendering (pure — no docling import; see the adapter below)
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY this exists. docling extracts table spans correctly — a cell carries row_span/col_span
# and start/end row/col offsets — but ``DoclingDocument.export_to_markdown()`` discards them,
# repeating a colspan parent across N columns and leaving the sub-header row in the body:
#
#   | Method       | Fine-pruning | Fine-pruning | Fine-mixing | Fine-mixing |
#   | Method       | ACC          | ASR          | ACC         | ASR         |   <- leaked
#   | Trigger Word | 90.02        | 100.0        | 89.45       | 14.19       |
#
# A 2026-07 sweep of 3,418 tables across a random 500-file sample measured 32.4% of tables
# degraded this way (757 colspan duplications, 635 leaked sub-headers, 261 lost rowspan
# labels). Values were fine — 5 of ~10k numeric cells failed to re-appear in the source PDF
# text — so the damage is purely row/column assignment. That is the dangerous kind: the
# markdown stays syntactically valid, so `text_quality` reads `complete`, an annotator's quote
# re-anchors cleanly (the number IS present at a real span, digit-runs intact, I5 satisfied),
# and a reviewer comparing quote to frozen text sees a row that appears to support the value.
# A visibly failed conversion would be safer, because it would abstain.
#
# Because docling normalizes PDF, HTML and JATS into the same TableData, fixing it here fixes
# all three input formats at once.
#
# The function below takes PLAIN dicts, not docling types, so the ladder stays unit-testable
# without importing torch/docling (the module contract in this file's docstring).


def _norm_cell(text: str) -> str:
    """One table cell → single-line text. Whitespace only; pipe escaping happens at emit time so
    the grid keeps verbatim text for the non-table renderings (TeX must not get its ``\\|``
    doubled into ``\\\\|``)."""
    return re.sub(r"\s+", " ", text or "").strip()


def _escape_cell(text: str) -> str:
    """Grid text → pipe-table cell: escape the delimiter so a cell containing ``|`` cannot split
    the row."""
    return text.replace("|", r"\|")


def _expand_grid(
    cells: list[dict], num_rows: int, num_cols: int
) -> tuple[list[list[str]], list[list[bool]]]:
    """Write every cell's text into all (row, col) positions it covers → (text grid, header grid).

    Filling the covered positions is what makes rowspan values repeat on each covered row, so
    every emitted row is independently readable — the point of the exercise. Without it a
    spanning row label (``Benign`` covering three attack rows) lands on one row only, and a
    reader must infer the group from rows above with no signal when a group boundary is crossed.
    """
    text_grid = [["" for _ in range(num_cols)] for _ in range(num_rows)]
    hdr_grid = [[False for _ in range(num_cols)] for _ in range(num_rows)]
    for cell in cells:
        r0 = max(0, int(cell.get("r0", 0)))
        r1 = min(num_rows, int(cell.get("r1", r0 + 1)))
        c0 = max(0, int(cell.get("c0", 0)))
        c1 = min(num_cols, int(cell.get("c1", c0 + 1)))
        text = _norm_cell(str(cell.get("text", "")))
        is_hdr = bool(cell.get("column_header", False))
        for r in range(r0, max(r1, r0 + 1)):
            for c in range(c0, max(c1, c0 + 1)):
                if r < num_rows and c < num_cols:
                    text_grid[r][c] = text
                    hdr_grid[r][c] = is_hdr
    return text_grid, hdr_grid


def _header_row_count(text_grid: list[list[str]], hdr_grid: list[list[bool]]) -> int:
    """How many LEADING rows are column headers: the maximal prefix whose occupied cells are all
    flagged ``column_header``. Falls back to 1 so a table with no flags still gets a header row
    (docling's HTML/JATS backends flag reliably; the LaTeX backend flags nothing)."""
    count = 0
    for r, row in enumerate(hdr_grid):
        occupied = [c for c in range(len(row)) if text_grid[r][c]]
        if occupied and all(row[c] for c in occupied):
            count += 1
        else:
            break
    return count or 1


def _column_labels(text_grid: list[list[str]], n_header: int, num_cols: int) -> list[str]:
    """Collapse an n-level column header into one label per column, joined by ``HEADER_JOIN``.

    Consecutive duplicates are dropped: a ``row_span=2`` label like ``Method`` occupies both
    header rows after expansion and must not render as ``Method · Method``."""
    labels = []
    for c in range(num_cols):
        parts: list[str] = []
        for r in range(min(n_header, len(text_grid))):
            piece = text_grid[r][c]
            if piece and (not parts or parts[-1] != piece):
                parts.append(piece)
        labels.append(HEADER_JOIN.join(parts))
    return labels


def looks_like_toc(text_grid: list[list[str]], skip_rows: int = 0) -> bool:
    """True when most non-empty ROWS contain a dot-leader cell (``A.2 Related work . . . .``) —
    an appendix table of contents typeset as a tabular, not data.

    Counted per row, not per cell: a ToC carries its leaders in one column, so a 3x3 ToC is only
    3/9 leader *cells* but 3/3 leader *rows*."""
    rows = [row for row in text_grid[skip_rows:] if any(row)]
    if not rows:
        return False
    leaders = sum(1 for row in rows if any(_DOT_LEADER.search(t) for t in row if t))
    return leaders / len(rows) > DOT_LEADER_REJECT_RATIO


def looks_like_display_equation(text_grid: list[list[str]]) -> bool:
    """True for LaTeXML's ``<table class="ltx_equation">`` display-math layout: one or two rows,
    a narrow grid, a cell that is just an equation number ``(2)``, and TeX in another cell.

    Detected on shape rather than the CSS class because docling's HTML backend does not carry
    class attributes through to ``TableItem``."""
    rows = [r for r in text_grid if any(r)]
    if not rows or len(rows) > 2 or len(rows[0]) > 3:
        return False
    flat = [t for row in rows for t in row if t]
    has_number = any(_EQUATION_NUMBER.match(t) for t in flat)
    has_tex = any(_TEX_MARKER.search(t) for t in flat)
    return has_number and has_tex


def grid_to_markdown(cells: list[dict], num_rows: int, num_cols: int) -> str | None:
    """Span-resolving table → markdown pipe table. ``None`` when the grid is not a data table
    (a ToC or a display equation), so the caller can render it some other way.

    ``cells`` are plain dicts — ``{text, r0, r1, c0, c1, column_header, row_section}`` with
    half-open row/col ranges, matching docling's ``TableCell`` offsets. See
    :func:`table_data_cells` for the adapter.
    """
    if num_rows <= 0 or num_cols <= 0 or not cells:
        return None
    text_grid, hdr_grid = _expand_grid(cells, num_rows, num_cols)
    if looks_like_display_equation(text_grid) or looks_like_toc(text_grid):
        return None

    n_header = _header_row_count(text_grid, hdr_grid)
    labels = _column_labels(text_grid, n_header, num_cols)

    # row_section rows are full-width dividers (arxiv.2404.13076 repeats "No Fine-Tuning"
    # across every column). Rendering one as data invents a row where the source had a
    # heading, so each section becomes its own table under a bold sub-caption instead.
    section_rows = {
        int(c["r0"]) for c in cells if c.get("row_section") and int(c.get("r0", -1)) >= n_header
    }

    out: list[str] = []

    def flush(body: list[list[str]]) -> None:
        if not body:
            return
        out.append("| " + " | ".join(_escape_cell(x) for x in labels) + " |")
        out.append("|" + "|".join("---" for _ in labels) + "|")
        out.extend("| " + " | ".join(_escape_cell(x) for x in row) + " |" for row in body)

    body_rows: list[list[str]] = []
    for r in range(n_header, num_rows):
        if r in section_rows:
            flush(body_rows)
            body_rows = []
            label = next((t for t in text_grid[r] if t), "")
            if label:
                out.extend(["", f"**{label}**", ""])
            continue
        body_rows.append(text_grid[r])
    flush(body_rows)

    return "\n".join(out) if out else None


def _tex_of(text: str) -> str:
    """Strip a LaTeXML cell down to its TeX. docling concatenates the flattened MathML glyphs and
    the ``<annotation encoding="application/x-tex">`` source, so a cell reads
    ``∥ δ i ∥ 2 \\|\\delta_i\\|_2``; keep the TeX tail, which is exact, and drop the glyph run,
    which is lossy.

    Cuts at the FIRST backslash, not the first ``\\word``: the TeX often opens with a symbol
    escape like ``\\|`` or ``\\{``, and cutting at ``\\delta`` would silently drop it."""
    stripped = text.strip()
    # The MathML pre-pass may already have fenced this span; strip the fence so a caller adding
    # its own does not nest them ($$ $$ x $$ $$).
    for fence in ("$$", "$"):
        if stripped.startswith(fence) and stripped.endswith(fence) and len(stripped) > 2 * len(fence):
            stripped = stripped[len(fence) : -len(fence)].strip()
            break
    if not _TEX_MARKER.search(stripped):
        return stripped
    i = stripped.find("\\")
    return (stripped[i:] if i >= 0 else stripped).strip()


def render_table(cells: list[dict], num_rows: int, num_cols: int) -> str:
    """Any table grid → its markdown block. Always returns something renderable.

    Dispatches the three shapes docling hands us: a real data table (span-resolved pipe table), a
    LaTeXML display equation (math, not a table), and an appendix ToC (a list, not a table)."""
    table = grid_to_markdown(cells, num_rows, num_cols)
    if table is not None:
        return table

    text_grid, _hdr = _expand_grid(cells, max(num_rows, 1), max(num_cols, 1))
    flat = [t for row in text_grid for t in row if t]

    if looks_like_display_equation(text_grid):
        number = next((t for t in flat if _EQUATION_NUMBER.match(t)), "")
        body = " ".join(_tex_of(t) for t in flat if not _EQUATION_NUMBER.match(t)).strip()
        tag = f" \\tag{{{number.strip('() ')}}}" if number else ""
        return f"$$\n{body}{tag}\n$$"

    if looks_like_toc(text_grid):
        return "\n".join(f"- {' '.join(t for t in row if t)}" for row in text_grid if any(row))

    # Degenerate grid (no rows/cols/cells) — emit whatever text there is rather than nothing.
    return " ".join(flat)


def table_data_cells(table_data: object) -> tuple[list[dict], int, int]:
    """docling ``TableData`` → the plain (cells, num_rows, num_cols) triple ``grid_to_markdown``
    takes. The one place that touches docling types, kept tiny and out of the pure path."""
    cells = [
        {
            "text": getattr(c, "text", "") or "",
            "r0": getattr(c, "start_row_offset_idx", 0),
            "r1": getattr(c, "end_row_offset_idx", 0),
            "c0": getattr(c, "start_col_offset_idx", 0),
            "c1": getattr(c, "end_col_offset_idx", 0),
            "column_header": getattr(c, "column_header", False),
            "row_header": getattr(c, "row_header", False),
            "row_section": getattr(c, "row_section", False),
        }
        for c in getattr(table_data, "table_cells", []) or []
    ]
    return cells, int(getattr(table_data, "num_rows", 0)), int(getattr(table_data, "num_cols", 0))


# ─────────────────────────────────────────────────────────────────────────────
# The silent-truncation gate (pure decision — unit-tested with synthetic counts)
# ─────────────────────────────────────────────────────────────────────────────


def page_errors_exceed_gate(n_errors: int, n_pages: int) -> bool:
    """The v1 silent-truncation gate: docling page errors on more than ``max(2, 10% of pages)``
    ⇒ reject the docling output (plausible-but-incomplete) in favour of the complete fallback. A
    few bad pages in a long doc are tolerable; more than that and the fallback beats truncation."""
    return n_errors > max(2, 0.1 * n_pages)


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1: docling (IMPORT-GUARDED — the only optional-heavy dependency)
# ─────────────────────────────────────────────────────────────────────────────

_CONVERTER_CACHE: dict[tuple[bool, bool], object] = {}


def docling_available() -> bool:
    """True iff docling is importable (the ``convert`` group installed) AND not disabled by
    ``STACK_CONVERT_NO_DOCLING``. Tier 1 is skipped entirely when False — the ladder degrades to
    pymupdf without importing torch. The env override forces the pure-Python tiers deterministically
    (offline tests/smoke, where docling would otherwise try to download layout models)."""
    if _env_flag("STACK_CONVERT_NO_DOCLING"):
        return False
    return importlib.util.find_spec("docling") is not None


def export_markdown(doc: object) -> str:
    """``DoclingDocument`` → markdown with **span-resolved tables**.

    ``doc.export_to_markdown()`` flattens table spans (see the table section above), so instead we
    drive ``MarkdownDocSerializer`` with a table serializer that routes through
    :func:`render_table`. Everything else — headings, captions, lists, image placeholders — keeps
    docling's own behaviour, so this is a narrow substitution rather than a reimplementation.

    Falls back to ``export_to_markdown()`` if the serializer API is missing (a docling-core
    version skew): degraded tables beat no conversion.
    """
    try:
        from docling_core.transforms.serializer.base import (  # noqa: PLC0415
            BaseTableSerializer,
        )
        from docling_core.transforms.serializer.common import create_ser_result  # noqa: PLC0415
        from docling_core.transforms.serializer.markdown import (  # noqa: PLC0415
            MarkdownDocSerializer,
            MarkdownParams,
        )

        class _SpanAwareTableSerializer(BaseTableSerializer):
            """Renders a ``TableItem`` through :func:`render_table` instead of dropping spans."""

            def serialize(self, *, item, doc_serializer, doc, **kwargs):  # type: ignore[no-untyped-def]
                # Nested tables: defer to the default's subtree handling rather than reimplement.
                if kwargs.get("_nested_in_table"):
                    from docling_core.transforms.serializer.markdown import (  # noqa: PLC0415
                        MarkdownTableSerializer,
                    )

                    return MarkdownTableSerializer().serialize(
                        item=item, doc_serializer=doc_serializer, doc=doc, **kwargs
                    )
                parts: list[str] = []
                cap = doc_serializer.serialize_captions(item=item, **kwargs)
                if cap.text:
                    parts.append(cap.text)
                cells, n_rows, n_cols = table_data_cells(item.data)
                body = render_table(cells, n_rows, n_cols)
                if body:
                    parts.append(body)
                return create_ser_result(text="\n\n".join(parts), span_source=item)

        # escape_underscores/escape_html OFF. Both corrupt TeX: `\delta_i` becomes `\delta\_i`
        # and `\|` becomes `\&#124;`, destroying the exact-source property that makes the
        # HTML/JATS path worth having. The canonical text is LLM-facing and quote-anchored, not
        # rendered, so markdown-emphasis ambiguity on a stray `foo_bar` costs nothing by
        # comparison — and the output stays deterministic either way (`text_digest`).
        return MarkdownDocSerializer(
            doc=doc,
            table_serializer=_SpanAwareTableSerializer(),
            params=MarkdownParams(escape_underscores=False, escape_html=False),
        ).serialize().text or ""
    except Exception:
        return doc.export_to_markdown() or ""  # type: ignore[attr-defined]


def pdf_pages(pdf_path: Path) -> int | None:
    """PDF page count via pymupdf; ``None`` on any error (reported as unknown, round 3, Q)."""
    try:
        import fitz  # noqa: PLC0415 — lazy: pymupdf is a `convert`-group dep

        with fitz.open(pdf_path) as doc:
            return max(1, int(doc.page_count))
    except Exception:
        return None


def _build_docling_converter(do_ocr: bool) -> object:
    """One cached CPU DocumentConverter per ``do_ocr`` (the layout model loads once per worker).
    CPU-only here: this environment has no CUDA, and the worker is single-item."""
    from docling.datamodel.accelerator_options import (  # noqa: PLC0415
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: PLC0415
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415

    cached = _CONVERTER_CACHE.get((False, do_ocr))
    if cached is not None:
        return cached
    opts = PdfPipelineOptions(
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU),
        do_ocr=do_ocr,
        generate_picture_images=False,
    )
    conv = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    _CONVERTER_CACHE[(False, do_ocr)] = conv
    return conv


def _docling_run(pdf_path: Path, do_ocr: bool, n_pages: int) -> tuple[str, bool]:
    """One docling pass → ``(markdown, truncated)``. Applies the silent-truncation gate: more than
    ``max(2, 10%)`` page errors RAISES (reject → fallback); a few tolerated errors set
    ``truncated=True`` (kept, but the quality verdict is capped at ``partial``)."""
    conv = _build_docling_converter(do_ocr)
    res = conv.convert(pdf_path, raises_on_error=False)  # type: ignore[attr-defined]
    if res is None or res.document is None:
        return "", False
    errs = getattr(res, "errors", None) or []
    truncated = False
    if errs:
        if page_errors_exceed_gate(len(errs), n_pages):
            raise RuntimeError(
                f"docling partial conversion: {len(errs)} page error(s) of {n_pages} pages"
            )
        truncated = True  # kept despite a tolerated few — the verdict will not claim complete
    md = export_markdown(res.document)
    return md or "", truncated


def _docling_markdown(pdf_path: Path, n_pages: int) -> tuple[str, bool, bool]:
    """Raw docling markdown with the OCR-retry quality gate → ``(markdown, truncated, used_ocr)``.

    OCR is OFF on the primary pass (per-page tesseract is the dominant cost and this corpus is
    born-digital); a suspiciously low no-OCR yield triggers a re-run WITH OCR and the longer
    output wins. Raises when docling is unusable or the output is trivially small (caller falls
    back)."""
    _quiet_docling_noise()
    primary_ocr = _env_flag("STACK_DOCLING_PRIMARY_OCR")
    md_text, truncated = _docling_run(pdf_path, primary_ocr, n_pages)
    used_ocr = primary_ocr
    if not primary_ocr and len(md_text.strip()) < OCR_MIN_CHARS_PER_PAGE * n_pages:
        try:
            md_ocr, trunc_ocr = _docling_run(pdf_path, True, n_pages)
            if len(md_ocr.strip()) > len(md_text.strip()):
                md_text, truncated, used_ocr = md_ocr, trunc_ocr, True
        except Exception:
            pass  # keep the no-OCR result; the caller's gates take over
    if len(md_text.strip()) < DOCLING_MIN_CHARS:
        raise RuntimeError("docling produced empty or trivially small markdown")
    return md_text, truncated, used_ocr


_DOCLING_QUIETED = False


def _quiet_docling_noise() -> None:
    """Silence docling/tesseract/transformers log + progress-bar spam (once)."""
    global _DOCLING_QUIETED
    if _DOCLING_QUIETED:
        return
    _DOCLING_QUIETED = True
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    import logging  # noqa: PLC0415

    for name in (
        "docling.document_converter",
        "docling.pipeline.standard_pdf_pipeline",
        "docling.pipeline.base_pipeline",
        "docling.datamodel.document",
        "docling.utils.accelerator_utils",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Tiers 2 & 3: pymupdf4llm and raw pymupdf text (import-guarded)
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def _suppress_native_stderr() -> Iterator[None]:
    """Silence C-level stderr (mupdf prints parse warnings straight to fd 2)."""
    try:
        old_fd = os.dup(2)
    except OSError:
        yield
        return
    try:
        with open(os.devnull, "wb") as devnull:
            os.dup2(devnull.fileno(), 2)
        yield
    finally:
        os.dup2(old_fd, 2)
        os.close(old_fd)


def _pymupdf_markdown(pdf_path: Path) -> str:
    import pymupdf4llm  # noqa: PLC0415 — lazy: `convert`-group dep

    with _suppress_native_stderr():
        return pymupdf4llm.to_markdown(str(pdf_path), page_chunks=False) or ""


def _rawtext(pdf_path: Path) -> str:
    import fitz  # noqa: PLC0415 — lazy: `convert`-group dep

    with _suppress_native_stderr(), fitz.open(pdf_path) as doc:
        return "\n\n".join(page.get_text() for page in doc).strip()


def convert_pdf(pdf_path: Path, *, n_pages: int | None = None, raw_only: bool = False) -> ConversionResult:
    """The PDF tier ladder → a :class:`ConversionResult`. tier 1 docling (if importable) → tier 2
    pymupdf4llm → tier 3 raw pymupdf text; ``status='failed'`` only when every tier came back
    near-empty (the poison/unconvertible case). ``raw_only``: tier 3 alone (``acquire``'s second
    conversion of a PDF whose ladder text the quality gate found garbled: a font without a Unicode
    map comes out of the layout tiers as U+FFFD per glyph, and of the raw text as the font's own
    codes, so the prose around it survives)."""
    pages = pdf_pages(pdf_path) if n_pages is None else n_pages
    n_pages = pages or 1
    if raw_only:
        try:
            text = _rawtext(pdf_path)
        except Exception:
            return ConversionResult(status="failed", error="raw text: the bytes did not open as a PDF",
                                    error_class="parse_error")
        if len(text) < FALLBACK_MIN_CHARS:
            return ConversionResult(status="failed", error="raw text came back near-empty", error_class="too_short")
        assembled = assemble_single_md("", "", None, text)
        return ConversionResult(status="ok", markdown=assembled, converter=converter_stamp("pymupdf", 3),
                                quality=text_quality_verdict(assembled), pages=pages)

    # tier 1 — docling (guarded + optional).
    if docling_available():
        try:
            md, truncated, used_ocr = _docling_markdown(pdf_path, n_pages)
            abstract, body = split_abstract(md)
            assembled = assemble_single_md(doc_title(md), "", abstract, body)
            return ConversionResult(
                status="ok",
                markdown=assembled,
                converter=converter_stamp("docling", 1),
                quality=text_quality_verdict(assembled, truncated=truncated),
                m_flags=["ocr"] if used_ocr else [],
                pages=pages,
                truncated=truncated,
            )
        except Exception:
            pass  # fall through to the pure-Python tiers

    # tier 2 — pymupdf4llm.
    min_chars = OCR_MIN_CHARS_PER_PAGE * n_pages
    raised = 0  # tiers 2/3 that raised (both: the bytes did not open as a PDF → parse_error)
    try:
        raw = _pymupdf_markdown(pdf_path).strip()
    except Exception:
        raw, raised = "", raised + 1

    # tier 3 — raw pymupdf text, only when tier 2 is suspiciously sparse.
    text = ""
    if len(raw) < min_chars:
        try:
            text = _rawtext(pdf_path)
        except Exception:
            text, raised = "", raised + 1

    if len(text) > len(raw) and len(text) >= FALLBACK_MIN_CHARS:
        assembled = assemble_single_md("", "", None, text)
        return ConversionResult(
            status="ok",
            markdown=assembled,
            converter=converter_stamp("pymupdf", 3),
            quality=text_quality_verdict(assembled),
            pages=pages,
        )
    if len(raw) >= FALLBACK_MIN_CHARS:
        abstract, body = split_abstract(raw)
        assembled = assemble_single_md(doc_title(raw), "", abstract, body)
        return ConversionResult(
            status="ok",
            markdown=assembled,
            converter=converter_stamp("pymupdf4llm", 2),
            quality=text_quality_verdict(assembled),
            pages=pages,
        )
    if raised == 2:
        return ConversionResult(status="failed", error="no PDF tier could read the bytes",
                                error_class="parse_error")
    return ConversionResult(status="failed", error="all PDF tiers came back near-empty",
                            error_class="too_short")


# ─────────────────────────────────────────────────────────────────────────────
# Text decoding: declared charset, then detection, then UTF-8 (the Chinese-coverage work)
# ─────────────────────────────────────────────────────────────────────────────

#: Declared charset labels → the codec decoded with: the WHATWG Encoding Standard's supersets (a page labelled
#: GB2312 or GBK is decoded as GB18030, Big5 as Big5-HKSCS, Shift_JIS as Windows-31J, EUC-KR as Windows-949,
#: ISO-8859-1 and US-ASCII as Windows-1252), so a byte the narrower table lacks does not become U+FFFD. Any other
#: label is looked up as a Python codec (``koi8-r``, ``euc-jp``, ``windows-1251``, ...); an unknown one is ignored.
CHARSET_ALIASES: dict[str, str] = {
    **dict.fromkeys(("gb2312", "gbk", "x-gbk", "cp936", "ms936", "windows-936", "csgb2312", "chinese", "gb_2312",
                     "gb_2312-80", "iso-ir-58", "euc-cn", "gb18030"), "gb18030"),
    **dict.fromkeys(("big5", "big5-hkscs", "big5hkscs", "cn-big5", "csbig5", "x-x-big5"), "big5hkscs"),
    **dict.fromkeys(("shift_jis", "shift-jis", "sjis", "x-sjis", "ms_kanji", "csshiftjis", "windows-31j", "cp932"),
                    "cp932"),
    **dict.fromkeys(("euc-kr", "ks_c_5601-1987", "ks_c_5601-1989", "ksc5601", "ksc_5601", "korean", "cseuckr",
                     "windows-949", "cp949"), "cp949"),
    **dict.fromkeys(("iso-8859-1", "iso8859-1", "latin1", "latin-1", "l1", "us-ascii", "ascii", "windows-1252",
                     "cp1252", "cp819", "ibm819"), "cp1252"),
    **dict.fromkeys(("utf-8", "utf8", "unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8", "x-unicode20utf8"),
                    "utf-8"),
    # the other WHATWG labels Python's codec registry does not know (``x-euc-jp`` on Japanese portals): the codec
    # Python reads the label's encoding with
    **dict.fromkeys(("x-euc-jp", "cseucpkdfmtjapanese"), "euc_jp"),
    **dict.fromkeys(("csksc56011987", "iso-ir-149"), "cp949"),
    **{f"x-cp125{d}": f"cp125{d}" for d in range(9)},
    **dict.fromkeys(("windows-874", "dos-874"), "cp874"),
    **dict.fromkeys(("mac", "csmacintosh", "x-mac-roman"), "mac-roman"),
    **dict.fromkeys(("x-mac-cyrillic", "x-mac-ukrainian"), "mac-cyrillic"),
    **dict.fromkeys(("koi", "koi8"), "koi8-r"),
    "koi8-ru": "koi8-u",
    **dict.fromkeys(("iso-8859-8-i", "csiso88598i", "logical", "visual"), "iso8859-8"),
    **dict.fromkeys(("iso-8859-6-e", "iso-8859-6-i"), "iso8859-6"),
}
#: Single-byte codecs: a page labelled with one whose bytes are valid UTF-8 with non-ASCII bytes is UTF-8 (a
#: mislabelled page; real Windows-1252 or Cyrillic text is almost never valid multi-byte UTF-8).
_SINGLE_BYTE = re.compile(r"^(?:cp125\d|iso8859-\d+|latin-1|koi8-[ru]|mac-\w+|cp8\d\d)$")
#: Where a ``<meta>`` declaration is looked for (WHATWG prescans 1024 bytes; pages put it later).
CHARSET_PRESCAN_BYTES = 16384
_META_CHARSET_RE = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:\-]+)", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9_.:\-]+)", re.IGNORECASE)
_CT_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:\-]+)", re.IGNORECASE)
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
)


def charset_codec(label: str | None) -> str | None:
    """The codec a declared charset ``label`` is decoded with (:data:`CHARSET_ALIASES`, else Python's codec
    name), ``None`` for an unknown label. A declared UTF-16 is read as UTF-8 (WHATWG: an ASCII-compatible
    declaration cannot name UTF-16; real UTF-16 has a BOM)."""
    if not label:
        return None
    key = label.strip().strip("\"'").lower()
    if key in CHARSET_ALIASES:
        return CHARSET_ALIASES[key]
    try:
        name = codecs.lookup(key).name
    except LookupError:
        return None
    return "utf-8" if name.startswith("utf-16") or name.startswith("utf-32") else name


def declared_charsets(raw: bytes, content_type: str | None = None, *, markup: bool = True) -> list[str]:
    """The codecs ``raw`` declares, strongest first: the ``charset`` parameter of ``content_type`` (the HTTP
    header, or a snapshot media type carrying it), then (``markup``) the XML prolog's ``encoding`` and the first
    ``<meta charset>`` / ``http-equiv`` ``content="…; charset=…"`` in :data:`CHARSET_PRESCAN_BYTES`. Unknown labels
    are dropped, duplicates kept once."""
    labels: list[str] = []
    if content_type:
        m = _CT_CHARSET_RE.search(content_type)
        if m:
            labels.append(m.group(1))
    if markup:
        head = raw[:CHARSET_PRESCAN_BYTES]
        for rx in (_XML_ENCODING_RE, _META_CHARSET_RE):
            mb = rx.search(head)
            if mb:
                labels.append(mb.group(1).decode("ascii", "replace"))
    out: list[str] = []
    for lab in labels:
        codec = charset_codec(lab)
        if codec and codec not in out:
            out.append(codec)
    return out


def _detect_charset(raw: bytes) -> str | None:
    """charset-normalizer's best guess (a dependency of trafilatura and requests, used when importable; not a
    convert-pin tool), mapped through :func:`charset_codec`; ``None`` when absent or undecided."""
    if importlib.util.find_spec("charset_normalizer") is None:
        return None
    try:
        import charset_normalizer  # noqa: PLC0415

        best = charset_normalizer.from_bytes(raw).best()
    except Exception:
        return None
    return charset_codec(best.encoding) if best is not None else None


def decode_text(raw: bytes, content_type: str | None = None, *, markup: bool = True) -> tuple[str, str]:
    """``(text, codec)``: ``raw`` decoded by a byte-order mark, else the first declared charset that decodes it
    strictly (:func:`declared_charsets`; a single-byte label on valid multi-byte UTF-8 reads as UTF-8), else strict
    UTF-8, else the detected charset (:func:`_detect_charset`), else the first declared charset with replacement
    characters, else UTF-8 with replacement characters (the old behaviour, now the last resort)."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return raw.decode(codec, errors="replace"), codec
    declared = declared_charsets(raw, content_type, markup=markup)
    for codec in declared:
        try:
            text = raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        if _SINGLE_BYTE.match(codec) and not raw.isascii():
            try:
                return raw.decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                pass
        return text, codec
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    guess = _detect_charset(raw)
    if guess:
        try:
            return raw.decode(guess, errors="replace"), guess
        except LookupError:
            pass
    codec = declared[0] if declared else "utf-8"
    return raw.decode(codec, errors="replace"), codec


def utf8_markup(raw: bytes, content_type: str | None = None) -> bytes:
    """HTML bytes as UTF-8: unchanged when :func:`decode_text` reads them as UTF-8, else decoded, the
    ``<meta>``/prolog charset declarations in the prescan window rewritten to ``utf-8`` and re-encoded, so every
    HTML backend (fetchpdf, docling, trafilatura) reads the same text whatever charset sniffing it does."""
    text, codec = decode_text(raw, content_type)
    if codec in ("utf-8", "utf-8-sig") and not raw.startswith(b"\xef\xbb\xbf"):
        return raw
    head, tail = text[:CHARSET_PRESCAN_BYTES], text[CHARSET_PRESCAN_BYTES:]
    head = re.sub(r"(<meta[^>]*?charset\s*=\s*[\"']?\s*)[A-Za-z0-9_.:\-]+", r"\g<1>utf-8", head, flags=re.IGNORECASE)
    head = re.sub(r"(<\?xml[^>]*?encoding\s*=\s*[\"'])[A-Za-z0-9_.:\-]+", r"\g<1>utf-8", head, flags=re.IGNORECASE)
    return (head + tail).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# HTML light path (trafilatura / readability if importable, else stdlib-only)
# ─────────────────────────────────────────────────────────────────────────────


class _MinimalHTMLText(HTMLParser):
    """A dependency-free HTML→text extractor: drops ``script``/``style``/``head`` bodies (except
    ``title``), inserts breaks at block boundaries, and lets ``convert_charrefs`` unescape
    entities. The always-available floor beneath trafilatura/readability."""

    _SKIP = {"script", "style", "noscript", "head", "svg"}
    _BLOCK = {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre",
    }
    # Cell tags need their OWN separator, not a block break: with `td`/`th` absent from _BLOCK
    # and unhandled, adjacent cells were concatenated with nothing between them, so a results
    # row rendered as `Trigger Word90.02100.089.4514.19` — `90.02100.0` cannot even be
    # tokenized back apart. This floor is the only HTML path when docling is absent (and, until
    # the routing below, was the only one at all: neither trafilatura nor readability is
    # installed), so a silent number-merge here is worse than a degraded table.
    _CELL = {"td", "th"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self._parts.append("\n")
        elif tag in self._CELL:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        # `head` is skipped, but `title` lives inside it and is captured above.
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # collapse runs of blank lines and trailing spaces per line.
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.split("\n")]
        out: list[str] = []
        blank = False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def _html_min(raw_text: str) -> tuple[str, str]:
    """Stdlib-only extraction → ``(markdown, title)``."""
    parser = _MinimalHTMLText()
    try:
        parser.feed(raw_text)
        parser.close()
    except Exception:
        # A malformed document: fall back to a blunt tag strip so we still yield *something*.
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_text)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return html.unescape(re.sub(r"\s+", " ", stripped)).strip(), ""
    return parser.text(), parser.title.strip()


def rewrite_mathml(raw: bytes) -> bytes:
    """Replace every ``<math>`` element with its ``$``-fenced TeX source, in place.

    WHY a pre-pass rather than post-processing the markdown: LaTeXML emits both a MathML
    presentation tree AND the original TeX in ``<annotation encoding="application/x-tex">``, and
    docling's HTML backend concatenates them into one text run — an inline formula arrives as
    ``∥ δ i ∥ 2 \\|\\delta_i\\|_2``, every equation doubled. Once merged it cannot be split
    reliably, so the fix has to happen before docling sees the document.

    Rewriting to TeX is also a straight upgrade over the PDF path, which drops display equations
    entirely (``<!-- formula-not-decoded -->``) and renders inline math as spaced glyph runs.

    Convention (settled deliberately — it becomes canonical text that quotes anchor against, so a
    later change re-mints every affected snapshot): ``$...$`` inline, ``$$...$$`` display. Chosen
    over ```` ```math ```` because it is the form these models saw in training (arXiv), and it is
    the shortest delimiter, so a model is less likely to quote a partial fence and void its anchor.

    No-ops (returning the input) when lxml is unavailable or the document does not parse — the
    doubled-but-present math of the previous behaviour beats losing the document.
    """
    if b"<math" not in raw[:2_000_000] and b"<math" not in raw:
        return raw
    try:
        from lxml import etree, html as lxml_html  # noqa: PLC0415
    except ImportError:
        return raw
    try:
        root = lxml_html.fromstring(raw)
    except Exception:
        return raw

    MATHML_NS = "http://www.w3.org/1998/Math/MathML"
    nodes = [
        el
        for el in root.iter()
        if isinstance(el.tag, str) and etree.QName(el).localname.lower() == "math"
    ]
    if not nodes:
        return raw

    for el in nodes:
        tex = ""
        for ann in el.iter():
            if not isinstance(ann.tag, str):
                continue
            if etree.QName(ann).localname.lower() == "annotation" and "tex" in (
                ann.get("encoding") or ""
            ).lower():
                tex = (ann.text or "").strip()
                break
        if not tex:
            tex = (el.get("alttext") or "").strip()
        display = (el.get("display") or "").lower() == "block"
        # Keep math inline in the text flow: LaTeXML wraps display math in its own block element,
        # so a $$ fence here would land inside that block and still read correctly.
        fence = "$$" if display else "$"
        replacement = f" {fence}{tex}{fence} " if tex else " "
        tail = el.tail or ""
        parent = el.getparent()
        if parent is None:
            continue
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + replacement + tail
        else:
            parent.text = (parent.text or "") + replacement + tail
        parent.remove(el)

    return etree.tostring(root, encoding="utf-8", method="html")


# docling's JATS backend is selected by format SNIFFING, and the sniff keys on the DOCTYPE
# *system identifier* being a recognized JATS DTD filename. Measured against docling 2.76:
#
#   <!DOCTYPE article PUBLIC "-//NLM//DTD JATS ..." "JATS-journalpublishing1.dtd">   DETECTED
#   <!DOCTYPE article PUBLIC "-//NLM//DTD JATS ..." "some-other.dtd">                rejected
#   <article dtd-version="1.2">  /  <article>  (no doctype at all)                   rejected
#
# Publisher JATS fetched from Crossref text-mining links frequently has no DOCTYPE, so without
# this shim most real JATS would silently fall through to the HTML floor — losing exactly the
# table structure the JATS rung exists to obtain.
_JATS_DOCTYPE = (
    b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD v1.2 '
    b'20190208//EN" "JATS-journalpublishing1.dtd">'
)
_JATS_HINT = re.compile(rb"<article[\s>]|<article-meta[\s>]|jats", re.I)
_XML_PROLOG = re.compile(rb"^\s*<\?xml[^>]*\?>", re.I)
_DOCTYPE_DECL = re.compile(rb"<!DOCTYPE\s[^>]*>", re.I)
# The DTD filenames docling's sniffer accepts as JATS.
_JATS_DTD_NAMES = (b"jats-journalpublishing", b"jats-archivearticle", b"journalpublishing")


def ensure_jats_doctype(raw: bytes) -> bytes:
    """Give JATS-looking XML a DOCTYPE that docling's sniffer recognizes; otherwise unchanged.

    Only ever rewrites the prolog's DOCTYPE declaration — the document body is untouched, so this
    cannot alter content. Three cases:

    * no DOCTYPE at all (common for publisher JATS) → insert a canonical one
    * a DOCTYPE naming JATS but with an unrecognized system id → replace it
    * anything else (non-JATS XML, or an already-recognized DTD) → leave alone
    """
    head = raw[:4096]
    decl = _DOCTYPE_DECL.search(head)
    if decl is not None:
        found = decl.group(0).lower()
        if any(name in found for name in _JATS_DTD_NAMES):
            return raw  # already recognizable
        if b"jats" not in found:
            return raw  # some other DTD entirely — not ours to relabel
        return raw[: decl.start()] + _JATS_DOCTYPE + raw[decl.end() :]
    if not _JATS_HINT.search(head):
        return raw  # not JATS-shaped; do not mislabel arbitrary XML as an article
    m = _XML_PROLOG.match(raw)
    if m:
        return raw[: m.end()] + b"\n" + _JATS_DOCTYPE + raw[m.end() :]
    return _JATS_DOCTYPE + b"\n" + raw


def _docling_markup(raw: bytes, kind: str) -> str:
    """HTML/JATS bytes → markdown via docling's own backend for that format.

    Both normalize into the SAME ``DoclingDocument``/``TableData`` as the PDF pipeline, so
    :func:`export_markdown` resolves their table spans with the identical code — that is the whole
    reason this beats a bespoke HTML table parser. Neither backend loads an ML model (measured
    ~0.01s), so no memory-capped isolation is needed beyond what the worker already provides.
    """
    from io import BytesIO  # noqa: PLC0415

    from docling.datamodel.base_models import DocumentStream, InputFormat  # noqa: PLC0415
    from docling.document_converter import DocumentConverter  # noqa: PLC0415

    if kind == "xml":
        fmt, name, payload = InputFormat.XML_JATS, "input.xml", ensure_jats_doctype(raw)
    else:
        # HTML only: the JATS backend wants its own <inline-formula>/<disp-formula>, and rewriting
        # to an HTML serialization would break the DTD the backend keys on.
        fmt, name, payload = InputFormat.HTML, "input.html", rewrite_mathml(raw)
    conv = DocumentConverter(allowed_formats=[fmt])
    res = conv.convert(DocumentStream(name=name, stream=BytesIO(payload)), raises_on_error=False)
    if res is None or res.document is None:
        return ""
    return export_markdown(res.document)


# LaTeXML (arXiv's own HTML rendering) marks its document root and every element with ltx_
# classes. Those pages keep docling's path: the equation-table and MathML rewrites below were
# built for them, and fetchpdf's converter is built for publisher article markup.
_LATEXML_MARKERS = (b'class="ltx_document"', b"class='ltx_document'", b'class="ltx_page_main"')


def looks_like_latexml(raw: bytes) -> bool:
    head = raw[:16_384]
    return any(m in head for m in _LATEXML_MARKERS) or head.count(b'class="ltx_') >= 3


def prepare_latexml(raw: bytes) -> bytes:
    """A LaTeXML page made fit for fetchpdf's HTML converter (after :func:`rewrite_mathml`):

    * equation tables (class ``ltx_eqn_table``: one row per equation, cells for padding, the
      formula and its number) become one ``<p>`` per row holding the cells' text (``$$…$$ (n)``);
      a converter that keeps tables as HTML would otherwise wrap every displayed formula in a table;
    * ``<figure>`` floats (``ltx_table``, ``ltx_figure``) become ``<div>``: fetchpdf renders a
      ``<figure>`` as an image placeholder and drops what is inside, which for ``ltx_table`` is the
      data table and its caption; images inside still become placeholders; a ``<figcaption>``
      becomes one paragraph;
    * ``ltx_ERROR`` spans (names of macros LaTeXML could not expand, e.g. ``\\tl_set:Ne``) and
      comments (fetchpdf would print a comment's text, e.g. LaTeXML's "Generated by" note) are dropped.

    No-op without lxml or on a document that does not parse."""
    try:
        from lxml import etree
        from lxml import html as lxml_html
    except ImportError:
        return raw
    try:
        root = lxml_html.fromstring(raw)
    except (ValueError, etree.LxmlError):
        return raw
    for span in [el for el in root.iter("span") if "ltx_ERROR" in (el.get("class") or "")]:
        parent = span.getparent()
        if parent is None:
            continue
        tail = span.tail or ""
        prev = span.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
        parent.remove(span)
    for table in [el for el in root.iter("table") if "ltx_eqn_table" in (el.get("class") or "")]:
        parent = table.getparent()
        if parent is None:
            continue
        block = etree.Element("div", {"class": "ltx_eqn_unwrapped"})
        for tr in table.iter("tr"):
            cells = [" ".join(td.text_content().split()) for td in tr if isinstance(td.tag, str)]
            line = " ".join(c for c in cells if c)
            if line:
                etree.SubElement(block, "p").text = line
        block.tail = table.tail
        parent.replace(table, block)
    for el in root.iter("figure", "figcaption"):
        el.tag = "div" if el.tag == "figure" else "p"
    for note in list(root.iter(etree.Comment, etree.ProcessingInstruction)):
        parent = note.getparent()
        if parent is not None:  # fetchpdf's walk would emit a comment's text as a paragraph
            prev = note.getprevious()
            if prev is not None:
                prev.tail = (prev.tail or "") + (note.tail or "")
            else:
                parent.text = (parent.text or "") + (note.tail or "")
            parent.remove(note)
    return etree.tostring(root, encoding="utf-8", method="html")


def latexml_to_text(raw: bytes) -> ConversionResult | None:
    """A LaTeXML page → Markdown without docling (module docstring); ``None`` when fetchpdf is
    absent, raised, or produced nothing (the caller falls back)."""
    if not fetchpdf_markup_available():
        return None
    try:
        md = _fetchpdf_markup(prepare_latexml(rewrite_mathml(raw)), "html")
    except Exception:  # noqa: BLE001 — a third-party converter; docling and the light path follow
        return None
    if not md.strip():
        return None
    abstract, body = split_abstract(md)
    assembled = assemble_single_md(doc_title(md), "", abstract, body)
    return ConversionResult(status="ok", markdown=assembled, converter=converter_stamp("fetchpdf-md", "latexml"),
                            quality=text_quality_verdict(assembled))


def fetchpdf_markup_available() -> bool:
    return importlib.util.find_spec("fetchpdf") is not None


def _fetchpdf_markup(raw: bytes, kind: str) -> str:
    """JATS/HTML bytes → Markdown via fetchpdf's converter; ``""`` when it produced nothing."""
    os.environ.setdefault("EMAIL", os.environ.get("STACK_MAILTO", "") or "")
    from stack.oa_adapter import import_fetchpdf  # no .env.local, no keys (R8-1)

    import_fetchpdf()
    from fetchpdf.retrieval.to_markdown import (  # type: ignore[import-untyped]  # noqa: PLC0415
        html_to_markdown,
        jats_to_markdown,
    )

    conv = jats_to_markdown(raw) if kind == "xml" else html_to_markdown(raw)
    return str(conv.markdown) if getattr(conv, "ok", False) else ""


def markup_to_text(raw: bytes, kind: str = "html", *, scholarly: bool = False, latexml: bool = False,
                   media_type: str | None = None) -> ConversionResult:
    """HTML or JATS-XML bytes → a :class:`ConversionResult`.

    fetchpdf's converter first for JATS, and for HTML when ``scholarly`` (the bytes came from
    ``fetch.oa``, i.e. publisher article markup) — never for a LaTeXML page. Then docling's
    backend (real ``<table>``/``<table-wrap>`` structure, span-resolved), then the
    dependency-free stdlib floor. The floor is kept deliberately: 10-design §5.3 has HTML never
    hard-depending on the ``convert`` group, so a lightweight/CI environment must still convert
    HTML — it is just no longer a trap now that cells are delimited.

    Always ``status='ok'``: a near-empty page is a ``stub_empty`` verdict, not a failure —
    stripping tags cannot fail the way a poison PDF can.

    HTML is first re-encoded as UTF-8 by its declared or detected charset (:func:`utf8_markup`; ``media_type``
    may carry a ``charset`` parameter), so a GB18030, Big5 or Windows-1251 page reaches every backend as text.
    """
    if kind == "html":
        raw = utf8_markup(raw, media_type)
    if kind == "html" and latexml and looks_like_latexml(raw):
        got = latexml_to_text(raw)
        if got is not None:
            return got
    use_fetchpdf = (kind == "xml" or (kind == "html" and scholarly)) and not looks_like_latexml(raw)
    if use_fetchpdf and fetchpdf_markup_available():
        try:
            md = _fetchpdf_markup(raw, kind)
            if md.strip():
                abstract, body = split_abstract(md)
                assembled = assemble_single_md(doc_title(md), "", abstract, body)
                return ConversionResult(
                    status="ok",
                    markdown=assembled,
                    converter=converter_stamp("fetchpdf-md", "jats" if kind == "xml" else "html"),
                    quality=text_quality_verdict(assembled),
                )
        except Exception:
            pass  # fall through to docling — a degraded conversion beats none

    if docling_available():
        try:
            md = _docling_markup(raw, kind)
            if md.strip():
                abstract, body = split_abstract(md)
                assembled = assemble_single_md(doc_title(md), "", abstract, body)
                return ConversionResult(
                    status="ok",
                    markdown=assembled,
                    converter=converter_stamp("docling", "jats" if kind == "xml" else "html"),
                    quality=text_quality_verdict(assembled),
                )
        except Exception:
            pass  # fall through to the floor — a degraded conversion beats none

    return html_to_text(raw, media_type)


def html_to_text(raw: bytes, media_type: str | None = None) -> ConversionResult:
    """The dependency-light HTML floor: trafilatura → readability → stdlib minimal (all optional
    except the last). Reached when docling is absent or its backend raised. The bytes are decoded by
    :func:`decode_text` (declared charset, detection, UTF-8)."""
    raw_text, _codec = decode_text(raw, media_type)

    # trafilatura → readability → stdlib minimal (all optional except the last).
    if importlib.util.find_spec("trafilatura") is not None:
        try:
            import trafilatura  # noqa: PLC0415

            extracted = trafilatura.extract(raw_text) or ""
            if extracted.strip():
                _, title = _html_min(raw_text)
                md = assemble_single_md(title, "", None, extracted.strip())
                return ConversionResult(
                    status="ok",
                    markdown=md,
                    converter=converter_stamp("trafilatura", "h"),
                    quality=text_quality_verdict(md),
                )
        except Exception:
            pass

    body, title = _html_min(raw_text)
    md = assemble_single_md(title, "", None, body) if (title or body) else "# Untitled\n"
    return ConversionResult(
        status="ok",
        markdown=md,
        converter="html-min@1+tierh",
        quality=text_quality_verdict(md),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Top-level dispatch
# ─────────────────────────────────────────────────────────────────────────────


def classify(media_type: str | None, raw: bytes) -> str:
    """``"pdf" | "xml" | "html" | "text"`` from the declared ``media_type`` then a magic-byte sniff.

    ``xml`` is split out from ``html`` so publisher JATS fulltext reaches docling's JATS backend
    rather than being parsed as HTML (it previously fell into the ``html`` bucket)."""
    mt = (media_type or "").lower()
    if "pdf" in mt:
        return "pdf"
    if "html" in mt:
        return "html"
    if "xml" in mt:
        return "xml"
    if mt.startswith("text/"):
        return "text"
    head = raw[:2048]
    if head[:5] == b"%PDF-" or head[:4] == b"%PDF":
        return "pdf"
    low = head.lstrip().lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html") or b"<html" in low:
        return "html"
    # JATS declares itself in the doctype/root element; check before the generic XML prolog so a
    # non-JATS XML payload does not claim the JATS backend.
    if b"//nlm//dtd jats" in low or b"<article" in low:
        return "xml"
    if low.startswith(b"<?xml"):
        return "xml"
    return "text"


def convert(
    raw: bytes, media_type: str | None, *, workdir: Path, origin: str | None = None, latexml: bool = False,
    pdf_raw: bool = False,
) -> ConversionResult:
    """Convert one snapshot's raw bytes → a :class:`ConversionResult`, dispatched by kind. The
    PDF path materializes a temp file (pymupdf/docling take a path); ``workdir`` is a caller-owned
    scratch dir (the memory-capped worker's tempdir). ``origin`` is the workflow that fetched the
    bytes (``snapshots.run_id`` → ``runs.workflow``): publisher HTML from ``fetch.oa`` goes to
    fetchpdf's converter, blog/forum HTML from ``fetch.http`` does not. ``latexml``: a LaTeXML page takes
    :func:`latexml_to_text` first (module docstring, LaTeXML without docling). ``pdf_raw``: a PDF
    takes the raw-text tier alone (:func:`convert_pdf` ``raw_only``)."""
    kind = classify(media_type, raw)
    if kind == "pdf":
        pdf_path = workdir / "input.pdf"
        pdf_path.write_bytes(raw)
        return convert_pdf(pdf_path, raw_only=pdf_raw)
    if kind in ("html", "xml"):
        return markup_to_text(raw, kind, scholarly=(origin == "fetch.oa"), latexml=latexml, media_type=media_type)
    # plain text — decode (BOM, a declared charset, UTF-8, detection) and store as-is (a mechanical
    # passthrough, still verdicted).
    text, _codec = decode_text(raw, media_type, markup=False)
    md = text if text.strip() else ""
    return ConversionResult(
        status="ok",
        markdown=md,
        converter="plain@1+tier0",
        quality=text_quality_verdict(md),
    )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false", "False")

