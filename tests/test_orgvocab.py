"""``stack.orgvocab`` — the organisation vocabulary's matcher (0.x).

Two groups:

* **Mechanics on a toy vocabulary**: segmentation, longest-span precedence, pooled candidates, the parent and
  country tie-breaks, ancestor collapse, cased aliases, patterns on token boundaries, e-mail domains, non-org
  surfaces, and the lint refusals that keep the matcher from being silently wrong.
* **The shipped vocabulary** (``taxonomy/orgs-0.2.2.yaml``, the catalogue envelope; D737, D760, D778): it validates against its
  JSON Schema, loads lint-clean, keeps the registry's, the channels' and the models catalogue's slugs, and answers
  the surfaces the bootstrap was checked on, including the near-misses ``tests/test_canon.py`` records for the
  registry (which must not move to a wrong organisation here either), the 0.2.0 units and the FAIR rule.
* **0.2.0 matcher rules on the toy**: unit words (``generic`` only for a unit without its organisation), US regions
  (a state or a postal code in an address position states the country), the longest country name, local names
  next to a sibling unit; 0.1.0 still loads (legacy shape) and resolves as before.
* **Matcher 3 on the toy** (D760): a place-name segment after an organisation's name is its address, not another
  organisation; the Latin letters NFKD leaves alone (``ß``, ``ø``, ``ł``, ...) are spelled out.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from stack.orgvocab import (
    DEFAULT_PATH,
    OrgVocabError,
    affiliation_orgs,
    load,
    matcher_from_entries,
    normalise,
    parse,
    tokens,
)

META: dict[str, Any] = {
    "countries": {"CN": ["China", "PR China"], "US": ["USA", "United States"]},
    "generic_words": ["institute", "of", "university", "ai", "lab", "department", "computer", "science", "and",
                      "school"],
    "non_org": [r"^independent( researcher)?$"],
}
TOY = [
    {"id": "stanford", "name": "Stanford University", "type": "university", "country": "US",
     "aliases": ["Stanford University"], "domains": ["stanford.edu"]},
    {"id": "google", "name": "Google", "type": "company", "country": "US", "aliases": ["Google"]},
    {"id": "deepmind", "name": "Google DeepMind", "type": "company", "parent": "google",
     "aliases": ["Google DeepMind", "DeepMind"]},
    {"id": "neu-us", "name": "Northeastern University", "type": "university", "country": "US",
     "aliases": ["Northeastern University"], "cased": ["NEU"]},
    {"id": "neu-cn", "name": "Northeastern University", "type": "university", "country": "CN",
     "aliases": ["Northeastern University"], "cased": ["NEU"]},
    {"id": "cas", "name": "Chinese Academy of Sciences", "type": "institute", "country": "CN",
     "aliases": ["Chinese Academy of Sciences"], "cased": ["CAS"]},
    {"id": "casia", "name": "Institute of Automation", "type": "institute", "country": "CN", "parent": "cas",
     "aliases": ["Institute of Automation Chinese Academy of Sciences"], "cased": ["CASIA"]},
    {"id": "auto-x", "name": "Institute of Automation X", "type": "institute", "country": "DE",
     "aliases": ["Automation Institute Foo"]},
    {"id": "auto-y", "name": "Automation Institute Foo", "type": "institute", "country": "CN", "parent": "cas",
     "aliases": ["Automation Institute Foo"]},
    {"id": "ict", "name": "Institute of Computing Technology", "type": "institute", "country": "CN", "parent": "cas",
     "local_names": ["Institute of Computing Technology"]},
    {"id": "ict-x", "name": "Institute of Computing Technology", "type": "institute", "country": "DE",
     "parent": "stanford", "local_names": ["Institute of Computing Technology"]},
    {"id": "kaggle", "name": "Kaggle", "type": "company", "segment_aliases": ["Kaggle"]},
    {"id": "scale-ai", "name": "Scale AI", "type": "company", "cased": ["Scale AI"]},
    {"id": "mila", "name": "Mila", "type": "institute", "patterns": [r"(?<!\bde )\bmila\b"]},
    {"id": "ucb", "name": "University of California, Berkeley", "type": "university", "country": "US",
     "aliases": ["University of California, Berkeley"]},
    {"id": "uc", "name": "University of California", "type": "university", "country": "US",
     "aliases": ["University of California"]},
]


@pytest.fixture(scope="module")
def toy() -> Any:
    return matcher_from_entries(TOY, META)


@pytest.mark.parametrize("surface,status,orgs", [
    ("Department of Computer Science, Stanford University, Stanford, CA, USA", "matched", ("stanford",)),
    ("Google DeepMind", "matched", ("deepmind",)),                      # not also google: one span
    ("Google; Stanford University", "matched", ("google", "stanford")),
    ("Stanford University and Google DeepMind", "matched", ("stanford", "deepmind")),
    ("Stanford University (NEU)", "matched", ("stanford",)),             # "(ACR)" abbreviates what precedes it
    ("NEU, Shenyang, China", "matched", ("neu-cn",)),
    ("Google DeepMind, Google", "matched", ("deepmind",)),              # the ancestor is implied
    ("University of California, Berkeley", "matched", ("ucb",)),       # the comma is not a boundary
    ("University of California; Berkeley", "matched", ("uc",)),        # the semicolon is
    ("Northeastern University", "ambiguous", ()),
    ("Northeastern University, Shenyang, China", "matched", ("neu-cn",)),
    ("Northeastern University, Boston, MA, USA", "matched", ("neu-us",)),
    ("Northeastern University, Stanford University", "matched", ("neu-us", "stanford")),  # a resolved neighbour's country
    ("Northeastern University, Stanford University, China", "matched", ("neu-cn", "stanford")),  # a stated one wins
    ("Institute of Automation, Chinese Academy of Sciences", "matched", ("casia",)),
    ("Automation Institute Foo, CAS", "matched", ("auto-y",)),         # the parent tie-break
    ("CASIA", "matched", ("casia",)),
    ("Scale AI", "matched", ("scale-ai",)),
    ("large-scale AI lab", "unmatched", ()),                            # cased: 'scale AI' is not 'Scale AI'
    ("Mila", "matched", ("mila",)),
    ("Centre Universitaire de Mila", "unmatched", ()),
    ("Independent Researcher", "non_org", ()),
    ("Department of Computer Science", "generic", ()),
    ("Kaggle", "matched", ("kaggle",)),
    ("Kaggle, Google", "matched", ("google", "kaggle")),
    ("Kaggle Grandmasters Team", "unmatched", ()),                      # a segment alias names only a whole segment
    ("Institute of Computing Technology, Chinese Academy of Sciences", "matched", ("ict",)),
    ("Institute of Computing Technology", "generic", ()),
    ("Foo Bar Inc", "unmatched", ()),
    ("alice@cs.stanford.edu", "matched", ("stanford",)),                # domain suffix
    ("alice@notstanford.edu", "unmatched", ()),
    ("Stanford  University", "matched", ("stanford",)),            # NBSP, double space
    ("The Stanford University", "matched", ("stanford",)),
])
def test_toy_surfaces(toy: Any, surface: str, status: str, orgs: tuple[str, ...]) -> None:
    got = toy.match(surface)
    assert (got.status, got.orgs) == (status, orgs), got


def test_ambiguous_keeps_candidates_and_resolved_ones(toy: Any) -> None:
    got = toy.match("Northeastern University; Google DeepMind")   # deepmind has no country here
    assert got.status == "ambiguous"
    assert got.orgs == ("deepmind",)
    assert got.ambiguous == (("neu-cn", "neu-us"),)


def test_match_author_resolves_a_local_name_by_a_sibling(toy: Any) -> None:
    got = toy.match_author(["Institute of Computing Technology", "Chinese Academy of Sciences"])
    assert [g.orgs for g in got] == [("ict",), ("cas",)]
    assert got[0].hits[0].resolved_by == "sibling"
    got = toy.match_author(["Institute of Computing Technology", "Google"])   # no ancestor among the siblings
    assert [g.status for g in got] == ["generic", "matched"]
    got = toy.match_author(["Institute of Computing Technology"])
    assert got[0].status == "generic"


def test_a_segment_alias_inside_a_wider_hit_does_not_fire() -> None:
    m = matcher_from_entries([{"id": "fair-x", "name": "FAIR X", "type": "institute", "segment_aliases": ["FAIR"]},
                              {"id": "meta-ai", "name": "Meta AI", "type": "company", "aliases": ["FAIR Meta"]}], META)
    assert m.match("FAIR, Meta").orgs == ("meta-ai",)
    assert m.match("FAIR").orgs == ("fair-x",)


def test_normalise() -> None:
    assert normalise("King’s College London") == "kings college london"
    assert normalise("Université de Montréal") == "universite de montreal"
    assert normalise("Texas A&M University") == "texas a and m university"
    assert normalise("The University of Tokyo") == "university of tokyo"
    # matcher 3 (D760): spelled out, not token breaks ("gie en", "k benhavns")
    assert normalise("Justus-Liebig-Universität Gießen") == "justus liebig universitat giessen"
    assert normalise("Københavns Universitet") == "kobenhavns universitet"
    assert normalise("Uniwersytet Łódzki, Wrocław") == "uniwersytet lodzki wroclaw"
    assert normalise("Mærsk Mc-Kinney Møller Instituttet") == "maersk mc kinney moller instituttet"
    # matcher 4: Han and kana one token each, Hangul runs kept, fullwidth folded (NFKC)
    assert normalise("清华大学（深圳）") == "清 华 大 学 深 圳"
    assert normalise("서울대학교 AI연구원") == "서울대학교 ai연구원"
    assert tokens("東京大学Ｃ") == ["東", "京", "大", "学", "C"]


@pytest.mark.parametrize("entry,problem", [
    ({"id": "x", "name": "X", "type": "company", "aliases": ["Department of Computer Science"]}, "generic"),
    ({"id": "x", "name": "X", "type": "company", "aliases": ["China"]}, "country"),
    ({"id": "x", "name": "X", "type": "company", "parent": "nope"}, "parent"),
    ({"id": "x", "name": "X", "type": "company", "patterns": ["(unclosed"]}, "compile"),
    ({"id": "x", "name": "X", "type": "company", "cased": ["A-B"]}, "cased"),
    ({"id": "x", "name": "X", "type": "company", "cased": ["USA"]}, "country"),
    ({"id": "x", "name": "X", "type": "company", "segment_aliases": ["Institute"]}, "generic"),
    ({"id": "x", "name": "X", "type": "company", "local_names": ["Institute of X"]}, "without a parent"),
])
def test_lint_refuses(entry: dict[str, Any], problem: str) -> None:
    with pytest.raises(OrgVocabError, match=problem):
        matcher_from_entries([entry], META)


def test_pattern_hits_only_on_token_boundaries() -> None:
    m = matcher_from_entries([{"id": "x", "name": "X", "type": "company", "patterns": [r"mila"]}], META)
    assert m.match("Mila").orgs == ("x",)
    assert m.match("Camila Labs").status == "unmatched"


def test_unlinted_matcher_still_drops_generic_and_country_aliases() -> None:
    """The bootstrap builds an unlinted matcher over all of ROR: what lint would refuse must not match there."""
    m = matcher_from_entries([{"id": "x", "name": "X", "type": "company",
                               "aliases": ["Department of Computer Science", "China", "Foo Labs"]}], META, check=False)
    assert m.match("Department of Computer Science").status == "generic"
    assert m.match("China").orgs == ()
    assert m.match("Foo Labs").orgs == ("x",)


def test_lint_refuses_a_parent_cycle() -> None:
    with pytest.raises(OrgVocabError, match="cycle"):
        matcher_from_entries([{"id": "a", "name": "A", "type": "company", "parent": "b"},
                              {"id": "b", "name": "B", "type": "company", "parent": "a"}], META)


def test_unknown_field_is_refused() -> None:
    with pytest.raises(OrgVocabError):
        parse(yaml.safe_dump({"version": "0.0.1", "meta": {"schema_version": "0", "updated": "", "types": {}},
                              "orgs": [{"id": "x", "name": "X", "type": "company", "acronyms": ["X"]}]}))


UNIT_META = {**META, "generic_words": [*META["generic_words"], "centre", "for", "research"],
             "unit_words": ["department", "school", "centre", "lab"],
             "countries": {**META["countries"], "MX": ["Mexico"]},
             "regions": {"US": {"names": ["Massachusetts", "New Mexico"], "codes": ["MA", "NM"]}}}


@pytest.fixture(scope="module")
def unit_toy() -> Any:
    return matcher_from_entries(TOY, UNIT_META)


@pytest.mark.parametrize("surface,status,orgs", [
    ("Department of Computer Science", "generic", ()),
    ("Centre for", "unmatched", ()),                 # a unit word with nothing after it
    ("University", "unmatched", ()),                 # all generic words, no unit head
    ("Northeastern University, Boston, MA", "matched", ("neu-us",)),        # postal code in an address position
    ("Northeastern University, Boston, MA 02115", "matched", ("neu-us",)),
    ("Northeastern University, Massachusetts", "matched", ("neu-us",)),
    ("Northeastern University, MA", "matched", ("neu-us",)),
    ("MA, Northeastern University", "ambiguous", ()),                       # the first segment is not an address
    ("Northeastern University, Ma", "ambiguous", ()),                       # codes are cased
    ("Northeastern University, New Mexico", "matched", ("neu-us",)),        # not also Mexico (longest name)
    ("Northeastern University, Shenyang, MA, China", "matched", ("neu-cn",)),  # a named country beats a code
])
def test_unit_words_and_regions(unit_toy: Any, surface: str, status: str, orgs: tuple[str, ...]) -> None:
    got = unit_toy.match(surface)
    assert (got.status, got.orgs) == (status, orgs), got


def test_the_longest_country_name_wins(unit_toy: Any) -> None:
    assert unit_toy._countries(["new", "mexico"], set()) == {"US"}
    assert unit_toy._countries(["mexico", "city"], set()) == {"MX"}


def test_a_local_name_resolves_next_to_a_sibling_unit(toy: Any) -> None:
    # "casia" is a CAS unit: CAS (the parent of the local name's entry) counts through it (D737)
    assert toy.match("Institute of Computing Technology, CASIA").orgs == ("casia", "ict")


def test_without_unit_words_any_all_generic_surface_is_generic(toy: Any) -> None:
    assert toy.match("University").status == "generic"  # the 0.1 rule (no meta.unit_words)


def test_affiliation_orgs_shape(toy: Any) -> None:
    payload = {"items": [
        {"item_key": "0", "byline_index": 0, "affiliations": [{"surface": "Google DeepMind", "org_slugs": []}]},
        {"item_key": "1", "byline_index": 1, "affiliations": [{"surface": "Stanford University"},
                                                               {"surface": "Northeastern University"}]},
        {"item_key": "2", "byline_index": 2, "affiliations": []},
        {"item_key": "3", "byline_index": 3, "affiliations": [{"surface": "Institute of Computing Technology"},
                                                               {"surface": "CAS"}]},
    ]}
    got = affiliation_orgs(payload, toy)
    assert got["org_ids"] == ["deepmind", "stanford", "ict", "cas"]
    assert "root_org_ids" not in got  # D737
    assert [a["status"] for a in got["items"][1]["affiliations"]] == ["matched", "ambiguous"]
    assert got["items"][1]["affiliations"][1]["ambiguous"] == [["neu-cn", "neu-us"]]
    assert affiliation_orgs(None, toy) == {"items": [], "org_ids": []}


# ── the shipped draft ────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def vocab() -> Any:
    return load(DEFAULT_PATH)


def test_draft_loads_lint_clean_with_known_slugs(vocab: Any) -> None:
    m = vocab.matcher
    reg = yaml.safe_load((DEFAULT_PATH.parents[1] / "configs" / "registry.yaml").read_text())
    from stack.modelvocab import load_vocab, maker_refs

    makers = {r.id for e in load_vocab().validated.entries for r in maker_refs(e)}
    channels = yaml.safe_load((DEFAULT_PATH.parents[1] / "configs" / "channels.yaml").read_text())
    known = set(m.entries) | {x for e in m.entries.values() for x in e.other_ids}
    assert {o["slug"] for o in reg["orgs"]} <= set(m.entries)  # the registry's slugs are ids (D737)
    assert {i["org"] for i in channels["institutions"]} <= set(m.entries)  # channels 2.4.0
    assert makers <= set(m.entries)  # models 1.1.0 (D738): maker ids are entry ids (1.0.0's internlm an other id)
    assert "internlm" in known and "shanghai-ai-lab" in makers
    # 0.2.0 in the data repository (D738): the Arc Institute maker is the US institute, not ARC Institute Indonesia
    assert (m.entries["arc-institute"].country, m.entries["arc-institute-indonesia"].country) == ("US", "ID")
    assert DEFAULT_PATH.name == "orgs-0.2.2.yaml" and vocab.version == "0.2.2"


@pytest.mark.parametrize("surface,orgs", [
    ("Google DeepMind", ("deepmind",)),
    ("DeepMind, London, UK", ("deepmind",)),
    ("Google Research", ("google-research",)),
    ("Google", ("google",)),
    ("Google (United States)", ("google",)),
    ("Microsoft Research Asia", ("microsoft-research-asia",)),
    ("Microsoft Research", ("microsoft-research",)),
    ("Microsoft (United States)", ("microsoft",)),
    ("Meta FAIR", ("meta-ai",)),
    ("Meta", ("meta",)),
    ("Department of Computer Science, Stanford University", ("stanford",)),
    ("Stanford University; Anthropic", ("stanford", "anthropic")),
    ("Shanghai Artificial Intelligence Laboratory", ("shanghai-ai-lab",)),
    ("Shanghai AI Laboratory", ("shanghai-ai-lab",)),
    ("Scale AI, San Francisco", ("scale-ai",)),
    ("Mila / McGill University", ("mila", "mcgill-university")),
    ("Vector Institute, Toronto", ("vector",)),
    ("US AI Safety Institute", ("us-aisi",)),
    ("UK AI Safety Institute", ("uk-aisi",)),
    ("University of California, Berkeley", ("berkeley",)),
    ("Massachusetts Institute of Technology", ("massachusetts-institute-of-technology",)),
    ("Tsinghua University", ("tsinghua",)),
    ("Allen Institute for AI (AI2)", ("ai2",)),
    # 0.2.0 (D737): named teams and labs are their own entries under the company
    ("Google Brain", ("google-brain",)),
    ("Google Research, Brain Team", ("google-brain",)),
    ("Tencent AI Lab", ("tencent-ai-lab",)),
    ("AWS AI Labs", ("aws-ai-labs",)),
    ("Berkeley AI Research, UC Berkeley", ("bair",)),
    ("FAIR, Meta", ("meta-ai",)),
    ("Meta AI Research , FAIR", ("meta-ai",)),
    ("FAIR, IALAB, University of Buenos Aires", ("universidad-de-buenos-aires",)),  # not Meta, not the facility
    ("FAIR GmbH, Darmstadt", ("facility-for-antiproton-and-ion-research",)),
    ("Northeastern University, Boston, MA 02115", ("northeastern-university-us",)),
    ("Northeastern University, Shenyang, China", ("northeastern-university",)),
    # 0.2.1 and matcher 3 (D760): a city after an organisation is its address; the city as the institution stays
    ("FAR.AI, Berkeley, California, USA", ("far-ai",)),
    ("MATS , Berkeley , CA , USA", ("ml-alignment-theory-scholars",)),
    ("Department of Computer Science and Engineering, University of South Carolina, Columbia, SC, USA",
     ("university-of-south-carolina",)),
    ("ALLT.AI, LLC, Columbia, SC", ("allt-ai",)),
    ("Department of Electrical and Computer Engineering, Purdue University\nPrinceton\nNJ\n08540\nUSA",
     ("purdue-university",)),
    ("Institute for Advanced Study, Princeton, NJ", ("institute-for-advanced-study",)),
    ("Berkeley, Department of EECS", ("berkeley",)),
    ("Department of EECS, Berkeley", ("berkeley",)),
    ("UC Berkeley, Berkeley, CA", ("berkeley",)),
    ("CS, Standford", ("stanford",)),
    ("Stanford, CA", ("stanford",)),
    ("Columbia University, New York", ("columbia-university",)),
    ("UCL, MATS", ("university-college-london", "ml-alignment-theory-scholars")),  # not a place: still named
    ("Monash University, RIKEN", ("monash-university", "riken")),
    ("RIKEN AIP", ("riken-center-for-advanced-intelligence-project",)),
    ("Centre for AI Safety", ("cais",)),
])
def test_draft_answers(vocab: Any, surface: str, orgs: tuple[str, ...]) -> None:
    got = vocab.matcher.match(surface)
    assert got.orgs == orgs, got


@pytest.mark.parametrize("surface,orgs", [
    # The Chinese-coverage work: Chinese-script aliases (ROR, OpenAlex, hand) matched inside unspaced surfaces
    ("清华大学计算机科学与技术系", ("tsinghua",)),
    ("上海人工智能实验室", ("shanghai-ai-lab",)),
    ("复旦大学，上海", ("fudan",)),
    ("北京智源人工智能研究院", ("baai",)),
    ("中国科学院自动化研究所, 北京, 中国", ("chinese-academy-of-sciences",)),
    ("中国科学院大学", ("university-of-chinese-academy-of-sciences",)),
    ("香港科技大学（广州）", ("hkust-guangzhou",)),      # the longer alias, not also HKUST
    ("香港科技大學", ("hong-kong-university-of-science-and-technology",)),
    ("Department of CS, 西安电子科技大学", ("xidian-university",)),
    ("华中科技大学 计算机科学与技术学院, 湖北 武汉 430074", ("huazhong-university-of-science-and-technology",)),
    ("智谱AI", ("zhipu",)),
    ("杭州深度求索人工智能基础技术研究有限公司", ("deepseek",)),
    ("Peking University (北京大学)", ("peking-university",)),
])
def test_draft_answers_chinese_script(vocab: Any, surface: str, orgs: tuple[str, ...]) -> None:
    got = vocab.matcher.match(surface)
    assert got.orgs == orgs, got


@pytest.mark.parametrize("surface,forbidden", [
    ("杭州电子科技大学", "university-of-electronic-science-and-technology-of-china"),  # excluded alias 电子科技大学
    ("北京东城区某研究所", "jingdong"),                                             # 京东 inside 北京东…
])
def test_draft_near_misses_chinese_script(vocab: Any, surface: str, forbidden: str) -> None:
    assert forbidden not in vocab.matcher.match(surface).orgs


@pytest.mark.parametrize("surface,forbidden", [
    ("Centre Universitaire de Mila", "mila"),
    ("large-scale AI systems for alignment", "scale-ai"),
    ("UK Advanced Research + Invention Agency (ARIA)", "uk-aisi"),
    ("we report the metric across parametric models", "metr"),
    ("chain-of-thought coherence analysis", "cohere"),
    ("FAIR at the University of Somewhere", "far-ai"),
    ("explainable AI (XAI) methods", "xai"),
    ("a meta-analysis of meta-learning", "meta"),
    ("our moonshot project", "moonshot"),
    ("Google Research", "google"),          # google-research, never the bare parent besides it
])
def test_draft_near_misses(vocab: Any, surface: str, forbidden: str) -> None:
    assert forbidden not in vocab.matcher.match(surface).orgs


def test_draft_parents_of_the_new_units(vocab: Any) -> None:
    m = vocab.matcher
    assert m.parent["google-brain"] == "google" and m.parent["ibm-research"] == "ibm"
    assert m.parent["ibm-research-thomas-j-watson-research-center"] == "ibm-research"
    assert m.root("qwen-team") == "alibaba" and m.root("msl-preparedness-team") == "meta"


@pytest.mark.parametrize("surface,status", [
    ("Department of Computer Science", "generic"),   # a unit without its organisation
    ("School of Computing", "generic"),
    ("Centre for", "unmatched"),                     # junk, not a unit
    ("University", "unmatched"),
    ("{A", "unmatched"),
    ("Transformative Futures Institute", "unmatched"),  # a name the vocabulary lacks, not a unit
    ("International Computer Science Institute", "matched"),   # all generic words: a cased alias (D737)
    ("Independent researcher", "non_org"),
    ("Northeastern University", "ambiguous"),        # no default
    ("Institute for Advanced Study", "generic"),     # many institutes have the name (D760)
])
def test_draft_statuses(vocab: Any, surface: str, status: str) -> None:
    assert vocab.matcher.match(surface).status == status


def test_the_envelope_is_validated_and_0_1_0_still_loads() -> None:
    from stack.catalogue import CatalogueError

    raw = yaml.safe_load(DEFAULT_PATH.read_text())
    assert raw["meta"]["schema"] == "orgs-0" and "entries" in raw and "orgs" not in raw
    bad = {**raw, "entries": [{**raw["entries"][0], "acronyms": ["X"]}] + raw["entries"][1:]}
    with pytest.raises(OrgVocabError, match="schema problem"):
        parse(yaml.safe_dump(bad))
    dup = {**raw, "entries": raw["entries"][:2] + raw["entries"][:1]}
    with pytest.raises(OrgVocabError, match="duplicate ids"):
        parse(yaml.safe_dump(dup))
    with pytest.raises(CatalogueError, match="meta.schema"):
        from stack.catalogue import validate
        validate({**raw, "meta": {**raw["meta"], "schema": "models-1"}}, "orgs-0")
    old = load(DEFAULT_PATH.with_name("orgs-0.1.0.yaml"))
    assert old.version == "0.1.0" and old.matcher.match("Meta AI Research , FAIR").orgs == (
        "meta-ai", "facility-for-antiproton-and-ion-research")  # 0.1.0 as it was (the records it made stand)


# ── matcher 3 on the toy (D760) ──────────────────────────────────────────────────────────────

PLACE_TOY = [
    {"id": "ucb", "name": "UC Berkeley", "type": "university", "country": "US", "aliases": ["UC Berkeley"],
     "segment_aliases": ["Berkeley"]},
    {"id": "redwood", "name": "Redwood Research", "type": "nonprofit", "country": "US", "aliases": ["Redwood Research"]},
    {"id": "mats", "name": "MATS", "type": "nonprofit", "segment_aliases": ["MATS"]},
    {"id": "riken", "name": "RIKEN", "type": "institute", "segment_aliases": ["RIKEN"]},
]


@pytest.mark.parametrize("surface,orgs", [
    ("Redwood Research, Berkeley, CA, USA", ("redwood",)),     # the address of the organisation before it
    ("MATS, Berkeley, CA", ("mats",)),                          # an earlier segment alias is an organisation too
    ("Redwood Research\nBerkeley\nCalifornia", ("redwood",)),  # hard separators are segments as well
    ("Berkeley, Department of EECS", ("ucb",)),                 # the first segment is never an address
    ("Department of EECS, Berkeley", ("ucb",)),                 # a unit before it: the city is the institution
    ("Berkeley", ("ucb",)),
    ("UC Berkeley, Berkeley, CA", ("ucb",)),
    ("Redwood Research, RIKEN", ("redwood", "riken")),          # not a place name: the segment rule as before
])
def test_a_place_segment_after_an_organisation_is_its_address(surface: str, orgs: tuple[str, ...]) -> None:
    m = matcher_from_entries(PLACE_TOY, {**META, "places": ["Berkeley"]})
    assert m.match(surface).orgs == orgs


def test_without_places_a_city_segment_still_names_its_entry() -> None:
    m = matcher_from_entries(PLACE_TOY, META)  # 0.2.0 has no meta.places: the 0.2.0 answers
    assert m.match("Redwood Research, Berkeley, CA, USA").orgs == ("redwood", "ucb")


def test_spelled_out_letters_match_the_ascii_spelling() -> None:
    m = matcher_from_entries([{"id": "jlu", "name": "JLU", "type": "university", "aliases": ["Universität Gießen"]},
                              {"id": "lodz", "name": "Lodz", "type": "university", "aliases": ["University of Lodz"]}],
                             META)
    assert m.match("Universitat Giessen").orgs == ("jlu",) and m.match("Universität Gießen").orgs == ("jlu",)
    assert m.match("University of Łódź").orgs == ("lodz",)
