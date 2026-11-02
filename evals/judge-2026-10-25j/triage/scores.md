# Judge eval scores: triage (2026-10-25j)

100 items. Judge records: opus: {"triage:valid": 100}; sol: {"triage:valid": 100}; sonnet: {"triage:valid": 100}

Production is the prediction, the judge (or the reference label set) the reference. Majority = items (categorical) or units (sets) on which at least 2 of the 3 judges (Opus, Sol, Sonnet) agree. Consensus = items (categorical) or units (sets) on which Opus and Sol agree (the 2026-10-25 two-judge rule; Sonnet ignored).

## Headline: production vs majority (2 of 3) and vs consensus (Opus and Sol)

| judgement | n maj | agreement maj | kappa maj | n cons | agreement cons | kappa cons | no majority |
|---|---|---|---|---|---|---|---|
| verdict | 99 | 0.950 | 0.646 | 94 | 0.968 | 0.654 | 1 |
| fetch (relevant\|borderline vs not_relevant) | 100 | 0.990 | 0.936 | 97 | 0.990 | 0.928 | 0 |

Per-class P / R / F1 for each categorical judgement are in its section below.

## verdict

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.920 | 0.524 |
| production_vs_sol | 100 | 0.960 | 0.747 |
| production_vs_sonnet | 100 | 0.940 | 0.643 |
| opus_vs_sol | 100 | 0.940 | 0.624 |
| opus_vs_sonnet | 100 | 0.950 | 0.702 |
| sol_vs_sonnet | 100 | 0.940 | 0.626 |
| production_vs_consensus | 94 | 0.968 | 0.654 |
| production_vs_majority | 99 | 0.950 | 0.646 |

Items with at least two judge labels but no majority (1-1-1): 1.

production_vs_majority (production rows, majority columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 0 | 2 |
| **not_relevant** | 0 | 91 | 0 |
| **relevant** | 2 | 1 | 2 |

production_vs_consensus (production rows, consensus columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 0 | 2 |
| **not_relevant** | 0 | 89 | 0 |
| **relevant** | 0 | 1 | 2 |

production_vs_opus (production rows, opus columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 1 | 0 | 2 |
| **not_relevant** | 2 | 89 | 0 |
| **relevant** | 2 | 2 | 2 |

production_vs_sol (production rows, sol columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 0 | 3 |
| **not_relevant** | 0 | 91 | 0 |
| **relevant** | 0 | 1 | 5 |

production_vs_sonnet (production rows, sonnet columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 2 | 0 | 1 |
| **not_relevant** | 1 | 90 | 0 |
| **relevant** | 3 | 1 | 2 |

opus_vs_sol (opus rows, sol columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 2 | 3 |
| **not_relevant** | 0 | 90 | 1 |
| **relevant** | 0 | 0 | 4 |

opus_vs_sonnet (opus rows, sonnet columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 3 | 2 | 0 |
| **not_relevant** | 2 | 89 | 0 |
| **relevant** | 1 | 0 | 3 |

sol_vs_sonnet (sol rows, sonnet columns):

| | borderline | not_relevant | relevant |
|---|---|---|---|
| **borderline** | 0 | 0 | 0 |
| **not_relevant** | 1 | 91 | 0 |
| **relevant** | 5 | 0 | 3 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.333 | 0.333 | 0.333 | 3 | 3 |
| not_relevant | 1.000 | 0.989 | 0.995 | 91 | 92 |
| relevant | 0.400 | 0.500 | 0.444 | 5 | 4 |

Confusion (production -> majority): borderline->borderline: 1, borderline->relevant: 2, not_relevant->not_relevant: 91, relevant->borderline: 2, relevant->not_relevant: 1, relevant->relevant: 2

By basis (majority): fields: n 87, agreement 0.954, kappa 0.617; title: n 12, agreement 0.917, kappa 0.714

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| borderline | 0.000 | n/a | n/a | 2 | 0 |
| not_relevant | 1.000 | 0.989 | 0.994 | 89 | 90 |
| relevant | 0.667 | 0.500 | 0.571 | 3 | 4 |

Confusion (production -> consensus): borderline->relevant: 2, not_relevant->not_relevant: 89, relevant->not_relevant: 1, relevant->relevant: 2

By basis (consensus): fields: n 84, agreement 0.964, kappa 0.652; title: n 10, agreement 1.000, kappa n/a

## fetch (relevant|borderline vs not_relevant)

| comparison | n | agreement | kappa |
|---|---|---|---|
| production_vs_opus | 100 | 0.960 | 0.756 |
| production_vs_sol | 100 | 0.990 | 0.936 |
| production_vs_sonnet | 100 | 0.980 | 0.878 |
| opus_vs_sol | 100 | 0.970 | 0.807 |
| opus_vs_sonnet | 100 | 0.960 | 0.756 |
| sol_vs_sonnet | 100 | 0.990 | 0.936 |
| production_vs_consensus | 97 | 0.990 | 0.928 |
| production_vs_majority | 100 | 0.990 | 0.936 |

Items with at least two judge labels but no majority (1-1-1): 0.

production_vs_majority (production rows, majority columns):

| | drop | keep |
|---|---|---|
| **drop** | 91 | 0 |
| **keep** | 1 | 8 |

production_vs_consensus (production rows, consensus columns):

| | drop | keep |
|---|---|---|
| **drop** | 89 | 0 |
| **keep** | 1 | 7 |

production_vs_opus (production rows, opus columns):

| | drop | keep |
|---|---|---|
| **drop** | 89 | 2 |
| **keep** | 2 | 7 |

production_vs_sol (production rows, sol columns):

| | drop | keep |
|---|---|---|
| **drop** | 91 | 0 |
| **keep** | 1 | 8 |

production_vs_sonnet (production rows, sonnet columns):

| | drop | keep |
|---|---|---|
| **drop** | 90 | 1 |
| **keep** | 1 | 8 |

opus_vs_sol (opus rows, sol columns):

| | drop | keep |
|---|---|---|
| **drop** | 90 | 1 |
| **keep** | 2 | 7 |

opus_vs_sonnet (opus rows, sonnet columns):

| | drop | keep |
|---|---|---|
| **drop** | 89 | 2 |
| **keep** | 2 | 7 |

sol_vs_sonnet (sol rows, sonnet columns):

| | drop | keep |
|---|---|---|
| **drop** | 91 | 1 |
| **keep** | 0 | 8 |

Production vs majority, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| drop | 1.000 | 0.989 | 0.995 | 91 | 92 |
| keep | 0.889 | 1.000 | 0.941 | 9 | 8 |

Confusion (production -> majority): drop->drop: 91, keep->drop: 1, keep->keep: 8

Production vs consensus, per class:

| class | precision | recall | F1 | n_pred | n_ref |
|---|---|---|---|---|---|
| drop | 1.000 | 0.989 | 0.994 | 89 | 90 |
| keep | 0.875 | 1.000 | 0.933 | 8 | 7 |

Confusion (production -> consensus): drop->drop: 89, keep->drop: 1, keep->keep: 7

## Disagreements (10)

- {"item_id": "jtri-0009", "judgement": "verdict", "production": "relevant", "opus": "not_relevant", "sol": "not_relevant", "sonnet": "not_relevant", "majority": "not_relevant", "title": "Efficient ML Models for Practical Secure Inference"}
- {"item_id": "jtri-0024", "judgement": "verdict", "production": "borderline", "opus": "relevant", "sol": "relevant", "sonnet": "borderline", "majority": "relevant", "title": "When Background Matters: Breaking Medical Vision Language Models by Transferable Attack"}
- {"item_id": "jtri-0031", "judgement": "verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Exploring the Interplay of Interpretability and Robustness in Deep Neural Networks: A Sali"}
- {"item_id": "jtri-0037", "judgement": "verdict", "production": "relevant", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "AI: Defend the Defender: Part IV — Fault-Tolerant Defense Architecture"}
- {"item_id": "jtri-0041", "judgement": "verdict", "production": "relevant", "opus": "not_relevant", "sol": "relevant", "sonnet": "borderline", "majority": null, "title": "The Nuances of Confucianism in Technology Policy: an Inquiry into the Interaction Between "}
- {"item_id": "jtri-0052", "judgement": "verdict", "production": "not_relevant", "opus": "not_relevant", "sol": "not_relevant", "sonnet": "borderline", "majority": "not_relevant", "title": "Less Discriminatory Algorithms"}
- {"item_id": "jtri-0064", "judgement": "verdict", "production": "borderline", "opus": "relevant", "sol": "relevant", "sonnet": "relevant", "majority": "relevant", "title": "The Computational Complexity of Single-Player Imperfect-Recall Games"}
- {"item_id": "jtri-0072", "judgement": "verdict", "production": "not_relevant", "opus": "borderline", "sol": "not_relevant", "sonnet": "not_relevant", "majority": "not_relevant", "title": "The Slingshot Mechanism: An Empirical Study of Adaptive Optimizers and the Grokking Phenom"}
- {"item_id": "jtri-0078", "judgement": "verdict", "production": "borderline", "opus": "borderline", "sol": "relevant", "sonnet": "borderline", "majority": "borderline", "title": "Mark My Words: Dangers of Watermarked Images in ImageNet"}
- {"item_id": "jtri-0099", "judgement": "verdict", "production": "not_relevant", "opus": "borderline", "sol": "not_relevant", "sonnet": "not_relevant", "majority": "not_relevant", "title": "Neighboring Words Affect Human Interpretation of Saliency Explanations"}
