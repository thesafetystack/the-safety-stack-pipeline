# technique_staged eval set 2026-10-17a: 36 documents, gold on taxonomy v0.9

The set for `annotate.technique_staged` (techpipe, D606): one gold for all four modes (`single`, `sweep`,
`staged`, `lexical`) and for any model, scored by `stack eval technique_staged` / `stack eval score` /
`staged-compare` / `staged-table` (D607; `stack/evals/technique_staged.py`). Experimental: no production
decision rests on it alone (LLM-only gold, 36 documents).

## Items (36; dev/test 20/16; results are reported over all 36, `--split all`)
- **an-0001..an-0025**: the technique_roles 2026-10-13a documents (doc_id, source_item, split, stratum, licence
  class and texts byte-identical, digests checked by the freeze): 11 forum posts, 7 lab / research pages, 7 papers
  (3 abstract-only).
- **an-0026..an-0036** (`source_item` `mirror:<doc_id>`), from a read-only store copy: 5 long arXiv papers
  (an-0026..an-0030), 2 system cards (o1, GPT-4.5: an-0031, an-0032), 1 technical report (Qwen3Guard, an-0033),
  2 lab posts (an-0034, an-0036) and 1 think-tank critique (an-0035). Metadata in `sources/mirror-docs.jsonl`
  (title, channel and channel group, licence class of the member supplying the text, basis role, text digest);
  split alternating dev/test by length within channel group.
- Composition: channel group url 13, paper 12, forum 11; gold `doc_kind` paper 13, blogpost 12, commentary 6,
  system_card 3, technical_report 1, other 1; length bands (characters) <20k 18, 20-60k 5, 60-100k 9, >100k 4.
- Per item `input`: `title`, `basis_role`, `text_digest`, `full_text_digest`, `full_text_chars` and `staged` =
  `main.freeze_info(text)`: segmenter and lexical versions, taxonomy sha256, digests of the segmentation summary and
  of the lexical hits, `staged_fills_digest`, the chunk count, and the client `input_digest` of every
  answer-independent user message (`single`, `lexical`, each P chunk). `input_digest` = sha256 of the `single` user
  message. The input check (`TechniqueStagedAdapter.frozen_problems`) compares per mode what its calls depend on;
  the S and final messages of `sweep` / `staged` depend on the P and S answers and cannot be frozen.

## Gold (`gold.jsonl`; provenance and agreement in `adjudication/README.md`)
406 rows (245 with a role >= 2), 16 extra techniques, 68 menu gaps, 4 documents with no row. Two independent
labellers (Opus; Codex gpt-6-sol / gpt-6-astra, a second Opus on the 3 abstract-only items) and an Opus reconciler
with written reasons, guide v1.1 + rulings (`adjudication/GUIDE.md`, `RULINGS.md`); A-vs-B slug F1 0.796.
- Row fields: `slug`, `roles` (12 roles, 1-4), `quote`, `confidence` (high / med / low), `sources` (A, B, old,
  reconciler), `alt_slugs`, `implemented_with`, `note` (the reconciler's decision, else the row's reason).
- `candidate: true` on the 8 `candidate:<slug>` rows (entries proposed for the taxonomy, not admitted): never
  scored. Group nodes (20 rows) are outside the primary metric (slug presence without group nodes) and inside the
  secondary one. Gold variants: full (primary), rows given by both labellers, high confidence only.
- `stack eval` refuses the run when a non-candidate gold slug is not in the current taxonomy file.

## Length weighting
Length-weighted recall weighs each item by the production share of its band divided by the number of eval items in
that band. Shares computed read-only on 2026-10-17 by `scripts/length_bands.py <store copy>` over the newest
`documents` row of each doc with a current text (characters of the text blob):

| population | documents measured | <20k | 20-60k | 60-100k | >100k |
|---|---|---|---|---|---|
| `screen_relevant` (**default**): newest relevance-screen verdict `relevant` | 12,568 | 0.0489 | 0.2641 | 0.4135 | 0.2736 |
| `all_text`: every document with a text | 43,968 | 0.4698 | 0.1667 | 0.2252 | 0.1383 |
| `brief`: the eval brief's fallback numbers | — | 0.20 | 0.25 | 0.28 | 0.27 |

Method notes. The store copy carries no `stack_relevance` verdict (the column is null on every document), so the
first-choice population, documents whose `stack_relevance` verdict is `relevant`, could not be measured. Its
nearest available stand-in is the relevance screen (`screen` family, newest verdict per doc `relevant`), the
population `annotate.technique_staged` reads in production; its >100k share (0.27) matches an earlier scan of the
relevant corpus (27 % above 100k characters). The store copy is partial: 49,318 of the 93,286 documents with a
current text have no blob in it (5,711 of the 18,279 screen-relevant ones), so the shares come from the documents
whose blob is present; if blob presence correlates with length the shares are biased, by an unknown amount.
`all_text` includes every collected document, most of which production never annotates, and is not used by default.
The scorer takes `--length-shares screen_relevant | all_text | brief` (`stack/evals/technique_staged.py`,
`LENGTH_SHARES`).

## Scripts
- `scripts/stage.py <labelling dir> <store copy> [--write]`: the one-time import (no LLM): `sources/`, the texts of
  an-0026..an-0036 and `adjudication/` (paths are arguments; neither is recorded).
- `scripts/freeze.py [--write]`: rebuilds `items.jsonl`, `gold.jsonl`, `manifest.json` and the texts of
  an-0001..an-0025 from the staged files and the 2026-10-13a set, byte for byte; refuses on any digest mismatch
  (2026-10-13a texts vs their digests, staged texts vs the store digest, the labellers' copies vs the frozen texts).
  Re-run it with `--write` after a change to the prompts, the segmenter, the lexical matcher or the taxonomy (the
  input check refuses the set until then).
- `scripts/length_bands.py <store copy> [--json]`: the length-band shares above.

`manifest.json` `frozen_with`: taxonomy path / sha256 / version, rubric `rubric-v0.9-a` digest, prompt set
`technique-staged-v1` and the digest of each prompt file per mode, segmenter `seg-v1`, lexical `lex-v2`.

## Predeclared comparisons (design E1-E3)
Primary: sweep-luna vs single-luna, slug-presence recall (no group nodes, no candidates), delivered over all 36
documents, paired document bootstrap 95 % CI; co-primary: length-weighted recall and the precision difference.
Secondary: staged-luna vs sweep-luna; union-of-3 single-luna vs sweep-luna; cells >= 2. Repeats: single-luna x3,
sweep-luna x2, staged-luna x2 (each repeat its own P/S cache); others x1. Commands: the run book of the
experiment (outside the repository) and `stack eval --help`.
