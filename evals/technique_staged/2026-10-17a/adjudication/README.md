# Gold provenance: technique_staged 2026-10-17a

The gold (`../gold.jsonl`) is LLM-made: two independent labellings per document, then one reconciliation with
written reasons. No human labelled or checked it (see Limitations).

## Files
- `adj-A.jsonl` (labeller A) and `adj-B.jsonl` (labeller B, with `labeler` per item): the two independent
  labellings, one object per item as the labellers returned them (`techniques[{slug, roles, quote, confidence,
  reason, implemented_with, alt_slugs}]`, `extra_techniques`, `menu_gaps`, `doc_kind`, `doc_note`). Kept for
  agreement statistics and provenance; not scored.
- `reconciled.jsonl`: the reconciler's final labels (per row `sources` = which labellings supported it: `A`, `B`,
  `old` = the 2026-10-13a gold of an-0001..an-0025, read only after the reconciler's own decisions, `reconciler` =
  added in its completeness sweep; `decision` = its written reason where it changed or merged rows; `dropped`
  rows with reasons, `remap_decisions`, `doc_note`). `../scripts/freeze.py` converts it to `../gold.jsonl`.
- `texts.sha256`: sha256 of the 36 texts the labellers read; the freeze checks them against the frozen texts (all
  identical).
- `GUIDE.md`: the labelling guide v1.1 the labellers and the reconciler followed (completeness first; group nodes
  only for umbrella mentions; generic ML methods under `implemented_with`, not rows; bibliography-only names get no
  row; a document's own new method within an entry's definition is `introduced` on that entry).
- `RULINGS.md`: 40 ambiguities the labellers reported, with the rulings (`[R]`) the reconciler applied as binding.

## Who labelled
- **A**: an Opus subagent, all 36 documents, in 8 size-packed batches (`opus-a`).
- **B**: Codex gpt-6-sol (`codex-sol`, 14 documents: batches B1, B3, B5, B7) and Codex gpt-6-astra (`codex-astra`,
  19 documents: B2, B4, B6, B8); for the three abstract-only items an-0001, an-0002 and an-0025 (public status not
  verified, so not sent to an external labeller) a second, independent Opus subagent (`opus-b`).
- **Reconciler**: Opus (`opus-reconciler`), per document with both labellings, the guide and the rulings, a written
  reason per contested row and a final completeness sweep over the menu.
- Menu: taxonomy v0.9 (214 entries) + 9 group nodes + an appendix of 20 proposed entries labelled `candidate:<slug>`
  (proposals of a later taxonomy review, not admitted when the gold was made).

## Agreement and base rates (computed from the files here; `tests/test_eval_technique_staged.py` re-checks them)
- A-vs-B slug agreement F1 0.796 over 36 docs (296 slugs in both, 93 A only, 59 B only); A vs codex-astra 0.824
  (19 docs), A vs codex-sol 0.767 (14 docs), A vs opus-b 0.783 (3 docs).
- Final gold: 406 rows on 36 documents (245 with a role >= 2; 4 documents with none); 16 extra techniques, 68 menu
  gaps. Rows given by both labellers 297, A only 68, B only 31, reconciler / old gold only 10.
- Confidence: high 150, med 169, low 87. Kinds: assessment 173, intervention 151, monitoring 54, group node 20,
  `candidate:` 8 (excluded from slug scoring). Primary scoring universe (no group nodes, no candidates): 378 rows,
  of which 279 given by both labellers and 147 high-confidence.
- Reconciliation pattern (reconciler notes): B grades higher and adds marginal evaluation rows; A adds
  low-confidence inferred rows and is more complete on commentary; the main systematic correction was the
  new-method -> `introduced` ruling.

## Limitations
- LLM-only gold, no human pass; a stratified human spot-check was recommended and has not been done.
- Labeller A and the reconciler are the same model family (Opus); on the three abstract-only items both
  labellings are Opus.
- Three items are abstracts only (an-0001, an-0002, an-0025); an-0025's text is truncated mid-sentence as frozen.
- One process slip: on an-0025 the reconciler saw the old gold together with A and B (noted in its `doc_note`).
- The eight pilot items of batch B8 were labelled by A under guide v1.0 (v1.1 added `alt_slugs`, `menu_gaps`, the
  classifier-as-implementation rule and first-party deployment statements); the reconciler applied v1.1.
- `candidate:` rows follow those review proposals; when entries are admitted to the taxonomy the gold can be remapped
  (`candidate:<slug>` -> the admitted slug) and the set re-frozen.
