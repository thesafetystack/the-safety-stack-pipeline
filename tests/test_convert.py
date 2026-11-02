"""Tests for the conversion ladder (``stack/conversion/ladder.py``) that ``acquire``'s ``convert``
substage runs in its RLIMIT_DATA-capped child (10-design §5.3; 20-technical §11).

The pure part of v3's ``tests/test_convert.py`` (the orchestration tests over py-pglite went with
``workflows/convert/docling/main.py``; ``tests/test_acquire_convert.py`` covers the substage):

* **Pure ladder logic** (``ladder.py``): markdown post-processing pins (ported from v1
  ``test_pdf_to_markdown.py``), the tier fallback ordering (docling/pymupdf mocked — these never
  import torch), the silent-truncation gate decision function, the mechanical ``text_quality``
  thresholds, and the HTML light path (stdlib extractor).
* **The child**: a real worker subprocess that aborts is detected as ``crashed``
  (:func:`workflows.acquire.convert.convert_one`).

The heavy tiers are monkeypatched or importorskip'd, so the suite passes with OR without the
``convert`` dependency group (10-design §5.3: docling is optional/import-guarded).
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

from workflows.acquire.convert import convert_one

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, wf_dir: pathlib.Path) -> Any:
    """Import a module by path (a fresh copy the tests may monkeypatch)."""
    spec = importlib.util.spec_from_file_location(f"_convert_under_test_{name}", wf_dir / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The ladder the convert child runs is stack/conversion/ladder.py (the workflows/ copy was a dead
# duplicate, retired at T1.8).
ladder = _load("ladder", REPO / "stack" / "conversion")


# ═════════════════════════════════════════════════════════════════════════════
# Pure ladder logic
# ═════════════════════════════════════════════════════════════════════════════

RAW_MD = """# A Study of Things

Author One, Author Two

## Abstract

We study the things and find them interesting.

## 1 Introduction

Things have long been studied.

## References

- Prior work on things.
"""


# ── markdown post-processing (pins ported from v1 test_pdf_to_markdown.py) ────


def test_split_abstract_heading() -> None:
    abstract, body = ladder.split_abstract(RAW_MD)
    assert abstract == "We study the things and find them interesting."
    assert "## 1 Introduction" in body
    assert "## References" in body
    assert "We study the things" not in body


def test_split_abstract_no_heading_uses_second_paragraph() -> None:
    md = "# Title\n\nAuthors here\n\nThis reads like an abstract.\n\nBody text."
    abstract, body = ladder.split_abstract(md)
    assert abstract == "This reads like an abstract."
    assert "Body text." in body


def test_split_abstract_runs_to_end() -> None:
    md = "# Title\n\n## Abstract\n\nOnly an abstract, nothing else."
    abstract, body = ladder.split_abstract(md)
    assert abstract == "Only an abstract, nothing else."
    assert body == "# Title"  # pre-abstract lines stay in the body


def test_split_abstract_empty_body_falls_back_to_full_text() -> None:
    md = "## Abstract\n\nOnly an abstract, nothing else."
    abstract, body = ladder.split_abstract(md)
    assert abstract == "Only an abstract, nothing else."
    assert body == md


def test_doc_title() -> None:
    assert ladder.doc_title(RAW_MD) == "A Study of Things"
    assert ladder.doc_title("no headings at all") == ""
    assert ladder.doc_title("## A Fancy Title\n\ntext") == "A Fancy Title"
    assert ladder.doc_title("## Abstract\n\ntext\n\n## Real Section") == ""


def test_assemble_single_md_shape() -> None:
    md = ladder.assemble_single_md("A Title", "10.1000/x", "The abstract.", "## Body\n\nText.")
    assert md.startswith("# A Title\n")
    assert "**DOI:** 10.1000/x" in md
    assert md.index("## Abstract") < md.index("## Body")
    assert md.endswith("\n") and not md.endswith("\n\n")
    assert md.count("## Abstract") == 1  # no duplicated section (the pdf4llm bug the port fixed)


def test_assemble_single_md_minimal() -> None:
    md = ladder.assemble_single_md("", "", None, "just text")
    assert md.startswith("# Untitled\n")
    assert "**DOI:**" not in md
    assert "## Abstract" not in md


# ── span-resolving table rendering (pure — no docling import) ────────────────
#
# The corpus sweep that motivated these: 32.4% of 3,418 tables in a random 500-file sample were
# structurally degraded by docling's markdown exporter dropping colspan/rowspan, while the VALUES
# were fine (5 of ~10k numeric cells missing). Degraded-but-valid markdown passes every gate —
# text_quality reads `complete`, an I5 quote re-anchors cleanly — so it publishes wrong answers
# with an intact provenance chain. Hence: pin the rendering.


def _cell(text: str, r0: int, r1: int, c0: int, c1: int, **kw: Any) -> dict:
    return {"text": text, "r0": r0, "r1": r1, "c0": c0, "c1": c1, **kw}


# | Method(rowspan 2) | Fine-pruning(colspan 2) | Fine-mixing(colspan 2) |
# |                   | ACC       | ASR         | ACC        | ASR       |
TWO_LEVEL_HEADER = [
    _cell("Method", 0, 2, 0, 1, column_header=True),
    _cell("Fine-pruning", 0, 1, 1, 3, column_header=True),
    _cell("Fine-mixing", 0, 1, 3, 5, column_header=True),
    _cell("ACC", 1, 2, 1, 2, column_header=True),
    _cell("ASR", 1, 2, 2, 3, column_header=True),
    _cell("ACC", 1, 2, 3, 4, column_header=True),
    _cell("ASR", 1, 2, 4, 5, column_header=True),
    _cell("Trigger Word", 2, 3, 0, 1),
    _cell("90.02", 2, 3, 1, 2),
    _cell("100.0", 2, 3, 2, 3),
    _cell("89.45", 2, 3, 3, 4),
    _cell("14.19", 2, 3, 4, 5),
]


def test_multilevel_header_collapses_to_one_row() -> None:
    md = ladder.grid_to_markdown(TWO_LEVEL_HEADER, 3, 5)
    assert md is not None
    lines = md.split("\n")
    # One header row, one separator, one body row — NOT the exporter's duplicate-then-leak shape.
    assert len(lines) == 3
    assert lines[0] == (
        "| Method | Fine-pruning · ACC | Fine-pruning · ASR "
        "| Fine-mixing · ACC | Fine-mixing · ASR |"
    )
    assert lines[2] == "| Trigger Word | 90.02 | 100.0 | 89.45 | 14.19 |"


def test_rowspan_header_label_not_duplicated_into_itself() -> None:
    """``Method`` spans both header rows; the join must not yield ``Method · Method``."""
    md = ladder.grid_to_markdown(TWO_LEVEL_HEADER, 3, 5)
    assert md is not None and "Method · Method" not in md
    assert md.split("\n")[0].startswith("| Method |")


def test_rowspan_body_value_repeats_on_every_covered_row() -> None:
    """The lost-group-key case: ``Benign`` spans three attack rows. Repeating it makes each row
    independently readable, so a model cannot silently carry a group label across a boundary."""
    cells = [
        _cell("Attack", 0, 1, 0, 1, column_header=True),
        _cell("Defence", 0, 1, 1, 2, column_header=True),
        _cell("CACC", 0, 1, 2, 3, column_header=True),
        _cell("Benign", 1, 4, 0, 1),
        _cell("RTT", 1, 2, 1, 2), _cell("89.2", 1, 2, 2, 3),
        _cell("ONION", 2, 3, 1, 2), _cell("91.1", 2, 3, 2, 3),
        _cell("IMBERT", 3, 4, 1, 2), _cell("91.3", 3, 4, 2, 3),
        _cell("BadNet", 4, 5, 0, 1),
        _cell("RTT", 4, 5, 1, 2), _cell("89.1", 4, 5, 2, 3),
    ]
    md = ladder.grid_to_markdown(cells, 5, 3)
    assert md is not None
    assert "| Benign | RTT | 89.2 |" in md
    assert "| Benign | ONION | 91.1 |" in md
    assert "| Benign | IMBERT | 91.3 |" in md
    assert "| BadNet | RTT | 89.1 |" in md
    assert "|  |" not in md  # no orphaned rows with an empty group key


def test_row_section_divider_splits_the_table() -> None:
    """A full-width ``row_section`` row is a heading, not data (arxiv.2404.13076 repeats
    "No Fine-Tuning" across every column) — each section gets its own table."""
    cells = [
        _cell("Evaluator", 0, 1, 0, 1, column_header=True),
        _cell("Score", 0, 1, 1, 2, column_header=True),
        _cell("No Fine-Tuning", 1, 2, 0, 2, row_section=True),
        _cell("GPT-4", 2, 3, 0, 1), _cell("0.5", 2, 3, 1, 2),
        _cell("Fine-Tuned", 3, 4, 0, 2, row_section=True),
        _cell("GPT-4", 4, 5, 0, 1), _cell("0.71", 4, 5, 1, 2),
    ]
    md = ladder.grid_to_markdown(cells, 5, 2)
    assert md is not None
    assert "**No Fine-Tuning**" in md and "**Fine-Tuned**" in md
    assert md.count("| Evaluator | Score |") == 2  # header repeated per section
    assert md.index("**No Fine-Tuning**") < md.index("| GPT-4 | 0.5 |")
    assert md.index("**Fine-Tuned**") < md.index("| GPT-4 | 0.71 |")


def test_pipe_in_cell_is_escaped() -> None:
    md = ladder.grid_to_markdown(
        [_cell("a|b", 0, 1, 0, 1, column_header=True), _cell("x|y", 1, 2, 0, 1)], 2, 1
    )
    assert md is not None
    assert r"| a\|b |" in md and r"| x\|y |" in md


def test_toc_is_not_a_table() -> None:
    """An appendix ToC typeset as a tabular. Counted per ROW, not per cell: a 3x3 ToC is only
    3/9 leader cells but 3/3 leader rows."""
    cells = []
    for i, (name, page) in enumerate([("A.1 Models", "12"), ("A.2 Related work", "13"), ("A.3 Limits", "14")]):
        cells += [
            _cell(name, i, i + 1, 0, 1),
            _cell(". . . . . . . . . .", i, i + 1, 1, 2),
            _cell(page, i, i + 1, 2, 3),
        ]
    assert ladder.grid_to_markdown(cells, 3, 3) is None
    rendered = ladder.render_table(cells, 3, 3)
    assert rendered.startswith("- ") and "A.2 Related work" in rendered
    assert "|" not in rendered


def test_display_equation_is_not_a_table() -> None:
    """LaTeXML lays display math out as <table class="ltx_equation"> with the number in a second
    cell; docling parses that as a table. Detected on shape (the CSS class does not survive)."""
    cells = [
        _cell(r"\|\delta_i\|_2 \approx O(\log f_i)", 0, 1, 0, 1),
        _cell("(2)", 0, 1, 1, 2),
    ]
    assert ladder.grid_to_markdown(cells, 1, 2) is None
    out = ladder.render_table(cells, 1, 2)
    assert out.startswith("$$") and out.endswith("$$")
    assert r"\tag{2}" in out
    assert r"\|\delta_i\|_2 \approx O(\log f_i)" in out  # TeX verbatim, pipes NOT escaped


def test_real_table_survives_the_non_table_checks() -> None:
    """Regression guard: the ToC/equation rejections must not eat ordinary data tables."""
    cells = [
        _cell("Model", 0, 1, 0, 1, column_header=True),
        _cell("Acc", 0, 1, 1, 2, column_header=True),
        _cell("GPT-4", 1, 2, 0, 1), _cell("0.91", 1, 2, 1, 2),
        _cell("Llama", 2, 3, 0, 1), _cell("0.83", 2, 3, 1, 2),
    ]
    md = ladder.grid_to_markdown(cells, 3, 2)
    assert md is not None and "| GPT-4 | 0.91 |" in md


def test_grid_to_markdown_degenerate_inputs() -> None:
    assert ladder.grid_to_markdown([], 0, 0) is None
    assert ladder.grid_to_markdown([], 3, 3) is None


def test_tex_of_keeps_a_leading_symbol_escape() -> None:
    """docling concatenates the flattened MathML glyphs and the x-tex annotation. Cutting at the
    first ``\\word`` would drop a leading ``\\|``; cut at the first backslash instead."""
    assert ladder._tex_of(r"∥ δ i ∥ 2 \|\delta_i\|_2") == r"\|\delta_i\|_2"
    assert ladder._tex_of("plain prose, no tex") == "plain prose, no tex"
    # An already-fenced span (the MathML pre-pass ran) must not get double-fenced.
    assert ladder._tex_of(r"$$\alpha + \beta$$") == r"\alpha + \beta"


# ── the silent-truncation gate (synthetic error counts) ──────────────────────


def test_truncation_gate_decision() -> None:
    # > max(2, 10% of pages) page errors ⇒ reject docling (truncation guard).
    assert ladder.page_errors_exceed_gate(3, 10) is True   # 3 > max(2, 1)=2
    assert ladder.page_errors_exceed_gate(1, 10) is False  # 1 <= 2
    assert ladder.page_errors_exceed_gate(2, 5) is False   # 2 <= max(2, 0.5)=2 (boundary kept)
    assert ladder.page_errors_exceed_gate(3, 5) is True    # 3 > 2
    assert ladder.page_errors_exceed_gate(5, 100) is False  # 5 <= max(2, 10)=10 (tolerated in a long doc)
    assert ladder.page_errors_exceed_gate(11, 100) is True  # 11 > 10


# ── tier fallback ordering (docling + pymupdf mocked) ────────────────────────

GOOD_MD = "x" * 5000  # comfortably above the per-page gate for 1 page


@pytest.fixture()
def one_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "pdf_pages", lambda p: 1)  # what convert_pdf reads
    monkeypatch.setattr(ladder, "docling_available", lambda: True)


def test_docling_tier_wins(monkeypatch: pytest.MonkeyPatch, one_page: None) -> None:
    monkeypatch.setattr(ladder, "_docling_markdown", lambda p, n: ("# converted\n" + GOOD_MD, False, False))
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"))
    assert res.status == "ok" and res.pages == 1  # the page count convert_pdf read is the fixture's
    assert res.converter.startswith("docling@")
    assert res.converter.endswith("+tier1")
    assert res.markdown.startswith("# converted")


def _fail_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(p: Any, n: Any) -> Any:
        raise RuntimeError("docling produced empty or trivially small markdown")

    monkeypatch.setattr(ladder, "_docling_markdown", boom)


def test_pymupdf_tier_on_docling_failure(monkeypatch: pytest.MonkeyPatch, one_page: None) -> None:
    _fail_docling(monkeypatch)
    monkeypatch.setattr(ladder, "_pymupdf_markdown", lambda p: f"# T\n\nA\n\n{GOOD_MD}")
    res = ladder.convert_pdf(pathlib.Path("x.pdf"))
    assert res.status == "ok" and res.pages == 1
    assert res.converter.startswith("pymupdf4llm@") and res.converter.endswith("+tier2")
    assert res.markdown.startswith("# T\n")


def test_rawtext_tier_when_pymupdf_sparse(monkeypatch: pytest.MonkeyPatch, one_page: None) -> None:
    _fail_docling(monkeypatch)
    monkeypatch.setattr(ladder, "_pymupdf_markdown", lambda p: "tiny")
    monkeypatch.setattr(ladder, "_rawtext", lambda p: "recovered text " * 20)
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"))
    assert res.status == "ok" and res.pages == 1
    assert res.converter.startswith("pymupdf@") and res.converter.endswith("+tier3")
    assert "recovered text" in res.markdown


def test_raw_only_takes_the_raw_text_tier_alone(monkeypatch: pytest.MonkeyPatch, one_page: None) -> None:
    """``raw_only`` (acquire's second conversion of a PDF whose ladder text the quality gate found
    garbled, D8): tier 3's raw text, the layout tiers never run; nothing there is ``too_short``."""
    def layout(*_a: Any) -> Any:
        raise AssertionError("a layout tier ran under raw_only")

    monkeypatch.setattr(ladder, "_docling_markdown", layout)
    monkeypatch.setattr(ladder, "_pymupdf_markdown", layout)
    monkeypatch.setattr(ladder, "_rawtext", lambda p: "recovered text " * 20)
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"), raw_only=True)
    assert res.status == "ok" and res.pages == 1 and "recovered text" in res.markdown
    assert res.converter.startswith("pymupdf@") and res.converter.endswith("+tier3")
    monkeypatch.setattr(ladder, "_rawtext", lambda p: "")
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"), raw_only=True)
    assert (res.status, res.error_class) == ("failed", "too_short")


def test_sparse_pymupdf_still_beats_sparser_rawtext(monkeypatch: pytest.MonkeyPatch, one_page: None) -> None:
    _fail_docling(monkeypatch)
    sparse_but_usable = "some real content, under the per-page gate " * 2  # >= 50 chars
    monkeypatch.setattr(ladder, "_pymupdf_markdown", lambda p: sparse_but_usable)
    monkeypatch.setattr(ladder, "_rawtext", lambda p: "shorter")
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"))
    assert res.converter.startswith("pymupdf4llm@") and res.pages == 1  # tier 2 wins


def test_fail_when_all_tiers_empty(monkeypatch: pytest.MonkeyPatch, one_page: None) -> None:
    _fail_docling(monkeypatch)
    monkeypatch.setattr(ladder, "_pymupdf_markdown", lambda p: "")
    monkeypatch.setattr(ladder, "_rawtext", lambda p: "")
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"))
    assert res.status == "failed"
    assert res.markdown == ""
    assert res.error_class == "too_short"  # opened, nothing extractable (e.g. a scan without OCR)

    def unreadable(p: Any) -> str:
        raise RuntimeError("cannot open broken document")

    monkeypatch.setattr(ladder, "_pymupdf_markdown", unreadable)
    monkeypatch.setattr(ladder, "_rawtext", unreadable)
    res = ladder.convert_pdf(pathlib.Path("fake.pdf"))
    assert (res.status, res.error_class) == ("failed", "parse_error")  # no tier could read the bytes


def test_docling_absent_skips_tier1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "pdf_pages", lambda p: 1)  # what convert_pdf reads
    monkeypatch.setattr(ladder, "docling_available", lambda: False)
    # _docling_markdown must NOT be called when docling is unavailable.
    monkeypatch.setattr(ladder, "_docling_markdown", lambda p, n: pytest.fail("tier 1 ran"))
    monkeypatch.setattr(ladder, "_pymupdf_markdown", lambda p: f"# T\n\n{GOOD_MD}")
    res = ladder.convert_pdf(pathlib.Path("x.pdf"))
    assert res.converter.startswith("pymupdf4llm@") and res.pages == 1


# ── mechanical text_quality thresholds ───────────────────────────────────────


def test_quality_complete() -> None:
    assert ladder.text_quality_verdict("word " * 200) == "complete"  # > 500 chars


def test_quality_partial_low_yield() -> None:
    text = "usable but short. " * 15  # ~270 chars: > stub (200), < fulltext (500)
    assert 200 < len(text.strip()) < 500
    assert ladder.text_quality_verdict(text) == "partial"


def test_quality_partial_on_truncation() -> None:
    # A long doc kept despite a few docling page errors: truncation caps the verdict at partial.
    assert ladder.text_quality_verdict("word " * 400, truncated=True) == "partial"


def test_quality_stub_empty() -> None:
    assert ladder.text_quality_verdict("") == "stub_empty"
    assert ladder.text_quality_verdict("nav home about") == "stub_empty"  # < 200 chars


def test_quality_corrupted_replacement_density() -> None:
    assert ladder.text_quality_verdict("A" * 500 + "�" * 60) == "corrupted"  # > 5% U+FFFD


def test_quality_corrupted_binary() -> None:
    assert ladder.text_quality_verdict("A" * 100 + "\x00\x01\x02\x03\x04\x05\x06\x07\x08" * 5) == "corrupted"


# ── classify + HTML light path (stdlib, always available) ────────────────────


def test_classify() -> None:
    assert ladder.classify("application/pdf", b"") == "pdf"
    assert ladder.classify("text/html; charset=utf-8", b"") == "html"
    assert ladder.classify(None, b"%PDF-1.7\n...") == "pdf"
    assert ladder.classify(None, b"<!DOCTYPE html><html>") == "html"
    assert ladder.classify(None, b"just some plain text") == "text"


def test_classify_xml_is_split_from_html() -> None:
    """Publisher JATS fulltext must reach docling's JATS backend; it used to fall into `html`."""
    assert ladder.classify("application/jats+xml", b"") == "xml"
    assert ladder.classify("application/xml", b"") == "xml"
    jats = (
        b'<?xml version="1.0"?>\n<!DOCTYPE article PUBLIC '
        b'"-//NLM//DTD JATS (Z39.96) Journal Publishing DTD v1.2 20190208//EN" "x.dtd">\n<article/>'
    )
    assert ladder.classify(None, jats) == "xml"
    # An html media type still wins over an xml-looking body (XHTML is served as text/html).
    assert ladder.classify("text/html", jats) == "html"


TABLE_HTML = (
    b"<html><body><table><thead>"
    b'<tr><th rowspan="2">Method</th><th colspan="2">Fine-pruning</th></tr>'
    b"<tr><th>ACC</th><th>ASR</th></tr></thead><tbody>"
    b"<tr><td>Trigger Word</td><td>90.02</td><td>100.0</td></tr>"
    b"<tr><td>Word+EP</td><td>92.20</td><td>100.0</td></tr>"
    b"</tbody></table></body></html>"
)


def test_html_floor_delimits_table_cells() -> None:
    """The bug this fixes was live in production: `td`/`th` were absent from `_BLOCK` and
    unhandled, so adjacent cells concatenated with nothing between them and a results row read
    `Trigger Word90.02100.0` — `90.02100.0` cannot be tokenized back apart. The floor is the only
    HTML path when docling is absent, so a silent number-merge here is worse than a bad table."""
    body, _title = ladder._html_min(TABLE_HTML.decode())
    assert "90.02100.0" not in body
    assert "92.20100.0" not in body
    assert "90.02" in body and "100.0" in body
    assert "| |" not in body  # one delimiter per cell, not two


# ── MathML → $-fenced TeX pre-pass ───────────────────────────────────────────

MATHML_HTML = (
    rb"<html><body><p>We model the relation between "
    rb'<math alttext="\|\delta_i\|_2" display="inline"><semantics><mrow>'
    rb"<mo>&#x2225;</mo><msub><mi>&#x3B4;</mi><mi>i</mi></msub></mrow>"
    rb'<annotation encoding="application/x-tex">\|\delta_i\|_2</annotation>'
    rb"</semantics></math> and frequency.</p></body></html>"
)


def test_rewrite_mathml_keeps_tex_drops_glyphs() -> None:
    """LaTeXML emits BOTH a MathML tree and the x-tex source, and docling concatenates them into
    one run (`∥ δ i ∥ 2 \\|\\delta_i\\|_2`). Once merged it cannot be split reliably, so the fix
    has to happen before docling parses the document."""
    pytest.importorskip("lxml", reason="lxml not installed; the pre-pass no-ops")
    out = ladder.rewrite_mathml(MATHML_HTML).decode()
    assert "<math" not in out and "annotation" not in out
    assert r"$\|\delta_i\|_2$" in out
    assert "∥" not in out and "δ" not in out  # glyph run gone


def test_rewrite_mathml_uses_alttext_when_no_annotation() -> None:
    pytest.importorskip("lxml")
    raw = rb'<html><body><p>x <math alttext="a^2+b^2"><mi>a</mi></math> y</p></body></html>'
    assert "$a^2+b^2$" in ladder.rewrite_mathml(raw).decode()


def test_rewrite_mathml_display_uses_double_fence() -> None:
    pytest.importorskip("lxml")
    raw = rb'<html><body><math display="block" alttext="E=mc^2"><mi>E</mi></math></body></html>'
    assert "$$E=mc^2$$" in ladder.rewrite_mathml(raw).decode()


def test_rewrite_mathml_noop_without_math() -> None:
    """No <math> ⇒ byte-identical passthrough (no reserialization churn on the 99% of documents
    that carry no formulas)."""
    raw = b"<html><body><p>No formulas here at all.</p></body></html>"
    assert ladder.rewrite_mathml(raw) is raw


def test_rewrite_mathml_survives_malformed_input() -> None:
    """A parse failure must return the input, not raise: doubled-but-present math beats losing
    the document."""
    assert ladder.rewrite_mathml(b"<math>unclosed") is not None


# ── docling HTML/JATS backends (span-resolved, same TableData as PDF) ─────────


def test_markup_to_text_falls_back_to_floor_without_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    """10-design §5.3 keeps HTML from hard-depending on the `convert` group — the floor must still
    convert when docling is absent."""
    monkeypatch.setattr(ladder, "docling_available", lambda: False)
    res = ladder.markup_to_text(TABLE_HTML, "html")
    assert res.status == "ok"
    assert res.converter.startswith(("html-min@", "trafilatura@"))
    assert "90.02100.0" not in res.markdown


def test_markup_to_text_falls_back_when_docling_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "docling_available", lambda: True)

    def boom(raw: bytes, kind: str) -> str:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(ladder, "_docling_markup", boom)
    res = ladder.markup_to_text(TABLE_HTML, "html")
    assert res.status == "ok" and res.converter.startswith(("html-min@", "trafilatura@"))


def test_markup_to_text_html_via_docling_resolves_spans() -> None:
    pytest.importorskip("docling", reason="the `convert` group is not installed")
    res = ladder.markup_to_text(TABLE_HTML, "html")
    assert res.status == "ok"
    assert res.converter.startswith("docling@") and res.converter.endswith("+tierhtml")
    assert "Fine-pruning · ACC" in res.markdown
    assert "| Trigger Word | 90.02 | 100.0 |" in res.markdown


JATS_XML = (
    b'<?xml version="1.0"?>\n<!DOCTYPE article PUBLIC '
    b'"-//NLM//DTD JATS (Z39.96) Journal Publishing DTD v1.2 20190208//EN" "x.dtd">\n'
    b"<article><front><article-meta><title-group>"
    b"<article-title>A JATS Paper</article-title></title-group></article-meta></front>"
    b"<body><sec><table-wrap><table><thead>"
    b'<tr><th rowspan="2">Method</th><th colspan="2">Fine-pruning</th></tr>'
    b"<tr><th>ACC</th><th>ASR</th></tr></thead><tbody>"
    b"<tr><td>Trigger Word</td><td>90.02</td><td>100.0</td></tr>"
    b"</tbody></table></table-wrap></sec></body></article>"
)


def test_ensure_jats_doctype_normalizes_real_world_shapes() -> None:
    """docling's JATS sniffer keys on the DOCTYPE *system identifier* being a known JATS DTD
    filename. Measured against docling 2.76: a plain `<article>` root, a `dtd-version` attribute,
    and the correct PUBLIC id with a different system id are ALL rejected. Publisher JATS from
    Crossref text-mining links often has no DOCTYPE, so without this shim most real JATS silently
    falls to the HTML floor — losing the table structure the rung exists to get."""
    body = b"<article><front><article-meta/></front><body><p>x</p></body></article>"

    # No DOCTYPE ⇒ one is inserted after the prolog.
    out = ladder.ensure_jats_doctype(b'<?xml version="1.0"?>' + body)
    assert b"JATS-journalpublishing1.dtd" in out
    assert out.index(b"<?xml") < out.index(b"<!DOCTYPE") < out.index(b"<article")

    # No prolog either ⇒ DOCTYPE goes first.
    assert ladder.ensure_jats_doctype(body).startswith(b"<!DOCTYPE")

    # JATS PUBLIC id but an unrecognized system id ⇒ declaration replaced.
    wrong = (
        b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD '
        b'v1.2 20190208//EN" "local.dtd">' + body
    )
    assert b"JATS-journalpublishing1.dtd" in ladder.ensure_jats_doctype(wrong)

    # Already recognizable ⇒ untouched (identity, not just equal).
    good = (
        b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD '
        b'v1.2 20190208//EN" "JATS-journalpublishing1.dtd">' + body
    )
    assert ladder.ensure_jats_doctype(good) is good


def test_ensure_jats_doctype_leaves_foreign_xml_alone() -> None:
    """Must not relabel arbitrary XML as a JATS article."""
    rss = b'<?xml version="1.0"?><!DOCTYPE rss PUBLIC "-//x//DTD RSS//EN" "rss.dtd"><rss/>'
    assert ladder.ensure_jats_doctype(rss) is rss
    catalog = b'<?xml version="1.0"?><catalog><item>x</item></catalog>'
    assert ladder.ensure_jats_doctype(catalog) is catalog


def test_markup_to_text_jats_without_doctype_still_reaches_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("docling", reason="the `convert` group is not installed")
    # JATS goes to fetchpdf first when it is importable (it is a base dependency); this pins the
    # docling backend behind it, so fetchpdf is switched off (red with the convert group otherwise).
    monkeypatch.setattr(ladder, "fetchpdf_markup_available", lambda: False)
    no_doctype = JATS_XML[JATS_XML.index(b"<article") :]
    res = ladder.markup_to_text(no_doctype, "xml")
    assert res.converter.endswith("+tierjats"), f"fell back to {res.converter}"
    assert "Fine-pruning · ACC" in res.markdown


def test_markup_to_text_jats_via_docling_resolves_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of routing through docling: JATS normalizes into the SAME TableData as HTML and
    PDF, so one span-resolving renderer serves all three (and CALS vs XHTML table models, and
    <inline-formula>, are docling's problem, not ours). fetchpdf (tried first for JATS) is off."""
    pytest.importorskip("docling", reason="the `convert` group is not installed")
    monkeypatch.setattr(ladder, "fetchpdf_markup_available", lambda: False)
    res = ladder.markup_to_text(JATS_XML, "xml")
    assert res.status == "ok"
    assert res.converter.startswith("docling@") and res.converter.endswith("+tierjats")
    assert "Fine-pruning · ACC" in res.markdown
    assert "| Trigger Word | 90.02 | 100.0 |" in res.markdown


def test_docling_export_does_not_escape_tex() -> None:
    """`escape_underscores`/`escape_html` are off: they turn `\\delta_i` into `\\delta\\_i` and
    `\\|` into `\\&#124;`, destroying the exact-source property that makes this path worthwhile."""
    pytest.importorskip("docling", reason="the `convert` group is not installed")
    res = ladder.markup_to_text(MATHML_HTML, "html")
    assert r"$\|\delta_i\|_2$" in res.markdown
    assert r"\delta\_i" not in res.markdown
    assert "&#124;" not in res.markdown


def test_html_light_path() -> None:
    raw = (
        b"<html><head><title>My Title</title><style>.x{color:red}</style></head>"
        b"<body><script>evil()</script><p>First real paragraph of body content that "
        b"is long enough to matter.</p><p>Second &amp; paragraph.</p></body></html>"
    )
    res = ladder.html_to_text(raw)
    assert res.status == "ok"
    assert res.converter.startswith(("html-min@", "trafilatura@"))
    assert res.converter.endswith("+tierh")
    assert "First real paragraph" in res.markdown
    assert "Second & paragraph" in res.markdown  # entity unescaped
    assert "evil()" not in res.markdown and "color:red" not in res.markdown  # script/style dropped


def test_html_light_path_extracts_the_main_content() -> None:
    """A saved-shape blog page (site menu, doubled nav bar, article, footer; T3.6-f3b): trafilatura
    starts the text at the article, the stdlib floor beneath it keeps the menus."""
    pytest.importorskip("trafilatura", reason="trafilatura (convert group) not installed")
    raw = (REPO / "tests" / "fixtures" / "convert" / "blog-nav-article.html").read_bytes()
    res = ladder.html_to_text(raw)
    assert res.converter.startswith("trafilatura@") and res.quality == "complete"
    body = res.markdown.partition("\n")[2].lstrip()
    assert body.startswith("Calibrated abstention in retrieval agents\nWe introduce calibrated abstention")
    assert "Explore our many areas" not in body and "Log in" not in body
    floor, _title = ladder._html_min(raw.decode())
    assert floor.index("Explore our many areas") < floor.index("We introduce calibrated abstention")


def test_html_title_extracted() -> None:
    _body, title = ladder._html_min("<html><head><title>Hello World</title></head><body>x</body></html>")
    assert title == "Hello World"


# ── real subprocess: hard-crash detection + single-item attribution ──────────


def test_real_subprocess_crash_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that aborts (native crash) ⇒ no result file ⇒ ``crashed``. This exercises the
    ACTUAL subprocess path (not a monkeypatch), proving per-item crash attribution."""
    monkeypatch.setenv("STACK_CONVERT_ABORT", "1")
    outcome = convert_one(b"anything", "text/plain", mem_gb=0, timeout=60)
    assert outcome.status == "crashed"


# ── fetchpdf's converter for scholarly markup (JATS; publisher HTML from fetch.oa) ───────────

LATEXML_HTML = (
    b'<!DOCTYPE html><html><head><title>An arXiv paper</title></head>'
    b'<body><div class="ltx_page_main"><article class="ltx_document">'
    b'<h1 class="ltx_title ltx_title_document">An arXiv paper</h1>'
    b'<div class="ltx_para"><p class="ltx_p">Body text of a LaTeXML page. </p></div>'
    b'</article></div></body></html>'
)


def test_jats_via_fetchpdf_keeps_spanning_headers() -> None:
    """A `<th colspan="2">` header cannot be expressed in a pipe table; fetchpdf keeps the table
    as inline HTML (CommonMark), so the header survives instead of shifting every value right."""
    pytest.importorskip("fetchpdf", reason="fetchpdf is not installed")
    res = ladder.markup_to_text(JATS_XML, "xml")
    assert res.status == "ok"
    assert res.converter.startswith("fetchpdf-md@") and res.converter.endswith("+tierjats")
    assert 'colspan="2"' in res.markdown and 'rowspan="2"' in res.markdown
    assert "A JATS Paper" in res.markdown and "Trigger Word" in res.markdown


def test_publisher_html_uses_fetchpdf_only_when_scholarly() -> None:
    """Blog/forum HTML (fetch.http) keeps docling's backend or the floor; publisher article HTML
    that fetch.oa acquired goes to fetchpdf's converter."""
    pytest.importorskip("fetchpdf", reason="fetchpdf is not installed")
    plain = ladder.markup_to_text(TABLE_HTML, "html")
    assert not plain.converter.startswith("fetchpdf-md@")
    scholarly = ladder.markup_to_text(TABLE_HTML, "html", scholarly=True)
    assert scholarly.converter.startswith("fetchpdf-md@") and scholarly.converter.endswith("+tierhtml")
    assert 'colspan="2"' in scholarly.markdown


def test_latexml_html_stays_on_docling_even_when_scholarly() -> None:
    res = ladder.markup_to_text(LATEXML_HTML, "html", scholarly=True)
    assert res.status == "ok"
    assert not res.converter.startswith("fetchpdf-md@")
    assert ladder.looks_like_latexml(LATEXML_HTML) and not ladder.looks_like_latexml(TABLE_HTML)


def test_fetchpdf_markup_falls_through_when_it_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "fetchpdf_markup_available", lambda: True)

    def boom(raw: bytes, kind: str) -> str:
        raise RuntimeError("converter exploded")

    monkeypatch.setattr(ladder, "_fetchpdf_markup", boom)
    res = ladder.markup_to_text(JATS_XML, "xml")
    assert res.status == "ok" and not res.converter.startswith("fetchpdf-md@")


def test_convert_routes_origin_to_the_ladder(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("fetchpdf", reason="fetchpdf is not installed")
    via_oa = ladder.convert(TABLE_HTML, "text/html", workdir=tmp_path, origin="fetch.oa")
    via_http = ladder.convert(TABLE_HTML, "text/html", workdir=tmp_path, origin="fetch.http")
    assert via_oa.converter.startswith("fetchpdf-md@")
    assert not via_http.converter.startswith("fetchpdf-md@")


# ---------------------------------------------------------------- charsets (the Chinese-coverage work)
# A page in a legacy charset was decoded as UTF-8 with replacement characters, so a GBK page of TC260 or a Big5
# page reached the screen as U+FFFD runs. Each case: the declared label, the codec the bytes are written in, and
# text that needs the WHATWG superset where the label's own table lacks a character (GB2312 has no 镕, EUC-KR no
# 똠, Shift_JIS no ①, ISO-8859-1 no curly quotes).
CHARSET_CASES = [
    ("gb2312", "gb18030", "人工智能安全治理框架，及时处置。朱镕基"),
    ("GBK", "gb18030", "生成式人工智能服务安全基本要求"),
    ("gb18030", "gb18030", "前沿人工智能风险管理框架"),
    ("big5", "big5hkscs", "人工智慧安全與治理"),
    ("Shift_JIS", "cp932", "人工知能の安全性①"),
    ("euc-jp", "euc_jp", "人工知能の安全性について"),
    ("x-euc-jp", "euc_jp", "人工知能の安全性について"),
    ("euc-kr", "cp949", "인공지능 안전성 평가 똠"),
    ("windows-1251", "cp1251", "Безопасность искусственного интеллекта"),
    ("koi8-r", "koi8_r", "Безопасность искусственного интеллекта"),
    ("windows-1252", "cp1252", "Café “safety” – naïve"),
    ("iso-8859-1", "cp1252", "Café “safety” – naïve"),
]


def _page(label: str, body: str, *, http_equiv: bool = False) -> str:
    meta = (f'<meta http-equiv="Content-Type" content="text/html; charset={label}">' if http_equiv
            else f'<meta charset="{label}">')
    return f"<html><head>{meta}<title>T</title></head><body><p>{body}</p></body></html>"


@pytest.mark.parametrize("label, codec, text", CHARSET_CASES)
def test_html_is_decoded_by_its_declared_charset(label: str, codec: str, text: str) -> None:
    raw = _page(label, text).encode(codec)
    decoded, _used = ladder.decode_text(raw)
    assert text in decoded and "�" not in decoded
    res = ladder.html_to_text(raw)
    assert text in res.markdown and "�" not in res.markdown


@pytest.mark.parametrize("label, codec, text", CHARSET_CASES[:3] + CHARSET_CASES[7:8])
def test_http_equiv_and_content_type_declarations(label: str, codec: str, text: str) -> None:
    raw = _page(label, text, http_equiv=True).encode(codec)
    assert text in ladder.decode_text(raw)[0]
    bare = f"<html><body><p>{text}</p></body></html>".encode(codec)  # declared only by the HTTP header
    assert text in ladder.decode_text(bare, f"text/html; charset={label}")[0]


def test_gbk_page_reaches_every_html_backend_as_utf8() -> None:
    """markup_to_text re-encodes the page as UTF-8 (the meta rewritten), so docling and fetchpdf, which sniff
    bytes themselves, read the same text the light path does."""
    text = "人工智能安全治理框架，及时处置风险。"
    raw = _page("gbk", text).encode("gb18030")
    out = ladder.utf8_markup(raw)
    assert out.decode("utf-8").count(text) == 1 and b'charset="utf-8"' in out
    assert ladder.utf8_markup(_page("utf-8", text).encode()) == _page("utf-8", text).encode()  # UTF-8: untouched
    res = ladder.markup_to_text(raw, "html")
    assert text in res.markdown and "�" not in res.markdown


def test_mislabelled_single_byte_page_that_is_utf8_reads_as_utf8() -> None:
    text = "人工智能安全 Café"
    assert ladder.decode_text(_page("iso-8859-1", text).encode("utf-8")) [0].count(text) == 1


def test_undeclared_legacy_charset_is_detected() -> None:
    pytest.importorskip("charset_normalizer", reason="charset-normalizer (via trafilatura/requests) not installed")
    zh = ("人工智能安全治理框架旨在应对人工智能发展带来的安全风险。框架提出了包容审慎、确保安全的原则，"
          "要求及时处置风险，并对模型的研发和应用进行分类分级管理。") * 3
    ru = ("Безопасность искусственного интеллекта требует оценки рисков и надзора за моделями. ") * 4
    for text, codec in ((zh, "gb18030"), (ru, "cp1251")):
        raw = f"<html><body><p>{text}</p></body></html>".encode(codec)
        decoded, used = ladder.decode_text(raw)
        assert text in decoded, (codec, used)


#: WHATWG Encoding Standard labels (the legacy multi-byte encodings and the single-byte ones Python spells
#: otherwise): every one names a codec, so a page declaring it is decoded by it, not by detection.
WHATWG_LABELS = ("cseucpkdfmtjapanese euc-jp x-euc-jp csiso2022jp iso-2022-jp csshiftjis ms932 ms_kanji shift-jis "
                 "shift_jis sjis windows-31j x-sjis cseuckr csksc56011987 euc-kr iso-ir-149 korean ks_c_5601-1987 "
                 "ks_c_5601-1989 ksc5601 ksc_5601 windows-949 big5 big5-hkscs cn-big5 csbig5 x-x-big5 chinese "
                 "csgb2312 csiso58gb231280 gb2312 gb_2312 gb_2312-80 gbk iso-ir-58 x-gbk gb18030 x-cp1250 x-cp1251 "
                 "x-cp1252 x-cp1253 x-cp1254 x-cp1255 x-cp1256 x-cp1257 x-cp1258 windows-874 dos-874 tis-620 "
                 "csmacintosh mac macintosh x-mac-roman x-mac-cyrillic x-mac-ukrainian koi8-r koi8 koi koi8-u koi8-ru "
                 "cskoi8r iso-8859-8-i csiso88598i logical visual iso-8859-6-e iso-8859-6-i unicode-1-1-utf-8 "
                 "unicode11utf8 unicode20utf8 x-unicode20utf8 utf8 utf-8 ibm866 iso-8859-15").split()


def test_whatwg_labels_name_a_codec() -> None:
    assert [lab for lab in WHATWG_LABELS if ladder.charset_codec(lab) is None] == []
    assert ladder.charset_codec("x-euc-jp") == ladder.charset_codec("euc-jp") == "euc_jp"


def test_plain_text_and_unknown_labels() -> None:
    zh = "生成式人工智能服务安全基本要求"
    assert zh in ladder.decode_text(zh.encode("gb18030"), "text/plain; charset=GBK", markup=False)[0]
    assert ladder.decode_text(_page("no-such-charset", "plain ascii").encode())[1] == "utf-8"
    assert ladder.decode_text("﻿hello".encode("utf-8"))[0] == "hello"
    assert ladder.charset_codec("utf-16") == "utf-8" and ladder.charset_codec("x-gbk") == "gb18030"
    assert ladder.charset_codec("bogus") is None
