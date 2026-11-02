"""``stack.canon`` — the config-pure canonicalizers (10-design §9.3).

Three groups of assertions:

* **Protocol shape** — ``canon_org``/``canon_model`` return the §9.3 ``CanonResult``, unmatched
  is a first-class answer, and a bad regex fails loud at construction.
* **Ordering is precedence** — the two entry pairs where a less specific org would otherwise
  swallow a more specific one. These are the rules ``configs/registry.yaml``'s header promises;
  a reordering of the file that breaks them must fail here, not in a corpus six months later.
* **The negative fixture** — surfaces that must resolve to NOTHING. Every case is a real
  near-miss recorded in v1's hand-tuned ``prompts/affiliations.txt`` after it bit
  (`pipeline/config/prompts/affiliations.txt`), plus the whole-word traps its ``orgs.yaml``
  comments call out. This is the part that stops "add another alias" from quietly costing
  precision — the rules used to live as untestable prose in an LLM prompt.

No DB and no network: `Canon` is a pure function of the pinned config.
"""

from __future__ import annotations

from typing import Any

import pytest

from stack.canon import Canon, CanonError, CanonResult, email_domain
from stack.config import OrgEntry, RegistryConfig


@pytest.fixture()
def canon(config: Any) -> Canon:
    """The canonicalizer over the SHIPPED configs/registry.yaml — these tests are about the
    real roster, not a toy one."""
    return Canon.from_config(config)


# ─────────────────────────── protocol shape (§9.3) ──────────────────────────


def test_canon_org_returns_slug_and_firing_rule(canon: Canon) -> None:
    got = canon.canon_org("Anthropic, San Francisco")
    assert got.matched and got.slug == "anthropic"
    assert got.rule is not None and "anthropic" in got.rule


def test_unmatched_is_a_first_class_answer(canon: Canon) -> None:
    """§9.3: no match is a recorded outcome, never an exception. The caller decides what to do
    with it (here: the aggregate counts it toward a registry proposal)."""
    got = canon.canon_org("Department of Basket Weaving, Somewhere State")
    assert got == CanonResult(slug=None, matched=False, rule=None)


def test_empty_surface_is_unmatched_not_an_error(canon: Canon) -> None:
    assert canon.canon_org("").matched is False
    assert canon.orgs_in("") == []


def test_canon_model_shares_the_walk(canon: Canon) -> None:
    got = canon.canon_model("we evaluated claude-4-opus on this")
    assert got.matched and got.slug == "claude-4-opus"


def test_bad_regex_fails_loud_at_construction() -> None:
    """Fail early (30-stack-module §3), not mid-sweep on the one document that hits the rule."""
    registry = RegistryConfig(
        version="9.9.9",
        taxonomy={"active": "0.6", "file": "taxonomy-v0.6.yaml"},
        models=[],
        orgs=[OrgEntry(slug="broken", name="Broken", kind="lab", aliases=["(unclosed"])],
    )
    with pytest.raises(CanonError, match="not a valid regex"):
        Canon(registry)


# ─────────────────────── ordering IS precedence (§9.3) ──────────────────────


def test_microsoft_research_outranks_bare_microsoft(canon: Canon) -> None:
    """v1's prompt rule "NEVER upgrade a bare parent company to a specific research division",
    enforced by entry ORDER instead of prose. The bare `microsoft` alias (registry 1.2.0, 29 hits
    in the eval) makes this ordering load-bearing rather than theoretical."""
    assert canon.canon_org("Microsoft Research, Cambridge").slug == "microsoft-research"
    assert canon.canon_org("Microsoft Corporation").slug == "microsoft"
    assert canon.canon_org("Microsoft").slug == "microsoft"
    assert canon.canon_org("Microsoft Redmond, WA").slug == "microsoft"


@pytest.mark.parametrize(
    "surface,expected",
    [
        ("Meta FAIR", "meta-ai"),
        ("Scale AI, San Francisco", "scale-ai"),
        ("Scale.AI", "scale-ai"),
        ("Shanghai AI Laboratory", "shanghai-ai-lab"),
        ("Mila / McGill University", "mila"),
        ("Vector Institute, Toronto", "vector"),
        ("LawZero", "lawzero"),
    ],
)
def test_registry_1_2_0_additions(canon: Canon, surface: str, expected: str) -> None:
    """Each was among the most frequent surfaces matching NOTHING in the 200-paper eval."""
    assert canon.canon_org(surface).slug == expected


def test_us_aisi_outranks_uk_aisi(canon: Canon) -> None:
    """"US AI Safety Institute" CONTAINS "AI Safety Institute". v1's orgs.yaml listed UK first
    and therefore mislabelled every US AISI affiliation; the ported order fixes it."""
    assert canon.canon_org("US AI Safety Institute").slug == "us-aisi"
    assert canon.canon_org("U.S. AI Safety Institute").slug == "us-aisi"
    assert canon.canon_org("UK AI Safety Institute").slug == "uk-aisi"
    assert canon.canon_org("AI Security Institute, London").slug == "uk-aisi"


def test_orgs_in_does_not_double_count_an_overlapping_alias(canon: Canon) -> None:
    """The scan must not report both `microsoft-research` and `microsoft` for one span."""
    assert [r.slug for r in canon.orgs_in("Microsoft Research")] == ["microsoft-research"]
    assert [r.slug for r in canon.orgs_in("US AI Safety Institute")] == ["us-aisi"]


# ─────────────────── whitespace / unicode normalization ─────────────────────
#
# PDF-derived surfaces carry NBSPs, doubled spaces, and line breaks. Before normalization these
# fell THROUGH the specific alias onto a less specific one — "Microsoft Research" resolved
# to bare `microsoft`, a silently WRONG answer rather than a miss — or matched nothing at all
# ("Mistral AI"). The wrong-parent case is the dangerous one, hence the exact-list asserts.


@pytest.mark.parametrize(
    "surface",
    [
        "Microsoft  Research",     # doubled space
        "Microsoft Research", # NBSP
        "Microsoft\nResearch",     # line break from PDF text extraction
    ],
    ids=["double-space", "nbsp", "newline"],
)
def test_messy_whitespace_still_hits_the_specific_entry(canon: Canon, surface: str) -> None:
    assert [r.slug for r in canon.orgs_in(surface)] == ["microsoft-research"]
    assert canon.canon_org(surface).slug == "microsoft-research"


def test_nbsp_does_not_hide_an_org_entirely(canon: Canon) -> None:
    assert [r.slug for r in canon.orgs_in("Mistral AI")] == ["mistral"]


# ────────────────────────── multi-org affiliation ───────────────────────────


def test_dual_affiliation_yields_both_orgs(canon: Canon) -> None:
    """The case a scalar canon_org would silently halve — and the reason `orgs_in` exists."""
    slugs = {r.slug for r in canon.orgs_in("MIT ; Anthropic")}
    assert slugs == {"anthropic"}  # MIT is deliberately untracked (universities are excluded)

    slugs = {r.slug for r in canon.orgs_in("Google DeepMind and Redwood Research")}
    assert slugs == {"deepmind", "redwood"}


def test_orgs_in_preserves_first_occurrence_order(canon: Canon) -> None:
    assert [r.slug for r in canon.orgs_in("Anthropic, formerly OpenAI")] == [
        "anthropic",
        "openai",
    ]


def test_canon_org_is_singular_where_orgs_in_is_plural(canon: Canon) -> None:
    """The two surfaces answer different questions; this pins that they stay different."""
    raw = "Google DeepMind and Redwood Research"
    assert canon.canon_org(raw).slug == "deepmind"          # "which ONE entity?"
    assert len(canon.orgs_in(raw)) == 2                     # "which orgs are MENTIONED?"


# ───────────────────── the negative fixture (must NOT match) ────────────────
#
# Every row is a real near-miss. The first four are quoted almost verbatim from v1's
# prompts/affiliations.txt, which lists them as hard rules after they caused misattribution;
# the rest are the whole-word traps orgs.yaml's comments call out.

_MUST_NOT_MATCH = [
    # Registry 1.2.0 additions brought two new near-misses with them; both were found in the
    # live corpus BEFORE the alias shipped, and both are guarded by a lookbehind.
    ("Centre Universitaire de Mila", "an Algerian university, not Quebec's Mila"),
    ("large-scale AI systems for alignment", "'-scale AI' must not hit Scale AI"),
    ("UK Advanced Research + Invention Agency (ARIA)", "ARIA is not the UK AI Safety Institute"),
    ("Allen Institute for AI (AI2)", "not a tracked org"),
    ("MATS / ML Alignment & Theory Scholars", "not a tracked org"),
    ("Massachusetts Institute of Technology", "universities are deliberately excluded"),
    ("we report the metric across parametric models", "`metr` must be whole-word"),
    ("chain-of-thought coherence analysis", "`cohere` must be whole-word"),
    ("FAIR at the University of Somewhere", "`far ai` must not hit FAIR"),
    ("explainable AI (XAI) methods", "bare `xai` is omitted for exactly this collision"),
    ("a meta-analysis of meta-learning", "bare `meta` is omitted"),
    ("our moonshot project", "bare `moonshot` is omitted"),
]


@pytest.mark.parametrize("surface,why", _MUST_NOT_MATCH, ids=[s for s, _ in _MUST_NOT_MATCH])
def test_negative_fixture(canon: Canon, surface: str, why: str) -> None:
    assert canon.canon_org(surface).matched is False, why
    assert canon.orgs_in(surface) == [], why


# ───────────────────────── email-domain corroboration ───────────────────────


def test_domain_tier_resolves_and_accepts_subdomains(canon: Canon) -> None:
    assert canon.org_for_domain("anthropic.com").slug == "anthropic"
    assert canon.org_for_domain("research.anthropic.com").slug == "anthropic"
    assert canon.org_for_domain("@metr.org").slug == "metr"


def test_domain_tier_stays_narrow(canon: Canon) -> None:
    """Broad shared domains are deliberately absent from registry.yaml so this tier can only
    corroborate, never misattribute (v1's rule, preserved)."""
    assert canon.org_for_domain("google.com").matched is False
    assert canon.org_for_domain("mit.edu").matched is False
    assert canon.org_for_domain("").matched is False


def test_domain_tier_does_not_match_a_bare_suffix_collision(canon: Canon) -> None:
    """'notanthropic.com' ends with 'anthropic.com' as a STRING but is a different domain."""
    assert canon.org_for_domain("notanthropic.com").matched is False


def test_email_domain_helper() -> None:
    assert email_domain("alice@anthropic.com") == "anthropic.com"
    assert email_domain("Alice@Anthropic.Com ") == "anthropic.com"
    assert email_domain("not-an-email") == ""


# ──────────────────────────────── misc surface ──────────────────────────────


def test_org_kind_and_menu(canon: Canon) -> None:
    assert canon.org_kind("anthropic") == "lab"
    assert canon.org_kind("metr") == "nonprofit"
    assert canon.org_kind("nope") is None
    slugs = canon.org_slugs
    assert slugs[0] == "anthropic"  # file order, not sorted
    assert "microsoft-research" in slugs and len(slugs) == 37  # registry 1.2.0
