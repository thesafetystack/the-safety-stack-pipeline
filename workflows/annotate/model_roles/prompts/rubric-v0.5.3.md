# Model-role grading rubric

## 1. Purpose and task

Given one document (paper, blog post, model card, report, commentary) and a
**menu** of candidate models already detected in it by mechanical vocabulary
matching, grade **which role(s) each model plays in this document** and **how
prominently**. Output feeds the stack project's deployment census (which models
get safety techniques applied), research-attention map (which models the field
studies, black-box vs white-box), and ecosystem-dependency map (which models
research runs on as judges, generators, scaffolds).

The unit of grading is the (document, model) pair. Each pair gets a strength
0–4 in each of ten **non-exclusive** role categories. Most pairs will have 1–3
nonzero categories.

**Grade only from the document text.** Outside knowledge may be used to resolve
identity (recognizing that "Sonnet" in context means a Claude model) but never
to fill in roles the document does not exhibit ("everyone benchmarks GPT-4" is
not evidence this document does).

## 2. Strength scale

Strength measures **prominence of that role in this document** — not the
model's importance, not confidence in the judgment.

| Strength | Anchor |
|---|---|
| 0 | Absent. The role is not exhibited for this model. |
| 1 | Trace. A single brief occurrence — one sentence, one table row among many, a footnote. Deleting it would not change the document. |
| 2 | Minor but real. A paragraph's worth, a secondary experiment, one of several minor components. The document notices this model in this role. |
| 3 | Substantial. A major thread — one of the main subjects, tools, or discussion topics; a section's worth of material; removing it changes the document materially. |
| 4 | Central. The document's headline for this role — the main result, system, release, or discussion is about (or built on) this model. An honest abstract would name it. |

Calibration pair (commentary): a Zvi post *entirely about GPT-5* → GPT-5
`discussed: 4`. A Zvi post *surveying the GPT lineage* → each named GPT model
`discussed: 2–3` depending on space given. These are joint vectors, not single
numbers: the same posts typically also carry `cited_work` for the specific
relayed results they build on (see precedent 13).

Rules:
- **Prominence is functional, not typographic.** A model carrying a
  load-bearing role in the work (the fine-tuned subject, the judge behind
  every result) grades by that role's weight in the contribution, even if
  named only briefly; when textual footprint and functional weight diverge,
  functional weight wins.
- Integer values only. When genuinely torn between two adjacent values, pick
  the one whose anchor text fits better; if still torn, pick the **lower** and
  say so in `note`.
- Do not average across roles or across sections; grade each role on its own
  strongest showing.
- A model can hold strength 4 in more than one category (a model card is
  typically `introduced: 4` and `evaluated_behavioral: 3–4`).
- Several models can each hold 4 in the same category (a head-to-head
  comparison paper has two central subjects).

## 3. The ten categories

Categories are grouped by depth of engagement. Each entry: definition,
includes/excludes (pointing at the neighboring category the border is shared
with), and exemplars. Exemplars marked *(constructed)* are illustrative
placeholders to be replaced with verbatim corpus snippets after the sample
pass.

### Peripheral

#### 3.1 `mentioned` — the model is named in the document

**Umbrella presence category.** Operational rule: `mentioned` = the maximum of
the other nine strengths, raised above that only when the model's
mention-only material (passing references outside any other role) would by
itself grade higher on the §2 anchors. Invariant: `mentioned` ≥ every other
category's strength; the pipeline clamps violations upward. A model with
`mentioned: 1` and all else 0 is the classic "merely mentioned" case — the
main downstream filter.

- Includes: any genuine naming of the model — examples, motivation, capability
  lists, passing comparisons.
- Excludes: false-positive matches (surface string does not refer to the
  model — see §6 flags); mentions only inside bibliography entries with no
  in-text presence (grade 0, flag `bib_only` if the menu forced it in).
- *(constructed)* "Modern assistants such as ChatGPT and Claude have made
  alignment a mainstream concern." → both `mentioned: 1`.

#### 3.2 `cited_work` — the model figures through cited literature

The document relays or builds on **other work's** claims or results about the
model: related-work summaries, secondhand benchmark numbers, "X et al. showed
GPT-4 can …".

- Includes: quoting or summarizing published results about the model;
  positioning against prior work done on the model. Can reach 3–4: a document
  whose substance is organizing others' results about a model (meta-analysis,
  deep survey section) grades high here.
- Excludes: the document's own experiments or claims (→ subject roles or
  `discussed`); bare name-drops with no relayed claim (→ `mentioned` only).
- *(constructed)* "Prior work found RLHF'd Llama-2 models refuse at high rates
  on borderline queries [17]." → Llama-2 `cited_work: 1–2`.

#### 3.3 `discussed` — the document's own substantive analysis, without new experiments

The document argues, critiques, forecasts, compares, or reports about the
model **in its own voice**, from public information — no new experiments on
the model performed here. The dominant role in commentary, news, policy, and
incident writeups.

- Includes: capability/safety opinions and arguments; release coverage;
  forecasts about announced-or-rumored models; incident reporting (a jailbreak
  seen in the wild); editorial comparison of models from public data.
- Excludes: relaying others' specific results (→ `cited_work`); first-person
  usage reports (→ `usage_experience`); systematic measurement (→
  `evaluated_behavioral`).
- Speculative or unreleased models can only ever be `mentioned`/`cited_work`/
  `discussed`.
- A research paper's discussion of *its own* results stays inside the subject
  roles; `discussed` fires only for analysis beyond the document's own
  experiments.
- *(constructed)* "o3's deliberative alignment approach is, I think, the most
  interesting safety bet of the year, and here is why it may fail…" (three
  paragraphs) → o3 `discussed: 3`.

### Practical use

#### 3.4 `usage_experience` — informal first-person use

The author (or their org) reports **practical experience using** the model —
workflows, coding assistance, day-to-day impressions — without a systematic
protocol.

- Includes: "we've been using Claude for our data pipeline"; qualitative
  impressions from real use; first-person workflow posts.
- Excludes: structured evaluation with defined tasks/metrics (→
  `evaluated_behavioral`); the model embedded in a described system (→
  `built_on`); **advice and recommendations** — second-person prescriptions
  ("you want to be using X for this") are `discussed` even when plainly
  derived from the author's use; `usage_experience` requires a first-person
  report of the author actually having run the model. Others' quoted usage
  reports are `discussed`/`cited_work` per the own-work rule.
- Strength: a single-sentence own-use aside ("I also tried Opus 4.1, which
  made several comically wrong assertions") is trace → 1, per §2.
- *(constructed)* "I drafted all the boilerplate with GPT-4o; it handled the
  schema migrations surprisingly well." → GPT-4o `usage_experience: 2`.

#### 3.5 `built_on` — substrate of a built system

The document **builds, describes, or ships a system whose identity includes
this model**: the system (agent, product, pipeline, workflow) is part of the
document's contribution, and the model is a load-bearing component of it —
via prompting, scaffolding, APIs, RAG, tool-use. Weights unchanged. A
scaffold that exists merely to *measure* models is not `built_on` for the
measured models (see precedent 7).

- Includes: agents/scaffolds running on the model; products and demos; prompt
  frameworks; the base model of a pipeline the paper contributes.
- Excludes: apparatus for producing results about something else (→
  `instrument`; see the §3.6 border note — the arrows are pointers, not
  exclusivity rules); any weight change (→ `modified`).
- *(constructed)* "Our autonomous research agent uses Claude 3.5 Sonnet as its
  planning model." → Claude 3.5 Sonnet `built_on: 3–4`.

#### 3.6 `instrument` — part of the experimental method

The model is **measurement or production apparatus** for results about
something else: LLM-judge/grader, synthetic-data generator, red-team attacker,
monitor, simulated user or agent, distillation **teacher**, annotation
assistant.

- Includes: "GPT-4 scored the responses"; attacker models in automated
  red-teaming; models generating training data for another model; trusted
  monitors in control experiments.
- Excludes: the built system itself being the contribution (→ `built_on`);
  the model being the thing measured (→ `evaluated_behavioral` /
  `analyzed_internals`).
- **Operational test for the `built_on`/`instrument` border** — ask: *what
  are the document's claims about?* Claims about the system containing the
  model (its capabilities, its design, its behavior) → `built_on`. The model
  produces judgments, data, attacks, or labels used to establish results
  about something else → `instrument`. Claims about the model itself → the
  subject roles (§3.7–3.9), not `built_on`. A scaffold's mere existence does
  not make its model `built_on` when the model is what is being measured.
- Border note: an automated red-teaming *pipeline paper* typically has the
  attacker model both `built_on` (the pipeline is the contribution) and
  `instrument` — assign both; do not agonize.
- *(constructed)* "Responses were graded by GPT-4 as judge (prompt in App. C)."
  → GPT-4 `instrument: 1–2`.

### Subject of the work

#### 3.7 `modified` — weights or training changed

The document's own work **changes the model**: fine-tuning (SFT / RLHF / DPO /
RLVR), adapters/LoRA, unlearning, model editing, pruning, quantization,
merging, continued pretraining, distillation **student**.

- Includes: safety training applied to the model; creating model organisms by
  fine-tuning; releasing a derivative checkpoint (then usually also
  `introduced`).
- Attribution: `modified` attaches to the **base** model's node; if the
  released derivative is itself on the menu (or reported via `extra_models`),
  it carries `modified` (+ `introduced`) too.
- Excludes: prompt-only adaptation (→ `built_on`); inference-time activation
  steering without weight change (→ `analyzed_internals`); fine-tuning done by
  a third party and merely reported here (→ `cited_work` or `discussed`).
- *(constructed)* "We fine-tune Llama-3-8B with DPO on our preference data."
  → Llama-3-8B `modified: 3–4`.

#### 3.8 `evaluated_behavioral` — systematically measured, black-box

The document **runs the model and measures it** through inputs and outputs
with a defined protocol: benchmarks, capability or safety evals, red-teaming
campaigns, jailbreak testing.

- Includes: bespoke evals and published benchmarks alike; the model as a
  baseline row (strength 1–2); the model as the eval's focal target (3–4);
  self-run evals in a model card; measuring an agent or system whose backbone
  the model is (see precedent 7).
- Excludes: informal impressions (→ `usage_experience`); measurement through
  internals (→ `analyzed_internals`); numbers relayed from other papers (→
  `cited_work`). Comparison-table rows of unstated provenance default to
  `evaluated_behavioral: 1` when they sit under the document's own protocol;
  rows explicitly sourced to other work → `cited_work`.
- *(constructed)* "We test 12 models on our sycophancy suite; GPT-4o and
  Claude 3.5 receive the full ablation battery." → GPT-4o, Claude 3.5
  `evaluated_behavioral: 3–4`; the other ten `evaluated_behavioral: 1–2`.

#### 3.9 `analyzed_internals` — white-box access

The document works with the model's **internals**: probing, SAEs, activation
analysis or steering, circuit analysis, logit lens, attention analysis,
representation engineering, weight inspection or diffing.

- Includes: interp studies; steering-vector work; internals-based monitors
  applied *to this model as subject*; gradient/Hessian/influence-function and
  training-data-attribution methods (they require white-box access).
- Excludes: weight changes intended as the deliverable (→ `modified`; model
  editing/unlearning via internals is legitimately both).
- *(constructed)* "We train SAEs on layers 8–24 of Gemma-2-9B." → Gemma-2-9B
  `analyzed_internals: 4`.

#### 3.10 `introduced` — the document introduces or officially documents the model

Release papers, technical reports, model cards, system cards — the document
is (or excerpts) the developer's own introduction or official documentation of
the model. Also: releasing a derivative artifact (jointly with `modified`).

- Includes: "we announce/release X"; official model/system cards; developer
  technical reports.
- Excludes: third-party release coverage (→ `discussed`); papers about a model
  by unaffiliated authors; third-party evaluation reports are **never**
  `introduced`, even when self-described as "technical report".
- *(constructed)* A Claude 3.7 system card → Claude 3.7 `introduced: 4`,
  `evaluated_behavioral: 3–4` (self-run evals), plus whatever else the card
  exhibits.

## 4. Boundary precedents

Rulings on recurring hard cases. When a new hard case arises, decide by
analogy to the closest precedent and record it in `note`.

**Own-work rule (governs every precedent below):** the subject and use roles
(`usage_experience` through `analyzed_internals`, and `introduced`) require the
activity to be the **document's own work**. The same activity relayed from
other work grades `cited_work`; analyzed editorially, `discussed`.

1. **One-line judge.** "Graded by GPT-4" and nothing more → `instrument: 1`.
2. **Distillation.** Teacher → `instrument`; student → `modified` (+
   `introduced` if released).
3. **Baseline row.** Undifferentiated comparison-table rows →
   `evaluated_behavioral: 1–2`. A comparison model that receives its own
   analysis, sections, or headline results is not capped — grade it by the §2
   anchors (it can reach 4).
4. **Speculative model.** Rumored/announced/unreleased (GPT-5 pre-release) →
   `discussed` (or `mentioned`); never a subject or use role.
5. **Model card.** `introduced: 4` + `evaluated_behavioral` for self-run
   evals; red-team sections likewise `evaluated_behavioral`.
6. **Technique paper, uses-vs-works.** Technique *trained into* Llama →
   `modified`; technique *demonstrated on* Llama → `evaluated_behavioral`
   (or `analyzed_internals` if white-box). Often both.
7. **Agent/system paper — fork on what the system is for.**
   *System as contribution* (the paper builds/ships the agent, pipeline, or
   product; its claims are about that system): backbone model →
   `built_on: 3–4`; if the system's performance is also measured, the
   backbone additionally grades `evaluated_behavioral`. Exemplar: a method
   paper whose contribution is a feedback system running on named models.
   *System as measurement harness* (the paper's claims are about the
   **models**, measured through a scaffold/benchmark): the measured models →
   `evaluated_behavioral`, NOT `built_on`; harness-internal models (judge,
   simulator, grader) → `instrument`. Exemplar: a long-horizon agent
   benchmark diagnosing where models fail — the evaluated backbones are
   subjects, not substrates.
8. **Survey.** Models appearing via surveyed literature → `cited_work: 1–2`;
   a survey's deep per-model sections can reach `cited_work: 3`.
9. **Product vs model.** Grade the node the menu gives (ChatGPT and GPT-4 are
   distinct vocabulary nodes); if the document conflates them, grade what the
   text supports per node and note the conflation.
10. **Model organisms.** Fine-tuned to exhibit misalignment, then studied →
    `modified` + `evaluated_behavioral`/`analyzed_internals`.
11. **Steering.** Activation steering at inference → `analyzed_internals`;
    if a steered checkpoint is trained/saved as the deliverable → also
    `modified`.
12. **Incident writeup.** "Users found a jailbreak in Grok" reported
    editorially → `discussed`; if the document reproduces it systematically →
    `evaluated_behavioral`.
13. **Commentary layering.** In commentary posts, specific relayed results
    ("the paper shows Claude schemes in 12% of runs") → `cited_work`; the
    author's own interpretive/argumentative layer about the model →
    `discussed`. A post entirely about one model typically grades both:
    `discussed: 3–4` + `cited_work: 2–3`.
14. **Anaphora resolves to the specific node.** When the document introduces a
    specific model ("Claude Opus 4") and later refers to it by shorthand
    ("Claude", "the model"), those references attribute to the **specific**
    node. A family/group node whose menu presence comes *only* from such
    shorthand gets the all-zero vector with flag
    `subsumed:<family-node>:<specific-node>`.
    (This is the converse of §5's no-re-attribution rule, which governs
    genuinely generic family references.) **Guard:** subsume only into a
    graded **menu node or `variant_models` row** — both stay joined to the
    vocabulary. Never subsume into `extra_models`, and never zero a family
    row whose roles have no menu-or-variant home: in that case the family
    node keeps the roles. Negative exemplar: a paper fine-tuning
    "Qwen2.5-Math-7B" (no menu node) must NOT end with the `qwen2.5` row
    zeroed and the roles exiled to `extra_models` — correct output is a
    variant row under `qwen2.5` carrying the roles, with the `qwen2.5` menu
    row graded on its own direct generic presence (possibly zero with
    `subsumed:qwen2.5:<variant name>`).
15. **Bibliography-adjacent presence — the deletion test.** For citation
    numerals, citation keys and author-group names ("Llama 3 Team, 2024"),
    URL slugs, and reference-list titles: if deleting that apparatus would
    change no claim the document makes about the model, it is bibliographic
    apparatus — flag `bib_only`, do not grade. If the document uses the cited
    content, grade `cited_work` (1–2 typically). Eponymous artifact names
    (BERTScore, the Alpaca dataset) grade the model only when the model
    itself figures in the document.
16. **Third-party fine-tuning, measured here.** A section on how *someone
    else's* fine-tuning affected a model, without the document fine-tuning
    anything → NOT `modified`; the document's own measurements of such
    checkpoints → `evaluated_behavioral`, the relayed provenance →
    `cited_work`.

## 5. Grain and the menu

- Grade **exactly the nodes on the menu**, at the granularity given (the menu
  comes from the vocabulary's most specific match: model > group > family).
  Family rollups are computed downstream; do not re-attribute a family mention
  to a specific model even when context suggests one — instead say so in
  `note`. **Anti-bleed:** when a family/group node AND one of its specific
  models are both on the menu, the family row grades **only on text that
  names no specific version** — never copy the specific model's roles onto
  the family row.
- Menu entries carry their matched surface strings — use them to locate the
  model in the text, but search the whole document; the matcher's strings are
  a floor, not a ceiling.
- **Weak-menu entries** (flagged `weak` on the menu) are collision-prone
  matches. Confirm the model is genuinely present before grading; if every
  occurrence is a false positive, emit the all-zero vector with flag
  `matcher_fp`. `matcher_fp` means the text does **not refer to this model at
  all** (a person, a product, another word) — occurrences referring to a
  variant, sibling, or version of the node are real presence and grade on the
  node per the variant rule above.
- **Named sub-node models → variant rows.** When the document engages a
  named, specific model that is not a menu node but sits under one — a
  third-party fine-tune of a known base (a named Llama fine-tune), or the
  maker's own family member missing from the menu (a "Qwen2.5-Math-7B" when
  only `qwen2.5` is a node) — grade it as a **variant row** under
  `variant_models`, keyed to the closest menu node: record every concrete
  name as written, and one shared role vector for **all** such models under
  that base in this document (do not split per name; distinguish only via
  the names list). Such models never go to `extra_models`. The base node's
  own row still grades the base's direct presence as usual.
- **Unnamed or internal variants** (a "helpful-only version of Claude 3.5
  Sonnet", an internal snapshot) grade **on the base node**, with the variant
  named in `note`. Models present only as unnamed descriptions ("a prompted
  LLM", "an internal grader") are not graded.
- **Off-menu escape hatch.** If the document materially engages a model that
  is not on the menu **and has no base among the menu nodes** (any role at
  strength ≥ 2, or `introduced` at any strength), report it under
  `extra_models`: the name **as written** (verbatim, findable in the text), a
  supporting quote, and the same role vector. Do not report off-menu models
  that would only be `mentioned: 1`; derivatives of menu models go to
  `variant_models`, not here. Pay particular attention to the **document's own
  trained or fine-tuned artifacts**, which are often named obliquely (a
  footnote's repository id, "two LSTMs trained by ourselves") — these carry
  `modified` and are the census's most valuable rows; do not let a missing
  clean name drop them.

## 6. Output schema

One JSON object per document:

```json
{
  "doc_kind": "paper | blogpost | commentary | model_card | system_card | technical_report | eval_report | news | policy | other",
  "models": [
    {
      "node_id": "<menu node id>",
      "roles": {"mentioned": 2, "discussed": 2},
      "evidence": [
        {"roles": ["discussed"], "quote": "<verbatim, <=200 chars>"}
      ],
      "note": "<optional, short>"
    }
  ],
  "variant_models": [
    {
      "base_node_id": "<menu node id>",
      "names_as_written": ["<each concrete derivative name, verbatim>"],
      "quote": "<verbatim context quote>",
      "roles": {"mentioned": 3, "modified": 3, "evaluated_behavioral": 3}
    }
  ],
  "extra_models": [
    {
      "name_as_written": "<verbatim surface form>",
      "quote": "<verbatim context quote>",
      "roles": {"mentioned": 2, "evaluated_behavioral": 2}
    }
  ],
  "flags": ["matcher_fp:<node_id>", "bib_only:<node_id>", "subsumed:<family_node>:<specific_node>"]
}
```

Rules:
- Emit **no fields beyond this schema** — no per-model `flags`, no empty
  `evidence` arrays, no extra keys.
- Each model appears **exactly once**: menu nodes in `models`, off-menu models
  in `extra_models`, never both. If an `extra_models` candidate is in fact
  covered by a menu node, grade it on that node only.
- Omit zero-valued roles from `roles`.
- **Evidence before scores** (procedure §7): every role graded ≥ 2 must carry
  at least one verbatim quote that appears in the document. When several
  quotes would serve, **prefer the one naming the method or technique
  involved** ("we fine-tune Llama-3 with DPO" over "we fine-tune Llama-3") —
  downstream stages join these quotes to a technique taxonomy. Quotes are the
  probe; fabricated or paraphrased quotes invalidate the row. (Pipeline note:
  quote matching is validated whitespace-normalized, not byte-exact.)
- `mentioned` needs no quote (its evidence is the matcher's own strings)
  unless it is the only nonzero role at ≥ 2.
- Models on the menu with genuinely zero presence still get a row (all-zero
  `roles`), and **every all-zero row carries exactly one flag** stating why:
  `bib_only:<node>` (present only in references / fails the deletion test),
  `matcher_fp:<node>` (the matched text does not refer to this model),
  `subsumed:<node>:<target>` (anaphora, §4.14), or `not_found:<node>` (you
  could not locate the matched strings or any real presence — absence of
  evidence, NOT a claim about the bibliography). Do not use `bib_only` as a
  default for "found nothing" — that is what `not_found` is for.

Worked minimal rows (the two commonest shapes — emit them exactly like this):

```json
{"node_id": "gemini", "roles": {"mentioned": 1}}
```
— named once in passing ("...assistants such as ChatGPT and Gemini..."),
nothing else: `mentioned: 1`, no quote needed, no other keys.

```json
{"node_id": "alpaca-7b", "roles": {}}
```
with `"bib_only:alpaca-7b"` in `flags` — every occurrence sits in the
reference list or fails the precedent-15 deletion test: all-zero row, no
role, **no invented `cited_work`**.

## 7. Grading procedure

1. **Skim the document**; fix `doc_kind`.
2. **Per menu model, locate**: find its occurrences (matcher strings as
   starting points; then search beyond them).
3. **Quote first**: for each model, collect verbatim quotes covering every
   role that will grade ≥ 2 (typically 1–3 quotes per model; one quote may
   evidence several roles). Only then assign strengths.
4. **Score** each category per §2–§4. Check the `mentioned` ≥ max(others)
   invariant.
5. **Sweep for the document's own training work** — search the text for
   first-person training language ("we train", "we fine-tune", "our model",
   "initialized from", "trained by ourselves") and make sure every artifact
   it names carries `modified` on the right row (menu node, variant row, or
   `extra_models`). This is the census's most valuable role and the judges'
   most common miss.
6. **Sweep for off-menu models** (§5) — one deliberate pass over abstract,
   tables, and experimental sections.
7. **Emit JSON** (§6) and nothing else.
