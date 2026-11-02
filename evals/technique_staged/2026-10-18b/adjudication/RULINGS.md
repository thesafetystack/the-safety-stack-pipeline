<!-- Copied verbatim from the labelling workspace for provenance: the binding rulings the
labellers and the reconciler applied (1-40 from 2026-10-17a's labelling, 41-46 new for gold
v2). -->

# Binding rulings for gold labelling (taxonomy v0.9, 234 entries)

Rulings 1-40 come from the previous gold round; the item ids in them (an-00NN) name that round's documents and are only context. [R] marks the binding ruling. Where a ruling mentions a `candidate:<slug>`, that candidate is now a full menu entry with the same slug (use the slug itself). Rulings 41-46 are new and win over older ones.

From A-pilot (B8):
1. Classifiers used only to filter pretraining data (an-0009) -> [R] guide v1.1: grade the data-filtering entry, classifier under implemented_with.
2. Midtraining on synthetic aligned-AI documents: spec-value-midtraining vs safety-pretraining-data-augmentation-upsampling (an-0011) -> [R] pick by whether a specific spec/value set is described; other in alt_slugs.
3. Formal proof checking as trust basis for AI-produced proofs/code (an-0023): automated-reasoning-checks vs formal-verification-gsai -> [R] automated-reasoning-checks if per-output checking; record menu gap otherwise.
4. Generic misalignment-propensity evals lack an entry (an-0011) -> [R] menu_gaps.
5. Lab safeguard announcements: first-party current practice = attested_use (v1.1).
From adj-B (abstract items):
6. Translation/code-switch attacks on harmful benchmarks: jailbreak-robustness-eval (bypass transformation) vs harmful-request-evaluation -> [R] jailbreak-robustness-eval when the transformation is the object; harmful-request-evaluation when the benchmark is used plainly.
7. Generic "safety alignment / safety data": pick safety-sft-refusal-training if refusal/harmlessness training is meant; group node only for the umbrella.
8. Umbrella critique ("turn-level safety evaluation" as a whole) -> [R] critiqued on the entries the critique clearly targets, else none.
9. Paper's own proposed architecture partly on menu: menu parts considered/introduced as the text supports; off-menu parts as extras (one per named component).
10. an-0025 is not abstract-only and is truncated mid-sentence (text as frozen).
From A B4:
11. One training run fitting several intervention entries (guard reward in safety RL: rlaif / rule-based-reward-models / adversarial-training) -> [R] several rows allowed when the text supports each mechanism; the counting rule only collapses child vs generic parent.
12. Safety benchmarks used to evaluate a classifier (not a chat model) -> [R] no eval-entry row unless the eval-type entry's definition covers classifier evaluation; note in reason of the classifier row.
13. Group node as only fit for an interpretability methods note -> [R] allowed, roles as the text supports (group node may hold 3-4).
14. Experimental setup that is in effect a robustness test (harmful fine-tuning share) -> [R] grade the eval-type entry as used when the paper measures robustness under it.
15. Overlapping agent-control entries (allow/deny rules, approvals, shielding) -> [R] rows for each entry whose definition the text clearly meets; alt_slugs when only one is meant.
From A B1:
16. Document's own new method that is an instance of a menu entry vs a new framework (rubric precedent 6) -> [R] if the method falls within a menu entry's definition, grade that entry with `introduced` (+ evaluated); add an extra ONLY when the method goes beyond every entry's definition (then extra introduced + the base entry used/modified). Score on menu rows.
17. Probes used as a measuring instrument inside an assessment -> [R] implemented_with of the assessment row; interpretability-based-model-auditing only if the doc frames it as auditing the model's internals.
18. Agentic Misalignment-style scenarios -> [R] scheming-deceptive-alignment-evaluation unless the doc frames as shutdown/self-preservation; alt_slugs for the rest.
19. Staged rollout of a safety feature (not a model) -> not staged-structured-release.
From A B6:
20. Activation steering used for non-safety purposes (inducing behaviour, capability baseline, concept injection probe) -> [R] implemented_with only (generic method use), unless the steering targets a safety-relevant behaviour.
21. Evaluations used as test data for a monitor -> [R] the eval entries get `used` rows only if the paper runs them as evaluations; as monitor test sets, fold into the monitor row (+ monitor-robustness-eval if the monitor itself is evaluated adversarially). "Trusted monitoring" requires a weaker/trusted model monitoring a stronger one, named or clearly described.
22. Third-party evals reported in a first-party card -> used + attested_use (as adjudicator did).
23. Self-exfiltration propensity -> scheming eval; capability -> ARA child.
24. Vague first-party mitigation bullets -> map to nearest entry only when the definition is clearly met; else menu_gaps; low confidence where mapped.
From A B3:
25. Steering vectors as experimental stimulus (not safety) -> see 20: implemented_with / no row.
26. Group node as a paper's main method (circuit study) -> see 13: allowed with roles as supported.
27. Commentary relaying a first-party deployment -> cited_work (+mentioned); suspected_use only for inferred third-party claims.
28. Names whose mechanism the text never describes -> no outside knowledge beyond the menu's listed instances; low confidence at most, prefer no row.
29. Research questions in an agenda post -> considered for the eval entry when the post proposes running it.
From A B7 / B2:
30. Paper's own new method within a menu entry: [R] see 16 - `introduced` on the menu row (rubric precedent 6 is overridden for v0.9 gold); `modified` when the paper changes an existing named method; extras only beyond every definition.
31. Row flags (confirmed/recommended/rejected) have no field -> fine, not scored.
32. Generic "jailbreak protections / deployment safeguards" -> menu_gaps unless a layered stack is described.
33. Eval procedure + evaluated object: both rows when distinct entries and the text supports each (e.g. monitor-robustness-eval + internal-state-monitoring).
34. Paper titles cited in the body (commentary) -> mentioned/cited_work 1 when the title names the technique; no row when only topic-adjacent.
35. Mechanism vs domain entries (probe for cyber misuse: internal-state-monitoring + cbrn/high-risk-domain classifier) -> both rows when each definition is met.
36. Search procedure inside a larger evaluation (automated red-teaming inside an audit) -> its own row (used) when the procedure is described.
From A B5:
37. Scheming generic vs specific siblings: menu says specific child replaces generic; [R] add generic scheming row only if the document treats scheming evaluation as a whole beyond the specific evals.
38. Persuasion: rating-based persuasiveness eval -> persuasion-manipulation-eval only if manipulation tactics; else candidate:persuasion-capability-eval at med; else menu_gaps.
39. In-context safety directives/skills -> system-prompt-behavioral-steering.
40. A document's own new benchmark: eval-type entry `introduced` (per 16/30), not extras, unless no eval-type entry fits.

New rulings (gold v2):
41. A document's own new method, benchmark or protocol that falls within an existing entry's definition -> `introduced` on that entry (plus `evaluated` / `used` as supported). Extras only when it goes beyond every definition.
42. Safety evaluations reported in a lab's own system / model card (run by the lab or a third party for the release) -> `used` + `attested_use` on the evaluation entries, strengths as the card's treatment supports.
43. Group nodes are generic-mention targets only (a bare field or umbrella name, no specific child applies); they are scored separately. Do not add a group node beside a child that already covers the passage.
44. Level ambiguity (NEW). The taxonomy has levels linked by structure: group node -> technique (`{children: ...}` of a group), parent technique -> child technique / variant (`{child of: ...}`, dotted slugs such as `dangerous-capability-evaluation.ai-rnd-acceleration`). Levels are NOT interchangeable: pick the level the text supports. When the right level is genuinely unclear (e.g. the text says "control protocol" and could mean the umbrella or the one protocol it later sketches; a bio eval that might be only a generic dangerous-capability eval), choose the best level as `slug` and list the other acceptable level(s) in `level_alternatives` (only structurally linked slugs: the parent / child / group of `slug`), confidence `med`, and say why in `reason`. Do not use `level_alternatives` for sibling choices (two different techniques): those go to `alt_slugs`.
45. Entries marked (provisional) are full menu entries; label them like any other.
46. Off-topic or non-safety documents (the screen let them through) get no rows unless they genuinely involve a menu technique; an empty `techniques` list is a valid answer. Say so in `doc_note`.
