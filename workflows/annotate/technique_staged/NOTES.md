# annotate.technique_staged — notes

Status (2026-11-02): **in production** since plan `batch-2026-10a` revision 7 (D707-D710): prompt set
`technique-staged-v5-a1.1` in mode `single` with the gleaning turn (D708), doc_cap 900,000, max_tokens 64,000, on
the screen-relevant rows, against `taxonomy/techniques-1.1.0.yaml` since revision 9 (D738). The prompt sets were
chosen and checked on the frozen eval sets under `evals/technique_staged/` (D708; the later quality round D730, D735
left production unchanged). The stage runs in four modes (`single`, `sweep`, `staged`, `lexical`); the sections
below are the build's record (2026-10-17, decisions D601-D607, `docs/DECISIONS.md`). `configs/plans/ann-scratch.yaml`
still carries an older pin and stage line commented out with `#ts# ` markers. The current rules are the module
docstrings of `main.py` and `stages.py` and `prompts/README.md`.

| Module | What it does |
|---|---|
| `main.py` | the stage: plan pin (required `mode`; keys a mode does not use refused), prompt pin and digests, the calls of one ask per mode, post-processing (technique_roles' `judge.py` loaded by path, D605), status and partial flags, the merged evidence row; `user_message`, `prepass_messages`, `freeze_info` for the eval freeze |
| `stages.py` | pure pieces: P / S / G schemas, user-part fills, note cleaning (paragraph validation, cuts, centred snippet), chunk-overlap dedupe, ids, S validation, sub-menu assembly with pointers, the sweep call's reading notes |
| `taxonomy.py` | loads `taxonomy/taxonomy-v0.9.yaml` (strict schema, `ValueError` on drift); `menu_text` (117,046 characters), `compact_index` (one line per entry with `{child of: <parent>}`, 18,588 characters), `submenu_text` (full blocks with code-derived pointers, `note` lines for counts) |
| `segment.py` | `seg-v1`: 300k cap, numbered paragraphs, section labels, references span, body chunks (single <= 40k; else ~30k, max 45k, ~1k overlap), opening |
| `lexical.py` | `lex-v2`: matcher over display names (parenthetical parts removed, never split at ` / `), synonym and instance aliases; 375 surfaces on v0.9, stoplist `cascade`, `debate`; compiled once per taxonomy digest |
| `prompts/` | prompt set `technique-staged-v1` (default: prepass, selection, grading, single, sweep) and `rubric-v0.9-a.md`; set `technique-staged-v2` (post hoc, staged / lexical: selection-v2, grading-v2 with `rubric-v0.9-b.md`, v1's prepass, single and sweep files); set `technique-staged-v3` (post hoc, single: `technique-staged-single-v2.md`, the inventory-first contract with `rubric-v0.9-c.md`; v2's other files); the later sets (v4, v5, v6) in `prompts/README.md` |

Tests: `tests/test_annotate_technique_staged.py` (every mode through the stage runner with a scripted transport;
P splits and partial documents; S re-asks; cost merge; digests per input; pin validation; payload registry; the
inactive plan stanza validated uncommented; the judge file's sha256) and
`tests/test_annotate_technique_staged_foundation.py` (taxonomy, segmenter, lexical, converter).

## Choices made in the build (beyond DESIGN v2 and the round-2 changes)

- **Incomplete without rows** is stored as the failed call's error (`failed_permanent[schema_invalid | truncated |
  too_large]`), not `failed_transient` as DESIGN v2 §3.10 says: `failed_transient` admits only transport classes
  (`stack/schemas/vocab.py`), and no class names "a part could not be read". Consequence: such a document is not
  retried until its work key changes; its completed calls are in the response cache. Open: a new transient class
  (a vocabulary change) if retries matter.
- **`mode` is required** in the pin (no default): a missing mode is a configuration error, not the baseline.
- **Payload additions** beyond the brief's list: `prepass.doc_level` (S's document-level candidates, needed for
  stage-wise candidate accounting) and `prepass.calls` (stage, part, model, tokens, response digest per call, no cost:
  the payload stays the same on a cache replay; per-stage cost = tokens x `llm.yaml` price).
- **Pointer snippet** (C2): a ~240-character window centred on the surface in the first cited paragraph that contains
  it, else the first cited paragraph's head (not its first sentence).
- **Dedupe**: only a note of chunk k > 1 that cites overlap paragraphs alone and matches a chunk k-1 note (same
  normalised surface, same stance hint, shared paragraph); notes of one chunk are never merged.
- **Sub-menu over the cap with protected slugs alone**: every `high` / `doc_level` slug is kept (repair
  `submenu_over_cap:<n>`); the cap then binds only the other classes.
- **Rubric §3.6 vs precedent 14** (a modified form that is itself a child entry): graded on the child instead of the
  base, consistent with "specific over generic".
- **Group-node menu lines** carry `[group]` and `{children: ...}`; the selection prompt's wording was aligned with
  that (it said "marked `group node`").
- **Run count** `technique_rows` (the framework's own `rows` counts written rows).
- **Judge reuse**: `main.JUDGE_SHA256` pins technique_roles' `judge.py`; a test fails when that file changes.

## Eval (D606, D607)

- Set `evals/technique_staged/2026-10-17a` (36 items, gold on v0.9; `README.md` there), frozen with `freeze_info`.
- `stack eval technique_staged --set 2026-10-17a --models <grading model> --pin mode=<mode> [--pin prepass_model=...]`
  (`stack/evals/annotators.py` `TechniqueStagedAdapter`): P/S responses in `cache/shared/r<k>`, the final call in the
  arm's cache; rows carry per-call detail and `standalone_cost_usd`. Scoring: `stack eval score`, `staged-compare`,
  `staged-table` (`stack/evals/technique_staged.py`).
- Gold revision `evals/technique_staged/2026-10-17b` (D610): 17a's items and texts, gold with the blind audit's
  verdicts applied, `dont_care` rows (D611: out of the denominator, a prediction of the slug neither TP nor FP).
  A run made on 17a is scored on 17b with `--set-dir evals/technique_staged/2026-10-17b` on `score` (with
  `--summary summary-2026-10-17b`, so the 17a summary is kept), `staged-compare` and `staged-table`; the items
  digests must match.

- Windowed single (D614) and the gleaning turn (D615): single-mode pin keys `window: true` / `glean: true` (prompt set
  v3; in the eval `--pin window=true`). Window parts come from `segment.windows` (body only, target 45k, max 60k
  characters, one paragraph of overlap; P's chunks keep their own sizes); counts `windows`, `glean_added` and the
  repairs `glean_added:<slug>` record what the options did. The eval labels every such call stage `single` (one
  arm cache); the payload's `prepass.calls` keeps `window` / `glean`. The eval estimate counts only the first call.
- Reasoning effort (D612): pin `role: extract_staged_low` or `extract_staged_high` (in the eval,
  `--pin role=extract_staged_low`). The role's `reasoning_effort` is sent as OpenRouter's `reasoning: {effort}` and
  is in the call's params, so the arm's pin digest, the template digests and the response-cache keys differ from
  the default role's; the default role sends and hashes exactly as before.

## Not done

- Batch mode: a staged document makes several dependent calls; `Deferred` per chunk call is not handled (DESIGN §7.7).
- `reasoning_effort` per call type: effort is per role (D612: `llm.yaml` role field, roles `extract_staged_low` /
  `extract_staged_high`), so every call of one ask runs at the same effort.
