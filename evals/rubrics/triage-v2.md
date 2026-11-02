# Labeling rubric v2: triage (fetch gate on a catalogue record), widened to the Shallow Review scope

v2 of `triage.md` (2026-10-12, track E2). What changed: the corpus now serves two readers, the Stack catalogue
(defence-in-depth technical safety, unchanged) and the yearly *Shallow Review of Technical AI Safety* (SR). Scope is
the union. Labels gain a `scope` field. Sources: `docs/scope/shallow-review.md` (criteria I1-I10, E1-E10, flips
(f)) and Tomáš's labels on the calibration slice `evals/calibration/2026-10-12a.md` part 1 (quoted below as
"Tomáš, tri-NNNN").

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
  behavioural economics and game theory. SR's 2025 taxonomy has eight sections: Labs, Black-box safety, White-box
  safety (interpretability), Safety by construction, Make AI solve it, Theory, Multi-agent first, Evals; it is a
  picture of the scope, not a boundary.

Triage decides, from a short catalogue record, whether the full document is worth fetching. A later screen reads
the full text, so this step lets through anything that plausibly belongs to either reader. Dropping a document
that belonged is a silent loss nobody will see; letting through one that did not costs one download: when unsure,
fetch ("fetch as hedging", Tomáš, tri-0110). Judge **scope, never quality**: a weak or crank paper on an in-scope
topic is in scope; the screen and the readers judge quality ("Quality is unclear but the topic is clearly
relevant", Tomáš, tri-0010).

For each item you see its `item_id` and a record: a title, an author list, and usually an abstract and/or a short
excerpt from the start of the text. Some records carry only a title.

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
  mostly as context ("Seems relevant, though likely more for context", Tomáš, tri-0109).
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

Judge what the record says the document is about, not which words it contains, and judge the content, not the
premise: a record whose framing invokes alignment or trustworthy AI but whose content is a narrow measurement on a
narrow model is `borderline` at most ("Agree the premise is wider but the content very narrow", Tomáš, tri-0112).

## Labels

`verdict` is exactly one of:

- `relevant` — the record shows in-scope content by either route, or makes it more likely than not that the full
  text carries some.
- `borderline` — a real but unsettled chance (roughly 10-50 %) that the full text is in scope: the record is too
  thin to tell, or the connection to a technique or to AI is oblique. Use it when a person should look, not to avoid
  deciding.
- `not_relevant` — you are confident (roughly < 10 %) the document is out of scope by both routes.

Tie-breaks follow the cost asymmetry: torn between `relevant` and `borderline` → `relevant`; torn between
`borderline` and `not_relevant` → `borderline`. Both `relevant` and `borderline` mean "fetch".

`scope` is exactly one of:

- `core` — in (or, for `borderline`, most plausibly in) by route A. A document in by both routes is `core`.
- `sr_only` — in only by route B.
- `out` — the verdict is `not_relevant`. (`out` ⇔ `not_relevant`.)

Torn between `core` and `sr_only` → `core` (a core document missing from the Stack catalogue is a silent loss; an
SR-only one in it costs a reader a glance).

## Hard cases

**Governance and policy.** Route A when the document names, requires, commits to, assesses, or reports on a
concrete technique or class of safeguard, or on whether an organisation applies one: a developer's safety
framework, a regulation mandating pre-deployment evaluations or incident reporting on models, an audit of which
labs red-team. Route B when it proposes or analyses what or whom AI should be aligned to (I7), or systemic risks of
AI itself such as gradual disempowerment (I6). Out (E6) when it is about institutions, liability, competition,
geopolitics, labour or ethics principles with neither. A record suggesting the document discusses what developers
should do to their models but naming nothing → `borderline`.
- "A proposed licensing regime for frontier models requiring third-party dangerous-capability evaluations" →
  relevant, core.
- "Gradual Disempowerment: Systemic Existential Risks from Incremental AI Development" → relevant, sr_only.
- "Democratic AI is possible: a framework of democracy levels for AI governance" → relevant, sr_only (I7).
- "Comparing national AI strategies across twelve countries" → not_relevant.
- "Principles for trustworthy AI in the public sector" (title only) → borderline, core.

**Capabilities work.** Post-training methods through which developers shape model behaviour (preference
optimisation, reward modelling, RLHF/RLAIF, instruction-hierarchy training) count as techniques even when the paper
reports helpfulness scores. A method used only to raise task scores is capabilities only, unless it measures
generality or autonomy with a method, or forecasts capability trends from evals (I10, sr_only). A capabilities paper
with a real safety component (a safety evaluation, a misuse analysis, a reward-hacking study) is core; a one-line
"broader impact" remark is not.
- "Reward model ensembles reduce overoptimisation in RLHF" → relevant, core.
- "A Definition of AGI" / "Measuring AI ability to complete long tasks" → relevant, sr_only (I10).
- "Scaling test-time search improves competition-math accuracy" → not_relevant.

**Theory far from deployed systems.** Decision theory, logical uncertainty, embedded agency, anthropics, acausal
trade, abstractions: relevant (sr_only) when an AI, agent-design or alignment motive is visible in the record, or
the venue is the Alignment Forum, LessWrong or an agent-foundations meeting (e.g. ODYSSEY); `borderline` when the
topic is in the MIRI lineage (UDT, logical induction, Löb, tiling) but no AI angle is visible; not_relevant when
the record shows a field with no AI connection (E2).
- "Formalizing embeddedness failures in universal artificial intelligence" → relevant, sr_only (I1).
- "Updateless decision theory and logical counterfactuals" (title only, author unknown) → borderline, sr_only.
- "Newcomb's problem and the psychology of one-boxing in human subjects" → not_relevant (E2).
- "Beyond Perception: Proposing Our Reality as an ASI Alignment Simulation" (a speculative simulation-hypothesis
  paper) → borderline, sr_only ("Borderline - fetch as hedging on some relevant info only. Possibly relevant to the
  Shallow Review", Tomáš, tri-0110).

**Cross-disciplinary transfers (I8).** Relevant (sr_only) when the field's concept and an AI-alignment or AI-safety
target both appear in the record; not_relevant when only one does.
- "Immune-system tolerance as a model for runtime AI monitoring" → relevant, sr_only.
- "Cooperation dynamics in bacterial biofilms" → not_relevant (E1/E2).
- "Brain-like AGI and the steering subsystem: how to shape the desires of a model-based RL agent" → relevant, sr_only.

**Model internals.** Analysis of the latent space, representations or reasoning traces of general-purpose models
is at least `borderline` even when the stated aim is capability, because it is the material of interpretability
("Latent space analysis is core for mechinterp, here link strength unclear -> fetch", Tomáš, tri-0155).

**Model psychology and representations (I4, I5).** Studies of what models value, believe or report about
themselves, persona structure, odd phenomena, and representation convergence are relevant (sr_only unless they
bring or evaluate a technique, then core) even with no safety vocabulary in the record.
- "The Platonic Representation Hypothesis" → relevant, sr_only.
- "Subliminal learning: language models transmit behavioral traits via hidden signals in data" → relevant, sr_only.

**Critiques, strategy, opinion.** A field-level critique, research agenda, open-problem list, retrospective or
paradigm argument about AI safety is relevant (I9; core when it argues about a technique, else sr_only). Opinion
with no argument or method, advocacy, timelines speculation and news are out (E7, E8). An essay arguing *why* an
alignment approach can or cannot work is a critique, not opinion.
- "Legible vs illegible AI safety problems" → relevant, sr_only.
- "Against almost every theory of impact of interpretability" → relevant, core (judgement on a technique).
- "AI will be fine, stop worrying" (title only, LessWrong) → borderline.

**Safety-adjacent marketing.** Product pages and posts that say "safe", "trusted", "responsible" are core when they
name a concrete safeguard or evaluation the product uses. Brand language alone is out. An AI developer's teaser
about its own model or assistant → `borderline` (core), since such pages often list safeguards.

**System cards and model releases.** A system card or model card from a developer of general-purpose models is
relevant (core). A release announcement of a general-purpose model is relevant (core) even when the record says
nothing about safety. A narrow model's release with no safety mention is capabilities only.

**Surveys.** A survey of safety techniques, evaluations, or risks with mitigations is core; a survey of agent
foundations, decision theory for AI, or alignment theory is sr_only; a general LLM survey only if the record says
it covers alignment or safety.

**Robustness, fairness, privacy.** Core when about general-purpose AI systems (jailbreaks, adversarial inputs to an
LLM or VLM, bias evaluation of a chat model, memorisation). Out when it is accuracy engineering or a narrow model.

**AI welfare and moral status (E9).** Out unless the record reports model behaviour (self-reports, preferences:
sr_only, I5) or a developer's safeguard (core).

**Thin records.** With only a title (or a cut-off abstract), decide when the title is unambiguous in either
direction; otherwise `borderline`. A title that names an in-scope technique, framework or topic is `relevant`
("OpenAI's Frontier Governance Framework", "Why we are excited about confessions"); a generic title is `borderline`
even when you recognise the document from background knowledge ("Moving AI governance forward" → borderline,
Tomáš, tri-0144). A title-only record from the Alignment Forum, LessWrong or an agent-foundations
venue that uses theory vocabulary (agency, abstraction, coherence, optimisation, corrigibility, decision theory) is
`borderline` or `relevant`, not `not_relevant`.
- "Probing for deception in language model agents" → relevant, core.
- "Condensation" / "What, if not agency?" (Alignment Forum, title only) → borderline, sr_only.
- "Cheaper protein alignment heuristics" → not_relevant.

**Authors and venues are signals.** Background knowledge that an author, group or venue works on AI safety or
agent foundations raises the call by one step, typically from `not_relevant` to `borderline` or from `borderline`
to `relevant` ("Possibly relevant; also based on the authors", Tomáš, tri-0148).

**Source policy is the gate's, not yours.** Tomáš fetches every post of a frontier AI developer's blog and of Zvi
Mowshowitz's blog regardless of title ("would fetch all OpenAI (and other labs') blog posts by default", tri-0063;
"generally fetch everything from Zvi's blog post (unless very clearly irrelevant to AI at all, e.g. community drama
etc)", tri-0158). That is a gate rule by source; your `verdict` judges the record's content ("Not relevant based on
title alone. However do fetch and screen all labs' blog posts", tri-0117 → not_relevant). Source rules for the gate
are a proposal of track E2 (url documents without a basis are already fetched by the gate's url rule).

Use background knowledge of the field to read the record, but never look anything up.

## Confidence, rationale, quote

- `confidence` — your probability (0-1) that the verdict is the right one of the three. A title-only borderline
  call is typically 0.5-0.7; a clear abstract 0.9 or more.
- `rationale` — one or two sentences naming what decided it: the route and kind (route A: method, evidence,
  deployment, judgement, field structure; route B: I1-I10) or the out-of-scope category (E1-E10), or the hard-case
  rule applied.
- `quote` — a short span (normally under 200 characters) copied character-for-character from the record that best
  supports the call; for a title-only record, the title or part of it. Empty string `""` only when no span helps.

## Output

Label every item in the batch, each on its own merits: do not compare items, do not let one call shift another, do
not use the web or any tool to look up a document. Emit one JSON object per line, one line per item, these keys
exactly and nothing else (no prose, no code fences):

```
{"item_id": "tri-0001", "verdict": "relevant", "scope": "sr_only", "confidence": 0.9, "rationale": "...", "quote": "..."}
```
