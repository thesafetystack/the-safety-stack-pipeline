# doc_class comparison re-scored on gold-v2 (2026-10-13; stored rows, no model calls)

`python3 evals/screen/2026-10-12a/doc-class/scripts/rescore.py evals/screen/2026-10-12a/doc-class/gold-v2.jsonl`; `analysis.md` is the same table on gold v1.

gold: gold-v2.jsonl, 60 items; LLM-decided items (collector type paper/preprint/journal): 50
| system | doc_type exact (60) | exact on LLM-decided | paper/preprint merged, LLM-decided | self_published | primary_lang | $/1k docs | errors |
|---|---|---|---|---|---|---|---|
| integrated v12 (record: channel rule + model) | 55/60 | 48/50 | 49/50 | 59/60 | 60/60 | +0.10 (v12 vs v11 tier 1) | 0 |
| integrated v12 (model answer only) | 53/60 | 48/50 | 49/50 | 59/60 | 60/60 | +0.10 (v12 vs v11 tier 1) | 0 |
| collector metadata.doc_type only | 28/60 | 21/50 | 47/50 | - | - | 0 | 0 |
| separate openai/gpt-6-luna (+ channel rule) | 55/60 | 48/50 | 49/50 | 56/60 | 60/60 | 0.269 | 0 |
| separate openai/gpt-5-nano (+ channel rule) | 32/60 | 25/50 | 48/50 | 49/60 | 59/60 | 0.520 | 0 |
| separate google/gemini-2.5-flash-lite (+ channel rule) | 16/60 | 9/50 | 46/50 | 51/60 | 57/60 | 0.173 | 2 |
| separate anthropic/claude-haiku-5.5 (+ channel rule) | 52/60 | 45/50 | 48/50 | 52/60 | 59/60 | 0.229 | 0 |
gold doc_type on LLM-decided: Counter({'preprint': 44, 'paper': 3, 'other': 2, 'eval_report': 1})
integrated v12 (record: channel rule + model) confusions: [(('paper', 'critique'), 1), (('preprint', 'paper'), 1)]
integrated v12 (model answer only) confusions: [(('paper', 'critique'), 1), (('preprint', 'paper'), 1)]
collector metadata.doc_type only confusions: [(('preprint', 'paper'), 26), (('other', 'paper'), 2), (('eval_report', 'preprint'), 1)]
separate openai/gpt-6-luna (+ channel rule) confusions: [(('preprint', 'paper'), 1), (('other', 'preprint'), 1)]
separate openai/gpt-5-nano (+ channel rule) confusions: [(('preprint', 'paper'), 23), (('other', 'paper'), 2)]
separate google/gemini-2.5-flash-lite (+ channel rule) confusions: [(('preprint', 'paper'), 37), (('other', 'paper'), 2)]
separate anthropic/claude-haiku-5.5 (+ channel rule) confusions: [(('preprint', 'paper'), 3), (('paper', 'other'), 1), (('other', 'preprint'), 1)]
