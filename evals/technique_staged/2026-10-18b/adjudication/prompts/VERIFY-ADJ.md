<!-- Copied verbatim from the labelling workspace for provenance: the task prompt of one labelling
pass (L1, L2), the reconciliation (RECONCILE) or the verification adjudication (VERIFY-ADJ:
written for a Codex adjudicator; used, with an agent preamble, by a Sonnet subagent because Codex
was out of credits); {{ITEMS}} was filled per batch. -->

You are an independent adjudicator for an AI-safety technique annotation gold set (taxonomy v0.9, 234 entries). Two independent labellers (P and Q) labelled each document against the same guide. You receive only the rows on which they DISAGREE. Decide each disputed row from the document text.

Files in the current directory: GOLD-GUIDE.md (the labelling guide), RULINGS.md (binding rulings; 41-46 win over older ones), rubric-v0.4.0.md (role rubric), menu-v0.9-234.md (taxonomy), texts/<item>.txt (documents), DISPUTES.md (the disputed rows per item: slug, which labeller gave it, their quote, roles, confidence and reason, and the slugs the other labeller gave for nearby material).

Items: {{ITEMS}}

For each disputed row: read the relevant parts of the text (search for the quote and for the technique's names and mechanism; read enough context, and the whole document when the question is whether something is present anywhere). Answer whether the document involves that menu technique in any role at strength >= 1 under the guide and rulings (presence, not role grading):
- `yes` (the row belongs in the gold), `no` (it does not: topic adjacency, ML method only, bibliography only, wrong entry), or `borderline` (defensible either way);
- if the passage is real but the slug is the wrong entry or level, answer `no` for this slug and name the right slug in `better_slug` (it may be the other labeller's slug, or a parent / child / group of it); if the level is genuinely ambiguous, answer `yes` and list the acceptable linked slugs in `level_alternatives`.
Give a verbatim quote (<= 200 chars) supporting a `yes` / `borderline`, and a one-sentence reason in every case.

You cannot write files. Your final message must contain, for every item in order, one line `ITEM g2-NNNN` followed by one ```json fenced block:
{"item_id": "...", "verdicts": [{"slug": "...", "answer": "yes|no|borderline", "better_slug": null, "level_alternatives": [], "quote": "...", "reason": "..."}]}
with one verdict per disputed slug listed in DISPUTES.md for that item, in the same order. Nothing else.
