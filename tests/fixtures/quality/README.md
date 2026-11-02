# Text-quality fixtures

Documents for `tests/test_text_quality.py`, which tests `stack/conversion/quality.py`. They are synthetic: written
for these tests, with invented titles, authors, journals, sites and DOIs (test prefix `10.5555/`, addresses on
`example.org`); none contains third-party text. Each is laid out as the converter emits that kind of document
(pymupdf4llm Markdown for PDFs, the HTML converters' output for pages) and built to land in one verdict. `cases.json`
gives each case's role and media type (and the raw HTML size where the text-to-raw ratio matters) and a note on what
it exercises.

| Case | Role | Verdict | Exercises |
|---|---|---|---|
| `abs-landing-page` | full_text | failed, `abstract_only` | a preprint server's abstract page as a paper body: landing-page labels, no body section |
| `press-article` | full_text | ok | a complete article with an Article-in-Press banner and a reference list at the end; `in_press` |
| `press-references` | full_text | failed, `references_only` | a reference section alone (references heading at the start); `in_press` |
| `press-running-headers` | full_text | flagged, `repeated_lines` | journal name, DOI and "Article in Press" repeated on every page; `in_press` |
| `indonesian-paper` | full_text | flagged, `non_english` | a paper written in Indonesian |
| `japanese-abstract` | abstract | flagged, `non_latin` | an abstract mostly in Japanese with an English summary: non-Latin, not non-English |
| `latex-residue-page` | page | flagged, `latex_residue` | rendered formulas left as raw LaTeX commands outside math delimiters |
| `toc-dot-leaders` | full_text | flagged (`too_short` only) | dot leaders match the glyph-run pattern but count as no glyph run |
| `figure-text` | full_text | flagged, `figure_text` | a picture-text block of axis ticks and legend labels |
| `short-blog-post` | page | ok | a short complete post from a large HTML page, text-to-raw ratio just above the flag |
| `abstract-only-page` | page | flagged, `low_text_ratio` | a page that is only an abstract and a link, from a large HTML page |
| `replacement-chars-pdf` | full_text | failed, `replacement` | a figure block extracted as U+FFFD replacement characters |

The `full_text` cases below 8,000 characters also carry `too_short` at flag level. The three `press-*` cases are the
only ones with an Article-in-Press phrase in their first 5,000 characters.
