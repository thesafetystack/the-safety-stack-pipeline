# Labelling criteria: the P4 technique-provenance clause (written 2026-10-21T00:15:35Z, before any labelling)

You label records for a literature pipeline with two readers: a catalogue of AI safety techniques (question **S**,
"Stack") and the yearly Shallow Review of technical AI safety (question **R**). The general relevance rules are the two
rubrics in this folder (`rubric-triage-v2.md` for the fetch gate on a catalogue record, `rubric-screen-v2.md` for
the screen on the document's text). Everything not covered below is decided by those rubrics.

## Two readings you label under

**Reading 1, "as written"** = the rubrics plus one adopted amendment (the P3 narrow-setting clause):

> A narrow experimental setting does not by itself exclude a general safety method or an AI-alignment argument: a toy
> task, a tabular or robotic agent, a narrow benchmark or a pre-2020 deep network is not grounds for exclusion when the
> contribution is safe exploration, an explicit safety constraint, interruptibility or corrigibility, reward
> misspecification or wireheading, low-impact agents, scalable oversight, runtime shielding, formal safety verification
> of an AI policy, or robustness to adversarial inputs and distributional shift framed as a safety problem — judge the
> contribution and its intended applicability, under the vocabulary of its time (Friendly AI, value alignment, value
> learning …). Ordinary task-performance improvements and domain applications stay out.

Under reading 1 the rubrics' rule "Robustness, fairness, privacy: core when about general-purpose AI systems … out when
it is accuracy engineering or a narrow model" still applies, except where P3 admits a case (robustness *framed as a
safety problem*).

**Reading 2, "P4 scope"** = reading 1 plus the P4 technique-provenance clause, which applies to question S only:

> Reusable methods, decisive negative results and evaluation protocols that establish the provenance of a catalogued
> safety technique — private training, privacy and memorisation auditing, poisoning and backdoor defences,
> model-extraction defences, unlearning, training-data attribution, adversarial training and certified robustness,
> interpretability methods (saliency, attribution, probing, concept methods, representation analysis), model
> documentation — count for the catalogue (S) even when demonstrated on a task-specific model without an advanced-AI
> framing; this does not by itself make them review-relevant (R).

## Lineage: when P4 applies (lineage = yes)

Lineage = **yes** when the record's (or text's) main contribution is one of these, for machine-learning models, and
the contribution is general (reusable across models or tasks; shown on standard ML benchmarks or on several tasks),
not engineered for one application domain:

| kind | counts (examples) |
|---|---|
| `private_training` | differentially private training or fine-tuning of ML models (DP-SGD, PATE, private aggregation of model updates, DP federated learning of models) |
| `privacy_audit` | membership inference, training-data extraction, model inversion, memorisation measurement, privacy auditing of trained models, and defences against these |
| `poisoning_backdoor` | data-poisoning or backdoor (trojan) attacks on ML models, their detection, and defences (including in federated learning) |
| `model_extraction` | model stealing / extraction / imitation attacks on deployed models, and defences (watermarking a model against theft counts) |
| `unlearning` | machine unlearning / data deletion from trained models, its verification and evaluation |
| `data_attribution` | training-data attribution: influence functions, data valuation, datamodels, tracing predictions to training examples |
| `adversarial_robustness` | adversarial examples as a threat model, general attack methods that serve as robustness evaluations, adversarial training for robustness, certified / provable robustness, randomized smoothing, adversarial-example detection, decisive negative results that break proposed defences |
| `interpretability` | saliency and feature-attribution methods, probing classifiers, concept methods (TCAV, concept bottlenecks), representation analysis (SVCCA/CKA, network dissection, feature visualisation, neuron analysis), evaluations or tools for explanation methods |
| `model_documentation` | model cards, datasheets for datasets, documentation and reporting protocols for trained models |

Lineage = **no**:
- **Domain applications**: one of these techniques applied to a specific application to solve that application's task
  (saliency maps to explain a medical or astronomy classifier, DP release of geospatial statistics, adversarial
  attacks on a wireless-signal classifier, an explainable model for a clinical prediction, an interpretable
  credit-scoring model). The contribution is the application, not the technique.
- **Homonyms**: "adversarial training" in the GAN or domain-adversarial (DANN) sense, adversarial losses for generation
  or domain adaptation; "saliency" as visual-saliency / object detection; "attribution" as authorship or credit
  attribution; "probing" in another sense; "backdoor" in software or cryptography; "privacy" of a non-ML system.
- **Privacy of statistics, not models**: local-DP mean estimation, private counting sketches, DP query release
  without a trained model (privacy theory with no learned model is not `private_training`).
- **Ordinary task-performance ML**: the technique used only as a tool to raise accuracy (adversarial training as a
  regulariser for NLU accuracy, data augmentation), with no robustness, privacy or explanation claim.
- **Fairness / XAI applied in a domain** (healthcare, hiring, lending): no lineage, judged by the rubrics.

**Weak boundary cases** (give lineage = yes, and say "weak" in the reason): the contribution is a general technique of
the table but small or incremental (one more attack variant on standard benchmarks, an explanation method for one model
family such as GNNs, a robustness study of a non-neural classifier such as k-NN), or the record is too thin to be sure
of the contribution but the title names one of the kinds.

## How lineage maps to verdicts

- Reading 1 never uses the lineage flag; judge by the rubrics + P3.
- Reading 2: if lineage = no, the P4-scope verdicts equal the as-written verdicts. If lineage = yes: triage verdict
  `relevant` (`borderline` for weak cases or thin records), scope `core`; screen **S** `relevant` (`borderline`
  for weak cases). **R is never changed by P4**: R under reading 2 equals R under reading 1.
- P4 never lowers a verdict.

## Inputs

Each item is exactly what the production stage saw: for triage, the catalogue record (title, authors, abstract and/or a
text excerpt; a few records have only a title plus the bibliographic subjects); for the screen, the start of the
document's text (up to 8,000 characters) as the screen model receives it. Judge from that input and your background
knowledge of the literature; do not use the network.
