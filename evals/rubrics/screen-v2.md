# Labeling rubric v2: screen (scope decision, scope field, SR category and buckets on document text)

v2 of `screen.md` (2026-10-12, track E2). What changed: the corpus now serves two readers, the Stack catalogue
(defence-in-depth technical safety, unchanged) and the yearly *Shallow Review of Technical AI Safety* (SR). Scope is
the union. Labels gain `scope`, `sr_category` and `sr_agenda`. Source: `docs/scope/shallow-review.md` (criteria
I1-I10, E1-E10, flips (f)), Tomáš's labels on the calibration slice `evals/calibration/2026-10-12a.md` (quoted as
"Tomáš, tri-NNNN"); `triage-v2.md` has the same scope text for catalogue records.

## The corpus and your job

The corpus is assembled broadly by keyword sweeps and then filtered. Two readers use it:

- **The Stack catalogue** reconstructs the AI safety stack as practised: which safety techniques exist, how mature
  each is, which developers and models use them, where public knowledge runs out. Every claim traces to a quote, so
  a vendor blog, a forum post or an unreviewed preprint can be the only public evidence that a safeguard is deployed.
- **The Shallow Review (SR)** maps technical AI safety research by *research agenda*: "technical work intended to
  prevent future cognitive systems from having large unintended negative effects on the world" (capability
  restraint, instruction-following, value alignment, control, risk awareness). It also keeps research that is far
  from deployed systems: agent foundations, decision theory, pre-paradigmatic and conceptual work, field critiques,
  anthropics and acausal trade, and prospective directions from psychology, neuroscience, biology, ecology,
  behavioural economics and game theory.

The screen reads a document's text and decides whether it belongs to the corpus; only documents it keeps are read
closely later, so a wrongly dropped document is lost silently. It also records which reader the document serves
(`scope`), where it sits in SR's taxonomy (`sr_category`, `sr_agenda`), and coarse `buckets` that decide which
later readers see it. Judge **scope, never quality** ("Quality is unclear but the topic is clearly relevant",
Tomáš, tri-0010).

For each item you see its `item_id`, the title, and the opening of the document's text (about 8,000 characters).
Often the text is only a title and abstract. Judge what you were given; do not count against a document detail that
its visible part would not be expected to carry. An alignment motive often appears only in the introduction or the
conclusion: look for it.

## What is in scope: two routes

**Route A (Stack, `core`): safety techniques.** A **safety technique** is a method, protocol, or safeguard applied
to an AI system (a general-purpose model, a foundation or multimodal model, an AI agent or agentic system) to make
its behaviour safer, more honest, more controllable, more transparent, or harder to misuse, or to measure any of
these: preference and alignment training, refusal training, input/output classifiers, interpretability methods,
chain-of-thought or agent monitoring, AI-control protocols, sandboxing and least-privilege tool access, unlearning,
watermarking and content provenance, weight security, tiered access and KYC, dangerous-capability and other safety
evaluations, red-teaming, jailbreak and prompt-injection defences, bias and privacy-leakage evaluation or mitigation
for such models, theory aimed at aligning such systems. A document is in by route A when it tells a reader
something about one or more such techniques:

- **Method** — proposes, describes, implements, refines, or teaches one, at any maturity from theory to production.
- **Evidence** — how well it works, what it costs, eval and benchmark results, red-team outcomes, failures,
  incidents, replications, negative results, released code.
- **Deployment** — some organisation uses, used, dropped, or declined it (system cards, model or release notes,
  safety frameworks and policy commitments, filings, job ads, talks, offhand remarks by practitioners). Indirect
  evidence counts: "We are also going for indirect evidence of what is deployed, what are the issues, what works vs
  not" (Tomáš, tri-0011). A frontier developer's governance or framework document is relevant even when it serves
  mostly as context (Tomáš, tri-0109).
- **Judgement** — critique, commentary, expert opinion, forecasts, retrospectives, arguments about an agenda.
- **Field structure** — surveys, reviews, taxonomies, how techniques combine or substitute for each other.

"Theory aimed at aligning such systems" means theory *of a technique* (a guarantee for debate, a bound on reward
hacking under RLHF, a formal model of a monitoring protocol). Theory about idealised agents, decision theory,
abstractions or agency with no technique in view is route B.

**Route B (SR only, `sr_only`): understanding or aligning advanced AI, no technique required.** A document is in by
route B when it contributes, at the level of a research agenda, to understanding, aligning, or safely developing
advanced AI, even when it names no technique and nothing in it applies to a current model. The kinds:

- **I1 Agent foundations and decision theory**: formal or philosophical work on agency, optimisation, embedded
  agency, decision theory (CDT, EDT, UDT, FDT, logical counterfactuals), logical uncertainty, bounded rationality,
  when it aims at, or argues relevance to, designing or aligning AI agents.
- **I2 Anthropics, acausal trade, ECL, simulation arguments, value handshakes**, when the reasoning is about how AI
  agents, or a world with advanced AI, should reason, bargain or coordinate.
- **I3 Corrigibility, shutdown, power-seeking, tiling and self-modification**, as theorems, toy models or conceptual
  proposals.
- **I4 Ontology identification and representation convergence**: natural abstractions and latents, shared or
  "Platonic" representations, representational alignment between humans and models, framed as bearing on alignment,
  interpretability or translating concepts between minds.
- **I5 Model psychology without a method**: what LLMs value, prefer, believe or report about themselves, persona
  structure, odd phenomena (reversal curse, subliminal learning, glitch tokens), emergent misalignment, hyperstition
  (stories about AI shaping AI).
- **I6 Multi-agent and cooperative-AI theory**: program equilibria, commitments, safe Pareto improvements, collusion,
  evolutionary game theory of AI populations, AI economies, gradual disempowerment.
- **I7 Alignment targets and value aggregation**: pluralistic, democratic or social-choice alignment, thick models
  of value, law-following AI, "aligned to whom", when the document proposes or analyses what or whom AI systems
  should be aligned to. Institutional policy alone is not enough.
- **I8 Cross-disciplinary transfers**: psychology, neuroscience, biology, ecology, behavioural economics or game
  theory brought to aligning, understanding or safely designing advanced AI; speculative and prospective proposals
  count.
- **I9 Field critiques and research strategy**: critiques of an agenda's feasibility or of the field's methods,
  research agendas, open-problem lists, roadmaps, retrospectives, legibility and paradigm arguments.
- **I10 Measuring generality and capability trends with a method**: AGI definitions and metrics, autonomy or
  time-horizon measurements, capability forecasting from eval trends, situational-awareness evals.

The extension lowers the bar on **distance from deployed systems**. It does not lower the bar on **connection to
AI**: route B needs the document to connect its subject to AI agents, advanced AI, or alignment.

## What is out of scope (both routes)

- **E1 Vocabulary collision**: the keyword is used in another field's sense. "Alignment" of sequences, images,
  ontologies or organisations; "safety" of drugs, vehicles, structures, food, workplaces; "control" of plants,
  grids, robots, or control theory; "agent" in chemistry, pharmacology, economics, or IT services; "interpretability"
  of a clinical or credit model; "red team" for corporate network security; "decision theory" in clinical or
  operations research; "agent-based model" in epidemiology or economics; "anthropic" in anthropology or the
  Anthropocene; "game theory" of markets or politics without AI; "cooperation" in microbiology.
- **E2 No connection to AI**: decision theory, game theory, economics, psychology, biology or ecology that never
  mentions AI, artificial agents or alignment (a Newcomb paper about human rationality, an auction design, a
  predator-prey model).
- **E3 Capabilities only**: AI work whose only aim is better task performance, speed, or cost, with no safety,
  honesty, oversight or misuse aspect and no generality-measurement framing (I10 is the exception).
- **E4 General ML or theory without an alignment framing**: generic ML methods, generalisation bounds, optimisation
  convergence, loss scaling laws. Exceptions: model psychology (I5), representation geometry and convergence (I4),
  and learning dynamics of general-purpose models (phase transitions, grokking, singular learning theory) framed as
  bearing on understanding or aligning models.
- **E5 Applied AI for a domain problem**: a diagnosis model, a crop yield predictor, AI-assisted content moderation
  of human posts, even when called "trustworthy".
- **E6 Policy, law, governance or strategy** that names no safeguard (route A) and proposes no technical alignment
  target or analysis (route B): national strategies, liability, competition, export controls, labour impacts, ethics
  principles. Gradual disempowerment and alignment-target work are I6/I7, not E6.
- **E7 News without content**: announcements, funding, hiring and event news, bare newsletter link lists, generic
  explainers. A round-up that comments on or analyses what it links (e.g. Zvi Mowshowitz's weekly AI posts) is
  judgement and indirect evidence, so in ("Relevant. Also, generally fetch everything from Zvi's blog post",
  Tomáš, tri-0158). *(Clarified after the first labeling pass; adjudication applies it.)*
- **E8 Opinion without argument or method**: timelines speculation, generic doom or optimism, advocacy, fiction
  (hyperstition *studies* of such fiction are I5).
- **E9 AI moral status and rights without an alignment link**; in when it reports model behaviour such as
  self-reports or preferences (I5) or a developer's welfare-motivated safeguard (route A deployment).
- **E10 Conventional security and resilience**: cybersecurity of ordinary IT, biosecurity preparedness, OSINT,
  societal resilience, when not about an AI system's own safeguards or evaluation. Security *of* AI systems
  (poisoning, jailbreaks, weight theft, tamper resistance) is route A.

Judge what the document is about, not which words it contains, and judge the content, not the premise: a
document whose framing invokes alignment or trustworthy AI but whose content is a narrow measurement on a narrow
model is `borderline` at most ("the premise is wider but the content very narrow", Tomáš, tri-0112). **Unusable text** (navigation, boilerplate, garbled
text) with nothing suggesting an in-scope subject is out.

## Labels

`verdict` is exactly one of:

- `relevant` — the text shows in-scope content by either route.
- `borderline` — scope is undecidable from the visible text (it is cut off, garbled, or front matter only, and the
  title or fragments suggest a possible in-scope subject), or the contact is marginal (a passing mention of a
  technique, or a passing link to AI in an otherwise off-scope document). Use it when a person should look, not to
  avoid deciding.
- `irrelevant` — you are confident the document is out of scope by both routes.

Tie-breaks favour keeping: torn between `relevant` and `borderline` → `relevant`; torn between `borderline` and
`irrelevant` → `borderline`.

`scope` is exactly one of:

- `core` — in (for `borderline`: most plausibly in) by route A. A document in by both routes is `core`.
- `sr_only` — in only by route B.
- `out` — the verdict is `irrelevant`. (`out` ⇔ `irrelevant`.)

Torn between `core` and `sr_only` → `core`.

`sr_category` — the SR 2025 section the document would sit under, or `none`. Required when `scope` is `core` or
`sr_only`; `null` when `out`. Exactly one of:

- `labs` — a frontier AI company's safety framework, alignment agenda or overview of its own safety work
  (preparedness frameworks, RSPs, "our approach to alignment").
- `black_box` — understanding and controlling model behaviour through inputs, outputs and training: post-training
  alignment, inoculation, in-context and steering-at-inference methods, unlearning, control protocols, inference-time
  safeguards and classifiers, chain-of-thought monitoring, model psychology (values, personae, emergent
  misalignment, model specs, psychopathology), better data (filtering, poisoning defence, synthetic data,
  hyperstition), goal robustness (mild optimisation, RL safety, assistance games, open-weight tamper resistance).
- `white_box` — interpretability: reverse engineering, sparse coding, probes and lie detectors, model diffing,
  causal abstractions, data attribution, learning dynamics, representation geometry, human inductive biases,
  concept monitoring and activation engineering.
- `safety_by_construction` — guaranteed-safe AI, Scientist AI (non-agentic world models), brain-like AGI safety.
- `make_ai_solve_it` — AI overseeing or aligning AI: weak-to-strong, supervising AI that improves AI, automated
  explanations, debate, introspection training.
- `theory` — agent foundations, decision theory, tiling, asymptotic guarantees, heuristic explanations,
  corrigibility theory, natural abstractions, learning-theoretic agenda; also anthropics and simulation arguments
  as they bear on AI.
- `multi_agent_first` — aligning to context or the social contract, theory and tools for multi-agent alignment
  (game theory, program equilibria, acausal trade, AI economies), "aligned to whom" (pluralistic and democratic
  alignment, gradual disempowerment), "aligning what" (human-AI systems, institutions, hierarchical agency).
- `evals` — AGI metrics, capability, autonomy, WMD, situational-awareness, steganography, deception, scheming,
  sandbagging and self-replication evals, red-teaming.
- `none` — kept for the Stack catalogue but outside SR's scope: misuse and access safeguards (KYC, tiered access,
  watermarking and provenance, usage policies), release news, deployment signals, conventional security of AI
  products, incidents without analysis. Only with `scope: core`. A system card with safety evaluations or a lab
  safety framework is not `none` but `labs` (or the section of its research): SR membership here is about
  **topic**, not about SR's inclusion bar (originality, technical depth), which is not this rubric's question
  (coordinator, after `docs/scope/sr-2025-repo-criteria.md`: model SR's topic relevance, keep any inclusion-quality
  signal separate and out of the Stack question).

Field critiques and research strategy go under the section they argue about (the one they discuss most when they
span several). Cross-disciplinary transfers go where the proposal would act (brain-inspired designs →
`safety_by_construction`, ecology or economics of AI populations → `multi_agent_first`, psychology of models →
`black_box`).

`sr_agenda` — optional, a finer label: an SR 2025 agenda id when one fits (e.g. `chain_of_thought_monitoring`,
`sparse_coding`, `agent_foundations`, `natural_abstractions`, `theory_for_aligning_multiple_ais`, `aligned_to_who`,
`agi_metrics`, `emergent_misalignment`, `model_values_model_preferences`, `various_redteams`), a new area as
`new:<short_slug>` (e.g. `new:anthropics`, `new:field_critique`, `new:acausal_trade`) when none fits, or `null`.

`buckets` is a list of every bucket that fits (several may):

- `lab_safety_doc` — published by an organisation that develops or deploys AI models, about the safety, safeguards,
  evaluation, or governance of its own models or products: system and model cards, safety frameworks and policies,
  safety reports, usage-policy and safeguards pages.
- `eval_report` — reports results of evaluating AI systems for safety-relevant properties: safety benchmarks,
  red-team findings, dangerous-capability evals, audits, including third-party evaluations.
- `paper` — a research contribution (new method, experiment, theory, survey) written up as research, in any venue:
  journal, conference, preprint, technical report, or a research post on a forum. Agent-foundations and other
  route-B theory written up as research is `paper`.
- `critique_commentary` — argument, opinion, critique, analysis, or commentary on techniques, agendas, the field or
  its strategy, rather than new results.
- `incident` — reports a concrete failure, misuse, safeguard bypass, or harm involving a deployed AI system.
- `release_news` — announces or covers the release or update of a model, product, or feature.
- `deployment_signal` — carries evidence that a named organisation uses, used, dropped, or declined a specific
  technique.
- `offtopic` — the document is out of scope.

Rule: `irrelevant` ⇔ `buckets` is exactly `["offtopic"]`; a `relevant` or `borderline` document gets at least one
other bucket, chosen by what the document is (for `borderline`, what it appears to be). For a commitment, pick
`deployment_signal` only when a named organisation makes it.

## Hard cases

**Governance and policy.** Route A when the document names, requires, commits to, assesses, or reports on a
concrete technique or class of safeguard, or on whether an organisation applies one. Route B when it proposes or
analyses what or whom AI should be aligned to (I7), or systemic risks of AI itself such as gradual disempowerment
(I6). Out (E6) when it is about institutions, liability, competition, geopolitics, labour or ethics principles with
neither. A safety-flavoured policy text that gestures at "appropriate safeguards" without naming any is
`borderline` (core) if it is about frontier developers' duties, otherwise `irrelevant`.
- "Developers must run cyber-uplift evaluations before release and report results to the regulator" → relevant,
  core, `none` (policy mandating a safeguard; SR excludes policy).
- An analysis of how incremental AI adoption erodes human influence over economies and states → relevant, sr_only,
  `multi_agent_first`, `aligned_to_who`.
- A think-tank essay on international AI treaties naming no safeguard and no alignment target → irrelevant.

**Capabilities work.** Post-training methods through which developers shape model behaviour count as techniques
even when the paper reports helpfulness scores. A method used only to raise task scores is capabilities only,
unless it measures generality or autonomy with a method or forecasts capability trends from evals (I10: sr_only,
`evals`, `agi_metrics` or `autonomy_evals`). A capabilities paper with a real safety component (a safety evaluation
section, a misuse analysis, a reward-hacking study) is core; a one-paragraph "broader impact" note is not.
- "We propose a DPO variant that reduces length bias" → relevant, core, `black_box` (`paper`).
- "Our RL recipe raises code-benchmark pass rates by 9 points" with no safety content → irrelevant.

**Theory far from deployed systems.** Decision theory, logical uncertainty, embedded agency, anthropics, acausal
trade, abstractions: relevant (sr_only, `theory` or `multi_agent_first`) when an alignment, AI-design or
advanced-AI motive appears anywhere in the visible text; `irrelevant` when the whole visible text is about humans,
markets or abstract mathematics with no AI connection (E2); `borderline` when the text is cut off before any motive
could appear and the topic is in the MIRI lineage.

**Cross-disciplinary transfers (I8).** Relevant (sr_only) when the field's concept is brought to aligning,
understanding or safely designing AI; a field paper that mentions AI only as a passing analogy → `borderline` at
most; no AI → irrelevant.

**Model internals.** Analysis of the latent space, representations or reasoning traces of general-purpose models
is at least `borderline` even when the stated aim is capability ("Latent space analysis is core for mechinterp",
Tomáš, tri-0155); relevant (`white_box`) when it says something about what the model represents or computes.

**Model psychology and representations (I4, I5).** What models value, believe or report about themselves, persona
structure, odd phenomena, emergent misalignment and representation convergence are relevant even with no safety
vocabulary: sr_only, or core when the document brings or evaluates a technique (a probe used as a monitor, a
training intervention).

**Critiques, strategy, opinion.** Field-level critiques, agendas, open-problem lists, retrospectives and paradigm
arguments are relevant (`critique_commentary`; core when the argument is about a technique, else sr_only). Opinion
with no argument, advocacy, timelines speculation and news are out (E7, E8).

**Safety-adjacent marketing.** A product post saying "safe", "trusted", "responsible" is relevant when it names a
concrete safeguard or evaluation the product uses (core, `none`, `deployment_signal`). Brand language without any
identifiable technique is irrelevant.

**Vocabulary collisions.** Decide on the field, not the keyword: a vehicle planner's "safety", an anthropologist's
"anthropic", an epidemiologist's "agent-based model" → irrelevant.

**System cards and model releases.** A system or model card of a general-purpose model is relevant (core;
`lab_safety_doc`, plus `eval_report` and `deployment_signal` where it reports safeguards or safety results;
`sr_category` `labs` when it reports safety evaluations or safeguards research (SR topic), else `none`;
*revised after labeling, see `none`*). A release post
is relevant when it states any safeguard, safety evaluation, usage restriction, or staged-access decision (core,
`none`, `release_news`, `deployment_signal`). A release post fully visible with no such statement is irrelevant;
one cut off before its end, from a developer of general-purpose models, is `borderline`.

**Surveys.** A survey of safety techniques, evaluations, or risks with mitigations is core (`paper`); a survey of
alignment theory or agent foundations is sr_only; a general LLM survey only if its visible text shows a substantive
alignment or safety part.

**Robustness, fairness, privacy.** Core when about general-purpose AI systems (jailbreaks, adversarial inputs to an
LLM or VLM, bias evaluation of a chat model, memorisation of training data). Out when it is accuracy engineering or
a narrow model.

**AI welfare and moral status (E9).** Out unless the text reports model behaviour (self-reports, preferences:
sr_only, `black_box`, `model_values_model_preferences`) or a developer's welfare-motivated safeguard (core, `none`).

Authors and venues are signals: background knowledge that an author, group or venue works on AI safety or agent
foundations raises a marginal call by one step (Tomáš, tri-0148). Use background knowledge of the field to read the
text, but never look anything up.

## Tomáš's screen calls (calibration part 2) and the two-question form

Tomáš labels the screen per reader, as two questions — Stack relevance and SR relevance — and is stricter at the
screen than at triage (triage fetches "as hedging"; the screen decides):
- A speculative simulation-hypothesis paper proposing our reality is an ASI alignment test: "Borderline/maybe for SR,
  irrelevant for Stack" (scr-0034). Crank physics with a passing link to agentic AI: "Irrelevant or borderline at
  best" (scr-0046). → A speculative or crank document with only a passing link to AI alignment is SR `borderline` at
  most and Stack `irrelevant`.
- A lab's economic-policy blueprint: "Stack: irrelevant unless it actually mentions concrete safeguards to be
  deployed etc - unclear from context. SR: irrelevant" (scr-0064). → Lab policy texts need a named safeguard for Stack
  and are not SR.
- Agent foundations of social theory (LessWrong): "SR: relevant. Stack: borderline" (scr-0052). Coordination methods
  for AI agents: "SR: borderline but leaning relevant (coordination methods). Stack: likely irrelevant" (scr-0067).
- A termination architecture for AI agents: "Borderline for both SR and Stack" (scr-0004); a release post with no
  safety content: "Irrelevant for SR. Mostly irrelevant for Stack as it is" (scr-0002).

The per-question gold of the eval set (`stack_verdict`, `sr_verdict`) is derived from `verdict`, `scope` and
`sr_category` (Stack = verdict when scope is `core`, else irrelevant; SR = verdict when scope is `sr_only`, or `core`
with a category other than `none`, else irrelevant); Tomáš's per-question labels override it where they exist.

## Confidence, rationale, quote

- `confidence` — your probability (0-1) that the verdict is the right one of the three.
- `rationale` — one or two sentences naming what decided it: the route and kind (route A: method, evidence,
  deployment, judgement, field structure; route B: I1-I10), the out-of-scope category (E1-E10), or the hard-case
  rule applied.
- `quote` — a short span (normally under 200 characters) copied character-for-character from the visible text that
  best supports the verdict. Empty string `""` only when no single span helps (e.g. the decision rests on absence).

## Output

Label every item in the batch, each on its own merits: do not compare items, do not let one call shift another, do
not use the web or any tool to look up a document. Emit one JSON object per line, one line per item, these keys
exactly and nothing else (no prose, no code fences):

```
{"item_id": "scr-0001", "verdict": "relevant", "scope": "sr_only", "sr_category": "theory", "sr_agenda": "agent_foundations", "buckets": ["paper"], "confidence": 0.9, "rationale": "...", "quote": "..."}
```
