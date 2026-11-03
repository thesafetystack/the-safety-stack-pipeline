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

## Record (as written 2026-10-13)
The measurements and findings as recorded when the set was labelled and run; the status at the top supersedes them.
The runs are in a local evals store (not in the repository).

**Purpose.** A quick eval: do newer models bring direct improvements, measured on a small set that can be run quickly
(a more thorough iteration and eval to follow).

**What the earlier pipeline had (no reusable gold).** No human gold for any of the three annotators. model_roles: a
calibration of 10 documents × 5 judges, anchor = median of the five (its notes are not in this repository); exact
agreement with the anchor opus-5 79.5 %, gpt-5.6-terra 79.3 %, gpt-5.6-luna 68.8 %, sonnet-5 48.2 %,
deepseek-v4-flash 36.7 %; ≥ 2 recall terra/luna 0.94; $/doc terra 0.047, luna 0.0049. Production took gpt-5.6-luna on
cost (2026-08-02), never re-measured. technique_roles: 23 documents, leave-one-out F1 luna 0.76, tencent/hy3 0.75,
terra 0.62, deepseek-v4-flash P 0.81 R 0.51; a union of deepseek-v4-flash + luna-pro + hy3 was recommended.
model_mentions: no eval. The earlier documents carry that pipeline's source ids and model-consensus outputs only, so
this set is new.

**Set details.** Production scoped the annotators to stack_relevance = relevant (D269); store head `rcprvcnepyb1g`
(that of the screen set). The 21 items with a strong model mention are every such relevant item of the screen set
except scr-0077, a 564k-character system card: beyond model_roles' 300k window and too long to label. For the 4
without one, production answers model_roles `empty` without a call; they still test mention recall and technique
rows. `split` dev/test alternates by length within channel group. Frozen by `scripts/freeze.py` with production code;
`input_digest` = sha256 of the production user message; `stack eval` refused the set once production code no longer
produced it.

**Labelling.** Packets built by `scripts/packets.py`; the Opus labeller A was a sub-agent. Codex labelled batch 01 (3
documents) and then ran out of credits ("Your workspace is out of credits", 13:00 UTC), so the second labeller on the
other 22 is an independent second Opus agent ("opus2", same packet, no sight of A). Adjudicated labels
`labels/adjudicated-batch-*.jsonl`. The adjudicator sided with A on most contested elements in batch 01 (40 of 47 vs
Codex 3, third answer 4): a same-family preference is possible. Contested documents: model_roles 15/25 (57
elements), technique_roles 19/25 (129).

**model_mentions on the original gold** (no LLM; `stack eval model_mentions --set 2026-10-13a --split all`), micro
over (item, node):

| prediction | P | R | F1 |
|---|---|---|---|
| strong | 0.801 | 0.860 | 0.830 |
| strong ∪ weak | 0.693 | 0.963 | 0.806 |
| strong, lenient (ancestor = match) | 0.925 | 0.956 | 0.940 |

43 gold mentions are not in the vocabulary (v0.4, 2026-08-02): newer releases (e.g. Opus 5.5, GPT-6 Astra) are
missing, and their names produce strong false positives on an older sibling (`claude-opus-5` inside "Opus 5.5" on
an-0003, an-0005, an-0019). Strict FNs are mostly family-vs-model granularity (the lenient row).

**Vocabulary v0.5** (2026-10-13, D292). Not added (15): products and tools (Claude Code × 3, Dots, Grok Bot, Artex AI,
Pangram, Hark.com), a hypothetical (Claude 8 Pantheon), a described classifier, and bare names the text gives no
family for (Samuel's checkers learner, Beam, Griffin, Sydney, HIPM). Both vocabularies were scored on the v0.5 gold
(`stack eval model_mentions --set 2026-10-13a --split all --gold gold-vocab-v0.5.jsonl`, v0.4 through
`STACK_CONFIG_DIR` on a copy of `configs/` with the old file; runs `20261013T152334Z_all` (v0.4) and
`20261013T152344Z_all` (v0.5); table in "Results" above). The old strong false positives that went: `claude-opus-5` on
an-0003, an-0005, an-0019; on an-0019 also `claude-sonnet-5`, `deepseek-v4`, `embeddinggemma`, `gemini`, `glm-5`,
`grok-4`, `mistral-large`, `nano-banana-2` (an-0019 has no strong FP left); on an-0010 `glm-5`, `nemotron-3`,
`qwen3.8-max`. A `.<digit>` end guard in the matcher was tried and broke 65 of the 4,827 fixtures carried over from
the earlier pipeline, which fold point releases into the major node on purpose (`claude-1.3` → `claude-1`,
`imagen-3.0-generate-001` → `imagen-3`), so an unknown future point version ("Opus 5.7") would still resolve to its
older sibling. New weak hits: bare "astra" (GPT-6 Astra) also hits "Astra Fellowship" (an-0010, an-0017) and "a
monitor named Astra" (an-0011). Left as strict FNs: `gpt-6.1-luna` on an-0019 (the text writes only "GPT-6.1 Sol and
Luna"; bare "luna" stays weak on gpt-5.6-luna), and `gpt-6-astra` / `inkling` where the texts use only the bare,
weak name.

**model_roles runs.** Run `20261013T131209Z_all_a1862291` (role `extract_main`, model overridden); luna repeat 2 in
`20261013T132759Z_all_ff4a25ff`. Luna's F1 against gold moves 0.786 → 0.754 between repeats. haiku-5.5: every call is
a strict-schema `http_400` then the degraded retry, 4 truncated attempts, ≈ 4× luna's cost. $/1k = settled OpenRouter
cost × 1000/25 (4 documents make no call).
