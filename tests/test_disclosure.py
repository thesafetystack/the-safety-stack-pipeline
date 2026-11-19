"""The public variant's disclosure rules (stack/fold/disclosure.py, D808): the per-document quotation budget (floor,
budget, ceiling, order, duplicates, omission), the withheld abstract's covered share (the review's two
counterexamples among the cases) and the email redaction and its independent check."""

from __future__ import annotations

from typing import Any

import pytest

from stack.fold import disclosure as dz
from stack.fold.dataset import cut_string

POL = dz.QuotePolicy(version=2, floor_cut=150, budget_chars=2000, budget_share=0.15, ceiling_chars=6000,
                     ceiling_share=0.25, abstract_share=0.30, shingle_words=4)
SCREEN = "annotations.screen.payload.quotes[].text"
TECH = "annotations.safety_techniques.payload.techniques[].quotes[].text"
VARIANT = "annotations.model_roles.payload.variant_models[].quote"
PATHS = [SCREEN, TECH, VARIANT]


def words(tag: str, n: int) -> str:
    """``n`` distinct words (no 4-gram shared with another tag's)."""
    return " ".join(f"{tag}{i}" for i in range(n))


def ann(screen: list[str] | None = None, techniques: list[tuple[int, list[str]]] | None = None,
        variants: list[str] | None = None) -> dict[str, Any]:
    return {"screen": {"payload": {"quotes": [{"text": t, "offset": 0} for t in screen or []]}},
            "safety_techniques": {"payload": {"techniques": [
                {"slug": f"t{i}", "roles": {"used": s, "mentioned": 1}, "quotes": [{"text": q} for q in qs]}
                for i, (s, qs) in enumerate(techniques or [])]}},
            "model_roles": {"payload": {"variant_models": [{"roles": {"mentioned": 2}, "quote": q}
                                                           for q in variants or []]}}}


def alloc(a: dict[str, Any], length: int | None = 100_000, abstract: str | None = None,
          pol: dz.QuotePolicy = POL) -> dz.Allocation:
    return dz.allocate(a, PATHS, [VARIANT], pol, 300, length, abstract, cut_string)


def texts(a: dict[str, Any]) -> tuple[list[str], list[list[str]], list[str]]:
    return ([q["text"] for q in a["screen"]["payload"]["quotes"]],
            [[q["text"] for q in t["quotes"]] for t in a["safety_techniques"]["payload"]["techniques"]],
            [v["quote"] for v in a["model_roles"]["payload"]["variant_models"]])


def test_limits_and_the_policy_record() -> None:
    assert POL.limits(100_000) == (2000, 6000) and POL.limits(4000) == (600, 1000)
    rec = POL.record()
    assert rec["version"] == 2 and rec["budget"] == {"chars": 2000, "share": 0.15}
    with pytest.raises(ValueError, match="quote_policy needs exactly"):
        dz.QuotePolicy.parse({**rec})  # length, normalisation are the record's, not the mapping's


def test_floor_is_cut_to_150_then_upgraded_and_the_rest_fill_the_budget_round_robin() -> None:
    long1, long2 = words("a", 60), words("b", 60)  # ~ 230 characters each
    a = ann(screen=[long1, words("c", 10)], techniques=[(2, [long2, words("d", 10)])])
    al = alloc(a)
    sc, (tq,), _v = texts(a)
    # pass 1 took each annotation's shortest quote, cut to 150 (here whole); pass 2 added the long ones at the cap
    assert sc == [long1, words("c", 10)] and tq == [long2, words("d", 10)]
    assert al.floor_cost == 2 * len(words("c", 10)) and al.cost == sum(map(len, sc + tq))
    assert al.kept == al.quotes == 4 and al.cut == 0 and not al.lost


def test_a_floor_quote_is_upgraded_for_its_extra_cost_only_when_the_budget_allows() -> None:
    long = words("a", 60)
    floor, whole = cut_string(long, 150), long
    for budget, want in ((len(whole), whole), (len(whole) - 1, floor)):
        # budget = the whole quote's length: the upgrade fits only when charged the difference
        a = ann(techniques=[(2, [long])])
        al = alloc(a, length=10_000, pol=dz.QuotePolicy(2, 150, budget, 1.0, 6000, 1.0, 0.3, 4))
        assert texts(a)[1] == [[want]] and al.cost == len(dz.norm(want)) and al.floor_cost == len(dz.norm(floor))


def test_no_upgrade_when_another_text_ships_the_same_floor_string() -> None:
    """Two texts whose 150-cuts coincide share one floor string: upgrading one would leave the other's uncharged."""
    head = words("h", 40)
    a1, a2 = head + " " + words("x", 20), head + " " + words("y", 20)
    floor = cut_string(a1, 150)
    assert cut_string(a2, 150) == floor
    a = ann(screen=[a1], techniques=[(2, [a2])])
    al = alloc(a)
    assert texts(a)[0] == [floor] and texts(a)[1] == [[floor]] and al.cost == len(dz.norm(floor))


def test_pass_1_takes_the_shortest_quote_ties_in_order() -> None:
    pol = dz.QuotePolicy(2, 150, 10, 1.0, 6000, 1.0, 0.3, 4)  # a budget too small for anything after pass 1
    a = ann(techniques=[(2, [words("l", 60), words("s", 5), words("t", 5)])])
    alloc(a, length=10_000, pol=pol)
    assert texts(a)[1] == [[words("s", 5)]]


def test_budget_ceiling_and_no_length() -> None:
    # L = 2,000: budget 300, ceiling 500; three annotations of one 150-char floor quote each fit the ceiling only
    a = ann(techniques=[(4, [words("a", 40)]), (3, [words("b", 40)]), (2, [words("c", 40)]), (1, [words("d", 40)])],
            screen=[words("e", 5)])
    al = alloc(a, length=2000)
    _s, tq, _v = texts(a)
    assert al.ceiling == 500 and al.cost <= 500
    assert [t[0] for t in tq[:3]] == [cut_string(words(x, 40), 150) for x in "abc"]
    assert tq[3] == [] and al.lost == {"safety_techniques.techniques": 1}  # the weakest annotation lost its quote
    for length in (None, 0):
        b = ann(screen=[words("a", 3)], techniques=[(2, [words("b", 3)])], variants=[words("v", 3)])
        al = alloc(b, length=length)
        assert texts(b) == ([], [[]], [""]) and al.kept == 0  # no length: no quotes; a scalar quote ships ""
        assert al.lost == {"screen": 1, "safety_techniques.techniques": 1, "model_roles.variant_models": 1}


def test_order_records_first_then_strength_then_row_order() -> None:
    # room for exactly two floor quotes (ceiling 2*150 + a little): the screen record and the strongest technique
    pol = dz.QuotePolicy(2, 150, 10, 1.0, 320, 1.0, 0.3, 4)
    a = ann(techniques=[(1, [words("w", 40)]), (4, [words("s", 40)])], screen=[words("r", 40)])
    al = alloc(a, length=10_000, pol=pol)
    sc, tq, _v = texts(a)
    assert sc and tq[1] and tq[0] == [] and al.lost == {"safety_techniques.techniques": 1}


def test_floor_continues_past_a_rejected_quote_and_cuts_index_the_output() -> None:
    abstract = words("x", 50)
    a = ann(techniques=[(2, [words("k", 60), abstract])])
    # L = 1,200: ceiling 300, budget 180, too small for the upgrade; the shorter quote is the abstract: refused
    alloc(a, length=1200, abstract=abstract)
    _s, (tq,), _v = texts(a)
    assert tq == [cut_string(words("k", 60), 150)]  # pass 1's cut; the kept quote is at index 0


def test_a_duplicate_costs_nothing_and_ships_the_final_retained_string() -> None:
    long = words("a", 60)
    for length, ship in ((100_000, long), (1100, cut_string(long, 150))):  # upgraded, or the budget binds
        a = ann(screen=[long], techniques=[(2, [words("t", 5), long + "  "]), (2, [" " + long])])
        al = alloc(a, length=length)
        sc, tq, _v = texts(a)
        assert sc == [ship] and tq[1] == [ship] and ship in tq[0], (length, tq)
        assert al.cost == len(dz.norm(ship)) + (len(words("t", 5)) if words("t", 5) in tq[0] else 0)


def test_scalar_quote_is_emptied_not_removed() -> None:
    pol = dz.QuotePolicy(2, 150, 10, 1.0, 20, 1.0, 0.3, 4)
    a = ann(variants=[words("v", 5), words("w", 5)])  # 14 characters each: one fits the ceiling
    alloc(a, length=1000, pol=pol)
    assert texts(a)[2] == [words("v", 5), ""]
    assert len(a["model_roles"]["payload"]["variant_models"]) == 2  # the label stays


def test_abstract_cover_joined_excerpts_counterexample() -> None:
    """Two disjoint 60-character abstract excerpts joined by '...' disclose 40% of a 300-character abstract: refused
    (a longest-common-substring measure charged only 60)."""
    ab = words("ab", 30)
    toks = ab.split()
    a1, a2 = " ".join(toks[0:7]), " ".join(toks[15:22])
    cover = dz.AbstractCover(ab, 4)
    share = cover.share(cover.adding(f"{a1} ... {a2}"))
    assert share == pytest.approx(sum(map(len, toks[0:7] + toks[15:22])) / sum(map(len, toks)))
    assert share > 0.30
    a = ann(screen=[f"{a1} ... {a2}"])
    alloc(a, abstract=ab)
    assert texts(a)[0] == []


def test_abstract_cover_many_short_quotes_counterexample() -> None:
    """22 disjoint short quotes of a 1,000-character abstract: the union is charged, so they stop at 30%."""
    toks = [f"w{i:03d}" for i in range(200)]  # 4 + 1 characters per word, 1,000 with spaces
    ab = " ".join(toks)
    quotes = [" ".join(toks[i:i + 8]) for i in range(0, 176, 8)]  # 22 quotes of 8 words, 39 characters each
    assert len(quotes) == 22 and {len(q) for q in quotes} == {39}
    a = ann(techniques=[(2, quotes)])
    al = alloc(a, abstract=ab)
    kept = texts(a)[1][0]
    assert al.abstract_share is not None and al.abstract_share <= 0.30
    assert 0 < len(kept) < 22 and len(kept) == int(0.30 * 200 // 8)


def test_abstract_cover_is_case_and_space_insensitive_and_ignores_short_quotes() -> None:
    cover = dz.AbstractCover("The  Quick brown fox jumps over", 4)
    assert cover.share(cover.adding("the QUICK\nbrown   fox")) == pytest.approx(16 / 25)
    assert cover.share(cover.adding("quick brown fox")) == 0.0  # fewer than 4 words: not measured


@pytest.mark.parametrize("s,want", [
    ("write to john.o'neil+lists@uni-bonn.de.", "write to [email]."),
    ("Jürgen.Müller@universität.de", "[email]"),
    ("{alice, bob.c}@lab.example.org wrote", "[email] wrote"),
    ("a@b.co.uk and c@d.com", "[email] and [email]"),
    ("(李@例子.中国)", "([email])"),
])
def test_redact_emails(s: str, want: str) -> None:
    assert dz.redact_emails(s)[0] == want


@pytest.mark.parametrize("s", [
    "fetchpdf-md@0.1.0+tierlatexml", "hf://datasets/thesafetystack/the-safety-stack@release-2026-11-03/data/x.parquet",
    "https://medium.com/@handle/post", "doi:10.1002/(SICI)1097-4571(199806)49:8<693::AID-ASI4>3.0.CO;2-O",
    "github.com/org/repo@main", "pkg@v1", "@user said", "a@b.c", "x/john@a.org",
])
def test_redact_emails_leaves_non_addresses(s: str) -> None:
    assert dz.redact_emails(s) == (s, 0)


@pytest.mark.parametrize("url,want", [
    ("mailto:a@b.org", True), ("MAILTO:a@b.org", True), ("https://x.org/people/a@b.org", True),
    ("https://x.org/?u=a%40b.org", True), ("https://x.org/p#to=a@b.org", True), ("https://a%40b.org@x.org/", True),
    ("https://medium.com/@h/p", False), ("https://user:pw@x.org/", False), ("https://github.com/x/y@main", False),
    ("https://example.org/landing", False),
])
def test_url_has_email(url: str, want: bool) -> None:
    assert dz.url_has_email(url) is want


def test_email_hits_is_broader_and_allows_only_its_list() -> None:
    assert dz.email_hits("x/john@a.org", contact_allowed=False) == ["john@a.org"]  # the redaction leaves this one
    assert dz.email_hits("pkg@v1.x", contact_allowed=False) == ["pkg@v1.x"]
    # version pins: a prompt pinned to a taxonomy version (config names in release.json, FORMAT.md), a tool stamp
    pins = "model-roles-v3b@models-1.1.0 tool@0.1.0 pkg@v1.2 hf://d/x@r1.2/a"
    assert dz.email_hits(pins, contact_allowed=False) == []
    assert dz.email_hits(f"write to {dz.CONTACT_ADDRESS}", contact_allowed=True) == []
    assert dz.email_hits(f"write to {dz.CONTACT_ADDRESS}", contact_allowed=False) == [dz.CONTACT_ADDRESS]
    assert dz.email_hits("[email]", contact_allowed=False) == []


def test_string_leaves_paths() -> None:
    obj = {"a": "x", "b": [{"c": "y"}, {"c": None}], "d": {"e": ["z"]}}
    got = [(p, v) for p, _h, _k, v in dz.string_leaves(obj, "r")]
    assert got == [("r.a", "x"), ("r.b[].c", "y"), ("r.d.e[]", "z")]
