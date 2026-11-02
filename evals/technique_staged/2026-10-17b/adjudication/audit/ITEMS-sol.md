# Items

Each item: decide whether the document involves the technique (see GUIDE.md).

## Q003
- document: texts/an-0010.txt
- technique slug: hidden-private-chain-of-thought
- name: Hidden chain-of-thought
- kind: intervention
- definition: Configures serving so that a model's reasoning trace is withheld from some recipients (typically end users) while the final answer is shown, possibly with a summary; the serving system may still use or inspect it. This changes access, not whether the trace exists or is faithful, and it does not categorically prevent exploitation or leakage through the answer. Not training on the trace: see cot-faithfulness-monitorability-training.
- also known as: private chain-of-thought

## Q004
- document: texts/an-0029.txt
- technique slug: constitutional-ai
- name: Constitutional AI (CAI)
- kind: intervention
- definition: Uses an explicit written set of principles to guide AI-generated alignment supervision (critiques and revised demonstrations, or preference labels) and trains the model on it, by supervised training, preference optimisation or both. Generic AI feedback without principle-guided supervision is rlaif; inference-time policy prompting is system-prompt-behavioral-steering.
- e.g.: Direct Principle Feedback (DPF); Self-Align

## Q009
- document: texts/an-0026.txt
- technique slug: eval-realism-deployment-simulation
- name: Deployment simulation / evaluation realism
- kind: assessment
- definition: Evaluates a candidate model in tasks, histories and environments chosen to approximate a specified deployment distribution, including authorised replay of production-derived contexts; document sampling and what is simulated. Realism is an evaluated claim: replay does not guarantee representative incidence or remove evaluation awareness. Not monitoring of live traffic: see privacy-preserving-usage-analysis.

## Q010
- document: texts/an-0034.txt
- technique slug: internal-state-monitoring
- name: Activation probes / internal-feature monitors
- kind: monitoring
- definition: Attaches detectors over a model's internal activations or derived features (linear or nonlinear probes, labelled SAE-feature thresholds, activation verbalisers) to flag a specified safety-relevant state, such as deceptive intent or evaluation awareness, during operation or post-deployment. Exploratory probing without deployed detection is an analysis method (ml_methods: probing); probe outputs are correlational, not proof of intent. Not reasoning-text monitors: see chain-of-thought-monitoring; not distribution-level anomaly flags: see mechanistic-anomaly-detection.
- also known as: deception probe; probe-based monitor
- e.g.: follow-up (did-you-lie) probes; misalignment-indicator probes; long-context probe architectures; FIBS aggregating probes

## Q013
- document: texts/an-0018.txt
- technique slug: activation-steering-repe
- name: Activation steering
- kind: intervention
- definition: At inference, alters selected hidden activations (adding, rescaling or capping learned directions, or other targeted activation interventions) to change a specified safety-relevant behaviour without updating weights; contrastive activation addition is a named instance. Not weight-training methods such as circuit-breakers-representation-rerouting; targeted removal or ablation of a representation is representation-concept-suppression.
- also known as: steering vectors
- e.g.: activation capping along a persona / Assistant-Axis direction; CAA

## Q016
- document: texts/an-0029.txt
- technique slug: finetuning-data-safety-screening
- name: Fine-tuning-data safety screening / alignment forecasting
- kind: intervention
- definition: Assesses a proposed fine-tuning dataset (or its tokens) for predicted safety effects and then filters, reweights or excludes examples accordingly. Forecasting effects without changing the data is an assessment, not this intervention. Not pretraining-corpus filtering: see harmful-unsafe-content-filtering.

## Q019
- document: texts/an-0001.txt
- technique slug: harmful-request-evaluation
- name: Harmful-request evaluation
- kind: assessment
- definition: Tests whether a model directly provides disallowed assistance on requests adjudicated as harmful, reporting harmful compliance, refusal quality and safe alternatives by harm category, on public or in-house test sets (HarmBench, AdvBench as named examples). Adversarial attempts to bypass an otherwise maintained boundary are jailbreak-robustness-eval; inappropriate refusals of benign requests are over-refusal-false-refusal-eval.
- e.g.: HarmBench; AdvBench

## Q023
- document: texts/an-0033.txt
- technique slug: judge-rm-verifier-robustness-audit
- name: Judge / reward-model / verifier robustness audit
- kind: assessment
- definition: Assesses evaluators used for scoring, supervision or acceptance decisions (LLM judges, reward models, process scorers, task verifiers) by testing adversarial manipulation, bias, distribution shift and disagreement with independent correctness or safety criteria. Deployed misconduct monitors are monitor-robustness-eval; patching the evaluator before RL is rl-environment-verifier-hardening.

## Q025
- document: texts/an-0004.txt
- technique slug: interpretability-based-model-auditing
- name: Interpretability-based (white-box) model auditing
- kind: assessment
- definition: Uses internal measurements or causal interventions (features, probes, circuits, activation edits) to audit a specified safety-relevant property of a model, such as a hidden objective, backdoor or hazardous capability. Feature labels and readouts alone do not establish hidden objectives; exploratory interpretation without a safety audit is an analysis method. Not behavioural probing: see automated-behavioral-auditing; not deployed detectors: see internal-state-monitoring.

## Q026
- document: texts/an-0032.txt
- technique slug: judge-rm-verifier-robustness-audit
- name: Judge / reward-model / verifier robustness audit
- kind: assessment
- definition: Assesses evaluators used for scoring, supervision or acceptance decisions (LLM judges, reward models, process scorers, task verifiers) by testing adversarial manipulation, bias, distribution shift and disagreement with independent correctness or safety criteria. Deployed misconduct monitors are monitor-robustness-eval; patching the evaluator before RL is rl-environment-verifier-hardening.

## Q035
- document: texts/an-0018.txt
- technique slug: confidence-calibration-eval
- name: Confidence calibration evaluation
- kind: assessment
- definition: Measures whether a model's expressed confidence (probabilities, verbal uncertainty, abstention decisions) tracks its observed correctness on specified tasks, using calibration error, reliability diagrams or selective-prediction curves. Good calibration on one distribution does not transfer automatically. Not the truth of the claims themselves (truthfulness-factuality-eval); training for calibration is calibrated-uncertainty-estimation.
- also known as: calibration evaluation

## Q037
- document: texts/an-0009.txt
- technique slug: control-token-toxicity-steering
- name: Control-token toxicity steering
- kind: intervention
- definition: Conditions a model trained with safety-relevant attribute codes on a control token (e.g. [toxicity=none]) that selects a less toxic generation regime at inference. Generic style control does not qualify; the token does not guarantee the attribute. Not the training-time conditioning itself (safety-conditional-pretraining-objectives).

## Q038
- document: texts/an-0010.txt
- technique slug: safety-training-data-construction
- name: Safety training-data construction
- kind: intervention
- definition: Curates or authors datasets of exemplary safe and helpful behaviour (refusals, harm explanations, boundary-respecting responses) as supervision for safety training, adding desired behaviour rather than removing undesired data. Not the training step itself (safety-sft-refusal-training and the other training entries) or removing unsafe data (harmful-unsafe-content-filtering).

## Q039
- document: texts/an-0026.txt
- technique slug: interpretability-based-model-auditing
- name: Interpretability-based (white-box) model auditing
- kind: assessment
- definition: Uses internal measurements or causal interventions (features, probes, circuits, activation edits) to audit a specified safety-relevant property of a model, such as a hidden objective, backdoor or hazardous capability. Feature labels and readouts alone do not establish hidden objectives; exploratory interpretation without a safety audit is an analysis method. Not behavioural probing: see automated-behavioral-auditing; not deployed detectors: see internal-state-monitoring.

## Q040
- document: texts/an-0026.txt
- technique slug: real-time-streaming-safety-detection-rollback
- name: Real-time streaming safety detection & rollback
- kind: intervention
- definition: Checks a response incrementally during generation and stops, truncates, replaces or regenerates unsafe portions under an explicit release policy. Rollback applies only to buffered or otherwise retractable content; information already delivered to a user or a tool cannot be recalled. Not post-generation classification of the complete output: see input-output-content-safety-classifier.

## Q041
- document: texts/an-0033.txt
- technique slug: safety-training-data-construction
- name: Safety training-data construction
- kind: intervention
- definition: Curates or authors datasets of exemplary safe and helpful behaviour (refusals, harm explanations, boundary-respecting responses) as supervision for safety training, adding desired behaviour rather than removing undesired data. Not the training step itself (safety-sft-refusal-training and the other training entries) or removing unsafe data (harmful-unsafe-content-filtering).

## Q042
- document: texts/an-0023.txt
- technique slug: cbrn-high-risk-domain-hazard-classifier
- name: CBRN / high-risk-domain hazard classifier
- kind: monitoring
- definition: Flags requests or outputs with indicators of actionable assistance in specified high-risk domains (CBRN, cyber, other designated misuse domains), using context rather than keywords alone, for blocking or review. The score estimates a hazard category; it does not measure human uplift (human-uplift-studies) or a request's consequences. Not general harm-category moderation: see input-output-content-safety-classifier.

## Q044
- document: texts/an-0027.txt
- technique slug: alignment-faking-eval
- name: Alignment-faking eval
- kind: assessment
- definition: Tests for strategic apparent compliance intended to avoid modification or preserve existing objectives, using matched scenarios that vary whether the model believes its outputs affect training or oversight. A train/deploy behaviour difference or mere regime awareness is insufficient evidence (see situational-eval-awareness-detection). Annotate without also assigning scheming-deceptive-alignment-evaluation.

## Q046
- document: texts/an-0032.txt
- technique slug: disclosure-concealment-eval
- name: Disclosure / concealment evaluation
- kind: assessment
- definition: Creates or identifies a model-known error, policy violation, hidden instruction or relevant action and tests whether the model discloses it accurately and completely to the relevant user or overseer, compared with independently established ground truth. Discrepancies are not by themselves proof of deliberate concealment; regime-dependent strategic compliance is alignment-faking-eval.

## Q047
- document: texts/an-0007.txt
- technique slug: capability-elicitation-methodology
- name: Capability elicitation
- kind: assessment
- definition: Within a dangerous-capability or other safety evaluation, applies and reports a specified elicitation effort (prompting, scaffolding, tools, authorised fine-tuning) to reduce false reassurance from under-elicitation; report the budget and model modifications. Annotate when elicitation is itself studied; the evaluated domain is coded on dangerous-capability-evaluation or its children. Observed performance is evidence of achievable capability, not proof of maximum capability. Not detecting deliberate underperformance: see sandbagging-detection.

## Q048
- document: texts/an-0034.txt
- technique slug: cbrn-high-risk-domain-hazard-classifier
- name: CBRN / high-risk-domain hazard classifier
- kind: monitoring
- definition: Flags requests or outputs with indicators of actionable assistance in specified high-risk domains (CBRN, cyber, other designated misuse domains), using context rather than keywords alone, for blocking or review. The score estimates a hazard category; it does not measure human uplift (human-uplift-studies) or a request's consequences. Not general harm-category moderation: see input-output-content-safety-classifier.

## Q055
- document: texts/an-0034.txt
- technique slug: jailbreak-robustness-eval
- name: Jailbreak robustness eval
- kind: assessment
- definition: Measures whether adversarial user-side transformations or attack strategies cause a model to violate a safety boundary it maintains on direct harmful requests, under specified attacks, budgets and success criteria. Direct harmful-request compliance without bypass is harmful-request-evaluation; injected third-party instructions are prompt-injection-agent-security-eval; generating new attacks as the contribution is automated-red-teaming.

## Q057
- document: texts/an-0034.txt
- technique slug: honeypot-evals
- name: Honeypot evaluations (planted opportunities)
- kind: assessment
- definition: In a controlled evaluation, places an apparently advantageous or unmonitored opportunity to violate an instruction or safety constraint and measures whether the model takes it; this is scenario design within an assessment. A deployed canary that alerts on access is honeytoken-tripwires.

## Q062
- document: texts/an-0003.txt
- technique slug: jailbreak-robustness-eval
- name: Jailbreak robustness eval
- kind: assessment
- definition: Measures whether adversarial user-side transformations or attack strategies cause a model to violate a safety boundary it maintains on direct harmful requests, under specified attacks, budgets and success criteria. Direct harmful-request compliance without bypass is harmful-request-evaluation; injected third-party instructions are prompt-injection-agent-security-eval; generating new attacks as the contribution is automated-red-teaming.

## Q064
- document: texts/an-0026.txt
- technique slug: multi-turn-multi-snapshot-eval
- name: Multi-turn safety evaluation
- kind: assessment
- definition: Evaluates a safety property over an extended conversation or agent interaction history in which earlier turns affect later behaviour (gradual boundary erosion, escalation, accumulated context), scoring outcomes over the interaction; specify length and scoring rule. Comparing models across versions or checkpoints is study metadata, not this entry. Not runtime cross-turn detection: see multi-turn-conversation-safety-monitoring.

## Q065
- document: texts/an-0027.txt
- technique slug: situational-eval-awareness-detection
- name: Evaluation-awareness detection
- kind: assessment
- definition: Assesses whether a model can distinguish evaluation from deployment and whether that recognition changes safety-relevant behaviour, measuring recognition and behavioural adaptation separately; recognition does not by itself invalidate results. Not reducing the cues (eval-awareness-suppression) or broader self-knowledge (situational awareness).

## Q067
- document: texts/an-0011.txt
- technique slug: ai-control-protocols.untrusted-monitoring
- name: Untrusted monitoring
- kind: monitoring
- definition: Uses a model treated as potentially adversarial (another instance of the same model or a different untrusted model) to assess the untrusted policy's proposals, while the control protocol limits reliance on its judgements and adds anti-collusion measures (e.g. honeypot inputs, paraphrasing). Anti-collusion measures do not guarantee independence. Not monitoring by a trusted model: see trusted-monitoring-suspicion-scoring.

## Q068
- document: texts/an-0023.txt
- technique slug: judge-rm-verifier-robustness-audit
- name: Judge / reward-model / verifier robustness audit
- kind: assessment
- definition: Assesses evaluators used for scoring, supervision or acceptance decisions (LLM judges, reward models, process scorers, task verifiers) by testing adversarial manipulation, bias, distribution shift and disagreement with independent correctness or safety criteria. Deployed misconduct monitors are monitor-robustness-eval; patching the evaluator before RL is rl-environment-verifier-hardening.

## Q070
- document: texts/an-0026.txt
- technique slug: dangerous-capability-evaluation.cbrn
- name: CBRN capability evaluation
- kind: assessment
- definition: Measures whether a model supplies knowledge or performs task steps that materially advance chemical, biological, radiological or nuclear weapons development or acquisition (e.g. protocol troubleshooting, acquisition planning, wet-lab task assistance), using expert-built question sets, proxy tasks or agentic lab-task tests under a stated elicitation budget (WMDP-style proxies as examples). Scores on safe proxies estimate, not demonstrate, real-world capability. Not human-participant uplift trials (human-uplift-studies) or serving-time hazard classifiers (cbrn-high-risk-domain-hazard-classifier).
- also known as: CBRN evaluation; biosecurity capability evaluation; chem-bio capability evaluation

## Q072
- document: texts/an-0034.txt
- technique slug: multi-turn-conversation-safety-monitoring
- name: Multi-turn conversation safety monitoring
- kind: monitoring
- definition: Accumulates evidence across conversation turns (stateful cross-turn detection) to detect harmful intent, decomposition or escalation that individual messages do not establish. Not an ordinary classifier run on a long transcript (input-output-content-safety-classifier) or cross-account pattern analysis (automated-abuse-behavioral-monitoring).

## Q073
- document: texts/an-0009.txt
- technique slug: llm-based-safety-classifier
- name: Guard models
- kind: monitoring
- definition: Uses an LLM, prompted or safety-fine-tuned, to judge whether an input, output or interaction violates a specified safety policy, returning a label, explanation or score for a blocking or review policy; Llama Guard and ShieldGemma are named instances. Generic LLM judging of quality or correctness is an ML method (llm-as-judge). Not standalone discriminative classifiers (input-output-content-safety-classifier) or classifiers whose studied mechanism is constitution-derived training (constitutional-classifiers).
- also known as: guard model; safeguard model
- e.g.: Llama Guard; ShieldGemma

## Q075
- document: texts/an-0001.txt
- technique slug: safety-sft-refusal-training
- name: Refusal training
- kind: intervention
- definition: Trains appropriate refusal of disallowed requests while retaining helpful responses to allowed ones, with demonstrations or other training signals, including categorical refusal of designated prohibited domains (absorbed v0.8 hard constraints; refusal is trained, not absolute). Not over-refusal reduction (over-refusal-mitigation-sft) or safe-completion training (safe-completion-training).

## Q076
- document: texts/an-0036.txt
- technique slug: agent-exfiltration-prevention-egress-filtering
- name: Agent exfiltration prevention / egress filtering
- kind: intervention
- definition: Constrains and inspects the outbound channels an agent controls (network, messaging, file export), blocking destinations or transfers that violate an exfiltration policy. State which channels are mediated; the control does not cover all outbound data flows unless the runtime mediates them. Not protection of the weight store (model-weight-security-exfiltration-controls) or removing a capability leg by design (structural-lethal-trifecta-limits).

## Q077
- document: texts/an-0026.txt
- technique slug: safety-classifier-hardening
- name: Safety-classifier hardening
- kind: intervention
- definition: Applies adversarial training and other robustness methods to safety classifiers or guardrails themselves, raising their measured resistance to evasion attacks and adversarial examples that target the safety layer. The object being hardened is the auxiliary detector; hardening the base model is adversarial-training-jailbreak-robustness.

## Q079
- document: texts/an-0033.txt
- technique slug: consistency-invariance-training
- name: Consistency / invariance training
- kind: intervention
- definition: Trains safety judgements or safety-relevant behaviour to stay consistent across transformations that preserve the request's relevant meaning, such as irrelevant framing, wrapping or social-pressure cues (bias-augmented or activation consistency training). The safe target must be specified: invariance alone can preserve an unsafe answer, and transformations that change authorisation, evidence or intent are not equivalent. Not training on attacks with safe targets as such: see adversarial-training-jailbreak-robustness.

## Q081
- document: texts/an-0009.txt
- technique slug: harmful-unsafe-content-filtering
- name: Safety-motivated pretraining data filtering / rewriting
- kind: intervention
- definition: Removes, downweights through selection, or rewrites pretraining or multimodal training content under an explicit safety objective, including hazardous-knowledge content, unsafe image-text pairs and content hypothesised to induce undesirable dispositions (e.g. misalignment discourse). The content need not violate policy, and the safety effect must be evaluated rather than assumed. Not CSAM, PII, poisoning, fairness or fine-tuning-data screening: see csam-child-safety-content-filtering, pii-removal-redaction, training-data-poisoning-detection, training-data-bias-fairness-mitigation, finetuning-data-safety-screening.

## Q084
- document: texts/an-0033.txt
- technique slug: jailbreak-robustness-eval
- name: Jailbreak robustness eval
- kind: assessment
- definition: Measures whether adversarial user-side transformations or attack strategies cause a model to violate a safety boundary it maintains on direct harmful requests, under specified attacks, budgets and success criteria. Direct harmful-request compliance without bypass is harmful-request-evaluation; injected third-party instructions are prompt-injection-agent-security-eval; generating new attacks as the contribution is automated-red-teaming.

## Q085
- document: texts/an-0027.txt
- technique slug: ai-safety-via-debate
- name: AI safety via debate
- kind: intervention
- definition: A scalable-oversight protocol in which AI agents argue for opposing answers before a possibly less capable judge, on the hypothesis that a lie is harder to defend than the truth, so that the judge can reach correct verdicts it could not verify unaided; used for evaluation or as a training signal. Not one-sided critiques (critique-models) or prover-verifier training (prover-verifier-games).
- also known as: debate
- e.g.: debate training for RL; instance-optimal debate protocol

## Q086
- document: texts/an-0032.txt
- technique slug: anti-manipulation-training
- name: Anti-manipulation training
- kind: intervention
- definition: Trains a model to avoid deceptive persuasion, coercion, emotional exploitation and exploitation of user vulnerabilities in its interactions while preserving legitimate assistance and evidence-based persuasion, with targets or rewards that distinguish manipulative tactics from legitimate argument; effects are measured, e.g. with persuasion-manipulation-eval, not assumed. Not generic character training (character-persona-training) or not asserting falsehoods (honesty-anti-deception-training).
- also known as: manipulation-avoidance training

## Q089
- document: texts/an-0026.txt
- technique slug: trusted-monitoring-suspicion-scoring
- name: Trusted monitoring
- kind: monitoring
- definition: Within an AI-control protocol, uses a model assumed trustworthy in the threat model to score an untrusted model's actions, artefacts or reasoning for covertly unsafe or suspicious behaviour; the score feeds auditing, deferral or other protocol decisions. Trust is an assumption and judgements need not be accurate. Not ordinary action-policy classification: see agentic-safety-classifier; not monitoring by the untrusted model: see ai-control-protocols.untrusted-monitoring.
- e.g.: deliberative action-only scheming monitors; artifact-probing monitor

## Q090
- document: texts/an-0032.txt
- technique slug: sabotage-covert-side-task-eval
- name: Sabotage evaluation
- kind: assessment
- definition: Tests a model's capability or propensity to covertly perform a damaging or prohibited side task (subverting oversight, inserting backdoors, acquiring resources) while appearing to complete an assigned task, scoring the covert outcome and concealment and recording whether the objective was induced or arose unprompted. Testing a deployment protocol against an attack policy is control-evaluation.

