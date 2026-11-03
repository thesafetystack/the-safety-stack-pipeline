# technique_staged eval set 2026-10-28a: the held-out judge-set documents, frozen for the effort-high confirmation

Status: protocol set, closed (2026-10-28). The confirmation ran under the frozen protocol; production stayed on its
setup by decision (D735).

The 100 documents of `evals/judge-2026-10-25j/technique_staged` (item ids `jtec-0001` .. `jtec-0100`, same doc ids and
texts), frozen for `stack eval technique_staged` with the production prompt set `technique-staged-v5-a1.1` and
taxonomy 1.0.0, so `stack eval` checks every item's single user message. Purpose: the test-set confirmation step of
the 2026-10-27 technique round; the frozen protocol is `PROTOCOL.md` / `protocol.json` (committed before any test
label was read).

## Where the set lives
One text is `restricted`, so **no text is in the repository**. This directory holds `items.jsonl`, `manifest.json`,
the protocol and the scripts. The complete set (the same two files, `texts/jtec-NNNN.txt.gz`, an empty `gold.jsonl`)
is kept in a private directory outside the repository, written by `scripts/freeze.py <STORE_DIR> <private evals
directory> --write` (without `--write` it checks both copies byte for byte). Run with `--evals <private evals
directory> --set 2026-10-28a --split all`. The set has no gold: the references
are the judge set's labels and the adjudicated reference the protocol describes.

## Items
`input` = title, basis_role, text_digest (the store blob sha256 = the judge item's `text_sha256`), full_text_digest,
full_text_chars and `staged` = `main.freeze_info(text, name="technique-staged-v5-a1.1", taxonomy_path=1.0.0)`;
`input_digest` = sha256 of the single user message (default 300,000-character cap: one text, jtec-0002, is longer;
`manifest.json` `truncated_items`; the production pin's doc_cap is 900,000, so production read it whole);
`stratum` = {group: the judge set's doc_type stratum, band: length band}; `weight` = the judge set's design weight;
every item `split: test`.

## Results (2026-10-28) and deviations from the protocol
`results/scores.md` / `scores.json` (`scripts/confirm.py score`) and `results/reference-t.jsonl` (ref-t, slug level:
verdict, rule that decided it, judge support, calibration flag, severity votes, the two adjudicators' verdicts; no
text). Summary and the full record of the round: `docs/design/technique-quality-2026-10.md` (D735). Verdict under the
frozen rule: effort high **confirms** on ref-t (ΔF1 +.044 [+.022, +.068], ΔP +.009, median latency 147 s), robust
reading; production unchanged by decision.

Deviations: none in the arms, metric, references or rule. Implementation notes: `scripts/confirm.py` was written
after the protocol commit and checked against the judge set's committed production-vs-judge counts before ref-t
existed; its reference-count labels were corrected once (cosmetic: `3/3` units were printed as "3 split"; no
verdict changed). One Sol adjudication batch failed with "model at capacity" and was rerun unchanged; the
fallback (majority reference) was not needed. Private working files (packets, answers, quotes) stay outside the
repository.
