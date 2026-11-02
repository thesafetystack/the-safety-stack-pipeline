# Technique extraction quality, October 2026: what was tried, measured and decided

`annotate.technique_staged` (gpt-6-luna) reads one document and returns which of the 234 techniques of taxonomy
1.0.0 the document involves, with roles and strengths. This note records the improvement round of 2026-10-27
(gold v3 dev / validation, decision D730) and its held-out confirmation of 2026-10-28 (the judge-set test documents,
decision D735): every arm, the eval sets and splits, how the reference was built, all results with intervals, cost
and latency, the failure modes, the decision and what would reopen it.

**Decision (2026-10-28).** Production stays on the current setup: plan `batch-2026-10a`
`annotate.technique_staged` = prompt set `technique-staged-v5-a1.1`, role `extract_staged_openai` (default
reasoning effort), single mode with gleaning, doc_cap 900,000, taxonomy 1.0.0; the corpus annotations already made
with it stay. No plan, pin or config changes. The effort-high rollout is deferred (section 5).

## 1. Task, metric and production configuration

- **Production call.** One request with the rubric `rubric-v0.9-c` and the catalogue in the cached system part and the
  document (up to doc_cap characters) as the user part, then a gleaning turn on the same conversation that may only
  add rows. Model gpt-6-luna on the OpenAI API (role `extract_staged_openai`, no fallback model; D714).
- **Primary metric: slug presence.** Per document, the set of taxonomy slugs with at least one role of strength
  >= 1, group nodes left out on both sides (the gold-v3 strict unit). Micro precision / recall / F1 over documents;
  replicates of one arm pooled by averaging per-document counts (never by uniting replicate predictions). Against a
  reference with `dont_care` units, such a slug is removed from both the prediction and the reference of its
  document. Intervals: 95 % percentile document bootstrap, 2,000 resamples within stratum, paired for differences.
- Secondary views (114): (slug, role) cells at strength >= 2, recall by severity of the reference unit (core /
  substantive / peripheral), cost per 1,000 documents (standalone: every call at its provider-reported cost), latency
  per document (both turns as made).

## 2. Eval sets, splits and references

| set | documents | labels | use |
|---|---|---|---|
| gold v3 (`evals/technique_staged/2026-10-23a`) | 100: 60 papers, 30 forum posts, 4 lab blogs, 3 system cards, 3 reports; 1.3k-585k characters, median 52k | six blind agentic labeller runs: Opus 5.5 x2, Sonnet 5.5 x2, Sol 6.1 x2 | dev 60 (all tuning) / validation 40 (measured once per candidate); `adjudication/split-dev-validation.json` (= the set's `split.json`, seed 20261024, its test part named validation) |
| test (`evals/judge-2026-10-25j/technique_staged`, frozen for `stack eval` as `evals/technique_staged/2026-10-28a`) | 100, self-weighting sample of build ws1qpn8myzxah's luna rows: preprint 49, forum_post 32, paper 14, lab_blog 5; 551-389k characters, median 57k | three blind judges: Opus 5.5, Sonnet 5.5, Sol 6.1 (one run each, whole text) | held out: never run and its labels never read before the 2026-10-28 protocol commit |

Labeller agreement is the measurement ceiling's first warning: on gold v3 the labeller families agree at slug F1
.74 (Sonnet-Sol) to .83 (Opus-Sol); on the test set Opus-Sonnet .828, Opus-Sol .726, Sonnet-Sol .648, and Sol lists
~40 % more slugs than the Claude judges (5.1 vs 3.0-3.6 per document).

### 2.1 Gold v3 reference ref-v3x (the gold-v3 technique round)
1. Universe: every slug any of the six runs gave (780 units) plus 49 slugs the compared arms predicted outside that
   union (pooled over all arms, the baseline included).
2. Adjudication of the 455 units with support 1-5 of 6 and a blind calibration sample of 64 support-6 units (one per
   document with disputed units): two blind adjudicators of different families (Claude Opus 5.5 as Claude Code
   subagents; GPT Sol 6.1 via codex, read-only), guide `adjudication/GUIDE.md` v2 (threshold rules R1-R10), the
   rubric and the catalogue; the labellers' quotes shown as unattributed pointers, never support counts or any arm
   output. A pilot on 79 candidates: guide v1 present-vs-not kappa .54, v2 .56, the same family across runs .76-.80,
   so the residual disagreement is systematic between families; hence arbitration rather than a vote. Full run:
   kappa .47 (agreement .74); calibration units 61/64 both present. Unsampled support-6 units are `in`.
3. Arbitration of the 154 split units: Sol 6.1 (codex) and Fable (Claude Code subagent) saw both readings anonymised
   in random order (`adjudication/ARBITRATION.md`); they agreed on 86 -> decided; the rest `dont_care`.
4. Arm extras (49): the same two adjudicators with the arms' quotes as pointers; both present -> `in` (3), both
   absent -> `out` (27), split -> `dont_care` (19), without arbitration.
5. Result: 504 `in` (261 support-6 unadjudicated, 218 both adjudicators present, 22 both arbiters present, 3
   extras), 238 `out`, 87 `dont_care`. Implied agreement of the full references: adjudicators F1 .875, arbiters .946.

### 2.2 Test reference ref-t (the test-set confirmation)
Built as ref-v3x with three judges in place of six runs, frozen in `evals/technique_staged/2026-10-28a/PROTOCOL.md`
before any label was read, with one change prompted by the 114 critique (point 3 of section 6): arm extras go through
the same adjudication and arbitration as judge-disputed units, in the same packets, with pointer quotes pooled from
every source. Counts and agreement are in section 4.2.

## 3. Arms of the 2026-10-27 round (gold v3)

All arms use gpt-6-luna unless stated, taxonomy 1.0.0, doc_cap 900,000, max_tokens 64,000.

| arm | what | prompt set | role / effort | provider | split, replicates |
|---|---|---|---|---|---|
| BASE | production config, 4 replicates: one fresh run (B0) + two earlier runs (A11G r1, r2) + the production build's own rows (build w0vt3g81xtaqj) | v5-a1.1 + gleaning | extract_staged(_openai), default | B0 and production OpenAI direct; A11G via OpenRouter (2 transport failures on one document) | all 100 |
| BASEbp | the failure-free baseline: B0 + the production rows | as BASE | as BASE | OpenAI direct | all 100 |
| HALL | effort high, prompt and gleaning unchanged | v5-a1.1 + gleaning | extract_staged_high (OpenRouter) on dev; extract_staged_openai_high (OpenAI direct, llm.yaml 3.8.0) on validation | as role | dev: assembled from the >60k runs (H60) and the <=60k runs (HSH), 3 replicates in the scoring table; validation 3 |
| H60 | effort high only for documents > 60k characters (overlay on BASE below) | v5-a1.1 + gleaning | extract_staged_high | OpenRouter | dev >60k (25 docs) x2 |
| S1 | sector turns: after gleaning, one more turn per catalogue sector (6 sectors of 25-55 slugs, `prompts/technique-sectors-v1.yaml`), adding only new rows, shared cached prefix | v6-s1 (pin `sectors: true`) | extract_staged_openai, default | OpenAI direct | dev 2, validation 2 |
| HSALL | S1 at effort high | v6-s1 | high | dev OpenRouter, validation OpenAI direct | dev 2 (HS >60k + HSS <=60k), validation 2 |
| P1 | two system-part edits: evaluation and attack methods applied as tools (incl. red-teaming of a defence) are their entries; the interpretability-auditing identity check spelled out | v6-p1 | extract_staged_openai, default | OpenAI direct | dev 2 |
| HK | claude-haiku-5.5 at the same list price as luna | v5-a1.1 + gleaning | extract_staged | OpenRouter | dev 1 |

Offline, no LLM: the production lexical matcher over taxonomy names and aliases finds only 16 % of dev reference
units (45/286) and 3 of 62 persistent baseline misses, so an alias prefilter or hint list is not a recall lever.
Miss analysis of BASE on dev (ref >= 4/6, preliminary): recall .69; 62 persistent and 51 fickle misses of 286
units; recall core .83 / substantive .74 / peripheral .59; weakest areas agent security / containment .47,
internals / chain-of-thought .52, misalignment evaluations .49; evidence located at 50-80 % of the text .57; one
system card holds 15.7 expected misses per replicate of ~89.

### 3.1 Results on ref-v3x (presence; dF1 paired)

Dev (60 documents), against BASE:

| arm | reps | P | R | F1 | dF1 [95 % CI] | $/1k standalone | p50 s |
|---|---|---|---|---|---|---|---|
| BASE | 4 | .924 | .691 | .791 [.764, .820] | - | 6.22 | 65 |
| BASEbp | 2 | .922 | .689 | .789 | -.002 [-.012, +.009] | 7.35 | 75 |
| HALL | 3 | .901 | .771 | .831 | +.040 [+.022, +.060] | 11.00 | 187 |
| S1 | 2 | .898 | .758 | .822 | +.031 [+.003, +.059] | 12.05 | 109 |
| HSALL | 2 | .885 | .796 | .838 | +.047 [+.024, +.072] | 15.75 | 204 |
| H60 | 3 | .901 | .741 | .813 | +.023 [+.006, +.039] | 8.63 | 85 |
| P1 | 2 | .911 | .706 | .796 | +.005 [-.017, +.029] | 6.36 | 80 |
| HK (Haiku 5.5, ref-v3) | 1 | .773 | .659 | .711 | -.078 [-.137, -.013] | 42.27 | 34 |

Validation (40 documents), against BASEbp:

| arm | reps | P | R | F1 | dF1 [95 % CI] | dR | $/1k standalone | p50 s |
|---|---|---|---|---|---|---|---|---|
| BASEbp | 2 | .932 | .709 | .805 [.756, .848] | - | - | 6.74 | 62 |
| HALL | 3 | .930 | .764 | .838 | +.033 [+.015, +.055] | +.055 | 9.54 | 133 |
| S1 | 2 | .930 | .754 | .833 | +.027 [+.010, +.048] | +.045 | 11.60 | 107 |
| HSALL | 2 | .905 | .800 | .849 | +.044 [+.024, +.069] | +.092 | 16.01 | 170 |

All 100, against BASEbp: HALL +.040 [+.025, +.055] (P .911 vs .926, R .772 vs .697), S1 +.031 [+.013, +.050],
HSALL +.047 [+.030, +.064]; $/1k 10.62 / 11.87 / 15.86 vs 7.11; p50 168 / 108 / 194 s vs 65 s. Paired against HALL
on validation: HSALL +.011 [-.006, +.029], S1 -.006 [-.024, +.014], H60 -.025 [-.051, -.000].

Robustness of HALL (114, `robustness.py`): every `dont_care` unit labelled against HALL +.016 (validation) / +.025
(all 100); `dont_care` as `in` or as `out` moved arm differences by at most ~.015; single-adjudicator references
+.031 to +.046; p95 latency 368 s vs 138 s; paired per-document cost ratio high / default 1.41-1.50. Recommendation
of the round: effort high, a new role, prompt files unchanged; sectors and P1 kept on the branch as measured options.
LLM spend of the round: $12.04 (production-candidate runs; adjudication on subscription agents).

## 4. Test-set confirmation (2026-10-28)

### 4.1 Protocol (frozen before any test label was read)
`evals/technique_staged/2026-10-28a/PROTOCOL.md` and `protocol.json`, committed before any judge label, score or
disagreement file of the test set was opened. Arms: **PROD** = the production rows already annotated and evaluated
(the judge set's `production.jsonl`, build ws1qpn8myzxah, 1 replicate); **HALL** = the same pin with role
`extract_staged_openai_high`, 2 replicates via `stack eval technique_staged`; **BASEF** = the same pin as PROD run
fresh by the same harness (secondary: harness reproduction and matched cost / latency). Binding rule, on ref-t:
HALL confirms iff ΔF1 >= +0.020, ΔP >= -0.030 and HALL's median per-document latency < 180 s (point estimates).
Pre-registered readings beside it: "refuted" vs "inconclusive" for a non-confirmation; "robust" when ΔF1 > 0 against
each judge, the majority and the adversarial `dont_care` assignment.

### 4.2 Reference ref-t
- Universe: 622 (item, slug) units on 84 of the 100 documents (16 documents have no unit from any source); judge
  support 0 / 1 / 2 / 3 = 53 / 215 / 104 / 250.
- Adjudication: 433 candidates (support 0: 53, support 1: 215, support 2: 104, calibration support-3: 61) on 79
  documents in 35 batches; Opus 5.5 subagents and Sol 6.1 via codex, 114's guide and prompts unchanged. Present-vs-not
  kappa **.30** (agreement .66; 114: .47), 3-way kappa .28; confusion (Opus / Sol) present/present 186,
  absent/absent 87, absent/present 85, present/absent 47, borderline/present 15, borderline/absent 13; calibration
  59/61 both present.
- Arbitration: 160 split units on 66 documents in 23 batches; Sol 6.1 and Fable agreed on **56 of 160** (114: 86 of
  154); confusion (Sol / Fable) absent/absent 46, present/present 10, present/absent 52, absent/present 33,
  absent/borderline 12, present/borderline 7. Where they agreed they sided with Opus 26 times, with Sol 28, with
  neither 2.
- Result (`results/reference-t.jsonl`): **385 `in`** (189 unsampled support-3, 186 both adjudicators present, 10 both
  arbiters present), **133 `out`** (87 both adjudicators absent, 46 both arbiters absent), **104 `dont_care`** (17 %
  of units; 114: 10.5 %). By judge support: 0 -> 6 in / 39 out / 8 dc; 1 -> 67 / 80 / 68; 2 -> 64 / 14 / 26;
  3 -> 248 in / 2 dc. `dont_care` units by who predicts them more often: HALL 25, PROD 10, tie 69.

### 4.3 Results

Presence; HALL replicates pooled; 95 % paired document bootstrap within doc_type, 2,000 resamples, seed 20261028.

| reference | PROD P / R / F1 | HALL P / R / F1 | BASEF F1 | ΔF1 HALL-PROD | ΔP | ΔR |
|---|---|---|---|---|---|---|
| **ref-t (primary)** | .883 / .686 / .772 | .892 / .752 / .816 | .763 | **+.044 [+.022, +.068]** | **+.009 [-.016, +.035]** | +.066 [+.038, +.097] |
| judge Opus | .782 / .690 / .733 | .774 / .757 / .765 | .743 | +.032 [+.010, +.055] | -.008 [-.039, +.024] | +.067 [+.040, +.097] |
| judge Sonnet | .720 / .770 / .744 | .697 / .827 / .756 | .755 | +.012 [-.010, +.037] | -.023 [-.052, +.007] | +.057 [+.026, +.091] |
| judge Sol | .882 / .556 / .682 | .899 / .629 / .740 | .680 | +.058 [+.037, +.081] | +.017 [-.013, +.046] | +.073 [+.048, +.100] |
| majority (>= 2 of 3) | .794 / .720 / .756 | .792 / .797 / .794 | .769 | +.039 [+.019, +.060] | -.002 [-.033, +.026] | +.076 [+.048, +.107] |
| ref-t, dont_care as in | .891 / .585 / .706 | .902 / .656 / .760 | .692 | +.054 [+.030, +.080] | +.011 | +.072 |
| ref-t, dont_care as out | .822 / .686 / .748 | .813 / .752 / .781 | .744 | +.034 [+.011, +.057] | -.009 | +.066 |

**Verdict under the frozen rule: HALL confirms (robust reading).** ΔF1 +.044 >= +.020; ΔP +.009 >= -.030; HALL
median latency 147 s < 180 s; ΔF1 > 0 against all three judges, the majority and the adversarial assignment.

Sensitivities on ref-t (ΔF1 HALL - PROD): adversarial `dont_care` (each labelled against HALL) **+.014 [-.009,
+.036]** (positive, but the interval includes 0); single-adjudicator references Opus +.035 [+.012, +.061], Sol +.055
[+.031, +.079]; per replicate r1 +.040 [+.016, +.067], r2 +.048 [+.025, +.073]; leave one document out +.040 ..
+.048 (largest change +.004, jtec-0006). By stratum (PROD -> HALL F1): forum_post (32) .747 -> .833, preprint (49)
.748 -> .782, paper (14) .846 -> .900, lab_blog (5) .947 -> 1.000. By length band: <20k (29) +.040, 20-60k (26)
+.088, 60-100k (24) +.056, >100k (21) **+.010**. HALL - BASEF on ref-t +.053 [+.025, +.081]; BASEF - PROD -.009
[-.028, +.014] (the eval harness reproduces production's rows within noise; BASEF vs PROD slug agreement F1 .876,
HALL r1 vs r2 .874).

Operations (eval harness, 64 workers, OpenAI direct, no failures, no truncation, no retries needed):

| arm | observations | $/1k docs standalone | settled $ | p50 s | p95 s | max s | reasoning tokens / doc | slugs / doc |
|---|---|---|---|---|---|---|---|---|
| HALL | 200 (25 empty) | 10.65 | 2.130 | 147 | 410 | 496 | 14,806 | 3.56 |
| BASEF | 100 (14 empty) | 7.35 | 0.735 | 60 | 146 | 240 | 6,400 | 3.09 |
| PROD | 100 (12 empty) | (not in the judge set) | - | - | - | - | - | 3.21 |

Cost ratio HALL / BASEF 1.45; latency x2.4 at the median, x2.8 at p95.

### 4.4 Spend of the confirmation
API: **$2.866** (HALL $2.130, BASEF $0.735) of the $15 cap; ledger ceilings were $9 and $4.5. Adjudication and
arbitration ran on subscription agents, not the API: 35 Opus subagent sessions (~3.6M tokens), 23 Fable subagent
sessions (~2.4M tokens), 59 codex Sol calls (2.55M tokens adjudicating, 1.63M arbitrating; one batch retried after
"model at capacity").

## 5. Decision, deferral and what would reopen it

- **2026-10-28: production unchanged.** The confirmation run was decided beforehand as a confirmation step only:
  whatever it showed, `annotate.technique_staged` stays on role `extract_staged_openai` at default effort and the
  corpus annotations already made with it stay. No plan, pin, prompt or `llm.yaml` change follows from this note.
- **Effort-high rollout deferred.** Switching the pin's role is a new config digest, so every document is re-asked:
  at the measured $10.65 / 1k standalone, ~$420 for the ~39k-document corpus (39,258 luna rows in build
  ws1qpn8myzxah). The $350 technique_staged budget (D716) was spent on the full run, so a rollout needs a new
  allocation, and a presence-F1 gain of about +.04 alone does not justify one; a default-effort re-ask costs
  ~$290 at $7.35 / 1k. Wall time per document roughly doubles (p50 147 s vs 60 s) on the same worker count.
- What the measurement supports if the switch is made: presence F1 about +.03 to +.05 on the self-weighting
  corpus sample (recall +.06 to +.08, precision not measurably lower on the adjudicated reference, -.01 to -.02
  against the most inclusive judge), consistent across the 114 validation split and this held-out test set, least
  on documents over 100k characters (+.010 here).
- **Reopen when** any of: (1) a corpus-wide re-ask is needed anyway (a prompt, rubric, taxonomy or model change that
  moves the technique_staged digest), most likely the next taxonomy update: then effort high costs only the
  difference, ~$130 at current prices, and should be the default candidate; (2) a new allocation of ~$420 plus
  margin is justified on other grounds; (3) recall of technique
  presence becomes the binding limit for a release consumer (dashboards, technique pages); (4) luna's price or
  latency at effort high changes, or a new luna version arrives (re-measure on this test set with the same protocol:
  the set, scorer and reference are reusable, but a re-run on the same test documents is no longer held out).

## 6. Failure modes and lessons

- **The reference is the bottleneck, not the arm.** Labeller families agree at F1 .65-.83; adjudicator families at
  kappa .47 (gold v3) and .30 (test); arbiters converged on 86/154 and 56/160 splits. The disagreement is systematic
  between model families (Claude adjudicators reject loose analogues more often; Sol accepts more), so a sharper guide
  did not close it (v1 .54 -> v2 .56 on the pilot). `dont_care` (10-17 % of units) is excluded from scoring; its
  direction matters: on the test set HALL predicts more of them, and the adversarial assignment shrinks ΔF1 from
  +.044 to +.014. Any future arm comparison should report the adversarial line beside the headline.
- **Per-judge numbers mislead without the reference.** Against Sonnet (the most inclusive Claude judge) effort high
  looks like +.012 with a precision cost; against Sol +.058 with a precision gain. Only the adjudicated reference
  and the majority agree on the size.
- **Mixed baselines.** 114's four-replicate BASE mixed OpenRouter runs (2 transport failures on one document) with
  OpenAI-direct ones; failure-free BASEbp is the clean comparison (Astra critique). Here BASEF (same harness, OpenAI
  direct) reproduced PROD within noise (-.009 [-.028, +.014]).
- **Recall levers that did not work.** Lexical alias matching finds 16 % of reference units; the p1 prompt edits
  +.005 [-.017, +.029]; Haiku 5.5 at luna's list price -.078 at 6x cost (no cross-document prompt-cache hits under
  the shared rubric prefix; precision .77). Sector turns help (+.027 to +.031) but cost more than effort high for
  less; combined with effort high they add +.011 [-.006, +.029] over effort high at 1.7x its cost.
- **Long documents.** 114's misses concentrate in long, dense documents (one system card holds ~1/6 of dev misses),
  yet effort high gains least there (+.010 above 100k characters on the test set): the remaining misses there are
  not fixed by more reasoning on the same single pass.
- **Operational.** Effort high doubles median latency and nearly triples p95 (410 s); a codex Sol call failed once
  with "model at capacity" and succeeded on retry; no extraction failures in 300 test observations.
- **Discipline that held.** The test set was untouched until the protocol commit; the rule, references, readings and
  spend ceilings were frozen first; the scorer was checked against the judge set's committed production-vs-judge
  numbers (identical tp / fp / fn) before the adjudicated reference existed.

## 7. Files

- `evals/technique_staged/2026-10-23a/adjudication/`: ref-v3x (`reference-v3x.jsonl`), GUIDE.md v2, ARBITRATION.md,
  `check.py`, the dev / validation split.
- `evals/technique_staged/2026-10-28a/`: the frozen test protocol (`PROTOCOL.md`, `protocol.json`), the set's
  `items.jsonl` / `manifest.json` and `scripts/freeze.py`, `scripts/confirm.py` (units, packets, reference,
  scores), `results/reference-t.jsonl` (slug level, no text), `results/scores.json` / `scores.md`.
- `evals/judge-2026-10-25j/technique_staged/`: the test labels (three judges) and the production rows scored here.
- Code of the arms: `workflows/annotate/technique_staged/main.py` (pin key `sectors`), prompt sets
  `technique-staged-v6-s1` / `v6-p1` / `v6-s2`, `configs/llm.yaml` 3.8.0 role `extract_staged_openai_high` (D730).
