"""``stack.identity`` (round 5, T-ID): the key adders, cross-group keys and matchers as pure functions, the
alternate-URL rules of ``stack.urls``, and the adders through ``Collector.observe`` and ``stack migrate rekey``.
Gather-level behaviour (edges, provenance, overrides, idempotence) is in ``tests/test_gather_identity.py``."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from stack import identity, urls
from stack.config import load as load_config
from stack.migrate import rekey_identity

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


@dataclass(frozen=True)
class Obs:
    source_id: str
    channel: str
    group: str
    title: str | None
    authors: tuple[str, ...] = ()
    days: float | None = 0
    doc_type: str | None = None
    keys: tuple[tuple[str, str], ...] = ()
    row: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        self.row["metadata"] = {
            "title": self.title, "doc_type": self.doc_type,
            "authors": [{"name": a} for a in self.authors],
            "published_at": None if self.days is None else T0 + dt.timedelta(days=self.days)}


def pairs(*obs: Obs) -> list[tuple[str, str, str]]:
    by = {o.source_id: o for o in obs}
    out, _ = identity.match_pairs(by, lambda s: (s,))
    return out


# ---------------------------------------------------------------- key adders
def test_lw_linkpost_to_a_blog_post_adds_its_url_key() -> None:
    rec = {"_id": "abc", "url": "https://blog.redwoodresearch.org/p/paper-distillation?utm_source=x"}
    assert identity.added_keys("lesswrong", "forum", rec, [("fm_post", "abc")]) == [
        ("url", "https://blog.redwoodresearch.org/p/paper-distillation")]


@pytest.mark.parametrize("target", [
    "https://arxiv.org/abs/2610.00001", "https://doi.org/10.1145/3846173", "https://openreview.net/forum?id=xyz",
    "https://example.org/paper.pdf", "https://x.com/someone/status/123", "https://www.nytimes.com/2026/01/01/a.html",
    "https://someones-blog.org/", "https://someones-blog.substack.com",
    None, "", "not a url"])
def test_lw_linkpost_to_a_paper_news_tweet_or_nothing_adds_no_key(target: Any) -> None:
    assert identity.added_keys("lesswrong", "forum", {"_id": "abc", "url": target}, [("fm_post", "abc")]) == []


def test_lw_linkpost_to_itself_adds_nothing_and_to_another_forum_post_adds_its_id() -> None:
    own = [("fm_post", "abc")]
    assert identity.added_keys("lesswrong", "forum", {"url": "https://www.lesswrong.com/posts/abc/slug"}, own) == []
    assert identity.added_keys("lesswrong", "forum", {"url": "https://forum.effectivealtruism.org/posts/xyz/s"},
                               own) == [("fm_post", "xyz")]


def test_adders_are_scoped_by_group_and_channel() -> None:
    rec = {"url": "https://blog.example.org/p/post"}
    assert identity.added_keys("manual_forum", "forum", rec) == []           # not a linkpost channel
    assert identity.added_keys("openai_blog", "url", rec) == []              # url group: no linkpost adder
    assert identity.added_keys("lesswrong", "forum", None) == []             # no record


def test_feed_guid_other_than_link_adds_its_key() -> None:
    rec = {"link": "https://blog.redwoodresearch.org/p/x", "guid": "https://redwoodresearch.substack.com/p/x"}
    assert identity.added_keys("redwood_blog", "url", rec, [("url", "https://blog.redwoodresearch.org/p/x")]) == [
        ("url", "https://redwoodresearch.substack.com/p/x")]
    same = {"link": "https://thezvi.substack.com/p/x", "guid": "https://thezvi.substack.com/p/x"}
    assert identity.added_keys("zvi_substack", "url", same, [("url", "https://thezvi.substack.com/p/x")]) == []
    assert identity.added_keys("zvi_substack", "url", {"link": "https://a.org/x", "guid": "tag:a.org,2026:1"}) == []


def test_openalex_arxiv_location_adds_the_arxiv_key_and_its_bridge_doi() -> None:
    rec = {"id": "https://openalex.org/W1", "locations": [
        {"landing_page_url": "https://doi.org/10.1145/3846173", "pdf_url": None},
        {"landing_page_url": "http://arxiv.org/abs/2601.01234v2", "pdf_url": "https://arxiv.org/pdf/2601.01234v2"},
        {"landing_page_url": "https://zenodo.org/records/1", "pdf_url": None}, "junk"]}
    got = identity.added_keys("openalex", "paper", rec, [("openalex", "w1"), ("doi", "10.1145/3846173")])
    assert ("arxiv", "2601.01234") in got and ("doi", "10.48550/arxiv.2601.01234") in got
    assert all(k[0] in ("arxiv", "doi") for k in got) and ("doi", "10.1145/3846173") not in got
    assert identity.added_keys("semantic_scholar", "paper", rec) == []      # openalex records only


def test_url_alternates_forummagnum_hosts() -> None:
    assert urls.alternate_url_keys("https://www.alignmentforum.org/posts/AbC123/slug") == (
        "https://lesswrong.com/posts/AbC123",)
    assert urls.alternate_url_keys("https://greaterwrong.com/posts/AbC123") == ("https://alignmentforum.org/posts/AbC123",)
    assert urls.alternate_url_keys("https://example.org/posts/AbC123") == ()
    assert urls.alternate_url_keys("not a url") == ()
    # folded forms are one key already, never alternates
    assert urls.url_key("https://arxiv.org/pdf/2601.01234v2") == urls.url_key("https://arxiv.org/html/2601.01234")
    assert urls.url_key("https://edition.cnn.com/2026/a") == urls.url_key("https://www.cnn.com/2026/a")



#: (a linkpost's former address, the post's current address): one example per MOVED_POST_PATHS family, each a
#: crosspost pair of the documents head of 2026-10-23 (D772).
MOVED = [
    ("https://openai.com/blog/introducing-superalignment", "https://openai.com/index/introducing-superalignment"),
    ("https://openai.com/research/improving-mathematical-reasoning-with-process-supervision",
     "https://openai.com/index/improving-mathematical-reasoning-with-process-supervision"),
    ("https://redwoodresearch.substack.com/p/takeaways-from-sketching-a-control",
     "https://blog.redwoodresearch.org/p/takeaways-from-sketching-a-control"),
    ("https://apolloresearch.ai/blog/a-starter-guide-for-evals", "https://apolloresearch.ai/science/a-starter-guide-for-evals"),
    ("https://evals.alignment.org/blog/2023-09-26-rsp", "https://metr.org/blog/2023-09-26-rsp"),
    ("https://far.ai/news/defense-in-depth", "https://www.far.ai/blog/defense-in-depth"),
    ("https://aisi.gov.uk/work/early-lessons-from-evaluating-frontier-ai-systems",
     "https://www.aisi.gov.uk/blog/early-lessons-from-evaluating-frontier-ai-systems"),
    ("https://deepmind.google/discover/blog/introducing-the-frontier-safety-framework",
     "https://deepmind.google/blog/introducing-the-frontier-safety-framework"),
    ("https://www.deepmind.com/blog/discovering-novel-algorithms-with-alphatensor",
     "https://deepmind.google/blog/discovering-novel-algorithms-with-alphatensor"),
    ("https://epochai.org/blog/trading-off-compute-in-training-and-inference",
     "https://epoch.ai/publications/trading-off-compute-in-training-and-inference"),
]


@pytest.mark.parametrize("old, new", MOVED)
def test_url_alternates_moved_post_paths(old: str, new: str) -> None:
    assert urls.url_key(old) in urls.alternate_url_keys(new)
    assert urls.url_key(new) in urls.alternate_url_keys(old)
    # the post page's own row gets the former key from the url_alternates adder, so the linkpost's bridge key joins it
    assert ("url", urls.url_key(old)) in identity.added_keys("openai_blog", "url", {}, [("url", urls.url_key(new))])


def test_moved_post_paths_take_one_post_segment_only() -> None:
    for u in ("https://openai.com/blog/", "https://openai.com/blog", "https://openai.com/index/a/b",
              "https://far.ai/news/", "https://example.org/blog/x", "https://aisi.gov.uk/research/x"):
        assert urls.alternate_url_keys(u) == (), u
    assert {n for n, _ in urls.MOVED_POST_PATHS} == {
        "openai_index", "redwood_custom_domain", "apollo_science", "metr_renamed", "far_blog", "aisi_blog",
        "deepmind_blog", "epoch_publications"}

def test_adder_fields_are_in_the_channel_projection() -> None:
    """A key derived from a field outside the record projection would go stale when only that field changes
    (an unchanged digest writes no row)."""
    chans = {c.id: c for c in load_config().channels.channels}
    assert "url" in chans["lesswrong"].record_projection
    assert "locations" in chans["openalex"].record_projection


# ---------------------------------------------------------------- cross-group keys
def test_bridge_keys_only_forum_url() -> None:
    assert identity.bridge_keys("forum", [("fm_post", "a"), ("url", "https://b.org/p")]) == (
        ("url", "url", "https://b.org/p"),)
    assert identity.bridge_keys("forum", [("url", "https://b.org/p")], non_merging=["url"]) == ()
    assert identity.bridge_keys("paper", [("url", "https://b.org/p")]) == ()


# ---------------------------------------------------------------- normalisation
def test_norm_title_and_surname() -> None:
    assert identity.norm_title("[Paper] Distillation for  Incrimination: a Study!") == \
        "distillation for incrimination a study"
    assert identity.norm_title("Ünïcödé Títle") == "unicode title"
    assert identity.surname("Mowshowitz, Zvi") == "mowshowitz"
    assert identity.surname("Martin Luther King Jr.") == "king"
    assert identity.surname("J") == "" and identity.surname(None) == ""
    assert identity.same_author(["Zvi"], ["Zvi Mowshowitz"])
    assert identity.same_author(["Stuart_Armstrong"], ["Stuart Armstrong"])
    assert not identity.same_author(["Jo"], ["Jo Smith"])     # a two-letter name never matches alone
    assert not identity.same_author(["Alice Smith"], ["Bob Smith"])


def test_norm_title_keeps_non_latin_scripts() -> None:
    """The Chinese-coverage work: a Chinese or Russian title normalised to '' (every non-ASCII letter was a separator), so it took
    no part in matching, and titles differing only in Japanese or Korean text collided on their Latin residue."""
    assert identity.norm_title("人工智能安全治理框架") == "人 工 智 能 安 全 治 理 框 架"  # one token per Han char
    assert identity.norm_title("Безопасность ИИ: обзор") == "безопасность ии обзор"
    assert identity.norm_title("대형 언어 모델의 안전성") == "대형 언어 모델의 안전성"      # syllables, not jamo
    a = identity.norm_title("長い実行トレースからの簡潔なモデルの学習【JST・京大機械翻訳】")
    b = identity.norm_title("支配集合の近似のパラメータ化計算量について【JST・京大機械翻訳】")
    assert a != b and a.endswith("jst 京 大 機 械 翻 訳")  # both were 'jst'
    # Latin and Greek titles are unchanged (Greek is mostly maths: "$μ$J" and "µJ" of one paper)
    assert identity.norm_title("i-FlatCam: 91.49 $μ$J/Frame") == identity.norm_title("i-FlatCam: 91.49 µJ/Frame") \
        == "i flatcam 91 49 j frame"
    assert identity.norm_title("Straße ε-greedy") == "stra e greedy"
    assert identity.surname("王伟") == "王伟" and identity.author_key("王伟") == ("王伟", "")


@pytest.mark.parametrize(("name", "latin"), [
    ("曹东兴 Cao Dongxing", "Cao Dongxing"),
    ("杜晨林 DU Chenlin", "DU Chenlin"),
    ("Lei Zhang (张磊)", "Lei Zhang"),
    ("Chun Yu 揭春雨", "Chun Yu"),
    ("Jixuan Cai, 蔡纪烜", "Jixuan Cai"),   # the comma separates two renderings, not surname and given names
    ("蔡纪烜, Jixuan Cai", "Jixuan Cai"),
    ("Cai, Jixuan 蔡纪烜", "Cai, Jixuan"),
    ("Wei Wang 王伟", "Wei Wang"),
])
def test_two_script_names_key_by_their_latin_tokens(name: str, latin: str) -> None:
    """A byline with the name in Latin letters and in another script keys as the Latin-only byline (as before
    D744 kept the non-Latin letters), so its paper still matches a Latin-only record of the same first author;
    the name tokens of the post match keep both scripts (a subset test, so both renderings still match)."""
    assert identity.author_key(name) == identity.author_key(latin) != ("", "")
    assert identity.surname(name) == identity.surname(latin)
    assert identity._name_tokens(latin) < identity._name_tokens(name)
    assert identity.same_author([name], [latin])


def test_one_script_names_key_as_before_and_null_is_dropped_beside_another_script() -> None:
    assert identity.author_key("曹东兴 Cao Dongxing") == ("dongxing", "c")   # the key before D744
    assert identity.author_key("章成志") == ("章成志", "") and identity.author_key("王 小明") == ("小明", "王")
    assert identity.author_key("Андрей Владимирович Кузнецов") == ("кузнецов", "а")
    assert identity.author_key("Lei Zhang (Tsinghua)") == ("tsinghua", "l")  # Latin only: unchanged
    assert identity.author_key("Ke Гонг") == identity.author_key("Ke") == ("ke", "")  # as before D744
    assert identity.author_key("null Smith") == ("smith", "n")
    # a missing name part written "null" beside a Han name is no Latin name: the Han name keys alone
    assert identity.author_key("null 章成志") == identity.author_key("章成志")
    assert identity.same_author(["null 章成志"], ["章成志"])
    # Cyrillic look-alike letters inside a Latin token are one token (not a second script): only the
    # Cyrillic-only token `А` is dropped
    assert identity.author_key("Мikhail А. Proskurnin") == ("proskurnin", "м")
    assert identity.author_key("Оleksandra Molloy") == ("molloy", "о")


def test_chinese_paper_records_match_by_title_and_author() -> None:
    a = Obs("s1", "openalex", "paper", "面向大语言模型的越狱攻击与防御综述", ("李明",), 0)
    b = Obs("s2", "manual_paper", "paper", "面向大语言模型的越狱攻击与防御综述。", ("李明",), 30)
    c = Obs("s3", "openalex", "paper", "面向多模态大模型的越狱攻击综述", ("李明",), 0)
    assert pairs(a, b, c) == [("s1", "s2", "paper_title_author")]


# ---------------------------------------------------------------- matchers
def test_paper_matches_first_author_title_within_a_year() -> None:
    a = Obs("s1", "openalex", "paper", "Sleeper Agents: Training Deceptive LLMs", ("Evan Hubinger", "B"), 0)
    b = Obs("s2", "semantic_scholar", "paper", "Sleeper agents — training deceptive LLMs.", ("E. Hubinger",), 200)
    c = Obs("s3", "openalex", "paper", "Sleeper Agents: Training Deceptive LLMs", ("Someone Else",), 0)
    d = Obs("s4", "openalex", "paper", "Sleeper Agents: Training Deceptive LLMs", ("Evan Hubinger",), 600)
    assert pairs(a, b, c, d) == [("s1", "s2", "paper_title_author")]


def test_paper_first_authors_sharing_only_a_surname_never_match() -> None:
    """S25-57 / L3-12: two works of one title by Wei Wang and Lei Wang are two works; an initial-only
    or surname-first byline of the same person still matches."""
    wei = Obs("s1", "openalex", "paper", "Safe Reinforcement Learning via Shielding", ("Wei Wang",), 0,
              keys=(("doi", "10.1/a"),))
    lei = Obs("s2", "openalex", "paper", "Safe Reinforcement Learning via Shielding", ("Lei Wang",), 200,
              keys=(("doi", "10.2/b"),))
    assert pairs(wei, lei) == []
    for alias in ("W. Wang", "Wang, Wei", "WANG, W."):
        same = Obs("s3", "semantic_scholar", "paper", "Safe Reinforcement Learning via Shielding", (alias,), 100)
        assert pairs(wei, same) == [("s1", "s3", "paper_title_author")], alias
    assert identity.author_key("Martin Luther King Jr.") == ("king", "m")
    assert identity.author_key("Wang") == ("wang", "") and identity.author_key(None) == ("", "")


def test_paper_two_arxiv_ids_never_match_and_undated_never_match() -> None:
    a = Obs("s1", "arxiv", "paper", "A Long Enough Title", ("Ann Lee",), 0, keys=(("arxiv", "2601.00001"),))
    b = Obs("s2", "arxiv", "paper", "A Long Enough Title", ("Ann Lee",), 1, keys=(("arxiv", "2601.00002"),))
    c = Obs("s3", "openalex", "paper", "A Long Enough Title", ("Ann Lee",), None)
    assert pairs(a, b, c) == []
    d = Obs("s4", "openalex", "paper", "A Long Enough Title", ("Ann Lee",), 3, keys=(("doi", "10.1/x"),))
    assert pairs(a, d) == [("s1", "s4", "paper_title_author")]


def test_paper_never_matches_a_post_and_generic_or_short_titles_are_skipped() -> None:
    p = Obs("s1", "arxiv", "paper", "Why Language Models Hallucinate", ("Adam Kalai",), 0)
    q = Obs("s2", "openai_blog", "url", "Why Language Models Hallucinate", ("Adam Kalai",), 0, doc_type="lab_blog")
    assert pairs(p, q) == []
    g1 = Obs("s3", "openalex", "paper", "Table of Contents", ("Ed Itor",), 0)
    g2 = Obs("s4", "openalex", "paper", "Table of Contents", ("Ed Itor",), 0)
    s1 = Obs("s5", "openalex", "paper", "Deep Learning", ("Yann LeCun",), 0)
    s2 = Obs("s6", "openalex", "paper", "Deep Learning", ("Yann LeCun",), 0)
    assert pairs(g1, g2, s1, s2) == []


def test_crowded_bucket_is_skipped_and_counted() -> None:
    many = [Obs(f"s{i}", "openalex", "paper", "A Very Common Title Here", ("Ann Lee",), i) for i in range(10)]
    out, stats = identity.match_pairs({o.source_id: o for o in many}, lambda s: (s,))
    assert out == [] and stats.crowded["paper"] == 1


def test_crowding_counts_key_clusters_not_sources() -> None:
    """S25-19: ten sources of two works (key clusters) are not a crowded bucket; ten works are."""
    many = {o.source_id: o for o in
            (Obs(f"s{i}", "openalex", "paper", "A Very Common Title Here", ("Ann Lee",), i) for i in range(10))}
    two = {s: ("s0" if int(s[1:]) < 5 else "s5") for s in many}
    out, stats = identity.match_pairs(many, lambda s: (s,), two)
    assert stats.crowded["paper"] == 0 and len(out) == 45  # every pair compared, as in a small bucket
    out, stats = identity.match_pairs(many, lambda s: (s,), {s: s for s in many})
    assert out == [] and stats.crowded["paper"] == 1


def test_post_same_author_or_channel_within_days() -> None:
    lw = Obs("s1", "lesswrong", "forum", "AI #187: Coming Into Play", ("Zvi",), 0, "forum_post")
    sub = Obs("s2", "zvi_substack", "url", "AI #187: Coming Into Play", ("Zvi Mowshowitz",), 1, "lab_blog")
    late = Obs("s3", "zvi_substack", "url", "AI #187: Coming Into Play", ("Zvi Mowshowitz",), 20, "lab_blog")
    other = Obs("s4", "lesswrong", "forum", "AI #187: Coming Into Play", ("Someone",), 0, "forum_post")
    assert pairs(lw, sub, late, other) == [("s1", "s2", "post_title_author")]   # one channel, two bylines: no
    b1 = Obs("s5", "openai_blog", "url", "Introducing the Model Spec Update", (), 0, "lab_blog")
    b2 = Obs("s6", "openai_blog", "url", "Introducing the Model Spec Update", (), 2, "lab_blog")
    b3 = Obs("s7", "miri_blog", "url", "Introducing the Model Spec Update", (), 2, "lab_blog")
    assert pairs(b1, b2, b3) == [("s5", "s6", "post_title_author")]     # same channel; no author across


def test_cards_and_untyped_url_rows_are_never_posts() -> None:
    a = Obs("s1", "anthropic_cards", "url", "Claude Model System Card Addendum", (), 0, "system_card")
    b = Obs("s2", "anthropic_cards", "url", "Claude Model System Card Addendum", (), 0, "system_card")
    c = Obs("s3", "links_url", "url", "Claude Model System Card Addendum", (), 0, None)
    assert pairs(a, b, c) == []


# ---------------------------------------------------------------- where adders run
def test_rekey_adds_linkpost_key_from_the_record() -> None:
    ident = {"channel": "lesswrong", "canonical_url": "https://www.lesswrong.com/posts/abc",
             "keys": [{"kind": "fm_post", "value": "abc"}]}
    r = rekey_identity("forum", ident, record={"url": "https://blog.example.org/p/post"})
    assert r.changed and set(r.new_keys) == {("fm_post", "abc"), ("url", "https://blog.example.org/p/post")}
    assert not rekey_identity("forum", ident, record={"url": None}).changed
    assert not rekey_identity("forum", ident).changed
