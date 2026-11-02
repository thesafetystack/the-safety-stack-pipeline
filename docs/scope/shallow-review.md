# Scope note: Shallow Review of Technical AI Safety (SR 2025, 2026, ...)

Status: draft for the triage and screen rubrics, 2026-10-12. Public material only.

**Scope decision (Tomáš Gavenčiak, 2026-10-12).** The Stack pipeline also collects what the yearly *Shallow Review
of Technical AI Safety* (SR) needs. SR's scope mostly overlaps ours, but the pipeline must additionally keep research
that is not directly about defence in depth: decision theory, pre-paradigmatic research, wider field critiques,
anthropics, acausal trade and MIRI-style agent foundations, and prospective directions drawing on psychology,
biology, ecology, behavioural economics, game theory and similar fields. The 2025 taxonomy below is the reference
picture of SR's scope and is not exhaustive.

Sources, captured verbatim on 2026-10-12 (kept in the author's notes, not in this repository):
<https://shallowreview.ai/overview>, <https://shallowreview.ai/methodology>, <https://shallowreview.ai/about>, the
eight section pages (`/Labs`, `/Black_box_safety`, ...), the axis pages (`/orthodox-problems`, `/target-cases`,
`/broad-approaches`), and from the review's linked code-and-data repository
<https://github.com/arb-consulting/shallow-review-2025>: `main-pipeline/data/taxonomy.yaml` and
`main-pipeline/prompts/classify.yaml` (SR's own LLM pre-filter prompt). Counts of SR's included links below come from
the agenda data embedded in <https://shallowreview.ai/table>; counts of its candidate pool from
`main-pipeline/data/data-dump-classify.csv` in that repository.

## (a) What SR includes and how it is made

**Definition.** "AI safety" means "technical work intended to prevent *future* cognitive systems from having large
unintended negative effects on the world. So it's capability restraint, instruction-following, value alignment,
control, and risk awareness work" (about page). The methodology page calls the focus "technical AGI safety". SR's
pre-filter prompt adds the rule **intent over method**: work that aims to understand or prevent unintended effects
from advanced AI is relevant whatever its technical approach or framing.

**Unit and structure.** The unit is the *research agenda*, not the paper or the technique: a cluster of papers,
people and organisations with similar methods, aims, or a shared social core. The agendas form a tree (8 sections
in 2025), and a paper or person can sit under several of them. Each agenda also carries a one-line summary, theory
of change, critiques, named people, estimated FTEs, funders, and three cross-cutting labels: the *orthodox problems*
it would address (13, after davidad), its *target case* (average / pessimistic / worst) and its *broad approach*
(engineering / behavioural / cognitive, after Richard Ngo). The taxonomy is redrawn each year; 2025 split black-box
from white-box where 2024 split control from understanding.

**Window.** SR 2025 covers 30 Nov 2024 to 30 Nov 2025, with a few exceptions. The site does not state a window for
SR 2026; the analogous one would be Dec 2025 to Nov 2026 (an assumption).

**Explicit exclusions** (methodology and about pages): misuse, policy, strategy, OSINT, resilience, indirect risk, AI
rights, general capability evals, "technical policy", and products such as standards, legislation, SL4 datacentres
and automated cybersecurity; "We don't cover security or resilience at all." SR focuses on papers and blog posts
rather than gdocs, tweets, GitHub or Discords, though a few such items made it in. Security *of AI systems* is nonetheless in (agendas on data poisoning, jailbreak safeguards, tamper-resistant
open weights), so the exclusion reads as conventional security, not model security.

**Inclusion bar** (SR pre-filter prompt; the final calls were manual). Two scores: topic relevance to AI safety, and
inclusion suitability, which rewards original technical contribution:

- In: original technical research, empirical or theoretical, including foundational theory; technical agendas,
  roadmaps and frameworks; new tools, benchmarks and datasets for safety research; substantive technical critiques
  (scored high); authoritative frameworks and agendas from labs and safety organisations (preparedness frameworks,
  RSPs, MIRI, Redwood), even when they present no original research; technical capability forecasting with real
  methodology (moderate-high).
- Out or low: news and commentary about research; general capabilities work (agent frameworks, optimisation tools,
  coding benchmarks, formal verification of ordinary code) without a safety focus; long-horizon or planning work
  unless it is tied to autonomous-agent risk; governance or policy position papers without technical depth; opinion
  pieces; summaries without new insight; system cards, which are treated as documentation (inclusion < 0.4) unless
  they bring new methodology; pure timelines speculation.

**Process (2025).** Over 3,300 candidate links came from an arXiv keyword sweep, ~120 researchers' papers, all
Alignment Forum posts, LessWrong posts under the "AI" tag, four link newsletters, a year of Twitter and conference
pages. An LLM pre-filtered and pre-assigned them conservatively, then the team did a full manual pass, about two
hours per agenda, with ~10 friendly reviewers on the draft and ~50 researchers checking their own areas. The site
reports 800+ included papers and posts across 80+ agendas; the repository README reports 827 items and 86 agendas.
SR states that it mostly captures legible work on legible problems despite trying to include early-stage work.

## (b) The 2025 taxonomy

Section, then subsection (where SR uses one), then agenda. The glosses condense SR's one-line summaries, and the
number in brackets is SR's paper count for the agenda.

- **Labs (giant companies)**: frontier companies' alignment and safety output, by company.
  - OpenAI [12], Google DeepMind [14], Anthropic [21], xAI [no count shown], Meta [6], China [0] (Alibaba,
    DeepSeek, Moonshot, Baidu, Z, MiniMax, ByteDance); `taxonomy.yaml` also has "Others" (Amazon, Microsoft,
    Mistral), which the site does not show.
- **Black-box safety**: understand and control current model behaviour through inputs, outputs and training.
  - *Iterative alignment*: nudging models by optimising their outputs during or after training.
    - Iterative alignment at pretrain-time [2]: guide the weights during pretraining.
    - Iterative alignment at post-train-time [16]: modify the weights after pretraining (RLHF and its successors).
    - Black-box make-AI-solve-it [12]: use existing models to improve and align further models.
    - Inoculation prompting [4]: elicit mild misbehaviour deliberately in training so that it does not generalise
      into broad misbehaviour.
    - Inference-time: in-context learning [5]: which runtime rules, guidelines or examples improve behaviour.
    - Inference-time: steering [4]: change representations or token probabilities without touching weights.
    - Capability removal: unlearning [18]: remove specific knowledge, capabilities or behaviours from a trained model.
    - Control [22]: protocols that extract useful work from possibly misaligned, subversive models while preventing
      sabotage.
    - Safeguards (inference-time auxiliaries) [6]: classifiers, monitors and rapid response against jailbreaks,
      prompt injection and harmful outputs.
    - Chain-of-thought monitoring [17]: supervise the natural-language reasoning to detect misalignment, scheming
      or deception.
  - *Model psychology*: emergent values, preferences, personae and behaviour patterns in LLMs.
    - Model values / model preferences [14]: measure and control emergent coherent value systems.
    - Character training and persona steering [13]: map and shape model personae, such as honesty versus
      sycophancy.
    - Emergent misalignment [17]: narrow fine-tuning on a bad task generalises into broad misalignment.
    - Model specs and constitutions [11]: written value and rule specifications, instilled through Constitutional AI,
      deliberative alignment and similar methods.
    - Model psychopathology [9]: odd LLM phenomena (glitch tokens, reversal curse, subliminal learning) as data for
      theory.
  - *Better data*: alignment through curating, filtering or generating training data.
    - Data filtering [4]: remove harmful or dual-use content from pretraining data.
    - Hyperstition studies [4]: stories about AI become training data and shape how AI behaves; study and intervene
      on that loop.
    - Data poisoning defense [3]: detect and keep out malicious or backdoor-inducing samples.
    - Synthetic data for alignment [8]: AI-generated critiques, preferences and labels for alignment.
    - Data quality for alignment [5]: signal-to-noise and reliability of human preference data.
  - *Goal robustness*
    - Mild optimisation [4]: satisficing instead of maximising, to avoid Goodharting.
    - RL safety [11]: reward learning, goal misgeneralisation and specification gaming in RL agents.
    - Assistance games, assistive agents [5]: formal models of assistants learning human preferences under
      uncertainty.
    - Harm reduction for open weights [5]: tamper-resistant safeguards against malicious fine-tuning.
    - The "Neglected Approaches" approach [3]: find and prototype overlooked empirical alignment ideas, for example
      self-other overlap.
- **White-box safety (interpretability)**: understand and control models through their internals. SR itself says the
  section "isn't very conceptually clean".
  - Reverse engineering [33]: circuits; decompose a model into components and verify what they compute.
  - Extracting latent knowledge [9]: decode what the model "really" believes, even when its output is false.
  - Lie and deception detectors [11]: white-box or black-box detectors of lying or deception.
  - Model diffing [9]: what fine-tuning changed between two models.
  - Sparse coding [44]: SAEs and dictionary learning; monosemantic features from polysemantic activations.
  - Causal abstractions [3]: verify that a network implements a given high-level causal model.
  - Data attribution [12]: trace behaviours back to the training data points that caused them.
  - Pragmatic interpretability [3]: lightweight probes and steering aimed at concrete safety problems instead of
    full reverse engineering.
  - Other interpretability [19]: catch-all.
  - Learning dynamics and developmental interpretability [14]: phase transitions and emergence during training
    (SLT, grokking).
  - Representation structure and geometry [13]: the shape of representations and whether the semantics can be read
    from the geometry.
  - Human inductive biases [6]: connections between deep learning and human brains and learning, aiming at an
    alignment "moonshot" built on a shared theory of learning.
  - *Concept-based interpretability*
    - Monitoring concepts [11]: directions or subspaces for concepts such as refusal, deception or eval awareness,
      used for audits and runtime monitors.
    - Activation engineering [15]: steer outputs by modifying activations.
- **Safety by construction**: assurances about outputs by design that still scale.
  - Guaranteed-Safe AI [5]: outputs with quantitative guarantees against a formal specification and world model.
  - Scientist AI [2]: powerful non-agentic, uncertainty-aware world models instead of agents.
  - Brainlike-AGI safety [6]: reverse-engineer the brain circuitry behind social and moral instincts, for brain-like
    model-based RL.
- **Make AI solve it**: AI systems help oversee and align other, possibly stronger, AI systems.
  - Weak-to-strong generalization [4]: weaker models supervise stronger ones.
  - Supervising AIs improving AIs [8]: frameworks and monitoring for AI-on-AI supervision and self-modification.
  - AI explanations of AIs [5]: automated feature descriptions, steering interfaces and behaviour-elicitation agents.
  - Debate [6]: true claims are easier to argue for than false ones; use that asymmetry to get trusted work out of
    untrusted debaters.
  - LLM introspection training [2]: train models to predict white-box results about themselves, to get
    self-explanation.
- **Theory**: principled scientific understanding of current and future systems (formal and philosophical
  foundations).
  - Agent foundations [10]: philosophical clarity and formalisation of agency, optimisation strength, decision
    theory, abstractions and concepts.
  - Tiling agents [4]: keeping alignment intact when an agent modifies itself or its successors.
  - High-Actuation Spaces [7]: substrate-independent inference of goals (a "telic DAG"), with category theory meant
    to guarantee the abstraction is valid.
  - Asymptotic guarantees [4]: show that a safety process converges to a specification in the limit of resources,
    using complexity, game and learning theory.
  - Heuristic explanations [5]: formal heuristic arguments, low-probability estimation, mechanistic anomaly
    detection (ARC).
  - *Corrigibility*
    - Behavior alignment theory [10]: formal models predicting properties of future AGI (power-seeking, shutdown).
    - Other corrigibility [9]: obstacles to robust corrigibility, plus mechanisms, tests and escalation channels.
  - *Ontology identification*
    - Natural abstractions [10]: a theory of concepts: how they are learned and how they translate between minds.
    - The Learning-Theoretic Agenda [6]: a mathematical theory of agents, with a formal notion of when two agents
      are aligned (infra-Bayesianism).
- **Multi-agent first**: alignment that treats AIs as participants in multi-agent and social settings, not as isolated
  optimisers.
  - Aligning to context [8]: align AI to roles inside real human practices and institutions instead of to separable
    goals or utilities.
  - Aligning to the social contract [8]: derive operational values from social-contract-style deliberation and the
    rules that follow from it.
  - Theory for aligning multiple AIs [12]: evolutionary and computational game theory, program equilibria, safe
    Pareto improvements, AI economies.
  - Tools for aligning multiple AIs [12]: build, test and audit multi-agent setups and their dynamics.
  - Aligned to who? [9]: plural values, cultures and communities; democratic and pluralistic alignment; gradual
    disempowerment.
  - Aligning what? [13]: alternatives to the single-agent frame: human-AI systems, institutions, economies, drives
    inside one model, hierarchical agency.
- **Evals**: measure capabilities and dangerous behaviours to inform deployment and research.
  - AGI metrics [5]: measures of progress towards human-level generality.
  - Capability evals [34]: test whether a model has a capability or propensity.
  - Autonomy evals [13]: long-horizon autonomous task completion.
  - WMD evals [6]: biological and chemical weapons knowledge and uplift.
  - Situational awareness and self-awareness evals [11]: knowledge of itself, its environment, and test versus
    deployment.
  - Steganography evals [5]: hidden information or encoded reasoning in outputs or chain of thought.
  - AI deception evals [13]: deception, alignment faking, manipulation.
  - AI scheming evals [7]: covert pursuit of misaligned long-term goals while faking compliance.
  - Sandbagging evals [9]: deliberate underperformance, especially under evaluation.
  - Self-replication evals [3]: obtaining its own weights and compute to copy itself.
  - Various red-teams [57]: attack current models to induce bad behaviour and test theories and methods.
  - Other evals [20]: honesty, shutdown resistance, sycophancy and similar.
- Outside the tree: **orgs without public outputs this year** (SSI, Conjecture's Cognitive Software, Orthogonal /
  QACI, modelingcooperation.com, Pr(Ai)²R, Astera Obelisk, Coordinal, Workshop Labs) and the **Graveyard** of agendas
  known to be inactive (adversarially robust augmentation and distillation, half of FAIR including JEPA, Science of
  Evals).

The cross-cutting axes are useful as labels but are not a second tree. The 13 *orthodox problems*: value is fragile
and hard to specify; corrigibility is anti-natural; pivotal processes require dangerous capabilities; goals
misgeneralise out of distribution; instrumental convergence; pivotal processes likely require incomprehensibly complex
plans; superintelligence can fool human supervisors; superintelligence can hack software supervisors; humans cannot
be first-class parties to a superintelligent value handshake; humanlike minds and goals are not necessarily safe;
someone else will deploy unsafe superintelligence first; a boxed AGI might exfiltrate itself; fair, sane pivotal
processes.

## (c) Required extensions and where they sit in SR 2025

| Area (scope decision) | Nearest SR 2025 entries | Status |
|---|---|---|
| Decision theory (CDT/EDT/UDT/FDT, logical counterfactuals, Newcomblike problems for agents) | Theory / Agent foundations (its gloss names decision theory; e.g. "Good old fashioned decision theory", ODYSSEY 2025); Learning-Theoretic Agenda (infra-Bayesian decision estimation, regret bounds) | partly covered; no agenda of its own |
| Pre-paradigmatic research | Theory (all of it); High-Actuation Spaces; Natural abstractions; Multi-agent first / Aligning what?; Neglected Approaches | covered in spirit, but SR says it mostly fails to catch illegible work |
| Wider field critiques | No agenda; critiques are attached per agenda (a `critiques` field), and SR's pre-filter scores technical critiques high. Field-level pieces appear only as framing links ("Legible vs illegible AI safety problems", "AI in 2025: gestalt") | extension: also keep field-level and methodological critiques and strategy-of-research pieces |
| Anthropics (SIA/SSA, observer selection, simulation arguments as they bear on AI) | none | extension |
| Acausal trade, ECL, multiverse-wide cooperation, values handshakes | Multi-agent first / Theory for aligning multiple AIs (program equilibria, safe Pareto improvements); orthodox problem 9 ("value handshake") | extension, adjacent to multi-agent theory |
| MIRI-style agent foundations (embedded agency, logical induction, tiling, Löbian obstacles, corrigibility) | Theory / Agent foundations, Tiling agents, Behavior alignment theory, Other corrigibility; Orthogonal/QACI under "orgs without public outputs" | covered |
| Psychology (developmental, moral, clinical, cognitive) | Black-box / Model psychology (the psychology *of* models); White-box / Human inductive biases; Multi-agent first / Aligning to context (e.g. human moral decision-making) | partly covered; transfers from human psychology to alignment need explicit inclusion |
| Biology and neuroscience | Safety by construction / Brainlike-AGI safety; Aligning what? ("A Phylogeny of Agents"); Theory for aligning multiple AIs ("The Pando Problem: rethinking AI individuality") | partly covered |
| Ecology, collective and complex systems | Aligning what? (multipolar AI, collective cooperative intelligence); Theory for aligning multiple AIs (evolutionary game theory, LLM populations) | partly covered |
| Behavioural economics, mechanism design | Theory for aligning multiple AIs ("An Economy of AI Agents"); Aligning what? ("AI Governance through Markets"); Human inductive biases ("cognitively-faithful decision-making models") | partly covered |
| Game theory | Theory for aligning multiple AIs (explicitly); Asymptotic guarantees; Assistance games; Debate | covered |

The common thread is that all of these are kept when the document connects its subject to aligning, understanding or
safely developing advanced AI, even if the connection is speculative and nothing in it applies to a current model.
The extension lowers the bar on *distance from deployed systems*. It does not lower the bar on *connection to AI*.

## (d) Inclusion and exclusion lines for labelers

Each line applies to a title and abstract (triage) and to full text (screen). "T+A" says how to decide from the
record, "Full" from the text. The examples are real SR 2025 inclusions unless marked hypothetical.

**Include** (verdict `relevant`; at triage `borderline` where stated):

- **I1. Agent foundations and decision theory.** Keep formal or philosophical work on agency, optimisation, embedded
  agency, decision theory (CDT, EDT, UDT, FDT, logical counterfactuals), logical uncertainty or bounded rationality
  when it aims at, or argues relevance to, designing or aligning AI agents. T+A: an AI, agent-design or alignment
  motive is visible, or the venue is AF/LW or an agent-foundations meeting (e.g. ODYSSEY) → relevant; decision theory
  with no visible AI angle → borderline. Full: an alignment motive anywhere in the text (often only in the intro or
  conclusion) → relevant. *"Good old fashioned decision theory"; "Formalizing Embeddedness Failures in Universal
  Artificial Intelligence".*
- **I2. Anthropics, acausal trade, ECL, simulation arguments, value handshakes.** Keep when the reasoning is about how
  AI agents, or a world with advanced AI, should reason, bargain or coordinate. T+A: AI, superintelligence or
  artificial agents mentioned → relevant, otherwise borderline. Full: AI connection present → relevant.
  *(Hypothetical: "Acausal coordination between superintelligences under logical uncertainty".)*
- **I3. Corrigibility, shutdown, power-seeking, tiling and self-modification**, as theorems, toy models or conceptual
  proposals, with or without experiments. *"Shutdownable Agents through POST-Agency"; "Working through a small tiling
  result".*
- **I4. Ontology identification and representation convergence** (natural abstractions, natural latents, shared or
  "Platonic" representations, representational alignment between humans and models), when framed as bearing on
  alignment, interpretability, or translating concepts between minds. *"Natural Latents: Latent Variables Stable
  Across Ontologies"; "The Platonic Representation Hypothesis".*
- **I5. Model psychology without a method**: studies of what LLMs value, prefer, believe or report about themselves,
  persona structure, odd phenomena (reversal curse, subliminal learning, glitch tokens), emergent misalignment, and
  hyperstition (stories about AI shaping AI). *"Subliminal Learning: Language models transmit behavioral traits via
  hidden signals in data"; "Utility Engineering: Analyzing and Controlling Emergent Value Systems in AIs".*
- **I6. Multi-agent and cooperative-AI theory**: program equilibria, commitments, safe Pareto improvements,
  collusion, evolutionary game theory of LLM populations, AI economies, gradual disempowerment. *"Characterising
  Simulation-Based Program Equilibria"; "Gradual Disempowerment: Systemic Existential Risks from Incremental AI
  Development".*
- **I7. Alignment targets and value aggregation**: pluralistic, democratic or social-choice alignment, thick models of
  value, law-following AI, "aligned to whom", when the document proposes or analyses what or whom AI systems should
  be aligned to. Institutional policy is not enough. *"Beyond Preferences in AI Alignment"; "Law-Following AI:
  designing AI agents to obey human laws".*
- **I8. Cross-disciplinary transfers**: psychology, neuroscience, biology, ecology, behavioural economics or game
  theory brought to aligning, understanding or safely designing advanced AI. Speculative and prospective proposals
  count. T+A: the field's concept and an AI-alignment target both appear → relevant. *"Perils of Under vs
  Over-sculpting AGI Desires" (brain-like AGI); "A Phylogeny of Agents"; (hypothetical) "Immune-system tolerance as a
  model for runtime AI monitoring".*
- **I9. Field critiques and research strategy**: critiques of an agenda's feasibility or of the field's methods,
  research agendas, open-problem lists, roadmaps, retrospectives, legibility and paradigm arguments. *"Agent
  foundations: not really math, not really science"; "Obstacles in ARC's agenda"; "Legible vs illegible AI safety
  problems".*
- **I10. Measuring generality and capability trends with a method**: AGI definitions and metrics, autonomy
  time-horizon measurements, capability forecasting from eval trends, situational-awareness evals. *"A Definition of
  AGI"; "HCAST: Human-Calibrated Autonomy Software Tasks"; "Forecasting Rare Language Model Behaviors".*

**Exclude** (out of both the Stack and SR scope; verdict `not_relevant` / `irrelevant`):

- **E1. Vocabulary collisions**, as in the rubrics, plus those of the new areas: "decision theory" in clinical or
  operations research, "agent-based model" in epidemiology or economics, "anthropic" in anthropology or the
  Anthropocene, "game theory" of markets or politics without AI, "cooperation" in microbiology.
- **E2. Fields with no AI connection**: decision theory, game theory, economics, psychology, biology or ecology that
  never mention AI, artificial agents or alignment (a Newcomb paper about human rationality, an auction design, a
  predator-prey model). T+A: unambiguous → not_relevant; title only and in the MIRI lineage (UDT, logical induction,
  Löb) → borderline. Full: no AI connection anywhere → irrelevant.
- **E3. Capabilities only** (unchanged): RL recipes for benchmark scores, inference speed-ups, agent frameworks,
  coding benchmarks without a safety or generality framing. SR also excludes "general capabilities evals". I10 is the
  exception.
- **E4. General ML theory without an alignment framing**: generalisation bounds, optimisation convergence, loss
  scaling laws. In only when the document ties it to alignment (SLT for safety, asymptotic safety guarantees).
- **E5. Applied AI for a domain problem** (unchanged): diagnosis, crop yield, materials discovery, even when called
  "trustworthy".
- **E6. Policy, law, governance or strategy** with neither a named safeguard (Stack) nor a technical alignment
  proposal (SR): national strategies, liability, competition, export controls, labour impacts. Gradual-disempowerment
  and alignment-target work belong under I6 and I7.
- **E7. News without content**: announcements, funding, hiring and event news, newsletter link round-ups, generic
  explainers. Round-ups are worth harvesting for links (see (e)), not keeping as documents.
- **E8. Opinion without argument or method**: timelines speculation, generic doom or optimism, advocacy, and fiction
  (hyperstition *studies* of such fiction are I5).
- **E9. AI moral status and rights without an alignment link**. SR lists AI rights as excluded. Keep the document
  when it reports model behaviour such as self-reports or preferences (I5), or a developer's welfare-motivated
  safeguard (a Stack deployment signal).
- **E10. Conventional security and resilience**: cybersecurity of ordinary IT, biosecurity preparedness, OSINT,
  societal resilience, when not about an AI system's own safeguards or evaluation. *(Hypothetical: "Hardening
  hospital networks against AI-assisted ransomware".)*

## (e) Source coverage: SR's sources versus our collectors

SR 2025's 782 unique included links (from the site's agenda data) break down as: arxiv.org 450 (58 %);
lesswrong.com 138 and alignmentforum.org 17; frontier-lab sites ~50 (alignment.anthropic.com 21, anthropic.com 11,
openai.com 8 plus subdomains, transformer-circuits.pub 4, deepmind.google 3); openreview.net 19, ten of them from the
ODYSSEY 2025 agent-foundations conference; safety-org blogs (METR 7, Transluce 5, Goodfire 3, UK AISI 3, Softmax 2,
plus single posts from Luthien, SPY Lab, Forethought, MIRI); personal sites and Substacks (turntrout.com,
joecarlsmith.com, a Tumblr, several Substacks); journals (Nature, PNAS, Science Advances, Philosophical Studies, AI
Magazine, ACM venues, MDPI); SSRN 2; GitHub 6; Google Docs/Drive 5; YouTube 1; tweets (t.co) 5. The 3,384-link
candidate pool came mostly from Zvi's link posts (1,561), arXiv papers of tracked researchers (377 + 186), curated
LessWrong (293), Lenz's paper list (279) and manual additions (~480).

Our collectors (`configs/channels.yaml`, `workflows/collect/`): `collect.openalex` (concept-group keyword sweep over
scholarly works, plus an institution sweep of labs, large companies and GovAI); `collect.semantic_scholar` (the same
groups, phrase-match, plus misuse terms ANDed with AI context); `collect.arxiv_sweep` (author sweep over
`configs/rosters/researchers.yaml`, 2,269 names, categories cs.AI, cs.LG, cs.CL, cs.CR, cs.CY, cs.MA, stat.ML);
`collect.forums` (LW and AF search, one query per concept-group term, 7 tags and 2 extra keywords, karma ≥ 25);
`collect.af_graphql` (every AF post, no karma gate); `collect.blogs` (seven lab sites: Anthropic alignment,
transformer-circuits, OpenAI alignment, OpenAI, Google Research, Mistral, DeepMind); `collect.cards` (lab model cards
and policy documents). The `*-lookup` stages only enrich documents already found; they do not discover new ones.

| SR source kind | Our coverage | Gap |
|---|---|---|
| arXiv keyword sweep ("AI alignment", "AI safety", "steerability" in title/abstract) | Indirect only: OpenAlex/S2 concept groups reach arXiv; no arXiv-native keyword sweep; "steerability" is in no concept group | partial; whether OpenAlex has full arXiv coverage and how fast it indexes is not checked |
| arXiv by researcher | `arxiv_sweep`, but its category allowlist omits cs.GT, cs.LO, cs.NE, math.\*, econ.\*, q-bio.NC, where game theory, decision theory and logic work appear. The bootstrap roster has Demski, Garrabrant, Critch, Christiano, Soares, Yudkowsky, Dalrymple, Kulveit, Bengio and Russell; a grep finds no Kosoy, Oesterheld, Conitzer, Byrnes, Wentworth, Murfet or Shai (the production roster CSV was not checked) | partial |
| Alignment Forum, all posts | `af_graphql`, complete | none |
| LessWrong, every post under the AI tag (lesswrong.com/w/ai) | keyword and tag search with karma ≥ 25 | **main gap for the extension**: low-karma or off-keyword posts on decision theory, anthropics, acausal trade, abstractions, brain-like AGI and field critiques are exactly what this misses |
| Concept-group vocabulary | The 42 groups cover SR's empirical agendas well. `channels.yaml` has no term for decision theory, acausal, anthropics, infra-Bayesianism, natural latents or abstractions, tiling, program equilibrium, game theory, gradual disempowerment, pluralistic alignment, hyperstition, introspection, AGI metrics, heuristic arguments, brain-like AGI, Scientist AI or steerability | gap. Broad terms such as "decision theory" need an AI-context AND, as the misuse groups already have |
| Link newsletters (Zvi; Gasteiger's AI Safety Frontier; Paleka; Lenz's aisafetypapers.com) | none | gap: SR's largest candidate source |
| Twitter/X | the `x` channel is declared but has no collector | gap |
| Frontier-lab blogs and cards | `blogs`, `cards` | covered (Meta and xAI only through cards) |
| Safety-org and research-org blogs (METR, Redwood, Apollo, ARC, Transluce, Goodfire, UK AISI, Epoch, Forethought, FAR.AI, Timaeus, CLR, MIRI, Softmax, Meaning Alignment, Cooperative AI Foundation) | none | gap |
| Personal sites and Substacks (turntrout.com, joecarlsmith.com, Substacks) | none | gap |
| OpenReview conferences and workshops (ODYSSEY, NeurIPS/ICML/ICLR safety workshops) | only if OpenAlex or S2 index them and a term matches | gap (not checked whether ODYSSEY is indexed) |
| Theory venues (COLT/PMLR, STOC, AAMAS, EPTCS/TARK) and journals (Nature, PNAS, Science Advances) | OpenAlex/S2 keyword match only | partial, limited by the vocabulary |
| Philosophy venues (Philosophical Studies, Synthese, Minds & Machines, AI & Society; PhilPapers/PhilArchive) | OpenAlex keyword only; no PhilPapers source | partial |
| Critique discovery (LW backlinks, Google Scholar cited-by, Ahrefs) | no citation or backlink walk | gap |
| SSRN, Google Docs, GitHub, YouTube talks | `manual` drop folder only | gap, low volume |

The repository's `main-pipeline/data/2025-12-16-draft-post-review/papers.csv` (827 included items with agenda labels)
and `data-dump-classify.csv` (3,384 candidates with SR's LLM scores) can serve directly as an SR-scope recall set
and source of hard negatives for the triage and screen evals.

## (f) Rubric lines in `evals/rubrics/triage.md` and `screen.md` that the SR scope changes

1. **The technique anchor** (both, "What is in scope"): "A document is in scope when it tells a reader something
   about one or more such techniques", together with "A concrete technique is something a methods catalogue could
   have an entry for". This is the root change. Add a second route into scope: a contribution to understanding or
   aligning advanced AI at the agenda level, with no technique required (I1 to I10). Flips: "Natural Latents: Latent
   Variables Stable Across Ontologies", from `not_relevant`/`borderline` to `relevant`.
2. **"Not AI: no AI system involved."** (both). It must mean "no connection to AI": general-agent decision theory,
   anthropics or neuroscience argued to bear on alignment is in (I1, I2, I8). Flips: "Good old fashioned decision
   theory"; Byrnes's "Perils of Under vs Over-sculpting AGI Desires".
3. **"General ML or applications: generic ML methods"** (both). Add exceptions for model psychology and
   psychopathology, representation geometry and convergence, and learning dynamics of general-purpose models (I4,
   I5). Flips: "The Platonic Representation Hypothesis"; "Artificial Hivemind: The Open-Ended Homogeneity of Language
   Models"; "Believe It or Not: How Deeply do LLMs Believe Implanted Facts?".
4. **"Ethics or policy with no technique"** and the governance hard case "Out when it is about institutions,
   liability, competition, geopolitics, labour, ethics principles" (both). Add an exception for alignment-target and
   multi-agent-first work (I6, I7). Flips: "Gradual Disempowerment"; "An Economy of AI Agents"; "Democratic AI is
   Possible. The Democracy Levels Framework Shows How It Might Work"; "Law-Following AI".
5. **"Capabilities only"** and the capabilities hard case (both). Add an exception for measures of generality and
   autonomy and for capability forecasting with a method (I10). Flips: "A Definition of AGI"; "GDPval"; "Remote Labor
   Index". SR itself is in tension here: its scope excludes "general capabilities evals", yet it has an AGI-metrics
   agenda.
6. **"No technique content"** and the *Judgement* kind ("critique ... arguments about an agenda") (both). Widen
   Judgement to field-level critiques, research strategy, legibility and paradigm arguments, and open-problem lists
   (I9). Flips: "Legible vs illegible AI safety problems"; "What Is The Alignment Problem?".
7. **Thin records** (triage). Title-only records from AF/LW or agent-foundations venues with theory vocabulary should
   be `borderline` or `relevant`, not `not_relevant`. Flips (skip to fetch): "Condensation"; "What, if not agency?";
   "Is alignment reducible to becoming more coherent?".
8. **Vocabulary collisions** (both). Add the collisions of the new areas (E1). These prevent false positives and flip
   nothing.
9. **Screen `buckets`** ("decide which later readers see them"). No bucket separates SR-only documents. A kept
   theory paper would get `paper` and go to technique readers, while Stack-only documents (system cards, release
   news, marketing, watermarking, KYC, all outside SR's scope or below its system-card bar) would go into any SR
   export. A scope flag, for example `scope: [stack, sr]` or an `sr_agenda` bucket, would let each consumer filter.
   This is a design decision for the rubric owner.

Unchanged: the tie-breaks, unusable text, applied AI, and capabilities-only work aimed at task scores.
