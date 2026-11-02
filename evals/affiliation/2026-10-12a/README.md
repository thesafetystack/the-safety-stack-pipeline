# Affiliation eval set `2026-10-12a`

Task: the LLM tier of `annotate.affiliation` (v3 `workflows/annotate/affiliation/{main.py,extract.py,extract.md}`,
which PLAN T4.4 copies into v4; role `extract_main`): from the paper's header window and the metadata author list,
return each author's organisations as printed, aligned to the roster and resolved to tracked-organisation ids.
Frozen 2026-10-12 15:02 UTC from `v4/store-dev` (gitignored; not in the repo), documents head `rzc8gbts73749.documents` (track B's second broad
acquire run), by `python -m stack.evals.freeze affiliation` (seed `track-e-2026-10-12a`).

## Items (`items.jsonl`, 30 papers, 180 gold authors)
- `input.user` is the exact user message production sends (v3 `main.py` `_user_message` over `extract.header_window`
  and the authors block); `input_digest` = sha256(user). `xml_snippet` / `xml_byline` are kept where the acquire OA
  lane stored publisher XML (1 paper); the harness skips the XML tier and records `production_uses_llm` (production
  would read that one paper's byline deterministically).
- Pool at freeze time: 215 papers with full text (arXiv 111, OpenAlex 86, Semantic Scholar 18), 205 with a clear
  byline in the window, 1 with parsed XML. Sample: openalex 14, arxiv 13, semantic_scholar 3; licence `open` for all;
  split 15 dev / 15 test.
- **Limit:** the set is easy. 29 / 30 have a clear byline, none has the flattened, marker-lost byline layout on which
  v3 measured `gpt-5.6-luna` "broadcasting" one organisation to every author (v3's `docs/taxonomy_extraction_evaluation/
  MODEL-AB-LUNA-VS-HAIKU.md`); tracked organisations are few (nvidia 18, deepmind 15, tencent 3 author pairs).

## Labels (`gold.jsonl`, `labels/`)
Rubric `evals/rubrics/affiliation.md`. Opus labelled all 30 papers (2 sub-agents); Codex `gpt-6-sol` checked a
seeded 30 % sample (9 papers, 48 authors, `labels/codex-sample.jsonl`). Agreement on the sample: 9 / 9 papers, 48 / 48
authors identical on normalised organisation strings and roster ids; no adjudication needed. Gold = the Opus labels
(`agreement: null` on the 21 unsampled papers). 2 of 180 authors have no organisation tied to them in the text.

## Results 2026-10-12 (dev 15 × 2 repeats / test 15 × 1; `v4/evals-store/affiliation/2026-10-12a/`, gitignored; not in the repo)
Pairs (author, organisation) after normalisation (NFKC, casefold, whitespace, edge punctuation, a leading "the"
dropped: running-text affiliations such as "the State Key Laboratory …"). Tracked = roster ids only.

| model | tracked F1 dev / test | all-org P / R / F1 dev | all-org P / R / F1 test | broadcast | schema degraded | repeat agree | $/1k |
|---|---|---|---|---|---|---|---|
| anthropic/claude-haiku-5.5 | 1.00 / 1.00 | 0.984 / 0.992 / 0.988 | 0.917 / 0.928 / 0.922 | 0 | 0 | 100 % | 0.70-0.77 |
| openai/gpt-6-luna | 1.00 / 1.00 | 0.984 / 0.992 / 0.988 | 0.988 / 1.000 / 0.994 | 0 | all rows | 100 % | 0.44-0.63 |
| deepseek/deepseek-v4.1-flash | 1.00 / 1.00 | 0.984 / 0.992 / 0.988 | 0.988 / 1.000 / 0.994 | 0 | 0 | 100 % | 0.54-0.65 |
| google/gemini-3.5-flash-lite | 1.00 / 1.00 | 0.799 / 0.841 / 0.820 | 0.988 / 1.000 / 0.994 | 2 flagged, both unit granularity (below) | 0 | 93 % | 1.8-2.0 |
| anthropic/claude-haiku-4.5 (dev-v2 pin) | 1.00 / 1.00 | 0.959 / 0.955 / 0.957 | 0.988 / 0.976 / 0.982 | 0 | 0 | 93 % | 5.4-5.7 |

Gemini's two flagged "broadcasts" are not misattributions: on aff-0013 it split one footnote affiliation (all
five authors) into two units, on aff-0017 it copied "University of Calabria, Rende, Italy" from the contact block
where gold copies the byline's "University of Calabria, Italy". The all-organisation metric is sensitive to which
printed unit is copied; the tracked (roster-id) metric is not. No parse failures; byline coverage 100 % for every model; deepseek's latency p90 is 13-26 s (others ≤ 6.5 s). Every
luna call goes through the strict-schema degrade (OpenAI rejects the optional-property schema with a 400; the
client re-sends with the schema in the prompt), as v3 found.

**Recommendation (conditional):** `openai/gpt-6-luna` matches or beats the incumbent haiku-4.5 here at about a tenth
of the cost (≈ $0.5 per 1,000 papers), but this set cannot see the failure that made v3 keep haiku (broadcasting on
flattened bylines). Before switching, run a 10-20-paper hard-layout set (marker-lost bylines, long author lists,
tables of contents in the window); v3's `si.v2i4.16671` / `arxiv.2510.13900` are the known cases.

**Provider prompt caching** (dev): system prefix ≈ 1,050 tokens first, byline window last. Cached share: luna 50 %,
deepseek 59 %, haiku-5.5 30 %, gemini-3.5-flash-lite 7 %, haiku-4.5 0 %; $/1k settled vs no cache: luna 0.44 vs 0.55,
deepseek 0.54 vs 0.99, haiku-5.5 0.77 vs 0.92, gemini 1.98 vs 2.05, haiku-4.5 5.66 vs 5.66.
