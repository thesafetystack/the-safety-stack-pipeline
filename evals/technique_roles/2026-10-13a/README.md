# Annotator eval set 2026-10-13a: technique_roles

Superseded: `annotate.technique_roles` is not in production; production technique extraction is
`annotate.technique_staged`, measured on `evals/technique_staged/2026-10-23a` (gold v3) and
`evals/judge-2026-10-25j/technique_staged`. This stays the set of the technique_roles stage (`stack eval
technique_roles`; `tests/test_eval_annotators.py` checks its input digests).

Same 25 documents, item ids, split and gold protocol as `evals/model_roles/2026-10-13a/README.md` (sampling,
labellers, adjudication, agreement; labels and instructions live there). Whole texts are not in the repository
(D790; `tools/fetch_eval_texts.py`). Items here carry no model mentions; `input_digest` = sha256 of production's user
message (`scripts/freeze.py` rebuilds `items.jsonl` byte for byte at HEAD; `scripts/packets.py` writes labeller
packets). `manifest.json` `frozen_with` records the menu source (`taxonomy_path`, `taxonomy_sha256`,
`taxonomy_version`): `stack eval technique_roles` refuses the set when a gold slug is not on the current menu and
warns when only the taxonomy digest moved. Gold `gold.jsonl`: `label.techniques` (catalogue slug from
`taxonomy/taxonomy-v0.8.yaml`, 12 roles 0-4, one verbatim quote) and `label.extra_techniques`. Base rates: 171
technique rows (6.8 per document, 125 with a role ≥ 2), 34 extra techniques, 3 documents with none. Contested: 19 of
25 documents (129 elements). Labeller agreement on technique presence: Opus vs Codex F1 0.667 (3 docs; Opus lists
more low-strength rows), Opus vs Opus 0.920 (22 docs).

## Results (2026-10-13)

Production `ask` (prompt `technique-roles-v1`, role `extract_main`, model overridden, permissive output schema per
D275). Techniques = slug present; cells = (slug, role) ≥ 2.

| model | techniques P / R / F1 | 95 % CI of F1 − luna | recall of gold rows ≥ 2 | cells ≥ 2 F1 | quotes verified | USD / 1k docs |
|---|---|---|---|---|---|---|
| openai/gpt-6-luna | 0.949 / 0.550 / **0.696** | — | 0.592 | 0.620 | 97.9 % of 189 | **2.87** |
| openai/gpt-6-luna, repeat 2 | — / — / 0.689 | [−0.047, +0.031] | — | — | — | (cache-warm) |
| openai/gpt-6-luna-pro | 0.960 / 0.561 / 0.708 | [−0.035, +0.057] | 0.624 | 0.630 | 97.4 % of 195 | 9.76 |
| openai/gpt-5.6-luna | 0.890 / 0.567 / 0.693 | [−0.065, +0.069] | 0.616 | 0.606 | 94.1 % of 188 | 5.78 |

Luna's run-to-run agreement on technique presence is F1 0.866. The models are precise and miss about 40 % of the gold
rows with a role ≥ 2; the candidates do not change that. Pooling runs does: luna ∪ luna-pro P 0.955 / R 0.620 / F1
0.752 (offline union).

`anthropic/claude-haiku-5.5` returned `"roles": {}` on every row under the then open-map `roles` schema, so every
document ended `empty`. Since then `judge.output_schema()` names the twelve roles as optional integer properties and
an answer whose every row has an empty roles map is `failed_permanent[schema_invalid]` (`judge.roles_degenerate`).
Re-run of luna with the fixed-key schema: techniques F1 0.686, inside its own repeat noise; a 3-document haiku probe
then answered with rows on every document.
