# model_roles eval set 2026-10-16a: set 2026-10-13a re-frozen for the merged stage (D591)

**Superseded by `evals/model_roles/2026-10-17a`**: frozen for the merged stage before registry version 3 (every
document judged) and before vocabulary v0.7, so `stack eval model_roles` refuses it; kept as history.

Since D591 `annotate.model_roles` scans the document itself with the current `configs/models.yaml` (the former
`annotate.model_mentions` merged in). Set `2026-10-13a` froze its menus with vocabulary v0.4, so 8 of its 25 items
no longer passed the input check under v0.5; this set is the same 25 documents re-frozen with vocabulary v0.5
(`7672a43bac3a…`) and prompt `model-roles-v1` by `scripts/freeze.py` (no LLM call). Whole texts are not in the
repository (D790; `tools/fetch_eval_texts.py`).

## Items
Same items as 2026-10-13a (`item_id`, `doc_id`, split dev/test 14/11, stratum, licence). Per item:
`input.model_mentions` (the scan), `input.menu` and `input.beyond_window`, `production_asks` (a strong mention: 22
items), `input.fills_digest` (sha256 of the prompt-independent fills: window, truncation, menu) and `input_digest`
(sha256 of the whole user message under `model-roles-v1`).

## Gold
`gold.jsonl` = 2026-10-13a's `gold-vocab-v0.5.jsonl`, byte for byte: mentions aligned to vocabulary v0.5, model
role grades as adjudicated over the **v0.4 menus**; no label was added or changed. The 8 items whose menu moved are
listed in `gold-gaps.jsonl` (`manifest.json` `gold.gap_items`): most gold rows that left the menu were graded on an
older sibling for the newer model the text names, which v0.5 now has a node for, so a judge that grades the new node
scores a false positive and a false negative on the same evidence. Consequence: recall at ≥ 2 cannot exceed 0.943
(≥ 1: 0.905); comparing prompts or models against each other on the set stays fair, absolute numbers are not
comparable with 2026-10-13a's. 2026-10-17a's gold remaps these rows onto the new nodes.
