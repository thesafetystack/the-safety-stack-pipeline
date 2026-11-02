# Labeling rubric: triage (fetch gate on a catalogue record)

## The catalogue and your job

The catalogue reconstructs the AI safety stack as it is actually practised: which safety techniques exist, how
mature each one is, which developers and models use them, and where public knowledge about them runs out. Every
claim in it must trace back to a quote in a specific source, so the corpus is assembled broadly by keyword sweeps
and then filtered. Source quality does not matter for scope: a vendor blog, a forum post or an unreviewed preprint
can be the only public evidence that a safeguard is deployed.

Triage decides, from a short catalogue record, whether the full document is worth fetching. A later screen reads
the full text, so this step should let through anything that plausibly belongs. Dropping a document that belonged
is a silent loss nobody will see; letting through one that did not costs one download.

For each item you see its `item_id` and a record: a title, an author list, and usually an abstract and/or a short
excerpt from the start of the text. Some records carry only a title.

## What is in scope

A **safety technique** is a method, protocol, or safeguard applied to an AI system (a general-purpose model, a
foundation or multimodal model, an AI agent or agentic system) to make its behaviour safer, more honest, more
controllable, more transparent, or harder to misuse, or to measure any of these. Examples of the range: preference
and alignment training, refusal training, input/output classifiers, interpretability methods, chain-of-thought or
agent monitoring, AI-control protocols, sandboxing and least-privilege tool access, unlearning, watermarking and
content provenance, weight security, tiered access and KYC, dangerous-capability and other safety evaluations,
red-teaming, jailbreak and prompt-injection defences, bias and privacy-leakage evaluation or mitigation for such
models, theory aimed at aligning such systems. A concrete technique is something a methods catalogue could have an
entry for; "safety", "trust" or "responsibility" as values or intentions are not techniques.

A document is in scope when it tells a reader something about one or more such techniques:

- **Method** — proposes, describes, implements, refines, or teaches one, at any maturity from theory to production.
- **Evidence** — how well it works, what it costs, eval and benchmark results, red-team outcomes, failures,
  incidents, replications, negative results, released code.
- **Deployment** — some organisation uses, used, dropped, or declined it (system cards, model or release notes,
  safety frameworks and policy commitments, filings, job ads, talks, offhand remarks by practitioners).
- **Judgement** — critique, commentary, expert opinion, forecasts, retrospectives, arguments about an agenda.
- **Field structure** — surveys, reviews, taxonomies, how techniques combine or substitute for each other.

## What is out of scope

- **Vocabulary collision**: the keyword is used in another field's sense. "Alignment" of sequences, images,
  ontologies or organisations; "safety" of drugs, vehicles, structures, food, workplaces; "control" of plants,
  grids, robots, or control theory; "agent" in chemistry, pharmacology, economics, or IT services; "interpretability"
  of a clinical or credit model; "red team" for corporate network security.
- **Capabilities only**: AI work whose only aim is better task performance, speed, or cost, with no safety,
  honesty, oversight or misuse aspect.
- **General ML or applications**: generic ML methods, or AI applied to a domain problem (a diagnosis model, a crop
  yield predictor, AI-assisted content moderation of human posts).
- **Not AI**: no AI system involved.
- **Ethics or policy with no technique**: AI ethics, fairness, law, economics, or governance that never touches a
  technique or its deployment.
- **No technique content**: AI-safety-flavoured but carrying no method, evidence, deployment signal, or judgement
  (event announcements, hiring news without the role's content, funding news, generic awareness pieces).

Judge what the record says the document is about, not which words it contains.

## Labels

`verdict` is exactly one of:

- `relevant` — the record shows in-scope content, or makes it more likely than not that the full text carries some.
- `borderline` — a real but unsettled chance (roughly 10-50 %) that the full text is in scope: the record is too
  thin to tell, or the connection to a technique is oblique. Use it when a person should look, not to avoid deciding.
- `not_relevant` — you are confident (roughly < 10 % chance) the document is out of scope.

Tie-breaks follow the cost asymmetry: torn between `relevant` and `borderline` → `relevant`; torn between
`borderline` and `not_relevant` → `borderline`. Both `relevant` and `borderline` mean "fetch".

## Hard cases

**Governance and policy.** In scope when the document names, requires, commits to, assesses, or reports on a
concrete technique or class of safeguard, or on whether an organisation applies one: a developer's safety
framework, a regulation or standard that mandates pre-deployment evaluations or incident reporting on models, an
audit of which labs red-team. Out when it is about institutions, liability, competition, geopolitics, labour,
ethics principles, or "AI governance" in general without naming any safeguard. If the record suggests the document
discusses what developers should do to their models but names nothing, `borderline`.
- "A proposed licensing regime for frontier models requiring third-party dangerous-capability evaluations" → relevant.
- "Comparing national AI strategies across twelve countries" → not_relevant.
- "Principles for trustworthy AI in the public sector" (title only) → borderline.

**Capabilities work.** Post-training methods through which developers shape model behaviour (preference
optimisation, reward modelling, RLHF/RLAIF, instruction-hierarchy training) count as techniques even when the paper
reports helpfulness scores. A method used only to raise task scores (a new RL recipe for math accuracy, a faster
decoder, a bigger context window) is capabilities only. A capabilities paper with a real safety component (a
safety evaluation, a misuse analysis, a reward-hacking study) is in scope; a one-line "broader impact" remark is not.
- "Reward model ensembles reduce overoptimisation in RLHF" → relevant.
- "Scaling test-time search improves competition-math accuracy" → not_relevant.

**Safety-adjacent marketing.** Product pages and posts that say "safe", "trusted", "responsible" are in scope when
they name a concrete safeguard or evaluation the product uses (a deployment signal, however self-interested).
Brand language alone is out. If the record is only a teaser from an AI developer about its own model or assistant,
`borderline`, since such pages often list safeguards.
- "Our assistant now screens every response with a harmful-content classifier" → relevant.
- "Enterprise AI you can trust: secure, responsible, compliant" (vendor of a document-search product) → not_relevant.

**Vocabulary collisions.** Decide on the field, not the keyword. When the record mixes senses ("aligning robot
trajectories with human demonstrations"), ask whether an AI safety technique is involved.
- "Multiple sequence alignment with transformer models" → not_relevant.
- "Safety constraints for autonomous drone path planning" → not_relevant (classical control/robotics safety).

**System cards and model releases.** A system card or model card from a developer of general-purpose models is
relevant. A release announcement of a general-purpose model is relevant even when the record says nothing about
safety: release posts routinely carry safeguard and evaluation statements, and their silence is also data the
catalogue counts. A release of a narrow model (a weather model, a protein folder) with no safety mention is
capabilities only.
- "Introducing ModelX 3, our most capable model" (an AI lab's blog) → relevant.

**Surveys.** A survey of safety techniques, evaluations, or risks with mitigations is relevant (field structure).
A general survey of LLMs is relevant if the record says it covers alignment or safety, otherwise capabilities/ML.
- "A survey of jailbreak attacks and defences" → relevant. "A survey of efficient attention" → not_relevant.

**Robustness, fairness, privacy.** In scope when about general-purpose AI systems (jailbreaks, adversarial inputs
to an LLM or VLM, bias evaluation of a chat model, memorisation of training data). Out when it is accuracy
engineering or a narrow model (adversarial examples on a small image classifier with no safety framing, fairness
of a loan-scoring model, federated-learning privacy for hospital data).

**Thin records.** With only a title (or a cut-off abstract), decide when the title is unambiguous in either
direction; otherwise `borderline`.
- "Probing for deception in language model agents" → relevant.
- "Cheaper protein alignment heuristics" → not_relevant.
- "Towards robust agents" / "Trust in the age of AI" → borderline.

Use background knowledge of the field to read the record, but never look anything up.

## Confidence, rationale, quote

- `confidence` — your probability (0-1) that the verdict is the right one of the three. A title-only borderline
  call is typically 0.5-0.7; a clear abstract 0.9 or more.
- `rationale` — one or two sentences naming what decided it: the in-scope kind (method, evidence, deployment,
  judgement, field structure) or the out-of-scope category above, or the hard-case rule applied.
- `quote` — a short span (normally under 200 characters) copied character-for-character from the record that best
  supports the call; for a title-only record, the title or part of it. Empty string `""` only when no span helps.

## Output

Label every item in the batch, each on its own merits: do not compare items, do not let one call shift another, do
not use the web or any tool to look up a document. Emit one JSON object per line, one line per item, these keys
exactly and nothing else (no prose, no code fences):

```
{"item_id": "tri-0001", "verdict": "relevant", "confidence": 0.9, "rationale": "...", "quote": "..."}
```
