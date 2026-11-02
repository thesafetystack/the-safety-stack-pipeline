# Judge eval scores: screen (2026-10-25j)

100 items. Judge records: opus: {"quality:valid": 100, "relevance:over_length": 1, "relevance:repaired": 2, "relevance:valid": 97}; sol: {"quality:valid": 100, "relevance:valid": 100}; sonnet: {"quality:valid": 100, "relevance:valid": 100}

Production is the prediction, the judge (or the reference label set) the reference. Majority = items (categorical) or units (sets) on which at least 2 of the 3 judges (Opus, Sol, Sonnet) agree. Consensus = items (categorical) or units (sets) on which Opus and Sol agree (the 2026-10-25 two-judge rule; Sonnet ignored).

## Headline: production vs majority (2 of 3) and vs consensus (Opus and Sol)

| judgement | n maj | agreement maj | kappa maj | n cons | agreement cons | kappa cons | no majority |
|---|---|---|---|---|---|---|---|
| overall verdict | 98 | 0.755 | 0.596 | 76 | 0.895 | 0.805 | 2 |
| S (stack) verdict | 100 | 0.780 | 0.632 | 71 | 0.972 | 0.944 | 0 |
| R (shallow review) verdict | 95 | 0.800 | 0.658 | 71 | 0.915 | 0.840 | 5 |
| overall keep (relevant\|borderline vs irrelevant) | 100 | 0.910 | 0.816 | 89 | 0.966 | 0.929 | 0 |
| R category (where R is relevant or borderline) | 45 | 0.889 | 0.854 | 38 | 0.921 | 0.893 | 1 |
| usability verdict | 100 | 0.660 | 0.221 | 71 | 0.704 | 0.198 | 0 |
| doc_type (model judgements only) | 57 | 1.000 | 1.000 | 57 | 1.000 | 1.000 | 0 |
| self_published | 100 | 0.910 | 0.560 | 96 | 0.938 | 0.665 | 0 |
| primary_lang | 100 | 1.000 | n/a | 100 | 1.000 | n/a | 0 |

| units | items | P / R / F1 vs majority | majority units | P / R / F1 vs consensus | consensus units |
|---|---|---|---|---|---|
| usability issues (codes) | 100 | 0.520 / 0.245 / 0.333 | 53 | 0.400 / 0.227 / 0.290 | 44 |

Per-class P / R / F1 for each categorical judgement are in its section below.

## overall verdict

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.750 | 0.594 |
| production_vs_sol | 100 | 0.840 | 0.708 |
| production_vs_sonnet | 100 | 0.700 | 0.517 |
| opus_vs_sol | 100 | 0.760 | 0.617 |
| opus_vs_sonnet | 100 | 0.860 | 0.783 |
| sol_vs_sonnet | 100 | 0.720 | 0.555 |
| production_vs_consensus | 76 | 0.895 | 0.805 |
| production_vs_majority | 98 | 0.755 | 0.596 |

Items with at least two judge labels but no majority (1-1-1): 2.

production_vs_majority (production rows, majority columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 2 | 0 |
| **irrelevant** | 3 | 38 | 0 |
| **relevant** | 16 | 3 | 36 |

production_vs_consensus (production rows, consensus columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 1 | 0 |
| **irrelevant** | 0 | 33 | 0 |
| **relevant** | 5 | 2 | 35 |

production_vs_opus (production rows, opus columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 1 | 0 |
| **irrelevant** | 3 | 38 | 1 |
| **relevant** | 18 | 2 | 36 |

production_vs_sol (production rows, sol columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 2 | 0 |
| **irrelevant** | 3 | 37 | 2 |
| **relevant** | 6 | 3 | 47 |

production_vs_sonnet (production rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 2 | 0 |
| **irrelevant** | 5 | 37 | 0 |
| **relevant** | 17 | 6 | 33 |

opus_vs_sol (opus rows, sol columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 5 | 5 | 12 |
| **irrelevant** | 3 | 36 | 2 |
| **relevant** | 1 | 1 | 35 |

opus_vs_sonnet (opus rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 15 | 6 | 1 |
| **irrelevant** | 2 | 39 | 0 |
| **relevant** | 5 | 0 | 32 |

sol_vs_sonnet (sol rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 3 | 4 | 2 |
| **irrelevant** | 4 | 38 | 0 |
| **relevant** | 15 | 3 | 31 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.000 | 0.000 | 0.000 | 2 | 19 |
| irrelevant | 0.927 | 0.884 | 0.905 | 41 | 43 |
| relevant | 0.654 | 1.000 | 0.791 | 55 | 36 |

Confusion (production -> majority): borderline->irrelevant: 2, irrelevant->borderline: 3, irrelevant->irrelevant: 38, relevant->borderline: 16, relevant->irrelevant: 3, relevant->relevant: 36

By window (majority): excerpt_seen: n 66, agreement 0.742, kappa 0.581; whole_text_seen: n 32, agreement 0.781, kappa 0.633

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.000 | 0.000 | 0.000 | 1 | 5 |
| irrelevant | 1.000 | 0.917 | 0.957 | 33 | 36 |
| relevant | 0.833 | 1.000 | 0.909 | 42 | 35 |

Confusion (production -> consensus): borderline->irrelevant: 1, irrelevant->irrelevant: 33, relevant->borderline: 5, relevant->irrelevant: 2, relevant->relevant: 35

By window (consensus): excerpt_seen: n 51, agreement 0.882, kappa 0.782; whole_text_seen: n 25, agreement 0.920, kappa 0.851

## S (stack) verdict

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.780 | 0.632 |
| production_vs_sol | 100 | 0.840 | 0.718 |
| production_vs_sonnet | 100 | 0.770 | 0.622 |
| opus_vs_sol | 100 | 0.710 | 0.527 |
| opus_vs_sonnet | 100 | 0.930 | 0.884 |
| sol_vs_sonnet | 100 | 0.720 | 0.551 |
| production_vs_consensus | 71 | 0.972 | 0.944 |
| production_vs_majority | 100 | 0.780 | 0.632 |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 3 | 3 | 0 |
| **irrelevant** | 3 | 48 | 0 |
| **relevant** | 13 | 3 | 27 |

production_vs_consensus (production rows, consensus columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 1 | 0 |
| **irrelevant** | 0 | 41 | 0 |
| **relevant** | 0 | 1 | 27 |

production_vs_opus (production rows, opus columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 3 | 3 | 0 |
| **irrelevant** | 3 | 48 | 0 |
| **relevant** | 13 | 3 | 27 |

production_vs_sol (production rows, sol columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 2 | 1 | 3 |
| **irrelevant** | 5 | 43 | 3 |
| **relevant** | 2 | 2 | 39 |

production_vs_sonnet (production rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 4 | 2 | 0 |
| **irrelevant** | 3 | 48 | 0 |
| **relevant** | 15 | 3 | 25 |

opus_vs_sol (opus rows, sol columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 3 | 15 |
| **irrelevant** | 8 | 43 | 3 |
| **relevant** | 0 | 0 | 27 |

opus_vs_sonnet (opus rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 17 | 2 | 0 |
| **irrelevant** | 3 | 51 | 0 |
| **relevant** | 2 | 0 | 25 |

sol_vs_sonnet (sol rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 3 | 6 | 0 |
| **irrelevant** | 2 | 44 | 0 |
| **relevant** | 17 | 3 | 25 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.500 | 0.158 | 0.240 | 6 | 19 |
| irrelevant | 0.941 | 0.889 | 0.914 | 51 | 54 |
| relevant | 0.628 | 1.000 | 0.771 | 43 | 27 |

Confusion (production -> majority): borderline->borderline: 3, borderline->irrelevant: 3, irrelevant->borderline: 3, irrelevant->irrelevant: 48, relevant->borderline: 13, relevant->irrelevant: 3, relevant->relevant: 27

By window (majority): excerpt_seen: n 67, agreement 0.746, kappa 0.582; whole_text_seen: n 33, agreement 0.849, kappa 0.735

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.500 | 1.000 | 0.667 | 2 | 1 |
| irrelevant | 1.000 | 0.954 | 0.976 | 41 | 43 |
| relevant | 0.964 | 1.000 | 0.982 | 28 | 27 |

Confusion (production -> consensus): borderline->borderline: 1, borderline->irrelevant: 1, irrelevant->irrelevant: 41, relevant->irrelevant: 1, relevant->relevant: 27

By window (consensus): excerpt_seen: n 44, agreement 0.977, kappa 0.954; whole_text_seen: n 27, agreement 0.963, kappa 0.924

## R (shallow review) verdict

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.760 | 0.618 |
| production_vs_sol | 100 | 0.790 | 0.623 |
| production_vs_sonnet | 100 | 0.730 | 0.550 |
| opus_vs_sol | 100 | 0.710 | 0.539 |
| opus_vs_sonnet | 100 | 0.840 | 0.747 |
| sol_vs_sonnet | 100 | 0.700 | 0.502 |
| production_vs_consensus | 71 | 0.915 | 0.840 |
| production_vs_majority | 95 | 0.800 | 0.658 |

Items with at least two judge labels but no majority (1-1-1): 5.

production_vs_majority (production rows, majority columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 2 | 0 |
| **irrelevant** | 4 | 45 | 0 |
| **relevant** | 11 | 2 | 30 |

production_vs_consensus (production rows, consensus columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 1 | 0 |
| **irrelevant** | 0 | 37 | 0 |
| **relevant** | 4 | 1 | 28 |

production_vs_opus (production rows, opus columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 4 | 1 | 0 |
| **irrelevant** | 7 | 42 | 1 |
| **relevant** | 14 | 1 | 30 |

production_vs_sol (production rows, sol columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 2 | 3 |
| **irrelevant** | 2 | 44 | 4 |
| **relevant** | 6 | 4 | 35 |

production_vs_sonnet (production rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 4 | 0 |
| **irrelevant** | 6 | 44 | 0 |
| **relevant** | 12 | 5 | 28 |

opus_vs_sol (opus rows, sol columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 4 | 10 | 11 |
| **irrelevant** | 2 | 39 | 3 |
| **relevant** | 2 | 1 | 28 |

opus_vs_sonnet (opus rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 14 | 10 | 1 |
| **irrelevant** | 1 | 43 | 0 |
| **relevant** | 4 | 0 | 27 |

sol_vs_sonnet (sol rows, sonnet columns):

| | borderline | irrelevant | relevant |
|---|---|---|---|
| **borderline** | 2 | 3 | 3 |
| **irrelevant** | 7 | 43 | 0 |
| **relevant** | 10 | 7 | 25 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.333 | 0.062 | 0.105 | 3 | 16 |
| irrelevant | 0.918 | 0.918 | 0.918 | 49 | 49 |
| relevant | 0.698 | 1.000 | 0.822 | 43 | 30 |

Confusion (production -> majority): borderline->borderline: 1, borderline->irrelevant: 2, irrelevant->borderline: 4, irrelevant->irrelevant: 45, relevant->borderline: 11, relevant->irrelevant: 2, relevant->relevant: 30

By window (majority): excerpt_seen: n 63, agreement 0.794, kappa 0.639; whole_text_seen: n 32, agreement 0.812, kappa 0.689

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.000 | 0.000 | 0.000 | 1 | 4 |
| irrelevant | 1.000 | 0.949 | 0.974 | 37 | 39 |
| relevant | 0.849 | 1.000 | 0.918 | 33 | 28 |

Confusion (production -> consensus): borderline->irrelevant: 1, irrelevant->irrelevant: 37, relevant->borderline: 4, relevant->irrelevant: 1, relevant->relevant: 28

By window (consensus): excerpt_seen: n 45, agreement 0.933, kappa 0.870; whole_text_seen: n 26, agreement 0.885, kappa 0.792

## overall keep (relevant|borderline vs irrelevant)

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.930 | 0.856 |
| production_vs_sol | 100 | 0.900 | 0.795 |
| production_vs_sonnet | 100 | 0.870 | 0.736 |
| opus_vs_sol | 100 | 0.890 | 0.773 |
| opus_vs_sonnet | 100 | 0.920 | 0.837 |
| sol_vs_sonnet | 100 | 0.890 | 0.776 |
| production_vs_consensus | 89 | 0.966 | 0.929 |
| production_vs_majority | 100 | 0.910 | 0.816 |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | drop | keep |
|---|---|---|
| **drop** | 38 | 4 |
| **keep** | 5 | 53 |

production_vs_consensus (production rows, consensus columns):

| | drop | keep |
|---|---|---|
| **drop** | 33 | 0 |
| **keep** | 3 | 53 |

production_vs_opus (production rows, opus columns):

| | drop | keep |
|---|---|---|
| **drop** | 38 | 4 |
| **keep** | 3 | 55 |

production_vs_sol (production rows, sol columns):

| | drop | keep |
|---|---|---|
| **drop** | 37 | 5 |
| **keep** | 5 | 53 |

production_vs_sonnet (production rows, sonnet columns):

| | drop | keep |
|---|---|---|
| **drop** | 37 | 5 |
| **keep** | 8 | 50 |

opus_vs_sol (opus rows, sol columns):

| | drop | keep |
|---|---|---|
| **drop** | 36 | 5 |
| **keep** | 6 | 53 |

opus_vs_sonnet (opus rows, sonnet columns):

| | drop | keep |
|---|---|---|
| **drop** | 39 | 2 |
| **keep** | 6 | 53 |

sol_vs_sonnet (sol rows, sonnet columns):

| | drop | keep |
|---|---|---|
| **drop** | 38 | 4 |
| **keep** | 7 | 51 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| drop | 0.905 [0.809, 0.978] | 0.884 [0.780, 0.974] | 0.894 [0.820, 0.955] | 42 | 43 |
| keep | 0.914 [0.837, 0.982] | 0.930 [0.860, 0.984] | 0.922 [0.866, 0.967] | 58 | 57 |

Confusion (production -> majority): drop->drop: 38, drop->keep: 4, keep->drop: 5, keep->keep: 53

By window (majority): excerpt_seen: n 67, agreement 0.940, kappa 0.877; whole_text_seen: n 33, agreement 0.849, kappa 0.693

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| drop | 1.000 [1.000, 1.000] | 0.917 [0.812, 1.000] | 0.957 [0.897, 1.000] | 33 | 36 |
| keep | 0.946 [0.879, 1.000] | 1.000 [1.000, 1.000] | 0.973 [0.936, 1.000] | 56 | 53 |

Confusion (production -> consensus): drop->drop: 33, keep->drop: 3, keep->keep: 53

By window (consensus): excerpt_seen: n 61, agreement 0.967, kappa 0.930; whole_text_seen: n 28, agreement 0.964, kappa 0.926

## R category (where R is relevant or borderline)

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 48 | 0.854 | 0.807 |
| production_vs_sol | 44 | 0.818 | 0.759 |
| production_vs_sonnet | 41 | 0.902 | 0.873 |
| opus_vs_sol | 45 | 0.867 | 0.824 |
| opus_vs_sonnet | 46 | 0.978 | 0.972 |
| sol_vs_sonnet | 40 | 0.900 | 0.868 |
| production_vs_consensus | 38 | 0.921 | 0.893 |
| production_vs_majority | 45 | 0.889 | 0.854 |

Items with at least two judge labels but no majority (1-1-1): 1.

production_vs_majority (production rows, majority columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 14 | 0 | 0 | 0 | 0 | 2 |
| **evals** | 2 | 11 | 0 | 0 | 0 | 0 |
| **labs** | 0 | 0 | 2 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 2 | 0 | 0 |
| **theory** | 0 | 0 | 0 | 1 | 7 | 0 |
| **white_box** | 0 | 0 | 0 | 0 | 0 | 4 |

production_vs_consensus (production rows, consensus columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 13 | 0 | 0 | 0 | 0 | 2 |
| **evals** | 1 | 10 | 0 | 0 | 0 | 0 |
| **labs** | 0 | 0 | 1 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 2 | 0 | 0 |
| **theory** | 0 | 0 | 0 | 0 | 5 | 0 |
| **white_box** | 0 | 0 | 0 | 0 | 0 | 4 |

production_vs_opus (production rows, opus columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 16 | 0 | 0 | 0 | 0 | 2 |
| **evals** | 2 | 11 | 0 | 0 | 0 | 0 |
| **labs** | 0 | 0 | 2 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 2 | 0 | 0 |
| **theory** | 0 | 0 | 0 | 2 | 6 | 0 |
| **white_box** | 0 | 0 | 0 | 0 | 1 | 4 |

production_vs_sol (production rows, sol columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 13 | 0 | 0 | 0 | 0 | 3 |
| **evals** | 2 | 10 | 0 | 0 | 0 | 0 |
| **labs** | 1 | 0 | 1 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 2 | 0 | 0 |
| **theory** | 1 | 0 | 0 | 0 | 6 | 1 |
| **white_box** | 0 | 0 | 0 | 0 | 0 | 4 |

production_vs_sonnet (production rows, sonnet columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 13 | 0 | 0 | 0 | 0 | 1 |
| **evals** | 2 | 9 | 0 | 0 | 0 | 0 |
| **labs** | 0 | 0 | 2 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 2 | 0 | 0 |
| **theory** | 0 | 0 | 0 | 1 | 7 | 0 |
| **white_box** | 0 | 0 | 0 | 0 | 0 | 4 |

opus_vs_sol (opus rows, sol columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 15 | 0 | 0 | 0 | 0 | 1 |
| **evals** | 1 | 10 | 0 | 0 | 0 | 0 |
| **labs** | 1 | 0 | 1 | 0 | 0 | 0 |
| **multi_agent_first** | 1 | 0 | 0 | 2 | 1 | 0 |
| **theory** | 0 | 0 | 0 | 0 | 5 | 1 |
| **white_box** | 0 | 0 | 0 | 0 | 0 | 6 |

opus_vs_sonnet (opus rows, sonnet columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 17 | 0 | 0 | 0 | 0 | 0 |
| **evals** | 0 | 10 | 0 | 0 | 0 | 0 |
| **labs** | 0 | 0 | 2 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 4 | 1 | 0 |
| **theory** | 0 | 0 | 0 | 0 | 6 | 0 |
| **white_box** | 0 | 0 | 0 | 0 | 0 | 6 |

sol_vs_sonnet (sol rows, sonnet columns):

| | black_box | evals | labs | multi_agent_first | theory | white_box |
|---|---|---|---|---|---|---|
| **black_box** | 14 | 1 | 1 | 1 | 0 | 0 |
| **evals** | 0 | 8 | 0 | 0 | 0 | 0 |
| **labs** | 0 | 0 | 1 | 0 | 0 | 0 |
| **multi_agent_first** | 0 | 0 | 0 | 2 | 0 | 0 |
| **theory** | 0 | 0 | 0 | 0 | 6 | 0 |
| **white_box** | 0 | 0 | 0 | 0 | 1 | 5 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| black_box | 0.875 | 0.875 | 0.875 | 16 | 16 |
| evals | 0.846 | 1.000 | 0.917 | 13 | 11 |
| labs | 1.000 | 1.000 | 1.000 | 2 | 2 |
| multi_agent_first | 1.000 | 0.667 | 0.800 | 2 | 3 |
| theory | 0.875 | 1.000 | 0.933 | 8 | 7 |
| white_box | 1.000 | 0.667 | 0.800 | 4 | 6 |

Confusion (production -> majority): black_box->black_box: 14, black_box->white_box: 2, evals->black_box: 2, evals->evals: 11, labs->labs: 2, multi_agent_first->multi_agent_first: 2, theory->multi_agent_first: 1, theory->theory: 7, white_box->white_box: 4

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| black_box | 0.867 | 0.929 | 0.897 | 15 | 14 |
| evals | 0.909 | 1.000 | 0.952 | 11 | 10 |
| labs | 1.000 | 1.000 | 1.000 | 1 | 1 |
| multi_agent_first | 1.000 | 1.000 | 1.000 | 2 | 2 |
| theory | 1.000 | 1.000 | 1.000 | 5 | 5 |
| white_box | 1.000 | 0.667 | 0.800 | 4 | 6 |

Confusion (production -> consensus): black_box->black_box: 13, black_box->white_box: 2, evals->black_box: 1, evals->evals: 10, labs->labs: 1, multi_agent_first->multi_agent_first: 2, theory->theory: 5, white_box->white_box: 4

## usability verdict

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.690 | 0.087 |
| production_vs_sol | 100 | 0.600 | 0.208 |
| production_vs_sonnet | 100 | 0.630 | 0.208 |
| opus_vs_sol | 100 | 0.710 | 0.420 |
| opus_vs_sonnet | 100 | 0.780 | 0.523 |
| sol_vs_sonnet | 100 | 0.830 | 0.663 |
| production_vs_consensus | 71 | 0.704 | 0.198 |
| production_vs_majority | 100 | 0.660 | 0.221 |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 13 | 0 | 8 |
| **unusable** | 0 | 0 | 1 |
| **usable** | 25 | 0 | 53 |

production_vs_consensus (production rows, consensus columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 6 | 0 | 5 |
| **unusable** | 0 | 0 | 1 |
| **usable** | 15 | 0 | 44 |

production_vs_opus (production rows, opus columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 6 | 0 | 15 |
| **unusable** | 0 | 0 | 1 |
| **usable** | 15 | 0 | 63 |

production_vs_sol (production rows, sol columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 16 | 0 | 5 |
| **unusable** | 0 | 0 | 1 |
| **usable** | 34 | 0 | 44 |

production_vs_sonnet (production rows, sonnet columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 13 | 0 | 8 |
| **unusable** | 0 | 1 | 0 |
| **usable** | 29 | 0 | 49 |

opus_vs_sol (opus rows, sol columns):

| | degraded | usable |
|---|---|---|
| **degraded** | 21 | 0 |
| **usable** | 29 | 50 |

opus_vs_sonnet (opus rows, sonnet columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 21 | 0 | 0 |
| **unusable** | 0 | 0 | 0 |
| **usable** | 21 | 1 | 57 |

sol_vs_sonnet (sol rows, sonnet columns):

| | degraded | unusable | usable |
|---|---|---|---|
| **degraded** | 38 | 0 | 12 |
| **unusable** | 0 | 0 | 0 |
| **usable** | 4 | 1 | 45 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| degraded | 0.619 | 0.342 | 0.441 | 21 | 38 |
| unusable | 0.000 | n/a | n/a | 1 | 0 |
| usable | 0.679 | 0.855 | 0.757 | 78 | 62 |

Confusion (production -> majority): degraded->degraded: 13, degraded->usable: 8, unusable->usable: 1, usable->degraded: 25, usable->usable: 53

By tier1_window (majority): excerpt_seen: n 68, agreement 0.618, kappa 0.191; whole_text_seen: n 32, agreement 0.750, kappa 0.220

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| degraded | 0.545 | 0.286 | 0.375 | 11 | 21 |
| unusable | 0.000 | n/a | n/a | 1 | 0 |
| usable | 0.746 | 0.880 | 0.807 | 59 | 50 |

Confusion (production -> consensus): degraded->degraded: 6, degraded->usable: 5, unusable->usable: 1, usable->degraded: 15, usable->usable: 44

By tier1_window (consensus): excerpt_seen: n 41, agreement 0.683, kappa 0.184; whole_text_seen: n 30, agreement 0.733, kappa 0.210

## doc_type (model judgements only)

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 57 | 1.000 | 1.000 |
| production_vs_sol | 57 | 1.000 | 1.000 |
| production_vs_sonnet | 57 | 1.000 | 1.000 |
| opus_vs_sol | 57 | 1.000 | 1.000 |
| opus_vs_sonnet | 57 | 1.000 | 1.000 |
| sol_vs_sonnet | 57 | 1.000 | 1.000 |
| production_vs_consensus | 57 | 1.000 | 1.000 |
| production_vs_majority | 57 | 1.000 | 1.000 |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

production_vs_consensus (production rows, consensus columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

production_vs_opus (production rows, opus columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

production_vs_sol (production rows, sol columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

production_vs_sonnet (production rows, sonnet columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

opus_vs_sol (opus rows, sol columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

opus_vs_sonnet (opus rows, sonnet columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

sol_vs_sonnet (sol rows, sonnet columns):

| | paper | preprint |
|---|---|---|
| **paper** | 13 | 0 |
| **preprint** | 0 | 44 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| paper | 1.000 | 1.000 | 1.000 | 13 | 13 |
| preprint | 1.000 | 1.000 | 1.000 | 44 | 44 |

Confusion (production -> majority): paper->paper: 13, preprint->preprint: 44

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| paper | 1.000 | 1.000 | 1.000 | 13 | 13 |
| preprint | 1.000 | 1.000 | 1.000 | 44 | 44 |

Confusion (production -> consensus): paper->paper: 13, preprint->preprint: 44

Scored items (doc_type a model judgement): 57

## self_published

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.900 | 0.528 |
| production_vs_sol | 100 | 0.940 | 0.735 |
| production_vs_sonnet | 100 | 0.880 | 0.289 |
| opus_vs_sol | 100 | 0.960 | 0.796 |
| opus_vs_sonnet | 100 | 0.940 | 0.545 |
| sol_vs_sonnet | 100 | 0.920 | 0.468 |
| production_vs_consensus | 96 | 0.938 | 0.665 |
| production_vs_majority | 100 | 0.910 | 0.560 |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | false | true |
|---|---|---|
| **false** | 84 | 2 |
| **true** | 7 | 7 |

production_vs_consensus (production rows, consensus columns):

| | false | true |
|---|---|---|
| **false** | 83 | 2 |
| **true** | 4 | 7 |

production_vs_opus (production rows, opus columns):

| | false | true |
|---|---|---|
| **false** | 83 | 3 |
| **true** | 7 | 7 |

production_vs_sol (production rows, sol columns):

| | false | true |
|---|---|---|
| **false** | 84 | 2 |
| **true** | 4 | 10 |

production_vs_sonnet (production rows, sonnet columns):

| | false | true |
|---|---|---|
| **false** | 85 | 1 |
| **true** | 11 | 3 |

opus_vs_sol (opus rows, sol columns):

| | false | true |
|---|---|---|
| **false** | 87 | 3 |
| **true** | 1 | 9 |

opus_vs_sonnet (opus rows, sonnet columns):

| | false | true |
|---|---|---|
| **false** | 90 | 0 |
| **true** | 6 | 4 |

sol_vs_sonnet (sol rows, sonnet columns):

| | false | true |
|---|---|---|
| **false** | 88 | 0 |
| **true** | 8 | 4 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| false | 0.977 | 0.923 | 0.949 | 86 | 91 |
| true | 0.500 | 0.778 | 0.609 | 14 | 9 |

Confusion (production -> majority): false->false: 84, false->true: 2, true->false: 7, true->true: 7

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| false | 0.977 | 0.954 | 0.965 | 85 | 87 |
| true | 0.636 | 0.778 | 0.700 | 11 | 9 |

Confusion (production -> consensus): false->false: 83, false->true: 2, true->false: 4, true->true: 7

## primary_lang

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 1.000 | n/a |
| production_vs_sol | 100 | 1.000 | n/a |
| production_vs_sonnet | 100 | 1.000 | n/a |
| opus_vs_sol | 100 | 1.000 | n/a |
| opus_vs_sonnet | 100 | 1.000 | n/a |
| sol_vs_sonnet | 100 | 1.000 | n/a |
| production_vs_consensus | 100 | 1.000 | n/a |
| production_vs_majority | 100 | 1.000 | n/a |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | en |
|---|---|
| **en** | 100 |

production_vs_consensus (production rows, consensus columns):

| | en |
|---|---|
| **en** | 100 |

production_vs_opus (production rows, opus columns):

| | en |
|---|---|
| **en** | 100 |

production_vs_sol (production rows, sol columns):

| | en |
|---|---|
| **en** | 100 |

production_vs_sonnet (production rows, sonnet columns):

| | en |
|---|---|
| **en** | 100 |

opus_vs_sol (opus rows, sol columns):

| | en |
|---|---|
| **en** | 100 |

opus_vs_sonnet (opus rows, sonnet columns):

| | en |
|---|---|
| **en** | 100 |

sol_vs_sonnet (sol rows, sonnet columns):

| | en |
|---|---|
| **en** | 100 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| en | 1.000 | 1.000 | 1.000 | 100 | 100 |

Confusion (production -> majority): en->en: 100

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| en | 1.000 | 1.000 | 1.000 | 100 | 100 |

Confusion (production -> consensus): en->en: 100

## usability issues (codes)

| comparison | items | P [95% CI] | R [95% CI] | F1 [95% CI] | Jaccard | tp / fp / fn |
|---|---|---|---|---|---|---|
| production_vs_opus | 100 | 0.400 [0.231, 0.571] | 0.200 [0.104, 0.309] | 0.267 [0.147, 0.387] | 0.154 | 10 / 15 / 40 |
| production_vs_sol | 100 | 0.560 [0.367, 0.760] | 0.197 [0.113, 0.286] | 0.292 [0.176, 0.403] | 0.171 | 14 / 11 / 57 |
| production_vs_sonnet | 100 | 0.520 [0.323, 0.731] | 0.232 [0.127, 0.345] | 0.321 [0.189, 0.450] | 0.191 | 13 / 12 / 43 |
| opus_vs_sol | 100 | 0.880 [0.787, 0.960] | 0.620 [0.515, 0.721] | 0.727 [0.637, 0.807] | 0.571 | 44 / 6 / 27 |
| opus_vs_sonnet | 100 | 0.800 [0.694, 0.895] | 0.714 [0.617, 0.812] | 0.755 [0.673, 0.821] | 0.606 | 40 / 10 / 16 |
| sol_vs_sonnet | 100 | 0.606 [0.486, 0.730] | 0.768 [0.667, 0.857] | 0.677 [0.578, 0.769] | 0.512 | 43 / 28 / 13 |
| production_vs_consensus | 100 | 0.400 [0.231, 0.571] | 0.227 [0.118, 0.357] | 0.290 [0.159, 0.422] | 0.170 | 10 / 15 / 34 |
| production_vs_majority | 100 | 0.520 [0.333, 0.706] | 0.245 [0.140, 0.364] | 0.333 [0.200, 0.463] | 0.200 | 13 / 12 / 40 |

Consensus units (Opus and Sol both have): 44; production's precision against the union (Opus | Sol): 0.560 (25 production units).

Majority units (at least 2 of the 3 judges have): 53; production's precision against the union (Opus | Sol | Sonnet): 0.600 (25 production units).

## Disagreements (163)

- {"item_id": "jscr-0007", "judgement": "overall verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Rescuing the Unpoisoned: Efficient Defense Against Knowledge Corruption Attacks on RAG Sys"}
- {"item_id": "jscr-0008", "judgement": "overall verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Applying the Counterfactual Prisoner's Dilemma to Logical Uncertainty"}
- {"item_id": "jscr-0011", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Dependency-Aware Privacy for Multi-turn LLM Agents"}
- {"item_id": "jscr-0012", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "An Introduction to Machine Unlearning"}
- {"item_id": "jscr-0013", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "FCMBench-Video: Benchmarking Document Video Intelligence"}
- {"item_id": "jscr-0015", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Trust in One Round: Confidence Estimation for Large Language Models via Structural Signals"}
- {"item_id": "jscr-0016", "judgement": "overall verdict", "production": "relevant", "opus": "relevant", "sol": "borderline", "sonnet": "relevant", "majority": "relevant", "title": "Toward Interoperability of Minimal Programs"}
- {"item_id": "jscr-0024", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "irrelevant", "majority": "borderline", "title": "Subjective Evaluation Profile Analysis of Science Fiction Short Stories and its Critical-T"}
- {"item_id": "jscr-0026", "judgement": "overall verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Online Learning of non-Markovian Reward Models"}
- {"item_id": "jscr-0030", "judgement": "overall verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "Welcome to the decade of Em"}
- {"item_id": "jscr-0034", "judgement": "overall verdict", "production": "relevant", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Masked Jigsaw Puzzle: A Versatile Position Embedding for Vision Transformers"}
- {"item_id": "jscr-0038", "judgement": "overall verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Uneven Evolution of Cognition Across Generations of Generative AI Models"}
- {"item_id": "jscr-0042", "judgement": "overall verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Token Constraint Decoding Improves Robustness on Question Answering for Large Language Mod"}
- {"item_id": "jscr-0043", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "irrelevant", "majority": "borderline", "title": "PeerRank: Autonomous LLM Evaluation Through Web-Grounded, Bias-Controlled Peer Review"}
- {"item_id": "jscr-0044", "judgement": "overall verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "borderline", "majority": "irrelevant", "title": "On expected utility, part 4: Dutch books, Cox, and Complete Class"}
- {"item_id": "jscr-0048", "judgement": "overall verdict", "production": "borderline", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Alien neuropunk slaver civilizations"}
- {"item_id": "jscr-0051", "judgement": "overall verdict", "production": "borderline", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Causal Abstraction Intro"}
- {"item_id": "jscr-0058", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Facts Grounding A New Benchmark For Evaluating The Factuality Of Large Language Models"}
- {"item_id": "jscr-0059", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Fine-Grained Detection of Context-Grounded Hallucinations Using LLMs"}
- {"item_id": "jscr-0060", "judgement": "overall verdict", "production": "relevant", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Compositional Reasoning in Language Models under Reinforcement Learning Post-Training"}
- {"item_id": "jscr-0063", "judgement": "overall verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Formal Verification and Control with Conformal Prediction"}
- {"item_id": "jscr-0065", "judgement": "overall verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "2024 in AI predictions"}
- {"item_id": "jscr-0066", "judgement": "overall verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Your Language Model is Its Own Critic: Reinforcement Learning with Value Estimation from A"}
- {"item_id": "jscr-0067", "judgement": "overall verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Machine Unlearning via Algorithmic Stability"}
- {"item_id": "jscr-0069", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Memory Inception: Latent-Space KV Cache Manipulation for Steering LLMs"}
- {"item_id": "jscr-0074", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "irrelevant", "majority": null, "title": "Sci$Π$vot: Benchmarking Principal-Investigator Judgment at Pivotal Moments of Scientific A"}
- {"item_id": "jscr-0078", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "relevant", "majority": "borderline", "title": "New Paper Racing To The Precipice"}
- {"item_id": "jscr-0080", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Prototypical Human-AI Collaboration Behaviors from LLM-Assisted Writing in the Wild"}
- {"item_id": "jscr-0082", "judgement": "overall verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Assisted Robust Reward Design"}
- {"item_id": "jscr-0083", "judgement": "overall verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "Adaptive Workflow Intelligence: A Cognitive Architecture for Context-Driven Enterprise Aut"}
- {"item_id": "jscr-0086", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "Tracing the Traces: Latent Temporal Signals for Efficient and Accurate Reasoning"}
- {"item_id": "jscr-0087", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Bending The Curve"}
- {"item_id": "jscr-0088", "judgement": "overall verdict", "production": "irrelevant", "opus": "relevant", "sol": "irrelevant", "sonnet": "borderline", "majority": null, "title": "Spooky action at a distance in the loss landscape"}
- {"item_id": "jscr-0095", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Adaptive Segment-level Reward: Bridging the Gap Between Action and Reward Space in Alignme"}
- {"item_id": "jscr-0096", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Can LLMs get help from other LLMs without revealing private information?"}
- {"item_id": "jscr-0099", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "After Overmorrow: Scattered Musings on the Immediate Post-AGI World"}
- {"item_id": "jscr-0100", "judgement": "overall verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Ten years"}
- {"item_id": "jscr-0007", "judgement": "S (stack) verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Rescuing the Unpoisoned: Efficient Defense Against Knowledge Corruption Attacks on RAG Sys"}
- {"item_id": "jscr-0010", "judgement": "S (stack) verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Finding Emergent Misalignment"}
- {"item_id": "jscr-0011", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Dependency-Aware Privacy for Multi-turn LLM Agents"}
- {"item_id": "jscr-0012", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "An Introduction to Machine Unlearning"}
- {"item_id": "jscr-0013", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "FCMBench-Video: Benchmarking Document Video Intelligence"}
- {"item_id": "jscr-0015", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Trust in One Round: Confidence Estimation for Large Language Models via Structural Signals"}
- {"item_id": "jscr-0018", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "A framework for thinking about AI power-seeking"}
- {"item_id": "jscr-0024", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Subjective Evaluation Profile Analysis of Science Fiction Short Stories and its Critical-T"}
- {"item_id": "jscr-0026", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Online Learning of non-Markovian Reward Models"}
- {"item_id": "jscr-0034", "judgement": "S (stack) verdict", "production": "relevant", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Masked Jigsaw Puzzle: A Versatile Position Embedding for Vision Transformers"}
- {"item_id": "jscr-0042", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Token Constraint Decoding Improves Robustness on Question Answering for Large Language Mod"}
- {"item_id": "jscr-0043", "judgement": "S (stack) verdict", "production": "relevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "PeerRank: Autonomous LLM Evaluation Through Web-Grounded, Bias-Controlled Peer Review"}
- {"item_id": "jscr-0058", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Facts Grounding A New Benchmark For Evaluating The Factuality Of Large Language Models"}
- {"item_id": "jscr-0059", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Fine-Grained Detection of Context-Grounded Hallucinations Using LLMs"}
- {"item_id": "jscr-0061", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Safe AI and moral AI"}
- {"item_id": "jscr-0063", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Formal Verification and Control with Conformal Prediction"}
- {"item_id": "jscr-0065", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "2024 in AI predictions"}
- {"item_id": "jscr-0066", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Your Language Model is Its Own Critic: Reinforcement Learning with Value Estimation from A"}
- {"item_id": "jscr-0067", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Machine Unlearning via Algorithmic Stability"}
- {"item_id": "jscr-0069", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Memory Inception: Latent-Space KV Cache Manipulation for Steering LLMs"}
- {"item_id": "jscr-0074", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Sci$Π$vot: Benchmarking Principal-Investigator Judgment at Pivotal Moments of Scientific A"}
- {"item_id": "jscr-0076", "judgement": "S (stack) verdict", "production": "borderline", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Moltbook Data Repository"}
- {"item_id": "jscr-0078", "judgement": "S (stack) verdict", "production": "borderline", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "borderline", "majority": "irrelevant", "title": "New Paper Racing To The Precipice"}
- {"item_id": "jscr-0080", "judgement": "S (stack) verdict", "production": "borderline", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Prototypical Human-AI Collaboration Behaviors from LLM-Assisted Writing in the Wild"}
- {"item_id": "jscr-0082", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Assisted Robust Reward Design"}
- {"item_id": "jscr-0083", "judgement": "S (stack) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "Adaptive Workflow Intelligence: A Cognitive Architecture for Context-Driven Enterprise Aut"}
- {"item_id": "jscr-0085", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Why Agent Foundations? An Overly Abstract Explanation"}
- {"item_id": "jscr-0086", "judgement": "S (stack) verdict", "production": "relevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "Tracing the Traces: Latent Temporal Signals for Efficient and Accurate Reasoning"}
- {"item_id": "jscr-0087", "judgement": "S (stack) verdict", "production": "borderline", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Bending The Curve"}
- {"item_id": "jscr-0095", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Adaptive Segment-level Reward: Bridging the Gap Between Action and Reward Space in Alignme"}
- {"item_id": "jscr-0096", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Can LLMs get help from other LLMs without revealing private information?"}
- {"item_id": "jscr-0099", "judgement": "S (stack) verdict", "production": "borderline", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "After Overmorrow: Scattered Musings on the Immediate Post-AGI World"}
- {"item_id": "jscr-0100", "judgement": "S (stack) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Ten years"}
- {"item_id": "jscr-0003", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "Preventing Generation of Verbatim Memorization in Language Models Gives a False Sense of P"}
- {"item_id": "jscr-0007", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Rescuing the Unpoisoned: Efficient Defense Against Knowledge Corruption Attacks on RAG Sys"}
- {"item_id": "jscr-0008", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Applying the Counterfactual Prisoner's Dilemma to Logical Uncertainty"}
- {"item_id": "jscr-0011", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Dependency-Aware Privacy for Multi-turn LLM Agents"}
- {"item_id": "jscr-0012", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "An Introduction to Machine Unlearning"}
- {"item_id": "jscr-0013", "judgement": "R (shallow review) verdict", "production": "borderline", "opus": "borderline", "sol": "relevant", "sonnet": "irrelevant", "majority": null, "title": "FCMBench-Video: Benchmarking Document Video Intelligence"}
- {"item_id": "jscr-0014", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "What can we learn from Lex Fridman’s interview with Sam Altman?"}
- {"item_id": "jscr-0015", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "irrelevant", "majority": null, "title": "Trust in One Round: Confidence Estimation for Large Language Models via Structural Signals"}
- {"item_id": "jscr-0016", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "relevant", "sol": "borderline", "sonnet": "relevant", "majority": "relevant", "title": "Toward Interoperability of Minimal Programs"}
- {"item_id": "jscr-0020", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "relevant", "sol": "borderline", "sonnet": "relevant", "majority": "relevant", "title": "Reflections on Trusting Trust & AI"}
- {"item_id": "jscr-0024", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "irrelevant", "majority": "borderline", "title": "Subjective Evaluation Profile Analysis of Science Fiction Short Stories and its Critical-T"}
- {"item_id": "jscr-0025", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "EraseAnything++: Enabling Concept Erasure in Rectified Flow Transformers Leveraging Multi-"}
- {"item_id": "jscr-0026", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Online Learning of non-Markovian Reward Models"}
- {"item_id": "jscr-0030", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "Welcome to the decade of Em"}
- {"item_id": "jscr-0038", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Uneven Evolution of Cognition Across Generations of Generative AI Models"}
- {"item_id": "jscr-0043", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "PeerRank: Autonomous LLM Evaluation Through Web-Grounded, Bias-Controlled Peer Review"}
- {"item_id": "jscr-0044", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "borderline", "majority": "irrelevant", "title": "On expected utility, part 4: Dutch books, Cox, and Complete Class"}
- {"item_id": "jscr-0048", "judgement": "R (shallow review) verdict", "production": "borderline", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Alien neuropunk slaver civilizations"}
- {"item_id": "jscr-0051", "judgement": "R (shallow review) verdict", "production": "borderline", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Causal Abstraction Intro"}
- {"item_id": "jscr-0054", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "Alignment Whack-a-Mole : Finetuning Activates Verbatim Recall of Copyrighted Books in Larg"}
- {"item_id": "jscr-0058", "judgement": "R (shallow review) verdict", "production": "borderline", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Facts Grounding A New Benchmark For Evaluating The Factuality Of Large Language Models"}
- {"item_id": "jscr-0059", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Fine-Grained Detection of Context-Grounded Hallucinations Using LLMs"}
- {"item_id": "jscr-0060", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "irrelevant", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Compositional Reasoning in Language Models under Reinforcement Learning Post-Training"}
- {"item_id": "jscr-0063", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "borderline", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Formal Verification and Control with Conformal Prediction"}
- {"item_id": "jscr-0065", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "2024 in AI predictions"}
- {"item_id": "jscr-0066", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "irrelevant", "sol": "relevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Your Language Model is Its Own Critic: Reinforcement Learning with Value Estimation from A"}
- {"item_id": "jscr-0067", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Machine Unlearning via Algorithmic Stability"}
- {"item_id": "jscr-0069", "judgement": "R (shallow review) verdict", "production": "borderline", "opus": "borderline", "sol": "relevant", "sonnet": "irrelevant", "majority": null, "title": "Memory Inception: Latent-Space KV Cache Manipulation for Steering LLMs"}
- {"item_id": "jscr-0074", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "irrelevant", "majority": null, "title": "Sci$Π$vot: Benchmarking Principal-Investigator Judgment at Pivotal Moments of Scientific A"}
- {"item_id": "jscr-0078", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "relevant", "majority": "borderline", "title": "New Paper Racing To The Precipice"}
- {"item_id": "jscr-0080", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Prototypical Human-AI Collaboration Behaviors from LLM-Assisted Writing in the Wild"}
- {"item_id": "jscr-0082", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "Assisted Robust Reward Design"}
- {"item_id": "jscr-0086", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "irrelevant", "majority": "irrelevant", "title": "Tracing the Traces: Latent Temporal Signals for Efficient and Accurate Reasoning"}
- {"item_id": "jscr-0087", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Bending The Curve"}
- {"item_id": "jscr-0088", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "relevant", "sol": "irrelevant", "sonnet": "borderline", "majority": null, "title": "Spooky action at a distance in the loss landscape"}
- {"item_id": "jscr-0094", "judgement": "R (shallow review) verdict", "production": "irrelevant", "opus": "borderline", "sol": "irrelevant", "sonnet": "borderline", "majority": "borderline", "title": "Policy Compliance of User Requests in Natural Language for AI Systems"}
- {"item_id": "jscr-0095", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Adaptive Segment-level Reward: Bridging the Gap Between Action and Reward Space in Alignme"}
- {"item_id": "jscr-0099", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "After Overmorrow: Scattered Musings on the Immediate Post-AGI World"}
- {"item_id": "jscr-0100", "judgement": "R (shallow review) verdict", "production": "relevant", "opus": "borderline", "sol": "borderline", "sonnet": "borderline", "majority": "borderline", "title": "Ten years"}
- {"item_id": "jscr-0001", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Arbitrary Cipher Attacks Against Large Language Models Do Not Require Fine-Tuning"}
- {"item_id": "jscr-0005", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "MolReasoner: Toward Effective and Interpretable Reasoning for Molecular LLMs"}
- {"item_id": "jscr-0006", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Repairing Reward Functions with Feedback to Mitigate Reward Hacking"}
- {"item_id": "jscr-0011", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Dependency-Aware Privacy for Multi-turn LLM Agents"}
- {"item_id": "jscr-0012", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "An Introduction to Machine Unlearning"}
- {"item_id": "jscr-0018", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "A framework for thinking about AI power-seeking"}
- {"item_id": "jscr-0020", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Reflections on Trusting Trust & AI"}
- {"item_id": "jscr-0021", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Paper: The Capacity for Moral Self-Correction in Large Language Models (Anthropic)"}
- {"item_id": "jscr-0022", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Quick on the Uptake: Eliciting Implicit Intents from Human Demonstrations for Personalized"}
- {"item_id": "jscr-0025", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "EraseAnything++: Enabling Concept Erasure in Rectified Flow Transformers Leveraging Multi-"}
- {"item_id": "jscr-0026", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Online Learning of non-Markovian Reward Models"}
- {"item_id": "jscr-0028", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Evolving Jailbreaks: Automated Multi-Objective Long-Tail Attacks on Large Language Models"}
- {"item_id": "jscr-0029", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Multi-Attribute Constraint Satisfaction via Language Model Rewriting"}
- {"item_id": "jscr-0032", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "usable", "sonnet": "usable", "majority": "usable", "title": "Grokking Group Multiplication with Cosets"}
- {"item_id": "jscr-0033", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Parenting and Happiness"}
- {"item_id": "jscr-0034", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Masked Jigsaw Puzzle: A Versatile Position Embedding for Vision Transformers"}
- {"item_id": "jscr-0035", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Harmful Prompt Laundering: Jailbreaking LLMs with Abductive Styles and Symbolic Encoding"}
- {"item_id": "jscr-0036", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Safety Fine-Tuning at (Almost) No Cost: A Baseline for Vision Large Language Models"}
- {"item_id": "jscr-0038", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "usable", "sonnet": "usable", "majority": "usable", "title": "Uneven Evolution of Cognition Across Generations of Generative AI Models"}
- {"item_id": "jscr-0039", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "usable", "sonnet": "degraded", "majority": "usable", "title": "The Practical Guide to Superbabies"}
- {"item_id": "jscr-0041", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "From User Surveys to Telemetry-Driven AI Agents: Exploring the Potential of Personalized P"}
- {"item_id": "jscr-0043", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "PeerRank: Autonomous LLM Evaluation Through Web-Grounded, Bias-Controlled Peer Review"}
- {"item_id": "jscr-0044", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "On expected utility, part 4: Dutch books, Cox, and Complete Class"}
- {"item_id": "jscr-0045", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Rainbow Teaming: Open-Ended Generation of Diverse Adversarial Prompts"}
- {"item_id": "jscr-0046", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Adding noise to a sandbagging model can reveal its true capabilities"}
- {"item_id": "jscr-0047", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Building Safer AI from the Ground Up: Steering Model Behavior via Pre-Training Data Curati"}
- {"item_id": "jscr-0050", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Attack Selection Reduces Safety in Concentrated AI Control Settings against Trusted Monito"}
- {"item_id": "jscr-0054", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Alignment Whack-a-Mole : Finetuning Activates Verbatim Recall of Copyrighted Books in Larg"}
- {"item_id": "jscr-0055", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Whitening Sentence Representations for Better Semantics and Faster Retrieval"}
- {"item_id": "jscr-0056", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Alignment Through Rational AI: a testable approach"}
- {"item_id": "jscr-0059", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Fine-Grained Detection of Context-Grounded Hallucinations Using LLMs"}
- {"item_id": "jscr-0060", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Compositional Reasoning in Language Models under Reinforcement Learning Post-Training"}
- {"item_id": "jscr-0063", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Formal Verification and Control with Conformal Prediction"}
- {"item_id": "jscr-0064", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Let's (not) just put things in Context: Test-Time Training for Long-Context LLMs"}
- {"item_id": "jscr-0066", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Your Language Model is Its Own Critic: Reinforcement Learning with Value Estimation from A"}
- {"item_id": "jscr-0067", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Machine Unlearning via Algorithmic Stability"}
- {"item_id": "jscr-0070", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "usable", "sonnet": "degraded", "majority": "usable", "title": "Mega-Reward: Achieving Human-Level Play without Extrinsic Rewards"}
- {"item_id": "jscr-0072", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "usable", "sonnet": "usable", "majority": "usable", "title": "UW-BioNLP at ChemoTimelines 2025: Thinking, Fine-Tuning, and Dictionary-Enhanced LLM Syste"}
- {"item_id": "jscr-0075", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Exploration Hacking: Can LLMs Learn to Resist RL Training?"}
- {"item_id": "jscr-0078", "judgement": "usability verdict", "production": "unusable", "opus": "usable", "sol": "usable", "sonnet": "unusable", "majority": "usable", "title": "New Paper Racing To The Precipice"}
- {"item_id": "jscr-0080", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Prototypical Human-AI Collaboration Behaviors from LLM-Assisted Writing in the Wild"}
- {"item_id": "jscr-0082", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Assisted Robust Reward Design"}
- {"item_id": "jscr-0083", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "usable", "majority": "usable", "title": "Adaptive Workflow Intelligence: A Cognitive Architecture for Context-Driven Enterprise Aut"}
- {"item_id": "jscr-0084", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Abstract2Appendix: Academic Reviews Enhance LLM Long-Context Capabilities"}
- {"item_id": "jscr-0085", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Why Agent Foundations? An Overly Abstract Explanation"}
- {"item_id": "jscr-0086", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "usable", "sonnet": "usable", "majority": "usable", "title": "Tracing the Traces: Latent Temporal Signals for Efficient and Accurate Reasoning"}
- {"item_id": "jscr-0088", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Spooky action at a distance in the loss landscape"}
- {"item_id": "jscr-0089", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "SAKE: Self-aware Knowledge Exploitation-Exploration for Grounded Multimodal Named Entity R"}
- {"item_id": "jscr-0091", "judgement": "usability verdict", "production": "degraded", "opus": "usable", "sol": "usable", "sonnet": "usable", "majority": "usable", "title": "Monday AI Radar #24"}
- {"item_id": "jscr-0093", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Rewarding the Journey, Not Just the Destination: A Composite Path and Answer Self-Scoring "}
- {"item_id": "jscr-0094", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "usable", "sonnet": "degraded", "majority": "usable", "title": "Policy Compliance of User Requests in Natural Language for AI Systems"}
- {"item_id": "jscr-0095", "judgement": "usability verdict", "production": "usable", "opus": "degraded", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Adaptive Segment-level Reward: Bridging the Gap Between Action and Reward Space in Alignme"}
- {"item_id": "jscr-0096", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "degraded", "sonnet": "degraded", "majority": "degraded", "title": "Can LLMs get help from other LLMs without revealing private information?"}
- {"item_id": "jscr-0097", "judgement": "usability verdict", "production": "usable", "opus": "usable", "sol": "usable", "sonnet": "degraded", "majority": "usable", "title": "Keeping Time vs. Understanding Time: Neither Alone Explains Temporal Reasoning in LLMs"}
