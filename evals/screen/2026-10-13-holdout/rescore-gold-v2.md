# v12 holdout run re-scored on gold-v2 (2026-10-13; stored rows, no model calls)

Run `20261013T044356Z_holdout_667ebb21` (rows.jsonl and run.json copied to a scratch directory; the run directory itself is not re-scored), `uv run --no-sync stack eval score --run <copy> --gold <gold-v2.jsonl>`. The original summary (gold v1) stays in the run directory. Below: gold-v2, then the sensitivity gold `gold-v2-alt-opus-adjudicator.jsonl` (main tables only).

run `eval-screen-20261013T044356Z_holdout_667ebb21`; models openai/gpt-6-luna; repeats 1; baseline openai/gpt-6-luna; gold yes (60 items, gold-v2.jsonl).
Skipped (egress) and not-attempted rows are outside every denominator; a failed row counts as no answer. Cost is settled USD over rows that made calls (cache hits excluded).

## screen

| model | n | acc | κ | rel recall | rel prec | kept recall | kept prec | bucket J | quote verbatim | tier-2 share | parse/other fail | egress skip | not attempted | USD / 1,000 docs | latency ms p50/p90 | repeat agree | McNemar p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 60 | 88.3% | 0.767 | 86.1% | 96.9% | 83.8% | 96.9% | 0.613 | 96.8% | 3.3% | 0/0 | 0 | 0 | 0.777 | 8888/16449 | n/a | — |

### Shallow Review scope (gold v2)

Scope acc/κ on answered rows with a scope; kept recall per gold scope; category accuracy on rows both kept with a category; SR export = kept and (sr_only, or core with a category other than none); Stack export = kept and core. n/a: the model's answers carry no scope (v7/v8).

| model | scope n | scope acc | scope κ | kept recall core | kept recall sr_only | category acc | SR export P / R | Stack export P / R |
|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 0 | n/a | n/a | 87.9% (n 33) | 50.0% (n 4) | n/a (n 0) | n/a | n/a |

### per question

S = Stack relevance, R = Shallow Review relevance (gold: `label.stack_verdict` / `label.sr_verdict`); overall = the keep decision vs gold `verdict`. A failed row or one without that answer is no answer. Quotes per question: returned per kept answer, verbatim = anchored / returned. USD per 1,000 documents settled (provider cache) / table price without cache; latency over rows that made calls.

| model / design | question | n | answered | parse fail | acc | κ | kept recall | kept prec | rel recall | rel prec | quotes / kept ans | verbatim | kept ans quoted | conf right / wrong | R category acc | USD/1k settled / no cache | latency p50 seq / par |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna (dual) | S (Stack) | 60 | 60 | 0 | 88.3% | 0.765 | 87.9% | 90.6% | 87.9% | 90.6% | 1.94 | 96.8% | 100.0% | 0.97 / 0.92 | — | — | — |
| openai/gpt-6-luna (dual) | R (Shallow Review) | 60 | 60 | 0 | 88.3% | 0.769 | 83.3% | 96.8% | 85.7% | 96.8% | 1.97 | 95.1% | 100.0% | 0.97 / 0.97 | 73.3% (n 30) | — | — |
| openai/gpt-6-luna (dual) | overall keep | 60 | 60 | 0 | 88.3% | 0.767 | 83.8% | 96.9% | 86.1% | 96.9% | — | — | — | — | — | 0.777 / 1.088 | 8888 / 8888 |

### text usability (question U)

Final answers over answered rows: verdict counts (`none` = no usability answer), issue codes (a row may carry several), rows with a note; chars shown = mean document characters tier 1 sent. Not scored: there is no usability gold.

| model | answered | usable | degraded | unusable | none | issues | notes | chars shown |
|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 60 | 35 | 22 | 3 | 0 | garbled_maths 13, other 6, not_body 4, bad_encoding 3, garbled_tables 2 | 25 | 11105 |

### what tier 2 changed

- **openai/gpt-6-luna**: escalated 2 (failed 0); {'borderline->irrelevant': 2}; correct vs gold before 0, after 0; {'borderline->irrelevant (gold relevant)': 2}

## provider prompt cache and USD per 1,000 documents

Over scored rows, each one (item, repeat) call (screen: tier 2 included); a row replayed from the local response cache carries the original call's tokens and cost (from its cache entry). *settled* = OpenRouter's `usage.cost` (provider cache discounts and write premiums included); *no cache* = prompt x in-price + completion x out-price at the `llm.yaml` table (version 1.9.2); for deepseek via DeepInfra the table holds the listing price, above the endpoint's. Retries and failed tiers count at their billed amount in both. Usage backfilled from cache entries: {'recorded': 60}.

| model | rows | prompt tok/row | cached share | cache-write tok | reasoning tok/row | USD/1k settled (provider cache) | USD/1k no cache (table) | rows from cache entry | usage missing |
|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 60 | 7072 | 59.9% | 169873 | 532 | 0.777 | 1.088 | 0 | 0 |


## Sensitivity: gold-v2-alt-opus-adjudicator.jsonl

## screen

| model | n | acc | κ | rel recall | rel prec | kept recall | kept prec | bucket J | quote verbatim | tier-2 share | parse/other fail | egress skip | not attempted | USD / 1,000 docs | latency ms p50/p90 | repeat agree | McNemar p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 60 | 90.0% | 0.804 | 91.2% | 96.9% | 86.1% | 96.9% | 0.630 | 96.8% | 3.3% | 0/0 | 0 | 0 | 0.777 | 8888/16449 | n/a | — |

### per question

S = Stack relevance, R = Shallow Review relevance (gold: `label.stack_verdict` / `label.sr_verdict`); overall = the keep decision vs gold `verdict`. A failed row or one without that answer is no answer. Quotes per question: returned per kept answer, verbatim = anchored / returned. USD per 1,000 documents settled (provider cache) / table price without cache; latency over rows that made calls.

| model / design | question | n | answered | parse fail | acc | κ | kept recall | kept prec | rel recall | rel prec | quotes / kept ans | verbatim | kept ans quoted | conf right / wrong | R category acc | USD/1k settled / no cache | latency p50 seq / par |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6-luna (dual) | S (Stack) | 60 | 60 | 0 | 88.3% | 0.769 | 87.9% | 90.6% | 90.6% | 90.6% | 1.94 | 96.8% | 100.0% | 0.97 / 0.92 | — | — | — |
| openai/gpt-6-luna (dual) | R (Shallow Review) | 60 | 60 | 0 | 90.0% | 0.806 | 85.7% | 96.8% | 90.9% | 96.8% | 1.97 | 95.1% | 100.0% | 0.97 / 0.97 | 73.3% (n 30) | — | — |
| openai/gpt-6-luna (dual) | overall keep | 60 | 60 | 0 | 90.0% | 0.804 | 86.1% | 96.9% | 91.2% | 96.9% | — | — | — | — | — | 0.777 / 1.088 | 8888 / 8888 |

### what tier 2 changed

- **openai/gpt-6-luna**: escalated 2 (failed 0); {'borderline->irrelevant': 2}; correct vs gold before 1, after 0; {'borderline->irrelevant (gold borderline)': 1, 'borderline->irrelevant (gold relevant)': 1}
