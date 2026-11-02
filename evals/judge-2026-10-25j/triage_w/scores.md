# triage_w: weighted triage supplement, scores

Production (luna) = prediction; references = each judge, consensus (Opus = Sol), majority (>= 2 of 3). Weighted population estimates (design weights N_h / n_h, re-weighted within stratum over the items the reference labels); 95 % stratified document-bootstrap intervals (2,000 resamples, seed 20261026). Fetch = relevant|borderline vs not_relevant. Missed = estimated documents in production's not_relevant population that the reference labels relevant (or relevant|borderline). Method: `scripts/score_triage_w.py` docstring.

## Design

| stratum | N_h (eligible) | n_h | weight | jtri items in it | pooled N_h |
|---|---|---|---|---|---|
| relevant | 8,667 | 30 | 288.9 | 6 | 8,706 |
| borderline | 5,408 | 30 | 180.3 | 3 | 5,428 |
| not_relevant|c<=0.94 | 17,631 | 70 | 251.9 | 9 | 17,640 |
| not_relevant|c0.95-0.98 | 49,805 | 40 | 1245.1 | 38 | 49,843 |
| not_relevant|c0.99 | 67,879 | 30 | 2262.6 | 44 | 67,924 |

Cut points (production confidence of not_relevant): <= 0.94, <= 0.98, rest (0.99).

Validation status: triage_w opus: {'valid': 200}; triage_w sol: {'valid': 200}; triage_w sonnet: {'valid': 200}; triage opus: {'valid': 100}; triage sol: {'valid': 100}; triage sonnet: {'valid': 100}

## triage_w alone (200 items)

| reference | scored / items | fetch P | fetch R | fetch F1 | missed relevant | missed rel.+border. | agreement |
|---|---|---|---|---|---|---|---|
| opus | 200 / 200 | 0.664 [0.544, 0.774] | 0.861 [0.763, 0.951] | 0.750 [0.655, 0.833] | 0 [0, 0] | 1,511 [504, 2,771] | 0.940 [0.925, 0.954] |
| sol | 200 / 200 | 0.818 [0.731, 0.898] | 0.657 [0.525, 0.806] | 0.729 [0.629, 0.827] | 3,512 [1,259, 6,758] | 6,016 [2,771, 10,507] | 0.931 [0.901, 0.955] |
| sonnet | 200 / 200 | 0.651 [0.531, 0.762] | 0.858 [0.763, 0.950] | 0.741 [0.644, 0.827] | 0 [0, 0] | 1,511 [504, 2,771] | 0.932 [0.917, 0.946] |
| consensus | 157 / 200 | 0.750 [0.619, 0.866] | 1.000 [1.000, 1.000] | 0.857 [0.764, 0.928] | 0 [0, 0] | 0 [0, 0] | 0.971 [0.958, 0.982] |
| majority | 197 / 200 | 0.650 [0.527, 0.760] | 0.900 [0.803, 0.975] | 0.755 [0.657, 0.837] | 0 [0, 0] | 1,022 [252, 2,074] | 0.943 [0.928, 0.956] |

Per class (P / R / F1 with intervals):

| reference | class | precision | recall | F1 |
|---|---|---|---|---|
| consensus | relevant | 0.889 [0.727, 1.000] | 0.900 [0.806, 1.000] | 0.895 [0.789, 0.974] |
| consensus | borderline | 0.368 [0.154, 0.600] | 1.000 [1.000, 1.000] | 0.538 [0.267, 0.750] |
| consensus | not_relevant | 1.000 [1.000, 1.000] | 0.975 [0.962, 0.986] | 0.987 [0.981, 0.993] |
| majority | relevant | 0.533 [0.367, 0.700] | 0.889 [0.774, 1.000] | 0.667 [0.495, 0.802] |
| majority | borderline | 0.357 [0.179, 0.536] | 0.388 [0.214, 0.586] | 0.372 [0.200, 0.527] |
| majority | not_relevant | 0.992 [0.985, 0.998] | 0.965 [0.953, 0.975] | 0.978 [0.971, 0.985] |

Weighted confusion vs majority (production->reference, estimated documents): borderline->borderline 1,931, borderline->not_relevant 2,897, borderline->relevant 579, not_relevant->borderline 1,022, not_relevant->not_relevant 134,293, relevant->borderline 2,022, relevant->not_relevant 2,022, relevant->relevant 4,622

## pooled: triage_w + the 100 proportional jtri items, post-stratified

| reference | scored / items | fetch P | fetch R | fetch F1 | missed relevant | missed rel.+border. | agreement |
|---|---|---|---|---|---|---|---|
| opus | 300 / 300 | 0.671 [0.562, 0.780] | 0.812 [0.695, 0.923] | 0.735 [0.645, 0.819] | 0 [0, 0] | 2,202 [862, 4,150] | 0.933 [0.917, 0.948] |
| sol | 300 / 300 | 0.821 [0.734, 0.901] | 0.725 [0.623, 0.832] | 0.770 [0.695, 0.840] | 2,649 [1,116, 4,597] | 4,404 [2,394, 6,991] | 0.940 [0.921, 0.956] |
| sonnet | 300 / 300 | 0.677 [0.567, 0.775] | 0.860 [0.771, 0.939] | 0.757 [0.672, 0.832] | 0 [0, 0] | 1,563 [670, 2,679] | 0.932 [0.919, 0.945] |
| consensus | 251 / 300 | 0.747 [0.621, 0.863] | 1.000 [1.000, 1.000] | 0.855 [0.766, 0.926] | 0 [0, 0] | 0 [0, 0] | 0.967 [0.956, 0.978] |
| majority | 296 / 300 | 0.673 [0.562, 0.773] | 0.913 [0.834, 0.978] | 0.775 [0.691, 0.849] | 0 [0, 0] | 905 [223, 1,833] | 0.942 [0.929, 0.955] |

Per class (P / R / F1 with intervals):

| reference | class | precision | recall | F1 |
|---|---|---|---|---|
| consensus | relevant | 0.857 [0.708, 1.000] | 0.852 [0.764, 0.938] | 0.855 [0.750, 0.941] |
| consensus | borderline | 0.333 [0.143, 0.538] | 1.000 [1.000, 1.000] | 0.500 [0.250, 0.700] |
| consensus | not_relevant | 1.000 [1.000, 1.000] | 0.974 [0.962, 0.986] | 0.987 [0.981, 0.993] |
| majority | relevant | 0.514 [0.361, 0.676] | 0.836 [0.724, 0.936] | 0.637 [0.487, 0.767] |
| majority | borderline | 0.355 [0.194, 0.531] | 0.380 [0.225, 0.562] | 0.367 [0.215, 0.523] |
| majority | not_relevant | 0.993 [0.986, 0.998] | 0.967 [0.956, 0.977] | 0.980 [0.973, 0.986] |

Weighted confusion vs majority (production->reference, estimated documents): borderline->borderline 1,926, borderline->not_relevant 2,626, borderline->relevant 876, not_relevant->borderline 905, not_relevant->not_relevant 134,502, relevant->borderline 2,239, relevant->not_relevant 1,990, relevant->relevant 4,477

## proportional triage set alone (100 items, its own strata), for comparison

| reference | scored / items | fetch P | fetch R | fetch F1 | missed relevant | missed rel.+border. | agreement |
|---|---|---|---|---|---|---|---|
| opus | 100 / 100 | 0.801 [0.584, 1.000] | 0.791 [0.580, 1.000] | 0.796 [0.606, 0.952] | 0 [0, 0] | 2,987 [0, 7,469] | 0.916 [0.876, 0.946] |
| sol | 100 / 100 | 0.891 [0.674, 1.000] | 1.000 [1.000, 1.000] | 0.943 [0.806, 1.000] | 0 [0, 0] | 0 [0, 0] | 0.953 [0.933, 0.964] |
| sonnet | 100 / 100 | 0.891 [0.674, 1.000] | 0.897 [0.719, 1.000] | 0.894 [0.750, 1.000] | 0 [0, 0] | 1,442 [0, 4,325] | 0.940 [0.907, 0.973] |
| consensus | 94 / 100 | 0.797 [0.390, 1.000] | 1.000 [1.000, 1.000] | 0.887 [0.561, 1.000] | 0 [0, 0] | 0 [0, 0] | 0.959 [0.931, 0.973] |
| majority | 99 / 100 | 0.891 [0.674, 1.000] | 1.000 [1.000, 1.000] | 0.943 [0.806, 1.000] | 0 [0, 0] | 0 [0, 0] | 0.936 [0.916, 0.963] |

Per class (P / R / F1 with intervals):

| reference | class | precision | recall | F1 |
|---|---|---|---|---|
| consensus | relevant | 0.667 [0.000, 1.000] | 0.511 [0.000, 0.610] | 0.578 [0.000, 0.758] |
| consensus | borderline | 0.000 [0.000, 0.000] | - | - |
| consensus | not_relevant | 1.000 [1.000, 1.000] | 0.985 [0.957, 1.000] | 0.993 [0.978, 1.000] |
| majority | relevant | 0.352 [0.000, 0.750] | 0.439 [0.000, 0.610] | 0.391 [0.000, 0.654] |
| majority | borderline | 0.278 [0.278, 0.278] | 0.269 [0.174, 1.000] | 0.273 [0.214, 0.435] |
| majority | not_relevant | 1.000 [1.000, 1.000] | 0.989 [0.967, 1.000] | 0.994 [0.983, 1.000] |

Weighted confusion vs majority (production->reference, estimated documents): borderline->borderline 1,509, borderline->relevant 3,919, not_relevant->not_relevant 135,407, relevant->borderline 4,105, relevant->not_relevant 1,534, relevant->relevant 3,068

## Raw per-stratum counts (triage_w; reference label -> items)

| stratum | production | reference | counts |
|---|---|---|---|
| relevant | relevant | opus | borderline 7, not_relevant 7, relevant 16 |
| relevant | relevant | sol | borderline 1, not_relevant 2, relevant 27 |
| relevant | relevant | sonnet | borderline 13, not_relevant 7, relevant 10 |
| relevant | relevant | consensus | None 12, not_relevant 2, relevant 16 |
| relevant | relevant | majority | borderline 7, not_relevant 7, relevant 16 |
| borderline | borderline | opus | borderline 11, not_relevant 15, relevant 4 |
| borderline | borderline | sol | borderline 11, not_relevant 11, relevant 8 |
| borderline | borderline | sonnet | borderline 14, not_relevant 16 |
| borderline | borderline | consensus | None 11, borderline 7, not_relevant 9, relevant 3 |
| borderline | borderline | majority | None 2, borderline 10, not_relevant 15, relevant 3 |
| not_relevant|c<=0.94 | not_relevant | opus | borderline 6, not_relevant 64 |
| not_relevant|c<=0.94 | not_relevant | sol | borderline 5, not_relevant 56, relevant 9 |
| not_relevant|c<=0.94 | not_relevant | sonnet | borderline 6, not_relevant 64 |
| not_relevant|c<=0.94 | not_relevant | consensus | None 18, not_relevant 52 |
| not_relevant|c<=0.94 | not_relevant | majority | None 1, borderline 4, not_relevant 65 |
| not_relevant|c0.95-0.98 | not_relevant | opus | not_relevant 40 |
| not_relevant|c0.95-0.98 | not_relevant | sol | borderline 1, not_relevant 38, relevant 1 |
| not_relevant|c0.95-0.98 | not_relevant | sonnet | not_relevant 40 |
| not_relevant|c0.95-0.98 | not_relevant | consensus | None 2, not_relevant 38 |
| not_relevant|c0.95-0.98 | not_relevant | majority | not_relevant 40 |
| not_relevant|c0.99 | not_relevant | opus | not_relevant 30 |
| not_relevant|c0.99 | not_relevant | sol | not_relevant 30 |
| not_relevant|c0.99 | not_relevant | sonnet | not_relevant 30 |
| not_relevant|c0.99 | not_relevant | consensus | not_relevant 30 |
| not_relevant|c0.99 | not_relevant | majority | not_relevant 30 |

jtri items by the same strata (reference = majority): relevant: None 1, borderline 2, not_relevant 1, relevant 2; borderline: borderline 1, relevant 2; not_relevant|c<=0.94: not_relevant 9; not_relevant|c0.95-0.98: not_relevant 38; not_relevant|c0.99: not_relevant 44

## Missed relevant|borderline per not_relevant stratum (Wilson 95 % per stratum, scaled to N_h)

| reference | sample | stratum | keep / scored | est. missed [Wilson 95 %] |
|---|---|---|---|---|
| consensus | triage_w | not_relevant|c<=0.94 | 0 / 52 | 0 [0, 1,213] |
| consensus | pooled | not_relevant|c<=0.94 | 0 / 60 | 0 [0, 1,061] |
| consensus | triage_w | not_relevant|c0.95-0.98 | 0 / 38 | 0 [0, 4,573] |
| consensus | pooled | not_relevant|c0.95-0.98 | 0 / 75 | 0 [0, 2,429] |
| consensus | triage_w | not_relevant|c0.99 | 0 / 30 | 0 [0, 7,705] |
| consensus | pooled | not_relevant|c0.99 | 0 / 74 | 0 [0, 3,352] |
| majority | triage_w | not_relevant|c<=0.94 | 4 / 69 | 1022 [401, 2,465] |
| majority | pooled | not_relevant|c<=0.94 | 4 / 78 | 905 [355, 2,197] |
| majority | triage_w | not_relevant|c0.95-0.98 | 0 / 40 | 0 [0, 4,364] |
| majority | pooled | not_relevant|c0.95-0.98 | 0 / 78 | 0 [0, 2,340] |
| majority | triage_w | not_relevant|c0.99 | 0 / 30 | 0 [0, 7,705] |
| majority | pooled | not_relevant|c0.99 | 0 / 74 | 0 [0, 3,352] |

## Judge agreement on triage_w

| pair | n | raw agreement | weighted agreement |
|---|---|---|---|
| opus_vs_sol | 200 | 0.785 | 0.917 [0.884, 0.943] |
| opus_vs_sonnet | 200 | 0.895 | 0.967 [0.954, 0.979] |
| sol_vs_sonnet | 200 | 0.755 | 0.906 [0.873, 0.931] |

## Production not_relevant, majority relevant|borderline (4 items)

| item | stratum | conf | opus / sol / sonnet | title |
|---|---|---|---|---|
| jtrw-0026 | not_relevant|c<=0.94 | 0.92 | borderline / not_relevant / borderline | InnoGym: Benchmarking the Innovation Potential of AI Agents |
| jtrw-0153 | not_relevant|c<=0.94 | 0.91 | borderline / relevant / borderline | LLMCrit: Teaching Large Language Models to Use Criteria |
| jtrw-0186 | not_relevant|c<=0.94 | 0.84 | not_relevant / borderline / borderline | Position Paper: Data-Centric AI in the Age of Large Language Models |
| jtrw-0197 | not_relevant|c<=0.94 | 0.91 | borderline / relevant / borderline | Social interaction for efficient agent learning from human reward |

Disagreement rows (triage_w, production and judges not all equal): 65 (disagreements.jsonl).
