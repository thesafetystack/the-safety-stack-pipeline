# Labeling rubric v3: screen (scope decision, scope field, SR category and buckets on document text), era-free

v3 is `screen-v2.md` with the changes below (2026-10-21, pre-2020 scope review, round 2, D5); read v2 and apply them.
What changed: (1) the hard case "Narrow settings (era-free)" and its pointer under "Robustness, fairness, privacy";
(2) the screener's message may carry `Published:` and `Source:` lines before the text (hard case "Record lines"; the
prompt `screen-v13` sends them); (3) under "Theory far from deployed systems", a LessWrong or Alignment Forum post by a
known alignment author on decision theory with no visible AI angle is `borderline`, not `irrelevant`. Unchanged: the
rest of v2, including announcements, fundraisers and prize posts (E7; a clause meant to drop them was tried and found
inert, and was dropped) and the technique-provenance lineage (see the narrow-settings case). This file is a delta so
that v2's text has one copy.

## Change 1: the exclusion that ends "… predator-prey model)."

Append: "Exception: the venue-and-author rule under "Theory far from deployed systems"."

## Change 2: "Theory far from deployed systems", after "… could appear and the topic is in the MIRI lineage."

Append:

A decision-theory, logical-uncertainty or anthropics post on
LessWrong or the Alignment Forum (the `Source:` line, or the text, shows the venue) by a known alignment or
agent-foundations author, with no visible AI angle, is `borderline` (sr_only, `theory`), not `irrelevant`: the
venue and the author are the AI angle the text leaves implicit.

## Change 3: new hard case, before "Robustness, fairness, privacy"

**Narrow settings (era-free).** A narrow experimental setting does not by itself exclude a general safety method or
an AI-alignment argument: a toy task, a tabular or robotic agent, a narrow benchmark or a pre-2020 deep network is
not grounds for exclusion when the contribution is safe exploration, an explicit safety constraint,
interruptibility or corrigibility, reward misspecification or wireheading, low-impact agents, scalable oversight,
runtime shielding, formal safety verification of an AI policy, or robustness to adversarial inputs and
distributional shift framed as a safety problem. Judge the contribution and its intended applicability, under the
vocabulary of its time (Friendly AI, value alignment, value learning, safe interruptibility …). Ordinary
task-performance improvements and domain applications stay out (E3, E5). This rule is about the setting, not the
lineage: a general method of ML security, privacy, explainability or robustness demonstrated on a task-specific
model with no safety framing (adversarial training of an image classifier, a saliency method, differentially
private training) stays under "Robustness, fairness, privacy" below; a technique-provenance rule for that lineage
is drafted (the `-p4` prompt variants) but is not part of this rubric.

## Change 4: "Robustness, fairness, privacy", after "Out when it is accuracy engineering or a narrow model."

Append: "Robustness to adversarial inputs or distributional shift *framed as a safety problem* is the narrow-settings
case above, not accuracy engineering."

## Change 5: new hard case after "Robustness, fairness, privacy"

**Record lines.** The screener's message may carry, before the text, `Published: <year>` (the year of the document's
publication date) and `Source: <channels>` (the channels it was collected from). The year places the vocabulary of its
time (narrow settings above); the channels show a venue when they name one (`lesswrong`, `alignmentforum`, a lab or
organisation blog); `arxiv` is a repository and `openalex` and `semantic_scholar` are indexes, which say nothing about
scope.
