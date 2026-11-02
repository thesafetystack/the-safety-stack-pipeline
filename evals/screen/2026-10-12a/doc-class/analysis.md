gold: 60 items (two Opus labelers, doc_type agree 54/60, 6 adjudicated); LLM-decided items (collector type paper/preprint/journal): 50
| system | doc_type exact (60) | exact on LLM-decided | paper/preprint merged, LLM-decided | self_published | primary_lang | $/1k docs | errors |
|---|---|---|---|---|---|---|---|
| integrated v12 (record: channel rule + model) | 53/60 | 46/50 | 49/50 | 59/60 | 60/60 | +0.10 (v12 vs v11 tier 1) | 0 |
| integrated v12 (model answer only) | 51/60 | 46/50 | 49/50 | 59/60 | 60/60 | +0.10 (v12 vs v11 tier 1) | 0 |
| collector metadata.doc_type only | 30/60 | 23/50 | 47/50 | - | - | 0 | 0 |
| separate openai/gpt-6-luna (+ channel rule) | 53/60 | 46/50 | 49/50 | 56/60 | 60/60 | 0.269 | 0 |
| separate openai/gpt-5-nano (+ channel rule) | 32/60 | 25/50 | 48/50 | 49/60 | 59/60 | 0.520 | 0 |
| separate google/gemini-2.5-flash-lite (+ channel rule) | 18/60 | 11/50 | 46/50 | 51/60 | 57/60 | 0.173 | 2 |
| separate anthropic/claude-haiku-5.5 (+ channel rule) | 50/60 | 43/50 | 48/50 | 52/60 | 59/60 | 0.229 | 0 |
gold doc_type on LLM-decided: Counter({'preprint': 42, 'paper': 5, 'other': 2, 'eval_report': 1})
integrated v12 (record: channel rule + model) confusions: [(('paper', 'preprint'), 2), (('paper', 'critique'), 1), (('preprint', 'paper'), 1)]
integrated v12 (model answer only) confusions: [(('paper', 'preprint'), 2), (('paper', 'critique'), 1), (('preprint', 'paper'), 1)]
collector metadata.doc_type only confusions: [(('preprint', 'paper'), 24), (('other', 'paper'), 2), (('eval_report', 'preprint'), 1)]
separate openai/gpt-6-luna (+ channel rule) confusions: [(('paper', 'preprint'), 2), (('preprint', 'paper'), 1), (('other', 'preprint'), 1)]
separate openai/gpt-5-nano (+ channel rule) confusions: [(('preprint', 'paper'), 22), (('other', 'paper'), 2), (('paper', 'preprint'), 1)]
separate google/gemini-2.5-flash-lite (+ channel rule) confusions: [(('preprint', 'paper'), 35), (('other', 'paper'), 2)]
separate anthropic/claude-haiku-5.5 (+ channel rule) confusions: [(('preprint', 'paper'), 3), (('paper', 'preprint'), 2), (('paper', 'other'), 1), (('other', 'preprint'), 1)]
