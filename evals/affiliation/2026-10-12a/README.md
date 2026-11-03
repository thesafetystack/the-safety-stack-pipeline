# Affiliation eval set `2026-10-12a`

Superseded: historical. Its follow-up `2026-10-12b-bylines` (hard bylines) decided the move of `extract_main` to
`openai/gpt-6-luna` (D201); production affiliation output is now measured by `evals/judge-2026-10-25j/affiliation`.

Task: the LLM tier of `annotate.affiliation` (role `extract_main`): from the paper's header window and the metadata
author list, return each author's organisations as printed, aligned to the roster and resolved to
tracked-organisation ids. Frozen 2026-10-12 from a development store by `python -m stack.evals.freeze affiliation`
(seed `track-e-2026-10-12a`).

## Items (`items.jsonl`, 30 papers, 180 gold authors)
`items.jsonl` is not in the repository (it holds long excerpts of third-party text, D795): `tools/fetch_eval_texts.py`
fetches it into the eval-text cache, checked against `evals/archived.sha256` and the manifest's `items_sha256`.
- `input.user` is the exact user message production sends; `input_digest` = sha256(user). One paper has publisher
  XML; the harness skips the XML tier and records `production_uses_llm`.
- Sample: openalex 14, arxiv 13, semantic_scholar 3 (from 215 papers with full text); licence `open` for all; split
  15 dev / 15 test.
- **Limit:** the set is easy. 29 / 30 have a clear byline, none has the flattened, marker-lost byline layout on which
  a model can "broadcast" one organisation to every author; tracked organisations are few (nvidia 18, deepmind 15,
  tencent 3 author pairs).

## Labels (`gold.jsonl`, `labels/`)
Rubric `evals/rubrics/affiliation.md`. Opus labelled all 30 papers; Codex `gpt-6-sol` checked a seeded 30 % sample
(9 papers, 48 authors, `labels/codex-sample.jsonl`): 48 / 48 authors identical, no adjudication needed. Gold = the
Opus labels (`agreement: null` on the 21 unsampled papers).

## Result (2026-10-12; dev 15 × 2 repeats, test 15 × 1)
Every model scored tracked F1 1.00 on dev and test. All-organisation F1 on test: gpt-6-luna 0.994,
deepseek-v4.1-flash 0.994, gemini-3.5-flash-lite 0.994, claude-haiku-4.5 (then pinned) 0.982, claude-haiku-5.5 0.922;
luna at about a tenth of haiku-4.5's cost (≈ $0.5 vs $5.5 per 1,000 papers). The set could not show the
broadcasting failure, hence `2026-10-12b-bylines`.
