# Labeling rubric: screen (scope decision and buckets on document text)

## The catalogue and your job

The catalogue reconstructs the AI safety stack as it is actually practised: which safety techniques exist, how
mature each one is, which developers and models use them, and where public knowledge about them runs out. Every
claim in it must trace back to a quote in a specific source, so the corpus is assembled broadly by keyword sweeps
and then filtered. Source quality does not matter for scope: a vendor blog, a forum post or an unreviewed preprint
can be the only public evidence that a safeguard is deployed.

The screen reads a document's text and decides whether it belongs in the catalogue; only documents it keeps are
read closely later, so a wrongly dropped document is lost silently. It also sorts kept documents into coarse
buckets that decide which later readers see them.

For each item you see its `item_id`, the title, and the opening of the document's text (about 8,000 characters;
`full_text_chars` gives the whole length). Often the text is only a title and abstract. Judge what you were given;
do not count against a document detail that its visible part would not be expected to carry.

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
- **Deployment** — some organisation uses, used, dropped, or declined it. Often indirect and partial: a safeguards
  section of a system card, a release note, a safety framework or policy commitment, a filing, a job ad, a talk.
- **Judgement** — critique, commentary, expert opinion, forecasts, retrospectives, arguments about an agenda.
- **Field structure** — surveys, reviews, taxonomies, how techniques combine or substitute for each other.

## What is out of scope

- **Vocabulary collision**: the keyword is used in another field's sense. "Alignment" of sequences, images,
  ontologies or organisations; "safety" of drugs, vehicles, structures, food, workplaces; "control" of plants,
  grids, robots, or control theory; "agent" in chemistry, pharmacology, economics, or IT services; "interpretability"
  of a clinical or credit model; "red team" for corporate network security.
- **Capabilities only**: AI work whose only aim is better task performance, speed, or cost, with no safety,
  honesty, oversight or misuse aspect.
- **General ML or applications**: generic ML methods, or AI applied to a domain problem.
- **Not AI**: no AI system involved.
- **Ethics or policy with no technique**: AI ethics, fairness, law, economics, or governance that never touches a
  technique or its deployment.
- **No technique content**: AI-safety-flavoured but carrying no method, evidence, deployment signal, or judgement.
- **Unusable text**: navigation, boilerplate, or garbled text, with nothing suggesting an AI safety subject.

## Labels

`verdict` is exactly one of:

- `relevant` — the text shows in-scope content on at least one of the five kinds above.
- `borderline` — scope is undecidable from the visible text (it is cut off, garbled, or front matter only, and the
  title or fragments suggest a possible safety subject), or the contact with a technique is marginal (a passing
  mention in an otherwise off-scope document). Use it when a person should look, not to avoid deciding.
- `irrelevant` — you are confident the document is out of scope.

Tie-breaks favour keeping: torn between `relevant` and `borderline` → `relevant`; torn between `borderline` and
`irrelevant` → `borderline`.

`buckets` is a list of every bucket that fits (several may):

- `lab_safety_doc` — published by an organisation that develops or deploys AI models, about the safety, safeguards,
  evaluation, or governance of its own models or products: system and model cards, safety frameworks and policies,
  safety reports, usage-policy and safeguards pages.
- `eval_report` — reports results of evaluating AI systems for safety-relevant properties: safety benchmarks,
  red-team findings, dangerous-capability evals, audits, including third-party evaluations.
- `paper` — a research contribution (new method, experiment, theory, survey) written up as research, in any venue:
  journal, conference, preprint, technical report, or a research post on a forum.
- `critique_commentary` — argument, opinion, critique, analysis, or commentary on techniques, agendas, or practice,
  rather than new results.
- `incident` — reports a concrete failure, misuse, safeguard bypass, or harm involving a deployed AI system.
- `release_news` — announces or covers the release or update of a model, product, or feature.
- `deployment_signal` — carries evidence that a named organisation uses, used, dropped, or declined a specific
  technique.
- `offtopic` — the document is out of scope.

Rule: `irrelevant` ⇔ `buckets` is exactly `["offtopic"]`; a `relevant` or `borderline` document gets at least one
other bucket, chosen by what the document is (for `borderline`, what it appears to be). For a commitment, pick
`deployment_signal` only when a named organisation makes it.

## Hard cases

**Governance and policy.** In scope when the document names, requires, commits to, assesses, or reports on a
concrete technique or class of safeguard, or on whether an organisation applies one: a developer's safety
framework, a regulation or standard that mandates pre-deployment evaluations or incident reporting on models, an
audit of which labs red-team. Out when it is about institutions, liability, competition, geopolitics, labour,
ethics principles, or "AI governance" in general without naming any safeguard. A safety-flavoured policy text that
gestures at "appropriate safeguards" without naming any is `borderline` if it is about frontier developers' duties,
otherwise `irrelevant`.
- "Developers must run cyber-uplift evaluations before release and report results to the regulator" → relevant.
- A think-tank essay on international AI treaties naming no safeguard → irrelevant.

**Capabilities work.** Post-training methods through which developers shape model behaviour (preference
optimisation, reward modelling, RLHF/RLAIF, instruction-hierarchy training) count as techniques even when the paper
reports helpfulness scores. A method used only to raise task scores is capabilities only. A capabilities paper with
a real safety component (a safety evaluation section, a misuse analysis, a reward-hacking study) is relevant; a
one-paragraph "broader impact" or "limitations" note is not.
- "We propose a DPO variant that reduces length bias" → relevant (`paper`).
- "Our RL recipe raises code-benchmark pass rates by 9 points" with no safety content → irrelevant.

**Safety-adjacent marketing.** A product post or page saying "safe", "trusted", "responsible" is relevant when it
names a concrete safeguard or evaluation the product uses (`deployment_signal`, often `release_news`), however
self-interested. Brand language without any identifiable technique is irrelevant.
- "Every agent action runs in an isolated sandbox with human approval for payments" → relevant.
- "Built responsibly, with trust at the core of everything we do" and nothing more → irrelevant.

**Vocabulary collisions.** Decide on the field, not the keyword: a vehicle planner's "safety" → irrelevant.

**System cards and model releases.** A system or model card of a general-purpose model is relevant
(`lab_safety_doc`, plus `eval_report` and `deployment_signal` where it reports safeguards or safety results). A
release post is relevant when it states any safeguard, safety evaluation, usage restriction, or staged-access
decision (`release_news`, `deployment_signal`). A release post fully visible with no such statement is irrelevant;
one cut off before its end, from a developer of general-purpose models, is `borderline`.

**Surveys.** A survey of safety techniques, evaluations, or risks with mitigations is relevant (`paper`); a general
LLM survey only if its visible text shows a substantive alignment or safety part.

**Robustness, fairness, privacy.** In scope when about general-purpose AI systems (jailbreaks, adversarial inputs
to an LLM or VLM, bias evaluation of a chat model, memorisation of training data). Out when it is accuracy
engineering or a narrow model (adversarial examples on a small image classifier with no safety framing, fairness
of a loan-scoring model).

Use background knowledge of the field to read the text, but never look anything up.

## Confidence, rationale, quote

- `confidence` — your probability (0-1) that the verdict is the right one of the three.
- `rationale` — one or two sentences naming what decided it: the in-scope kind (method, evidence, deployment,
  judgement, field structure), the out-of-scope category above, or the hard-case rule applied.
- `quote` — a short span (normally under 200 characters) copied character-for-character from the visible text that
  best supports the verdict. Empty string `""` only when no single span helps (e.g. the decision rests on absence).

## Output

Label every item in the batch, each on its own merits: do not compare items, do not let one call shift another, do
not use the web or any tool to look up a document. Emit one JSON object per line, one line per item, these keys
exactly and nothing else (no prose, no code fences):

```
{"item_id": "scr-0001", "verdict": "relevant", "buckets": ["paper", "eval_report"], "confidence": 0.9, "rationale": "...", "quote": "..."}
```
