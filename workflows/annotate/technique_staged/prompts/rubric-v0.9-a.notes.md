---
version: "0.9-a"
status: draft — rubric v0.4.0 adapted to taxonomy v0.9, after two design critique rounds; unmeasured
date: 2026-10-17
scope: annotate.technique_staged (modes single, sweep, staged, lexical) — role grading of v0.9 catalogue techniques
depends_on: taxonomy/taxonomy-v0.9.yaml (214 techniques with a kind, 9 group nodes)
changelog:
  - 0.9-a — from rubric v0.4.0 (technique_roles). Mode-neutral: the rubric speaks of "the
    catalogue", shown as the whole menu or as a candidate list plus a compact index; menu or
    candidate wording, `sweeps`, `menu_gaps` and `rejected` live in each prompt's output
    contract. Catalogue v0.9: entries carry a kind (intervention, monitoring, assessment),
    non-counting group nodes take generic mentions only (precedent 13, suppressed by evidence,
    not by document), lineage only from an entry's `parent` and the entries' own
    cross-references (§5, precedent 14). New rules shared by every mode and the gold: a
    bibliography-only presence gives no row (precedent 2); the document's own new method within
    an entry is `introduced` on that entry with `variant:<name>` (precedent 6, §3.7), extras only
    beyond every entry; ML implementation methods are never rows (precedent 15); a vague
    attestation supports no row (§3.9). Round-2 critique changes: §1 an unnamed technique is
    eligible when quoted text unambiguously describes it; §2 a passing sentence about RLHF
    lineage is `mentioned: 1` (2 only for paragraph-level treatment or an evidenced secondary
    functional role); §3.4 a secondhand deployment report is `cited_work` / `discussed`
    (`suspected_use` when argued from evidence), first-party shipped safeguards and first-party
    pre-release evaluations in a card are `used` + `attested_use`; §3.6 `modified` only for an
    alteration of a specific existing named method, and a modified form that is itself a child
    entry is graded on the child instead of the base (consistent with precedent 14); §3.7
    `used` / `evaluated` only when the document applies or measures the proposal; precedent 15's
    DPO-variant example is `introduced` on the preference-training entry; precedent 14's
    lineage examples without a v0.9 `parent` (scheming evaluations, constitutional classifiers)
    removed; the second worked example satisfies `mentioned` >= every role; §6 / §7
    `no_techniques_found` only when both `techniques` and `extra_techniques` are empty.
---

`rubric-v0.9-a.md` is the rubric as the grader sees it (§1-§7), included by the grading, single and sweep
prompt files. This file is not sent.
