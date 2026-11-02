# Annotator eval set 2026-10-13a: technique_roles

Same 25 documents, item ids, split and gold protocol as `evals/model_roles/2026-10-13a/README.md` (sampling, labellers,
adjudication, agreement, what v3 had; labels and instructions live there, its scripts are historical). Items here carry no model
mentions; `input_digest` = sha256 of production's user message (frozen 2026-10-13 with `render.render_user`; since
D591 the prompt file's user part, the same bytes: `scripts/freeze.py` rebuilds `items.jsonl` byte for byte at HEAD
and `scripts/packets.py` writes labeller packets). `manifest.json` `frozen_with` records the menu source
(`taxonomy_path`, `taxonomy_sha256`, `taxonomy_version`): `stack eval technique_roles` refuses the set when a gold
slug is not on the current menu and warns when only the taxonomy digest moved. Gold `gold.jsonl`:
`label.techniques` (catalogue slug from `taxonomy/taxonomy-v0.8.yaml`, 12 roles 0-4, one verbatim quote) and
`label.extra_techniques`. Base rates: 171 technique rows (6.8 per document, 125 with a role ≥ 2), 34 extra
techniques, 3 documents with none. Contested: 19 of 25 documents (129 elements). Labeller agreement on technique
presence: Opus vs Codex F1 0.667 (3 docs; Opus lists more low-strength rows), Opus vs Opus 0.920 (22 docs).

## Results (2026-10-13; runs in `v4/evals-store/technique_roles/2026-10-13a/`, gitignored; not in the repo)

Production `ask` (pin technique-roles-v1, role extract_main, model overridden, permissive output schema per D275),
runs `20261013T132018Z_all_31a4e585` (luna, luna-pro, haiku-5.5), `20261013T132609Z_all_27d7d664` (gpt-5.6-luna),
luna repeat 2 `20261013T132944Z_all_9ebcfda2`. Techniques = slug present; cells = (slug, role) ≥ 2.

| model | techniques P / R / F1 | 95 % CI of F1 − luna | recall of gold rows ≥ 2 | cells ≥ 2 F1 | quotes verified | USD / 1k docs |
|---|---|---|---|---|---|---|
| openai/gpt-6-luna (pinned) | 0.949 / 0.550 / **0.696** | — | 0.592 | 0.620 | 97.9 % of 189 | **2.87** |
| openai/gpt-6-luna, repeat 2 | — / — / 0.689 | [−0.047, +0.031] | — | — | — | (cache-warm) |
| openai/gpt-6-luna-pro | 0.960 / 0.561 / 0.708 | [−0.035, +0.057] | 0.624 | 0.630 | 97.4 % of 195 | 9.76 |
| openai/gpt-5.6-luna (v3's) | 0.890 / 0.567 / 0.693 | [−0.065, +0.069] | 0.616 | 0.606 | 94.1 % of 188 | 5.78 |
| anthropic/claude-haiku-5.5 | no rows (see below) | — | 0 | — | — | 6.31 |

No parse or schema failures; every OpenAI call takes the strict-schema 400 and the degraded retry (D275). Luna's
run-to-run agreement on technique presence is F1 0.866. The models are precise and miss about 40 % of the gold
rows with a role ≥ 2 (recall 0.59-0.62); the candidates do not change that. Pooling runs does: luna r1 ∪ r2 gives
P 0.921 / R 0.614 / F1 0.737, luna ∪ luna-pro P 0.955 / R 0.620 / F1 0.752 (offline union of these runs; v3's
recommended strategy was also a union).

**haiku-5.5 returns `"roles": {}` on every technique row** (raw cached reply, e.g. `{"slug":
"jailbreak-robustness-eval","roles":{},"quotes":[…],"note":"placeholder removed"}`): the Anthropic route accepts the
permissive open-map `roles` schema without a 400, constrained decoding leaves the map empty, and `judge.normalize`
drops every row (`empty_roles_dropped`), so all 25 documents end `empty` with status ok. Any Anthropic model pinned to
technique_roles today would silently produce nothing; a strict fixed-key `roles` schema (as model_roles has since
D275) would be needed before Anthropic models can be compared here.

## Re-run after the fixed-key `roles` schema (2026-10-13, T-R6 item 5)

`judge.output_schema()` now names the twelve roles as optional integer properties of `roles` (no open map); an
answer whose every row has an empty roles map is `failed_permanent[schema_invalid]`, not `empty` (`main.ask`,
`judge.roles_degenerate`). gpt-6-luna, 25 items, one repeat, in three runs because one all-split run's reservation
($0.343 at the 16k-token cap) exceeds a $0.20 budget: `20261013T151124Z_dev_52008058`,
`20261013T151305Z_test_de160a2b`, `20261013T151418Z_test_73168d4d` (settled $0.0874). Same metrics as above,
recomputed from the rows (`technique_roles_block`; recall of gold rows with a role ≥ 2 by slug presence; paired
bootstrap over items, 10,000 resamples):

| run | techniques P / R / F1 | 95 % CI of F1 − luna r1 | recall of gold rows ≥ 2 | cells ≥ 2 P / R / F1 | quotes verified | failed / empty / degraded | USD / 1k docs |
|---|---|---|---|---|---|---|---|
| luna r1 (open map) | 0.949 / 0.550 / 0.696 | — | 0.592 | 0.793 / 0.509 / 0.620 | 97.9 % of 189 | 0 / 4 / 25 | 2.87 |
| luna repeat 2 (open map) | 0.922 / 0.550 / 0.689 | [−0.045, +0.031] | 0.616 | 0.770 / 0.544 / 0.638 | 96.4 % of 196 | 0 / 4 / 25 | (cache-warm) |
| **luna, fixed-key roles** | 0.930 / 0.544 / 0.686 | [−0.051, +0.033] | 0.608 | 0.765 / 0.521 / 0.620 | 96.4 % of 196 | 0 / 4 / 25 | 3.50 (cold) |

Inside luna's own repeat noise on every metric (difference to r1 −0.010 against repeat 2's −0.008; presence agreement
with r1 0.834, repeat 2 with r1 0.866). Luna still takes the strict-schema 400 and answers on the degrade path on
every call (25 / 25), as D275 requires. The same four documents come out `empty` (an-0002, an-0003, an-0007, an-0008;
gold has no technique or extra technique on an-0007 and an-0008), so the new check is narrowed to the degenerate
shape (rows present, every roles map empty, nothing kept): luna's three earlier runs have 0 such answers, haiku's 23
of 25.

**claude-haiku-5.5 probe** (an-0001, an-0004, an-0006; runs `20261013T151511Z_all_c6fdf6f1`,
`20261013T151533Z_all_52c45700`, settled $0.0183): the Anthropic route now answers the strict request with a 400
too and haiku answers on the degrade path; 3 / 3 documents `ok` with rows (4, 5 and 12 rows incl. extras, every
quote verified, no `empty_roles_dropped`), techniques F1 0.778 / 0.643 on these documents (n too small to compare).
The hole is closed by the schema; the check is the backstop if a provider fills the map with nothing again.
