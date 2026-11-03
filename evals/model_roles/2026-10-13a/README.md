# Annotator eval set 2026-10-13a: model_mentions and model_roles (technique_roles: `evals/technique_roles/2026-10-13a`)

**Superseded for model_roles by `evals/model_roles/2026-10-17a`**: this set's menus were frozen with vocabulary v0.4
under the pre-merge stage (before D591), so `stack eval model_roles` refuses it. It is kept as the labelling record
(protocol, labeller and adjudicator files, agreement) whose gold, remapped, is 2026-10-17a's. The technique_roles
half (`evals/technique_roles/2026-10-13a`, same item ids) is separate. `scripts/freeze.py` and `scripts/packets.py`
import code removed by D591 and no longer run.

## Set
25 documents, all `relevant` in screen set `evals/screen/2026-10-12a` `gold-v2.jsonl` (production scopes the
annotators to screen-relevant rows); whole texts copied byte-identical from that set (not in the repository, D790:
`tools/fetch_eval_texts.py` fetches them into the eval-text cache). 21 have ≥ 1 strong model mention, 4 have none.
Channel groups forum 11, paper 7, url 7; licence open 18, public_web 7; 900k characters in all (1k-219k per doc).
`split` dev/test 14/11; every number below is over all 25. `input.model_mentions` = `scan_payload` (models.yaml v0.4,
sha in `manifest.json`).

## Gold labels
Protocol: `labels/labelling-instructions.md` (task definitions = the production rubrics, the production model menu
per document, the technique catalogue, a vocabulary index). One pass per document gives all three labels:
`mentions` (vocab node ids, most specific node named; `mentions_out_of_vocab`), `models` (one row per menu node, 10
roles 0-4; all-zero = matcher false positive / subsumed), `techniques` (catalogue slugs, 12 roles 0-4, verbatim
quote). Labellers: Opus "A" on all 25; second labeller Codex `gpt-6-sol` on 3 documents, then (Codex out of
credits) an independent second Opus agent on the other 22. An Opus adjudicator per batch saw both labellings and a
diff (`labels/adjudication-instructions.md`); gold = the adjudicated label (`scripts/merge_gold.py`). All quotes
verified verbatim (`scripts/check.py`). Agreement (`scripts/agree.py`):

| pair | docs | mentions F1 | model cells ≥ 2 F1 / κ | technique slugs F1 | technique cells ≥ 2 F1 |
|---|---|---|---|---|---|
| Opus vs Codex | 3 | 1.000 (17/17) | 0.913 / 0.905 | 0.667 (11 both, 10 Opus only, 1 Codex only) | 0.642 |
| Opus vs Opus | 22 | 0.983 | 0.950 / 0.944 | 0.920 | 0.873 |

Opus-vs-Opus agreement is inflated by a shared model; the cross-family pair says technique presence is the soft
part. Base rates: 136 in-vocab gold mentions + 43 out of vocabulary; 186 menu nodes, 52 of them all-zero in gold;
194 model-role cells ≥ 2.

## Results (2026-10-13)

**model_mentions** (no LLM; `stack eval model_mentions`). The 43 out-of-vocabulary gold mentions led to vocabulary
v0.5 (D292: 25 nodes for 28 of the names; `vocab-v0.5-oov-map.jsonl` maps each (item, name) to its node or the reason
it was not added; `gold-vocab-v0.5.jsonl` is `gold.jsonl` with the mapped names moved into `label.mentions`, via
`scripts/apply_vocab_map.py`). Both vocabularies on the v0.5 gold (164 gold nodes):

| prediction | v0.4 P / R / F1 | v0.5 P / R / F1 |
|---|---|---|
| strong | 0.801 / 0.713 / 0.755 | 0.903 / 0.854 / **0.878** |
| strong ∪ weak | 0.693 / 0.799 / 0.742 | 0.778 / 0.963 / 0.861 |
| strong, lenient | 0.925 / 0.793 / 0.854 | 0.987 / 0.945 / 0.966 |
| strong ∪ weak, lenient | 0.815 / 0.823 / 0.819 | 0.872 / 0.994 / 0.929 |

The old strong false positives (an older sibling matched inside a newer name, e.g. `claude-opus-5` inside "Opus 5.5")
went because the longer name now has a node (longest span wins), not by a matcher change.

**model_roles** (production `ask`, prompt `model-roles-v1`, one repeat; luna repeat 2 for the noise):

| model | cells ≥ 2 P / R / F1 | 95 % CI of F1 − luna | cells ≥ 1 F1 | nodes F1 | failures | USD / 1k docs |
|---|---|---|---|---|---|---|
| openai/gpt-6-luna (pinned) | 0.805 / 0.768 / **0.786** | — | 0.856 | 0.911 | 0 | **3.24** |
| openai/gpt-6-luna, repeat 2 | — / — / 0.754 | [−0.104, +0.048] | — | — | 0 | (cache-warm) |
| openai/gpt-6-luna-pro | 0.753 / 0.768 / 0.760 | [−0.072, +0.032] | 0.848 | 0.918 | 0 | 5.86 |
| anthropic/claude-haiku-5.5 | 0.804 / 0.742 / 0.772 | [−0.135, +0.077] | 0.763 | 0.814 | 1 truncated (an-0019, 16k then 32k) | 13.14 |
| openai/gpt-5.6-luna (v3's) | 0.680 / 0.799 / 0.735 | [−0.118, +0.052] | 0.833 | 0.897 | 0 | 6.27 |

Luna's own run-to-run agreement on ≥ 2 cells is F1 0.879: every candidate is inside that noise; luna stayed.
