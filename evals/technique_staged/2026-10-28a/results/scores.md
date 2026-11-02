# Effort-high confirmation on the test set: scores (`confirm.py score`)

**Verdict (frozen rule, reference ref-t): CONFIRMS (robust).** ΔF1 +0.044 [+0.022, +0.068], ΔP +0.009 [-0.016, +0.035], ΔR +0.066 [+0.038, +0.097], HALL median latency 147 s; criteria {'delta_f1': True, 'delta_p': True, 'latency': True}.

## Presence P / R / F1 per reference (HALL: 2 replicates pooled)

| reference | PROD P | PROD R | PROD F1 | HALL P | HALL R | HALL F1 | BASEF P | BASEF R | BASEF F1 | ΔF1 HALL-PROD [95% CI] | ΔP | ΔR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ref-t | 0.883 | 0.686 | 0.772 | 0.892 | 0.752 | 0.816 | 0.887 | 0.670 | 0.763 | +0.044 [+0.022, +0.068] | +0.009 [-0.016, +0.035] | +0.066 [+0.038, +0.097] |
| judge opus | 0.782 | 0.690 | 0.733 | 0.774 | 0.757 | 0.765 | 0.809 | 0.687 | 0.743 | +0.032 [+0.010, +0.055] | -0.008 [-0.039, +0.024] | +0.067 [+0.040, +0.097] |
| judge sonnet | 0.720 | 0.770 | 0.744 | 0.697 | 0.827 | 0.756 | 0.744 | 0.767 | 0.755 | +0.012 [-0.010, +0.037] | -0.023 [-0.052, +0.007] | +0.057 [+0.026, +0.091] |
| judge sol | 0.882 | 0.556 | 0.682 | 0.899 | 0.629 | 0.740 | 0.900 | 0.546 | 0.680 | +0.058 [+0.037, +0.081] | +0.017 [-0.013, +0.046] | +0.073 [+0.048, +0.100] |
| majority | 0.794 | 0.720 | 0.756 | 0.792 | 0.797 | 0.794 | 0.825 | 0.720 | 0.769 | +0.039 [+0.019, +0.060] | -0.002 [-0.033, +0.026] | +0.076 [+0.048, +0.107] |
| ref-t dc=in | 0.891 | 0.585 | 0.706 | 0.902 | 0.656 | 0.760 | 0.893 | 0.564 | 0.692 | +0.054 [+0.030, +0.080] | +0.011 [-0.013, +0.035] | +0.072 [+0.042, +0.104] |
| ref-t dc=out | 0.822 | 0.686 | 0.748 | 0.813 | 0.752 | 0.781 | 0.835 | 0.670 | 0.744 | +0.034 [+0.011, +0.057] | -0.009 [-0.041, +0.024] | +0.066 [+0.038, +0.097] |

## Other paired differences (F1)

| reference | HALL - BASEF | BASEF - PROD |
|---|---|---|
| ref-t | +0.053 [+0.025, +0.081] | -0.009 [-0.028, +0.014] |
| judge opus | +0.022 [+0.001, +0.043] | +0.010 [-0.013, +0.036] |
| judge sonnet | +0.001 [-0.026, +0.026] | +0.011 [-0.009, +0.037] |
| judge sol | +0.060 [+0.037, +0.087] | -0.002 [-0.025, +0.022] |
| majority | +0.025 [+0.001, +0.048] | +0.014 [-0.005, +0.037] |
| ref-t dc=in | +0.068 [+0.042, +0.097] | -0.014 [-0.036, +0.009] |
| ref-t dc=out | +0.038 [+0.008, +0.067] | -0.004 [-0.025, +0.020] |

## Operations (eval-harness arms)

| arm | obs | status | p50 s | p95 s | max s | $/1k standalone | actual $ | reasoning tok | completion tok | truncated |
|---|---|---|---|---|---|---|---|---|---|---|
| HALL | 200 | {'ok': 200} | 147 | 410 | 496 | 10.65 | 2.130 | 14806 | 16119 | 0 |
| BASEF | 100 | {'ok': 100} | 60 | 146 | 240 | 7.35 | 0.735 | 6400 | 7371 | 0 |

## Sensitivity on ref-t (ΔF1 HALL - PROD)

- HALL r1 - PROD: +0.040 [+0.016, +0.067]
- HALL r2 - PROD: +0.048 [+0.025, +0.073]
- leave one document out: largest change +0.004 (jtec-0006); range +0.040 .. +0.048
- by_stratum: forum_post (n 32) 0.747 -> 0.833 (+0.086); lab_blog (n 5) 0.947 -> 1.000 (+0.053); paper (n 14) 0.846 -> 0.900 (+0.054); preprint (n 49) 0.748 -> 0.782 (+0.034)
- by_band: 20-60k (n 26) 0.747 -> 0.836 (+0.088); 60-100k (n 24) 0.725 -> 0.780 (+0.056); <20k (n 29) 0.825 -> 0.866 (+0.040); >100k (n 21) 0.799 -> 0.809 (+0.010)
- adversarial_dont_care: +0.014 [-0.009, +0.036]
- dont_care_by_who_predicts: {'PROD predicts more': 10, 'tie': 69, 'HALL predicts more': 25}
- single adjudicator opus-t: +0.035 [+0.012, +0.061]
- single adjudicator sol-t: +0.055 [+0.031, +0.079]

## Agreement (slug F1 between observations)

- HALL r1 vs r2: 0.874
- BASEF vs PROD: 0.876
- judge opus vs sonnet: 0.828
- judge opus vs sol: 0.726
- judge sonnet vs sol: 0.648

Mean slugs per document: PROD 3.21, HALL 3.56, BASEF 3.09, judge opus 3.64, judge sonnet 3.00, judge sol 5.09, majority 3.54
Judge status: {'opus': {'valid': 100}, 'sonnet': {'valid': 100}, 'sol': {'valid': 100}}; invalid slugs dropped: {}

## Reference ref-t

```
{
 "candidates": 433,
 "calibration": 61,
 "calibration_both_present": 59,
 "adjudicator_confusion": {
  "absent/absent": 87,
  "absent/present": 85,
  "borderline/absent": 13,
  "borderline/present": 15,
  "present/absent": 47,
  "present/present": 186
 },
 "adjudicator_kappa_3way": 0.2750635679680224,
 "adjudicator_kappa_present": 0.30398792796142193,
 "adjudicator_agreement_present": 0.6605080831408776,
 "split": 160,
 "arbiters_agree": 56,
 "arbiter_confusion": {
  "absent/absent": 46,
  "absent/borderline": 12,
  "absent/present": 33,
  "present/absent": 52,
  "present/borderline": 7,
  "present/present": 10
 },
 "counts": {
  "dont_care <- arbiters split": 104,
  "in <- 3/3": 189,
  "in <- adjudicators present": 186,
  "in <- arbiters present": 10,
  "out <- adjudicators absent": 87,
  "out <- arbiters absent": 46
 },
 "verdict_by_support": {
  "support 0 dont_care": 8,
  "support 0 in": 6,
  "support 0 out": 39,
  "support 1 dont_care": 68,
  "support 1 in": 67,
  "support 1 out": 80,
  "support 2 dont_care": 26,
  "support 2 in": 64,
  "support 2 out": 14,
  "support 3 dont_care": 2,
  "support 3 in": 248
 },
 "arbiters_agreeing_sided_with": {
  "opus-t": 26,
  "sol-t": 28,
  "neither": 2
 },
 "verdicts": {
  "out": 133,
  "dont_care": 104,
  "in": 385
 }
}
```
