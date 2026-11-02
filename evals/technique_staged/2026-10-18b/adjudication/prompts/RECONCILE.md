<!-- Copied verbatim from the labelling workspace for provenance: the task prompt of one labelling
pass (L1, L2), the reconciliation (RECONCILE) or the verification adjudication (VERIFY-ADJ:
written for a Codex adjudicator; used, with an agent preamble, by a Sonnet subagent because Codex
was out of credits); {{ITEMS}} was filled per batch. -->

You are the gold RECONCILER for an AI-safety technique annotation gold set (taxonomy v0.9, 234 entries). Two independent labellers (X and Y) labelled each document against the same guide; X read document-first, Y menu-first. Your job: produce the final gold per document, deciding every disagreement from the document text with a written reason, and making a final completeness sweep. Recall and precision of automatic annotators will be measured against your output.

Files in the current directory: GOLD-GUIDE.md (the labelling guide), RULINGS.md (binding rulings; 41-46 are new and win), rubric-v0.4.0.md (role rubric; guide and rulings win), menu-v0.9-234.md (taxonomy), texts/<item>.txt (documents), labels/X/<item>.json and labels/Y/<item>.json (the two labellings).

Reconcile these documents: {{ITEMS}}

Procedure per item:
1. Read the text in full (page through long files), then both labellings.
2. Rows both gave (same slug): keep unless clearly wrong; reconcile roles (strengths per rubric; decide from the text when they differ) and choose the best verbatim quote (<= 200 chars, exact text).
3. Rows only one gave, and sibling choices (X slug a, Y slug b for the same passage): decide from the text. Keep a row when the document supports any role >= 1 under the guide and rulings; drop topic adjacency, ML-method-only rows, bibliography-only rows. Level choices (group / parent / child / variant for the same passage): pick the level the text supports; when genuinely ambiguous, keep the best as `slug` and list the other acceptable structurally linked slug(s) in `level_alternatives` (ruling 44) - never two rows for one passage at two levels unless the document separately treats both.
4. Completeness sweep: re-scan the text section by section AND the menu by kind; add any missed row (source "reconciler").
5. Per row: `confidence` (high: clearly present and slug clearly right; med: arguable presence or slug or level; low: borderline), `sources` (subset of ["X","Y","reconciler"]: who had this slug), and for every row not given by both X and Y a `decision` reason; for every row you dropped that one of them gave, an entry in `dropped` with a reason.

You cannot write files. Your final message must contain, for every item in order, one line `ITEM g2-NNNN` followed by one ```json fenced block holding exactly:
{"item_id": "...", "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
 "techniques": [{"slug": "...", "roles": {...}, "quote": "...", "confidence": "...", "sources": [...], "alt_slugs": [...], "level_alternatives": [...], "implemented_with": [...], "decision": "(only when not both X and Y) ..."}],
 "dropped": [{"slug": "...", "by": ["X"|"Y"], "reason": "..."}],
 "extra_techniques": [...as in the guide...], "menu_gaps": [...as in the guide...],
 "doc_note": "...", "disagreement_note": "one sentence on the main X/Y disagreement pattern in this item, or none"}
Nothing else. Do not abbreviate, do not skip items.
