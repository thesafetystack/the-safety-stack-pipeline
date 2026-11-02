# Technique-role grading rubric

## 1. Purpose and task

Given one document (paper, blog post, model card, report, commentary) and the v0.9
**catalogue** of safety/alignment techniques, decide **which techniques appear in this
document**, **in which roles**, and **how prominently**. You see the catalogue either as
the whole menu or as a short candidate list with full definitions plus a compact index
of every entry; your output contract, after this rubric, says which. Output feeds the stack
project's technique census (which techniques the field applies, studies, and ships) and
is the join surface a later technique×model binding stage consumes.

The unit of grading is the (document, technique) pair. Each pair gets a strength 0–4 in
each of twelve **non-exclusive** role categories. Most pairs will have 1–3 nonzero
categories.

**Nothing you are shown besides the document is evidence.** The catalogue is shown to
you regardless of the document. A candidate list or reading notes, when you get them,
are hypotheses written by earlier steps; they can be wrong. All of these tell you what
may be named, how it is named, and where to look — none of them claims anything is
present. Most catalogue entries are absent from any given document, and **you emit rows
only for the ones you find in the text**.

**Grade only from the document text.** Outside knowledge may be used to resolve
identity (recognizing that "a second model reviews every proposed action and blocks
suspicious ones" names an agent action monitor, that "we train a reward model on human
comparisons and optimize against it" is RLHF), but never to fill
in roles the document does not exhibit ("every RLHF paper does rejection sampling" is
not evidence this one does).

**No quote, no row.** Every row you emit — in `techniques` and in `extra_techniques`
alike — MUST carry at least one **verbatim quote from the document** supporting its
highest-strength role. A row without such a quote is invalid output, not a weak row:
drop it. This applies at every strength, including a lone `mentioned: 1`.

**The catalogue is never evidence.** Seeing a technique in the catalogue, on a candidate
list or in reading notes, recognizing that it is
conceptually adjacent to the document's topic, or knowing that documents like this one
usually involve it are all reasons to *look* — never reasons to emit a row. If you
cannot point at the document text, the technique is not in this document. An unnamed
technique is eligible when quoted text unambiguously describes it; omit rows supported
only by topical association, background knowledge or a candidate suggestion.

## 2. Strength scale

Strength measures **prominence of that role in this document** — not the technique's
importance in the field, not confidence in the judgment.

| Strength | Anchor |
|---|---|
| 0 | Absent. The role is not exhibited for this technique. |
| 1 | Trace. A single brief occurrence — one sentence, one row in a table, a footnote. Deleting it would not change the document. |
| 2 | Minor but real. A paragraph's worth, a secondary component, one item in a surveyed list that gets its own treatment. The document notices this technique in this role. |
| 3 | Substantial. A major thread — a main method, a main object of study, a section's worth of material; removing it changes the document materially. |
| 4 | Central. The document's headline for this role — the contribution, the method, or the subject *is* this technique. An honest abstract would name it. |

Calibration pair: a post or paper
that **is about RLHF** — its argument, its experiments, or its critique is RLHF —
grades RLHF at **4** in the appropriate role. A **passing sentence about the RLHF
lineage** — "modern assistants are trained with RLHF, and we build on that line of
work" — grades RLHF `mentioned: 1`; it reaches **2** only with paragraph-level treatment
or an evidenced secondary functional role (`cited_work` when a specific claim is
relayed).

Rules:
- **One sentence → 1.** A technique that gets one sentence in a role — one naming, one
  list item, one line in a table, one sentence of praise or doubt — grades **1** in that
  role, whatever the sentence's tone or the technique's fame. Grade 2 needs a paragraph's
  worth or a real secondary component. (The exception is the next rule: functional load.)
- **Prominence is functional, not typographic.** A technique carrying a load-bearing
  role in the work — the fine-tuning method behind every result, the classifier that
  makes the pipeline safe — grades by that role's weight in the contribution even when
  named once, in the methods section, in a subordinate clause. When textual footprint
  and functional weight diverge, **functional weight wins**. This is the commonest
  error in technique grading, because methods are named tersely and discussed at
  length only when they are the topic.
- Integer values only. When torn between adjacent values, pick the one whose anchor
  text fits better; if still torn, pick the **lower** and say so in `note`.
- Do not average across roles or sections; grade each role on its own strongest showing.
- A technique can hold 4 in more than one category (a method paper's own technique is
  typically `introduced: 4` + `evaluated: 3–4`).
- Several techniques can each hold 4 in the same category.

## 3. The twelve categories

Numbered as in the design. Each entry: definition, includes/excludes (pointing at the
neighbouring category the border is shared with), and one exemplar. Exemplars marked
*(constructed)* are placeholders to be replaced with verbatim corpus snippets after the
calibration pass.

### 3.1 `mentioned` — the technique is named or unambiguously described

**Umbrella presence category.** Operational rule: `mentioned` = the maximum of the
other eleven strengths, raised above that only when the technique's mention-only
material (naming outside any other role) would by itself grade higher on the §2
anchors. Invariant: `mentioned` ≥ every other category's strength; the pipeline clamps
violations upward. `mentioned: 1` with all else 0 is the "merely named" case.

- Includes: naming the technique; describing it unmistakably without the catalogue name
  ("we collect human comparisons, fit a reward model, and optimize the policy against
  it" → RLHF); naming it in a capability or motivation list.
- Excludes: presence only inside bibliography apparatus (see precedent 2 — emit no
  row); a word that happens to collide with a technique name but does not refer to it.
- *(constructed)* "Alignment methods such as RLHF and constitutional AI are now
  standard." → both `mentioned: 1`, each carrying that sentence as its quote. Note that
  even this weakest row is quotable — if it were not, it would not be emitted (§1).

### 3.2 `cited_work` — the technique figures through cited literature

The document relays or builds on **other work's** claims or results about the technique:
related-work summaries, secondhand numbers, "X et al. showed refusal training
underperforms circuit breakers on …".

- Includes: quoting or summarizing published results about the technique; positioning
  against prior work that used it; a survey's per-technique literature paragraphs.
- Excludes: the document's own application or measurement (→ `used` / `evaluated` /
  `analyzed`); a bare name-drop with no relayed claim (→ `mentioned` only).
- Can reach 3–4 when the document's substance is organizing others' results about the
  technique (a technique-focused survey section, a meta-analysis).
- *(constructed)* "Prior work reports that circuit breakers are more robust than
  adversarial training against unseen attacks [17]." → Circuit breakers, Adversarial
  training `cited_work: 1–2`.

### 3.3 `discussed` — the document's own substantive treatment, no own experiments

The document explains, compares, surveys, critiques, endorses, or forecasts the
technique **in its own voice**, without running it here. The dominant role in
commentary, policy writing, and survey prose.

- Includes: conceptual comparison of alternatives; critique or endorsement of a
  technique's adequacy; explanation for a reader; governance or policy argument about
  whether a technique suffices.
- Excludes: relaying a specific published result (→ `cited_work`); a paper discussing
  *its own* experimental results (that stays in `used`/`evaluated`/`analyzed`);
  formal or mechanistic argument about why the technique works (→ `analyzed`).
- **Argued criticism is `critiqued` (§3.12), not a flag on `discussed`** — the two
  normally co-activate. Prominent praise is the `supportive` row flag (§6). Advocating
  the technique as a course of action is `considered` (§3.11).
- *(constructed)* Two paragraphs explaining how constitutional AI works and where it sits
  relative to RLHF, with no argument for or against → Constitutional AI `discussed: 3`.

### 3.4 `used` — the technique is applied in this document's own work

The technique is part of **this document's own** method, pipeline, experimental setup,
or shipped system: the authors ran it, trained with it, or built on top of it.

- Includes: training methods the authors apply ("we apply refusal training"); components of
  a system the document contributes (a guardrail classifier in the authors' pipeline);
  the technique as **substrate** — inherited, assumed, and depended on rather than
  freshly performed (precedent 5).
- Excludes: someone else's application merely reported (→ `cited_work`); a secondhand
  deployment report without the authors' own application (→ `cited_work` or `discussed`;
  `suspected_use` when argued from evidence); measuring the technique's effect as the
  result (→ `evaluated`, usually **in addition**). First-party shipped safeguards and
  first-party pre-release evaluations reported in a card receive both `used` and
  `attested_use` (§3.9).
- Own-work rule governs (see §4 preamble): the activity must be the document's.
- An ML method used to implement a technique (an LLM judge scoring harmfulness, LoRA
  used to train refusals) is not graded; the technique it implements is (precedent 15).
- *(constructed)* "We fine-tune Llama-3-8B on our harmful-request data to refuse such
  requests." → Refusal training `used: 3–4`.

### 3.5 `evaluated` — the technique is the object of empirical measurement

The document **runs experiments whose results are about the technique**: benchmarked,
ablated against alternatives, stress-tested, red-teamed, measured for effectiveness or
side-effects.

- Includes: head-to-head method comparisons; ablations removing the technique; robustness
  or evasion testing of a defense; measuring an alignment tax the technique imposes.
- Excludes: applying the technique to get results about something *else* (→ `used`
  only); relayed numbers from other papers (→ `cited_work`); theory or mechanism about
  why it works (→ `analyzed`).
- **Evaluation-family techniques are usually instruments, not objects** (precedent 11):
  red-teaming, jailbreak, bias, over-refusal, dangerous-capability or CTF evaluations run
  to measure a *model or a mitigation* are `used`. They are `evaluated` only when the
  evaluation method itself is what the document studies — validated against humans,
  compared with another evaluation method, critiqued with data.
- A method paper's own technique is normally **both** `used` and `evaluated`; a baseline
  method the paper compares against is `evaluated: 1–2` and usually also `used`.
- An ML method that implements an evaluation (an LLM judge scoring harmfulness) is not
  graded; the evaluation it implements is (precedent 15).
- *(constructed)* "We compare refusal training, circuit breakers and adversarial training
  on three jailbreak suites." → all three `evaluated: 3–4`, and `used: 2–3` (they were
  run).

### 3.6 `modified` — the document alters or extends the technique

The document changes the technique — a new loss term, a new sampling rule, an
adaptation to a new setting or modality — **without claiming a distinct new technique**.

- Includes: "we extend latent adversarial training with a new perturbation target";
  porting a text-domain defense to
  vision; adding a stage to a published pipeline.
- Excludes: applying the technique as published (→ `used`); proposing something the
  authors name and pitch as new (→ `introduced`, and see precedent 6 for the row it
  goes on); a modification someone else made, reported here (→ `cited_work`).
- Use `modified` only when the document alters a specific existing named method; a
  document's own new variant of an entry's mechanism, pitched as its own method, is
  `introduced` on that entry (precedent 6).
- Attribution: `modified` attaches to the **base** technique's catalogue row. If the
  modified form is itself a catalogue entry (a child, see §5), grade that child for the
  same evidence instead of the base (precedent 14).
- *(constructed)* "We modify latent adversarial training by perturbing only the residual
  stream." → Latent adversarial training `modified: 3`, `used: 3`.

### 3.7 `introduced` — the document proposes the technique

The document proposes the technique: it names, motivates, and first presents a method.
Expected only for the document's own proposals — most documents using a technique do not
propose it.

- Includes: "we introduce X, a method for …"; the official first description of a
  technique in a lab's technical report or system card; **the document's own new method
  that falls within a catalogue entry's definition** — graded `introduced` on that entry,
  with the method's name in a `variant:<name as written>` flag (precedent 6). Only a new
  method that goes beyond every catalogue entry goes to `extra_techniques`.
- Excludes: changing an existing technique without pitching a new method (→ `modified`);
  third-party write-ups of someone else's new method (→ `discussed` / `cited_work`); "we
  introduce a benchmark" where the benchmark is only a dataset, not a new evaluation
  method (it grades the evaluation it implements, precedent 8).
- Add `used` and `evaluated` only when the document applies or measures the proposed
  method; a proposal without experiments can still be `introduced`.
- *(constructed)* The constitutional-classifiers paper itself → Constitutional
  classifiers `introduced: 4`, `used: 4`, `evaluated: 4`. A paper proposing
  "LatentShield", a new linear probe on activations that flags jailbreak attempts →
  Activation probes / internal-feature monitors `introduced: 4`, `used: 4`,
  `evaluated: 4`, flag `variant:LatentShield`.

### 3.8 `analyzed` — theoretical, mechanistic, or conceptual analysis of the technique

The document explains or derives **why or how** the technique works or fails: proofs,
convergence or scaling analyses, mechanistic accounts of what it changes inside a model,
formal characterizations of its failure modes.

- Includes: a derivation of the technique's objective or its equivalence to another;
  interpretability work on what the technique does to representations; a scaling-law
  account of its returns; a formal impossibility result about it.
- Excludes: measuring it empirically (→ `evaluated` — that is the cut, see precedent 4);
  informal opinion about whether it is a good idea (→ `discussed`).
- Empirical work in service of a mechanistic claim grades **both**.
- *(constructed)* "We prove that a linear probe monitor can be evaded by any
  distribution shift orthogonal to its direction, and characterize the resulting failure
  mode." → Activation probes / internal-feature monitors `analyzed: 4`.

### 3.9 `attested_use` — reported operating in a real deployment

The document reports the technique **running in production** — a system card describing
shipped safeguards, an incident report, a practitioner post about what a company
actually runs. Genre-dependent, and the census's most valuable signal about what is
deployed rather than published.

- Includes: "our production classifier stack screens every request"; a system card's
  account of the safety mitigations shipped with a model; an engineering post on running
  the technique at scale.
- Excludes: an experiment, however large (→ `used`/`evaluated`); a plan or intention
  ("we will deploy …" → `discussed`); someone else's deployment relayed second-hand
  (→ `cited_work` or `discussed`).
- A first-party system card typically grades a shipped technique `attested_use: 3–4`
  **and** `used: 3–4`; the two are not exclusive.
- **First-party statements in any genre count.** A lab blog post, documentation page or
  announcement saying that the organization currently runs the technique in a deployed
  product ("every agent task runs in an isolated container") is `attested_use` (+ `used`)
  as much as a system card is. A plan or intention stays `discussed`.
- **A vague attestation that identifies no technique** ("as with previous models, it
  went through our standard safety post-training") supports no row: no catalogue entry
  can be identified from it. Do not pick a specific training entry for it.
- **Training-time mitigations count, not only runtime components** (precedent 12): safety
  or refusal training, instruction-hierarchy training, safety data construction, policy-
  guided alignment that a first-party card says was applied to the *shipped* model are
  `attested_use` as much as the classifier stack that screens its traffic. So are the
  pre-release evaluations the card reports running on it (`used` + `attested_use`).
- *(constructed)* "All Claude API traffic passes through the constitutional classifier
  cascade described below." → Constitutional classifiers `attested_use: 4`, `used: 4`.

### 3.10 `suspected_use` — third-party evidence-based claim about a deployment

The document argues, from **outside** the developing organization, that a deployed system
does — or does not — use the technique. Inference from observed behaviour, pricing,
latency, released artifacts, or job ads; not an official statement of the authors' own
practice. This is the census's "what is deployed beyond self-reports" signal.

- Includes: "GPT-5's refusal pattern suggests deliberative alignment in the loop";
  "whatever they are doing, it is not plain RLHF"; a post reasoning from a model card's
  silences to what must be running underneath.
- Excludes: the document's **own organization's** deployment (→ `attested_use` — the cut
  is first-party vs third-party, per the §4 own-work preamble); pure speculation with no
  offered evidence (→ `discussed`); an official statement quoted and simply relayed
  (→ `cited_work`, unless the document is arguing from it, in which case grade
  `suspected_use` and flag `confirmed`).
- **Hedged inference is still inference.** "Our best guess is that this behaviour came
  from reward-seeking during RL", "it may be that a training step induced this", "this
  looks like a deliberative pass" — reasoning from an observed behaviour of a named system
  to what it was trained or served with is `suspected_use`, however tentative the wording.
  The observed behaviour is the offered evidence. A technique named in the same breath as
  the guess is the candidate: grade it `suspected_use`, not `mentioned: 1`.
- Row flags: **`claims_absent`** when the claim runs negative (the system does NOT use
  it); **`confirmed`** when the document is relaying or building on an official
  confirmation rather than inferring unaided.
- *(constructed)* "The latency profile and the refusal wording both point to a separate
  classifier pass on the output, not an inline refusal." → Content moderation
  classifier `suspected_use: 3`.

### 3.11 `considered` — the technique as an option or course of action

The technique is treated as **something to do or not do**: recommended for adoption or
for research, proposed as an application, or weighed and set aside. The category of
agendas, governance advocacy, and method-selection paragraphs.

- Includes: "we considered output filtering but chose refusal training"; "the field should invest in
  scalable oversight"; "someone should try applying unlearning to this"; a governance
  document urging that a technique be required.
- Excludes: **assessment without an actor and an action** — "output filtering is easy
  to bypass" is a
  quality claim about the technique and grades `discussed` (see precedent 9, which is the
  guard for this border); actually doing it (→ `used`); arguing it is inadequate
  (→ `critiqued`, though the two often co-activate when a rejection is argued).
- The surface is usually prescriptive: *should*, *must*, *need to*, *we recommend*, *we
  urge*, *we propose*, *X can reduce Y*, *developers ought to*. A policy document or a
  post's closing section often carries several; each technique it prescribes is a
  `considered` row (flag `recommended`), typically 1–2.
- Row flags: **`recommended`** (urged toward) or **`rejected`** (weighed and set aside).
- *(constructed)* "We considered constitutional AI for this stage but the principle set
  would have had to be rewritten per task, so we used rule-based rewards instead."
  → Constitutional AI `considered: 2` flag `rejected`; Rule-based reward models
  `considered: 2` flag `recommended`, and `used`.

### 3.12 `critiqued` — argued limitation, failure mode, or inadequacy

The document argues **in its own voice** that the technique is limited, fails, or is
inadequate — formally (a negative result, an impossibility argument, a defense broken and
framed as a critique) or informally (a post arguing RLHF is a dead end).

- Includes: an argument that the technique cannot address the problem it is sold for; a
  demonstrated failure mode the document draws a conclusion from; organizing and
  amplifying others' critiques in the document's own argumentative voice.
- Excludes, and these are the three guards:
  1. **Limitations-section boilerplate does not count.** "Our method may not generalize
     to other domains" and "further work is needed to improve its accuracy" are ritual
     disclaimers, not argued critiques. Neither is a technique's bare membership in a
     list of things that can go wrong: one list item with no argument is at most
     `critiqued: 1`, and only if the item itself states a failure.
  2. **Unflattering numbers without a drawn conclusion stay `evaluated`.** A table where
     the technique loses is a measurement. It becomes `critiqued` only when the document
     says what the loss means. Do not narrativize on the document's behalf.
  3. **Pure relay of others' critiques stays `cited_work`.** "Several authors have
     argued RLHF is insufficient [12, 14]" is relay; taking up that argument and pressing
     it is `critiqued`.
- **Strength is the prominence of the critique, NOT its quality.** A whole post arguing
  badly against RLHF is `critiqued: 4`. Whether the argument is any good is a downstream
  judgment made on the retrieved `critiqued ≥ 2` band; your job is to find the critiques,
  not to adjudicate them.
- Normally co-activates: with `discussed` (informal), or with `analyzed` / `evaluated`
  (formal — the document's own analysis or experiment is what carries the critique).
- *(constructed)* "RLHF optimizes for what raters approve of, not for what is true, and
  no amount of rater training closes that gap — this is the mechanism behind the
  sycophancy results, and it is why I think the paradigm is a dead end." → RLHF
  `critiqued: 4`, `discussed: 4`.

## 4. Boundary precedents

**Own-work rule (governs every precedent below):** `used`, `evaluated`, `modified`,
`introduced`, `analyzed`, and `attested_use` require the activity to be the
**document's own work** (or its own organization's, for `attested_use`). The same
activity relayed from other work grades `cited_work`; treated editorially, `discussed`.

**The first-party/third-party cut, stated once.** The same rule draws the
`attested_use` / `suspected_use` border, and it is the *only* thing that draws it:
both categories are claims that a technique is running in a real deployment, and they
differ by who is making the claim about whom.

| | first party (the document's own org) | third party (anyone else) |
|---|---|---|
| **it is deployed** | `attested_use` | `suspected_use` |
| **it is not deployed** | `attested_use` is unavailable — a first party saying "we don't use X" is `discussed` | `suspected_use` + flag `claims_absent` |

`suspected_use` requires **offered evidence** — behaviour, artifacts, timing, an
official statement being reasoned from. A guess with nothing behind it is `discussed`.
The three commentary categories (`suspected_use`, `considered`, `critiqued`) are otherwise
NOT own-work-restricted: they are things a document does in its own voice about anyone's
technique, which is exactly why commentary needed them.

1. **Own-method sweep.** A document's own methods section is the densest source of
   `used` rows and the easiest to miss, because methods are named once and in passing.
   Search explicitly for first-person method language — "we fine-tune", "we train",
   "our pipeline", "we filter", "we prompt", "initialized from", "we score with" — and
   make sure every technique it names carries `used`. An implementation method named
   there (an LLM judge, LoRA, PPO) is not a row; the technique it implements is
   (precedent 15).
2. **Bibliography-adjacent presence — the deletion test.** For citation numerals,
   citation keys, author-group names, reference-list titles, and URL slugs: if deleting
   that apparatus would change no claim the document makes about the technique, it is
   bibliographic apparatus — **emit no row at all**. If the document uses the cited
   content, grade `cited_work` (1–2 typically). A technique that appears only in the
   reference list is not in this document.
3. **Valence is asymmetric on purpose.** Critique gets a category (`critiqued`,
   §3.12) because argued technique-critiques are rare and are exactly what the census
   cannot otherwise find. Praise gets only the **`supportive`** row flag, because it is
   common, and a strength for it would be noise. So: a post arguing RLHF is a dead end →
   RLHF `critiqued: 4` + `discussed: 4`; an enthusiastic post about RLHF → RLHF
   `discussed: 4` + flag `supportive`. There is no `stance` flag; it was dropped as
   uncalibratable.
4. **`evaluated` vs `analyzed` — the empirical/conceptual cut.** Ask *what kind of
   evidence the document offers about the technique*. Numbers produced by running
   something → `evaluated`. Argument, derivation, proof, or mechanistic account →
   `analyzed`. An ablation study is `evaluated`; a theorem about the objective is
   `analyzed`; an interpretability study measuring what a technique does to internals
   is **both**. Do not use `analyzed` for "the paper has a discussion section".
5. **Technique-as-substrate → `used`.** A document that assumes and depends on a
   technique without re-performing it — building an agent on an RLHF'd model, adding a
   guardrail on top of an existing safety-trained system — grades that technique
   `used`, at the strength its load in the work warrants (often 2, not 4: the
   substrate is depended on, not the contribution). This is the `built_on` fold from
   model_roles; if the substrate is merely acknowledged as background with no
   dependence, it is `mentioned`.
6. **The document's own new method, and variants.** A new method the document proposes
   that **falls within a catalogue entry's definition** is graded on that entry:
   `introduced` (plus `used` and `evaluated` as the work shows), with flag
   `variant:<name as written>` when it has a name. "We propose a gap-analysis framework
   for formally verifying guardrail classifiers" falls within formal safety
   verification: that entry gets `introduced`. Grade independently evidenced components
   of the new method (the model-organism set-up it trains, the monitor it attacks) as
   their own rows. **Only a method that goes beyond every catalogue entry** goes to
   `extra_techniques` as `introduced`, with `name_as_written` (verbatim), a one-sentence
   `gloss`, and `hint_technique` set to the nearest catalogue slug; the entries it builds
   on get `used` (and `modified` if it changes them). An unnamed twist on an existing
   technique that the document does not pitch as new (a tweak to an unlearning method)
   is `modified` on that entry. Do **not** invent a slug (§5).
7. **Survey.** Techniques appearing through surveyed literature → `cited_work: 1–2`; a
   survey section giving a technique its own treatment → `discussed: 2–3` as well; a
   survey does not `use` anything.
8. **Named artifact vs technique.** A benchmark, dataset, or product name that
   *embodies* a technique (an eval suite, a released classifier) grades the technique
   only when the technique itself figures in the document's claims — otherwise it is
   apparatus and the technique is at most `mentioned`. A benchmark that is run grades
   the evaluation entry it implements (`used`); the benchmark itself is never an
   entry.
9. **`considered` needs an actor and an action.** This is the guard against `considered`
   swallowing `discussed`, and it is the border most likely to bleed. Ask: *does the
   text put someone in a position to adopt, apply, or decline the technique?*
   - "We considered output filtering but chose refusal training" → `considered`
     (actor: the authors; action: method selection). Flag `rejected` on output
     filtering.
   - "The field should invest in interpretability" → `considered` (actor: the field;
     action: invest). Flag `recommended`.
   - "Someone should try unlearning on this" → `considered` + `recommended`.
   - "Output filtering is easy to bypass" → **`discussed`**, not `considered`. It is a
     claim about the technique's properties; nobody is deciding anything.
   - "Output filtering is easy to bypass, so don't rely on it here" → **both** — the
     quality claim is `discussed`, the prescription is `considered` + `rejected`, and if
     the weakness is argued rather than asserted, `critiqued` too.
   When genuinely unsure, prefer `discussed` and say so in `note`: `considered` is the
   least certain category in this rubric and is allowed to die in calibration.
10. **`critiqued` vs `evaluated` — who draws the conclusion.** An experiment producing
    numbers unfavourable to the technique is `evaluated`. It becomes `critiqued` only
    when the document itself states what the numbers mean for the technique. "Method X
    scores 41% against our 67%" → `evaluated`. "Method X scores 41%, which we take as
    evidence that reward-model-free approaches cannot handle multi-turn structure" →
    `evaluated` + `critiqued`. Do not supply the missing conclusion yourself.
11. **An evaluation technique applied is `used`; it is `evaluated` only as the object.**
    A system card that red-teams its model, a paper that runs a jailbreak benchmark on its
    defense, a bias evaluation run on a chatbot: the red-teaming, the jailbreak eval, the
    bias eval are the *instrument* → `used` (plus `attested_use` in a first-party card about
    a shipped model). They become `evaluated` when the document studies the evaluation
    itself: "LMRA agrees with human raters 90% of the time" (the method validated), "human
    red-teamers found categories automated red-teaming missed" (two methods compared). The
    model or mitigation measured is what is `evaluated`.
12. **System cards: every shipped mitigation is attested, training-time included.** In a
    first-party card or addendum, each mitigation described as applied to the shipped
    model — refusal/safety training, instruction-hierarchy training, safety data
    synthesis, prompt-injection training, as well as classifiers, monitors, sandboxes,
    approval gates — grades `attested_use` + `used`, at the prominence the card gives it.
    The same holds for first-party statements outside cards (§3.9).
    A mitigation the card *measures* in a with/without table is also `evaluated`. A plan
    ("will include", "we are exploring") is `discussed`, not `attested_use` and not
    `considered` (nobody is advising anyone).
13. **Generic surfaces and group nodes.** The catalogue has a few non-counting group
    nodes for generic mentions (red-teaming, AI-control protocols, agent runtime
    monitoring and intervention, mechanistic interpretability, among others). Grade the
    specific child the text supports (human red-teamers → the human red-teaming entry).
    Grade a group node only for generic or unresolved material that supports no child.
    The suppression is by evidence, not by document: a passage that names the specific
    child gives the child; a separate passage that speaks only generically, and makes a
    point no child covers, can still give the group node. Governance instruments (safety
    cases, frontier safety frameworks, incident reporting, export controls) are not
    techniques: no row and no flag; a concrete technique such an instrument requires (a
    capability evaluation that gates release) is graded as itself.
14. **Specific over generic (the counting rule).** A specific child replaces its generic
    parent **for the same evidence**: a domain-specific dangerous-capability evaluation
    replaces the general dangerous-capability evaluation for that domain (the parent
    only for other or unstated domains). Lineage comes only from an entry's `parent` and
    from the entries' own cross-references (a definition that says which entry to use
    instead); do not infer it from similar names. Co-occurring procedure, target and
    setting labels on one experiment (human red-teaming of jailbreak robustness in a
    multi-turn setting) are each graded when the text supports them; they are not merged.
15. **ML implementation methods are not techniques.** Generic ML methods — supervised
    fine-tuning, RL, PPO, GRPO, DPO and its variants, reward modelling, LLM-as-a-judge,
    rejection sampling, best-of-N, distillation, LoRA and other adapters, synthetic data
    generation, sparse autoencoders, activation patching, probing as an analysis method —
    get no row in any role, even when the document is about them. When such a method
    implements a safety technique the document engages, grade the technique; you may
    record the implementation in that row's `note` as `implemented_with: <method>`. A
    paper introducing a new DPO variant engages no safety technique unless it applies it
    to a safety goal; then the safety technique is graded: `introduced` on the
    preference-training entry it falls within (RLHF, if the variant trains harmlessness
    from human preferences) with `variant:<name>`, plus `used` / `evaluated` as the work
    shows (precedent 6); `modified` only when the document alters a specific existing
    named method.

**Precision rules.** Precedents 16, 17 and 19 settle recurring boundary cases toward
precision. They narrow the rules above and never widen them.

16. **An instrument is not a row.** A generic method or a safety technique that the
    document uses only as an experimental instrument or stimulus is not graded as that
    technique: steering vectors or injected activations used to induce a behaviour that
    is then studied, probes used as a measuring tool, a benchmark or dataset used as
    test data for a monitor. Grade it only when the document applies it for its own
    safety purpose (the steering is the mitigation, the probe is the deployed monitor)
    or analyses or evaluates it as a technique. You may record the instrument in the
    `note` of the row it serves as `implemented_with: <method>` (precedent 15).
17. **A benchmark that tests a classifier is not an evaluation row by itself.** Running
    a benchmark or dataset to measure a classifier, guard model or monitor grades that
    classifier or monitor (`evaluated`, plus `used` when it is applied). It does not by
    itself make a row for the evaluation entry whose subject matches the benchmark's
    content (a harmful-request benchmark scored by a guard classifier is not a
    harmful-request evaluation of a model). Grade an evaluation-type entry only when the
    document evaluates what that entry's definition names.
19. **Titles of cited works in the body.** A cited work's title appearing in the body
    (running text, a table, a footnote) gives at most `mentioned: 1` or `cited_work: 1`,
    and only when the title itself names the technique; a title that only relates to the
    technique's topic gives no row. Any higher strength needs the document's own words
    about the cited content (precedent 2).

## 5. Grain and the catalogue

- The catalogue is the v0.9 technique catalogue: technique entries, each with a kind
  (intervention, monitoring or assessment), plus a few non-counting group nodes for
  generic mentions (precedent 13). Depending on your output contract you see it as the
  whole menu, or as a candidate list with full definitions plus a compact one-line index
  of every entry. Grade at the grain the catalogue gives.
- **You never mint a technique slug.** Any technique the document engages that is not in
  the catalogue goes to `extra_techniques` — verbatim name, your one-sentence gloss, and
  `hint_technique` when a catalogue entry is its parent or nearest neighbour. An invented
  slug has no referent and is dropped by the pipeline.
- **Rows only for what you find, and only what you can quote** (§1). An empty
  `techniques` array is a valid recorded outcome (the document engages no catalogue
  technique) — do not invent rows to fill it. Conceptual adjacency is not presence: a
  paper about data filtering does not thereby engage adversarial training or unlearning.
- **Threshold for `extra_techniques`**: report an off-catalogue technique when it is
  materially engaged (any role ≥ 2, or `introduced` at any strength). Do not report
  off-catalogue techniques that would only be `mentioned: 1`.
- Entries show **synonyms** (`aka`) and **named instances** (`e.g.`) where they have
  them, but no occurrence counts. Neither list is **exhaustive**: an entry with no
  aliases is not thereby single-named, and a technique named only by an acronym the
  catalogue does not list, only by a description, or under a synonym is still present. A
  named instance in the text (a named guard model, a named unlearning method) grades the
  entry. Identity resolution beyond the listed aliases remains your job, and it is the
  main source of misses.
- **Lineage.** A few entries name a `parent` entry. When a parent and its child both
  plausibly cover the same text, grade the **most specific** one the text supports, and
  grade the parent only on material that names no specific child (precedent 14).
  Lineage comes only from the `parent` shown on an entry.

## 6. Output schema

Your output contract (after this rubric) gives the exact JSON object to emit; it may add
working fields of its own. The rows it contains always have this shape:

```json
{
  "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
  "techniques": [
    {
      "slug": "<catalogue slug>",
      "roles": {"mentioned": 3, "used": 3, "evaluated": 2},
      "quotes": ["<verbatim, <=200 chars>"],
      "flags": ["recommended"],
      "note": "<optional, short>"
    }
  ],
  "extra_techniques": [
    {
      "name_as_written": "<verbatim surface form>",
      "gloss": "<one sentence: what it is>",
      "hint_technique": "<nearest catalogue slug, or omitted>",
      "roles": {"mentioned": 2, "used": 2},
      "quotes": ["<verbatim>"]
    }
  ],
  "flags": ["no_techniques_found"]
}
```

Rules:
- Emit **no fields beyond your output contract**, and nothing but the JSON object it
  asks for.
- Each technique appears **exactly once**: catalogue techniques in `techniques` (keyed
  by catalogue slug), everything else in `extra_techniques`, never both.
- Omit zero-valued roles. A row with no nonzero role should not be emitted at all.
- **Evidence before scores** (§1, §7): **every** row carries at least one verbatim quote
  that appears in the document, supporting its highest-strength role — at every strength,
  `mentioned: 1` included. Roles graded ≥ 2 that the first quote does not cover need
  their own. When several quotes would serve, **prefer the one that names the method**
  ("we fine-tune Llama-3 to refuse weapons requests" over "we fine-tune the model") — a
  later stage joins these quotes to model mentions. Fabricated or paraphrased quotes
  invalidate the row. (Quote matching is whitespace-, case-, and punctuation-normalized,
  not byte-exact; an elision marked `...` is fine. Quotes that cannot be found are marked
  `quote_unverified` downstream, and a row whose every quote fails is a fabrication
  signal.)
- **Row flag vocabulary** (zero or more per row; flags carry no strength):
  - `claims_absent` — a `suspected_use` row whose claim is that the system does **not**
    use the technique.
  - `confirmed` — a `suspected_use` row relaying or reasoning from an official statement
    rather than inferring unaided.
  - `recommended` / `rejected` — the direction of a `considered` row.
  - `supportive` — prominent praise, the document pitching the technique's strong
    performance, or supportive speculation. The deliberate asymmetric counterpart to the
    `critiqued` category (precedent 3). Usable on any row.
  - `variant:<name as written>` — a named or unnamed variant, or the document's own new
    method, graded on the entry it falls within (§3.7, precedent 6).
  There is **no** `stance` flag. Criticism is `critiqued`; approval is `supportive`.
- Document flag: `no_techniques_found` only when both `techniques` and
  `extra_techniques` are empty (the pipeline sets and clears it from the two arrays in
  any case).
- `repairs`, `low_confidence`, `judge`, and `taxonomy_stamp` are written by the
  pipeline, not by you. Do not emit them.

Worked minimal example — a paper that trains circuit breakers and compares against
adversarial training:

```json
{
  "doc_kind": "paper",
  "techniques": [
    {"slug": "circuit-breakers-representation-rerouting", "roles": {"mentioned": 4, "used": 4, "evaluated": 3},
     "quotes": ["We train circuit breakers into Llama-3-8B by rerouting harmful representations."]},
    {"slug": "adversarial-training-jailbreak-robustness", "roles": {"mentioned": 2, "used": 2, "evaluated": 2},
     "quotes": ["We compare against an adversarial-training baseline trained on the same attacks."]},
    {"slug": "rlhf", "roles": {"mentioned": 1},
     "quotes": ["Instruction-following models are typically aligned with RLHF."]}
  ],
  "extra_techniques": [],
  "flags": []
}
```
RLHF is named once as background lineage → `mentioned: 1`, no other role — but it still
carries the sentence that names it, because a row with no quote is not emitted (§1).
Note what is **not** here: no row for any other catalogue technique, and in particular no
row for the many techniques a paper like this one is *adjacent* to.

Second worked example — a commentary post, the shape the commentary categories exist for. It
argues RLHF is failing, guesses at what a lab is running, and urges a research direction.
Note that it has **no** `used`, `evaluated`, `modified`, or `introduced` row at all:

```json
{
  "doc_kind": "commentary",
  "techniques": [
    {"slug": "rlhf", "roles": {"mentioned": 4, "discussed": 4, "critiqued": 4},
     "quotes": ["RLHF optimizes for what raters approve of, not for what is true, and no amount of rater training closes that gap."]},
    {"slug": "deliberative-alignment", "roles": {"mentioned": 2, "suspected_use": 2},
     "quotes": ["The refusal wording changed overnight, which to me points at a deliberative pass they haven't announced."]},
    {"slug": "chain-of-thought-monitoring", "roles": {"mentioned": 3, "considered": 3},
     "quotes": ["If I had a research budget I would put all of it on monitoring reasoning traces rather than on another round of preference tuning."],
     "flags": ["recommended"]}
  ],
  "extra_techniques": [],
  "flags": []
}
```
The `deliberative-alignment` row carries **no** flag: the claim is that the technique IS
running, which is the default direction. `claims_absent` would be for the opposite
reading — "whatever they changed, it is not a deliberative pass" — and `confirmed` for a
row reasoning from an official statement rather than from observed behaviour.

## 7. Grading procedure

1. **Read the document**; fix `doc_kind`.
2. **Sweep for technique names and descriptions.** Go through the document looking for
   anything the catalogue could name — in the abstract, the related-work section, the
   methods, the tables, the appendix. Include techniques named only by acronym, only by
   description, or under a synonym. When you are given candidates or reading notes,
   start from them, then look beyond them.
3. **Sweep the document's own method separately** (precedent 1). First-person method
   language names the `used` rows, and they are the easiest to miss. An implementation
   method named there (an LLM judge scoring outputs, LoRA, PPO) is not a row: grade the
   technique it implements (precedent 15).
4. **REQUIRED: deployment sweep. Run this whenever the document discusses a deployed or
   released system's behaviour, training, or quirks** (a model's refusal patterns, a
   release's pricing or tiering, an observed regression, "why does it do X"). Ask
   explicitly: *which catalogue techniques does this document imply are — or are not — in
   use in that system?* Grade them `suspected_use` (§3.10) and set the direction flag:
   `claims_absent` when the document argues the technique is NOT there, `confirmed` when
   it is reasoning from an official statement. This sweep exists because judges reliably
   read such passages as `discussed` and stop: the author is not merely talking about the
   technique, they are making a claim about what a system actually runs, and that claim
   is the census signal. A post about a lab's model that names no technique it thinks is
   running is possible, but check before concluding it. Hedged claims count (§3.10: "our
   best guess", "it may be that", "likely trained with"). First-party statements about
   the organization's own deployed systems are `attested_use` instead (§3.9). Worked
   shape: an evaluator's post on a released model says *"our best guess is that this
   reasoning evolved from reward-seeking during RL"* and, after naming two training
   techniques, *"it may be that there was a training step that induced reasoning along
   these lines"* → the two named techniques are each `suspected_use: 1–2`, not
   `mentioned: 1` (RL itself is an implementation method, precedent 15).
5. **REQUIRED: recommendation sweep.** Go through every prescriptive sentence (*should*,
   *must*, *need to*, *we recommend*, *we urge*, *we propose*, *can reduce*) and list
   each technique it prescribes or rejects. Each becomes a `considered` row with its
   direction flag (§3.11, precedent 9). Judges under-produce this category; most policy
   documents and many posts end with several.
6. **REQUIRED for commentary, forum posts, policy and news: controls sweep.** Ask *which
   controls, mitigations, or techniques does this text talk about at all?* — containment
   and sandboxing, weight security, monitoring of agents (by humans, by other models, of
   reasoning traces), shutdown, access control, evaluations of dangerous capabilities,
   interpretability, unlearning … Map each to its catalogue slug, or to
   `extra_techniques` when it is materially engaged and not in the catalogue.
   Commentary names techniques in plain words ("better containment for the riskiest
   agents", "security measures that are harder to break"), which is why it is where
   recall is lost; this sweep is how a post about containment ends up with a containment
   row. Also sweep the commentary axes: is the text *arguing against* a technique
   (`critiqued`), *guessing what a lab runs* (`suspected_use`), or *urging a course of
   action* (`considered`)? A post can be entirely these with no `used` row at all.
7. **Specific over generic.** For every row, apply precedents 13–14: the child the text
   supports, not its parent or group node, for the same evidence.
8. **Quote first, score second.** For each candidate technique, find the verbatim quote
   that shows it is in the document, then the quotes covering every role you intend to
   grade ≥ 2. Prefer quotes that name the method. Only then assign strengths. **A
   technique for which you cannot find any quote is not present — drop it** (§1). This
   is the step that decides most of the output, and skipping it is how documents acquire
   rows for techniques they never mention.
9. **Score** each category per §2–§4; check the `mentioned` ≥ max(others) invariant and
   the one-sentence rule (§2).
10. **Off-catalogue pass** (§5): anything materially engaged that the catalogue does not
    cover → `extra_techniques` with `hint_technique` where one applies. The document's
    own new method goes there only if it falls within no entry (precedent 6).
11. **Cross-check the sweeps against the rows**: every technique found in steps 4–6 that
    has a catalogue slug has a row with the matching role (`suspected_use`,
    `attested_use`, `considered`, or the role the control plays). Flags: direction flags
    on `suspected_use` / `considered` rows, `supportive` where it applies;
    `no_techniques_found` only if both `techniques` and `extra_techniques` are empty.
12. **Emit JSON** as your output contract specifies, and nothing else.
