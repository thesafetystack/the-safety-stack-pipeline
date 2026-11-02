# Collection yield: which queries, authors and feeds flood the pipeline

Status: analysis, 2026-10-12 (round 4, track F), extended 2026-10-13 by §9.4-9.5 (T-RUN, T-ARX) and §10 (T-CUTS).
Read only over store snapshots; no store writes, no network, no LLM calls. Public material. What became of the cuts
(`docs/DECISIONS.md`; §6.1 and the §9.2 status column are as written before these decisions): adopted are C1 and the S2
half of C2 (channels.yaml 1.9.0, D229), C5 (roster fixes, D229), the OpenAlex half of C2, C3, C6 and C9 (1.11.0, D270),
§7.1 (`metadata.matched_by`, D228), §7.2 (arXiv categories in the record blob) and §10's backfill cuts (1.12.0,
D304-D306). Not adopted: C7 (kept as is, D270), C8 (measured, D277), C10 (no feed floor, D270). C4's name filter was not
built: std-v3 sweeps the roster's OpenAlex author ids instead and the arXiv name sweep is off (D277, D285).
Audience: the v4 pipeline's owners. Terms (`sources`, `coverage`, `documents`, gather, triage gate, screen, concept
groups, `needs_ai_context`) are those of `docs/DESIGN.md` and `configs/channels.yaml`; `Dnnn` are `docs/DECISIONS.md`.

## 0. Summary

**Volume.** Measured windows scaled to 30 days at the full configuration, the collectors measured here bring roughly
4,000 documents a month: the arXiv author sweep about 1,300 (projected from a 10 % roster sample), OpenAlex's
`umbrella` group about 960 and `jailbreak` about 470, the LessWrong AI-tag sweep about 370, the LessWrong/AF keyword
sweeps 200-400, the SR keyword families a few hundred, and the blog feeds about 70 (plus one-time backlogs of 3,000).
The OpenAlex institution sweep and its other 40 concept groups (std-v2) were not in these windows.

**Worst offenders** (volume × irrelevance, section 4): the "steerability" group on Semantic Scholar and OpenAlex
(both stem it to "steer": 613 of 836 S2 rows, 260 of 365 OpenAlex rows; my read of titles 4-8 relevant of 25),
nine high-volume author names in the arXiv sweep (46 % of its volume, 6 of 27 labelled relevant), the arXiv
scoped-category family (64 a month, none clearly relevant in 25 titles), "heuristic arguments" on S2 (0 of 14), the
LessWrong keyword sweep beyond the AI-tag sweep (18 extra posts a week, 3 of 15 labelled relevant), and
google_research (3 of 17 labelled relevant).

**Best performers** (section 5): by rate, the Anthropic alignment blog (21/21), Anthropic system cards (13/13),
transformer-circuits (12/12), LessWrong AI-tag posts with karma ≥ 25 (27/30), S2 `superalignment` (23/27), S2
`alignment-targets` (10-13 of 14 read), OpenAlex `jailbreak` (25/34). By relevant volume: the arXiv author sweep,
OpenAlex `umbrella` and `jailbreak`, and the LessWrong AI tag (each 200-500 relevant a month).

**Proposed cuts** (section 6, table 6.1): about −600 to −700 documents a month (−15 to −18 %) for an estimated loss of
2-3 labelled-relevant documents in the samples behind them (refined in section 9), plus a one-time −700 in feed
backlogs. The largest irrelevant volumes (OpenAlex `umbrella`, the author sweep) also carry the largest relevant
volumes; they stay with the triage question ($0.15 per 1,000 asked, RUNLOG M4.T4.2) rather than being cut at
collection.

**Roster defects** (section 3.2): 47 roster entries written `Last, First` are queried but every hit is rejected by
the name check; 19 of those researchers (plus `Stuart_Armstrong`) have no working entry, so they are never
collected; 57 roster entries collect nothing or nearly nothing.

**Concrete changes and redundancy** (section 9, added 2026-10-13): the exact config and code change per cut, and for
every relevant or plausible document a cut drops, whether another collector, query or newsletter link also reaches
it. Track P adopted C1, the S2 half of C2 and C5 in `673c3ee`. Net loss after redundancy:

- about 0 for C2, C3, C6 and C7;
- for C1, 3 clearly relevant papers over the 10-month S2 backfill;
- for C4, 1 labelled-relevant paper in the sample (≈ 10-30 a month at the full roster);
- for C8, measured in §9.4 (one 30-day tag sweep): 9 relevant or plausible a month, 5 clearly relevant by read, for a
  saving of 45 documents a month; recommendation there: do not adopt;
- for C9, 1-3 a year;
- for C10, a scope decision.

**Authors by OpenAlex id** (section 9.5, T-ARX, 2026-10-13): against the arXiv name sweep's 21-day sample, the
author-id family (rosters 1.3.0 and 1.3.1, every roster id queried) also has 42 of 125 papers; about 70 % of the
sweep's other papers are namesakes', but it misses an estimated 14 of our researchers' papers per 21 days in the
sample (split OpenAlex profiles, unresolved names), about 5 of which no other collector reaches (inferred); general
(not arXiv-only) scope kept; under $0.01 a month.

**Instrumentation gap** (section 7): no search collector records which query produced a `sources` row
(`metadata.query_digest` is NULL on every non-lookup row), and coverage counts are first-query-wins. Per-query yield
here is re-derived by re-matching titles and abstracts, which fails for fuzzy providers (LessWrong search, S2 and
OpenAlex stemming) and for arXiv categories. The numbers below are therefore estimates with small samples (section 8).

## 1. Data and method

**Snapshots** (copied 2026-10-12 21:05 UTC, before the round-4 URL re-key):

| Store | Files read | Rows |
|---|---|---|
| `v4/store-dev/` | documents head `r911yh013kctc` (full, fold.compact) + `rkf01qpwpmkfx` (acquire part); screen `rf3dzbg89bbra.screen` (screen-v8, luna) | 1,806 documents; 131 screened `ok` |
| `v4/store-sr-scratch/` (A2) | every `sources.*` and `coverage.*` file (the observation history); documents `r24enz7dn1yke` (gather, full) | 6,434 source rows; 4,898 documents = the dev store's 1,806 + 3,092 from the A2 windows |
| eval sets (repo) | `evals/triage/2026-10-12a/gold-v2.jsonl` (241), `evals/screen/2026-10-12a/gold-v2.jsonl` (100), `evals/triage/sr2025-recall/items.jsonl` (150) | 215 and 88 of the gold items are store documents |

The scratch store is a reflink copy of the dev store taken at 16:10 plus the A2 windows, so it holds every collector
window run so far; relevance labels come from the dev store and the eval sets, joined by `doc_id`.

**Windows** (dev windows from M2, SR windows from A2; options as run, not as in std-v2):

| Family | Collector, run | Query set | Window | Scale to 30 days |
|---|---|---|---|---|
| arXiv author sweep | `collect.arxiv_sweep` `rm20bsqsa24ck` | 234 of 2,269 roster names (fraction 0.1) | 21 d, 09-21..10-12 | overlap-corrected ×10.6 (below) |
| arXiv keyword family | `r41c9b34kpar7` | 21 groups (umbrella and the 20 SR groups) | 7 d | ×4.3 |
| arXiv scoped categories | `rr6cmd3d0cd5f` | cs.GT, cs.LO, math.LO, econ.TH, q-bio.NC AND an AI clause | 30 d | ×1 |
| OpenAlex dev | `rt9gy9wq4befh`, `r5qzaw56xjqhw` | `umbrella` (with `primary_topic.field.id:17`), `jailbreak` | publication dates 09-25..09-28 (4 d) | ×7.5 (lumpy) |
| OpenAlex SR | `r1m37283adw16` | 20 SR groups | 09-05..10-05 | ×1 |
| S2 dev | `r4y5da15b1cfk` | `superalignment` (`umbrella` failed) | since 2020, complete | backfill |
| S2 SR | `rys3ds2d0b9qb` | 20 SR groups, one page each, key-less; 11 groups failed (429), 2 truncated | since 2025-12-01 | not normalisable |
| LW/AF keyword sweep, dev | `collect.forums` `r3qmbwebvmj6c` | every term of the 42 groups, karma ≥ 25 | 7 d | ×4.3 |
| LW/AF keyword sweep, SR | `rkdfv6k3xnggf` | terms of the 20 SR groups, karma ≥ 25 | 30 d | ×1 |
| LessWrong AI tag | `collect.lw_graphql` `rnmc2ygtcn6na` | tag `ai`, no karma gate | 7 d | ×4.3 |
| LessWrong SR tags | `rm8de3cppxs8v` | 12 tags | 30 d | ×1 |
| Blogs and cards | `collect.blogs`, `collect.cards` | 21 feeds, 7 card channels (1 with rows) | whole feed backlog | posts dated in the last 12 months ÷ 12 |

**Full-roster projection.** A sampled paper with k roster authors (over the whole roster) is caught by a 10.3 %
name sample with probability 1 − 0.897^k; summing 1/that over the 125 sampled papers gives ≈ 925 papers per 21 days
for the full roster, ≈ 1,320 per 30 days (plain scaling by 2,269/234 would give ≈ 1,730; 49 of the 125 papers have
2-7 roster authors).

**Attribution** (source row → query). Collectors write no query on `sources` rows (section 7), so:

| Family | Method | Rows attributable to a single query, term or author |
|---|---|---|
| Blogs, cards, LW tags | channel = feed; run = tag set | all |
| arXiv author sweep | the collector's own `names_match` over the paper's authors against the queried names | 125 of 125 |
| arXiv keyword family | phrase re-match on title/abstract (+ AI clause for flagged groups) | 17 of 23 |
| arXiv scoped categories | none: entry categories are not stored | 0 of 64 (family level only) |
| OpenAlex dev | one group per run | 191 of 191 |
| OpenAlex SR | phrase re-match on title/abstract; fallback: the stem `steer` for steerability | 71 by phrase, 260 by stem, 34 none |
| S2 SR | phrase re-match; stem `steer` counted separately | 129 by phrase, 613 by stem only, 94 none |
| LW/AF keyword sweeps | phrase re-match on title + in-band body | 54 of 248 documents; LessWrong search is not a phrase match (D196) |

Coverage `kept` counts per query are kept beside the re-match: they are exact volumes but first-query-wins (the
harness keeps the first observation of a source in a run and counts `sources_repeat`), so they depend on query order.
Example: coverage credits 51 posts in 30 days to `kw:updateless decision theory` (the first term of its group); the
phrase occurs in 1 post of the whole 30-day sweep.

**Relevance signals.** No `stack_relevance` or `sr_relevance` annotation records exist in either store yet: the S6
migration added the slots as NULL and screen-v11 has not run on the dev store. Triage decisions carry little signal:
1,705 of 1,806 dev documents were fetched by rule (the M2-M3 sql gate fetched every row with a basis; url rows
without an abstract are fetched by the sql clause), and the question was asked for 82 documents (OpenAlex and S2).
What is used, per document, in this priority:

1. **gold screen v2** (100 items; LLM-labelled by Opus and Codex with adjudication, not human): relevant when the
   Stack or the SR verdict is `relevant` or `borderline`;
2. **screen-v8** verdicts in the dev store (131; a single Stack-oriented verdict);
3. **gold triage v2** (241 items, same labelling): fetch-worthy when the verdict is not `not_relevant`; url items are
   title-only probes;
4. **the triage question** (`safety-relevance`, luna): `relevant`/`borderline`.

"Labelled" below is the first available of 1-4 (screen-level where present, else triage-level). Two further signals
cover unlabelled documents:

- **Keyword proxy**: `stack.evals.freeze.proxy` (`kw_high` = a safety stem in the title or two distinct stems in title
  + abstract or forum body), calibrated on the labelled documents:

  | Channel group | P(labelled relevant \| kw_high) | P(\| kw_low) | n |
  |---|---|---|---|
  | paper | 0.70 | 0.29 | 117 / 89 |
  | forum | 1.00 | 0.50 | 15 / 30 |
  | url | 0.93 | 0.68 | 43 / 65 |

  "Proxy est." = the mean of these rates over a row's documents. It discriminates on papers and forum posts and hardly
  on url titles. It under-rates SR-scope vocabulary (decision theory, game theory) that carries no safety stem.
- **Title read**: I read 14-25 random titles of the larger unlabelled rows and counted clear and borderline
  Stack/SR relevance. Titles only, one reader (an LLM agent), so a rough check.

**Recall evidence.** Three kinds: labelled-relevant documents a cut would drop in these samples; the SR 2025 recall
sample (120 paper items with abstracts: which concept-group phrases and roster names reach them, ignoring date
windows); and overlap with other families inside a shared window (only the LessWrong tag/keyword pair shares one).

## 2. Volume and yield per family

| Family | Docs in window | ≈ per 30 d | Labelled: n, relevant | Proxy est. | Title read | Est. irrelevant per 30 d |
|---|---|---|---|---|---|---|
| arXiv author sweep | 125 | 1,320 | 65, 38 % (screen-v8 10/33) | 37 % | — | ≈ 820 |
| OpenAlex `umbrella` | 128 | 960 | 84, 45 % (question 30/61; gold screen 10/33) | 53 % | — | ≈ 530 |
| OpenAlex `jailbreak` | 63 | 470 | 34, 74 % | 59 % | — | ≈ 120 |
| LessWrong AI tag | 86 | 370 | 30, 90 % (all karma ≥ 25) | 65 % | karma < 25 (50 posts): 13 clear + 12 borderline | ≈ 130-180 |
| LW/AF keywords, 20 SR groups | 248 | 248 | 45, 67 % (7-day subset) | 73 % | 9 of 16 | ≈ 80 (most also in the tag sweep) |
| LW/AF keywords, 42 groups | 52 | 220 | 45, 67 % | 70 % | — | ≈ 70 (all 52 also in another family) |
| OpenAlex SR groups | 365 | 365 (≈ 105 after the steerability removal) | 4, 1 | 48 % | stem hits 4 + 4 / 25 | ≈ 270 (≈ 80 after) |
| S2 SR groups | 836 | — | 2, 1 | 50 % | stem hits 5 + 2 / 25 | — |
| arXiv keyword family | 23 | 100 | 0 | 49 % | — | ≈ 50 |
| arXiv scoped categories | 64 | 64 | 0 | 37 % | 0 + 4 / 25 | ≈ 60 |
| S2 `superalignment` | 31 | backfill | 27, 85 % | 65 % | — | — |
| LessWrong SR tags | 11 | 11 | 0 | 64 % | — | — |
| Blogs and cards (21 feeds + cards) | 3,040 | ≈ 70 (dated feeds) | 108, 78 % | 70 % | — | ≈ 15 |

"Est. irrelevant" uses the labelled rate where n ≥ 30, else the read or the proxy. The S2 SR window cannot be scaled:
it is a backfill to 2025-12-01 with 11 of 20 groups failed and two truncated at one page, and 280 of its 836 rows
carry a `published_at` before the floor (cause not verified: `publicationDateOrYear` may match on the year alone,
or the document's date comes from another member).

## 3. Per query, author and feed

### 3.1 Concept groups on OpenAlex, S2 and the arXiv keyword family (SR windows)

| Group | OpenAlex 30 d: coverage kept / re-matched | S2 since Dec: coverage kept / re-matched | arXiv kw 7 d | Read | SR 2025 sample (120 papers): match / only path |
|---|---|---|---|---|---|
| steerability | 272 / 6 phrase + 260 stem | 663 (truncated) / 17 phrase + 613 stem | 4 | OA 4 + 4 / 25; S2 5 + 2 / 25 | 1 / 0 ("steer" stem: 9) |
| game-theory-ai | 33 / 22 | failed | 1 | OA 1 + 2 / 14 | 1 / 1 |
| alignment-targets | 6 / 6 | 75 / 71 | 0 | S2 10 + 3 / 14 | 0 / 0 |
| heuristic-arguments | 7 / 4 | 46 (truncated) / 19 | 0 | S2 0 / 14 | 0 / 0 |
| scientist-ai | 11 / 3 | 36 / 14 (7 only via "non-agentic AI") | 0 | S2 4 + 2 / 14 | 1 / 0 |
| introspection | 13 / 9 | failed | 4 | — | 0 / 0 |
| natural-abstractions | 8 / 8 | failed | 2 | — | 1 / 1 |
| social-choice-ai | 6 / 5 | failed | 0 | — | 0 / 0 |
| other 12 SR groups | 0-2 each | 0-8 each or failed | 0-1 each | — | ≤ 1 each |
| unattributed | 34 | 94 | 6 | OA 2 + 1 / 14 | — |

Across all 62 groups, phrase matching reaches 52 of the 120 SR 2025 papers; roster authors reach 84 of 120. The
keyword groups are the minor recall path for SR-scope papers, the roster the major one. 70 of the 120 are `kw_high`.

### 3.2 arXiv author sweep (234 names, 21 days)

177 of the 234 sampled names returned nothing in 21 days, 31 returned one paper, 13 two, 4 three. Nine names returned
four or more and account for 57 of the 125 papers (46 %); their labelled relevance is 6 of 27 (22 %), against 19 of
38 (50 %) for the other names:

| Name (roster; yield of the query, not a judgement of the researcher) | Papers / 21 d | Labelled relevant |
|---|---|---|
| Zheng Zhang | 12 | 1 / 7 |
| Yu Cheng | 10 | 2 / 4 |
| Han Wang | 8 | 2 / 3 |
| Xiaomin Li | 6 | 0 / 4 |
| Hao Sun | 6 | — |
| Yi Dong | 5 | 1 / 3 |
| Pang Wei Koh | 5 | 0 / 4 |
| Anirudh Goyal | 5 | 0 / 1 |
| Jan Kautz | 5 | 1 / 3 |

Two causes, not verified per paper: common names whose `au:` query plus the name check matches several people
(namesake collisions), and prolific researchers whose output is mostly outside the scope. Scaled to the full roster,
about 4 % of names (≈ 90) would produce about 600 papers a month.

**Roster defects** (checked by calling `names_match` and `arxiv_author_query` on every entry):

- 47 entries are written `Last, First` (e.g. `Hilton, Benjamin`). The `au:` token comes out right, but `names_match`
  splits on whitespace, takes `Hilton,` as the first name and rejects every real author list, so every hit is
  discarded. 27 of them also have a working `First Last` entry; **20 have none**: 19 researchers (among them
  `Becker, Joel`, `Black, Sid`, `Hilton, Benjamin`, `Price, Sara`) and a junk `Llama Team, AI @ Meta`, so those
  researchers are queried and never collected.
- 10 single-token entries: 5 forum usernames with a working duplicate (`Andrew_Critch`, `Jacob_Hilton`,
  `CallumMcDougall`, `bilalchughtai`, `james.lucassen`), `Stuart_Armstrong` with none (never collected), two
  fragments of `Krishnamurthy Dvijotham`, and `Zhang` and `Aakanksha`, which match only a mononymous author or one
  whose first and last names both equal the token (3 hits for `Zhang` in the sample).

Together 57 entries collect nothing or nearly nothing, about 3 minutes of queries per full sweep at arXiv's
pacing.

### 3.3 LessWrong: tag sweep against keyword sweeps

In the 7 days both cover (10-05..10-12), the sweeps found 104 LessWrong/AF posts: 50 only by the AI tag, 36 by the tag
and the keyword sweeps, 18 only by the keyword sweeps. The 18 keyword-only posts are mostly off-topic (15 labelled,
3 relevant: "Social Theory and Agent Foundations", "Game Design Creativity evals", a Bayesian-updating post); the
others include "Gluten-free water", "On donating a kidney", "SS27 Color Stats". The keyword sweep's karma ≥ 25 gate
and LessWrong's non-phrase search make it close to "every karma ≥ 25 post of the period" (D196).

Of the 248 keyword-sweep documents, 54 contain their query term as a phrase in title or body; they hold 20 of the 30
labelled-relevant ones, the AI-tag sweep finds 9 of the other 10, and 1 is found by neither.

The tag sweep has no karma gate: 50 of its 86 posts had karma < 25 at collection, none labelled; my read finds 13
clearly relevant (e.g. "Examing Emergent Misalignment in a recurrent LLM with a logit lens", "exfiltration through
self-distillation", "Base Models Can Detect Hidden Intentions that Prompted Classifiers Miss", an incident report on a
false police tip) and 12 borderline. Karma at collection is low because the posts are 0-7 days old.

### 3.4 Feeds

| Feed | Backlog | Dated in last 12 months, per 30 d | Labelled relevant | Proxy `kw_high` |
|---|---|---|---|---|
| openai_blog | 1,185 (since 2015) | 43 | 27 / 35 (18 of the 27 are `kw_low`) | 6 % |
| google_research | 100 | 8.2 | 3 / 17 | 4 % |
| metr_blog | 84 | 3.5 | — | 13 % |
| forethought_blog | 78 | 3.2 | — | 6 % |
| epoch_blog | 94 (68 undated) | 2.1 | — | 0 % |
| openai_alignment_blog | 22 | 1.8 | 8 / 10 | 45 % |
| far_ai_blog | 109 (56 undated) | 1.7 | — | 45 % |
| redwood_blog | 138 (118 undated) | 1.6 | — | 27 % |
| miri_blog | 539 (since 2007) | 1.4 | — | 5 % |
| anthropic_alignment_blog | 54 (undated) | ? | 21 / 21 | 67 % |
| transformer_circuits | 30 | 0.6 | 12 / 12 | 13 % |
| anthropic_cards | 20 | 0.1 | 13 / 13 | 40 % |
| 10 more safety-org feeds | 587 | ≈ 4 (dated ones) | — | — |

Six feeds carry no dates (uk_aisi 163, clr 137, goodfire 61, anthropic_alignment 54, transluce 33, softmax 5), so
their monthly rate is unknown. The safety-org feeds are fetched by source rule (`stack/rules.py`), so their volume
goes straight to acquire.

## 4. Worst offenders

Ranked by estimated irrelevant documents per run or per month, then by rate:

| # | Source | Volume | Relevance | Why |
|---|---|---|---|---|
| 1 | S2 `steerability` | 613 of 836 S2 SR rows (backfill) | read 5 + 2 / 25 | S2 stems the term to "steer" (D196); one page cap only |
| 2 | arXiv author sweep, high-volume names | ≈ 600 / 30 d (46 % of the sweep) | 6 / 27 labelled | namesakes and broad-output researchers |
| 3 | OpenAlex `steerability` | 260 / 30 d | read 4 + 4 / 25 | stemming; already removed from OpenAlex after the A2 run (`openalex_terms: []`) |
| 4 | OpenAlex `umbrella`, non-phrase hits | 119 of 128 (≈ 890 / 30 d) | 30 / 76 labelled (39 %) | token search of "AI safety", "AI alignment" matches "AI" + "safety" anywhere |
| 5 | LW/AF keyword sweeps beyond the tag sweep | 18 posts / week; 306 queries in the dev run, 100 in the SR run | 3 / 15 labelled | fuzzy search + karma gate ≈ all popular posts |
| 6 | arXiv scoped categories | 64 / 30 d | read 0 + 4 / 25 | the AI clause admits any AI paper in the category (q-bio.NC 23, cs.GT 19, cs.LO 19) |
| 7 | S2 `heuristic-arguments` | 19-46 rows | read 0 / 14 | physics and mathematics usage |
| 8 | google_research | 8.2 / 30 d, backlog 100 | 3 / 17 labelled | mostly health, climate and efficiency research |
| 9 | OpenAlex `game-theory-ai` | 22-33 / 30 d | read 1 + 2 / 14 | generic AI clause; 5 of 14 read are a templated "──" preprint series |
| 10 | miri_blog backlog | 539 (481 before 2023) | — | feed history since 2007 |

## 5. Best performers

| Source | Labelled relevant | ≈ per 30 d | ≈ relevant per 30 d |
|---|---|---|---|
| arXiv author sweep (all names) | 25 / 65 | 1,320 | ≈ 500 |
| OpenAlex `umbrella` | 38 / 84 | 960 | ≈ 430 |
| OpenAlex `jailbreak` | 25 / 34 | 470 | ≈ 350 |
| LessWrong AI tag | 27 / 30 (karma ≥ 25) + read | 370 | ≈ 190-240 |
| openai_blog | 27 / 35 (Stack scope counts model and product releases) | 43 | ≈ 33 |
| anthropic_alignment_blog | 21 / 21 | ? | — |
| anthropic_cards | 13 / 13 | low | — |
| transformer_circuits | 12 / 12 | 0.6 | — |
| openai_alignment_blog | 8 / 10 | 1.8 | ≈ 1.5 |
| S2 `superalignment` | 23 / 27 | backfill (31 since 2020) | — |
| S2 `alignment-targets` | read 10 + 3 / 14 | 71 since Dec | — |
| LW/AF keyword hits with a phrase match | 20 / 25 | ≈ 54 / 30 d | ≈ 43 |

## 6. Proposed reductions

### 6.1 Recommended

| # | Change | Volume effect | Recall effect (evidence) | Confidence |
|---|---|---|---|---|
| C1 | Drop `steerability` on S2 (a per-channel term override like `openalex_terms: []`; the S2 fix track P has); keep it on the arXiv keyword family (phrase `ti:`/`abs:`) | −613 of 836 S2 SR rows on the backfill (−73 %) | SR 2025: 1 of 120 papers says "steerab", also reached by another group; read 5 + 2 of 25 stem hits relevant, most about activation steering, which the `steering` group covers (assumed, not measured) | high |
| C2 | Drop `heuristic-arguments` on S2 and OpenAlex, or flag it `needs_ai_context` | −19 to −46 S2 rows; −4 to −7 / 30 d on OpenAlex | 0 of 14 read; 0 of 120 SR 2025 papers match; ARC's own work arrives through arc_blog and arXiv | high |
| C3 | Remove the term "non-agentic AI" from `scientist-ai` | −7 of 14 S2 hits | the 4 clear hits use "Scientist AI" | medium |
| C4 | High-volume author names (≥ 4 hits per 21 days by the sweep's own coverage `seen`): keep a hit only with a second roster author or a safety stem in title/abstract; later, move these names to an OpenAlex author-id lookup (disambiguated) | −32 of 125 sample papers (−26 %), ≈ −340 / 30 d at the full roster | 1 of 25 labelled-relevant papers lost | medium (9 names; threshold chosen on the same sample) |
| C5 | Roster hygiene (section 3.2): rewrite the 47 `Last, First` entries as `First Last` (or make `names_match` accept the comma form), drop the duplicates and fragments, rename `Stuart_Armstrong` | −57 dead queries per sweep; + a few papers / 30 d | recall gain: 20 rostered researchers currently never collected | high |
| C6 | arXiv scoped categories: replace the generic AI clause with the umbrella and SR-group phrases, or drop q-bio.NC | 64 → ≤ 10 / 30 d (estimate) | unmeasured (categories not stored); 0 clear of 25 read | medium |
| C7 | `game-theory-ai`, `social-choice-ai`: AND with agent or alignment terms ("AI agents", "multi-agent", "LLM agents", "alignment", "AI safety") instead of the generic AI context | −60 to −80 % of ≈ 27 / 30 d (estimate) | SR 2025: 1 of 120 papers reached only via `game-theory-ai` | low-medium |
| C8 | LW keyword sweep: keep a search hit only if its query term occurs as a phrase in title or body; only with the AI-tag sweep running | −78 % (248 → 54 per 30 d on the SR terms; 52 → 32 per 7 d on the 42-group terms) | 1 of 30 labelled-relevant lost with the tag sweep; 10 of 30 without it | medium-high |
| C9 | google_research: title gate (safety stems) before fetch, or drop the feed | 8.2 → ≈ 0.3 / 30 d (−96 %) | keeps 2 of 3 labelled relevant | medium (17 labels) |
| C10 | Backfill floor for feed histories, e.g. 2023-01-01 | one-time −481 miri_blog, −223 openai_blog | pre-2023 posts are outside the current Stack and SR windows (assumption) | medium |

Combined steady-state effect of C2-C4 and C6-C9: about −600 to −700 documents a month out of ≈ 4,000 (C4 −340, C8
−190 plus an overlapping −85, C6 −55, C7 −20, C9 −8, C2 −5), with 2-3 labelled-relevant documents lost across the
samples behind them (C4 1, C8 1, C9 1). C1 removes 613 rows per S2 backfill; its monthly rate is unknown.

### 6.2 Considered and not recommended now

- **Karma floor on the LessWrong AI-tag sweep.** 50 of 86 posts were under 25 karma at collection, including about 13
  clearly relevant ones; karma grows after collection. If a floor is wanted, apply it in a second pass after a week
  (the two-pass build), not at collection.
- **Phrase-quoting OpenAlex `umbrella`.** Only 9 of 128 hits contain an umbrella phrase (8 of 8 labelled relevant);
  the other 119 hold 30 of the 38 labelled-relevant ones. Decide after a full std-v2 window shows how many of those
  the specific groups (jailbreak, interpretability, evals, …) also find.
- **C4's rule (second roster author or a safety stem) for every name.** −49 % volume, but 4 of 25 labelled-relevant
  lost, and 8 of the 84 roster-reachable SR 2025 papers have one roster author and no safety stem.
- **Title-keyword gating of openai_blog.** The proxy carries no signal there (18 of 27 labelled-relevant posts are
  `kw_low`); its relevance under Stack's scope is model and product releases. Its cost is the 43 posts a month.
- **A `kw_high` filter on OpenAlex `umbrella`.** −41 % volume but 8 of 30 question-relevant lost; the triage question
  does this job at $0.15 per 1,000.

## 7. Attribution gaps and instrumentation

1. **Record the query on `sources` rows.** `metadata.query_digest` exists in the schema but every search collector
   leaves it NULL (only the lookups set it); a source hit by several queries in one run keeps only the first (counted
   in `sources_repeat`). Setting the first query's digest on observe, and counting or listing the other matching
   digests, makes per-query yield exact and order-independent. *Done since:* `metadata.matched_by` (`f279822`,
   `673c3ee`); no rows carry it yet (section 9.1).
2. **Store arXiv categories** on the arXiv source record (metadata or record blob), so the category family can be
   judged per category. *Done since:* in the record blob (`673c3ee`).
3. **Measure instead of estimating.** Run the triage question over the A2 windows' 3,092 new documents (≈ $0.50 at
   $0.15 per 1,000) and screen-v11 over a stratified 200 (≈ $0.20); this replaces the proxy and the title reads with
   per-query relevance rates, Stack and SR separately.
4. **Rerun the S2 SR window with a key or slower pacing**: 11 of 20 groups failed with 429 and two were cut at one
   page, so S2's real volume per group is unknown.

## 8. Caveats

- **Small samples.** Most per-query rows have fewer than 10 labelled documents; per-author counts are 0-12 papers;
  the title reads are 14-25 titles, titles only, by one LLM reader. Treat percentages as ±15-25 points.
- **Labels are not human.** The gold sets are Opus and Codex labels with an Opus adjudicator; the samples are
  stratified with `kw_high` oversampled; url triage labels are title-only probes; screen-v8 asks a single Stack-scoped
  question. No SR-specific relevance exists for documents outside the 88 gold-screen items.
- **The proxy transfers imperfectly.** It was calibrated on the dev-store mix (umbrella, jailbreak, LessWrong, lab
  blogs); for SR-scope queries without safety vocabulary it under-rates, and inside a flooded query the `kw_low`
  documents are likely worse than the calibration's `kw_low` average.
- **Windows differ** (4 days to 30 days, backfills); scaling to 30 days assumes uniform rates, and OpenAlex
  publication dates cluster. Unique-contribution counts are inflated because the families ran over different windows.
- **Dev options, not production.** OpenAlex ran 2 of 42 groups and no institution sweep; arXiv swept 10 % of the
  roster; S2 ran key-less. std-v2's real mix was not measured.
- **Re-matching approximates provider semantics** (stemming, fuzzy search, OpenAlex full-text matching), so
  "unattributed" rows are partly real hits of a known query.
- **Snapshot.** Store files as of 2026-10-12 21:05 UTC; document ids may change with the round-4 URL re-key (url and
  forum documents mainly); the analysis scripts are not committed (method as above).

## 9. Concrete cuts and redundancy

Added 2026-10-13 after Tomáš asked what exactly would change and whether the relevant documents a cut drops are
also reached some other way. Same rules as above: read only over copies of the stores (second snapshot 23:56 UTC),
no network, no LLM calls. Track P has since adopted three of the cuts in `673c3ee`: C1, the S2 half of C2, and C5.

### 9.1 How redundancy was checked

- **Dropped set.** For each cut, the documents whose every discovery route in these windows is removed by the cut.
  Documents that another family or a surviving query also found are not counted as dropped.
- **Relevant or plausibly relevant.** Labelled relevant (section 1), or unlabelled and `kw_high`. For the smaller
  sets and for every dropped document without another route, I also read the titles (marked "read").
- **Routes observed in the stores.** (a) Other members of the same document: another channel, or another query of the
  same collector that the cut leaves in place. Gather merges equal keys across channels, so this covers a key that
  another collector found. Lookups (`openalex-lookup`, `s2-lookup`) only enrich documents that something else
  discovered, so they are never an independent route. (b) The links scratch store (track L, `v4/store-links-scratch`):
  the newsletter posts of 09-15..10-12 (Zvi 20, Import AI 4, paradigm3 8) and the 832 link targets the selector
  kept. A route counts when the arXiv id, DOI, LessWrong post id or normalised URL is the same, or when a newsletter
  post has the same title (Zvi's LessWrong crossposts).
- **Routes simulated under std-v2** (would match a query std-v2 runs; not run over these windows, so not observed):
  - another concept group on OpenAlex (stemmed token-AND for unquoted terms, stemmed phrase for quoted ones) or on
    S2 (stemmed phrase);
  - a roster author (roster 1.1.0) on an arXiv-keyed paper, assuming the paper is in the sweep's categories;
  - the arXiv keyword family's `umbrella` or `steerability` phrases.

  Most simulated routes run through broad OpenAlex queries (`umbrella`, `steering`, `specs`, `safety-tuning`). If
  those are narrowed later, the losses below grow.
- **`metadata.matched_by`** (`f279822`, `673c3ee`). The compacted source files of 23:18-23:20 carry the column, but
  none of their 7,244 rows has a value: no collector has run since `673c3ee`. So it cannot help with these windows.
  It will make the next windows' per-query counts exact.

### 9.2 Summary

| Cut | Status | Exact change (file) | Drops | Relevant or plausible dropped | Another route | Net loss after redundancy |
|---|---|---|---|---|---|---|
| C1 S2 steerability | adopted `673c3ee` | `steerability: s2_terms: []` (`configs/channels.yaml`) | 577 over the S2 backfill since 2025-12-01 (≈ 55 / month; more without the one-page cap) | 324 (`kw_high`) | 271 simulated, 0 observed | 53 without a route; read: 3 clear + 8 borderline over the backfill (≈ 0.3 clear / month) |
| C2 heuristic arguments | S2 adopted `673c3ee`; OpenAlex proposed | `heuristic-arguments: s2_terms: []` (adopted) and `openalex_terms: []` (`configs/channels.yaml`) | 22 (S2 19 over the backfill, OpenAlex 4 / 30 d; 1 in both) | 1 (`kw_high`; not relevant by read) | — | 0 |
| C3 Scientist AI | proposed | `scientist-ai: terms: ["Scientist AI"]`, `openalex_terms: ['"Scientist AI"']` (`configs/channels.yaml`) | 7 over the S2 backfill | 0; read: 1 borderline | none | 0 clear, 1 borderline |
| C4 high-volume names | proposed | roster list `disambiguate:` (`configs/rosters/researchers.yaml`) + a filter in the author family (`workflows/collect/arxiv_sweep/main.py`); safety stems moved to `stack/rules.py` | 32 of the 125 sample papers (≈ 340 / month at the full roster) | 1 labelled + 2 read | 14 simulated, all irrelevant | 1 labelled + 2 borderline in the sample (≈ 10-30 / month at the full roster, of ≈ 500 relevant the sweep brings) |
| C5 roster hygiene | adopted `673c3ee` | `Last, First` parsing, dead entries dropped, `Stuart Armstrong` (`configs/rosters/researchers.yaml`, `roster_name`) | 0 | — | — | 0 (adds recall) |
| C6 arXiv categories | proposed | keep a category hit only with a safety stem or an umbrella/SR phrase in title or abstract (client-side filter, `workflows/collect/arxiv_sweep/main.py`) | 39 of 64 / 30 d | 0; read: 7 borderline | 4 of the 7 simulated | 0 clear, 3 borderline / month |
| C7 game theory, social choice | proposed | new group field `context_terms` replacing `ai_context` (`configs/channels.yaml`, `stack/config.py`, channels schema, the OpenAlex, S2 and arXiv collectors) | 19 of 32 / 30 d (OpenAlex) | 2 (`kw_high`, not relevant by read); read: 2 borderline | both borderline simulated | 0 |
| C8 LW keyword phrase check | proposed, with the tag sweep | option `phrase_check: true` (`workflows/collect/forums/main.py`; `collect.forums` lines of the plans) | 181 / 30 d | 80 (1 labelled, 79 `kw_high`) | 8 observed (newsletters); 13 more posts not counted as dropped because the tag sweeps found them (11 AI tag, 2 SR tags) | 1 observed + ≈ 7 estimated / month (71 unverifiable offline) |
| C9 google_research | proposed | title filter on the feed surface (new `Surface` field `title_include_re`, `workflows/collect/blogs/main.py`) | 96 of 100 (≈ 8 / month) | 1 labelled + 2 read | none | 1-3 a year |
| C10 feed floor | proposed (scope decision) | new `collect.blogs` option `from: 2023-01-01` (plan stage line) | one-time 704 (miri 481, openai 223) | 36 (2 labelled, 34 `kw_high`) | none | 36 if pre-2023 history is in scope, else 0 |

### 9.3 Per cut: the relevant documents dropped and their other routes

Document ids are the A2 scratch store's before the round-4 re-key (`r24enz7dn1yke`). Paper and forum ids keep their
anchor source id; url documents may have been re-keyed.

**C1, S2 `steerability` (adopted).** 577 documents had no producer other than the S2 steerability query. 324 are
`kw_high`, and none is labelled because the S2 window was never triaged. None has an observed route: no other
channel's member, and no newsletter link. 271 have a simulated route; a document can have several:

| Simulated route | Docs |
|---|---|
| OpenAlex `umbrella` | 89 |
| OpenAlex `steering` | 69 |
| S2 `steering` | 63 |
| OpenAlex `specs` | 53 |
| arXiv author sweep | 49 |
| S2 `jailbreak` | 23 |
| S2 `safety-tuning` | 17 |

Examples of clearly relevant ones with a route: `s2372t55wnqdt` "Detection Is Cheap, Routing Is Learned: Why
Refusal-Based Alignment Evaluation Fails" (OpenAlex `specs`), `sht6r3mq63r5s` "Beyond Shallow Alignment: … Refusal
Circuits …" (OpenAlex `safety-tuning`), `shmyb39v5jts6` "Activation Decomposition and Steering for LLM Backdoor
Remediation" (OpenAlex `steering`), `se23na8n88cwp` "Lying Is Just a Phase …" (OpenAlex `steering`).

53 have no route. By my read, 3 are clearly relevant:

- `s7623y6vz1q0y` "A Finger on the Scale: Covert Policy Steering through Agentic Skills"
- `sjwkmpa3sve18` "Investigating and Alleviating Harm Amplification in LLM Interactions"
- `skcqyggm2s7aa` "Learning Safety Constraints for Large Language Models"

8 are borderline: `scd6jbf1b0yg0`, `se9npvx8jwqkf`, `sevvk5wzjqp4j`, `sezmp068x437h`, `snbee5305sa88`,
`swmt1phv8h7vm`, `sxjrvf1538bsc`, `sz6sk1n1yr46y`. The remaining 42 are off-topic (vehicle steering, antenna
design, wind-farm wake steering, protein design, …).

**C2, `heuristic-arguments`.** S2 dropped 19 documents and OpenAlex 4, all physics and mathematics. The one `kw_high`
document, `sxdmc3j4z8zb6` "Lightweight online offline authenticated encryption for secure UAV systems", is not
relevant. Net 0.

**C3, without "non-agentic AI".** 7 documents are dropped, none `kw_high`. One is borderline by read and has no other
route: `s40h5j2rc17dt` "Agentic AI vs Non-Agentic AI: Motivation, Security Implications, and Research Foundations".

**C4, high-volume names.** The rule drops a paper whose only roster authors are the nine names of section 3.2, which
has no second roster author (roster 1.1.0) and no safety stem. That is 32 of the 125 sample papers; 12 are labelled,
1 of them relevant: `s6tbfxkatvpax` "Same evidence, different judgments: Evidence noncommutative in vision/speech-text
conflicts" (via Yi Dong, screen-v8 relevant), with no other route. By read, 2 more are borderline and also have no
route: `sxxsfjv8rjafs` "Codoku: … Challenges for Frontier Coding Agents" and `s85zxbxw7zb28` "Case-Level
Verification in Scanner-LLM Cascades …". 14 of the 32 have a simulated route (OpenAlex, one also S2), but all 14
are irrelevant (labelled or read).

The scaled loss, 10-30 a month, is the largest of the proposed cuts. The OpenAlex author-id variant (a disambiguated
author id instead of the name for these nine) should keep the true author's papers and drop the namesakes; it is
untested here.

**C6, arXiv categories, client-side filter.** The filter keeps a hit with a safety stem or an umbrella/SR phrase,
25 of 64. Of the 39 dropped, none is labelled or `kw_high`. By read, 7 are borderline:

- 4 with a simulated route: `s60at12g8dqj3` "… Type-6 Logic for CoT Verification" (OpenAlex `specs`),
  `skymz4kp13zm2` "LogicTrack: Auditing Reasoning Trajectories …" (OpenAlex `rl-safety`, author Di Wang),
  `saqn6njkdst5s` "Prompt framing governs LLM default following in collective-action" (OpenAlex `social-choice-ai`,
  author Tom Lenaerts), `s8jjpy85gjf41` "RSIGame: … Recursive Self-improvement" (OpenAlex `power-seeking`);
- 3 without one: `s2nkk9qb0hphs` "Bayesian Intelligence from the Outside", `sxr7tyew6ezda` "Jev thinks 'I don't
  know', but doesn't say it: … Sys1Cal-v1", `syd6gm6a05awe` "Information Design Against Gaming and Learning
  Adversaries".

The narrower clause first proposed in 6.1 (umbrella and SR phrases only) would drop 60 and lose `sj70hrp0dsy8x`
"When Does Randomized Oversight Align AI Agents That Can Conceal?", which has no route; the stem clause keeps it.

**C7, game theory and social choice with agent or alignment context.** The context terms keep 12 of the 32 documents; 19
are dropped, and 1 more has another producer. The two `kw_high` among the dropped are not relevant by read:
`s8npw3bvr97pj`, a scoping review of market manipulation, and `sadk2ymy1sgaf`, game-theoretic cybersecurity. Two are
borderline by read and both have a simulated OpenAlex `decision-theory` route: `s80xdxtnc0rb3` "Collective dynamics of
strategic classification" and `sb5ctwkwrkec0` "Intuitions of compromise: Utilitarianism, egalitarianism,
contractualism".

**C8, LessWrong keyword phrase check.** The tag sweep is the redundant route, but it covered only 10-05..10-12,
against 09-12..10-12 for the keyword sweeps:

- **In the shared 7 days.** 10 relevant or plausible posts lack a phrase match. 9 were also found by the AI tag
  (so they are not in the dropped set). 1 was not: `sxz7j502drrqg` "You Heard of 'Bayesians Don't Predictably
  Update' Because of Robin Hanson" (labelled relevant).
- **09-12..10-04, which the tag sweep did not cover.** 79 dropped posts are `kw_high`, most of them plainly about AI
  safety ("Obstacles to the scalable oversight of auto-alignment research", "HoneyBench - A general benchmark for
  reward hacking in frontier models", …).
  - 8 have a newsletter route. Zvi's crossposts appear as `zvi_substack` feed posts: `s0sgwrtphndzq`,
    `s7k7rphdp6fyg`, `sbnb7wvnjmqga`, `shv023vskmphk`, `svajpppy644x6`, `sz1q3x3yfyt20`. Two are paradigm3 link
    targets: `s8zgdwygv8rbk`, `sp53fz05bca31`.
  - 71 cannot be checked offline for the AI tag. At the in-window rate (9 of 10 tagged, a small sample) about 7 would
    be lost.

One 30-day run of the tag sweep (a single GraphQL listing) settles this before C8 is adopted. Without the tag sweep,
C8 would lose most of the 79.

**C9, google_research title filter.** 96 of 100 posts are dropped. One is labelled relevant: `s5qbxsy2r9erg` "Empty
shelves or lost keys? Recall is the bottleneck for parametric factuality". By read, 2 more are plausible:
`sw8dqc06r9gzx` "Open and Emergent Problems in Agentic Privacy and Security: A Contextual Angle" and `sr44hs8k6f20b`
"A differentially private framework for gaining insights into AI chatbot use". No newsletter in the links store
links any of the 96.

**C10, feed floor 2023.** 704 posts are dropped once. By year: miri_blog 481 (2007-2022, 2014 the largest year with
123; 121 titles are newsletters or updates, 32 fundraisers) and openai_blog 223 (2015-2022). 36 look relevant:
2 labelled, 34 `kw_high`. Examples: `s9d82vxykqfpk` "AI safety via debate" (2018), `s0vrgbc9zkhgp` "Our approach to
alignment research" (2022), `sk4fkndt67egr` "Safety Gym" (2019), `sk1ee3wsqavz7` "Ngo And Yudkowsky On Alignment
Difficulty" (2021). None has another route in the stores, because the forum windows are recent. Whether the corpus
keeps pre-2023 history is a scope decision, not a question of redundancy.

### 9.4 C8 measured: one 30-day run of the tag sweeps

Added 2026-10-13 (round 5, T-RUN B7b) after Tomáš asked for "a 30-day LW tag sweep to measure redundancy" before
deciding C8.

**Run.** `collect.lw_graphql` (std-v2's tag list: `ai` and the 12 SR extension tags, `from: 2026-09-12`, `max_records:
2000`) into `v4/store-run-c8`, a reflink copy of the A2 scratch store that holds the keyword sweeps. Run
`rqv7crxsysczn`, 19 GraphQL pages, 0 errors: the AI tag's coverage gap 09-12..10-05 was listed as one bounded span (349
posts), then each tag's tail from 09-28 (the 14-day re-scan) to 10-13. The tag sweeps now cover 449 posts dated
09-12..10-13 (97 before). No other LessWrong call.

**Method.** As §9.1 and §9.3, over the same 248 keyword-sweep posts dated 09-12..10-12 (`rkdfv6k3xnggf`, 30 days, the
20 SR groups; the 7-day 42-group sweep `r3qmbwebvmj6c` adds no post outside them). Phrase match: one of the posts'
swept terms (the SR terms; for a post the 42-group sweep also found, every group term and the two extra keywords) as a
whole phrase, case-insensitive, in the title or the in-band search body. A post is dropped by C8 when it has no phrase
match and no tag sweep found it (by LessWrong post id). Relevant or plausible: labelled relevant (§1's labels), or
unlabelled and `kw_high`. `kw_high` is computed over the title and the body's first 3,000 characters, the triage
excerpt cap (`MAX_EXCERPT_CHARS`): with that cap the "before" column below reproduces §9.2-§9.3 exactly (181 dropped;
1 labelled + 79 `kw_high`; 8 newsletter routes; 71 unverifiable) and §1's forum calibration (14 of 14 labelled
`kw_high` relevant); over the whole body the proxy marks more posts (last column). Newsletter routes are §9.3's eight
documents. Script: `<scratch>/trun/b7b/scripts/c8_measure.py` (read only).

| 248 keyword-sweep posts, 09-12..10-12 | Before (AI tag 10-05..10-12, SR tags 30 d) | After (all tags 30 d) | After, `kw_high` over the whole body |
|---|---|---|---|
| With a phrase match | 55 | 55 | 55 |
| Without (C8 removes them from the keyword sweep) | 193 | 193 | 193 |
| of these, a tag sweep found | 12 (AI 10, SR 2) | 148 | 148 |
| **dropped from the corpus by C8** | 181 | **45** | 45 |
| dropped, relevant or plausible | 80 (1 labelled, 79 `kw_high`) | **9** (1 labelled, 8 `kw_high`) | 20 (1 labelled, 19 `kw_high`) |
| of these, a newsletter route | 8 | 0 (all 8 are now tag-found) | 0 |
| **without any route** | 72 (71 unverifiable) | **9** | 20 |

- **Shared window (10-05..10-12), the check against §9.3's 9 of 10.** 21 posts lack a phrase match, 9 of them relevant
  or plausible; the AI tag found 8 of the 9, as in A2's run (no post there was tagged since). The one left is the
  labelled `sxz7j502drrqg` ("You Heard of 'Bayesians Don't Predictably Update' Because of Robin Hanson"), as in §9.3.
  §9.3 counted 10 such posts and 9 tagged; the difference is one post's proxy, not a tag.
- **09-12..10-04, the 71 that could not be checked.** The tag sweeps now find 63 of them (89 %, against the 9 of 10
  assumed) and all 8 with a newsletter route; 8 have no route.
- **The 9 without a route, by title read** (one reader, titles only): clearly relevant 4 + the labelled one
  (`s9c1dmkw67yax` "Obstacles to the scalable oversight of auto-alignment research", `sb356m08afzr8` "Measuring
  alignment drift via trajectory prefixes", `sd8prc0vbs33x` "The Normalization of Deviance in AI Development",
  `sr7pq1ydeyw54` "Is METR A Meaningful Check On Anthropic?", and `sxz7j502drrqg`); borderline 2 (`sy3z8g3eeqstg`
  "Alignment & Succession: Morality Lives in the Human Individual", `sbwt1jy09f412` "We are too early for Astra");
  not relevant 2 (`sznny02a9813n` pandemic early warning, `sqwtw0xrtxnwv` "Addictions are anesthesia"). The
  whole-body proxy adds 11 more, among them `skztshh7y4atj` "Dialogue with Eliezer Yudkowsky on FOOM",
  `s3nmrnz6hek1a` "Yet another concerning result on Astra's no-CoT capabilities" and `snecpegrar2mg` "We've saved the
  world before: what the ozone hole teaches us about AI" (clear by read), plus borderline governance and field-building
  posts. None of these carries the AI tag 1-4 weeks after posting.
- **What C8 saves.** With the tag sweeps running, the keyword sweep adds 55 posts in 30 days that no tag sweep finds
  (14 relevant or plausible); C8 removes 45 of them. §6.1's −78 % (248 → 54) counts the keyword sweep's rows; in
  documents the corpus shrinks by 45 a month (≈ 1 % of the ≈ 4,000), because gather merges the other 148 with the
  tag sweeps' rows. Triage and screen of 45 posts cost about $0.04 a month (D260, RUNLOG M4.T4.2 rates).

**Recommendation: do not adopt C8.** Measured, it removes 45 documents a month and with them 5-8 clearly relevant
posts (1 labelled, 4 by read with the capped proxy, 3 more with the whole-body one) and 2 or more borderline ones:
AI-safety posts that never got the AI tag, the case the keyword sweep is there for. The saving is about $0.04 a month
of triage and screen. §9.3's estimate (1 observed + ≈ 7) was right; what changed is the volume side, which is 45, not
190, once the tag sweeps run. If the keyword sweep's API load matters later, a cheaper cut is to keep it and drop
its tag queries (`collect.forums` `tags`), which the GraphQL tag sweeps cover; not measured here. No C8 code was
written (`phrase_check` does not exist).

### 9.5 Authors by OpenAlex id against the arXiv name sweep (T-ARX)

Added 2026-10-13 (track T-ARX, builder M) after Tomáš asked whether the arXiv author (name) sweep is superseded by
OpenAlex author search ("we want concrete people, never just accidental name matches"); std-v3 already turns the name
family off and `collect.openalex`'s author-id family on (`e318fec`).

**Method.** The sweep side is the one stored author-sweep run, `rm20bsqsa24ck` (§3.2's sample): 226 `ok` coverage rows
over submitted dates 2026-09-21..2026-10-12 and 125 papers. The 6 other rows (`Zeng_Jiali`, `Tang_Jie`, `Skinner_Joe`,
`Treutlein_Johannes`, `Co-Reyes_John`, `Mellor_John`) are `failed_transient[rate_limit]` with zero-length windows:
never queried. This run predates D228: its rows carry no `matched_by` and no `record_ref`, so the arXiv categories are
not stored and the matched roster names are re-derived with the collector's `names_match` over the run-time roster
(`03cba6b`) and that code's `au:` token. The 226 queries are 227 roster names, 225 of them in roster 1.2.0 (`Zhang`
and the Meta junk entry were dropped by D229). Relevance labels are §1's (gold screen v2, screen-v8, gold triage v2,
triage question), joined through the dev store's documents head; they reproduce §2's 65 labelled, 25 relevant. The
OpenAlex side queries every id of the roster with `author_filter` (scholarly types, `from_publication_date:
2026-09-21`, `to_publication_date: 2026-10-12`, no created axis) and `fetch_group`, 100 ids per filter, through
`OpenAlexMeter`, on 2026-10-13 (one day after the sweep); the sample's works are those with an authorship id of one of
the sample's names. Works map to arXiv ids through `work_keys` (the `10.48550/arxiv.<id>` DOI) and the
`openalex_arxiv_location` key adder. Every sweep-only arXiv id was then looked up by DataCite DOI (`doi:` OR filter);
the 4 not found that way were found by `title.search` (an arXiv location on another record). Mechanical classes come
from the found record: the matched name has no roster id (*unresolved*), the paper's author of that name carries
another id (*id mismatch*), or carries ours outside the filter (*filter mismatch*); then reading decides whether the
paper is our researcher's (*split profile* or *ours, unresolved*) or a namesake's (*wrong-person name match*). Scripts
and cached answers: `<scratch>/arx/M/` (`run.sh <roster>` re-runs offline; with `live` it queries only ids and arXiv
ids the cache lacks). The 12-month window below was queried the same way; a `raw_author_name.search` probe covered
three names. Spend: 88 OpenAlex requests, $0.0151, no arXiv request.

**Comparison** (sample: the 225 names; "core" = submitted, or for OpenAlex-only works published, on or before
2026-10-08, past OpenAlex's ingestion lag):

| 225 sampled names, 09-21..10-12 (core in parentheses) | 1.2.0 | 1.3.0 | 1.3.1 |
|---|---|---|---|
| names with OpenAlex ids (ids) | 147 (147) | 180 (205) | 180 (205) |
| arXiv sweep papers | 125 (120) | 125 (120) | 125 (120) |
| OpenAlex works of the sampled names' ids (of them arXiv) | 43 (36) | 55 (48), core 53 (46) | 55 (48), core 53 (46) |
| both, by the sampled names' ids | 29 (29) | 38 (36) | 38 (36) |
| rescued: another roster author's id is on the paper | 10 | 4 | 4 |
| **both, as production collects (every roster id)** | **39** | **42** | **42** |
| **sweep-only after the rescue** | **86 (82)** | **83 (80)** | **83 (80)** |
| OpenAlex-only, arXiv / not arXiv | 7 / 7 | 10 / 7 | 10 / 7 |
| labelled-relevant sweep papers (25) collected | 10 | 10 | 10 |

Production queries every roster id, so a sweep paper with any roster author's id on it is collected through that
coauthor: 10 sweep-only papers with 1.2.0 (among them Krueger's evaluation-awareness paper via Sahar Abdelnabi and
Evtimov's via Debenedetti and Mehrabi), 4 with 1.3.0 (Hendrycks's CheatBench via Mantas Mazeika and Richard Ren, Kate
Larson's via three DeepMind authors). The 9 papers 1.3.0 adds to the sampled names' "both" are all plausibly the
roster person's (Pang Wei Koh 5, David Krueger, Ivan Evtimov, Lucius Bushnaq, Jan Kautz), but 6 of them were already
rescued, so 1.3.0's net gain is 3 papers. 1.3.1 (merged-profile guard, 11 ids removed) changes nothing in the sample.
The core window changes little: ingestion lag is not why the families differ. It does matter at the window's end:
full-roster works per publication date run at about 20 a day through 10-08 and at 9, 2, 5, 1 for 10-09..10-12 (queried
10-13), so a window's last ~4 days are incomplete until re-scanned.

**Sweep-only: why.** Every one of the 96 is in OpenAlex on 10-13: 92 under the arXiv DataCite DOI, 4 only as another
record with an arXiv location (two preprints without the DOI, an IEEE Access article, an OSF preprint). So no *lag*
case. Mechanical classes from the record (by the sampled names' ids, before the rescue):

| Sweep-only class (mechanical) | 1.2.0 | of them labelled / relevant | 1.3.0 | of them labelled / relevant |
|---|---|---|---|---|
| unresolved name (no roster id) | 66 | 35 / 11 | 47 | 21 / 8 |
| id mismatch (the paper's author of that name has another id) | 27 | 14 / 5 | 37 | 21 / 6 |
| filter mismatch (our id, date or type outside) | 0 | | 0 | |
| matched only a roster entry D229 dropped (`Zhang`) | 3 | 3 / 1 | 3 | 3 / 1 |
| lag | 0 | | 0 | |

25 of the 27 (1.2.0) id mismatches sit on profiles minted in the last weeks (ids from A515…) with 1-3 works. Reading
decides which are our researcher's work on a split profile and which are namesakes. **33 reads** (title, abstract,
authors and affiliations, both profiles; the random 20 (seed 13) plus all 17 relevant-labelled, 4 in both):

| Read class | random 20 | relevant-labelled 17 | all 33 |
|---|---|---|---|
| wrong-person name match (namesake; incl. 2 `Zhang`, 1 probable) | 14 | 3 | 14 |
| ours, split profile (id mismatch) | 2 | 3 | 5 |
| ours, name unresolved | 3 | 6 | 8 |
| unclear | 1 | 5 | 6 |

| arXiv | Name | Read | Reason |
|---|---|---|---|
| 2609.26368 | Yu Cheng | namesake (probable) | Xiaomi MiMo team; 4-work USTC/CUHK profile, not the roster's Microsoft one |
| 2609.26986 | Yi Dong | namesake | Liverpool Yi Dong; R's evidence ties the roster name to NVIDIA coauthors |
| 2609.28614 | Michel Galley | ours, split | Microsoft coauthors; new 1-work profile A5152859620 |
| 2609.29264 | Zhang | namesake | junk mononym entry matched "Zhang Zhang" (Tohoku) |
| 2609.29421 | Qingyu Yin | ours, split | Amazon Rufus paper; new 1-work profile |
| 2609.31121 | Julian Schulz | ours, unresolved | Meridian Cambridge, CoT-monitor evasion; name ambiguous (5 profiles) |
| 2609.31612 | Zhifeng Chen | namesake | UW-Madison statistics student |
| 2609.32764 | Gang Li | namesake | Chinese industry IoT/DDoS |
| 2609.32776 | Jen-tse Huang | ours, split | JHU (Dredze) coauthors; new 1-work profile |
| 2609.34277 | Zhang | namesake | junk entry; Sichuan pathology |
| 2609.34563 | Zheng Zhang | unclear | Amazon coauthors; roster name not pinned (40 active profiles) |
| 2609.35336 | Yu Cheng | namesake | ECNU chemistry agents |
| 2609.35822 | Logan Smith | ours, unresolved | Logan Riggs Smith, interpretability; name ambiguous |
| 2609.36178 | Xiaomin Li | ours, split | Microsoft coauthors as the roster profile; new 1-work profile |
| 2609.36316 | David Krueger | ours, unresolved (1.3.0: both) | Krueger with Anwar, Abdelnabi |
| 2609.36580 | Yu Cheng | namesake | the ECNU group of 2609.35336 |
| 2609.37132 | Zheng Zhang | namesake | ShanghaiTech student |
| 2609.39838 | Julian Schulz | ours, unresolved | steganographic reasoning, as 2609.31121 |
| 2610.02202 | Pang Wei Koh | ours, unresolved (1.3.0: both) | UW, 120-work profile |
| 2610.02741 | Han Wang | unclear | UIUC (Huan Zhang's group); roster name not pinned |
| 2610.04245 | Han Wang | unclear | the same UIUC Han Wang |
| 2610.04824 | Gang Li | unclear | Orby-style author list; cannot be tied |
| 2610.05197 | Xuezhi Wang | namesake | CAS data infrastructure; 1.3.0 resolves the name to another id |
| 2610.06240 | James A. Evans | ours, unresolved (1.3.0: id mismatch) | Chicago/SFI; OpenAlex keeps it under the OSF v1 record dated 09-18 on an id 1.3.0 lacks |
| 2610.07290 | Xingyu Chen | namesake | Texas A&M optimisation; roster's is Tencent |
| 2610.09090 | Zheng Zhang | namesake | UCSB tensor methods |
| 2610.10355 | Yu Cheng | namesake | Kunlun Tech text-to-music |
| 2610.10858 | Kevin Wang | namesake | analog/RF chip design |
| 2610.11575 | Yu Cheng | ours, split (by coauthors) | Shanghai AI Lab group the CUHK/ex-Microsoft Yu Cheng publishes with |
| 2610.11959 | Yu Cheng | unclear | Xiaomi MiMo report; same new profile as 2610.11575 |
| 2610.13806 | Han Wang | unclear | Meta recommender systems |
| 2610.13886 | Ivan Evtimov | ours, unresolved (1.3.0: both) | Meta red-teaming |
| 2610.13917 | Pang Wei Koh | ours, unresolved (1.3.0: both) | protein design; ours, out of scope |

What a relevant loss looks like: Michel Galley's "Reward Hacking Challenges Oversight of Autonomous Research Agents"
(OpenAlex put the preprint on a fresh one-work Michel Galley profile, so the roster id misses it), Julian Schulz's
"Monitor Jailbreaking" (name left ambiguous). With 1.3.0, 7 labelled-relevant sweep-only papers are ours by read (3
splits, Schulz twice, Logan Smith, Evans), 3 are namesakes', 5 unclear. Splits are not only fresh: Dan Hendrycks's
three works since July sit on three ids, none the roster's (`raw_author_name` probe), while Galley's August works are
on his roster id. By the random 20, about 70 % of the sweep-only papers are namesakes' (14 of 20; 95 % interval 48-85
%) and about 25 % ours (5 of 20; 11-47 %); with 1.3.0 or 1.3.1 and the rescue, 3 of the 18 still lost (17 %; 6-39 %).

**Other routes** (the marginal loss to the corpus). For the 17 papers still lost with 1.3.0/1.3.1 that are ours by
read or labelled relevant: *observed* = a row from an independent collector in `store-dev`, `store-sr-scratch` or
`store-links-scratch` (lookups and compactions, which follow the sweep's own rows, excluded); *std-v3, inferred* = a
std-v3 OpenAlex concept group whose phrases (and AI context) occur in the title or abstract, or an author institution
among channels.yaml's OpenAlex institutions (whole-phrase match; OpenAlex stems, so a lower bound). The stores'
windows are partial: OpenAlex `umbrella`/`jailbreak` 09-25..09-28, OpenAlex SR groups 09-05..10-05, S2 SR since
2025-12-01 (11 of 20 groups failed), LW/AF keywords 09-12..10-12; std-v3 runs the groups over every date.

| arXiv | Read, label | Route |
|---|---|---|
| 2609.31121 Monitor Jailbreaking | ours (Schulz), relevant | observed: OpenAlex `r5qzaw56xjqhw`; std-v3 `cot-monitor`, `stego` |
| 2609.39838 Steganographic reasoning | ours (Schulz), relevant | observed: LessWrong keyword sweep `rkdfv6k3xnggf`; std-v3 `cot-monitor`, `stego` |
| 2609.34563 Latent visual reasoning | unclear, relevant | observed: OpenAlex `umbrella` `rt9gy9wq4befh` |
| 2610.04245 Steering dimensionality of refusal | unclear, relevant | observed: OpenAlex SR `r1m37283adw16`, S2 SR `rys3ds2d0b9qb`; std-v3 `safety-tuning`, `steering` |
| 2609.28614 Reward hacking, research agents | ours (Galley, split), relevant | std-v3 `reward-hacking` |
| 2609.29421 Rufus-Air | ours (Qingyu Yin, split), relevant | std-v3 `rlhf` |
| 2609.35822 Sycophantic agreement | ours (Logan Smith), relevant | std-v3 `persona` |
| 2610.11959 MiMo-V2.6 | unclear, relevant | std-v3 `reward-hacking` |
| 2609.36178 ProVer | ours (Xiaomin Li, split), not relevant | std-v3 institution (Microsoft) |
| 2609.32776 Behavioral traits of LLM agents | ours (Jen-tse Huang, split), relevant | none |
| 2610.06240 AI and the new science of culture | ours (Evans), relevant | none |
| 2610.11575 Top-k MoE routing | ours (Yu Cheng, split), not relevant | none |
| 2610.02741 CoT monitorability of looped LMs | unclear, relevant | none by phrase (OpenAlex's stemming may reach it via `cot-monitor`) |
| 2610.04824 Agent Behavior as Code | unclear, relevant | none |
| 2609.26986 Evidence noncommutativity | namesake, relevant | none |
| 2609.29264 TP-CRIV | namesake (`Zhang`), relevant | none |
| 2609.36580 SafeCoEvo | namesake, relevant | none |

So 4 reach the corpus by an observed route and 5 more by a std-v3 query (inferred); 8 by none: 3 ours by read (2
labelled relevant), 3 relevant namesakes' papers the sweep found by accident, 2 unclear. Of the random 18 still lost,
1 ours paper has no route (the MoE paper; Xiaomin Li's and Logan Smith's have one).

Two sweep defects surfaced on the way (the family is off in std-v3, so recorded only): `names_match` does not fold `ı`
(dotless i) and so rejects "Emre Kiciman" for "Emre Kıcıman" (`_norm_name`'s NFKD leaves `ı`, `ł`, `ø` as they are);
and the run-time `au:` token of a `Last, First` entry was `First_Last` (`Carlini, Nicholas` → `Nicholas_Carlini`),
fixed since by D229's `roster_name`.

**OpenAlex-only: what.** All 17 (1.3.0) read; arXiv categories are not verified (the run stored none, and no arXiv
request was made), the category column is inferred from the topic:

| Class | n | Works (author; read) |
|---|---|---|
| arXiv, older preprint whose journal version is dated in the window (date axis) | 3 | Carlini, J. Cryptology version of 2410.05750 (ours; adjacent); Kirk, 2509.05219 conversational AI and political knowledge (ours; in scope); Evans, 2608.24748 sense-making of AI (ours; borderline) |
| arXiv, name form (sweep's `names_match` fails on `ı`) | 2 | Kıcıman, SelfSuite and ForkPilot agents (ours; borderline) |
| arXiv, probably outside the allowlist (cs.CV, cs.DC, cs.SD) | 5 | Qinlin Zhao GUI grounding; Castricato video world model; Xingyuan Bu video editing; Koh text-to-music (ours); Qingyu Yin RailWave, NUS (probably a namesake merged into the roster profile); all out of scope |
| not arXiv | 7 | Kasirzadeh, PNAS, pitfalls of autonomous AI scientists (in scope); Hassabis, Nature protein watermarking (borderline, biosecurity); Hassabis bioRxiv enzyme design, Matias Lancet diagnostic AI, Lepikhin/Matias Nature Medicine MedGemma, Matias Nature Medicine clinical AI (out); Bolin Ding, federated unlearning (out) |

One of the 17 is probably a namesake's (RailWave), none is clearly one; 2 are clearly and 5 borderline in scope. The
non-arXiv share is small (72 of 356 full-roster works in 21 days are articles) and carries one clearly in-scope paper
here, and a journal version of an arXiv preprint keeps its `arxiv` key, so gather merges it. **The family stays
general**: the reading shows no reason to restrict it to arXiv or CS.

**Volume and cost** (full roster, every id, production bucketing `plan_authors`, 64 buckets, $0.0001 a page):

| Roster | ids | 21 d (09-21..10-12): works (arXiv), requests, $ | 355 d (2025-10-01..2026-09-20): works (arXiv), requests, $ |
|---|---|---|---|
| 1.2.0 | 1,510 | 332 (273), 64, 0.0064 | 6,584 (3,292), 78, 0.0078 |
| 1.3.0 | 1,973 | 356 (290), 64, 0.0064 | 6,898 (3,528), 80, 0.0080 |
| 1.3.1 | 1,969 | 326 (283), 64, 0.0064 | 3,838 (3,090), 64, 0.0064 |

With 1.3.1, about 470 works a month and 110 a week, at one request per non-empty bucket ($0.0064 a run, weekly or
monthly); the 12-month backfill from 2025-10-01 costs under $0.01. 1.3.1's guard removes 11 merged profiles, among
them `A5100404947` (roster "Jian Yang", 2,045 of 1.3.0's backfill works): the backfill falls from 6,898 to 3,838,
mostly journal articles (3,085 → 664); the largest remaining id has 72 works. The name sweep over the same names
returned 125 papers in 21 days, ≈ 925 for the full roster by §1's projection (≈ 1,320 a month), of which by the read
above roughly two thirds outside the 42 shared papers are namesakes'.

**Verdict.** Measured on this sample, the id family returns concrete people (at most 1 of 17 OpenAlex-only works a
namesake's; about 70 % of the name sweep's own extra papers are namesakes') at a cost of about $0.01 a month, so on
precision it supersedes the name sweep. It does not yet match it on recall of our researchers: with roster 1.3.0 or
1.3.1, querying every roster id, it collects 42 of the 125 sweep papers and 10 of 25 labelled-relevant ones, and loses
an estimated 17 % of the remaining 83 (≈ 14 per 21 days in the 10 % sample; 3 of 18 read, interval 6-39 %) to
unresolved names and to OpenAlex placing new works on fresh split profiles; 7 labelled-relevant losses are ours by
read. The marginal loss to the corpus is smaller: other collectors reach 9 of the 17 ours-or-relevant losses (4
observed, 5 by std-v3 query, inferred); of the random 18, 1 ours paper has no route (≈ 5 per 21 days, interval 1-26 %,
i.e. 1-21). Inferred, not measured: at the full roster that is on the order of 150 of our researchers' papers a month
lost to the author family (interval roughly 50-340), about 50 of them (10-220) to the corpus, and in this sample 2
labelled-relevant ours papers and 5 other relevant ones with no route. Recovering the author-family losses needs a
split-profile pass (the coauthor evidence of `tools/resolve_openalex_authors.py` applied to new profiles) or a narrow
name fallback for the unresolved names.

## 10. Backfill cuts (2026-10-13)

Adopted 2026-10-13 (track T-CUTS) after Tomáš accepted the cuts the backfill analysis proposed ("Accept all 4-5
proposed cuts; document well what we may be losing"). The analysis used one rehearsal week of std-v3
(`v4/store-reh-7d`, window from 2026-10-06, 5,207 documents, screen-v12 relevance) scaled to the backfill from
2025-10-01 by 75.6 week-equivalents. Decisions: `docs/DECISIONS.md` D304-D306 (CUTS).

### 10.1 Mechanism (all cuts)

- **Where.** `configs/channels.yaml` 1.12.0, `keyword_cuts` on the `openalex` and `semantic_scholar` channels
  (`drop_sources`, `ai_term_groups`), and `s2_terms: []` on three groups. The rule code and its lists are in
  `stack/rules.py` (`keyword_cut`, `source_class`, `has_ai_term`, `AI_TERMS`, `SELF_PUBLICATION_DOI_PREFIXES`,
  `KEPT_PREPRINT_DOI_PREFIXES`, `OPENALEX_ARXIV_SOURCE_IDS`, `S2_ARXIV_VENUES`); the collectors call it per hit
  (`workflows/collect/openalex/main.py` `work_cut`, `workflows/collect/semantic_scholar/main.py` `paper_cut`).
- **Scope.** Only concept-group (keyword) hits on OpenAlex and S2. The OpenAlex institution and author-id families,
  the arXiv sweep, forums, blogs, cards and the link phase are not cut. A work that a cut drops from a group query
  and that an institution or author-id query returns in the same run is observed under that query.
- **Source class** of a hit, from its keys: `arxiv` (an `arxiv` key, which an arXiv DataCite DOI, `externalIds.ArXiv`
  or an arXiv URL among OpenAlex `locations` gives; or an OpenAlex location whose source is the arXiv repository
  `S4306400194`; or an S2 `venue` of `arXiv.org`), `kept_preprint` (SSRN, DOI prefix 10.2139), `self_publication`
  (a Zenodo key or a DOI of Zenodo 10.5281, Research Square 10.21203, Preprints.org 10.20944, OSF 10.17605 /
  10.31219 / 10.31234 / 10.31235, figshare 10.6084, TechRxiv 10.36227, Authorea 10.22541, Qeios 10.32388, bioRxiv /
  medRxiv 10.1101 and 10.64898), `publisher` (any other DOI), `no_doi`. A journal version of an arXiv preprint keeps
  the `arxiv` key and is kept.
- **Measured in production.** A dropped hit is not observed and is not in its window's `seen`. Each run's record
  counts `cut_publisher`, `cut_self_publication`, `cut_ai_term` (always present, 0 when nothing was cut) and, per
  group, `cut_<cut>:<group id>`. When a hit falls under several cuts, the first in the order `self_publication`,
  `publisher`, `ai_term` is counted. Cut 5 sends no query, so it has no count; its size is the S2 groups' earlier
  coverage `seen`.
- **Coverage key.** The cut set of a group query is part of its coverage key (`"cuts"` beside `query` and
  `filter` / `s2_query`). So switching a cut off makes a new key, and that group re-sweeps from the floor rather
  than inheriting a watermark earned with the cut on. The price: on a store that already has OpenAlex or S2 coverage
  (store-dev, store-reh-7d and their copies), the first run with channels 1.12.0 re-sweeps every concept group from
  the floor once. On a fresh backfill store there is no cost.

### 10.2 Replay on the rehearsal week

The stored OpenAlex (1,454) and S2 (299) source rows of `v4/store-reh-7d` (heads `rw4e1dw22b8kk`,
`rv0fj82sb7pnb`), each route (`metadata.matched_by`) through `keyword_cut` with the plan entries of the current
configuration, offline. A document is dropped when no member route survives; outcomes are the analysis's. Script:
`replay.py` (a session scratch script, not in this repository). The analysis's simulation (`sim2.py`) classified by the
document's merged keys; the replay uses each record's own keys, as the collectors do.

| Cut (alone) | Documents dropped, replay | Analysis | Relevant / borderline lost | Triage-relevant, no text, lost | Irrelevant / triage-skipped removed |
|---|---|---|---|---|---|
| 3: AI term on 24 groups | 263 | 263 | 3 / 0 | 0 | 17 / 222 |
| 2: self-publication servers | 433 | 434 | 114 / 0 | 52 | 72 / 195 |
| 1: publisher and no-DOI works | 680 | 680 | 15 / 2 | 65 | 14 / 523 |
| 5: S2 scheming, specs, control off | 92 | 92 | 0 / 0 | 3 | 1 / 83 |
| **All** | **1,138** | **1,138** | **129 / 2** | **117** | **89 / 740** |

The one document of difference in cut 2 is a document whose merged keys carry a self-publication DOI while one of
its OpenAlex/S2 records carries a publisher DOI only. Routes cut in the week, as the production counts would have
shown them (first cut counted): OpenAlex `self_publication` 576, `publisher` 626, `ai_term` 24, kept 539; S2
`publisher` 60, `self_publication` 2, kept 152, plus 93 routes of the three groups no longer asked. Source rows
dropped: OpenAlex 1,013 of 1,454, S2 154 of 299.

**Cut 4, the umbrella group, needs no rule of its own.** After cuts 1 and 2 every surviving `umbrella` route is
arXiv-located: OpenAlex 46 kept (all `arxiv`), 131 dropped (70 publisher, 4 no DOI, 57 self-publication); S2 6 kept
(all `arxiv`), 5 dropped.

### 10.3 Per cut

The "saved" and "lost" figures at backfill scale are the analysis's (§2.4, × 75.6 week-equivalents; the cuts are
stacked in the order 3+5, 2, 1). Example titles are from the week's dropped documents (the replay's lost list) and
the analysis's title reads.

**Cut 1: publisher journal articles only through the author-id and institution families.**

- Rule: `keyword_cuts.drop_sources: [publisher, …]` on `openalex` and `semantic_scholar` (`configs/channels.yaml`);
  `stack/rules.py` `source_class` → `publisher` or `no_doi`.
- Saved: about 32k documents beyond cuts 2-5. Publisher works were 41 % of the week's paper documents and 2.6 % of
  them were kept; most were triage-skipped, and 70 % of those triage wanted could not be acquired (publisher hosts
  answer 403).
- Lost: about 1.1k full-text relevant and 0.15k borderline documents, of which by the analysis's title read about 3
  in 15 are plausibly Stack/SR material; the rest are applied-AI governance and enterprise-agent papers. Also about
  4.9k triage-relevant documents that would never have reached a text. Examples: "The Safety Exposure Paradox: How
  Frontier AI Safety Research May Amplify Cyber Risk in Financial Infrastructure", "WHO ASSURES THE ASSURER? A
  POSITION ON META-ASSURANCE FOR AI-MEDIATED EVALUATION", "The Grounding Tax: How Domain Fine-Tuning Suppresses
  Context Utilisation in Retrieval-Augmented Generation" (the three plausible ones); "Self-Reflection and
  Error-Correction Mechanisms in LLMs Driven by Reinforcement Learning", "EPISTEMIC EXCLUSION AND INEQUALITY IN
  FRONTIER ARTIFICIAL INTELLIGENCE", "ZERO TRUST FOR MCP-CONNECTED ENTERPRISE AI AGENTS", "From model trust to
  software topology: a Perspective on structurally governed agentic AI systems", "Six pragmatic steps to separate
  medical GenAI's promise from its inherent dangers".
- Venue allowlist (D745): with `keyword_cuts.keep_venues` (on for both channels since channels.yaml 2.5.0) a
  hit with an ACL Anthology (10.18653), AAAI (10.1609), IJCAI (10.24963), NDSS (10.14722) or PLOS (10.1371) DOI is
  source class `venue` and kept; §10.5 has the
  measurement and the other candidate venues.
- Hypothesis, not measured: journal versions appear months after their preprints, so older weeks of the backfill
  probably hold a larger publisher share than the rehearsal week; the cut then saves more, and loses more, than the
  week shows.
- The SR-scope theory groups (`decision-theory`, `anthropics`, `acausal`, `program-equilibrium`, `social-choice-ai`
  and the like) lose their journal articles too, including philosophy and economics journals. The week has too few
  such hits to measure this.
- Other routes: a journal version of an arXiv preprint is kept by its `arxiv` key; the author-id family keeps a
  roster researcher's article when OpenAlex has it on the resolved profile (§9.5: precise, about 17 % recall loss on
  split profiles); the institution family keeps the configured labs' and safety orgs' articles; newsletter links
  (the link phase). In the week, 2 of the dropped publisher documents carried a roster name.
- Re-enable: remove `publisher` from `drop_sources` on the channel. A softer variant, keeping publisher works that
  OpenAlex marks open access, is not built (`is_oa` is not in `SELECT_FIELDS`; the filter name needs a probe).

**Cut 2: Zenodo and other self-publication servers out of the keyword families; SSRN kept.**

- Rule: `keyword_cuts.drop_sources: [self_publication, …]` on both channels; `stack/rules.py`
  `SELF_PUBLICATION_DOI_PREFIXES` (SSRN is `KEPT_PREPRINT_DOI_PREFIXES`).
- Saved: about 28k documents beyond cuts 3 and 5. Zenodo alone was 351 of the week's 446 self-published documents.
- Lost: about 8.6k topic-relevant documents. By the analysis's title read of 25 (one LLM reader, titles only), about
  5 are plausibly substantive, 8 weak and 12 off-topic or crank, so roughly 1-3k substantive at backfill. By the SR
  2025 base rate the SR-inclusion grade loss is about 0: none of SR 2025's 760 included links is a Zenodo link (2 are
  SSRN, which is why SSRN stays). Substantive-looking examples: "Verification Theater: measured failure modes of AI
  verifier panels" (analysis read), "Gating Tool Calls by Where Their Arguments Came From: A Prompt-Injection Defence
  Measured Without a Model", "Weakly Discriminative, Not Repaired by Profile Injection, and Manipulable:
  Self-Confidence in LLM Evaluation", "Are LLM-Agent Prompt-Injection Benchmarks Still Valid? A Cross-Model and
  Cross-Benchmark Study", "How Middle-Power Coordination Can Shape Frontier AI Governance"; and the bulk:
  "Structure-First Interpretability: Anchoring Sparse Autoencoders to the Daopology Knowledge Base", "The Hedonic
  Arrest Hypothesis: Reward-System Self-Capture as a Resolution of the Fermi Paradox", "Topological Taxonomy and
  Master Index of 156 Strategic Monographs: …".
- Other routes: an arXiv deposit of the same work (merged by the `arxiv` key); LessWrong/AF posts of the same work;
  the newsletter link phase; the institution and author-id families. In the week, 11 of the dropped self-published
  documents carried a roster author's name, all namesakes by reading. Anthropic's agentic-misalignment page, which
  OpenAlex records with a Zenodo DOI, arrives through the lab blog collectors.
- Re-enable: remove `self_publication` from `drop_sources`. To drop or keep a single server, edit
  `SELF_PUBLICATION_DOI_PREFIXES` / `KEPT_PREPRINT_DOI_PREFIXES` in `stack/rules.py`.

**Cut 3: concept groups that leak outside AI keep a hit only with an AI term.**

- Rule: `keyword_cuts.ai_term_groups` on `openalex` (24 groups: `rlhf`, `preference-opt`, `unlearning`,
  `cot-monitor`, `misalignment-behaviors`, `reward-hacking`, `data-safety`, `mech-interp`, `sparse-coding`,
  `interp-tools`, `model-diffing`, `dev-interp`, `elk`, `deception-detect`, `anomaly`, `safety-by-construction`,
  `corrigibility`, `oversight`, `w2s`, `debate`, `capability-evals`, `scheming`, `stego`, `red-team`: the unquoted,
  non-broad groups OpenAlex token-ANDs without the CS field filter). Client-side over title and abstract,
  `stack/rules.py` `AI_TERMS` and `AI_TERMS_UPPER` ("AI", "A.I.", "AGI" case-sensitive), the regex the analysis
  simulated. The groups are not phrase-quoted: that would lose about 22 relevant arXiv papers a week (1.7k at
  backfill), because relevant papers say "oversight" without "scalable" and "frontier evaluation" without the exact
  phrase. Not on S2 (phrase match).
- Saved: about 20k documents (`mech-interp`, `dev-interp`, `oversight` and `scheming` were 87-99 % waste; in random
  30 titles, about 20 `mech-interp` and 26 `dev-interp` titles were outside AI and ML altogether).
- Lost: about 0.2k relevant documents; in the week 3, all self-published (so cut 2 drops them anyway), none arXiv:
  "Architectural Frameworks for Decentralized Autonomous Intelligence: Mitigating Recursive Self-Conflict …",
  "Chemical Chain-of-Thought Functions as a Hallucination-Prone Molecular Scratchpad", "Corrigibility under Temporal
  Constraint: A Framework for Specifying and Evaluating Corrective Opportunities". The kind of paper at risk: one on
  a safety topic whose title and abstract never name an AI system in the listed words (for example only "agent" or
  "model" in a decision-theoretic framing).
- Other routes: the broad groups (`umbrella`, `specs`, `control`, `power-seeking`, …) and the arXiv keyword family,
  which phrase-match; S2 runs the same groups unfiltered (arXiv hits only, after cuts 1-2).
- Re-enable: remove the group from `ai_term_groups` (or the key). To widen the vocabulary, edit `AI_TERMS`.

**Cut 4: the umbrella group.** No rule of its own: cuts 1 and 2 remove its non-arXiv part (§10.2: every surviving
`umbrella` route is arXiv-located). The analysis put it first by exclusive waste (8.4k at backfill), almost all
publisher and self-published works.

**Cut 5: S2 groups `scheming`, `specs` and `control` off on S2.**

- Rule: `s2_terms: []` on the three groups (`configs/channels.yaml`), as C1 did for `steerability`.
- Saved: about 7k documents. In the week, `scheming` returned 38 hits (36 triage-skipped; "situational awareness"
  matches driving and military human factors), `specs` 33 (31 skipped; "model specification"), `control` 22 (17
  skipped); all were exclusive to S2, mostly publisher works.
- Lost: none in the corpus; 3 triage-relevant documents a week without a text (about 0.2k at backfill, abstract
  only).
- Other routes: the same three groups on OpenAlex (`scheming` as "AI scheming", `control` with its narrowed
  `openalex_terms`, `specs` CS-filtered), the arXiv keyword family, LessWrong/AF keyword sweeps.
- Re-enable: delete `s2_terms: []` from the group (S2 then queries `terms`).

### 10.4 Together

Backfill estimate (analysis §2.4): 132k → about 46k documents (48-50k with the author-id and arXiv kw/cat families,
which the rehearsal did not run); screened 44.9k → 28.3k; relevant 28.2k → 18.4k (Stack 25.1k → 16.0k, SR 23.1k →
15.5k). About 70 % of the relevant documents that remain are arXiv papers, the rest forum and blog posts. After the
cuts the largest waste is AI papers that are not about safety, matched by the broad groups; that is triage's job
(about $0.21 per 1,000).

Not measured offline: keyed S2 volume (22 of 60 S2 queries errored in the rehearsal), the OpenAlex `topics` filter
(§2.5 of the analysis proposes a count probe), and the rule's behaviour on records the store does not keep (the
OpenAlex `locations[].source.id` check acts only live; the replay used keys).

### 10.5 Chinese coverage (2026-10-30)

The Chinese-coverage review found the release at 76.7 % of the Concordia AI database of Chinese technical
safety papers (80.0 % collected), the misses carrying no concept group's phrase, Chinese venue-only work lost to cut 1
(non-arXiv 2025 jailbreak hits: CN 91 of 163 uncollected against US 30 of 78), and the Chinese frameworks and
standards absent. Approved and implemented (D739-D749): the venue allowlist below; the concept group `llm-safety`
("LLM safety", "safety benchmark", "safety evaluation", "harmful content", "harmlessness", "honesty", "safety
detector", "safe reinforcement learning"; OpenAlex quoted with the CS-field filter, S2 as phrases, no forum queries);
three institution queries (Shanghai AI Lab, BAAI, Zhipu; CS field); 16 Chinese safety researchers on the roster;
Concordia's newsletter feed; the hand-added `configs/overrides/china_coverage.yaml`; Chinese AI terms and safety stems
in `stack/rules.py`. The benchmark is repeatable with `tools/recall_concordia.py` (`evals/recall/`).

**Venue allowlist.** Acquire's oa lane on store documents without an arXiv key (store copy 2026-10-23; attempted =
an oa-lane `full_text` role that ended ok or failed; `oa_lane_by_prefix.py` of the Chinese-coverage work) against the 2025 non-arXiv
concept-group hits of the publisher (OpenAlex `host_organization`, all groups summed, before the cuts):

| DOI prefix | venue | oa lane ok / attempted | store documents with the prefix | 2025 hits | status |
|---|---|---|---|---|---|
| 10.18653 | ACL Anthology | 418 / 426 (98 %) | 2,867 attempted | 246 | **allowlisted** |
| 10.1609 | AAAI | 125 / 140 (89 %) | 502 attempted | 221 | **allowlisted** |
| 10.24963 | IJCAI | 35 / 38 (92 %) | 119 | not grouped | **allowlisted** (D753) |
| 10.14722 | NDSS (Internet Society) | 9 / 9 | 98 | not grouped | **allowlisted** (D753) |
| 10.3389 | Frontiers | 14 / 16 (88 %) | 38 | 644 | candidate (volume mostly not AI safety) |
| 10.1371 | PLOS | 7 / 7 | 219 | 133 | **allowlisted** (D753) |
| 10.23915 | Distill | 30 / 32 (94 %) | 42 | - | ended 2021; its works are hand-added (D664) |
| 10.70777 | SuperIntelligence - Robotics - Safety & Alignment | 14 / 18 (78 %) | 35 | - | candidate (quality unknown) |
| 10.1038 | Nature Portfolio | 71 / 141 (50 %) | 207 attempted | 438 | half fetchable |
| 10.1162 | MIT Press (TACL, CL) | 8 / 34 (24 %) | 137 attempted | 42 | low |
| 10.3390 | MDPI | 0 / 30 | 67 attempted | 1,856 | not fetchable by the lane |
| 10.52202 | NeurIPS proceedings (Curran) | 3 / 176 (2 %) | 851 attempted | - | not fetchable; the papers are mostly on arXiv |
| 10.1145 / 10.1109 / 10.1007 / 10.1016 | ACM / IEEE / Springer / Elsevier | 8 % / 7 % / 2 % / 8 % | | 483 / 3,940 / 2,785 / 4,080 | not fetchable |

PMLR, JMLR and OpenReview papers carry no DOI (source class `no_doi`): a DOI allowlist cannot admit them; an
allowlist by OpenAlex source id would (not built). "Not grouped": the venue is not a `host_organization` of
OpenAlex's 2025 grouping, so its hit volume is not measured.

