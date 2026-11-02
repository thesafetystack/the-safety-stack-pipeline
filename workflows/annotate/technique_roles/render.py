"""The menu for ``annotate.technique_roles`` (ported from the off-DB prototype
``tomas/technique-roles`` df411eb, ``render.py``; approach A only).

Turns the technique taxonomy into the *menu* text, the prompt file's system-part fill
``taxonomy_menu`` (``prompts/<name>.md``, :mod:`stack.annotate.prompt_files`), and cuts the document
window. The prompt text itself lives in the prompt files (D591); this module renders no prompt. Until
D591 it also re-implemented the prototype's ``prompts/a.md.j2`` in Python (v4 has no jinja2);
``prompts/technique-roles-v1.md`` reproduces that output byte for byte (checked by a golden when the
re-implementation was removed; ``technique-roles-v2`` is the default since).

Design decisions carried from the prototype (2026-08-02):

* **No mechanical mention pass.** The taxonomy carries no mention patterns, so the judge
  discovers techniques against the *whole catalogue*. The menu is document-independent.
* **Menu entry** = ``- <slug> — <name> (aka: <aliases>): <description>``, grouped under a family
  header. Fields and facets are dropped (the facet stage's input, noise for role grading).
  Variants are not marked: their dotted slug's prefix IS the parent (``dpo.ipo`` under ``dpo``;
  ``type == "variant"`` ⟺ dotted slug ⟺ ``variant_of`` = the prefix, 49/49 in v0.8), and the
  rubric (§5) and the prompt tell the judge to read the dot as lineage.
* **Prompt order**: rubric → menu → output contract → **document last**. In v4 the part before
  the document is the client's fixed prefix (the cached ``system`` block, byte-identical across
  every document of a run: 142k characters with technique-roles-v1, 153k with v2, measured
  2026-10-17 on v0.8; roughly 35-38k tokens at 4 characters a token) and the document is the user message.
* **No ``doc_meta`` line** (the prototype's title · venue · date · url header): the basis is the
  text only (``basis_kind = 'text'``), and a header that is not in the basis would let a title
  edit change the prompt without re-asking. Stated, not measured: the prototype's calibration
  ran with the header.

The family glosses (:data:`FAMILIES`) are the prototype's DRAFT glosses, unreviewed (the taxonomy
carries no family metadata); approach A shows them only as menu section headers.

No dataclass, no pydantic model (written when the stage loader ran ``main.py``, and so this
sibling, outside ``sys.modules``; registered since D435): :class:`TechniqueEntry` is a
``NamedTuple``, validated by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from stack import yamlio

#: The prototype's window (= model_roles'): 300k chars ≈ 75k tokens.
DOC_CHAR_CAP = 300_000

#: Family slug → (display name, gloss): the prototype's DRAFT glosses, verbatim (module docstring).
FAMILIES: dict[str, tuple[str, str]] = {
    "adversarial-robustness": (
        "Adversarial robustness",
        "Training- and inference-time defenses that keep safety behaviour intact under "
        "adversarial input — jailbreaks, prompt injection, distribution shift, tampering.",
    ),
    "agentic-control": (
        "Agentic control & runtime safety",
        "Runtime constraints, monitoring, and control protocols that bound what an autonomous "
        "agent may do and catch it when it strays.",
    ),
    "constitutional-ai-feedback": (
        "Constitutional AI & principle-based feedback",
        "Alignment from an explicit written principle set — constitutions, model specs, "
        "rule-based rewards — rather than from unstructured human preference labels.",
    ),
    "data-curation": (
        "Training-data curation",
        "Selecting, filtering, synthesising, and quality-controlling training data so that "
        "safety properties come from the corpus rather than from later correction.",
    ),
    "decoding-control": (
        "Decoding & inference-time control",
        "Steering generation at inference — sampling, reranking, constrained decoding, "
        "reasoning-mode and system-prompt control — without changing weights.",
    ),
    "foundations": (
        "Foundations",
        "Foundational and formal programmes: agent foundations, guaranteed-safe AI / formal "
        "verification, model welfare assessment.",
    ),
    "guardrail-classifiers": (
        "Guardrail classifiers & filters",
        "Separate classifier or filter components wrapped around a model that screen inputs, "
        "outputs, or whole exchanges at serving time.",
    ),
    "honesty-calibration": (
        "Honesty & calibration",
        "Making assertions truthful and confidence warranted — hallucination reduction, "
        "grounding and citation, abstention, calibration, anti-sycophancy.",
    ),
    "interpretability": (
        "Interpretability",
        "White-box methods for reading a model's internals — SAEs, probes, circuit analysis, "
        "activation steering, attribution — and the monitors built on them.",
    ),
    "preference-optimization": (
        "Preference optimization & reward modeling",
        "Learning from preference or reward signal: RLHF/RLAIF, reward modelling, and the "
        "direct-optimization family (DPO and its relatives).",
    ),
    "pretraining-safety": (
        "Pretraining-stage safety",
        "Safety interventions applied during pretraining itself — objectives, data "
        "augmentation, differential privacy, poisoning defenses, pre-training evaluation.",
    ),
    "provenance-technical": (
        "Provenance & technical controls",
        "Technical provenance and supply-chain controls — watermarking, content credentials, "
        "model signing, weight security, compute and access monitoring.",
    ),
    "redteam-eval": (
        "Red-teaming & evaluation",
        "Measurement instruments that probe for dangerous or misaligned behaviour: "
        "red-teaming, dangerous-capability evals, benchmark suites, auditing methodology.",
    ),
    "rl-reasoning": (
        "RL for reasoning",
        "RL that trains reasoning ability — verifiable-reward RL, the GRPO/PPO-variant "
        "algorithm family, process rewards, long-CoT and long-horizon agentic RL.",
    ),
    "scalable-oversight": (
        "Scalable oversight",
        "Supervising models on tasks humans cannot directly check — debate, critique models, "
        "weak-to-strong generalization, ELK, recursive amplification schemes.",
    ),
    "sft-instruction": (
        "SFT & instruction tuning",
        "Supervised fine-tuning and instruction tuning, including instruction-data synthesis, "
        "distillation, persona/character training, and parameter-efficient variants.",
    ),
    "unlearning-editing": (
        "Unlearning & model editing",
        "Removing or surgically altering knowledge and capabilities in a trained model — "
        "unlearning, model editing, concept suppression.",
    ),
}


# ── the taxonomy ─────────────────────────────────────────────────────────────────────────────


class TechniqueEntry(NamedTuple):
    """One taxonomy technique, on the fields the menu consumes (the rest is not read)."""

    slug: str
    name: str
    type: str
    family: str
    description: str
    aliases: tuple[str, ...]
    variant_of: str | None


class Taxonomy(NamedTuple):
    version: str
    techniques: tuple[TechniqueEntry, ...]


def load_taxonomy(path: Path) -> Taxonomy:
    """Load the taxonomy file, checking the fields the menu reads (the prototype validated them
    through a pydantic model for the same reason: a taxonomy schema change must break here, not
    reach a corpus run as an empty menu line). Raises ``ValueError`` naming the bad entry."""
    raw = yamlio.load(path.read_text("utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("techniques"), list) or "version" not in raw:
        raise ValueError(f"{path}: not a taxonomy (no `version` / `techniques` list)")
    out: list[TechniqueEntry] = []
    seen: set[str] = set()
    for i, t in enumerate(raw["techniques"]):
        if not isinstance(t, dict):
            raise ValueError(f"{path}: techniques[{i}] is not a mapping")
        missing = [k for k in ("slug", "name", "type", "family", "description")
                   if not isinstance(t.get(k), str) or not t[k].strip()]
        if missing:
            raise ValueError(f"{path}: techniques[{i}] ({t.get('slug')!r}) lacks {missing}")
        aliases = t.get("aliases") or []
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise ValueError(f"{path}: {t['slug']}: `aliases` must be a list of strings")
        variant_of = t.get("variant_of")
        if variant_of is not None and not isinstance(variant_of, str):
            raise ValueError(f"{path}: {t['slug']}: `variant_of` must be a string")
        if t["slug"] in seen:
            raise ValueError(f"{path}: duplicate slug {t['slug']!r}")
        seen.add(t["slug"])
        out.append(TechniqueEntry(slug=t["slug"], name=t["name"], type=t["type"], family=t["family"],
                                  description=t["description"], aliases=tuple(aliases), variant_of=variant_of))
    return Taxonomy(version=str(raw["version"]), techniques=tuple(out))


def by_family(tax: Taxonomy) -> dict[str, list[TechniqueEntry]]:
    """Techniques grouped by family, families in :data:`FAMILIES` order, members by slug.

    Deterministic (two renders of one taxonomy are byte-identical: the prefix is cacheable). A
    family missing from ``FAMILIES`` is not dropped: it lands at the end under its bare slug."""
    groups: dict[str, list[TechniqueEntry]] = {fam: [] for fam in FAMILIES}
    for entry in tax.techniques:
        groups.setdefault(entry.family, []).append(entry)
    return {fam: sorted(members, key=lambda e: e.slug) for fam, members in groups.items() if members}


def family_header(family: str) -> str:
    name, gloss = FAMILIES.get(family, (family, ""))
    return f"## {family} — {name}" + (f": {gloss}" if gloss else "")


def entry_line(entry: TechniqueEntry) -> str:
    """One menu row: ``- <slug> — <name> (aka: <aliases>): <description>`` (aliases in the
    taxonomy's own order; not exhaustive, which rubric §5 says)."""
    aka = f" (aka: {', '.join(entry.aliases)})" if entry.aliases else ""
    return f"- {entry.slug} — {entry.name}{aka}: {entry.description}"


def full_menu(tax: Taxonomy) -> str:
    """Every technique once, grouped by family (v0.8: 102,442 characters, measured 2026-10-17; roughly 25k
    tokens at 4 characters a token)."""
    return "\n\n".join("\n".join([family_header(fam), *(entry_line(e) for e in members)])
                       for fam, members in by_family(tax).items())


# ── the document window ──────────────────────────────────────────────────────────────────────


def doc_window(text: str, doc_char_cap: int = DOC_CHAR_CAP) -> tuple[str, bool]:
    """``(head, truncated)``: the document as the judge sees it. No menu row can be beyond the
    window (the menu is the taxonomy), so this is all the windowing the stage needs; a technique
    named only past the cap is invisible to the judge (the prototype's open calibration question)."""
    return text[:doc_char_cap], len(text) > doc_char_cap


def menu_slugs(tax: Taxonomy) -> list[str]:
    """Every slug in taxonomy order (the payload's row order)."""
    return [e.slug for e in tax.techniques]

