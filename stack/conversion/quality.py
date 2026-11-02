"""LLM-free text quality (round 3, Q): measured checks on every text ``acquire`` converts or takes
in-band, and the configurable gate that turns them into a bucket.

Two pure steps, so a gate change never needs the text again:

* :func:`measure` ``(text, facts) -> metrics``: threshold-free numbers (:data:`METRIC_FIELDS`, the
  ``metrics`` struct of the snapshot's ``quality``, ``stack/schemas/structs.py:QUALITY``);
* :func:`judge` ``(metrics, role, gate) -> Verdict``: each check (:data:`CHECKS`) compares one or
  more metrics with the gate's thresholds and fires at ``flag`` or ``fail`` level.

**Bucket** (``quality.bucket``, mirrored on ``content.current_text_quality`` for the current
text): ``failed`` when a check fires at fail level and the gate fails texts (``fail: true``, the
default); else ``flagged`` when any check fires; else ``ok``. A ``failed`` text is
``failed_permanent[quality_<reason>]`` on its snapshot (:data:`FAIL_CLASSES`; the first fail in
:data:`CHECKS` order names it) and the pointer does not use it (``current_text_status =
'convert_failed'``); its ``text`` stays so it can be reviewed and re-judged. A ``flagged``
text is used as usual; its ``flags`` say why (later refetch, review, LLM processing). Besides the
checks, ``flags`` can carry one provenance flag, ``article_in_press`` (:func:`in_press`: a publisher's
Article in Press; never fails, makes an ``ok`` text ``flagged``; ``acquire`` refetches it later).

**Thresholds** live in one place, :data:`THRESHOLDS`; a plan overrides any of them on the acquire
stage line, ``options: {quality: {fail: true|false, thresholds: {<name>: <number|null>}}}``
(:func:`gate_from_options`). ``null`` disables that level of that check; an unknown name is
refused. The gate's digest (:func:`Gate.digest`, over :data:`CHECKS_VERSION`, ``fail`` and the
effective thresholds) is stored with every verdict, so a changed gate re-judges stored metrics and
a changed :data:`CHECKS_VERSION` re-measures the text.

Every threshold below was set against the 595 current texts of the dev store
(``v4/store-dev`` head ``rak61rhkmnxw4.documents``, 2026-10-12): fail levels fire only on texts
that are not the document (empty, binary, challenge or error pages, landing pages, a reference
list); flag levels mark texts worth a second look. Calibration counts were in the 2026-10 run log (not in this repository).

Pure: no ``stack.*`` import (the conversion child or a report tool may load it alone).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

#: Bumped when a metric's definition changes: stored metrics of another version are re-measured.
#: 2 (round 4, P): the ``article_in_press`` provenance flag (:func:`in_press`), read from the text.
CHECKS_VERSION = 2

#: Text kinds :func:`kind_of` derives from a snapshot's media type.
KINDS: tuple[str, ...] = ("pdf", "html", "xml", "markdown", "text")

#: The ``quality.metrics`` fields in schema order (all nullable; ``None`` = not measurable here).
METRIC_FIELDS: tuple[str, ...] = (
    "chars", "words", "lines", "pages", "raw_bytes", "chars_per_page", "text_raw_ratio", "truncated",
    "replacement_per_1k", "mojibake_per_1k", "binary_ratio", "pua_ratio",
    "link_density", "link_line_ratio", "markup_ratio", "html_tags_per_1k",
    "table_rows", "table_bad_ratio", "latex_per_1k", "glyph_line_ratio", "glyph_runs_per_1k", "figure_text_ratio",
    "stub_hits", "repeated_line_ratio", "abstract_ratio", "body_sections", "landing_hits",
    "ref_line_ratio", "ref_start_ratio", "en_ratio", "non_latin_ratio",
)

# ---------------------------------------------------------------- thresholds (the one place)
#: Default gate thresholds. ``<check>.<level>`` (``.flag`` / ``.fail``), length checks per role
#: (``<check>.<role>.<level>``). Units as the metric they compare (module docstring).
THRESHOLDS: dict[str, float | None] = {
    # too_short: chars below. Roles: a paper body, a blog/web page, a forum post, an abstract.
    "too_short.full_text.fail": 1500, "too_short.full_text.flag": 8000,
    "too_short.page.fail": 200, "too_short.page.flag": 600,
    "too_short.post.fail": 200, "too_short.post.flag": 500,
    "too_short.readme.fail": 100, "too_short.readme.flag": 300,
    "too_short.abstract.fail": 100, "too_short.abstract.flag": 300,
    # sparse_pages: chars per PDF page below (from 3 pages on; a scan without OCR yields ~0).
    "sparse_pages.min_pages": 3, "sparse_pages.fail": 200, "sparse_pages.flag": 1200,
    # low_text_ratio: an HTML text this small against a raw page this large (flag only).
    "low_text_ratio.min_raw_bytes": 50_000, "low_text_ratio.max_chars": 3000, "low_text_ratio.flag": 0.004,
    # encoding: U+FFFD per 1,000 chars (v3 `corrupted` = 50); mojibake sequences per 1,000 chars.
    "replacement.fail": 20, "replacement.flag": 2,
    "mojibake.fail": 10, "mojibake.flag": 0.5,
    # binary: control/unassigned code points per char (v3 `corrupted` = 0.10); private-use glyphs.
    "binary.fail": 0.10, "binary.flag": 0.005,
    "private_use.flag": 0.002,
    # markup: share of chars that are URLs and tags; share of lines that are only a link.
    "markup.fail": 0.6, "markup.flag": 0.25,
    "link_lines.flag": 0.35,
    # table_broken: share of pipe-table rows whose cell count differs from the table's mode.
    "table_broken.min_rows": 6, "table_broken.flag": 0.2,
    # math_garbled: LaTeX commands outside math delimiters per 1,000 words; share of lines in a run
    # of single-glyph lines; inline runs of 9+ single glyphs (5+ letters or digits, so not a dot
    # leader) per 1,000 words.
    "latex_residue.flag": 15,
    "glyph_lines.flag": 0.05,
    "glyph_runs.flag": 2.0,
    # figure_text: share of chars inside pymupdf4llm's "picture text" blocks (text lifted out of
    # figures: axis ticks, legends).
    "figure_text.flag": 0.15,
    # stub: a stub/error phrase (access denied, JavaScript, cookie wall, challenge, 404, paywall,
    # server error) in a text shorter than this.
    "stub.fail": 2500, "stub.flag": 8000,
    # repeated_lines: share of lines repeating 3+ times (running headers and footers).
    "repeated_lines.flag": 0.15,
    # abstract_only (paper bodies): the text is at most this many times the metadata abstract, or
    # a landing page (2+ markers) shorter than `abstract_only.landing_chars` with no body section.
    "abstract_only.fail": 2.5, "abstract_only.flag": 5.0, "abstract_only.landing_chars": 12000,
    # references_only (paper bodies of 30+ lines): share of reference-like lines with the references
    # heading in the first fifth of the text (or none).
    "references_only.fail": 0.5, "references_only.flag": 0.3, "references_only.max_start": 0.2,
    "references_only.min_lines": 30,
    # non_english: English function words per word below, from 100 words on; non-Latin letters.
    "non_english.min_words": 100, "non_english.flag": 0.08, "non_latin.flag": 0.3,
}

#: Checks in verdict order (the first fail names the error class). ``roles``: where the check
#: applies (``None`` = every role).
class Check(NamedTuple):
    name: str
    roles: frozenset[str] | None
    can_fail: bool


_PAPER = frozenset({"full_text"})
CHECKS: tuple[Check, ...] = (
    Check("binary", None, True),
    Check("replacement", None, True),
    Check("mojibake", None, True),
    Check("stub", None, True),
    Check("too_short", None, True),
    Check("sparse_pages", None, True),
    Check("references_only", _PAPER, True),
    Check("abstract_only", _PAPER, True),
    Check("markup", None, True),
    Check("low_text_ratio", None, False),
    Check("truncated", None, False),
    Check("private_use", None, False),
    Check("link_lines", None, False),
    Check("table_broken", None, False),
    Check("latex_residue", None, False),
    Check("glyph_lines", None, False),
    Check("glyph_runs", None, False),
    Check("figure_text", None, False),
    Check("repeated_lines", None, False),
    Check("non_english", None, False),
    Check("non_latin", None, False),
)

#: The ``failed_permanent`` error class of each failing check (``stack/schemas/vocab.py``).
FAIL_CLASSES: dict[str, str] = {
    "binary": "quality_garbled", "replacement": "quality_garbled", "mojibake": "quality_garbled",
    "stub": "quality_stub", "too_short": "quality_too_short", "sparse_pages": "quality_too_short",
    "references_only": "quality_references_only", "abstract_only": "quality_abstract_only",
    "markup": "quality_markup",
}

BUCKETS: tuple[str, ...] = ("ok", "flagged", "failed")


# ---------------------------------------------------------------- facts, gate, verdict
class Facts(NamedTuple):
    """What :func:`measure` knows besides the text. ``role``: the snapshot's ``content_role``;
    ``kind``: :data:`KINDS`; ``raw_bytes``: the raw blob's size; ``pages``: the PDF's page count
    (from the conversion child; ``None`` when unknown); ``truncated``: the converter's own
    truncation signal (``None`` when it gave none); ``abstract``: the document's metadata abstract."""

    role: str
    kind: str
    raw_bytes: int | None = None
    pages: int | None = None
    truncated: bool | None = None
    abstract: str | None = None


class Gate(NamedTuple):
    """The effective gate: :data:`THRESHOLDS` with the plan's overrides, and whether a fail-level
    check fails the text (``fail``) or only flags it."""

    thresholds: Mapping[str, float | None]
    fail: bool = True

    def digest(self) -> str:
        doc = {"checks_version": CHECKS_VERSION, "fail": self.fail,
               "thresholds": {k: self.thresholds[k] for k in sorted(self.thresholds)}}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Verdict(NamedTuple):
    """``bucket`` (:data:`BUCKETS`); ``flags``: every check that fired, either level, in
    :data:`CHECKS` order; ``fails``: those that fired at fail level (also recorded when the gate
    does not fail texts); ``error_class``: the failing text's class, else ``None``."""

    bucket: str
    flags: list[str]
    fails: list[str]
    error_class: str | None


DEFAULT_GATE = Gate(dict(THRESHOLDS), True)


def gate_from_options(options: Mapping[str, Any] | None) -> Gate:
    """The gate of an acquire stage line's ``options.quality`` (module docstring). Raises
    ``ValueError`` on an unknown threshold name or a non-numeric value."""
    q = (options or {}).get("quality")
    if q is None:
        q = {}
    if not isinstance(q, Mapping):
        raise ValueError(f"options.quality must be a mapping, got {q!r}")  # noqa: TRY004
    unknown = set(q) - {"fail", "thresholds"}
    if unknown:
        raise ValueError(f"options.quality: unknown key(s) {sorted(unknown)} (fail, thresholds)")
    fail = q.get("fail", True)
    if not isinstance(fail, bool):
        raise ValueError(f"options.quality.fail must be a bool, got {fail!r}")  # noqa: TRY004
    over = q.get("thresholds") or {}
    bad = sorted(set(over) - set(THRESHOLDS))
    if bad:
        raise ValueError(f"options.quality.thresholds: unknown name(s) {bad}")
    for k, v in over.items():
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ValueError(f"options.quality.thresholds.{k} must be a number or null, got {v!r}")
    return Gate({**THRESHOLDS, **over}, fail)


def kind_of(media_type: str | None) -> str:
    """The text kind of a snapshot's media type (``pdf``, ``html``, ``xml``, ``markdown``, ``text``)."""
    m = (media_type or "").split(";")[0].strip().lower()
    if m == "application/pdf":
        return "pdf"
    if m in ("text/html", "application/xhtml+xml"):
        return "html"
    if m.endswith("xml"):
        return "xml"
    if m in ("text/markdown", "text/x-markdown"):
        return "markdown"
    return "text"


# ---------------------------------------------------------------- measure
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
_MOJIBAKE = re.compile(
    "Ã[\u0080-¿ŒœŠšŸŽž‘-„†-•…‰‹"
    "›€™]"
    "|â€[\u0080-¿ŒœŠšŽž˜‘-„†-•…‰‹"
    "›€™]"
    "|Â[ -¿]")
_MD_LINK = re.compile(r"!?\[([^\]\n]{0,500})\]\(([^)\s]{0,2000})(?:\s[^)\n]{0,300})?\)")
_BARE_URL = re.compile(r"(?<![(\[<\"'])\bhttps?://[^\s)\]>\"']+")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>\n]{0,500})?/?>")
_LIST_MARK = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
_MATH = re.compile(r"\$\$.+?\$\$|\$[^$\n]+?\$|\\\(.+?\\\)|\\\[.+?\\\]", re.DOTALL)
_LATEX_CMD = re.compile(r"\\[A-Za-z]{2,}")
_GLYPH_RUN = re.compile(r"(?:(?<!\S)\S[ \t]){8,}\S(?!\S)")
_ALNUM = re.compile(r"[^\W_]")
_FIGURE_TEXT = re.compile(r"<!--\s*Start of picture text\s*-->.*?<!--\s*End of picture text\s*-->", re.DOTALL | re.IGNORECASE)
_REF_HEADING = re.compile(
    r"^[ \t#*]*(?:\d+\.?[ \t]*)?(?:references|bibliography|works cited|literature cited|reference list)[ \t*:]*$",
    re.IGNORECASE | re.MULTILINE)
_YEAR = re.compile(r"\b(?:19[5-9]\d|20[0-4]\d)[a-z]?\b")
_REF_START = re.compile(r"^\s*(?:[-*]\s*)?(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\.\s+\S|\d{1,3}\s+[A-Z])")
_REF_MARK = re.compile(r"\bet al\.|\bdoi\b|doi\.org|arxiv[:.]|\bin proceedings\b|\bproc\.\s|\bjournal of\b|"
                       r"\bvol\.\s*\d|\bpp\.\s*\d|\bpreprint\b", re.IGNORECASE)
_BODY_SECTION = re.compile(
    r"^[ \t#*]*(?:(?:[IVX]+|\d+(?:\.\d+)*)\.?[ \t]+)?\**[ \t]*(?:introduction|background|related work|methods?|methodology|"
    r"approach|experiments?|experimental setup|results|discussion|conclusions?|evaluation|limitations|analysis)\b",
    re.IGNORECASE | re.MULTILINE)
_LANDING = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"submission history", r"\bview pdf\b", r"\bdownload pdf\b", r"\bcite as:", r"bibliographic tools",
    r"\bsubjects:\s", r"full[- ]text links", r"export citation", r"rdf:rdf", r"access (?:the )?paper",
    r"\bcomments:\s", r"view a pdf of the paper", r"\bhtml \(experimental\)"))
STUB_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {cat: tuple(re.compile(p, re.IGNORECASE) for p in pats) for cat, pats in {
    "access_denied": (r"\baccess denied\b", r"\b403 forbidden\b", r"you don'?t have permission to access",
                      r"you do not have (?:permission|access) to", r"\brequest (?:was )?blocked\b", r"\berror 403\b"),
    "javascript": (r"enable javascript", r"javascript (?:is )?(?:disabled|required)", r"requires javascript",
                   r"turn on javascript", r"javascript must be enabled", r"browser (?:does not|doesn't) support javascript"),
    "cookie_wall": (r"(?:accept|allow) (?:all )?cookies", r"we use cookies", r"consent to (?:the use of )?cookies",
                    r"cookie (?:settings|preferences)"),
    "challenge": (r"checking (?:if the site connection is secure|your browser)", r"^\s*just a moment\.\.\.",
                  r"verify(?:ing)? (?:that )?you are (?:a )?human", r"attention required!? \| cloudflare",
                  r"\bddos protection by\b", r"are you a robot", r"\bcomplete the captcha\b", r"\bcloudflare ray id\b",
                  r"performance (?:&|and) security by cloudflare"),
    "not_found": (r"\b404\b[^\n]{0,40}not found", r"\bpage not found\b",
                  r"(?:page|content) you (?:are|were) looking for (?:could not|cannot|can't|doesn't|does not|no longer)",
                  r"this page (?:does not|doesn't) exist", r"the requested url was not found"),
    "paywall": (r"subscribe (?:now )?to (?:continue|read|unlock)", r"\bsubscribers? only\b",
                r"(?:sign|log) in to (?:read|continue|view the full)", r"purchase (?:this article|access|a subscription)",
                r"\bbuy this article\b", r"\brent this article\b", r"access through your institution",
                r"\balready a subscriber\b", r"to read the full (?:article|story|text)"),
    "server_error": (r"\binternal server error\b", r"\b503 service unavailable\b", r"\b502 bad gateway\b",
                     r"\bsomething went wrong\b", r"\bunexpected error has occurred\b"),
}.items()}
_EN_WORDS = frozenset(
    ["the", "of", "and", "to", "in", "a", "is", "that", "for", "it", "as", "with", "on", "be", "by", "this", "are", "we", "an", "not", "or", "from", "at", "which", "can", "have", "has", "but", "our", "their", "these", "its", "was", "were", "will", "more", "than", "also", "such", "they", "been", "other", "into", "would", "there", "may"])


def _per(n: float, d: float, scale: float = 1.0) -> float | None:
    return None if d <= 0 else round(n * scale / d, 6)


_ASCII_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_ASCII_LETTERS = bytes(range(65, 91)) + bytes(range(97, 123))


def _char_classes(text: str) -> tuple[int, int, int, int]:
    """``(binary, private_use, letters, non_latin_letters)`` code point counts (tab/LF/CR are text).
    ASCII is counted in bulk; only non-ASCII code points are looked up one by one."""
    binary = len(_ASCII_CONTROL.findall(text))
    ascii_bytes = text.encode("ascii", "ignore")
    letters = len(ascii_bytes) - len(ascii_bytes.translate(None, _ASCII_LETTERS))
    pua = non_latin = 0
    for ch in _NON_ASCII.findall(text):
        o = ord(ch)
        cat = unicodedata.category(ch)
        if cat == "Co":
            pua += 1
        elif cat in ("Cc", "Cs", "Cn"):
            binary += 1
        elif cat.startswith("L"):
            letters += 1
            if o > 0x24F and not 0x1E00 <= o <= 0x1EFF and not 0x2C60 <= o <= 0x2C7F and not 0xA720 <= o <= 0xA7FF:
                non_latin += 1
    return binary, pua, letters, non_latin


def _tables(lines: Sequence[str]) -> tuple[int, int]:
    """``(rows, bad rows)`` over pipe tables (2+ consecutive lines starting with ``|``): a row is
    bad when its unescaped pipe count differs from its table's most common count."""
    rows = bad = 0
    block: list[int] = []

    def close() -> None:
        nonlocal rows, bad
        if len(block) >= 2:
            mode = Counter(block).most_common(1)[0][0]
            rows += len(block)
            bad += sum(1 for n in block if n != mode)
        block.clear()

    for line in lines:
        s = line.strip()
        if s.startswith("|"):
            block.append(len(re.findall(r"(?<!\\)\|", s)))
        else:
            close()
    close()
    return rows, bad


def _glyph_lines(lines: Sequence[str]) -> int:
    """Non-empty lines inside runs of 4+ consecutive lines of at most 2 non-space chars (maths or
    a table laid out one glyph per line)."""
    n = run = 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if len(s) <= 2:
            run += 1
            continue
        if run >= 4:
            n += run
        run = 0
    return n + (run if run >= 4 else 0)


def _norm_line(s: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"[\s*_#|]+", " ", s)).strip().lower()


def _repeated_lines(lines: Sequence[str]) -> int:
    """Lines (8+ chars with letters, not table rows) whose digit-folded form occurs 3+ times."""
    keys = [_norm_line(s) for s in lines if s.strip() and not s.lstrip().startswith("|")]
    keys = [k for k in keys if len(k) >= 8 and re.search(r"[^\W\d_]{3}", k)]
    c = Counter(keys)
    return sum(n for n in c.values() if n >= 3)


def _is_ref_line(s: str) -> bool:
    return bool(_YEAR.search(s)) and bool(_REF_START.match(s) or _REF_MARK.search(s))


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def measure(text: str, facts: Facts) -> dict[str, Any]:
    """The metrics of ``text`` (:data:`METRIC_FIELDS`; module docstring). Pure."""
    stripped = text.strip()
    chars = len(stripped)
    lines = stripped.splitlines()
    nonempty = [s for s in lines if s.strip()]
    words_list = _WORD.findall(stripped)
    words = len(words_list)
    binary, pua, letters, non_latin = _char_classes(stripped)
    links = _MD_LINK.findall(stripped)
    link_url_chars = sum(len(u) + 4 for _, u in links)  # url + the []() syntax
    bare = _BARE_URL.findall(_MD_LINK.sub(" ", stripped))
    tags = _HTML_TAG.findall(stripped)
    link_lines = 0
    for s in nonempty:
        body = _LIST_MARK.sub("", s.strip())
        if not body:
            continue
        covered = sum(len(m.group(0)) for m in _MD_LINK.finditer(body))
        if covered >= 0.8 * len(body):
            link_lines += 1
    table_rows, table_bad = _tables(lines)
    no_math = _MATH.sub(" ", stripped)
    glyph_lines = _glyph_lines(lines)
    stubs = sorted(cat for cat, pats in STUB_PATTERNS.items() if any(p.search(stripped) for p in pats))
    ref_lines = sum(1 for s in nonempty if len(s.strip()) >= 20 and _is_ref_line(s))
    long_lines = sum(1 for s in nonempty if len(s.strip()) >= 20)
    ref_heads = list(_REF_HEADING.finditer(stripped))
    abstract_ratio: float | None = None
    if facts.abstract and len(facts.abstract.strip()) >= 200:
        a = _squash(facts.abstract)
        probe = a[40:160] if len(a) >= 200 else a[:120]
        if probe and probe in _squash(stripped):
            abstract_ratio = round(chars / len(facts.abstract.strip()), 4)
    lowered = [w.lower() for w in words_list]
    pages = facts.pages if facts.pages and facts.pages > 0 else None
    raw_bytes = facts.raw_bytes if facts.raw_bytes and facts.raw_bytes > 0 else None
    return {
        "chars": chars,
        "words": words,
        "lines": len(nonempty),
        "pages": pages,
        "raw_bytes": raw_bytes,
        "chars_per_page": _per(chars, pages or 0),
        "text_raw_ratio": _per(chars, raw_bytes or 0) if facts.kind == "html" else None,
        "truncated": facts.truncated,
        "replacement_per_1k": _per(stripped.count("�"), chars, 1000),
        "mojibake_per_1k": _per(len(_MOJIBAKE.findall(stripped)), chars, 1000),
        "binary_ratio": _per(binary, chars),
        "pua_ratio": _per(pua, chars),
        "link_density": _per(len(links), words, 100),
        "link_line_ratio": _per(link_lines, len(nonempty)),
        "markup_ratio": _per(link_url_chars + sum(map(len, bare)) + sum(map(len, tags)), chars),
        "html_tags_per_1k": _per(len(tags), words, 1000),
        "table_rows": table_rows,
        "table_bad_ratio": _per(table_bad, table_rows),
        "latex_per_1k": _per(len(_LATEX_CMD.findall(no_math)), words, 1000),
        "glyph_line_ratio": _per(glyph_lines, len(nonempty)),
        "glyph_runs_per_1k": _per(sum(1 for g in _GLYPH_RUN.findall(stripped) if len(_ALNUM.findall(g)) >= 5),
                                  words, 1000),
        "figure_text_ratio": _per(sum(len(f) for f in _FIGURE_TEXT.findall(stripped)), chars),
        "stub_hits": stubs,
        "repeated_line_ratio": _per(_repeated_lines(lines), len(nonempty)),
        "abstract_ratio": abstract_ratio,
        "body_sections": len(_BODY_SECTION.findall(stripped)),
        "landing_hits": sum(1 for p in _LANDING if p.search(stripped)),
        "ref_line_ratio": _per(ref_lines, long_lines),
        "ref_start_ratio": _per(ref_heads[-1].start(), chars) if ref_heads else None,
        "en_ratio": _per(sum(1 for w in lowered if w in _EN_WORDS), words),
        "non_latin_ratio": _per(non_latin, letters),
    }


# ---------------------------------------------------------------- judge
def _level(value: float | None, gate: Gate, check: str, *, below: bool, role: str | None = None) -> str | None:
    """``'fail'`` / ``'flag'`` / ``None`` for one metric against ``<check>[.<role>].<level>``
    (``below``: fires when the value is below the threshold, else above)."""
    if value is None:
        return None
    prefix = f"{check}.{role}" if role else check
    for level in ("fail", "flag"):
        t = gate.thresholds.get(f"{prefix}.{level}")
        if t is not None and (value < t if below else value > t):
            return level
    return None


def _fires(check: str, m: Mapping[str, Any], role: str, gate: Gate) -> str | None:
    t = gate.thresholds
    if check == "binary":
        return _level(m["binary_ratio"], gate, check, below=False)
    if check == "replacement":
        return _level(m["replacement_per_1k"], gate, check, below=False)
    if check == "mojibake":
        return _level(m["mojibake_per_1k"], gate, check, below=False)
    if check == "stub":
        if not m["stub_hits"]:
            return None
        return _level(m["chars"], gate, check, below=True)
    if check == "too_short":
        return _level(m["chars"], gate, check, below=True, role=role)
    if check == "sparse_pages":
        if m["pages"] is None or m["pages"] < (t.get("sparse_pages.min_pages") or 0):
            return None
        return _level(m["chars_per_page"], gate, check, below=True)
    if check == "references_only":
        start = m["ref_start_ratio"]
        if start is not None and start > (t.get("references_only.max_start") or 0):
            return None
        if (m["lines"] or 0) < (t.get("references_only.min_lines") or 0):
            return None
        return _level(m["ref_line_ratio"], gate, check, below=False)
    if check == "abstract_only":
        level = _level(m["abstract_ratio"], gate, check, below=True)
        landing_max = t.get("abstract_only.landing_chars")
        if (level is None and landing_max is not None and m["landing_hits"] >= 2 and m["chars"] < landing_max
                and m["body_sections"] == 0):
            level = "fail" if t.get("abstract_only.fail") is not None else (
                "flag" if t.get("abstract_only.flag") is not None else None)
        return level
    if check == "markup":
        return _level(m["markup_ratio"], gate, check, below=False)
    if check == "low_text_ratio":
        if m["raw_bytes"] is None or m["raw_bytes"] < (t.get("low_text_ratio.min_raw_bytes") or 0):
            return None
        if m["chars"] >= (t.get("low_text_ratio.max_chars") or float("inf")):
            return None
        return _level(m["text_raw_ratio"], gate, check, below=True)
    if check == "truncated":
        return "flag" if m["truncated"] else None
    if check == "private_use":
        return _level(m["pua_ratio"], gate, check, below=False)
    if check == "link_lines":
        return _level(m["link_line_ratio"], gate, check, below=False)
    if check == "table_broken":
        if (m["table_rows"] or 0) < (t.get("table_broken.min_rows") or 0):
            return None
        return _level(m["table_bad_ratio"], gate, check, below=False)
    if check == "latex_residue":
        return _level(m["latex_per_1k"], gate, check, below=False)
    if check == "glyph_lines":
        return _level(m["glyph_line_ratio"], gate, check, below=False)
    if check == "glyph_runs":
        return _level(m["glyph_runs_per_1k"], gate, check, below=False)
    if check == "figure_text":
        return _level(m["figure_text_ratio"], gate, check, below=False)
    if check == "repeated_lines":
        return _level(m["repeated_line_ratio"], gate, check, below=False)
    if check == "non_english":
        if (m["words"] or 0) < (t.get("non_english.min_words") or 0):
            return None
        return _level(m["en_ratio"], gate, check, below=True)
    if check == "non_latin":
        return _level(m["non_latin_ratio"], gate, check, below=False)
    raise KeyError(check)


def judge(metrics: Mapping[str, Any], role: str, gate: Gate = DEFAULT_GATE) -> Verdict:
    """The :class:`Verdict` of ``metrics`` for a text in ``role`` under ``gate``. Pure."""
    flags: list[str] = []
    fails: list[str] = []
    for check in CHECKS:
        if check.roles is not None and role not in check.roles:
            continue
        level = _fires(check.name, metrics, role, gate)
        if level is None:
            continue
        flags.append(check.name)
        if level == "fail" and check.can_fail:
            fails.append(check.name)
    if fails and gate.fail:
        return Verdict("failed", flags, fails, FAIL_CLASSES[fails[0]])
    return Verdict("flagged" if flags else "ok", flags, fails, None)


def assess(text: str, facts: Facts, gate: Gate = DEFAULT_GATE) -> tuple[dict[str, Any], Verdict]:
    """:func:`measure` then :func:`judge`."""
    metrics = measure(text, facts)
    return metrics, judge(metrics, facts.role, gate)


# ---------------------------------------------------------------- provenance: Article in Press
#: A **provenance flag**, not a check (round 4, P; D206): the text is a publisher's Article in Press
#: (accepted, shared before the version of record). It is a complete article, so it never fails;
#: :func:`with_in_press` adds it to ``flags`` (bucket ``flagged``: "used, but look again"), and
#: ``acquire`` makes the role that fetched it due again on ``acquire.recheck.article_in_press``
#: (:mod:`workflows.acquire.quality`). No metric holds it (``QUALITY_METRICS`` is a fixed struct):
#: a re-judge from stored metrics keeps the flag of the earlier verdict of the same text.
ARTICLE_IN_PRESS = "article_in_press"
#: Characters of a text taken as its first page for :data:`_IN_PRESS_TEXT`.
IN_PRESS_HEAD_CHARS = 5000
#: First-page phrases, case-sensitive: Springer Nature's banner ("ARTICLE | Article in Press", "This
#: Article in Press is shared early"), Elsevier's running head ("ARTICLE IN PRESS") and pre-proof
#: cover ("Journal Pre-proof"). A citation's "(in press)" does not match.
_IN_PRESS_TEXT = re.compile(r"\bArticle in Press\b|\bARTICLE IN PRESS\b|\bJournal Pre-proof\b")
#: Nature's Article-in-Press PDF (``www.nature.com/articles/<id>_reference.pdf``; dev store docs
#: ``sbkp3dcezxtvn``, ``scvp70eneedfj``); the version of record is ``…/articles/<id>.pdf``.
_IN_PRESS_URL = re.compile(r"^https?://(?:[a-z0-9-]+\.)*nature\.com/articles/([^/?#]+)_reference\.pdf(?:[?#].*)?$",
                           re.IGNORECASE)


def in_press_url(url: str | None) -> bool:
    """``url`` is a known Article-in-Press PDF location (:data:`_IN_PRESS_URL`)."""
    return bool(url) and _IN_PRESS_URL.match(str(url)) is not None


def version_of_record_url(url: str | None) -> str | None:
    """The version-of-record PDF URL of an Article-in-Press ``url`` (Nature: ``<id>_reference.pdf``
    → ``<id>.pdf``), else ``None``."""
    m = _IN_PRESS_URL.match(str(url or ""))
    if m is None:
        return None
    head = str(url)[:m.end(1)]
    return head + ".pdf"


def in_press(text: str | None, role: str, urls: Sequence[str | None] = ()) -> bool:
    """The text is an Article in Press: one of ``urls`` (the snapshot's ``url``/``final_url``) is an
    Article-in-Press location, or (a paper body, role ``full_text``) its first
    :data:`IN_PRESS_HEAD_CHARS` characters carry an Article-in-Press phrase. Pure."""
    if any(in_press_url(u) for u in urls):
        return True
    return role in _PAPER and text is not None and _IN_PRESS_TEXT.search(text[:IN_PRESS_HEAD_CHARS]) is not None


def with_in_press(verdict: Verdict) -> Verdict:
    """``verdict`` with :data:`ARTICLE_IN_PRESS` in ``flags``; an ``ok`` bucket becomes ``flagged``,
    a ``failed`` one stays failed (the flag never fails a text)."""
    if ARTICLE_IN_PRESS in verdict.flags:
        return verdict
    bucket = "flagged" if verdict.bucket == "ok" else verdict.bucket
    return verdict._replace(bucket=bucket, flags=[*verdict.flags, ARTICLE_IN_PRESS])


# ---------------------------------------------------------------- structure markers (round 5, T-RUN B8)
_MATH_DISPLAY = re.compile(r"\$\$.+?\$\$", re.DOTALL)
_MATH_INLINE = re.compile(r"(?<![\\$])\$[^$\n]+?\$(?!\$)")
_TABLE_HTML = re.compile(r"<table[\s>]", re.IGNORECASE)
_TABLE_PIPE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.MULTILINE)


def structure_markers(text: str) -> dict[str, int]:
    """Counts of what a structured source keeps and a PDF text usually loses: ``math`` (``$$…$$``
    display and ``$…$`` inline formulas, TeX as :func:`stack.conversion.ladder.rewrite_mathml` writes
    it) and ``tables`` (inline-HTML ``<table>`` blocks plus Markdown pipe tables, by their separator
    row). A comparison aid for ``acquire``'s structured-vs-PDF counts, not a check: a ``$`` price can
    count as a formula. Pure."""
    display = len(_MATH_DISPLAY.findall(text))
    inline = len(_MATH_INLINE.findall(_MATH_DISPLAY.sub(" ", text)))
    return {"math": display + inline, "tables": len(_TABLE_HTML.findall(text)) + len(_TABLE_PIPE_SEP.findall(text))}


__all__ = [
    "ARTICLE_IN_PRESS",
    "BUCKETS",
    "CHECKS",
    "CHECKS_VERSION",
    "DEFAULT_GATE",
    "FAIL_CLASSES",
    "KINDS",
    "METRIC_FIELDS",
    "STUB_PATTERNS",
    "THRESHOLDS",
    "Facts",
    "Gate",
    "Verdict",
    "assess",
    "gate_from_options",
    "in_press",
    "in_press_url",
    "judge",
    "kind_of",
    "measure",
    "structure_markers",
    "version_of_record_url",
    "with_in_press",
]
