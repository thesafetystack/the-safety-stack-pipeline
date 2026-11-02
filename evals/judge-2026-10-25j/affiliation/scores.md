# Judge eval scores: affiliation (2026-10-25j)

100 items. Judge records: opus: {"affiliation:valid": 100}; sol: {"affiliation:valid": 100}; sonnet: {"affiliation:valid": 100}

Production is the prediction, the judge (or the reference label set) the reference. Majority = items (categorical) or units (sets) on which at least 2 of the 3 judges (Opus, Sol, Sonnet) agree. Consensus = items (categorical) or units (sets) on which Opus and Sol agree (the 2026-10-25 two-judge rule; Sonnet ignored).

## Headline: production vs majority (2 of 3) and vs consensus (Opus and Sol)

| units | items | P / R / F1 vs majority | majority units | P / R / F1 vs consensus | consensus units |
|---|---|---|---|---|---|
| (author, verbatim surface) [normalised: case, spacing, edge markers] | 100 | 0.823 / 0.840 / 0.832 | 188 | 0.792 / 0.899 / 0.842 | 169 |
| (author, org slug) [registry match on the surfaces] | 100 | 1.000 / 0.963 / 0.981 | 27 | 1.000 / 0.963 / 0.981 | 27 |
| authors with at least one affiliation | 100 | 0.994 / 1.000 / 0.997 | 163 | 0.957 / 1.000 / 0.978 | 157 |

Per-class P / R / F1 for each categorical judgement are in its section below.

## window split (consensus surfaces vs luna's header window, 8k head + 2k tail)

```
{
 "inside_window": {
  "consensus_units": 169,
  "production_recall": 0.8994
 },
 "outside_window": {
  "consensus_units": 0,
  "production_recall": null
 }
}
```

## window split (majority surfaces vs luna's header window, 8k head + 2k tail)

```
{
 "inside_window": {
  "majority_units": 188,
  "production_recall": 0.8404
 },
 "outside_window": {
  "majority_units": 0,
  "production_recall": null
 }
}
```

## (author, verbatim surface) [normalised: case, spacing, edge markers]

| comparison | items | P [95% CI] | R [95% CI] | F1 [95% CI] | Jaccard | tp / fp / fn |
|---|---|---|---|---|---|---|
| production_vs_opus | 100 | 0.828 [0.691, 0.946] | 0.841 [0.703, 0.955] | 0.835 [0.699, 0.948] | 0.716 | 159 / 33 / 30 |
| production_vs_sol | 100 | 0.859 [0.730, 0.964] | 0.902 [0.780, 0.989] | 0.880 [0.757, 0.971] | 0.786 | 165 / 27 / 18 |
| production_vs_sonnet | 100 | 0.823 [0.688, 0.939] | 0.845 [0.705, 0.961] | 0.834 [0.697, 0.947] | 0.715 | 158 / 34 / 29 |
| opus_vs_sol | 100 | 0.894 [0.766, 0.994] | 0.923 [0.810, 1.000] | 0.909 [0.791, 0.994] | 0.833 | 169 / 20 / 14 |
| opus_vs_sonnet | 100 | 0.989 [0.974, 1.000] | 1.000 [1.000, 1.000] | 0.995 [0.987, 1.000] | 0.989 | 187 / 2 / 0 |
| sol_vs_sonnet | 100 | 0.918 [0.804, 0.995] | 0.898 [0.767, 1.000] | 0.908 [0.791, 0.994] | 0.832 | 168 / 15 / 19 |
| production_vs_consensus | 100 | 0.792 [0.642, 0.918] | 0.899 [0.770, 0.994] | 0.842 [0.711, 0.945] | 0.727 | 152 / 40 / 17 |
| production_vs_majority | 100 | 0.823 [0.688, 0.939] | 0.840 [0.702, 0.955] | 0.832 [0.696, 0.945] | 0.712 | 158 / 34 / 30 |

Consensus units (Opus and Sol both have): 169; production's precision against the union (Opus | Sol): 0.896 (192 production units).

Majority units (at least 2 of the 3 judges have): 188; production's precision against the union (Opus | Sol | Sonnet): 0.896 (192 production units).

## (author, org slug) [registry match on the surfaces]

| comparison | items | P [95% CI] | R [95% CI] | F1 [95% CI] | Jaccard | tp / fp / fn |
|---|---|---|---|---|---|---|
| production_vs_opus | 100 | 1.000 [1.000, 1.000] | 0.963 [0.929, 1.000] | 0.981 [0.963, 1.000] | 0.963 | 26 / 0 / 1 |
| production_vs_sol | 100 | 1.000 [1.000, 1.000] | 0.963 [0.929, 1.000] | 0.981 [0.963, 1.000] | 0.963 | 26 / 0 / 1 |
| production_vs_sonnet | 100 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 | 26 / 0 / 0 |
| opus_vs_sol | 100 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 | 27 / 0 / 0 |
| opus_vs_sonnet | 100 | 0.963 [0.929, 1.000] | 1.000 [1.000, 1.000] | 0.981 [0.963, 1.000] | 0.963 | 26 / 1 / 0 |
| sol_vs_sonnet | 100 | 0.963 [0.929, 1.000] | 1.000 [1.000, 1.000] | 0.981 [0.963, 1.000] | 0.963 | 26 / 1 / 0 |
| production_vs_consensus | 100 | 1.000 [1.000, 1.000] | 0.963 [0.929, 1.000] | 0.981 [0.963, 1.000] | 0.963 | 26 / 0 / 1 |
| production_vs_majority | 100 | 1.000 [1.000, 1.000] | 0.963 [0.929, 1.000] | 0.981 [0.963, 1.000] | 0.963 | 26 / 0 / 1 |

Consensus units (Opus and Sol both have): 27; production's precision against the union (Opus | Sol): 1.000 (26 production units).

Majority units (at least 2 of the 3 judges have): 27; production's precision against the union (Opus | Sol | Sonnet): 1.000 (26 production units).

## authors with at least one affiliation

| comparison | items | P [95% CI] | R [95% CI] | F1 [95% CI] | Jaccard | tp / fp / fn |
|---|---|---|---|---|---|---|
| production_vs_opus | 100 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 | 164 / 0 / 0 |
| production_vs_sol | 100 | 0.957 [0.874, 1.000] | 1.000 [1.000, 1.000] | 0.978 [0.933, 1.000] | 0.957 | 157 / 7 / 0 |
| production_vs_sonnet | 100 | 0.994 [0.979, 1.000] | 1.000 [1.000, 1.000] | 0.997 [0.990, 1.000] | 0.994 | 163 / 1 / 0 |
| opus_vs_sol | 100 | 0.957 [0.874, 1.000] | 1.000 [1.000, 1.000] | 0.978 [0.933, 1.000] | 0.957 | 157 / 7 / 0 |
| opus_vs_sonnet | 100 | 0.994 [0.979, 1.000] | 1.000 [1.000, 1.000] | 0.997 [0.990, 1.000] | 0.994 | 163 / 1 / 0 |
| sol_vs_sonnet | 100 | 1.000 [1.000, 1.000] | 0.963 [0.878, 1.000] | 0.981 [0.935, 1.000] | 0.963 | 157 / 0 / 6 |
| production_vs_consensus | 100 | 0.957 [0.874, 1.000] | 1.000 [1.000, 1.000] | 0.978 [0.933, 1.000] | 0.957 | 157 / 7 / 0 |
| production_vs_majority | 100 | 0.994 [0.979, 1.000] | 1.000 [1.000, 1.000] | 0.997 [0.990, 1.000] | 0.994 | 163 / 1 / 0 |

Consensus units (Opus and Sol both have): 157; production's precision against the union (Opus | Sol): 1.000 (164 production units).

Majority units (at least 2 of the 3 judges have): 163; production's precision against the union (Opus | Sol | Sonnet): 1.000 (164 production units).

_note_: production surfaces: items with resolution 'text' (openalex / email-domain fallbacks are not text surfaces); authors keyed by production's name key (norm_author_name)

## Disagreements (1)

- {"item_id": "jaff-0087", "unit": "(author, org slug)", "majority_missing_in_production": ["('alex beutel', 'google-research')"], "production_not_in_majority": [], "production_not_in_any_judge": [], "consensus_missing_in_production": ["('alex beutel', 'google-research')"], "judges_disagree": ["('alex beutel', 'google-research')"]}
