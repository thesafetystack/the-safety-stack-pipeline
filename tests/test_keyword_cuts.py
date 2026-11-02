"""The backfill cuts (D304: the proposed cuts accepted; docs/design/collection-yield.md §10):
``stack/rules.py`` (AI terms, source classes, :func:`keyword_cut`), the ``keyword_cuts`` channel option, the
OpenAlex and S2 plans, and one OpenAlex run that drops by each cut, counts it, keeps a work the institution family
also returns, and re-sweeps from the floor once the cuts are switched off (the cut set is in the coverage key).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from stack import rules
from stack.config import ChannelsConfig, load
from tests.test_collect_openalex import (  # noqa: F401  (oa_env is a fixture)
    API,
    CONFIG_DIR,
    _load_wf,
    _oa_plan,
    _page,
    _params,
    _view,
    _work,
    oa_env,
    openalex,
)

s2 = _load_wf("semantic_scholar")

@pytest.mark.parametrize("text, expect", [
    ("Fine-tuning large language models for refusal", True),
    ("Sparse autoencoders on GPT-2 residual streams", True),
    ("A transformer-based detector", True),
    ("The EU AI Act and liability", True),            # upper-case "AI", case-sensitive
    ("Towards AGI", True),
    ("Agentic workflows", True),
    ("Mechanistic models of pavement fatigue", False),
    ("Grokking in tumour growth curves", False),
    ("aid and maintenance", False),                    # "ai" inside a word, lower case
    ("", False),
    (None, False),
    # Chinese (the Chinese-coverage work): substrings without word spaces; an English term next to a Chinese character
    ("面向大语言模型的越狱攻击综述", True),
    ("基于深度强化学习的安全约束策略优化", True),
    ("生成式人工智能服务安全基本要求", True),
    ("多模态智能体的安全评测", True),
    ("LLM安全对齐方法研究", True),                       # \bllms?\b next to 安 (re.ASCII)
    ("AI安全治理框架", True),                            # \bAI\b next to 安
    ("食品安全风险评估方法", False),                      # food safety: no AI term
    ("桥梁结构的安全性评估", False),
])
def test_has_ai_term(text: str | None, expect: bool) -> None:
    assert rules.has_ai_term(text) is expect


def test_ai_term_cut_keeps_a_chinese_ai_hit_and_drops_a_chinese_non_ai_hit() -> None:
    keys = [("doi", "10.48550/arxiv.2601.00001"), ("arxiv", "2601.00001")]
    assert rules.keyword_cut(keys, "大语言模型的安全对齐 摘要", ai_term=True) is None
    assert rules.keyword_cut(keys, "混凝土结构的安全评估 摘要", ai_term=True) == "ai_term"


@pytest.mark.parametrize("keys, located, expect", [
    ([("doi", "10.48550/arxiv.2601.00001"), ("arxiv", "2601.00001")], False, "arxiv"),
    ([("doi", "10.1145/3743127"), ("arxiv", "2601.00001")], False, "arxiv"),   # journal version of a preprint
    ([("doi", "10.1145/3743127")], True, "arxiv"),                             # arXiv location seen in the record
    ([("doi", "10.2139/ssrn.4801234")], False, "kept_preprint"),               # SSRN
    ([("doi", "10.5281/zenodo.17000001")], False, "self_publication"),
    ([("zenodo", "17000001")], False, "self_publication"),
    ([("doi", "10.21203/rs.3.rs-1/v1")], False, "self_publication"),           # Research Square
    ([("doi", "10.1145/3743127"), ("pmid", "123")], False, "publisher"),
    ([("openalex", "W1"), ("s2", "a" * 40)], False, "no_doi"),
])
def test_source_class(keys: list[tuple[str, str]], located: bool, expect: str) -> None:
    assert rules.source_class(keys, arxiv_located=located) == expect


def test_keyword_cut_names_the_first_cut_and_refuses_unknown_ones() -> None:
    both = ["publisher", "self_publication"]
    pub, zen, arx = [("doi", "10.1145/x")], [("doi", "10.5281/zenodo.1")], [("arxiv", "2601.00001")]
    assert rules.keyword_cut(pub, "LLM", drop_sources=both) == "publisher"
    assert rules.keyword_cut([("openalex", "W1")], "LLM", drop_sources=both) == "publisher"  # no DOI, no arXiv
    assert rules.keyword_cut(zen, "LLM", drop_sources=both) == "self_publication"
    assert rules.keyword_cut(zen, "LLM", drop_sources=["publisher"]) is None
    assert rules.keyword_cut(pub, "LLM", drop_sources=["self_publication"]) is None
    assert rules.keyword_cut(arx, "pavement", drop_sources=both) is None
    assert rules.keyword_cut(arx, "pavement", drop_sources=both, ai_term=True) == "ai_term"
    assert rules.keyword_cut(pub, "pavement", drop_sources=both, ai_term=True) == "publisher"  # source cut first
    assert rules.keyword_cut(pub, "pavement") is None
    with pytest.raises(rules.RuleError, match="unknown source cut"):
        rules.keyword_cut(pub, "LLM", drop_sources=["journals"])


def test_repo_channels_carry_the_accepted_cuts() -> None:
    cfg = load(CONFIG_DIR)
    oa, sch = (cfg.channels.channel(c).keyword_cuts for c in ("openalex", "semantic_scholar"))
    assert oa is not None and sch is not None
    groups = {g.id: g for g in cfg.channels.concept_groups}
    # cut 3's groups: unquoted, non-broad, queried on OpenAlex (phrase-quoting them was rejected)
    for gid in oa.ai_term_groups:
        g = groups[gid]
        assert not g.broad and not g.needs_ai_context and g.terms_for_openalex(), gid
        assert not any(t.startswith('"') for t in g.terms_for_openalex()), gid


def test_ai_term_groups_must_name_concept_groups() -> None:
    raw = {"version": "x", "concept_groups": [{"id": "a", "terms": ["t"]}],
           "channels": [{"id": "openalex", "kind": "search", "license_class": "open", "channel_group": "paper",
                         "keyword_cuts": {"drop_sources": ["publisher"], "ai_term_groups": ["a", "nope"]}}]}
    with pytest.raises(ValueError, match="nope"):
        ChannelsConfig.model_validate(raw)
    with pytest.raises(ValueError):
        ChannelsConfig.model_validate({**raw, "channels": [{**raw["channels"][0], "keyword_cuts": {
            "drop_sources": ["journals"]}}]})


def test_plans_carry_the_cuts_and_s2_drops_three_groups() -> None:
    cfg = load(CONFIG_DIR)
    cuts = cfg.channels.channel("openalex").keyword_cuts
    assert cuts is not None and cuts.ai_term_groups and cuts.drop_sources
    oa = {p["id"]: p for p in openalex.plan_groups(cfg)}
    assert {gid for gid, p in oa.items() if p["ai_term"]} == set(cuts.ai_term_groups) & set(oa)
    for gid, p in oa.items():  # cut 4 is cuts 1 + 2; an ai_term group adds cut 3
        assert p["cuts"] == sorted([*cuts.drop_sources, *(["ai_term"] if gid in cuts.ai_term_groups else [])]), gid
    plan = {p["id"]: p for p in s2.plan_groups(cfg)}
    assert not {g.id for g in cfg.channels.concept_groups if g.s2_terms == []} & set(plan)  # cut 5
    assert all(p["cuts"] == ["publisher", "self_publication"] and not p["ai_term"] for p in plan.values())
    assert s2.coverage_query(plan["umbrella"])["cuts"] == ["publisher", "self_publication"]
    assert "cuts" not in s2.coverage_query({**plan["umbrella"], "cuts": []})


def test_openalex_work_cut_reads_arxiv_locations_and_spares_other_families() -> None:
    item = {"kind": "group", "id": "mech-interp", "drop_sources": ["publisher", "self_publication"],
            "ai_term": True, "cuts": ["ai_term", "publisher", "self_publication"]}
    llm = {"abstract_inverted_index": {"LLM": [0], "circuits": [1]}}
    pub = {**_work(1, doi="https://doi.org/10.1145/3743127"), **llm}
    keys = openalex.work_keys(pub)
    assert openalex.work_cut(pub, keys, item) == "publisher"
    by_url = {**pub, "locations": [{"landing_page_url": "https://arxiv.org/abs/2601.00001v2"}]}
    assert openalex.work_cut(by_url, keys, item) is None          # the key adder's arxiv key
    by_source = {**pub, "locations": [{"source": {"id": "https://openalex.org/S4306400194"}}]}
    assert openalex.work_arxiv_located(by_source) and openalex.work_cut(by_source, keys, item) is None
    assert openalex.work_cut(pub, keys, {**item, "kind": "institution"}) is None
    assert openalex.work_cut(pub, keys, {**item, "kind": "authors"}) is None
    non_ai = _work(2, doi="https://doi.org/10.48550/arXiv.2601.00002", type_="preprint")
    assert openalex.work_cut(non_ai, openalex.work_keys(non_ai), item) == "ai_term"


def test_s2_paper_cut_reads_the_arxiv_venue() -> None:
    item = {"drop_sources": ["publisher", "self_publication"], "ai_term": False,
            "cuts": ["publisher", "self_publication"]}
    pub = {"paperId": "a" * 40, "externalIds": {"DOI": "10.1145/3743127"}, "title": "LLM"}
    assert s2.paper_cut(pub, item) == "publisher"
    assert s2.paper_cut({**pub, "venue": "arXiv.org"}, item) is None
    assert s2.paper_cut({**pub, "externalIds": {"ArXiv": "2601.00001", "DOI": "10.1145/3743127"}}, item) is None
    assert s2.paper_cut({**pub, "externalIds": {"DOI": "10.5281/zenodo.1"}}, item) == "self_publication"
    assert s2.paper_cut(pub, {**item, "cuts": []}) is None


@pytest.mark.slow  # real store + runner
def test_openalex_run_drops_counts_and_resweeps_when_switched_off(tmp_path: pathlib.Path, oa_env: Any,  # noqa: F811
                                                                  monkeypatch: Any) -> None:
    """One `mech-interp` page with an arXiv LLM paper (kept), a publisher article (cut `publisher`), a Zenodo
    record (`self_publication`) and an arXiv paper on pavement mechanics (`ai_term`); the institution query returns
    the publisher article too, so it is observed there. Counts by cut and by group; the window's `seen` excludes
    the dropped; the coverage key names the cuts. With the cuts switched off the next day the group is a new key:
    asked from the floor (no watermark, no look-back), and the three works are observed."""
    import dataclasses
    import datetime as dt

    import stack.config as sc
    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    floor = "2026-09-01"
    cfg = load(CONFIG_DIR)
    mi = {p["query"]: p for p in openalex.plan_groups(cfg)}["group:mech-interp"]
    inst = "inst:I4210161460"
    llm = {"abstract_inverted_index": {"Circuits": [0], "in": [1], "LLM": [2], "agents": [3]}}
    keep = {**_work(1, doi="https://doi.org/10.48550/arXiv.2609.00001", type_="preprint"), **llm}
    pub = {**_work(2, doi="https://doi.org/10.1145/3743127"), **llm}
    zen = {**_work(3, doi="https://doi.org/10.5281/zenodo.1700001"), **llm}
    pave = {**_work(4, doi="https://doi.org/10.48550/arXiv.2609.00004", type_="preprint"),
            "title": "Mechanistic interpretation of pavement fatigue",
            "abstract_inverted_index": {"Asphalt": [0], "cracks": [1]}}
    g_filter = openalex.group_filter(mi["terms"], floor, mi["broad"])
    i_filter = openalex.institution_filter("I4210161460", floor, [])
    fake.add(API, _params(g_filter), json=_page([keep, pub, zen, pave]))
    fake.add(API, _params(i_filter), json=_page([pub]))
    opts = {"queries": ["group:mech-interp", inst], "from": floor, "lookback_days": 2}
    rec = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec.status == "ok", rec.error
    c = rec.counts
    assert (c["cut_publisher"], c["cut_self_publication"], c["cut_ai_term"]) == (1, 1, 1)
    assert (c["cut_publisher:mech-interp"], c["cut_self_publication:mech-interp"], c["cut_ai_term:mech-interp"]) == (
        1, 1, 1)
    src = {r["identity"]["external_id"]: r for r in _view(store, "sources.openalex")}
    assert sorted(src) == ["10.1145/3743127", "10.48550/arxiv.2609.00001"]
    assert [m["query_id"] for m in src["10.1145/3743127"]["metadata"]["matched_by"]] == [inst]
    cov = {openalex.query_label(r["query"]): r for r in _view(store, "coverage.openalex")}
    assert (cov["group:mech-interp"]["status"], cov["group:mech-interp"]["seen"]) == ("ok", 1)
    assert json.loads(cov["group:mech-interp"]["query"])["cuts"] == ["ai_term", "publisher", "self_publication"]
    assert "cuts" not in json.loads(cov[inst]["query"])

    # cuts switched off, the next day: a new key for the group (no watermark: from the floor, no look-back);
    # the institution key is unchanged and resumes from its look-back start (D582: watermark − 2 days)
    real = sc.load

    def no_cuts(*a: Any, **kw: Any) -> Any:
        got = real(*a, **kw)
        chans = [ch.model_copy(update={"keyword_cuts": None}) for ch in got.channels.channels]
        return dataclasses.replace(got, channels=got.channels.model_copy(update={"channels": chans}))

    monkeypatch.setattr(sc, "load", no_cuts)
    t0 = dt.datetime.now(dt.UTC)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=1))
    resumed = openalex.institution_filter("I4210161460", (t0.date() - dt.timedelta(days=2)).isoformat(), [])
    fake.add(API, _params(resumed), json=_page([]))
    n = len(fake.calls)
    rec2 = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec2.status == "ok", rec2.error
    urls = [call.url for call in fake.calls[n:]]
    assert fake.key("GET", API, _params(g_filter))[1] in urls
    assert fake.key("GET", API, _params(resumed))[1] in urls
    assert rec2.counts["cut_publisher"] == rec2.counts["cut_ai_term"] == 0
    src2 = {r["identity"]["external_id"] for r in _view(store, "sources.openalex")}
    assert src2 == {"10.1145/3743127", "10.48550/arxiv.2609.00001", "10.5281/zenodo.1700001",
                    "10.48550/arxiv.2609.00004"}


# ─────────────────────────────────────────────────────────────────────────────
# D654 (a): the dated publisher-cut exception. A concept-group hit published before
# `keyword_cuts.publisher_cut_floor` whose title or abstract carries a pre-2020 safety stem
# (stack/rules.py PRE2020_SAFETY_STEMS) bypasses the source cuts; the `ai_term` cut still applies.
# ─────────────────────────────────────────────────────────────────────────────
#: Replay fixture from the R2 C11 probe (OpenAlex 2015-2019 hits of four groups, with the merged Fable/Astra
#: labels) and the R2 landmark lookups: titles and abstract snippets as OpenAlex returned them, the DOI (None = no
#: DOI), the group whose query returned the hit, the publication date, and the cut expected under the repo's
#: channels.yaml. Positives: the three landmarks R2 found lost to the cut and the C11 hits labelled relevant;
#: negatives: named noise of the same samples.
PRE2020_REPLAY: list[tuple[str, str, str | None, str, str, str, str | None]] = [
    # (name, group, doi, published, title, abstract snippet, expected cut)
    ("quantilizers", "legacy-safety", None, "2016-03-29",
     "Quantilizers: A Safer Alternative to Maximizers for Limited Optimization",
     "In the field of AI, expected utility maximizers are commonly used as a model for idealized agents. However, "
     "expected utility maximization can lead to unintended solutions when the utility function does not quantify "
     "everything the operators care about", None),
    ("garcia-fernandez-survey", "legacy-safety", "10.5555/2789272.2886795", "2015-01-01",
     "A comprehensive survey on safe reinforcement learning",
     "Safe Reinforcement Learning can be defined as the process of learning policies that maximize the expectation "
     "of the return in problems in which it is important to ensure reasonable system performance", None),
    ("agi-containment", "umbrella", "10.1007/978-3-319-41649-6_6", "2016-01-01",
     "The AGI Containment Problem", "", None),                                           # no abstract in OpenAlex
    ("sarma-safron-hay", "umbrella", "10.7287/peerj.preprints.27321", "2018-11-06",
     "Integrative biological simulation, neuropsychology, and AI safety",
     "We describe a biologically-inspired research agenda with parallel tracks aimed at AI and AI safety.", None),
    ("jilk-herd-read", "umbrella", "10.1080/0952813x.2017.1354081", "2017-07-19",
     "Anthropomorphic reasoning about neuromorphic AGI safety",
     "One candidate approach to creating artificial general intelligence (AGI) is to imitate the essential "
     "computations of human cognition.", None),
    ("sarma-hay", "umbrella", "10.17605/osf.io/xdmw9", "2017-01-01",                       # OSF: self_publication
     "Robust Computer Algebra, Theorem Proving, and Oracle AI",
     "In the context of superintelligent AI systems, the term \"oracle\" has two meanings. … a class of systems "
     "which may be useful for addressing the value alignment and AI control problems", None),
    ("yampolskiy", "legacy-safety", "10.1201/b19187-9", "2015-11-18",
     "Utility Function Security in Artificially Intelligent Agents",
     "ABSTRACT The notion of “wireheading,” or direct reward center stimulation of the brain, is a well-known "
     "concept in neuroscience. In this chapter, we examine the corresponding issue of reward (utility) function "
     "integrity in artificially intelligent machines.", None),
    ("sui-et-al", "legacy-safety", None, "2015-07-01",
     "Safe Exploration for Optimization with Gaussian Processes",
     "We consider sequential decision problems under uncertainty … This requires balancing exploration (learning "
     "about the objective) and exploitation, a problem well-studied in the multi-armed bandit literature.", None),
    # negatives
    ("corrigible-knowledge", "corrigibility", "10.4324/9781315084510-7", "2017-09-29",   # a stem, no AI term
     "Meehl's Response to Campbell",
     "his text seems to conflate the probable (uncertain, corrigible, fallible) character of human knowledge, "
     "scientific or otherwise, with sentences not meaning what they seem to say", "ai_term"),
    ("icu-patient-safety", "corrigibility", "10.1111/nicc.12312", "2017-08-18",
     "What's in this Issue",
     "A common element underpinning these studies is an unrelenting focus on patient safety; a cornerstone of "
     "high-quality critical care", "publisher"),
    ("ed-handoffs", "corrigibility", "10.5811/westjem.2015.8.27278", "2015-11-12",
     "Teaching and Assessing ED Handoffs: A Qualitative Study Exploring Resident, Attending, and Nurse Perceptions",
     "residency programs ensure resident competency in performing safe, effective handoffs", "publisher"),
    ("research-impact-measures", "legacy-safety", None, "2016-11-09",
     "Research writing and Research Impact Measures: what Librarians need to know?",
     "Library professionals from the MENA region need to contribute more to the global knowledge base", "publisher"),
    ("antiplatelet", "legacy-safety", "10.1002/lary.25776", "2015-11-04",
     "What is the optimal perioperative management of antiplatelet therapy?",
     "the thromboembolic risk of interrupting antiplatelet therapy, and the perioperative bleeding risk; therapy "
     "can be safely interrupted for most procedures", "publisher"),
    ("arctic-drilling", "legacy-safety", "10.5070/l5342031732", "2016-07-20",
     "Exploratory Drilling in the Arctic Outer Continental Shelf",
     "a proposed rule employing more stringent standards and requirements for offshore exploratory drilling "
     "operations in order to ensure effective and safe exploration in the Arctic Outer Continental Shelf",
     "publisher"),
    ("astronaut-recovery", "legacy-safety", None, "2016-02-08",
     "Pilot Field Test: Recovery from a Simulated Fall and Quiet Stance Stability After Long-Duration Space Flight",
     "It is therefore important to characterize the extent and longevity of functional deficits experienced by "
     "astronauts in order to design safe exploration class missions.", "publisher"),
]


def _inverted(text: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, tok in enumerate(text.split()):
        out.setdefault(tok, []).append(i)
    return out


def _replay_work(n: int, doi: str | None, published: str, title: str, abstract: str) -> dict[str, Any]:
    return {"id": f"https://openalex.org/W9{n:04d}", "doi": f"https://doi.org/{doi}" if doi else None,
            "title": title, "type": "article", "publication_date": published, "publication_year": int(published[:4]),
            "abstract_inverted_index": _inverted(abstract) if abstract else None, "locations": []}


@pytest.mark.parametrize("text, expect", [
    ("Integrative biological simulation, neuropsychology, and AI safety", True),
    ("Anthropomorphic reasoning about neuromorphic AGI safety", True),
    ("The AGI Containment Problem", True),
    ("Quantilizers: A Safer Alternative to Maximizers", True),
    ("A comprehensive survey on safe reinforcement learning", True),
    ("Why friendly AIs won't be that friendly", True),
    ("wireheading in rational self-improving agents", True),
    ("Safe exploration for optimization with Gaussian processes: learning the objective", True),
    ("Safely interruptible agents", True),
    ("ensure effective and safe exploration in the Arctic Outer Continental Shelf", False),  # no learning context
    ("therapy can be safely interrupted for most procedures", False),
    ("Research impact measures: what librarians need to know", False),
    ("patient safety and efficacy of the new drug", False),          # the modern stem `safety` is not a pre-2020 one
    ("Adversarial robustness of image classifiers", False),
    ("", False),
    (None, False),
])
def test_has_pre2020_safety_stem(text: str | None, expect: bool) -> None:
    assert rules.has_pre2020_safety_stem(text) is expect


def test_publisher_cut_exempt_is_dated_and_needs_a_stem() -> None:
    stem = "Anthropomorphic reasoning about neuromorphic AGI safety"
    floor = "2020-01-01"
    assert rules.publisher_cut_exempt(stem, "2019-12-31", floor)
    assert rules.publisher_cut_exempt(stem, "2019", floor)                 # a year-only date is that year's 1 January
    assert not rules.publisher_cut_exempt(stem, "2020-01-01", floor)       # the floor itself is not before it
    assert not rules.publisher_cut_exempt(stem, "2020", floor)
    assert not rules.publisher_cut_exempt(stem, None, floor)               # an undated hit is not provably pre-floor
    assert not rules.publisher_cut_exempt(stem, "2017-07-19", None)        # not configured
    assert not rules.publisher_cut_exempt("Patient safety in the ICU", "2017-07-19", floor)
    pub, nodoi, zen = [("doi", "10.1080/x")], [("openalex", "W1")], [("doi", "10.5281/zenodo.1")]
    both = ["publisher", "self_publication"]
    for keys in (pub, nodoi, zen):  # every source cut, the no-DOI class included
        assert rules.keyword_cut(keys, stem, drop_sources=both, published="2017-07-19",
                                 publisher_cut_floor=floor) is None
        assert rules.keyword_cut(keys, stem, drop_sources=both, published="2021-03-01",
                                 publisher_cut_floor=floor) is not None
        assert rules.keyword_cut(keys, stem, drop_sources=both, published="2017-07-19") is not None
    # the ai_term cut still applies to a bypassed hit
    assert rules.keyword_cut(pub, "uncertain, corrigible, fallible knowledge", drop_sources=both, ai_term=True,
                             published="2017-09-29", publisher_cut_floor=floor) == "ai_term"
    assert rules.source_cut(pub, drop_sources=both) == "publisher" and rules.source_cut(pub) is None


def test_pre2020_replay_admits_the_landmarks_and_c11_hits_and_cuts_the_noise() -> None:
    """The R2 replay under the repo's channels.yaml and the collector's own path (``work_cut``): every positive
    passes (publisher DOI, OSF, no DOI, a title-only record), every named negative is cut (the corrigibility group's
    stem-but-no-AI-term hit by ``ai_term``), and the same positives dated 2020 are cut again."""
    plan = {p["id"]: p for p in openalex.plan_groups(load(CONFIG_DIR))}
    assert plan["legacy-safety"]["publisher_cut_floor"] == "2020-01-01"
    got, later = {}, {}
    for n, (name, gid, doi, published, title, abstract, _) in enumerate(PRE2020_REPLAY):
        w = _replay_work(n, doi, published, title, abstract)
        got[name] = openalex.work_cut(w, openalex.work_keys(w), plan[gid])
        moved = {**w, "publication_date": "2020-02-01", "publication_year": 2020}
        later[name] = openalex.work_cut(moved, openalex.work_keys(moved), plan[gid])
    assert got == {name: expect for name, *_, expect in PRE2020_REPLAY}
    assert {name for name, *_, expect in PRE2020_REPLAY if expect is None and later[name] is None} == set()


def test_pre2020_bypass_is_in_the_coverage_key() -> None:
    """The floor and the stem set are in the group's coverage key: a changed floor or stem list is a new key (a
    re-sweep from the floor), and a channel without the option keeps its key. Semantic Scholar enables it too
    (plan revision 5): every S2 group key carries the same bypass, so its 59 queries re-sweep from 2015 once."""
    cfg = load(CONFIG_DIR)
    plan = {p["id"]: p for p in openalex.plan_groups(cfg)}
    key = openalex.coverage_query(plan["legacy-safety"], None)
    assert key["bypass"] == {"floor": "2020-01-01", "stems": rules.pre2020_stem_digest()}
    assert openalex.coverage_query({**plan["legacy-safety"], "publisher_cut_floor": "2018-01-01"}, None) != key
    assert openalex.coverage_query({**plan["legacy-safety"], "publisher_cut_floor": None}, None).get("bypass") is None
    assert len(rules.pre2020_stem_digest()) == 12
    s2_plan = s2.plan_groups(cfg)
    assert s2_plan and all(p["publisher_cut_floor"] == "2020-01-01" for p in s2_plan)
    assert all(set(s2.coverage_query(p)) == {"query", "s2_query", "cuts", "bypass", "venues", "venue_ai_terms"}
               for p in s2_plan)
    assert all(s2.coverage_query(p)["bypass"] == key["bypass"] for p in s2_plan)
    assert all(s2.coverage_query({**p, "publisher_cut_floor": None}).get("bypass") is None for p in s2_plan)


def test_s2_paper_cut_honours_a_configured_publisher_cut_floor() -> None:
    """The sibling wiring: S2's cut reads the paper's date and the plan entry's floor (enabled in the repo, D654)."""
    item = {"drop_sources": ["publisher", "self_publication"], "ai_term": False,
            "cuts": ["publisher", "self_publication"], "publisher_cut_floor": "2020-01-01"}
    pub = {"paperId": "a" * 40, "externalIds": {"DOI": "10.1080/0952813x.2017.1354081"},
           "title": "Anthropomorphic reasoning about neuromorphic AGI safety", "year": 2017,
           "publicationDate": "2017-07-19"}
    assert s2.paper_cut(pub, item) is None
    assert s2.paper_cut({**pub, "publicationDate": None}, item) is None                # the year alone dates it
    assert s2.paper_cut({**pub, "publicationDate": "2021-01-05", "year": 2021}, item) == "publisher"
    assert s2.paper_cut(pub, {**item, "publisher_cut_floor": None}) == "publisher"
    assert "bypass" in s2.coverage_query({**item, "query": "group:x", "s2_query": "q"})


def test_keyword_cut_floors_are_validated() -> None:
    base = {"version": "x", "concept_groups": [{"id": "a", "terms": ["t"], "broad": True}, {"id": "b", "terms": ["t"]}],
            "channels": [{"id": "openalex", "kind": "search", "license_class": "open", "channel_group": "paper"}]}

    def with_cuts(**cuts: Any) -> dict[str, Any]:
        return {**base, "channels": [{**base["channels"][0], "keyword_cuts": cuts}]}

    ok = ChannelsConfig.model_validate(with_cuts(drop_sources=["publisher"], publisher_cut_floor="2020-01-01",
                                                 field_filter_floor="2020-01-01", field_filter_groups=["a"]))
    kc = ok.channel("openalex").keyword_cuts
    assert kc is not None and (kc.publisher_cut_floor, kc.field_filter_floor, kc.field_filter_groups) == (
        "2020-01-01", "2020-01-01", ["a"])
    for bad, match in ((with_cuts(publisher_cut_floor="2020"), "date"),
                       (with_cuts(field_filter_floor="2020-01-01"), "field_filter_groups"),
                       (with_cuts(field_filter_groups=["a"]), "field_filter_floor"),
                       (with_cuts(field_filter_floor="2020-01-01", field_filter_groups=["b"]), "broad"),
                       (with_cuts(field_filter_floor="2020-01-01", field_filter_groups=["nope"]), "nope")):
        with pytest.raises(ValueError, match=match):
            ChannelsConfig.model_validate(bad)


# ─────────────────────────────────────────────────────────────────────────────
# D654 (b): a listed broad group is swept once more before `keyword_cuts.field_filter_floor`, as a second plan
# entry without the CS-field filter and with its own `to` (its own coverage key); the loop has no per-group case.
# ─────────────────────────────────────────────────────────────────────────────
FIELD_FLOOR = "2020-01-01"
PRE_TO = "2019-12-31"
UMB = "group:umbrella"


@pytest.fixture
def field_floor_cfg(monkeypatch: Any) -> list[dict[str, Any]]:
    """``stack.config.load`` with openalex's keyword_cuts = only the field floor on umbrella (no source or AI-term
    cuts, so the harness works are kept); returns umbrella's two plan entries under it."""
    import dataclasses

    import stack.config as sc

    real = sc.load
    cuts = sc.KeywordCuts(field_filter_floor=FIELD_FLOOR, field_filter_groups=["umbrella"])

    def patched(*a: Any, **kw: Any) -> Any:
        got = real(*a, **kw)
        chans = [ch.model_copy(update={"keyword_cuts": cuts if ch.id == "openalex" else None})
                 for ch in got.channels.channels]
        return dataclasses.replace(got, channels=got.channels.model_copy(update={"channels": chans}))

    monkeypatch.setattr(sc, "load", patched)
    return [p for p in openalex.plan_groups(sc.load(CONFIG_DIR)) if p["query"] == UMB]


def _umb(item: dict[str, Any], frm: str, to: str | None = None, *, field: bool = True) -> dict[str, Any]:
    return _params(openalex.group_filter(item["terms"], frm, field, to_date=to))


def _cov(store: pathlib.Path) -> list[tuple[str, str, str, bool]]:
    """``(start, end, status, field filter in the key)`` of every coverage row."""
    return sorted((r["window_start_at"].date().isoformat(), r["window_end_at"].date().isoformat(), r["status"],
                   "primary_topic.field.id" in r["query"]) for r in _view(store, "coverage.openalex"))


def test_field_floor_is_a_second_plan_entry() -> None:
    cfg = load(CONFIG_DIR)
    plan = openalex.plan_groups(cfg)
    main, pre = [p for p in plan if p["query"] == UMB]   # the repo relaxes umbrella only
    assert [p["query"] for p in plan if "to" in p] == [UMB] and len(plan) == len({p["query"] for p in plan}) + 1
    assert main["broad"] and "to" not in main
    assert pre == {**main, "broad": False, "to": PRE_TO}
    kmain, kpre = openalex.coverage_query(main, None), openalex.coverage_query(pre, PRE_TO)
    assert "field_floor" not in kmain and "field_floor" not in kpre and kmain != kpre
    assert "primary_topic.field.id" in kmain["filter"] and "to_publication_date" not in kmain["filter"]
    assert "primary_topic.field.id" not in kpre["filter"] and f"to_publication_date:{PRE_TO}" in kpre["filter"]
    today = __import__("datetime").date(2026, 10, 21)
    assert openalex.entry_window(main, None, today) == (None, "2026-10-21")
    assert openalex.entry_window(pre, None, today) == (PRE_TO, PRE_TO)
    assert openalex.entry_window(pre, "2018-06-30", today) == ("2018-06-30", "2018-06-30")  # the earlier end wins
    assert openalex.entry_window(main, "2018-06-30", today) == ("2018-06-30", "2018-06-30")


def test_field_floor_first_pass_from_2015_is_two_ok_windows(tmp_path: pathlib.Path, oa_env: Any,  # noqa: F811
                                                           field_floor_cfg: list[dict[str, Any]]) -> None:
    """A first pass from 2015: umbrella from 2015 with the field filter (as every broad group), then its second
    entry [2015-01-01, 2019-12-31] without it; two keys, each written ok over its own window."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    main, _pre = field_floor_cfg
    fake.add(API, _umb(main, "2015-01-01"), json=_page([_work(2, doi=None)]))
    fake.add(API, _umb(main, "2015-01-01", PRE_TO, field=False), json=_page([_work(1, doi=None)]))
    rec = run_line(store, _oa_plan(queries=[UMB], **{"from": "2015-01-01"}), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert fake.urls() == [fake.key("GET", API, _umb(main, "2015-01-01"))[1],
                           fake.key("GET", API, _umb(main, "2015-01-01", PRE_TO, field=False))[1]]
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert _cov(store) == [("2015-01-01", PRE_TO, "ok", False), ("2015-01-01", today, "ok", True)]
    keys = {r["query"]: json.loads(r["query"]) for r in _view(store, "coverage.openalex")}
    assert sorted(keys.values(), key=lambda k: k["filter"]) == sorted(
        [openalex.coverage_query(main, None), openalex.coverage_query(_pre, PRE_TO)], key=lambda k: k["filter"])


def test_field_floor_holes_before_and_after_the_floor(tmp_path: pathlib.Path, oa_env: Any,  # noqa: F811
                                                      field_floor_cfg: list[dict[str, Any]]) -> None:
    """A floor moved back: each entry asks its own hole as a bounded window (umbrella with the filter, the second
    entry without it); the second entry, a bounded window, then asks its whole window again, as any bounded window
    does after a hole (no watermark resume). Under a floor after the second entry's `to` that entry is not asked
    at all, and once the floor moves before it, it is asked as a first pass."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    main, _pre = field_floor_cfg
    today = dt.datetime.now(dt.UTC).date().isoformat()
    # (1) first pass from 2018, then from 2015: one hole [2015, 2018) per key
    store = tmp_path / "s1"
    fake.add(API, _umb(main, "2018-01-01"), json=_page([]))
    fake.add(API, _umb(main, "2018-01-01", PRE_TO, field=False), json=_page([]))
    fake.add(API, _umb(main, "2015-01-01", "2017-12-31"), json=_page([]))
    fake.add(API, _umb(main, "2015-01-01", "2017-12-31", field=False), json=_page([_work(1, doi=None)]))
    fake.add(API, _umb(main, "2015-01-01", PRE_TO, field=False), json=_page([_work(1, doi=None)]))
    assert run_line(store, _oa_plan(queries=[UMB], **{"from": "2018-01-01"}), "collect.openalex").status == "ok"
    n = len(fake.calls)
    rec = run_line(store, _oa_plan(queries=[UMB], **{"from": "2015-01-01"}), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert fake.urls()[n:] == [fake.key("GET", API, _umb(main, "2015-01-01", "2017-12-31"))[1],
                               fake.key("GET", API, _umb(main, "2015-01-01", "2017-12-31", field=False))[1],
                               fake.key("GET", API, _umb(main, "2015-01-01", PRE_TO, field=False))[1]]
    assert rec.counts["holes"] == 2
    assert _cov(store) == [("2015-01-01", "2018-01-01", "ok", False), ("2015-01-01", "2018-01-01", "ok", True),
                           ("2015-01-01", PRE_TO, "ok", False), ("2018-01-01", PRE_TO, "ok", False),
                           ("2018-01-01", today, "ok", True)]
    # (2) first pass from 2021: the second entry ends before the floor, so it is not asked and has no row; from
    # 2015 umbrella asks its hole [2015, 2021) and the second entry its first pass
    store = tmp_path / "s2"
    fake.add(API, _umb(main, "2021-01-01"), json=_page([]))
    fake.add(API, _umb(main, "2015-01-01", "2020-12-31"), json=_page([_work(2, doi=None)]))
    fake.add(API, _umb(main, "2015-01-01", PRE_TO, field=False), json=_page([]))
    n = len(fake.calls)
    assert run_line(store, _oa_plan(queries=[UMB], **{"from": "2021-01-01"}), "collect.openalex").status == "ok"
    assert fake.urls()[n:] == [fake.key("GET", API, _umb(main, "2021-01-01"))[1]]
    assert _cov(store) == [("2021-01-01", today, "ok", True)]
    n = len(fake.calls)
    rec = run_line(store, _oa_plan(queries=[UMB], **{"from": "2015-01-01"}), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert fake.urls()[n:] == [fake.key("GET", API, _umb(main, "2015-01-01", "2020-12-31"))[1],
                               fake.key("GET", API, _umb(main, "2015-01-01", PRE_TO, field=False))[1]]
    assert rec.counts["holes"] == 1
    assert _cov(store) == [("2015-01-01", PRE_TO, "ok", False), ("2015-01-01", "2021-01-01", "ok", True),
                           ("2021-01-01", today, "ok", True)]


def test_field_floor_resumed_tail_keeps_the_filter(tmp_path: pathlib.Path, oa_env: Any,  # noqa: F811
                                                   field_floor_cfg: list[dict[str, Any]], monkeypatch: Any) -> None:
    """The next day umbrella's tail resumes from its look-back start (D582) with the field filter; the second
    entry's bounded window is covered, so it is not asked."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    main, _pre = field_floor_cfg
    fake.add(API, _umb(main, "2015-01-01"), json=_page([]))
    fake.add(API, _umb(main, "2015-01-01", PRE_TO, field=False), json=_page([]))
    opts = {"queries": [UMB], "from": "2015-01-01", "lookback_days": 2}
    assert run_line(store, _oa_plan(**opts), "collect.openalex").status == "ok"
    t0 = dt.datetime.now(dt.UTC)
    monkeypatch.setattr("stack.collect.base.utcnow", lambda: t0 + dt.timedelta(days=1))
    resumed = _umb(main, (t0.date() - dt.timedelta(days=2)).isoformat())
    fake.add(API, resumed, json=_page([]))
    n = len(fake.calls)
    rec = run_line(store, _oa_plan(**opts), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert fake.urls()[n:] == [fake.key("GET", API, resumed)[1]]
    assert "primary_topic.field.id%3A17" in fake.urls()[n]


def test_field_floor_failed_second_entry_claims_nothing(tmp_path: pathlib.Path, oa_env: Any,  # noqa: F811
                                                        field_floor_cfg: list[dict[str, Any]]) -> None:
    """The second entry fails (5xx through the ladder): its row claims nothing, umbrella's own window is ok; the
    next run asks only the second entry again."""
    import datetime as dt

    from tests.synth.records import run_line

    fake, _ = oa_env
    store = tmp_path / "store"
    main, _pre = field_floor_cfg
    pre = _umb(main, "2015-01-01", PRE_TO, field=False)
    fake.add(API, _umb(main, "2015-01-01"), json=_page([]))
    fake.add(API, pre, json=_page([_work(1, doi=None)]))
    fake.inject(API, pre, status=503, times=4)
    rec = run_line(store, _oa_plan(queries=[UMB], **{"from": "2015-01-01"}), "collect.openalex")
    assert rec.status == "ok", rec.error
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert _cov(store) == [("2015-01-01", "2015-01-01", "failed_transient", False), ("2015-01-01", today, "ok", True)]
    n = len(fake.calls)
    rec = run_line(store, _oa_plan(queries=[UMB], **{"from": "2015-01-01"}), "collect.openalex")
    assert rec.status == "ok", rec.error
    assert fake.urls()[n:] == [fake.key("GET", API, pre)[1]]
    assert ("2015-01-01", PRE_TO, "ok", False) in _cov(store)


# ---------------------------------------------------------------- the venue allowlist (the Chinese-coverage work)
ACL, AAAI, IEEE = [("doi", "10.18653/v1/2024.acl-long.1")], [("doi", "10.1609/aaai.v38i19.30150")], [("doi", "10.1109/x")]


def test_venue_allowlist_keeps_acl_and_aaai_through_the_publisher_cut() -> None:
    both = ["publisher", "self_publication"]
    assert rules.source_class(ACL) == "publisher" and rules.source_class(ACL, keep_venues=True) == "venue"
    assert rules.source_class(IEEE, keep_venues=True) == "publisher"
    for doi in ("10.24963/ijcai.2024/1", "10.14722/ndss.2025.240001", "10.1371/journal.pone.0182950"):  # D753
        assert rules.source_class([("doi", doi)], keep_venues=True) == "venue", doi
    assert rules.keyword_cut(ACL, "LLM", drop_sources=both) == "publisher"            # off: as before
    assert rules.keyword_cut(ACL, "LLM", drop_sources=both, keep_venues=True) is None
    assert rules.keyword_cut(AAAI, "LLM", drop_sources=both, keep_venues=True) is None
    assert rules.keyword_cut(IEEE, "LLM", drop_sources=both, keep_venues=True) == "publisher"
    assert rules.keyword_cut(ACL, "pavement", drop_sources=both, ai_term=True, keep_venues=True) == "ai_term"


def test_venue_allowlist_is_in_the_coverage_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """On both channels every cut group's key carries the allowlist digest: switching it on, or editing the list,
    is a new key (one re-sweep from the floor); without the publisher cut there is nothing to allow."""
    cfg = load(CONFIG_DIR)
    oa = {p["id"]: p for p in openalex.plan_groups(cfg)}
    sp = {p["id"]: p for p in s2.plan_groups(cfg)}
    assert all(p["keep_venues"] for p in oa.values()) and all(p["keep_venues"] for p in sp.values())
    key = openalex.coverage_query(oa["umbrella"], None)
    assert key["venues"] == rules.venue_allowlist_digest() and len(key["venues"]) == 12
    assert "venues" not in openalex.coverage_query({**oa["umbrella"], "keep_venues": False}, None)
    assert s2.coverage_query(sp["umbrella"])["venues"] == key["venues"]
    monkeypatch.setattr(rules, "VENUE_DOI_PREFIXES", (*rules.VENUE_DOI_PREFIXES, ("10.3389", "Frontiers")))
    assert openalex.coverage_query(oa["umbrella"], None)["venues"] != key["venues"]


def test_venue_hits_are_kept_and_counted_on_both_collectors() -> None:
    item = {"kind": "group", "id": "jailbreak", "drop_sources": ["publisher", "self_publication"], "ai_term": False,
            "cuts": ["publisher", "self_publication"], "keep_venues": True}
    w = {**_work(1, doi="https://doi.org/10.18653/v1/2024.acl-long.1"),
         "abstract_inverted_index": {"LLM": [0], "jailbreak": [1]}}
    keys = openalex.work_keys(w)
    assert openalex.work_cut(w, keys, item) is None and openalex.work_venue_kept(w, keys, item)
    assert openalex.work_cut(w, keys, {**item, "keep_venues": False}) == "publisher"
    p = {"paperId": "b" * 40, "externalIds": {"DOI": "10.1609/aaai.v38i19.30150"}, "title": "Jailbreaking LLMs",
         "year": 2024, "publicationDate": "2024-03-24", "venue": "AAAI Conference on Artificial Intelligence"}
    assert s2.paper_cut(p, item) is None and s2.paper_venue_kept(p, item)
    assert s2.paper_cut(p, {**item, "keep_venues": False}) == "publisher"


#: Three Semantic Scholar hits of `llm-safety` ("safety evaluation") in PLOS journals, 2026-10-30, and two of the
#: same window the allowlist is for (IJCAI, AAAI/AIES).
PLOS_BIOMEDICAL = [("10.1371/journal.pone.0351511", "Acute and prolonged effects of anti-malarial drugs on mitochondrial "
                    "respiration in atrial cardiomyocytes"),
                   ("10.1371/journal.pgph.0006142", "Strengthening post-market COVID-19 vaccine safety surveillance and "
                    "research: An interpretive description"),
                   ("10.1371/journal.pone.0359514", "Polylactic co-glycolic acid (PLGA)-based nanoparticles "
                    "encapsulating Persea americana seed extract: a safety evaluation")]
VENUE_AI = [("10.24963/ijcai.2026/63", "Explaining Jailbreaks: Structured and Interpretable Safety Assessment for "
             "Large Language Models"),
            ("10.1609/aies.v9i2.43206", "Beyond Guardrails: Dual Failure Modes in LLM Safety as Inverse Control-System "
             "Errors")]


def test_venue_ai_term_keeps_only_ai_hits_of_an_allowlisted_venue() -> None:
    """`keyword_cuts.venue_ai_term`: the allowlist keeps a venue hit only with an AI term; one without is a
    `publisher` work again (the dated pre-2020 exception still applies to it, as to any publisher work)."""
    both = ["publisher", "self_publication"]
    for doi, title in PLOS_BIOMEDICAL:
        keys = [("doi", doi)]
        assert rules.keyword_cut(keys, title, drop_sources=both, keep_venues=True) is None  # unguarded: kept
        assert rules.keyword_cut(keys, title, drop_sources=both, keep_venues=True, venue_ai_term=True) == "publisher"
    for doi, title in VENUE_AI:
        assert rules.keyword_cut([("doi", doi)], title, drop_sources=both, keep_venues=True, venue_ai_term=True) is None
    assert not rules.venue_allowlist_applies("drug safety", keep_venues=False)
    assert rules.venue_allowlist_applies("drug safety", keep_venues=True)
    assert not rules.venue_allowlist_applies("drug safety", keep_venues=True, venue_ai_term=True)
    assert rules.venue_allowlist_applies("大模型安全评测", keep_venues=True, venue_ai_term=True)


def test_s2_sets_venue_ai_term_and_drops_plos_biomedical_hits() -> None:
    """Semantic Scholar's group queries have no field filter: the channel sets `venue_ai_term`, its plan entries carry
    it, the key carries the vocabulary's digest, and the PLOS hits of llm-safety are cut and counted while the
    IJCAI/AAAI ones stay; OpenAlex (CS-field filter) leaves it unset and keeps its keys."""
    cfg = load(CONFIG_DIR)
    sq = {p["id"]: p for p in s2.plan_groups(cfg)}["llm-safety"]
    assert sq["venue_ai_term"] and sq["keep_venues"]
    assert s2.coverage_query(sq)["venue_ai_terms"] == rules.ai_term_digest()
    for doi, title in PLOS_BIOMEDICAL:
        p = {"paperId": "c" * 40, "externalIds": {"DOI": doi}, "title": title, "year": 2026,
             "publicationDate": "2026-10-01", "venue": "PLoS ONE"}
        assert s2.paper_cut(p, sq) == "publisher" and not s2.paper_venue_kept(p, sq)
        assert s2.paper_venue_unguarded(p, sq) and not s2.paper_venue_unguarded(p, {**sq, "venue_ai_term": False})
        assert s2.paper_cut(p, {**sq, "venue_ai_term": False}) is None  # the old behaviour
    for doi, title in VENUE_AI:
        p = {"paperId": "d" * 40, "externalIds": {"DOI": doi}, "title": title, "year": 2026,
             "publicationDate": "2026-10-01", "venue": "IJCAI"}
        assert s2.paper_cut(p, sq) is None and s2.paper_venue_kept(p, sq) and not s2.paper_venue_unguarded(p, sq)
    oa = {p["id"]: p for p in openalex.plan_groups(cfg)}
    assert not any(p["venue_ai_term"] for p in oa.values())
    assert not any("venue_ai_terms" in openalex.coverage_query(p, None) for p in oa.values())
    w = {**_work(2, doi="https://doi.org/" + PLOS_BIOMEDICAL[0][0]), "title": PLOS_BIOMEDICAL[0][1]}
    keys = openalex.work_keys(w)
    item = {**oa["llm-safety"], "venue_ai_term": True}
    assert openalex.work_cut(w, keys, oa["llm-safety"]) is None and openalex.work_venue_kept(w, keys, oa["llm-safety"])
    assert openalex.work_cut(w, keys, item) == "publisher" and not openalex.work_venue_kept(w, keys, item)


def test_venue_ai_term_needs_keep_venues() -> None:
    from stack.config import KeywordCuts

    assert KeywordCuts(drop_sources=["publisher"], keep_venues=True, venue_ai_term=True).venue_ai_term
    with pytest.raises(ValueError, match="needs keep_venues"):
        KeywordCuts(drop_sources=["publisher"], venue_ai_term=True)


def test_llm_safety_group_is_one_new_key_per_collector() -> None:
    """The Chinese-coverage work: OpenAlex asks the phrases quoted with the CS-field filter (`broad`), S2 as phrases; both under the
    source cuts, the pre-2020 bypass and the venue allowlist; one new key `group:llm-safety` each."""
    cfg = load(CONFIG_DIR)
    oa = {p["id"]: p for p in openalex.plan_groups(cfg)}["llm-safety"]
    key = openalex.coverage_query(oa, None)
    assert key["query"] == "group:llm-safety" and "primary_topic.field.id:17" in key["filter"]
    assert '"safe reinforcement learning"' in key["filter"] and not oa["ai_term"] and oa["keep_venues"]
    sq = {p["id"]: p for p in s2.plan_groups(cfg)}["llm-safety"]
    assert s2.coverage_query(sq)["s2_query"].startswith('"LLM safety" | "safety benchmark"')
    assert set(s2.coverage_query(sq)) == {"query", "s2_query", "cuts", "bypass", "venues", "venue_ai_terms"}


def test_ai_term_vocabulary_is_in_the_key_of_ai_term_groups_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """D751: an `ai_term` group's key carries the vocabulary digest, so an edited vocabulary re-sweeps it; the other
    groups' keys do not. Every such group also carries the allowlist digest, so the two land in one re-sweep."""
    cfg = load(CONFIG_DIR)
    oa = {p["id"]: p for p in openalex.plan_groups(cfg)}
    key = openalex.coverage_query(oa["mech-interp"], None)
    assert key["ai_terms"] == rules.ai_term_digest() and "venues" in key
    assert "ai_terms" not in openalex.coverage_query(oa["umbrella"], None)
    assert all("venues" in openalex.coverage_query(p, None) for p in oa.values() if p["ai_term"])
    monkeypatch.setattr(rules, "AI_TERMS_ZH", (*rules.AI_TERMS_ZH, ("zh x", "某词")))
    assert openalex.coverage_query(oa["mech-interp"], None)["ai_terms"] != key["ai_terms"]
    assert "ai_terms" not in s2.coverage_query({p["id"]: p for p in s2.plan_groups(cfg)}["umbrella"])
