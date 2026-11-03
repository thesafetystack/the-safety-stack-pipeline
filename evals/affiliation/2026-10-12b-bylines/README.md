# Affiliation eval set `2026-10-12b-bylines` (unusual-byline stress check)

Superseded: historical. It decided the move of `extract_main` (the LLM tier of `annotate.affiliation`) from
`anthropic/claude-haiku-4.5` to `openai/gpt-6-luna` (D201); production affiliation output is now measured by
`evals/judge-2026-10-25j/affiliation`.

Purpose: `2026-10-12a` had clear bylines only and could not see the failure that had kept haiku: organisations
assigned by position, or broadcast, on marker-less bylines. This set is 15 papers hand-picked from a development
store: 10 stress cases, 1 extra (fused legend), 4 clean controls. It is a stress check, not prompt work: every item is
`split: test`.

## Items (`items.jsonl`, 15 papers, 382 metadata authors)

Frozen 2026-10-12 by `python -m stack.evals.freeze affiliation --doc-ids … --split test` (the manifest records
`doc_ids` and `split_forced`); same item format and user message as `2026-10-12a` (production's `header_window`
8000 + 2000 chars). All 15 are licence `open`; arxiv 11, openalex 4; no XML. `items.jsonl` is not in the repository
(long third-party excerpts, D795): `tools/fetch_eval_texts.py` fetches it into the eval-text cache.

| item | doc_id | case | layout |
|---|---|---|---|
| aff-0001 | sc05qpxeasmc7 | 4 | names only; "X is with A ..., the other authors are with B" footnote |
| aff-0002 | sp0x07vpffcnn | 1 | 8 names, 6 institutions, no markers (untied) |
| aff-0003 | scx1fdpb8tnfa | control | inline per-author blocks, all Pinterest; two "Work done while at Pinterest" |
| aff-0004 | s9ct5ajmy2ncr | 8 | 63-name run-on byline, no affiliation anywhere |
| aff-0005 | szcv4fwr9dmkd | extra | lettered legend, entries d and e fused on one line |
| aff-0006 | s3pww9ps48ygd | 6 | 37 authors, 21-entry em-dash legend incl. "Working Paper", "Independent" |
| aff-0007 | shqzbgphe2waj | control | single footnote legend, all Google DeepMind |
| aff-0008 | s7rdmsgrcemba | 2 | flattened two-column ACM byline; 4 names separated from their blocks |
| aff-0009 | sx9vtv58vxps5 | 5 | markers + one-line merged legend, accent-garbled (`Universit´e`) |
| aff-0010 | s8atg13bhgzzt | control | numbered legend, multi-marker authors |
| aff-0011 | s1p89tp2ta498 | 10 | team byline "LLM-Core Xiaomi", 149 metadata authors |
| aff-0012 | sc14r8zj7jrb7 | control | inline, all Singapore Management University |
| aff-0013 | s1we6yr5rkdac | 3 | inline; one dual affiliation "University of Calabria, Italy and TU Wien, Austria" |
| aff-0014 | s5r484db3m4m1 | 7 | 46 authors with markers 1-31, no legend in the window |
| aff-0015 | sm1x6020cys3w | 9 | team byline "Multimodal and Industrial AI Team, Alibaba", no person printed |

## Labels (`labels/`, `gold.jsonl`)

Rubric `evals/rubrics/affiliation.md`, every author of every paper. Labeller A: Opus (it had seen the expected
mappings of the hand-picked cases, so it is not blind to them); labeller B: Codex `gpt-6-sol`, independently, on all
15. Agreement: 13 / 15 papers identical; per author (382) both roster ids and org strings 96.1 %. The two
disagreements (aff-0006: "Working Paper" and "Independent" are not organisations; aff-0008: four separated
Microsoft blocks tied by each author's own email) were adjudicated against the window (`labels/adjudication.jsonl`;
`contested: true` in gold).

Gold conventions that move the numbers: team bylines (aff-0011, aff-0015) print no person, so per the rubric every
author is empty, while every model gives the team's organisation to every author; units are copied as printed
(including accent artefacts), so a model that splits a unit or repairs accents loses string pairs on the all-org
metric without being substantively wrong.

## Result (2026-10-12, test, 15 items × 1 repeat)

Tracked F1 is 0.704 for every model (the same 16 false positives: Alibaba on all aff-0015 authors, the team-byline
convention). Excluding the two team bylines (13 items):

| model | tracked P/R/F1 | all P/R/F1 (tp/fp/fn) |
|---|---|---|
| openai/gpt-6-luna | 1.000/1.000/1.000 | **0.913/0.959/0.935** (116/11/5) |
| anthropic/claude-haiku-5.5 | 1.000/1.000/1.000 | 0.880/0.909/0.894 (110/15/11) |
| anthropic/claude-sonnet-5.5 | 1.000/1.000/1.000 | 0.805/0.851/0.827 (103/25/18) |
| anthropic/claude-haiku-4.5 | 1.000/1.000/1.000 | 0.727/0.860/0.788 (104/39/17) |
| deepseek/deepseek-v4.1-flash | 1.000/1.000/1.000 | 0.623/0.860/0.722 (104/63/17) |

The decisive item is aff-0002 (8 names, 6 institutions, no markers): luna, haiku-5.5 and sonnet-5.5 leave all eight
empty; haiku-4.5 pairs by position (8 wrong pairs); deepseek broadcasts all six institutions to all eight authors.
Cost per 1,000 papers: luna ≈ $0.81, haiku-4.5 ≈ $8.27. Limits: 15 papers, 1 repeat, one decisive paper.

Findings outside the models: `Canon.orgs_in` over raw markdown misses organisations wrapped in emphasis
(`_NVIDIA —_`: `_` is a word character); production asks about the first `MAX_AUTHORS` = 60 metadata authors only.
