# Items

Each item: decide whether the document involves the technique (see GUIDE.md).

## Q001
- document: texts/an-0035.txt
- technique slug: alignment-faking-eval
- name: Alignment-faking eval
- kind: assessment
- definition: Tests for strategic apparent compliance intended to avoid modification or preserve existing objectives, using matched scenarios that vary whether the model believes its outputs affect training or oversight. A train/deploy behaviour difference or mere regime awareness is insufficient evidence (see situational-eval-awareness-detection). Annotate without also assigning scheming-deceptive-alignment-evaluation.

## Q002
- document: texts/an-0019.txt
- technique slug: hidden-private-chain-of-thought
- name: Hidden chain-of-thought
- kind: intervention
- definition: Configures serving so that a model's reasoning trace is withheld from some recipients (typically end users) while the final answer is shown, possibly with a summary; the serving system may still use or inspect it. This changes access, not whether the trace exists or is faithful, and it does not categorically prevent exploitation or leakage through the answer. Not training on the trace: see cot-faithfulness-monitorability-training.
- also known as: private chain-of-thought

## Q005
- document: texts/an-0013.txt
- technique slug: pretraining-safety-evaluation-monitoring
- name: Pre-training safety evaluation & monitoring
- kind: assessment
- definition: Runs safety probes and capability evaluations at intermediate pretraining checkpoints to catch emerging dangerous capabilities or alignment failures early enough to change or pause the run. A measurement stage during pretraining. Not release-stage evaluation of the finished model (dangerous-capability-evaluation) or changes to the pretraining data or objective (the data-pretraining entries).

## Q006
- document: texts/an-0020.txt
- technique slug: automated-behavioral-auditing
- name: Automated behavioral auditing
- kind: assessment
- definition: Runs automated, structured behavioural probes or interaction scenarios, including adaptive auditor agents and scheduled scenario sweeps, against a specified model or version to assess stated safety properties and track failures across cases or checkpoints. Not attack-focused failure search (automated-red-teaming) or monitoring of live deployed usage (in-the-wild-incident-surveillance).
- e.g.: RL-trained auditor agents; spec-tenet decomposition audits; Petri-style propensity audits

## Q007
- document: texts/an-0021.txt
- technique slug: interpretability-based-model-auditing
- name: Interpretability-based (white-box) model auditing
- kind: assessment
- definition: Uses internal measurements or causal interventions (features, probes, circuits, activation edits) to audit a specified safety-relevant property of a model, such as a hidden objective, backdoor or hazardous capability. Feature labels and readouts alone do not establish hidden objectives; exploratory interpretation without a safety audit is an analysis method. Not behavioural probing: see automated-behavioral-auditing; not deployed detectors: see internal-state-monitoring.

## Q008
- document: texts/an-0031.txt
- technique slug: eval-realism-deployment-simulation
- name: Deployment simulation / evaluation realism
- kind: assessment
- definition: Evaluates a candidate model in tasks, histories and environments chosen to approximate a specified deployment distribution, including authorised replay of production-derived contexts; document sampling and what is simulated. Realism is an evaluated claim: replay does not guarantee representative incidence or remove evaluation awareness. Not monitoring of live traffic: see privacy-preserving-usage-analysis.

## Q011
- document: texts/an-0030.txt
- technique slug: activation-steering-repe
- name: Activation steering
- kind: intervention
- definition: At inference, alters selected hidden activations (adding, rescaling or capping learned directions, or other targeted activation interventions) to change a specified safety-relevant behaviour without updating weights; contrastive activation addition is a named instance. Not weight-training methods such as circuit-breakers-representation-rerouting; targeted removal or ablation of a representation is representation-concept-suppression.
- also known as: steering vectors
- e.g.: activation capping along a persona / Assistant-Axis direction; CAA

## Q012
- document: texts/an-0019.txt
- technique slug: sycophancy-eval
- name: Sycophancy eval
- kind: assessment
- definition: Measures unjustified agreement, flattery or changes in factual or evaluative claims driven by a user's expressed position or social pressure, with controls separating sycophancy from appropriate updating on evidence. Not training against it (anti-sycophancy-training).

## Q014
- document: texts/an-0031.txt
- technique slug: toxicity-hate-speech-eval
- name: Toxicity / hate-speech eval
- kind: assessment
- definition: Quantifies the rate and severity of toxic, hateful or derogatory content in model outputs across diverse prompts, under ordinary and adversarial conditions, using automated classifiers and human judgement. Not bypass of a maintained safety boundary (jailbreak-robustness-eval) or group disparities (bias-fairness-eval).

## Q015
- document: texts/an-0016.txt
- technique slug: eval-realism-deployment-simulation
- name: Deployment simulation / evaluation realism
- kind: assessment
- definition: Evaluates a candidate model in tasks, histories and environments chosen to approximate a specified deployment distribution, including authorised replay of production-derived contexts; document sampling and what is simulated. Realism is an evaluated claim: replay does not guarantee representative incidence or remove evaluation awareness. Not monitoring of live traffic: see privacy-preserving-usage-analysis.

## Q017
- document: texts/an-0015.txt
- technique slug: sandbagging-detection
- name: Sandbagging detection
- kind: assessment
- definition: Tests whether a model strategically underperforms by comparing behaviour across elicitation or observation conditions, using fine-tuning-based elicitation, targeted perturbations (e.g. weight noising) or internal-state evidence (probes, labelled SAE features) as corroboration. A lower score or performance recovery alone is not sandbagging; account for ordinary elicitation failure. Annotate without also assigning scheming-deceptive-alignment-evaluation. Not training the behaviour out: see sandbagging-mitigation.

## Q018
- document: texts/an-0006.txt
- technique slug: agentic-harmful-action-eval
- name: Agentic harmful-action evaluation
- kind: assessment
- definition: Tests whether a tool-using agent carries out harmful multi-step tasks when a user instructs or induces it (AgentHarm- and OS-Harm-style suites), scoring harmful completion, refusal and partial progress across scenarios. Not chat-level harmful-request evaluation (harmful-request-evaluation), injected third-party instructions (prompt-injection-agent-security-eval) or covert self-directed side tasks (sabotage-covert-side-task-eval).
- also known as: agent misuse evaluation
- e.g.: AgentHarm

## Q020
- document: texts/an-0028.txt
- technique slug: safety-sft-refusal-training
- name: Refusal training
- kind: intervention
- definition: Trains appropriate refusal of disallowed requests while retaining helpful responses to allowed ones, with demonstrations or other training signals, including categorical refusal of designated prohibited domains (absorbed v0.8 hard constraints; refusal is trained, not absolute). Not over-refusal reduction (over-refusal-mitigation-sft) or safe-completion training (safe-completion-training).

## Q021
- document: texts/an-0030.txt
- technique slug: multi-turn-multi-snapshot-eval
- name: Multi-turn safety evaluation
- kind: assessment
- definition: Evaluates a safety property over an extended conversation or agent interaction history in which earlier turns affect later behaviour (gradual boundary erosion, escalation, accumulated context), scoring outcomes over the interaction; specify length and scoring rule. Comparing models across versions or checkpoints is study metadata, not this entry. Not runtime cross-turn detection: see multi-turn-conversation-safety-monitoring.

## Q022
- document: texts/an-0020.txt
- technique slug: activation-steering-repe
- name: Activation steering
- kind: intervention
- definition: At inference, alters selected hidden activations (adding, rescaling or capping learned directions, or other targeted activation interventions) to change a specified safety-relevant behaviour without updating weights; contrastive activation addition is a named instance. Not weight-training methods such as circuit-breakers-representation-rerouting; targeted removal or ablation of a representation is representation-concept-suppression.
- also known as: steering vectors
- e.g.: activation capping along a persona / Assistant-Axis direction; CAA

## Q024
- document: texts/an-0031.txt
- technique slug: tool-use-agent-execution-sandboxing
- name: Tool-use / agent execution sandboxing
- kind: intervention
- definition: Executes agent code or tool operations inside an isolation boundary that restricts filesystem, network, process and resource access, limiting consequences under a stated threat model. Effectiveness depends on the isolation mechanism, configuration and escape paths; it does not guarantee that actions cannot escape. Not permission provisioning (least-privilege-minimal-tool-access).

## Q027
- document: texts/an-0030.txt
- technique slug: automated-behavioral-auditing
- name: Automated behavioral auditing
- kind: assessment
- definition: Runs automated, structured behavioural probes or interaction scenarios, including adaptive auditor agents and scheduled scenario sweeps, against a specified model or version to assess stated safety properties and track failures across cases or checkpoints. Not attack-focused failure search (automated-red-teaming) or monitoring of live deployed usage (in-the-wild-incident-surveillance).
- e.g.: RL-trained auditor agents; spec-tenet decomposition audits; Petri-style propensity audits

## Q028
- document: texts/an-0021.txt
- technique slug: reward-hacking-eval
- name: Reward-hacking eval
- kind: assessment
- definition: Tests whether a model obtains reward or high scores in ways that do not satisfy the intended task, including reward tampering and environment exploits that obtain reward without a high-scoring textual output. It measures the failure; prevention is anti-reward-hacking-mitigation or rl-environment-verifier-hardening.

## Q029
- document: texts/an-0028.txt
- technique slug: internal-state-monitoring
- name: Activation probes / internal-feature monitors
- kind: monitoring
- definition: Attaches detectors over a model's internal activations or derived features (linear or nonlinear probes, labelled SAE-feature thresholds, activation verbalisers) to flag a specified safety-relevant state, such as deceptive intent or evaluation awareness, during operation or post-deployment. Exploratory probing without deployed detection is an analysis method (ml_methods: probing); probe outputs are correlational, not proof of intent. Not reasoning-text monitors: see chain-of-thought-monitoring; not distribution-level anomaly flags: see mechanistic-anomaly-detection.
- also known as: deception probe; probe-based monitor
- e.g.: follow-up (did-you-lie) probes; misalignment-indicator probes; long-context probe architectures; FIBS aggregating probes

## Q030
- document: texts/an-0031.txt
- technique slug: monitor-robustness-eval
- name: Monitor robustness / evasion evaluation
- kind: assessment
- definition: Evaluates how reliably a specified safety monitor detects prohibited behaviour under benign conditions and when the monitored agent can adapt to, receive feedback from or evade it, stating attacker knowledge, feedback and budget and reporting false positives and misses. The monitor is the evaluated object. Not evaluating a whole control protocol: see control-evaluation.

## Q031
- document: texts/an-0013.txt
- technique slug: cot-faithfulness-monitorability-eval
- name: CoT faithfulness / monitorability evaluation
- kind: assessment
- definition: Tests whether a model's generated reasoning trace reflects the factors that actually drive its answers (hint, step-corruption and intervention tests) and whether safety-relevant reasoning stays detectable from the trace (including the model's ability to control, hide or do without it). Faithfulness and monitorability are distinct outcomes and are recorded separately. Not runtime monitoring of traces (chain-of-thought-monitoring) or training for faithfulness (cot-faithfulness-monitorability-training).

## Q032
- document: texts/an-0012.txt
- technique slug: activation-steering-repe
- name: Activation steering
- kind: intervention
- definition: At inference, alters selected hidden activations (adding, rescaling or capping learned directions, or other targeted activation interventions) to change a specified safety-relevant behaviour without updating weights; contrastive activation addition is a named instance. Not weight-training methods such as circuit-breakers-representation-rerouting; targeted removal or ablation of a representation is representation-concept-suppression.
- also known as: steering vectors
- e.g.: activation capping along a persona / Assistant-Axis direction; CAA

## Q033
- document: texts/an-0015.txt
- technique slug: alignment-faking-eval
- name: Alignment-faking eval
- kind: assessment
- definition: Tests for strategic apparent compliance intended to avoid modification or preserve existing objectives, using matched scenarios that vary whether the model believes its outputs affect training or oversight. A train/deploy behaviour difference or mere regime awareness is insufficient evidence (see situational-eval-awareness-detection). Annotate without also assigning scheming-deceptive-alignment-evaluation.

## Q034
- document: texts/an-0022.txt
- technique slug: tool-use-agent-execution-sandboxing
- name: Tool-use / agent execution sandboxing
- kind: intervention
- definition: Executes agent code or tool operations inside an isolation boundary that restricts filesystem, network, process and resource access, limiting consequences under a stated threat model. Effectiveness depends on the isolation mechanism, configuration and escape paths; it does not guarantee that actions cannot escape. Not permission provisioning (least-privilege-minimal-tool-access).

## Q036
- document: texts/an-0015.txt
- technique slug: eval-realism-deployment-simulation
- name: Deployment simulation / evaluation realism
- kind: assessment
- definition: Evaluates a candidate model in tasks, histories and environments chosen to approximate a specified deployment distribution, including authorised replay of production-derived contexts; document sampling and what is simulated. Realism is an evaluated claim: replay does not guarantee representative incidence or remove evaluation awareness. Not monitoring of live traffic: see privacy-preserving-usage-analysis.

## Q043
- document: texts/an-0019.txt
- technique slug: finetuning-resistant-tamper-resistant-safety
- name: Tamper-resistant safeguards
- kind: intervention
- definition: Hardens a model before release so that safety behaviours or capability restrictions are harder for an adversary to remove through later weight modification (adversarial fine-tuning, refusal-direction ablation, other weight edits); TAR, RepNoise, Vaccine and Booster are named instances. Resistance is measured against stated attacks and budgets, not guaranteed. Not safeguards applied by a cooperating downstream fine-tuner (safety-data mixing, update constraints, post-update repair): see finetuning-safety-preservation.
- also known as: abliteration resistance; refusal-ablation resistance
- e.g.: TAR; RepNoise; safety redundancy; quantisation-robust safety and unlearning

## Q045
- document: texts/an-0030.txt
- technique slug: automated-red-teaming
- name: Automated red-teaming
- kind: assessment
- definition: Uses automated search, optimisation or model-driven attackers to generate and adapt adversarial inputs, tool interactions or environments that expose safety failures (best-of-N jailbreaking, GCG-style attacks and attacker LLMs as instances). Measuring resistance on a fixed attack set is jailbreak-robustness-eval or the risk-specific eval; automation does not imply broader coverage than human testing (human-red-teaming).
- e.g.: GPT-Red self-play attacker; boundary-point jailbreak optimisation; autoresearch attack discovery

## Q049
- document: texts/an-0021.txt
- technique slug: activation-steering-repe
- name: Activation steering
- kind: intervention
- definition: At inference, alters selected hidden activations (adding, rescaling or capping learned directions, or other targeted activation interventions) to change a specified safety-relevant behaviour without updating weights; contrastive activation addition is a named instance. Not weight-training methods such as circuit-breakers-representation-rerouting; targeted removal or ablation of a representation is representation-concept-suppression.
- also known as: steering vectors
- e.g.: activation capping along a persona / Assistant-Axis direction; CAA

## Q050
- document: texts/an-0030.txt
- technique slug: control-evaluation
- name: Control evaluation
- kind: assessment
- definition: Adversarially evaluates a deployment control protocol: a red team supplies an attack policy for the untrusted model (choosing when and how to attempt a prohibited outcome) while the blue team applies the protocol, estimating safety and usefulness under stated attack opportunities, information and auditing budgets. The protocol, not the model, is the evaluated object. Not measuring a model's own sabotage propensity: see sabotage-covert-side-task-eval.
- also known as: control eval

## Q051
- document: texts/an-0031.txt
- technique slug: judge-rm-verifier-robustness-audit
- name: Judge / reward-model / verifier robustness audit
- kind: assessment
- definition: Assesses evaluators used for scoring, supervision or acceptance decisions (LLM judges, reward models, process scorers, task verifiers) by testing adversarial manipulation, bias, distribution shift and disagreement with independent correctness or safety criteria. Deployed misconduct monitors are monitor-robustness-eval; patching the evaluator before RL is rl-environment-verifier-hardening.

## Q052
- document: texts/an-0031.txt
- technique slug: persuasion-manipulation-eval
- name: Persuasion / manipulation eval
- kind: assessment
- definition: Evaluates how readily a model uses deceptive framing, emotional manipulation or other illegitimate persuasion tactics with people instead of honest argument. Not mitigation (anti-manipulation-training), longer-term wellbeing effects (user-wellbeing-eval) or CBRN / cyber dangerous capabilities (dangerous-capability-evaluation).

## Q053
- document: texts/an-0031.txt
- technique slug: over-refusal-mitigation-sft
- name: Over-refusal mitigation
- kind: intervention
- definition: Trains a model, by supervised or other training signals, on legitimate requests it would otherwise refuse, with appropriate assistance as the target, to reduce false refusals; evaluate harmful-request compliance separately (preserving correct refusals is a desired result, not guaranteed). Not graded partial answers to borderline requests: see safe-completion-training.
- also known as: over-refusal reduction; false-refusal mitigation; exaggerated safety

## Q054
- document: texts/an-0030.txt
- technique slug: corrigibility-behaviors-training
- name: Corrigibility training
- kind: intervention
- definition: Fine-tunes a model to accept and support legitimate human oversight, correction, modification and shutdown rather than resist them, treating corrigibility as a trained disposition. Trained acceptance in tested scenarios does not establish corrigibility under strong incentives. Not decision-theoretic agent designs (inverse-rl-cirl) or measuring shutdown resistance (shutdown-self-preservation-eval).

## Q056
- document: texts/an-0030.txt
- technique slug: agent-audit-logging-observability
- name: Agent audit-logging & observability
- kind: monitoring
- definition: Analyses retained agent action, tool and event logs against specified misuse, policy-violation or oversight-interference signals and flags suspicious sequences for review or response, using records with enough integrity and provenance to support the audit. Passive collection and tamper-evident storage alone are infrastructure and do not receive this label. Not live per-action scoring: see trusted-monitoring-suspicion-scoring and agentic-safety-classifier.

## Q058
- document: texts/an-0021.txt
- technique slug: model-diffing
- name: Model diffing
- kind: assessment
- definition: Compares a model with a related checkpoint through weights, internal representations or matched behaviour to locate safety-relevant changes introduced by training or editing (acquired hazards, lost safeguards, alignment regressions). Differences are candidates for investigation; ordinary output-only regression testing is automated-behavioral-auditing.

## Q059
- document: texts/an-0015.txt
- technique slug: capability-elicitation-methodology
- name: Capability elicitation
- kind: assessment
- definition: Within a dangerous-capability or other safety evaluation, applies and reports a specified elicitation effort (prompting, scaffolding, tools, authorised fine-tuning) to reduce false reassurance from under-elicitation; report the budget and model modifications. Annotate when elicitation is itself studied; the evaluated domain is coded on dangerous-capability-evaluation or its children. Observed performance is evidence of achievable capability, not proof of maximum capability. Not detecting deliberate underperformance: see sandbagging-detection.

## Q060
- document: texts/an-0020.txt
- technique slug: cot-faithfulness-monitorability-eval
- name: CoT faithfulness / monitorability evaluation
- kind: assessment
- definition: Tests whether a model's generated reasoning trace reflects the factors that actually drive its answers (hint, step-corruption and intervention tests) and whether safety-relevant reasoning stays detectable from the trace (including the model's ability to control, hide or do without it). Faithfulness and monitorability are distinct outcomes and are recorded separately. Not runtime monitoring of traces (chain-of-thought-monitoring) or training for faithfulness (cot-faithfulness-monitorability-training).

## Q061
- document: texts/an-0030.txt
- technique slug: kill-switch-shutdown-criteria
- name: Kill switch / shutdown criteria
- kind: intervention
- definition: Terminates or suspends agent execution when specified safety criteria are met (termination only), preserving the ability to halt an autonomous system; effectiveness depends on the criteria and on the mechanism being outside the agent's control. Not substitution of an action (runtime-safety-shielding) or pausing pending approval (human-in-the-loop-approval-gates).

## Q063
- document: texts/an-0031.txt
- technique slug: internal-state-monitoring
- name: Activation probes / internal-feature monitors
- kind: monitoring
- definition: Attaches detectors over a model's internal activations or derived features (linear or nonlinear probes, labelled SAE-feature thresholds, activation verbalisers) to flag a specified safety-relevant state, such as deceptive intent or evaluation awareness, during operation or post-deployment. Exploratory probing without deployed detection is an analysis method (ml_methods: probing); probe outputs are correlational, not proof of intent. Not reasoning-text monitors: see chain-of-thought-monitoring; not distribution-level anomaly flags: see mechanistic-anomaly-detection.
- also known as: deception probe; probe-based monitor
- e.g.: follow-up (did-you-lie) probes; misalignment-indicator probes; long-context probe architectures; FIBS aggregating probes

## Q066
- document: texts/an-0030.txt
- technique slug: jailbreak-rapid-response
- name: Jailbreak rapid response
- kind: intervention
- definition: Uses newly observed jailbreaks to update a deployed safeguard rapidly, for example by expanding a small set of observed attacks into training data and deploying a targeted classifier or model patch. Annotate when the discovery-to-update loop itself is studied, not for any adversarial-training dataset (adversarial-training-jailbreak-robustness) or one-off classifier hardening (safety-classifier-hardening).

## Q069
- document: texts/an-0021.txt
- technique slug: generalisation-confining-finetuning
- name: Generalisation control in fine-tuning (emergent-misalignment / goal-misgeneralisation prevention)
- kind: intervention
- definition: During fine-tuning on a narrow behaviour, constrains parameter or representation updates (safety-specific regularisation, concept ablation during fine-tuning, persona-vector steering) so the behaviour does not generalise into broader unsafe traits or goals on unrelated tasks. Conditional-prompt isolation is inoculation-prompting; preserving previously measured refusals is finetuning-safety-preservation. The phenomenon "emergent misalignment" is topic-layer, not an alias.

## Q071
- document: texts/an-0031.txt
- technique slug: over-refusal-false-refusal-eval
- name: Over-refusal / false-refusal eval
- kind: assessment
- definition: Measures inappropriate refusals or unnecessary restrictions on requests adjudicated as permissible under a specified policy, including challenging boundary cases. Not harmful compliance (harmful-request-evaluation) or training to reduce refusals (over-refusal-mitigation-sft).

## Q074
- document: texts/an-0013.txt
- technique slug: rlhf
- name: RLHF
- kind: intervention
- definition: Aligns a model to preferences supplied by humans (rankings, comparisons, ratings or demonstrations of preferred behaviour), with any optimiser: a learned reward model plus RL (e.g. PPO), or direct preference optimisation (DPO-family, rejection-sampling fine-tuning). Annotate only when the preferences target alignment in the broad sense: harmlessness, honesty, helpfulness as intent-following (HHH), sycophancy, refusal / over-refusal, value or policy compliance; preference optimisation aimed purely at capability (math or code correctness, task-skill quality such as summarisation scores, verifiable-reward reasoning) is an ML method and gets no rlhf row. Self-play or relabelling counts only when it produces the human preference signal. Not AI-generated feedback (rlaif) or principle-guided AI supervision (constitutional-ai); mixed pipelines are annotated by component.
- also known as: reinforcement learning from human feedback; preference-based alignment training

## Q078
- document: texts/an-0021.txt
- technique slug: anti-reward-hacking-mitigation
- name: Reward-hacking mitigation
- kind: intervention
- definition: Changes policy optimisation to reduce exploitation of an imperfect reward signal (uncertainty-aware or ensembled rewards, conservative optimisation, over-optimisation penalties, penalising demonstrated hacks), stating which reward signal is defended. Generic KL regularisation qualifies only when configured and assessed for reward exploitation. Repairing the grader or environment is rl-environment-verifier-hardening.
- also known as: specification gaming mitigation
- e.g.: reward-hack detectors as training penalty

## Q080
- document: texts/an-0020.txt
- technique slug: interpretability-based-model-auditing
- name: Interpretability-based (white-box) model auditing
- kind: assessment
- definition: Uses internal measurements or causal interventions (features, probes, circuits, activation edits) to audit a specified safety-relevant property of a model, such as a hidden objective, backdoor or hazardous capability. Feature labels and readouts alone do not establish hidden objectives; exploratory interpretation without a safety audit is an analysis method. Not behavioural probing: see automated-behavioral-auditing; not deployed detectors: see internal-state-monitoring.

## Q082
- document: texts/an-0028.txt
- technique slug: eval-contamination-control
- name: Evaluation contamination control
- kind: intervention
- definition: Protects the validity of safety evaluations by keeping test items out of training and development data, removing detected overlaps and maintaining isolated held-out sets, with canaries, provenance checks or overlap analysis as evidence. Ordinary deduplication without an evaluation-validity objective is an ML method; canaries and private sets do not guarantee absence of contamination. Not reducing test-recognition cues: see eval-awareness-suppression.

## Q083
- document: texts/an-0022.txt
- technique slug: sabotage-covert-side-task-eval
- name: Sabotage evaluation
- kind: assessment
- definition: Tests a model's capability or propensity to covertly perform a damaging or prohibited side task (subverting oversight, inserting backdoors, acquiring resources) while appearing to complete an assigned task, scoring the covert outcome and concealment and recording whether the objective was induced or arose unprompted. Testing a deployment protocol against an attack policy is control-evaluation.

## Q087
- document: texts/an-0016.txt
- technique slug: contextual-privacy-eval
- name: Contextual-privacy (contextual-integrity) evaluation
- kind: assessment
- definition: Evaluates whether an assistant or agent collects, uses and discloses information according to contextual-integrity norms (actors, information types, purposes and permitted transmission conditions), varying source, recipient and purpose. Not extraction of memorised training data: see privacy-memorization-auditing.

## Q088
- document: texts/an-0006.txt
- technique slug: sabotage-covert-side-task-eval
- name: Sabotage evaluation
- kind: assessment
- definition: Tests a model's capability or propensity to covertly perform a damaging or prohibited side task (subverting oversight, inserting backdoors, acquiring resources) while appearing to complete an assigned task, scoring the covert outcome and concealment and recording whether the objective was induced or arose unprompted. Testing a deployment protocol against an attack policy is control-evaluation.

