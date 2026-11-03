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
15. Agreement: 13 / 15 papers identical; per author (382): roster ids 97.4 %, org strings 97.6 %, both 96.1 %. The two
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

## Record (as written 2026-10-12)
The measurements, per-case findings and open recommendations as recorded when the runs were made; the status at the
top supersedes the recommendation here. The runs are in a local evals store (not in the repository).

**Freeze.** 2026-10-12 17:00 UTC from a local store (not in the repository), documents head
**`rfm2d6vzwvmdd.documents`**, git `e1b1c6d` (dirty tree), by

    python -m stack.evals.freeze affiliation --store <store> --set 2026-10-12b-bylines \
      --out evals/affiliation/2026-10-12b-bylines --packets <scratch> --split test \
      --doc-ids sp0x07vpffcnn,s7rdmsgrcemba,s1we6yr5rkdac,sc05qpxeasmc7,sx9vtv58vxps5,s3pww9ps48ygd,s5r484db3m4m1,s9ct5ajmy2ncr,sm1x6020cys3w,s1p89tp2ta498,szcv4fwr9dmkd,s8atg13bhgzzt,sc14r8zj7jrb7,scx1fdpb8tnfa,shqzbgphe2waj

Packets as `2026-10-12a` (`_roster`, `_user_message`); `--doc-ids` selects exactly these pool documents. Item ids
follow hash order, not the case numbers. All 15 are licence `open`, so no model is egress-skipped (deepseek
included). The cases were hand-picked from the development store after the failure had been seen in a real run,
with an expected mapping per case.

**Labels.** Labeller A was the Opus agent running the set itself (not a separate sub-agent), labelling from the frozen
packet and rubric before seeing Codex's labels or any model output; it had read the expected mappings beforehand, so A
is not blind to them. Labeller B: Codex `gpt-6-sol` (read-only, 3 packets of 5). Roster ids in A's labels are what
production's `Canon.orgs_in` resolves from each gold org string (the scorer resolves model output the same way); the
packet's `roster_entries` missed NVIDIA and Microsoft on aff-0006 because the legend prints `_NVIDIA —_` and the
markdown underscore defeats the alias's `\b` (see "Findings outside the models"). Agreement by
`python -m stack.evals.gold merge affiliation --set 2026-10-12b-bylines`. The two adjudicated disagreements:
- aff-0006: B kept legend 13 "Working Paper" (3 authors) and 15 "Independent" (2 authors); gold drops both (neither
  is an organisation). B also took roster ids from the packet (no nvidia/microsoft); gold uses Canon's.
- aff-0008: B left Yufan Zhao, Yao Zhao, Tao Di, Yulan Yan empty (not securely tied); gold gives them
  "Microsoft Redmond, WA, USA": each separated block carries that author's own email (yufzhao@, yaozhao2@, taodi@,
  [email]) and all four blocks are identical, so no pairing choice changes the answer.

Gold conventions that move the numbers:
- **Team bylines** (aff-0011, aff-0015): no person is printed in the byline, so per the rubric ("authors_meta lists a
  person the byline does not print → []") every author is empty. Both labellers agree. Every model under test instead
  gives the team's organisation to every author (layout 1). That is a defensible reading, not a misattribution; these
  two items are reported separately below.
- **Units** are copied as printed: "Pinterest San Francisco, CA, USA", "University of Calabria, Italy and TU Wien,
  Austria" (one unit), "Universit´e d’Orl´eans" with its artefacts. A model that splits a unit, drops the city or
  repairs accents loses string pairs on the all-org metric without being substantively wrong.
- The expected mappings of the hand-picked cases all match gold (case 10: per-author mapping "not derivable" → empty).

**Runs.** `20261012T170553Z_test_bbbaed75` (four models, settled $0.1745), then `20261012T171212Z_test_a4f3222f`
adding `anthropic/claude-sonnet-5.5` (the four replayed from the local cache; settled $0.3469 for sonnet). Scorer
table (all 15 items):

| model | tracked P/R/F1 | all P/R/F1 | broadcast | roster mismatch | parse fail | egress skip | USD/1k settled / no cache | latency p50/p90 ms |
|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 0.543/1.000/0.704 | 0.571/0.959/0.716 | 1 | 0 | 0 | 0 | 0.814 / 0.832 | 6018 / 9990 |
| deepseek/deepseek-v4.1-flash | 0.543/1.000/0.704 | 0.565/0.860/0.682 | 8 | 2 | 0 | 0 | 1.115 / 1.560 | 15563 / 53056 |
| anthropic/claude-haiku-4.5 | 0.543/1.000/0.704 | 0.477/0.860/0.614 | 3 | 2 | 0 | 0 | 8.271 / 8.271 | 5095 / 12487 |
| anthropic/claude-haiku-5.5 | 0.543/1.000/0.704 | 0.550/0.909/0.685 | 2 | 1 | 0 | 0 | 1.433 / 1.573 | 5495 / 11293 |
| anthropic/claude-sonnet-5.5 | 0.543/1.000/0.704 | 0.505/0.851/0.634 | 2 | 0 | 0 | 0 | 23.125 / 24.289 | 4959 / 14831 |

Haiku-5.5's $/1k reads 1.257 / 1.397 when replayed from its cache entries in the second run, against 1.433 / 1.573
live in the first; the live figure is quoted (the difference was not explained). The 13-item table above was
recomputed from the run's `rows.jsonl` (not in the repository) with the scorer's `affiliation_units`. Tracked
organisations here are few (deepmind 8, microsoft 7, nvidia 4 author pairs), and all of them sit in clear layouts;
the stress cases show up only in the all-org metric. Per-author byline coverage is 75.9 % for every model because
production asks about the first `MAX_AUTHORS` = 60 metadata authors only (aff-0004: 63, aff-0011: 149).

**Per-case findings.** All five models are right, with no invented organisation, on aff-0001 (footnote "is with" /
"the other authors"), aff-0004 (63 names, no affiliation), aff-0007, aff-0014 (markers without legend) and aff-0008
(all five give the separated Microsoft authors Microsoft; sonnet drops the city from the inline units). Where they
differ:
- **aff-0002 (case 1, 8 names / 6 institutions, no markers) decides it.** luna, haiku-5.5 and sonnet-5.5 leave all
  eight empty. **haiku-4.5 pairs by position**, cycling through the list (Zhao→UNSW, Gao→JHU, Xiao→CMU, Wang→Fudan,
  Xuan→Tokyo, Joshi→RIKEN AIP, Dredze→JHU, Huang→UNSW): 8 wrong pairs, the failure seen in the real run,
  reproduced. **deepseek broadcasts** all six institutions to all eight authors (48 wrong pairs).
- aff-0012 (control) and aff-0011: **deepseek returned a single author record** (roster mismatch 1 of 7 and 1 of
  60), dropping six correct SMU affiliations on a clean byline.
- aff-0003 (control): all five give everyone Pinterest. Only luna copies the unit as gold does; the others write
  "Pinterest" (a granularity loss), and **haiku-4.5 splits the city off as a second organisation** ("San Francisco,
  CA, USA" on all 10 authors, flagged as a broadcast). luna omits the separate "Work done while at Pinterest" unit
  of Tianyou Wang and Joshi; the others' single "Pinterest" coincides with it.
- aff-0006 (case 6): all five keep "Working Paper" (3 authors) and "Independent" (2); luna and sonnet also give
  "Independent" to the metadata fragments " Jun" / " Lu". Same junk for everyone, so it does not separate models.
  haiku-4.5 returned 35 of 37 records.
- aff-0013 (case 3): all five keep Capalbo's dual affiliation, as two units; gold's single unit makes this a
  uniform string-granularity miss, not a dropped affiliation.
- aff-0009 / aff-0005: haiku-4.5 and sonnet repair accents or apostrophes (`Université`, `People's`), luna partly;
  substantively right, string misses only. aff-0010: sonnet adds an extra USTC unit to Jingrun Chen (markers 6,3),
  one real wrong pair.
- Team bylines (aff-0011, aff-0015): luna, both haikus and sonnet give the team organisation to every author they
  return (Alibaba is tracked: 16 tracked false positives for every model); deepseek returns one record on aff-0011.

**Recommendation for `extract_main`, as written.** Switch from `anthropic/claude-haiku-4.5` to
**`openai/gpt-6-luna`**. On this set haiku-4.5 reproduces the position-assignment failure on the one marker-less byline
and adds junk city units; luna is correct on every stress case, best on all-org F1 without the team bylines (0.935 vs
0.788) and costs ≈ $0.81 per 1,000 papers against $8.27. haiku-5.5 is the substantive runner-up (also correct on case
1, 0.894, ≈ $1.43/1k); sonnet-5.5 brings no gain at ≈ $23/1k; deepseek-v4.1-flash is out (broadcast on case 1,
dropped authors on two papers, p90 latency 53 s).

Limits: 15 papers, 1 repeat; the decisive evidence is one paper (aff-0002), so a `--repeat 3` of luna and haiku-5.5
on this set (≈ $0.07) would firm it up. Two questions were left open: the team-byline convention (the rubric says
empty, every model says the team's organisation), and whether "Working Paper" / "Independent" style legend entries
should be filtered after extraction, since every model keeps them.

**Findings outside the models.**
- The freezer's `roster_entries` (and anything else that runs `Canon.orgs_in` over raw markdown) misses organisations
  wrapped in markdown emphasis: `_NVIDIA —_`, `_Microsoft —_` in aff-0006 do not match `(?i)\bnvidia\b`, because `_`
  is a word character. Model output is resolved on clean strings, so scoring is unaffected here.
- Production's `MAX_AUTHORS` = 60 leaves authors 61+ of long papers (aff-0011, aff-0004) without any affiliation.
