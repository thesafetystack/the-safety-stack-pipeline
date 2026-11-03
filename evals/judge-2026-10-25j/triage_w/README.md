# triage_w: positive-weighted triage supplement (2026-10-26)

200 documents of the triage population, stratified by production's verdict and oversampling production's positives
and its low-confidence `not_relevant` rows, labelled blind by the same judges as the proportional `triage/` set
(Opus 5.5, Sol 6.1, Sonnet 5.5) with the same jail, wrapper prompt, guides and schema (sha256-checked against
`guides/` for every item). Scored with design weights, so the numbers are population estimates. No text is here: the
rendered records are in the private directory outside the repository (`../README.md`).

Why: triage only gates fetching (a document that already has text skips it); its costly error is a false negative,
a relevant document never fetched. The proportional set has 9 production positives, too few to estimate recall.

## Design

- **Population**: the triage frame of `scripts/sample.py` (149,644 `documents` rows in state acquired with an LLM
  triage verdict; 28 retired rows out). By production verdict: not_relevant 135,483, relevant 8,726, borderline
  5,435 (135,507 / 8,729 / 5,436 with the 28 retired rows).
- **Exclusions**: every existing eval set (`common.existing_eval_keys`, as for `triage/`: 103) and every document
  of the five 2026-10-25j sets, the 100 jtri items included (151 matched); eligible 149,390.
- **Strata and allocation**: relevant 30, borderline 30, not_relevant split by production confidence (discrete, steps
  of 0.01) at <= 0.94 (13.0 % of not_relevant), 0.95-0.98 (36.8 %), 0.99 (50.2 %), allocated 70 / 40 / 30. A
  lowest-decile cut falls inside the 0.94 mass point (<= 0.93 is 7.6 %, <= 0.94 13.0 %); 0.94 was
  taken. Evidence basis (fields / title) is stratified implicitly (first sort key). `sampling.json` has the stratum
  sizes, weights, cut points, the full not_relevant confidence distribution and the per-stratum basis split.
- **Selection**: systematic within stratum over units sorted by (basis, anchor channel, year bucket, unit key); seed
  scheme of `sample.py` (seed 20261025, set name `triage_w`). Weight w = N_h / n_h over the eligible frame.
- **Freeze**: `freeze.freeze_triage` unchanged (task dirs redirected to `tasks/triage_w/`): rendered record rebuilt with
  production's functions, `rendered_digest` matches the evidence row for 200 / 200.

## Scoring (`scripts/score_triage_w.py`)

Production is the prediction; references: each judge, consensus (Opus = Sol), majority (>= 2 of 3). Estimated
population confusion T[a][b] = sum_h N_h / |S_h| #{i in S_h: production a, reference b}, S_h = the stratum's items the
reference labels (a dropped item is re-weighted within its stratum: optimistic for the consensus, which drops the
disagreements). Fetch (relevant|borderline vs not_relevant) and per-class P / R / F1, estimated missed documents
(production not_relevant, reference relevant; or relevant|borderline), 95 % percentile bootstrap (2,000 resamples
within each (sample, stratum) cell, seed 20261026). Per not_relevant stratum also a Wilson 95 % interval on the missed
rate scaled to N_h: the bootstrap gives a zero-width interval where no miss was observed (simulation: 0.82 coverage
for rare misses), the Wilson interval does not (`missed_keep_wilson95`; for relevant alone `missed_relevant_wilson95`,
added 2026-10-27).

Three designs are reported: `triage_w` alone; `pooled` = triage_w plus the 100 jtri items post-stratified into the
same five strata (per stratum N_h x the pooled mean: the sample-size-weighted composite of two independent estimates;
N_h from the triage set's eligible frame); `triage` (the proportional set alone, its own strata) for comparison.

## Results (2026-10-26; full tables in `scores.md`)

600 judge outputs, all valid (3 after the runner's one retry: Sol jtrw-0085, jtrw-0092, Sonnet jtrw-0130). Production vs the 3-judge majority
(197 / 200 items have one), fetch view: precision 0.650 [0.527, 0.760], recall 0.900 [0.803, 0.975], F1 0.755
[0.657, 0.837]; pooled with jtri: 0.673 / 0.913 / 0.775. Estimated missed documents in production's 135k
not_relevant: relevant 0 (no item in any stratum), relevant|borderline 1,022 [252, 2,074] (pooled 905 [223, 1,833]),
all from the <= 0.94 confidence stratum (4 / 69 items, all borderline-ish: benchmarks of agents, LLM criteria use,
learning from human reward). The 0.95-0.98 and 0.99 strata had no miss in 70 items (pooled 152); their Wilson upper
bounds (2,340 and 3,352 documents pooled) are the honest uncertainty there, not the bootstrap's zero-width interval.

- The precision side is the larger disagreement: of production's relevant, the majority calls 7 / 30 not_relevant and
  7 borderline; of production's borderline, 15 / 30 not_relevant.
- Consensus (Opus = Sol) shows recall 1.000 but drops 43 / 200 items where the two disagree (18 of them in the
  low-confidence not_relevant stratum): optimistic by construction; the majority is the better reference here.
- Sol is the outlier: it calls 9 of the 70 low-confidence not_relevant items relevant (Opus and Sonnet 0), mostly
  agentic-RL and capability papers; Sol-as-reference gives 3,512 [1,259, 6,758] missed relevant. Opus-Sonnet agree on
  0.895 of items raw, Opus-Sol 0.785, Sol-Sonnet 0.755.

## Files

`sample.jsonl`, `sampling.json`, `items.jsonl`, `production.jsonl`, `manifest.json` (freeze);
`labels-{opus,sol,sonnet}.jsonl` (`release.release_set`, same capping: verbatim fields at 300 characters at a word
boundary with "…"); `scores.md`, `scores.json`, `disagreements.jsonl` (items where production and the judges do not
all agree, with stratum and weight); `existing-eval-keys.json`, the keys (doc ids, text digests, normalised titles)
of the earlier eval sets that the frame excludes, which the release reads instead of those sets' items (D795). A dataset release re-scores this set on its own build with the strata
recomputed there (`stack.evals.release_quality`, D728; `../README.md`, "Re-scoring at release time").

## Running

    # environment as for the parent set (`../README.md`, "Running")
    $PY scripts/triage_w.py sample && $PY scripts/triage_w.py freeze
    $PY scripts/triage_w.py run --judges opus,sol,sonnet --parallel-opus 2 --parallel-sol 6 --parallel-sonnet 2
    $PY scripts/triage_w.py validate && $PY scripts/score_triage_w.py && $PY scripts/triage_w.py release
    $PY scripts/secret_scan.py triage_w

`triage_w.py` imports the shared modules unchanged and registers the set name `triage_w` process-locally (it is not in
`common.SETS`, so `validate.py` / `score.py` / `release.py` without arguments do not see it).

## Cost

Opus notional `total_cost_usd` $8.97, Sonnet $4.39 (pilot included). Sol: 13.1M input tokens (11.8M cached), 92k
output. Sol hit 40 usage-limit pauses (runner waits, no attempt spent), so the run took 12:16-16:50Z.
