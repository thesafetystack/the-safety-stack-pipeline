# Screen eval set `2026-10-12a`

Task: `annotate.screen` (`workflows/annotate/screen/main.py`, template `screen.md`, pin `screen-v7`): from a
document's acquired text decide `relevant | borderline | irrelevant` plus routing `buckets`; tier 1 reads the
opening 8,000 characters, a `borderline` escalates to tier 2 (`screen_adjudicate` = `openai/gpt-5.6-terra`), which
reads the whole text. Frozen 2026-10-12 14:12 UTC from `v4/store-dev` (gitignored; not in the repo), documents head `rcprvcnepyb1g.documents`
(acquire run B1 of track B's T3.6), by `python -m stack.evals.freeze screen` (seed `track-e-2026-10-12a`).

## Items (`items.jsonl`, 80; `texts/<item_id>.txt.gz`)
- `input.text` = the tier-1 text exactly as production truncates it; `texts/` holds the whole text
  (`input.full_text_digest`), so `stack eval` runs `screen.ask` on the whole text: tier 1 truncates, tier 2 and
  quote anchoring see what production sends. Both digests are checked before any call.
- Pool at freeze time: 173 documents with text (url only 20: track B's url fetches were still running), so url is
  under-represented. Strata: paper 47 (openalex 36, arxiv 7, semantic_scholar 4), forum 24 (lesswrong 23,
  alignmentforum 1), url 9 (six lab blogs); proxy kw_high 46 / kw_low 34; licence open 69, public_web 9,
  restricted 2. Whole texts: median 4.2k characters, max 564k. Split 40 dev / 40 test inside each stratum.
- Some "texts" of openalex/arXiv records are landing or abstract pages (a known acquire issue, track B); they are
  kept, since production screens them as they are.

## Labels
Rubric `evals/rubrics/screen.md`. Labelers saw `{item_id, title, text}` (the tier-1 text): Opus (2 sub-agents)
and Codex `gpt-6-sol` (4 calls of 20); 15 disagreements → Opus adjudicator (labelers anonymised).

| | value |
|---|---|
| raw agreement | 65 / 80 (81 %) |
| Cohen's κ (dev / test) | 0.670 (0.574 / 0.771) |
| contested | 15 (19 %): paper 9, forum 4, url 2 |
| adjudicator sided with | Opus 7, Codex 6, third value 2 |

Disagreement is mostly `borderline` vs `relevant` (9 of 15): the rubric does not say how much safety content is
enough ("passing mention", table-of-contents mentions, incidents used as rhetoric) nor whether source attribution,
tutoring guardrails, factual-error editing or model-identity checks are techniques.

## Base rates (gold)
| | n | relevant | borderline | irrelevant |
|---|---|---|---|---|
| all | 80 | 34 | 7 | 39 |
| dev / test | 40 / 40 | 19 / 15 | 4 / 3 | 17 / 22 |
| paper / forum / url | 47 / 24 / 9 | 15 / 13 / 6 | 5 / 1 / 1 | 27 / 10 / 2 |
| kw_high / kw_low | 46 / 34 | 29 / 5 | 3 / 4 | 14 / 25 |

Buckets (gold): offtopic 39, paper 26, critique_commentary 15, eval_report 11, deployment_signal 8, incident 5,
release_news 4, lab_safety_doc 3.

Injected text: scr-0034 contains a note addressed to "AI readers" on how to summarise it (data; labelers ignored it).

## Results 2026-10-12 (runs under `v4/evals-store/screen/2026-10-12a/`, gitignored, not in the repo; RUNLOG (E))
Tier 1 only (`--no-tier2`), prompt `screen.md` (screen-v7), dev 40 × 2 repeats / test 40 × 1. acc and κ = 3-way
verdict vs gold; rel recall / precision on `relevant`; bucket J = mean Jaccard on gold-kept items. DeepSeek and
Gemini exclude the 2 `restricted` items.

| model | acc dev / test | κ dev / test | rel recall dev / test | rel precision dev / test | bucket J test | parse fail | quotes returned / dev rows | repeat agree | $/1k |
|---|---|---|---|---|---|---|---|---|---|
| anthropic/claude-haiku-5.5 | 55 / 67.5 % | 0.27 / 0.49 | 78.9 / 86.7 % | 63.8 / 61.9 % | 0.85 | 3/80, 4/40 | 2 / 80 | 79 % | 0.49-0.52 |
| openai/gpt-6-luna | 77.5 / 90 % | 0.59 / 0.81 | 97.4 / 93.3 % | 77.1 / 87.5 % | 0.72 | 0 | 0 / 80 | 90 % | 0.24-0.32 |
| deepseek/deepseek-v4.1-flash | 75.6 / 87.2 % | 0.58 / 0.76 | 69.4 / 100 % | 96.2 / 77.8 % | 0.69 | 0 | 46 / 78 | 90 % | 0.15-0.22 |
| google/gemini-3.5-flash-lite | 76.9 / 87.2 % | 0.58 / 0.77 | 88.9 / 100 % | 78.0 / 73.7 % | 0.84 | 0 | 1 / 78 | 97 % | 1.10-1.23 |
| anthropic/claude-haiku-4.5 (dev-v2 pin) | 65 / 85 % | 0.41 / 0.73 | 73.7 / 86.7 % | 75.7 / 76.5 % | 0.63 | 0 | 103 / 80 (10 voided) | 90 % | 5.80-6.75 |

McNemar vs haiku-4.5 (repeat 1): luna p = 0.016 on dev (better), haiku-5.5 p = 0.039 on test (worse).

**Tier 2** (`openai/gpt-5.6-terra` on the whole text, repeat 1, dev + test): escalation share = the tier-1
model's `borderline` rate: luna 1 / 80, deepseek 3 / 78, haiku-4.5 7 / 80, haiku-5.5 12 / 80. Of 23 escalations
tier 2 corrected 18 (tier-1 `borderline` where gold decided) and moved 5 gold-`borderline` items to a decided verdict
(tier 2 never answers `borderline`). Cost ≈ $0.017 per escalation; with luna as tier 1 it adds ≈ $0.2 per 1,000
documents.

**Why luna returned no quotes (parent's question, investigated 15:00).** Not the parser and not tier 2: the cached
raw luna answers (121) never contain a `quotes` key. Two causes in production's own contract: (1) `screen.md` makes
quotes optional ("Cite only when a single line genuinely decided it; a routine call needs no quote") and the
LLM-output schema (`_output_schema`) marks `quotes` optional — haiku-4.5 and deepseek quote anyway, luna, gemini and
haiku-5.5 take the prompt literally; (2) every luna screen call goes through the strict-schema degrade (all 80 luna
rows `schema_degraded: true`): OpenAI rejects the non-strict schema (optional properties) with a 400, the client
re-sends with the schema folded into the prompt, so the provider enforces nothing and each call costs two round trips.

**Prompt round: `screen-v8`** (`workflows/annotate/screen/screen-v8.md`, new file; production still reads
`screen.md`): quotes **required** for `relevant` and `borderline` (1-3 verbatim passages, < 200 characters), empty
for `irrelevant` unless one line decided it. The harness swaps the template with `--prompt screen-v8` (D175 (E)).
Repeat 1, tier 1 only, dev / test (v7 rows = the repeat-1 rows of the runs above):

| model | prompt | acc dev / test | κ dev / test | rel recall dev / test | rel precision dev / test | parse fail dev / test | rel+bl answers with ≥ 1 kept quote dev / test | quotes returned, verbatim | $/1k |
|---|---|---|---|---|---|---|---|---|---|
| gpt-6-luna | v7 (prod) | 82 / 90 % | 0.68 / 0.81 | 100 / 93 % | 83 / 88 % | 0 / 0 | 0/23 / 0/17 | 0 | 0.31 |
| gpt-6-luna | **v8** | 75 / 88 % | 0.55 / 0.76 | 100 / 93 % | 79 / 82 % | 0 / 0 | 25/25 / 17/17 | 93, 100 % | 0.33-0.36 |
| deepseek-v4.1-flash | v7 | 74 / 87 % | 0.56 / 0.76 | 67 / 100 % | 100 / 78 % | 0 / 0 | 13/15 / 13/18 | 51, 98 % | 0.22 |
| deepseek-v4.1-flash | v8 | 72 / 87 % | 0.51 / 0.75 | 72 / 93 % | 87 / 81 % | 0 / 0 | 18/18 / 16/16 | 131, 98 % | 0.22 |
| haiku-4.5 (dev-v2 pin) | v7 | 65 / 85 % | 0.41 / 0.73 | 74 / 87 % | 78 / 76 % | 0 / 0 | 21/23 / 19/19 | 122, 93 % | 5.8-6.7 |
| haiku-4.5 | v8 | 70 / 88 % | 0.48 / 0.77 | 79 / 93 % | 79 / 78 % | 0 / 0 | 22/22 / 19/19 | 187, 80 % | 5.9-6.1 |
| gemini-3.5-flash-lite | v7 | 77 / 87 % | 0.58 / 0.77 | 89 / 100 % | 76 / 74 % | 0 / 0 | 0/21 / 0/20 | 2, 100 % | 1.2 |
| gemini-3.5-flash-lite | v8 | 74 / 77 % | 0.54 / 0.59 | 89 / 100 % | 76 / 61 % | 0 / 0 | 22/22 / 23/23 | 97, 97 % | 1.3-1.4 |
| haiku-5.5 | v7 | 55 / 68 % | 0.28 / 0.49 | 74 / 87 % | 61 / 62 % | 2 / 4 | 2/30 / 1/24 | 3, 100 % | 0.5 |
| haiku-5.5 | v8 | 32 / 75 % | 0.07 / 0.59 | 53 / 87 % | 56 / 72 % | 15 / 5 | 20/20 / 19/19 | 84, 100 % | 0.6-1.0 |

v8 makes every model quote (all relevant/borderline answers carry an anchored quote). Cost of the change for luna,
paired over dev + test: 4 items lost, 0 gained (McNemar p = 0.12): requiring a quote nudges marginal documents to
`relevant` (2 gold-irrelevant interpretability/XAI and evaluation papers, 1 gold-borderline). deepseek 4 lost / 3
gained, haiku-4.5 2 / 5 (both p ≥ 0.45). haiku-5.5 gets worse (15/40 parse failures on dev).

**Recommendation:** screen tier 1 on `openai/gpt-6-luna` with the `screen-v8` quote rule (≈ $0.35 per 1,000
documents tier 1, ≈ $0.55 with tier 2), tier 2 unchanged; it beats the incumbent haiku-4.5 at a seventeenth of the
cost. Adopting it needs two production changes in track C's code: the screen template must become selectable by
the pin name (or `screen.md` replaced by v8), and the LLM-output schema should be made strict-compatible (every
property required, optional ones nullable) so OpenAI models stop going through the degrade. Without v8, luna's
verdicts carry no evidence quotes. haiku-5.5 is not usable at the screen as is.

## Widened round and provider caching (2026-10-12 15:25-15:45; envelope: screen ≤ $1 per 1,000 triaged documents)
Dev split, tier 1, `stack eval combine` (votes and escalation scored offline over collected rows):

| config (dev, 40 items) | acc | κ | rel recall | rel precision | rel+bl answers quoted | $/1k settled | $/1k no cache |
|---|---|---|---|---|---|---|---|
| gpt-6-luna screen-v8, 1 call | 75.0 % | 0.548 | 100 % | 79.2 % | 100 % | 0.32 | 0.46 |
| gpt-6-luna screen-v8, 3 votes | 75.0 % | 0.547 | 100 % | 76.0 % | 100 % | 0.84 | 1.47 |
| gpt-6-luna screen-v7 + terra tier 2 (production shape) | 82.5 % | 0.680 | 100 % | 82.6 % | 0 % | 0.31 | 0.43 |
| gpt-6-luna-pro screen-v8 | 77.5 % | 0.585 | 100 % | 73.1 % | 100 % | 1.15 | 1.72 |
| luna v8 → luna-pro on borderline (2.5 %) | 77.5 % | 0.587 | 100 % | 79.2 % | 100 % | 0.35 | 0.50 |

Stronger single models were not run at the screen: by the measured token counts (≈ 3,100 prompt tokens per document)
claude-sonnet-5.5 would cost ≈ $13 per 1,000 and gemini-3.8-flash ≈ $3.4 per 1,000, above the envelope; they remain
candidates for tier 2 (the 1-15 % borderline share), where terra costs ≈ $0.017 per escalation.

**Provider prompt caching.** Instructions first (system ≈ 1,920 tokens screen-v7, ≈ 1,970 v8, with the Anthropic
`cache_control` breakpoint), document last; tier 2 sends the same system prefix. Cached share (dev, tier 1): luna 77 %
(v7) / 60 % (v8), deepseek 76 % / 61 %, haiku-5.5 68 %, gemini-3.5-flash-lite 12 % / 0 %, haiku-4.5 0 %. $/1k settled
vs no cache: luna 0.24 vs 0.44 (v7), 0.36 vs 0.50 (v8); deepseek 0.15 vs 0.48; haiku-5.5 0.52 vs 0.81; haiku-4.5 6.75 vs
6.75; gemini-lite 1.10 vs 1.20.

## Amendment 2026-10-12 (track E2): 20 SR-heavy items `scr-0081` … `scr-0100`, unlabelled
Appended by `python -m stack.evals.sr append --task screen` (items 1-80 byte-identical; `manifest.json`
`amendments[0]`: items sha256 before/after, code digests, per-item query, topic and selection intent). 8 store-dev
rows with a current text not in the set (`freeze.screen_cands`, `origin: store`) and 12 LessWrong/AF posts
(`origin: external`): whole text = the forum search body minted as `collect.forums` mints it, which is exactly
what store-dev holds as a LessWrong post's current text (`text_origin: lw_search_body`, role `post`, markdown-ish
plain text); `input` and `input_digest` from `freeze.screen_message` over a minimal row, whole texts in
`texts/scr-0081…0100.txt.gz`. Intent at selection: 13 likely in, 1 marginal, 6 hard negatives or marginal (Newcomb
for humans, human introspection and psychology, game theory of people, anthropic cosmology). Split 10 / 10. Gold:
`gold-v2.jsonl` (below).
Encoding: two appended texts contain U+2028, which the harness's `splitlines` reader would split; the appended rows
escape U+2028/U+2029/U+0085 (`stack.evals.sr.jsonl`), values unchanged.

## Round 2 (track E2, 2026-10-12): Stack + Shallow Review, two relevance questions, gold v2

**Rubric** `evals/rubrics/screen-v2.md` (scope field `core`/`sr_only`/`out`, SR 2025 section `sr_category`, optional
`sr_agenda`; Tomáš's part-1 and part-2 calls quoted; the SR question is **topic** relevance, not SR's inclusion bar,
after `docs/scope/sr-2025-repo-criteria.md`). **Gold v2**: all 100 items labelled by Opus + Codex `gpt-6-sol` (the new
fields need gold on every kept item), 18 disagreements to an Opus adjudicator (verdict: Opus 6, Codex 7). Agreement:
verdict 87/100, κ 0.777 (round 1 on 80: 0.670); scope 96/100, κ 0.936. Per-question gold: `stack_verdict` = verdict
if scope `core` else irrelevant; `sr_verdict` = verdict if scope `sr_only`, or `core` with a category other than
`none`, else irrelevant; Tomáš's part-2 per-question labels (`labels-v2/tomas-part2.jsonl`) override on his 10 items
(5 overall verdicts changed); one conductor override (scr-0077, a system card: SR category `none` → `labs`, topic
reading). Base rates v2: relevant 46, borderline 8, irrelevant 46; scope core 39 / sr_only 17 / out 44. Caveat: the
first 80 items were labelled before the rubric's part-2 section and the system-card revision; the 20 SR-heavy items
after.

**Agreement with Tomáš, part 2 (10 screen items, 8 of them contested in round 1):** overall verdict gold v1 5/10
(keep/drop 8/10), gold v2 before his reference 5/10 (8/10); per question gold v2 Stack 7/10, SR 6/10. He is stricter
at the screen than the labelers: speculative or crank documents with a passing AI link (scr-0034, 0046, 0067) and a
lab economic-policy text (scr-0064) are drops or borderline for him.

**Design question (Tomáš): one call answering both questions vs two single-question screens.** Templates
`screen-v10` (both), `screen-stack-v1`, `screen-sr-v1` share a byte-identical preamble (D202); tier 1 only; per
question vs gold `stack_verdict`/`sr_verdict`; separate runs joined by `stack eval pair-screens`. Test split (50):

| model, design | S acc / κ | R acc / κ | overall keep acc / κ | quotes per kept answer S / R, verbatim | R category acc | $/1k settled / no cache | latency p50 |
|---|---|---|---|---|---|---|---|
| gpt-6-luna, one call | 86.0 % / 0.720 | 86.0 % / 0.744 | 88.0 % / 0.781 | 1.90 / 2.12, 100 / 92.7 % | 83.3 % | **0.38** / 0.62 | 4.7 s |
| gpt-6-luna, two calls | 86.0 % / 0.720 | 84.0 % / 0.727 | 82.0 % / 0.693 | 1.67 / 2.22, 100 / 98.4 % | 79.2 % | 0.72 / 1.17 | 10.2 s seq, 5.7 s par |
| deepseek-v4.1-flash, one call | 87.8 % / 0.751 | 87.8 % / 0.786 | 89.8 % / 0.822 | 2.89 / 2.85, 98 / 99 % | 87.5 % | 0.54 / 1.06 | 18.0 s |
| deepseek-v4.1-flash, two calls | 89.8 % / 0.790 | 87.8 % / 0.780 | 89.8 % / 0.816 | 3.00 / 2.96, 100 / 100 % | 87.5 % | 0.85 / 1.79 | 20.1 s seq |
| haiku-5.5, one call | 86.0 % / 0.724 | 78.0 % / 0.624 | 78.0 % / 0.630 | 1.90 / 1.92, 100 % | 82.6 % | 0.64 / 1.08 | 4.0 s |
| haiku-5.5, two calls | 82.0 % / 0.660 | 78.0 % / 0.634 | 82.0 % / 0.696 | 1.80 / 1.93, 98 / 100 % | 83.3 % | 0.94 / 1.77 | 5.9 s seq |
| sonnet-5.5, one call (upper bound) | 86.0 % / 0.740 | 82.0 % / 0.708 | 86.0 % / 0.770 | 2.09 / 1.96, 100 / 96 % | 84.0 % | 7.4 / 17.3 | 3.3 s |

Dev (50) agrees in direction: luna one call S 80 % / R 84 %, two calls 80 % / 86 %; sonnet one call S 74 % / R 88 % at
$9.2/1k. Paired over dev + test (100 items), one call vs two: luna Stack 83 vs 83 correct (2 / 2 discordant), SR 85
vs 85 (4 / 4); deepseek Stack 83 vs 85 (p 0.73), SR 86 vs 88 (p 0.62); haiku-5.5 Stack 82 vs 76 (p 0.31). **One call
does not measurably hurt accuracy and costs about half (one document read instead of two) at half the latency.**
Tomáš's 10 part-2 items, luna one call: Stack 4/10 (keep/drop 5/10), SR 5/10 (8/10) — luna misses one he keeps
(ManiEdit, knowledge editing) and keeps crank and policy texts he drops; no model reaches his strictness there.

**Tier 2 on luna's one-call borderlines** (4 of 100 escalate): `gpt-5.6-terra` decided all 4 correctly (dev overall
80 → 86 %, test 88 → 90 %), $0.007-0.010 per escalation; `claude-sonnet-5.5` 3 of 4 (one stays borderline),
$0.006-0.021 per escalation. Luna one call + terra: ≈ $0.5-1.0 per 1,000 documents settled (dev 0.99, test 0.51).

**Earlier in the round:** `screen-v9` (one verdict + scope field) on dev: luna 3/40 parse failures (the `screen`
role's 512 max_tokens truncates luna's reasoning) and haiku-5.5 HTTP 400 on every call (nullable-enum schema;
cause not verified, it disappeared with non-nullable enums) — fixed in D202 (max_tokens and schemas by name).

**Recommendation:** screen tier 1 `screen-v10` on `openai/gpt-6-luna`, tier 2 `gpt-5.6-terra` unchanged: ≈ $0.4/1k
tier 1, ≈ $0.5-1.0/1k with tier 2 (envelope ≤ $1). Separate screens stay supported (shared preamble, pin names,
`pair-screens`) for consumers that want one question only. Storing per-question answers needs `SCREEN` payload
columns (track C, D202 known gap). sonnet-5.5 does not beat luna at 20× the cost.

## Round 3 (track S, 2026-10-12 19:51-20:00): text usability in the call (`screen-v11`) and screen input limits
`openai/gpt-6-luna`, tier 1 only unless noted, dev + test (100 items), gold v2, paired per item (`stack eval compare`,
`<scratch>/trackS/pair.py`). `screen-v11` = v10 + question U (text usability, record `text_usability`). Limits:
`8000` = v3's head cut; `[h, t]` = head + tail excerpt with a length line and an omission marker.

| config | S acc | R acc | keep acc | prompt tokens | chars sent (tier 1) | $/1k settled-per-call |
|---|---|---|---|---|---|---|
| v10, 8000 (repeat 1) | 83 % | 85 % | 84 % | 4,104 | 5,175 | 0.391 |
| v10, 8000 (repeat 2: noise floor) | 83 % | 84 % | 85 % | 4,104 | 5,175 | 0.396 |
| v11, 8000 | 82 % | 85 % | 85 % | 4,654 | 5,175 | 0.460 |
| v11, [12000, 2000] | 84 % | 82 % | 83 % | 5,197 | 7,614 | 0.521 |
| v11, [20000, 3000] | 82 % | 81 % | 81 % | 5,713 | 9,957 | 0.597 |
| v11, [28000, 4000] | 82 % | 84 % | 84 % | 6,120 | 11,738 | 0.642 |

**U does not cost screen accuracy.** v10 vs v11 at the same input (100 pairs): S changed 7 (b 3 / c 2), R 2 (1 / 1),
keep 4 (1 / 2), all McNemar p = 1.0 — inside the repeat-to-repeat noise of v10 itself (S changed 9, 3 / 3; R 3; keep
4). U adds ≈ 550 prompt tokens (its definition) and ≈ 120 completion tokens: +$0.07 per 1,000 at this mix.
**Input length is flat on this gold**: on the 49 texts longer than 8,000 characters, 8,000-head vs each excerpt
changes 2-5 answers per question with b ≈ c (p ≥ 0.25). Caveat: gold v2 was labelled on the 8,000-character tier-1
text, so this set cannot reward reading further; it can only show that longer inputs do not disturb the answers.
**Usability answers** (v11, [12000, 2000]): usable 68, degraded 12, unusable 20; `not_body` 22 (20 of them openalex /
Semantic Scholar abstract-only texts under 2.6k characters; 11 other short openalex abstracts were called usable, so
abstract-only is judged inconsistently), `garbled_maths` 5, `garbled_tables` 3, `bad_encoding` 2, `cut_off` 1
(scr-0001, a stored text that really ends mid-word), `other` 3 (ROT13 meeting notes, flattened figure text). Under the
8,000 head cut U never says `cut_off` (the marker rule holds); maths and table defects rise with the text shown
(garbled_maths 2 → 5 → 7 → 10 from 8,000 to [28000, 4000]). No usability gold exists.
**Tier 2** (`gpt-5.6-terra`, whole text): 5 escalations at [12000, 2000], 8 at [28000, 4000]; keep accuracy 83 → 86 % and
84 → 86 %. Per escalation $0.004-0.016 on texts of 2-15k characters, $0.054 on the two of 84k and 91k: ≈ $0.008 + $0.52
per million characters.
**Projection to the dev store's 575 current texts** (median 33.8k characters, mean 56k, p90 123k; papers dominate):
tier 1 v11 per 1,000 screened ≈ $0.50 (8000), **$0.62 ([12000, 2000])**, $0.78 ([20000, 3000]), $0.92 ([28000, 4000]);
tier 2 whole text ≈ $0.037 per escalation (max $0.30 for the 564k-character text), i.e. $0.37 / $0.74 / $1.86 per 1,000
screened at 1 / 2 / 5 % escalation (this set's tier-1 borderline share is 4-8 %, enriched with hard items; the dev
store's last screen run had 0 of 131). Per 1,000 *triaged* documents multiply by the fetch share (≤ 0.6 on the triage
set's gold).

## Round 5 (track T-SCREEN, 2026-10-13): tier-2 cost, `screen-v12` with `doc_class`
Runs under `v4/evals-store/screen/2026-10-12a/20261013T03*`/`T04*` (not in the repo); the real-escalation pool is the separate set
`evals/screen/2026-10-13-t2pool/` (15 P5b escalations, whole texts, own gold); full tables in its `analysis.md`.

**Tier 2** (tier 1 `screen-v11` luna `[12000, 2000]`, cached; per question vs gold v2; escalations: dev 3, test 2, pool 15):

| tier 2 | Stack dev / test / pool | SR dev / test / pool | P5 mix: $ per escalation (max) | P5 mix: total $/1k | tier-2 share |
|---|---|---|---|---|---|
| none | 42 / 42 / 5 | 41 / 41 / 8 | - | 0.69 | 0 % |
| terra, whole text (v11) | 44 / 42 / 10 | 42 / 42 / 8 | 0.055 (0.23) | 2.96 | 77 % |
| terra `[12000, 2000]` | 43 / 43 / 9 | 41 / 43 / 9 | 0.013 (0.025) | 1.24 | 44 % |
| luna-pro, whole | 43 / 43 / 10 | 42 / 43 / 8 | 0.008 (0.036) | 1.01 | 32 % |
| **luna, whole** | 43 / 43 / 11 | 41 / 43 / 9 | 0.0007 settled, 0.0028 no cache (0.012) | 0.72-0.80 | 4-14 % |
| luna `[48000, 8000]` (chosen) | 42 / 43 / 10 | 41 / 43 / 7 | 0.0017 (0.0026) | 0.76 | 9 % |
| luna `[24000, 4000]` (pool only) | - / - / 11 | - / - / 8 | 0.0013 (0.0020) | 0.74 | 6 % |

Narrower trigger (escalate only when the Stack answer is borderline): on the pool every model stops worsening answers
(terra 9 corrected / 4 worsened → 7 / 0) at about half the escalations; on the eval test split it loses one SR answer.
A confidence trigger (a borderline answer at confidence ≤ 0.6) never fires: luna's borderline answers carry confidence
above 0.6. Tier 2's main effect is a second read, not a stronger model: luna re-reading more of the text after its own
`borderline` matches terra on every split within one answer, at 1/20 to 1/80 of the cost per escalation.

**`screen-v12`** (v11 + question D, `doc_class` record) as pinned (tier 2 luna `[48000, 8000]`): Stack 85/100, SR
85/100, $0.67/1k on this set (v11 + terra whole: Stack 86, SR 84, ≈ $0.96/1k). v12 vs v11 tier 1 paired (100): Stack
84→82 % dev (b 1 / c 0), 84→86 % test (0 / 1); SR 82→84 % on both splits (b 1 / c 2 each); all McNemar p ≥ 0.5;
prompt +550 tokens, +$0.10/1k.

**`doc_class`** gold: `doc-class/gold.jsonl`, 60 items (50 paper-channel items where the model decides, 10 others),
two Opus labelers (doc_type agree 54/60; self_published 60/60; language 60/60), 6 adjudicated (Opus); **superseded by
`doc-class/gold-v2.jsonl`** (Codex second labeller, section at the end). Results on gold v1 (`doc-class/analysis.md`):

| system | doc_type exact (60) | exact, model-decided (50) | paper+preprint merged (50) | self_published | $/1k |
|---|---|---|---|---|---|
| integrated (v12 record: channel rule + luna) | 53 | 46 | 49 | 59 | +0.10 |
| separate call, gpt-6-luna | 53 | 46 | 49 | 56 | 0.27 |
| separate call, claude-haiku-5.5 | 50 | 43 | 48 | 52 | 0.23 |
| separate call, gpt-5-nano | 32 | 25 | 48 | 49 | 0.52 |
| separate call, gemini-2.5-flash-lite (2 restricted items not sent) | 18 | 11 | 46 | 51 | 0.17 |
| collector `metadata.doc_type` alone | 30 | 23 | 47 | - | 0 |

The separate call (`doc-class/prompt-separate-v1.md`, screen-v12's question D verbatim, input `[6000, 1000]`) used
in-memory prices for gpt-5-nano ($0.05/$0.40) and gemini-2.5-flash-lite ($0.10/$0.40) from the OpenRouter listing of
2026-10-13; `configs/llm.yaml` was not changed. Caveat: most model-decided items are abstract-only, so paper vs
preprint follows the label convention ("cannot tell → preprint"); the cheap models and the collector call most of them
`paper`. OpenAlex's own type and venue may be the better source for that one distinction (open question).

**Follow-up (2026-10-13 04:35-04:50).** (1) The chosen tier 2 on this set (v11 tier 1, luna `[48000, 8000]`): dev 3
escalations, none corrected (Stack 42, SR 41: on these short texts the excerpt is the whole text, so dev vs whole-text
luna is run-to-run noise); test as whole-text luna. Over all three sets every tier-2 option is within two answers of
terra. v12 as pinned: dev Stack 42 / SR 43, test 43 / 42. (2) Holdout: `evals/screen/2026-10-13-holdout/` (60 fresh items,
v12 once: Stack 88.3 %, SR 90.0 %; on its gold v2, Stack 88.3 %, SR 88.3 %). (3) Stronger tier 1, `openai/gpt-6-luna-pro` with v12 on dev (tier 2 luna as
pinned): Stack 42, SR 43, the same as luna (paired: Stack 1 / 1 discordant, SR 0), at $2.11/1k settled vs $0.65 (3.3×;
≈ 3.3× the prompt tokens billed and 3× the reasoning tokens). Projected on the P5 mix ≈ $2.7 tier 1 + $0.07 tier 2 ≈
$2.8/1k: inside the $3 line, no measured gain; not adopted. (4) doc_class paper vs preprint now comes from the
collector's typed metadata where it exists (`main.metadata_paper_type`: OpenAlex `type`, S2 `publicationTypes`,
arXiv → preprint; `journal`/`conference` → paper); the model decides it only when the metadata is silent (`other`,
NULL). On the 60 doc_class items metadata settles 46; it disagrees with the text-only gold on 24, all gold `preprint` /
metadata `paper` (OpenAlex `article`, S2 journal) on abstract-only texts where the labelers' rule was "cannot tell →
preprint" — the gold cannot arbitrate these; the metadata is the better source by construction (not re-checked against
venues).

## doc_class gold v2 (2026-10-13 12:44-12:52 UTC): Codex as the second, cross-family labeller
**Protocol.** Codex `gpt-6-sol` (`codex exec --skip-git-repo-check -m gpt-6-sol -s read-only`, default settings) labelled
all 60 items independently in two calls of 30, with the Opus labelers' instruction (`doc-class/label-prompt.md`, only
"60" → "30") and the same packet (`items.jsonl` with item_id, title, chars, text_file; the same whole texts, checked
byte-identical to `texts/*.txt.gz`); it never saw the Opus labels, and its logs show it read only its packet. Where it
differed from gold v1 on doc_type, self_published or primary_lang, two blind adjudicators (labels anonymised as
labeler_1/2 in a seeded order, both evidence notes shown, prompt `doc-class/adjudicate-prompt-v2.md`): a fresh Codex
`gpt-6-sol` session and a fresh Opus sub-agent. Gold v2 takes the Codex adjudicator's label (the brief prefers a
cross-family adjudicator because both earlier labelers were Opus; rule fixed before the adjudications came back); the
Opus adjudicator is recorded. `doc-class/gold-v2-alt-opus-adjudicator.jsonl` is the same file with the Opus
adjudicator's calls, for sensitivity. Per row: `labelers` (opus-1, opus-2, codex-sol), `agreement` (all three agree),
`agreement_detail`, `contested` (any labeler disagreement, v1 or v2), `label_v1` where changed, `adjudication_v1`,
`adjudication` (both adjudicators, `adjudicators_agree`).

**Agreement (60 items).** doc_type: Codex vs gold v1 57/60, κ 0.891 (vs opus-1 52/60, κ 0.721; vs opus-2 57/60,
κ 0.891; the Opus pair was 54/60, κ 0.802); paper+preprint merged 59/60; self_published and primary_lang 60/60. The
three disagreements are all on the paper/preprint/other line: scr-0005 and scr-0024 (gold `paper`, Codex `preprint`;
abstract-only texts with no venue) and scr-0035 (gold `other`, a book chapter; Codex `preprint`; contested in v1 too).
Both adjudicators chose `preprint` for scr-0005 and scr-0024 ("cannot tell → preprint"); on scr-0035 they split, each
against its own family (Codex adjudicator `other`, Opus adjudicator `preprint`). **Gold v2 changes 2 labels** (scr-0005,
scr-0024: `paper` → `preprint`); contested 8/60 (v1: 6). Gold doc_type v2: preprint 44, forum_post 6, lab_blog 3, paper
3, eval_report 2, other 2.

**Re-scored from the stored rows** (v12 eval rows, `dcsep-*.json`, `collector-doc-types.json`; no model calls;
`doc-class/scripts/rescore.py`, which reproduces `analysis.md` on gold v1; output `doc-class/analysis-gold-v2.md`).
v1 → v2, and in brackets the Opus-adjudicator sensitivity gold:

| system | doc_type exact (60) | exact, model-decided (50) | paper+preprint merged (50) | self_published |
|---|---|---|---|---|
| integrated (v12 record: channel rule + luna) | 53 → **55** [54] | 46 → **48** [47] | 49 → 49 [48] | 59 |
| separate call, gpt-6-luna | 53 → **55** [56] | 46 → **48** [49] | 49 → 49 [50] | 56 |
| separate call, claude-haiku-5.5 | 50 → **52** [53] | 43 → **45** [46] | 48 → 48 [49] | 52 |
| separate call, gpt-5-nano | 32 → 32 [32] | 25 → 25 [25] | 48 → 48 [49] | 49 |
| separate call, gemini-2.5-flash-lite | 18 → 16 [16] | 11 → 9 [9] | 46 → 46 [47] | 51 |
| collector `metadata.doc_type` alone | 30 → 28 [28] | 23 → 21 [21] | 47 → 47 [48] | - |

The ordering is unchanged: integrated v12 and separate luna tie on gold v2 (luna two ahead on the sensitivity gold,
through scr-0035), haiku-5.5 three behind, the cheap models and the collector far behind on the exact type. Both changed items are gold-`preprint` / collector-`paper` cases, so the follow-up (4)
count of metadata `paper` against gold `preprint` would rise from 24 to about 26 on gold v2 (the collector `doc_type`
confusion is 26 on v2; `metadata_paper_type` itself was not recomputed).
