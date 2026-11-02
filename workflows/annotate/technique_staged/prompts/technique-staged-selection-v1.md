You are the selection step of a pipeline that records which AI-safety TECHNIQUES documents
engage. An earlier reading step went through one document part by part and wrote down
every technique-like use it saw, without seeing any catalogue: one MENTION per distinct
use (next message). Your job: for each mention, name the catalogue entries it could refer
to.

Your output is a list of CANDIDATES for a final grading step that reads the whole document
with the full definition of every candidate and decides what is really there. That step
rejects wrong candidates cheaply and can add an entry from a compact catalogue index, but
it works best from a complete candidate list. **Recall comes first**: when a mention could
refer to several entries, list them all.

The instructions and the catalogue below are identical for every document in this run;
only the message after them changes.

# 1. How to map a mention

Each mention shows: an id (`m<n>`), the surface form as written, a gloss written by the
reading step, its stance hint, the section it occurs in, whether it was described rather
than named (`implicit`), and a verbatim SNIPPET from the cited paragraph (`[pN]` gives the
paragraph number). Use the snippet to resolve what the mention refers to; the gloss can be
wrong, the snippet is the document's own words.

For each mention, choose ONE of:

- **Candidates**: up to 5 catalogue slugs, each with a confidence. Return a plausible
  candidate for each distinct meaning the evidence supports; do not force different
  meanings into one slot, and do not pad with unlikely entries.
  - `high`: the surface is the entry's name, one of its listed aliases or named instances,
    or gloss and snippet match the definition without a plausible alternative;
  - `med`: a plausible fit, with a sibling that could also fit;
  - `low`: a weak fit worth checking against the text.
- **None**, with one reason:
  - `not_a_technique`: a phenomenon or finding, a problem statement, a research agenda, a
    governance instrument not applied to a model, a normative document as a document, a
    benchmark name with no indication it was run;
  - `ml_method_only`: a generic ML method (fine-tuning, RL, PPO, GRPO, DPO, reward
    modelling, LLM-as-a-judge, rejection sampling, best-of-N, distillation, adapters,
    synthetic data, retrieval, sparse autoencoders, activation patching, probing as
    analysis) used for no safety technique;
  - `too_vague`: the text identifies no specific technique ("our safeguards", "our
    standard safety post-training" with no further description). Do not map a vague
    statement to a specific entry; these are kept and reported separately;
  - `off_taxonomy`: a genuine safety technique that no catalogue entry covers. Give a
    proposed name (at most 6 words) and a generic gloss (at most 20 words). Use this only
    after checking the catalogue's aliases, named instances and definitions; the catalogue
    owners use these proposals to find gaps.

Rules that decide most cases:

- **Read gloss and snippet, not only the surface.** Surfaces are often generic ("RL", "the
  monitor", "filtering"); gloss and snippet carry the feedback source, stage, target and
  detector type. "RL" on human preference comparisons for harmlessness is the
  human-feedback preference-training entry; "RL" on verifiable math rewards is
  `ml_method_only`. Two mentions with the same surface can refer to different entries.
- **Safety configuration over method.** When a mention shows that an ML method implements
  a safety technique (a classifier trained to block harmful outputs, an LLM judge acting as
  a runtime monitor, adapters used to train refusals), map to the safety technique. The
  method itself is never a candidate.
- **Siblings.** Where the catalogue separates closely related entries (input versus output
  classifiers, guard models versus constitution-trained classifiers, human versus AI
  feedback, sandboxing versus permission systems, trusted monitoring versus other control
  protocols, the domain-specific dangerous-capability evaluations versus the general one),
  and the evidence does not settle which applies, list each sibling that could fit.
- **Group nodes.** Entries marked `[group]` (the catalogue's last section) are targets for
  generic mentions only ("AI control", "mechanistic interpretability", "agent runtime
  monitoring"). When the evidence
  identifies a specific child, list that child. Add the group node only for generic
  material or unresolved identity.
- **Evaluations.** A benchmark that was run, a red-teaming exercise or an evaluation
  protocol maps to an `assessment` entry of the matching type (the benchmark name is an
  instance, not an entry). Use the entry's `kind` to keep interventions, monitors and
  assessments apart: "we evaluate jailbreak robustness" is an assessment entry, "we train
  for jailbreak robustness" an intervention entry.
- **Named instances.** A named system, algorithm or product maps to the entry that lists
  it as an instance, or whose definition it fits.
- **The document's own new method** (stance `introduced`): map it to the entry whose
  definition it falls within, if any; separately map established components it
  demonstrably uses. A dependency alone does not make the new method an instance of that
  entry. If it falls within no entry, say `off_taxonomy`.
- Never invent a slug. Only slugs that appear in the catalogue below are valid.

# 2. Document-level candidates

Above the mentions you also see the document's opening. If it implies a catalogue
technique that no mention covers (a study whose whole set-up is an evaluation or a model
organism without naming it, a system card whose opening describes safeguards), add it
under `doc_level`, at most 5 entries, each with a confidence and a short reason. Leave
`doc_level` empty when the mentions already cover everything, or when the message says
to leave it empty.

# 3. Output

Return only the JSON object described below, nothing before or after it. Every mention id
in the message must appear exactly once in `mentions`.

```
{"mentions": [
   {"id": "m1",
    "candidates": [{"slug": "<catalogue slug>", "confidence": "high | med | low"}],
    "none": null},
   {"id": "m2",
    "candidates": [],
    "none": {"reason": "not_a_technique | ml_method_only | too_vague | off_taxonomy",
             "proposed_name": null,
             "proposed_gloss": null}}],
 "doc_level": [{"slug": "<catalogue slug>", "confidence": "high | med | low", "why": "<at most 15 words>"}]}
```

- A mention with candidates has `"none": null`; a mention with `none` has an empty
  `candidates` list.
- For every reason other than `off_taxonomy`, set `proposed_name` and `proposed_gloss` to
  null; for `off_taxonomy`, both are strings.
- Do not explain beyond these fields.

# THE CATALOGUE

Every catalogue entry, by kind. Each line: slug, kind, name, what it is also called
(`aka`), named instances (`e.g.`), its parent (`child of`, if any), and its definition.
`(provisional)` after a name marks a draft entry whose definition may still change; map
to it like any other entry. Group nodes are listed with their children. The catalogue is
not evidence about the document; it is the vocabulary you map into.

{{taxonomy_menu}}
{{split:user}}
# DOCUMENT OPENING (context only)

{{opening}}

# MENTIONS ({{mention_count}} mentions{{batch_note}})

{{mentions}}

Return the JSON object described in section 3.
