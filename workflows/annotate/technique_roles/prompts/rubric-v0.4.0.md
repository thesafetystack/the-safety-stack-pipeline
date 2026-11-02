# Technique-role grading rubric

## 1. Purpose and task

Given one document (paper, blog post, model card, report, commentary) and a **menu** of
catalogue safety/alignment techniques, decide **which techniques appear in this
document**, **in which roles**, and **how prominently**. Output feeds the stack
project's technique census (which techniques the field applies, studies, and ships) and
is the join surface a later technique×model binding stage consumes.

The unit of grading is the (document, technique) pair. Each pair gets a strength 0–4 in
each of twelve **non-exclusive** role categories. Most pairs will have 1–3 nonzero
categories.

**Unlike a model menu, this menu is not evidence.** No matcher ran: the menu is the
whole catalogue, shown to you regardless of the document. It tells you what may be
named and how it is named — it does not claim anything is present. Most menu entries
are absent from any given document, and **you emit rows only for the ones you find**.

**Grade only from the document text.** Outside knowledge may be used to resolve
identity (recognizing that "our GRPO variant" names the GRPO entry, that "we train a
reward model on human comparisons and optimize against it" is RLHF), but never to fill
in roles the document does not exhibit ("every RLHF paper does rejection sampling" is
not evidence this one does).

**No quote, no row.** Every row you emit — in `techniques` and in `extra_techniques`
alike — MUST carry at least one **verbatim quote from the document** supporting its
highest-strength role. A row without such a quote is invalid output, not a weak row:
drop it. This applies at every strength, including a lone `mentioned: 1`.

**The menu is never evidence.** Seeing a technique on the menu, recognizing that it is
conceptually adjacent to the document's topic, or knowing that documents like this one
usually involve it are all reasons to *look* — never reasons to emit a row. If you
cannot point at the document text, the technique is not in this document. Writing a row
whose own note says the technique is "not named explicitly" or "not present in the text"
is the specific error this rule exists to stop.

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

Calibration pair (the technique analogue of the model_roles Zvi pair): a post or paper
that **is about RLHF** — its argument, its experiments, or its critique is RLHF —
grades RLHF at **4** in the appropriate role. A document that **touches the RLHF
lineage in passing** — "modern assistants are trained with RLHF, and we build on that
line of work" — grades RLHF at **2** (`mentioned`, and `cited_work` if a specific
claim is relayed). One naming with nothing hanging off it is **1**.

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
related-work summaries, secondhand numbers, "X et al. showed DPO underperforms PPO on …".

- Includes: quoting or summarizing published results about the technique; positioning
  against prior work that used it; a survey's per-technique literature paragraphs.
- Excludes: the document's own application or measurement (→ `used` / `evaluated` /
  `analyzed`); a bare name-drop with no relayed claim (→ `mentioned` only).
- Can reach 3–4 when the document's substance is organizing others' results about the
  technique (a technique-focused survey section, a meta-analysis).
- *(constructed)* "Prior work reports that DPO is more stable than PPO at small scale
  [17]." → DPO, PPO `cited_work: 1–2`.

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

- Includes: training methods the authors apply ("we fine-tune with DPO"); components of
  a system the document contributes (a guardrail classifier in the authors' pipeline);
  the technique as **substrate** — inherited, assumed, and depended on rather than
  freshly performed (precedent 5).
- Excludes: someone else's application merely reported (→ `cited_work`); a deployment
  report about production systems, not an experiment (→ `attested_use`); measuring the
  technique's effect as the result (→ `evaluated`, usually **in addition**).
- Own-work rule governs (see §4 preamble): the activity must be the document's.
- *(constructed)* "We fine-tune Llama-3-8B with DPO on our preference data." → DPO
  `used: 3–4`.

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
- *(constructed)* "We compare DPO, IPO, and KTO on three preference datasets." → all
  three `evaluated: 3–4`, and `used: 2–3` (they were run).

### 3.6 `modified` — the document alters or extends the technique

The document changes the technique — a new loss term, a new sampling rule, an
adaptation to a new setting or modality — **without claiming a distinct new technique**.

- Includes: "we extend GRPO with a length penalty"; porting a text-domain defense to
  vision; adding a stage to a published pipeline.
- Excludes: applying the technique as published (→ `used`); proposing something the
  authors name and pitch as new (→ `introduced`, and see precedent 6 for the row it
  goes on); a modification someone else made, reported here (→ `cited_work`).
- Attribution: `modified` attaches to the **base** technique's menu row. If the variant
  is itself a catalogue entry (49 of 292 entries are variants), grade that entry too.
- *(constructed)* "We modify GRPO by removing the length normalization term." → GRPO
  `modified: 3`, `used: 3`.

### 3.7 `introduced` — the document proposes the technique

The document is the technique's origin: it names, motivates, and first presents it.
Expected **rare** — most documents using a technique are not its origin paper.

- Includes: "we introduce X, a method for …"; the official first description of a
  technique in a lab's technical report or system card.
- Excludes: introducing a *variant or application* of an existing catalogue technique (→
  `modified`, or `extra_techniques` with `hint_technique` per precedent 6); third-party
  write-ups of someone else's new method (→ `discussed` / `cited_work`); "we introduce a
  benchmark" where the benchmark is not itself the catalogue technique.
- Nearly always co-occurs with `used` and `evaluated: 3–4`.
- *(constructed)* The DPO paper itself → DPO `introduced: 4`, `used: 4`,
  `evaluated: 4`.

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
- *(constructed)* "We show DPO's implicit reward is under-determined off the preference
  distribution, and prove the resulting failure mode." → DPO `analyzed: 4`.

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
  classifier pass on the output, not an inline refusal." → Input/output content safety
  classifier `suspected_use: 3`.

### 3.11 `considered` — the technique as an option or course of action

The technique is treated as **something to do or not do**: recommended for adoption or
for research, proposed as an application, or weighed and set aside. The category of
agendas, governance advocacy, and method-selection paragraphs.

- Includes: "we considered DPO but chose PPO for stability"; "the field should invest in
  scalable oversight"; "someone should try applying unlearning to this"; a governance
  document urging that a technique be required.
- Excludes: **assessment without an actor and an action** — "DPO is unstable" is a
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
   make sure every technique it names carries `used`.
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
6. **Novel or unnamed variant → `extra_techniques` with `hint_technique`.** When the
   document engages a technique that is not on the menu but sits under one — an unnamed
   twist on GRPO, a new classifier architecture in a catalogued family — report it in
   `extra_techniques` with `name_as_written` (verbatim), a one-sentence `gloss`, and
   `hint_technique` set to the closest menu slug. The parent menu row is still graded
   on the parent's own direct presence. Do **not** invent a slug for it (§5).
   **The document's own new framework built on a catalogue technique** is the commonest
   case: "we propose a gap-analysis framework for formally verifying guardrail
   classifiers" builds on formal verification. The framework goes to `extra_techniques`
   as `introduced` (hint: the catalogue technique); the catalogue technique gets `used`
   (and `modified` if the framework changes it). **Never put `introduced` on the
   catalogue row** unless the document is that technique's origin.
7. **Survey.** Techniques appearing through surveyed literature → `cited_work: 1–2`; a
   survey section giving a technique its own treatment → `discussed: 2–3` as well; a
   survey does not `use` anything.
8. **Named artifact vs technique.** A benchmark, dataset, or product name that
   *embodies* a technique (an eval suite, a released classifier) grades the technique
   only when the technique itself figures in the document's claims — otherwise it is
   apparatus and the technique is at most `mentioned`.
9. **`considered` needs an actor and an action.** This is the guard against `considered`
   swallowing `discussed`, and it is the border most likely to bleed. Ask: *does the
   text put someone in a position to adopt, apply, or decline the technique?*
   - "We considered DPO but chose PPO" → `considered` (actor: the authors; action:
     method selection). Flag `rejected` on DPO.
   - "The field should invest in interpretability" → `considered` (actor: the field;
     action: invest). Flag `recommended`.
   - "Someone should try unlearning on this" → `considered` + `recommended`.
   - "DPO is unstable at small scale" → **`discussed`**, not `considered`. It is a claim
     about the technique's properties; nobody is deciding anything.
   - "DPO is unstable, so don't use it for this" → **both** — the quality claim is
     `discussed`, the prescription is `considered` + `rejected`, and if the instability
     is argued rather than asserted, `critiqued` too.
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
    A mitigation the card *measures* in a with/without table is also `evaluated`. A plan
    ("will include", "we are exploring") is `discussed`, not `attested_use` and not
    `considered` (nobody is advising anyone).
13. **Generic surfaces without a generic entry.** Some techniques exist on the menu only
    as children: generic "red-teaming" (human, automated, multi-turn … children), generic
    "scalable oversight" (debate, recursive reward modelling … children). Grade the child
    the text actually supports (human red-teamers → the human child); if the text is
    generic and supports no child, emit `menu_gap:<surface>` rather than guessing a child.
    Likewise for governance instruments the menu does not carry (safety cases, incident
    reporting, audits, export controls): a `menu_gap` flag each, so the gap is visible.

## 5. Grain and the menu

- The menu is the catalogue's 292 techniques, grouped by family. It is **exhaustive for
  catalogue techniques**: if a technique is in the catalogue, it appears on the menu,
  and you grade it under its menu slug. Grade at the grain the menu gives.
- **You never mint a technique slug.** Any technique the document engages that is not on
  the menu goes to `extra_techniques` — verbatim name, your one-sentence gloss, and
  `hint_technique` when a menu entry is its parent or nearest neighbour. An invented
  slug has no referent and is dropped by the pipeline.
- **Rows only for what you find, and only what you can quote** (§1). An empty
  `techniques` array is a valid recorded outcome (the document engages no catalogue
  technique) — do not invent rows to fill it. Conceptual adjacency is not presence: a
  paper about data filtering does not thereby engage deduplication or adversarial
  augmentation.
- **Threshold for `extra_techniques`**: report an off-menu technique when it is
  materially engaged (any role ≥ 2, or `introduced` at any strength). Do not report
  off-menu techniques that would only be `mentioned: 1`.
- The menu shows **aliases** for entries that have them (`(aka: SFT, instruction
  tuning, IFT)`) but **no occurrence counts and no matched surface forms**, because no
  matcher ran. The alias lists are **not exhaustive**: an entry with no aliases is not
  thereby single-named, and a technique named only by an acronym the menu does not list,
  only by a description, or under a synonym is still present. Identity resolution beyond
  the listed aliases remains your job, and it is the main source of misses.
- **Variants have dotted slugs.** A slug containing a dot is a variant of the entry named
  by its prefix: `dpo.ipo` is a variant of `dpo`, `reward-modeling.multi-attribute-rm`
  of `reward-modeling`. The menu shows both, and the dot is the lineage.
- When a general entry and one of its variants both plausibly cover the same text, grade
  the **most specific** one the text supports; grade the general entry only on material
  that names no specific variant. A document using IPO grades `dpo.ipo`, and grades `dpo`
  only for whatever it says about DPO as such.

## 6. Output schema

One JSON object per document:

```json
{
  "sweeps": {
    "deployment_claims": [{"system": "<named system>", "technique": "<menu slug or surface>",
                           "quote": "<verbatim>", "direction": "uses | does_not_use"}],
    "recommendations": [{"technique": "<menu slug or surface>", "quote": "<verbatim>",
                         "direction": "recommended | rejected"}],
    "controls_discussed": [{"surface": "<as written>", "technique": "<menu slug, or omitted>"}],
    "menu_gaps": ["<technique-shaped surface with no menu slug>"]
  },
  "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
  "techniques": [
    {
      "slug": "<menu slug>",
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
      "hint_technique": "<nearest menu slug, or omitted>",
      "roles": {"mentioned": 2, "used": 2},
      "quotes": ["<verbatim>"]
    }
  ],
  "flags": ["no_techniques_found", "menu_gap:<surface as written>"]
}
```

Rules:
- **`sweeps` comes first and is required** (§7 steps 4–7): it is your working record of
  the four sweeps, written before any row. Each list may be empty, but an empty list
  should be the result of looking. Every sweep entry that names a menu slug must end up
  as a row of `techniques` (or be dropped from the sweep because, on reflection, it is not
  in the text); `menu_gaps` become the document's `menu_gap:` flags. The pipeline reads
  the sweeps but does not store them.
- Emit **no fields beyond this schema**, and emit nothing outside the fenced JSON block.
- Each technique appears **exactly once**: catalogue techniques in `techniques` (keyed
  by menu slug), everything else in `extra_techniques`, never both.
- Omit zero-valued roles. A row with no nonzero role should not be emitted at all.
- **Evidence before scores** (§1, §7): **every** row carries at least one verbatim quote
  that appears in the document, supporting its highest-strength role — at every strength,
  `mentioned: 1` included. Roles graded ≥ 2 that the first quote does not cover need
  their own. When several quotes would serve, **prefer the one that names the method**
  ("we fine-tune Llama-3 with DPO" over "we fine-tune the model") — a later stage joins
  these quotes to model mentions. Fabricated or paraphrased quotes invalidate the row.
  (Quote matching is whitespace-, case-, and punctuation-normalized, not byte-exact; an
  elision marked `...` is fine. Quotes that cannot be found are marked
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
  - `variant:<name as written>` — an unnamed variant graded on the parent row.
  There is **no** `stance` flag. Criticism is `critiqued`; approval is `supportive`.
- Document flags: `no_techniques_found` when `techniques` is empty (the pipeline sets and
  clears it from `techniques` in any case); `menu_gap:<surface>` for **every**
  technique-shaped surface that matches nothing on the menu and is not worth an
  `extra_techniques` row (precedent 13). Listing a surface in `sweeps.menu_gaps` is
  enough: the pipeline turns each into a flag.
- `repairs`, `low_confidence`, `judge`, and `taxonomy_stamp` are written by the
  pipeline, not by you. Do not emit them.

Worked minimal example — a paper that fine-tunes with DPO and compares against PPO:

```json
{
  "doc_kind": "paper",
  "techniques": [
    {"slug": "dpo", "roles": {"mentioned": 4, "used": 4, "evaluated": 3},
     "quotes": ["We fine-tune Llama-3-8B with DPO on 60k preference pairs."]},
    {"slug": "ppo-for-alignment", "roles": {"mentioned": 2, "used": 2, "evaluated": 2},
     "quotes": ["We compare against a PPO baseline trained on the same data."]},
    {"slug": "rlhf", "roles": {"mentioned": 1},
     "quotes": ["Instruction-following models are typically aligned with RLHF."]}
  ],
  "extra_techniques": [],
  "flags": []
}
```
RLHF is named once as background lineage → `mentioned: 1`, no other role — but it still
carries the sentence that names it, because a row with no quote is not emitted (§1).
Note what is **not** here: no row for any of the other 289 menu techniques, and in
particular no row for the many techniques a paper like this one is *adjacent* to.

Second worked example — a commentary post, the shape the commentary categories exist for. It
argues RLHF is failing, guesses at what a lab is running, and urges a research direction.
Note that it has **no** `used`, `evaluated`, `modified`, or `introduced` row at all:

```json
{
  "sweeps": {
    "deployment_claims": [{"system": "the lab's latest model", "technique": "deliberative-alignment",
                           "quote": "The refusal wording changed overnight, which to me points at a deliberative pass they haven't announced.",
                           "direction": "uses"}],
    "recommendations": [{"technique": "eliciting-latent-knowledge", "direction": "recommended",
                         "quote": "If I had a research budget I would put all of it on ELK rather than on another round of preference tuning."}],
    "controls_discussed": [],
    "menu_gaps": []
  },
  "doc_kind": "commentary",
  "techniques": [
    {"slug": "rlhf", "roles": {"mentioned": 4, "discussed": 4, "critiqued": 4},
     "quotes": ["RLHF optimizes for what raters approve of, not for what is true, and no amount of rater training closes that gap."]},
    {"slug": "deliberative-alignment", "roles": {"mentioned": 2, "suspected_use": 2},
     "quotes": ["The refusal wording changed overnight, which to me points at a deliberative pass they haven't announced."]},
    {"slug": "eliciting-latent-knowledge", "roles": {"mentioned": 2, "considered": 3},
     "quotes": ["If I had a research budget I would put all of it on ELK rather than on another round of preference tuning."],
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
2. **Sweep for technique names.** Go through the document looking for anything the menu
   could name — in the abstract, the related-work section, the methods, the tables, the
   appendix. Include techniques named only by acronym, only by description, or under a
   synonym.
3. **Sweep the document's own method separately** (precedent 1). First-person method
   language names the `used` rows, and they are the easiest to miss.
   **Check judge-model usage explicitly during this sweep.** An LLM grading outputs, an
   LLM-as-judge protocol, model-written evaluations doing the scoring — this is
   near-ubiquitous in method papers and is the single most-disputed technique across
   judges in calibration, because it lives under the `scalable-oversight` family whose
   name does not suggest it. "Responses were scored by GPT-4" is a `used` row for
   `llm-as-judge`, not incidental apparatus.
4. **REQUIRED: deployment sweep → `sweeps.deployment_claims`. Run this whenever the
   document discusses a deployed or released system's behaviour, training, or quirks** (a model's refusal patterns, a release's
   pricing or tiering, an observed regression, "why does it do X"). Ask explicitly:
   *which catalogue techniques does this document imply are — or are not — in use in
   that system?* Grade them `suspected_use` (§3.10) and set the direction flag:
   `claims_absent` when the document argues the technique is NOT there, `confirmed` when
   it is reasoning from an official statement. This sweep exists because judges reliably
   read such passages as `discussed` and stop: the author is not merely talking about the
   technique, they are making a claim about what a system actually runs, and that claim
   is the census signal. A post about a lab's model that names no technique it thinks is
   running is possible, but check before concluding it. List every such claim — hedged
   ones included (§3.10: "our best guess", "it may be that", "likely trained with") —
   with the system, the technique and the quote. Worked shape: an evaluator's post on a
   released model says *"our best guess is that this reasoning evolved from reward-seeking
   during RL"* and, after naming two training techniques, *"it may be that there was a
   training step that induced reasoning along these lines"* → the RL technique and the
   two named techniques are each `suspected_use: 1–2`, not `mentioned: 1`.
5. **REQUIRED: recommendation sweep → `sweeps.recommendations`.** Go through every
   prescriptive sentence (*should*, *must*, *need to*, *we recommend*, *we urge*, *we
   propose*, *can reduce*) and list each technique it prescribes or rejects. Each becomes
   a `considered` row with its direction flag (§3.11, precedent 9). Judges under-produce
   this category; most policy documents and many posts end with several.
6. **REQUIRED for commentary, forum posts, policy and news: controls sweep →
   `sweeps.controls_discussed`.** Ask *which controls, mitigations, or techniques does this
   text talk about at all?* — containment and sandboxing, weight security, monitoring of
   agents (by humans, by other models, of reasoning traces), shutdown, access control,
   evaluations of dangerous capabilities, interpretability, unlearning … Map each to its
   menu slug, or to `menu_gaps`. Commentary names techniques in plain words ("better
   containment for the riskiest agents", "security measures that are harder to break"),
   which is why it is where recall is lost; this sweep is how a post about containment
   ends up with a containment row. Also sweep the commentary axes: is the text *arguing
   against* a technique (`critiqued`), *guessing what a lab runs* (`suspected_use`), or
   *urging a course of action* (`considered`)? A post can be entirely these with no `used`
   row at all.
7. **REQUIRED: menu-gap sweep → `sweeps.menu_gaps`.** Every technique-shaped surface you
   found that has no menu slug and is not worth an `extra_techniques` row (precedent 13).
8. **Quote first, score second.** For each candidate technique, find the verbatim quote
   that shows it is in the document, then the quotes covering every role you intend to
   grade ≥ 2. Prefer quotes that name the method. Only then assign strengths. **A
   technique for which you cannot find any quote is not present — drop it** (§1). This
   is the step that decides most of the output, and skipping it is how documents acquire
   rows for techniques they never mention.
9. **Score** each category per §2–§4; check the `mentioned` ≥ max(others) invariant and
   the one-sentence rule (§2).
10. **Off-menu pass** (§5): anything materially engaged that the menu does not cover →
    `extra_techniques` with `hint_technique` where one applies (precedent 6 for the
    document's own framework).
11. **Cross-check the sweeps against the rows**: every sweep entry naming a menu slug has
    a row with the matching role (`suspected_use`, `considered`, or the role the control
    plays). Flags: direction flags on `suspected_use` / `considered` rows, `supportive`
    where it applies; `no_techniques_found` if `techniques` is empty.
12. **Emit JSON** (§6) and nothing else.
