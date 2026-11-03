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

## Record (as written 2026-10-16)
The set's details and gold-gap analysis as recorded at the re-freeze; the status at the top supersedes them. Its run
commands are left out: `stack eval model_roles` refuses the set.

**Items.** `production_asks`: an-0023 newly asks, an-0022, an-0024, an-0025 make no call. `input.menu` is
`[{node_id, weak}]`, the judge's menu over the window. `stack eval` refused the set when production no longer asked
exactly these items or built other fills; the whole user message was compared only for the prompt the set was frozen
with, so another prompt (`--prompt model-roles-v2`) ran on the same set.

**Scoring against the v0.4-menu gold.** The scorer (`stack/evals/score.py`, model_roles) compares (node, role) cells
of the payload's `models` with gold `label.models`: a graded menu node without a gold row puts its cells ≥ 1 / ≥ 2
on the false-positive side (and the node, if any role ≥ 1); a gold row whose node is no longer on the menu leaves
its cells as false negatives the judge cannot reach. A new node the judge grades all-zero (a matcher false
positive) costs nothing.

**Gaps** (`gold-gaps.jsonl`, the 8 items whose menu moved; `manifest.json` `gold.gap_items`):

| item | split | menu nodes without a gold row | gold rows off the menu (graded: cells ≥ 1 / ≥ 2) |
|---|---|---|---|
| an-0003 | dev | claude-opus-5.5 | claude-opus-5 (all zero) |
| an-0005 | test | claude-opus-5.5, gpt-6.1-sol, jev-1.13, jev (weak) | claude-opus-5 (2 / 0), gpt-5.6-sol (2 / 0); gpt (all zero) |
| an-0010 | dev | claude-opus-5.5, glm-5.3, inkling-small, nemotron-3.5-lightning-30b-a3b, qwen3.8-27b, gpt-6-astra (weak) | glm-5 (2 / 2), nemotron-3 (3 / 3), qwen3.8-max (3 / 3) |
| an-0011 | test | gpt-6-astra (weak) | — |
| an-0017 | dev | gpt-6-astra (weak) | — |
| an-0019 | dev | claude-fable-5.1, claude-haiku-5.5, claude-opus-5.5, claude-sonnet-5.5, deepseek-v4.1-flash, embeddinggemma-2, gemini-4-argon, glm-5.3, gpt-6, gpt-6.1-sol, grok-4.6, kimi-k3-swarm, mistral-large-4, nano-banana-2.1, gpt-6-astra (weak) | claude-opus-5 (3 / 3), claude-sonnet-5, deepseek-v4, embeddinggemma, gemini, glm-5, grok-4, mistral-large, nano-banana-2 (2 / 0 each) |
| an-0021 | dev | inkling (weak) | — |
| an-0023 | test | gpt-6-astra, opt (weak) | — |

What the rows say: most graded gold rows that left the menu were graded on an older sibling **for the newer model
the text names** (the labellers' notes: "text names GLM 5.3 (no vocab node) … graded here per variant rule"; Opus 5.5
→ claude-opus-5, Sonnet 5.5 → claude-sonnet-5, Qwen3.8-27B → qwen3.8-max, Nemotron-3.5-Lightning → nemotron-3, GPT
6.1 Sol → gpt-5.6-sol, …), and v0.5 has a node for that model. So a judge that grades the new node as gold graded
the old one scores a false positive **and** a false negative on the same evidence. Moving those rows onto the new
nodes, and grading the real new mentions the gold left ungraded (an-0003 claude-opus-5.5, an-0021 inkling, an-0023
gpt-6-astra, an-0005 jev-1.13 and gpt-6.1-sol, an-0010 inkling-small, …; gold lists them only as `off_menu_models`
names), was left as an adjudication, not done here (2026-10-17a's gold remaps those rows). The weak `gpt-6-astra` on an-0010, an-0011, an-0017
("Astra Fellowship", a monitor named Astra) and `opt` on an-0023 should be graded all-zero (cost nothing).

Consequences on the whole set with `gold.jsonl`: 11 of the 194 gold cells ≥ 2 (31 of 325 ≥ 1) are on nodes off the
menu, hence the recall ceilings above; precision on the gap items is understated by whatever the judge grades on the
new nodes. The clean subset was the 17 items without a gold gap (an-0001, -0002, -0004, -0006, -0007, -0008, -0009,
-0012, -0013, -0014, -0015, -0016, -0018, -0020, -0022, -0024, -0025; 3 of them make no call). `scripts/packets.py
<out-dir> [item ids]` wrote labeller packets for the gap items (text, menu as the judge sees it, the rubric the
pinned prompt includes, a vocabulary index; nodes without a gold row marked).
