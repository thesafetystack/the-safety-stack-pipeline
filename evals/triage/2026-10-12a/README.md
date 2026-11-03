# Triage eval set `2026-10-12a`

Superseded as a measure: production triage (`openai/gpt-6-luna`, `fetch_on: [relevant, borderline]`; prompt
`safety-relevance-v3`, since D656/D663 its revision `safety-relevance-v4`) was chosen on this set (D201); production
triage output is now measured by `evals/judge-2026-10-25j/triage` and `triage_w`.

Task: the triage gate's LLM question (`workflows/triage/ask.py`): from a catalogue record (title, authors, abstract,
in-band excerpt) decide `relevant | borderline | not_relevant`. Frozen 2026-10-12 from a development store (1,806
documents) by `python -m stack.evals.freeze triage` (seed `track-e-2026-10-12a`; `manifest.json` has the code
digests).

## Items (`items.jsonl`, 241)
`items.jsonl` is not in the repository (long third-party excerpts, D795): `tools/fetch_eval_texts.py` fetches it
into the eval-text cache, checked against `evals/archived.sha256` and the manifest.
- `input.record` is the exact user message production sent at freeze time (`workflows.triage.ask.document_text`
  over the gate view row); `input_digest` = sha256(record). `stack eval` recomputes it and refuses the set if
  production changed.
- Items 1-200: paper 100 (openalex 45, arxiv 35, semantic_scholar 20), forum 40 (lesswrong), url 60 (six lab blogs).
  Within each channel about half `kw_high` / `kw_low` on a safety-keyword proxy (106 / 94); 39 a-priori hard cases
  (capabilities 14, policy 12, governance 8, safety_marketing 5). The sample is stratified, not representative.
  The 60 url items are **title-only probes** (`production_asks` false: production fetches url documents without
  asking). Licence: open 120, public_web 60, restricted 20. Split 50/50 inside each stratum: `dev` 100, `test` 100.
- Items `tri-0201` … `tri-0241` (amendment, 2026-10-12): 41 Shallow-Review-heavy items appended by
  `python -m stack.evals.sr append --task triage` (items 1-200 byte-identical; `manifest.json` `amendments[0]` has
  the hashes before and after and, per item, the query, topic and selection intent: 27 likely in, 14 hard
  negatives). Split 21 dev / 20 test. Items of `evals/triage/sr2025-recall` are excluded.

## Labels and gold
- **Gold v1** (`gold.jsonl`, items 1-200; rubric `evals/rubrics/triage.md`): Opus and Codex `gpt-6-sol`
  independently, from the rubric and `{item_id, record}`; the 35 disagreements to an Opus adjudicator with the
  labellers anonymised (`labels/adjudication.jsonl`; gold `contested: true`). Agreement 165 / 200, κ 0.705; fetch
  (verdict ≠ `not_relevant`) 184 / 200, κ 0.836. Disagreement is almost entirely about `borderline`. Caveat: the
  adjudicator sided with Opus 31 of 35 times.
- **Gold v2** (`gold-v2.jsonl`, all 241; rubric `evals/rubrics/triage-v2.md`: scope = Stack ∪ Shallow Review, from
  `docs/scope/shallow-review.md`, plus a `scope` label `core` / `sr_only` / `out` and calibration rules). Relabelled
  166 items (the v1 `not_relevant` / `borderline` items, a seeded 15 % control of v1 `relevant`, the 41 appended
  items) by Opus + Codex, 38 disagreements to an Opus adjudicator; 75 v1 `relevant` items carried
  (`carried_from: v1`, scope null). The maintainer's labels on 20 items (`labels-v2/tomas-part1.jsonl`) are the
  reference where present. Verdict agreement 134 / 166, κ 0.677; scope κ 0.711. Base rates: relevant 132,
  borderline 24, not_relevant 85.

## Results (2026-10-12)
Primary metric: recall of gold fetch under the gate's `fetch_on`; cost = settled USD per 1,000 records.
- **Gold v1:** `openai/gpt-6-luna` beat the then incumbent `claude-haiku-4.5` (McNemar p = 0.015 on dev). A prompt
  written from dev errors only, `safety-relevance-v2`, raised luna's test fetch correctness 79 → 90 / 100
  (p = 0.003). Stronger models (claude-sonnet-5.5, gemini-3.8-flash, luna-pro) agree more with the 3-way label but
  park more gold-fetch records in `borderline`, so they do not beat luna, at 15-25× its cost; votes add nothing.
  With `fetch_on: [relevant, borderline]` luna + v2 reached recall 95.2 % at precision 96.8 % on test, ≈ $0.15 per
  1,000 records. A title-only question is not good enough to replace "fetch every url document".
- **Gold v2:** `safety-relevance-v3` on luna: recall under `[relevant, borderline]` dev 100 % / test 97.5 %, test
  precision 97.1 %, ≈ $0.15 per 1,000; v3 widens without costing precision, its gain concentrated on `sr_only` items
  (luna test 11 → 15 / 15). The Shallow Review recall set is `evals/triage/sr2025-recall`.

## Use
Once `tools/fetch_eval_texts.py` has fetched `items.jsonl`:

    STACK_ENV_FILE=.env uv run --no-sync stack eval triage --set 2026-10-12a --plan configs/plans/batch-2026-10a.yaml \
        --split dev --models <ids> [--repeat 2] [--prompt safety-relevance-v3] --max-usd X

and re-score with `stack eval score --run <dir>` (gold `gold-v2.jsonl` by default). Without `--prompt` the plan's
pin is asked (`safety-relevance-v4`). The harness sends each item's frozen record, which predates the `Published:` /
`Source:` lines that v4 reads.

## Record (as written 2026-10-12)
The measurements and findings as recorded when the runs were made; the status at the top supersedes any
recommendation here. The runs are in a local evals store (not in the repository).

**Freeze.** 2026-10-12 13:30 UTC from a local store (not in the repository), documents head `r4j9tqbmy8n3d.documents`
(1,806 documents), prompt pin then `safety-relevance-v1`. Url channels: openai_blog 24, google_research 10,
anthropic_alignment_blog 8, transformer_circuits 6, openai_alignment_blog 6, anthropic_cards 6; openai_blog was
66 % of the corpus and 12 % of the set. The 39 hard cases were picked first by term heuristics. `production_asks`:
140 items have an abstract or excerpt; the 60 url title-only probes (no basis → fetched by the sql clause /
`no_basis_fetches`) measure whether a title-only question would be usable for url channels. DeepSeek (eval overlay)
and Gemini may not see the 20 `restricted` items (llm.yaml egress); those rows are `skipped[egress]` and outside
their denominators. Excluded: paper/forum rows without abstract and excerpt (openalex 15, semantic_scholar 3,
alignmentforum 1).

**Gold v1 labelling.** Rubric written from the earlier pipeline's goals and the production definitions, not the
prompt text. Opus 3 sub-agents (`labels/opus-batch-0*.jsonl`), Codex `gpt-6-sol` 5 calls of 40
(`labels/codex-batch-0*.jsonl`); disagreements `labels/disagreements.jsonl` (archived, D795;
`tools/fetch_eval_texts.py`) → `labels/adjudication.jsonl`.

| | value |
|---|---|
| raw agreement (3-way) | 165 / 200 (82.5 %) |
| Cohen's κ, 3-way (dev / test) | 0.705 (0.758 / 0.654) |
| fetch agreement, κ | 184 / 200, 0.836 |
| contested | 35 (17.5 %): url 20, paper 10, forum 5 |
| adjudicator sided with | Opus 31, Codex 4, third value 0 |

Confusion (Opus | Codex): relevant|relevant 83, not_relevant|not_relevant 76, borderline|borderline 6,
relevant|borderline 12, borderline|not_relevant 12, borderline|relevant 7, not_relevant|borderline 2,
relevant|not_relevant 1, not_relevant|relevant 1.

Caveat: the adjudicator is an Opus model and sided with the Opus labeller 31/35 times, so contested gold leans toward
Opus's reading; a calibration slice (15 contested + 5 agreed items, labelled by the maintainer; a file not in this
repository) checked this, and its labels are gold v2's reference. Rubric ambiguities the adjudicator named:
background knowledge of a known document vs the thin-records rule (~10 items), governance posts that "name nothing"
(~8), whether factuality/hallucination work counts as honesty (3), the threshold of a "real safety component" (5),
feature launches vs model releases, model welfare.

Base rates (gold v1):

| | n | relevant | borderline | not_relevant | fetch |
|---|---|---|---|---|---|
| all | 200 | 99 | 21 | 80 | 60 % |
| dev / test | 100 / 100 | 48 / 51 | 9 / 12 | 43 / 37 | 57 % / 63 % |
| paper | 100 | 47 | 4 | 49 | 51 % |
| forum | 40 | 13 | 6 | 21 | 48 % |
| url (title only) | 60 | 39 | 11 | 10 | 83 % |
| kw_high / kw_low | 106 / 94 | 71 / 28 | 8 / 13 | 27 / 53 | 75 % / 44 % |
| hard | 39 | 17 | 9 | 13 | 67 % |

### Model and prompt choice on gold v1 (2026-10-12)
Primary metric: recall of gold fetch under the then plan's `fetch_on: [relevant]` (a failed or `borderline` answer
is not fetched); "rec r+b" = recall under `[relevant, borderline]`; precision of `relevant`; κ = 3-way vs gold. Cost
= settled USD per 1,000 documents. DeepSeek and Gemini exclude the 20 `restricted` items (egress).

**Prompt v1 (`safety-relevance-v1`), dev (100 items × 2 repeats) / test (100 × 1):**

| model | recall dev / test | rec r+b dev / test | precision dev / test | 3-way acc dev / test | κ test | repeat agree | $/1k |
|---|---|---|---|---|---|---|---|
| anthropic/claude-haiku-5.5 | 65.8 / 69.8 % | 93.0 / 96.8 % | 80.6 / 89.8 % | 66 / 71 % | 0.530 | 93 % | 0.17-0.18 |
| openai/gpt-6-luna | 78.1 / 71.4 % | 94.7 / 95.2 % | 90.8 / 93.8 % | 85 / 79 % | 0.653 | 90 % | 0.18-0.23 |
| deepseek/deepseek-v4.1-flash | 62.0 / 67.9 % | 86.0 / 88.7 % | 88.6 / 100 % | 77.5 / 78.7 % | 0.648 | 82 % | 0.09-0.10 |
| google/gemini-3.5-flash-lite | 71.0 / 69.8 % | 91.0 / 100 % | 73.2 / 82.2 % | 67 / 70.8 % | 0.525 | 93 % | 0.58-0.60 |
| anthropic/claude-haiku-4.5 (then pinned) | 57.9 / 65.1 % | 74.6 / 79.4 % | 86.8 / 100 % | 68.5 / 73 % | 0.550 | 93 % | 2.03-2.10 |

Zero parse or schema failures. McNemar (fetch correctness, repeat 1, vs haiku-4.5): luna p = 0.015 on dev.

**Prompt v2 (`safety-relevance-v2`, written from dev errors only), the two best cheap models:**

| model, prompt | recall dev / test | precision dev / test | 3-way acc dev / test | κ test | production-asked test: recall / precision | $/1k |
|---|---|---|---|---|---|---|
| gpt-6-luna v1 | 78.1 / 71.4 % | 90.8 / 93.8 % | 85 / 79 % | 0.653 | 89.2 / 91.7 % | 0.18-0.23 |
| gpt-6-luna **v2** | 92.1 / 87.3 % | 90.5 / 96.5 % | 87 / 84 % | 0.716 | 94.6 / 94.6 % | 0.12-0.15 |
| haiku-5.5 v1 | 65.8 / 69.8 % | 80.6 / 89.8 % | 66 / 71 % | 0.530 | 86.5 / 86.5 % | 0.17-0.18 |
| haiku-5.5 v2 | 83.3 / 79.4 % | 94.1 / 100 % | 83 / 84 % | 0.731 | 83.8 / 100 % | 0.19-0.21 |

Paired McNemar v1 → v2 on test fetch correctness: luna 79 → 90 / 100 (p = 0.003), haiku-5.5 76 → 87 (p = 0.013);
on dev: luna p = 0.30, haiku-5.5 p < 0.001. Title-only probe (url, production does not ask): with v2, luna fetches
65-67 % of lab-blog titles and keeps 77-81 % of gold-fetch ones (precision 100 %): not good enough to replace
"fetch every url document", whose cost is one page download.

Recommendation at the time: triage question on `openai/gpt-6-luna` with `safety-relevance-v2` (≈ $0.15 per 1,000
records); with v1, any model loses 20-40 % of gold-fetch records under `fetch_on: [relevant]`.

### Wider comparison and provider caching (2026-10-12; envelope: triage ≤ $0.5-1 per 1,000)
Prompt v2 throughout. `stack eval combine` scores scaffolds offline over the collected rows (no extra calls):
votes = majority over repeats (ties → the more inclusive verdict); escalate = second model's answer where the first
answered `borderline` (or confidence < 0.9); cost = first pass on all items + second pass on escalated items.
$/1k "settled" = OpenRouter's billed cost with provider prompt caching; "no cache" = tokens × list price.

| config (test split, 100 items) | recall [rel] | recall [rel+bl] | precision [rel] | 3-way acc | κ | $/1k settled | $/1k no cache |
|---|---|---|---|---|---|---|---|
| gpt-6-luna, 1 call | 87.3 % | 95.2 % | 96.5 % | 84 % | 0.716 | 0.14 | 0.23 |
| gpt-6-luna, 3 votes | 87.3 % | 95.2 % | 94.8 % | 83 % | 0.697 | 0.38 | 0.71 |
| gpt-6-luna-pro, 1 call | 85.7 % | 93.7 % | 96.4 % | 84 % | 0.716 | 0.61 | 0.90 |
| luna → claude-haiku-5.5 on borderline (5 %) | 88.9 % | 95.2 % | 96.6 % | 85 % | 0.732 | 0.15 | 0.25 |
| luna → claude-sonnet-5.5 on borderline (5 %) | 88.9 % | 95.2 % | 96.6 % | 85 % | 0.732 | 0.30 | 0.52 |
| luna → claude-sonnet-5.5 on confidence < 0.9 (22 %) | 82.5 % | 95.2 % | 98.1 % | 86 % | 0.757 | 1.11 | 1.64 |
| claude-haiku-4.5 v1 (then pinned) | 65.1 % | 79.4 % | 100 % | 73 % | 0.550 | 2.03 | 2.03 |

Dev (single calls, for the stronger models run only there): claude-sonnet-5.5 recall 84.2 % / precision 98 % / acc
90 % / κ 0.827 at $3.45 (no cache $7.02); gemini-3.8-flash 84.0 % / 100 % / 89 % / κ 0.806 at $2.35 (restricted items
excluded); luna-pro 93.0 % / 93.0 % / 87 % at $0.64. The strong models agree more with gold on the 3-way label but
park more gold-fetch records in `borderline`, so under `fetch_on: [relevant]` they do not beat luna, at 15-25× its
cost. Votes add nothing (luna's repeats agree 95 %); escalating luna's borderlines gains one item on test.

Best within the envelope at the time: luna + v2 with the gate fetching on `[relevant, borderline]` — recall 95.2 %
(test) / 100 % (dev) at precision 96.8 % / 90.5 %, ≈ $0.15 per 1,000 records with caching (no extra calls). Second
choice if the plan kept `fetch_on: [relevant]`: luna + v2 with borderline escalated to haiku-5.5 (≈ $0.15 per 1,000).

**Provider prompt caching** (dev runs; `stack eval score` provider-cache table; `stack eval audit-prompts`). Every
triage request puts the instructions first as the system message (≈ 910 tokens v1, ≈ 1,180 v2) carrying the
Anthropic `cache_control` breakpoint, and the record last as the user message; no `cache:` marker exists in
`configs/` (caching is the client's fixed behaviour). Cached share of prompt tokens: haiku-5.5 63 %, sonnet-5.5 67 %,
deepseek 68 %, luna 40 % (v1) / luna-pro 50 %, haiku-4.5 0 %, gemini-3.5-flash-lite and gemini-3.8-flash 0 %. $/1k
settled vs no cache: luna 0.18 vs 0.22 (v1), deepseek 0.09 vs 0.24, haiku-5.5 0.18 vs 0.32, sonnet-5.5 3.45 vs 7.02,
haiku-4.5 and gemini unchanged. OpenRouter bills luna's cache writes at $0.125/M (above its $0.10/M input price), so
a short luna run gains little; the gain grows with batch size. Haiku-4.5's 0 % is consistent with its prefix being
below Anthropic's minimum cacheable length for that model (not verified against Anthropic's documentation).

### The amendment's items (2026-10-12)
Purpose: the first 200 items hold few documents of the widened Shallow Review scope (`docs/scope/shallow-review.md`
(c)-(d)). The original top-level `items_sha256` and `counts` of `manifest.json` describe the 200-item freeze. Chosen
by query and title only: 15 rows of the development store with a basis, not already in the set (frozen through
`freeze.triage_cands`, `origin: store`), 15 LessWrong/AF posts (forum search, any karma; the record is the
`lesswrong` channel's, search body minted as the collector mints it) and 11 arXiv papers (API keyword search),
`origin: external`, `doc_id` `ext:<source>:<id>`. Intent at selection: 27 likely in (decision theory, agent
foundations, anthropics, acausal trade, corrigibility, natural abstractions, field critiques and strategy, model
psychology, gradual disempowerment, pluralistic alignment, game theory of AI agents, cross-disciplinary transfers,
AGI metrics, superalignment) and 14 hard negatives (anthropic principle in cosmology, microbial cooperation,
agent-based epidemiology, game theory of markets and of people, Newcomb for humans, anthropology, behavioural
decision theory, human psychology, vocabulary collisions). Split 50/50 per channel group × intent.

### Widened scope (Stack + Shallow Review), gold v2, `safety-relevance-v3` (2026-10-12)
**Rubric** `evals/rubrics/triage-v2.md`: scope = Stack (route A, unchanged) ∪ Shallow Review (route B: agent
foundations, decision theory, anthropics and acausal trade, corrigibility, abstractions, model psychology,
multi-agent and alignment-target work, cross-disciplinary transfers, field critiques, AGI metrics; source
`docs/scope/shallow-review.md`), plus the `scope` label and calibration rules taken from the maintainer's part-1
labels: fetch as hedging, topic not quality, indirect evidence, authors as signals, content over premise,
model-internals at least borderline, lab-blog / Zvi fetching as a gate source rule, not a verdict.

**Gold v2 labelling.** Relabelled: the 101 v1 `not_relevant`/`borderline` items, a seeded 15 % control of v1
`relevant` (15) and the 9 remaining calibration items, plus the 41 SR-heavy items. The adjudicator is Opus because
Codex agreed with the maintainer's part-1 labels on 10/20 verdicts, Opus on 15/20. The maintainer's 20 part-1 labels
(`labels-v2/tomas-part1.jsonl`, verdicts mapped onto the rubric's) are the reference where present; 1 override.

| | value |
|---|---|
| verdict agreement Opus/Codex (166) | 134 (80.7 %), κ 0.677 (relabel subset alone: 76 %, κ 0.569 — enriched for borderline/negatives) |
| scope agreement | 137 (82.5 %), κ 0.711 |
| adjudicator sided with (verdict) | Opus 26, Codex 6, third 0 (Opus lean again; the maintainer's labels cover their 20) |
| v1 → v2 verdict flips (125 relabelled old items) | 22: not_relevant→borderline 9, →relevant 1; borderline→relevant 7, →not_relevant 3; relevant→borderline 2 |
| base rates v2 (241) | relevant 132, borderline 24, not_relevant 85; scope core 54 / sr_only 27 / out 85 / null 75 |

**Agreement with the maintainer's part-1 labels (20 triage items):** gold v1 verdict 15/20, fetch 17/20; gold v2
before applying his labels 19/20, fetch 20/20. Inflated: rubric v2 encodes his calls on several of these very items.

**Results** (gold v2; dev 121 / test 120 items incl. SR-heavy; recall of gold fetch under `[relevant]` /
`[relevant, borderline]`; $/1k settled with provider caching):

| model, prompt | dev rec | test rec | test precision | test acc / κ | sr_only recall test ([rel] / [rel+bl], n 15) | $/1k |
|---|---|---|---|---|---|---|
| gpt-6-luna, v2 | 86.8 / 90.8 % | 82.5 / 90.0 % | 97.1 % | 85.0 % / 0.727 | 10 / 11 | 0.14-0.16 |
| gpt-6-luna, **v3** | 89.5 / **100 %** | 85.0 / **97.5 %** | 97.1 % | 85.0 % / 0.733 | 13 / **15** | 0.15-0.16 |
| haiku-5.5, v2 | 77.6 / 89.5 % | 73.8 / 93.8 % | 100 % | 84.2 % / 0.732 | 4 / 11 | 0.25 |
| haiku-5.5, v3 | 76.3 / 96.1 % | 77.5 / 98.8 % | 98.4 % | 83.3 % / 0.717 | 10 / 14 | 0.18-0.19 |
| deepseek-v4.1-flash, v2 | 72.1 / 80.9 % | 71.6 / 83.6 % | 98.0 % | 82.1 % / 0.692 | 10 / 10 (n 14) | 0.10-0.12 |
| deepseek-v4.1-flash, v3 | 79.4 / 89.7 % | 79.1 / 89.6 % | 100 % | 86.8 % / 0.771 | 12 / 13 (n 14) | 0.12-0.13 |

Paired v2 → v3 fetch correctness (McNemar, [rel+bl]): luna test 110 → 112 / 120 (p 0.79), dev 106 → 105 (p 1.0);
deepseek test [rel] 86 → 92 / 106 (p 0.03). v3 widens without costing precision; its gain is concentrated on
`sr_only` items (luna dev 7 → 12 / 12, test 11 → 15 / 15). On the maintainer's 20 part-1 items, fetch under
[rel+bl]: luna v2 18/20, v3 19/20 (8 of the 20 are url title-only probes production does not ask).

Recommendation at the time: `safety-relevance-v3` on `openai/gpt-6-luna` with `fetch_on: [relevant, borderline]`
(as `dev-v2-m4` already fetched): ≈ $0.15 per 1,000 records (envelope ≤ $0.5-1). Proposal for the gate (not the
prompt): source rules that fetch every frontier-lab blog post (url documents without a basis already are) and every
post by Zvi Mowshowitz unless clearly unrelated to AI (from the maintainer's labels on tri-0063, 0080, 0139 and 0158).
