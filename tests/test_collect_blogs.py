"""Tests for ``collect.blogs`` (the ``kind: scrape`` lab-blog channels, v1's near-identical scrape
stages) and the feed parser of the shared listing engine (``stack/collect/listing.py``). Split out
of v3's ``test_collect_blogs_openalex.py`` (K#8). Section D drives the stage through
``stack.stage_runner.run_stage`` on a temporary store over ``tests/fakes/http.py`` (no network):
feed ∪ archive index give one row per post, an undated archive post resolves to the id its feed
sighting established (same run, then from the own chain), a truly undated post is ``unyeared``
and not observed, a failed feed is ``failed_*``, a re-run mints nothing and keeps the feed's date,
and V1/V2 pass.

The per-site blog id rules must reproduce what ``collect.v1_import`` already minted: an id drift
re-mints every previously seen post.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
from typing import Any

import pytest

from stack import stage_runner
from stack.checks.verify import run_checks
from stack.collect import listing
from stack.plan import Plan
from tests.synth.records import run_line, sdir
from stack.store.chain import read_view, resolve
from tests.fakes.http import FakeHttp

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO / "workflows"


def _load_wf(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"_collect_{name}_under_test", WORKFLOWS_DIR / "collect" / name / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


blogs = _load_wf("blogs")

# ─────────────────────────────────────────────────────────────────────────────
# (B) Feed parsing (the shared engine's newest surface method)
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_feed_rss_and_atom() -> None:
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Post One</title><link>https://alignment.openai.com/post-one/</link>
            <pubDate>Tue, 15 Apr 2025 09:00:00 GMT</pubDate></item>
    </channel></rss>"""
    assert listing.parse_feed(rss) == [
        ("https://alignment.openai.com/post-one/", "Post One", "2025-04-15")
    ]
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Post Two</title><link href="https://x.test/two/"/>
             <published>2024-11-02T00:00:00Z</published></entry>
    </feed>"""
    assert listing.parse_feed(atom) == [("https://x.test/two/", "Post Two", "2024-11-02")]


# ─────────────────────────────────────────────────────────────────────────────
# (C) Blogs: the per-site identity rules (must reproduce collect.v1_import's stems)
# ─────────────────────────────────────────────────────────────────────────────
def _cand(url: str, text: str = "", date_hint: str = "") -> Any:
    slug, from_url = listing.slug_from_url(url)
    return listing.Candidate(url=url, slug=slug, text=text, date_hint=date_hint or from_url)


@pytest.mark.parametrize(
    "channel, url, date_hint, expect",
    [
        # v1 stem alignment:<y>-<slug>
        ("anthropic_alignment_blog",
         "https://alignment.anthropic.com/2025/automated-auditing/", "", "2025-automated-auditing"),
        # circuits nests papers: the slug is the whole path after the year
        ("transformer_circuits",
         "https://transformer-circuits.pub/2025/attribution-graphs/biology.html", "",
         "2025-attribution-graphs-biology"),
        # …and the directory form collapses its /index.html
        ("transformer_circuits",
         "https://transformer-circuits.pub/2024/scaling-monosemanticity/index.html", "",
         "2024-scaling-monosemanticity"),
        # openai.com/index posts are year-less by design (v1 stem openai:<slug>)
        ("openai_blog", "https://openai.com/index/gpt-5-safe-completions/", "",
         "gpt-5-safe-completions"),
        # alignment.openai.com has no year in the URL: it comes from the FEED date
        ("openai_alignment_blog", "https://alignment.openai.com/deliberative-alignment/",
         "2024-12-20", "2024-deliberative-alignment"),
    ],
)
def test_blog_external_ids_match_v1_stems(
    channel: str, url: str, date_hint: str, expect: str
) -> None:
    assert blogs.external_id_for(_cand(url, date_hint=date_hint), blogs.SITE_SPECS[channel]) == expect


def test_blog_unyeared_post_is_not_minted_under_a_guess() -> None:
    """A year-qualified site whose post carries no year yields no id at all — a wrong year is a
    permanent duplicate, a skipped post is recoverable next run."""
    cand = _cand("https://alignment.openai.com/some-old-post/")
    assert blogs.external_id_for(cand, blogs.SITE_SPECS["openai_alignment_blog"]) is None


@pytest.mark.parametrize(
    "url, excluded",
    [
        # every URL spelling of a Circuits Updates page — the bare `-updates?$` anchor missed the
        # directory form and let 22 of them into the corpus on the first live run
        ("https://transformer-circuits.pub/2023/may-update/index.html", True),
        ("https://transformer-circuits.pub/2024/april-update/", True),
        ("https://transformer-circuits.pub/2024/jan-update.html", True),
        ("https://transformer-circuits.pub/2025/attribution-graphs/biology.html", False),
    ],
)
def test_circuits_update_pages_are_excluded(url: str, excluded: bool) -> None:
    spec = blogs.SITE_SPECS["transformer_circuits"]["surfaces"][0]
    assert listing.url_passes(spec, url, "") is (not excluded)


def test_openai_blog_leaves_cards_to_the_cards_watcher() -> None:
    """Card pages live under openai.com/index too. collect.cards mints them as <org>-<slug>; if
    this surface also minted them as <slug> there would be two sources per card and nothing to
    merge them (no cross-source title dedup in v3 yet)."""
    for spec in blogs.SITE_SPECS["openai_blog"]["surfaces"]:
        assert listing.url_passes(spec, "https://openai.com/index/gpt-4o-system-card/", "") is False
        assert listing.url_passes(spec, "https://openai.com/index/introducing-swe-bench/", "") is True
        assert listing.url_passes(spec, "https://openai.com/index/introducing-swe-bench", "") is True
        assert listing.url_passes(spec, "https://openai.com/academy/prompting", "") is False


# ─────────────────────────────────────────────────────────────────────────────
# (D) the stage on the harness: run_stage on a temp store over tests/fakes/http.py
# ─────────────────────────────────────────────────────────────────────────────
OA_FEED = "https://alignment.openai.com/rss.xml"
OA_INDEX = "https://alignment.openai.com"
FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Deliberative alignment</title><link>https://alignment.openai.com/deliberative-alignment/</link>
        <pubDate>Fri, 20 Dec 2024 09:00:00 GMT</pubDate></item>
</channel></rss>"""
INDEX = ('<html><body><a href="/deliberative-alignment/">Read</a>'
         '<a href="/some-old-post/">Some old post</a><a href="https://example.com/x">x</a>'
         '</body></html>')
_ENVS = ("STACK_BLOGS_LIMIT", "STACK_BLOGS_ONLY", "STACK_BLOGS_FIXTURES",
         "STACK_COLLECT_FRACTION", "STACK_COLLECT_MAX_QUERIES")


@pytest.fixture
def blogs_stage(tmp_path: pathlib.Path, monkeypatch: Any) -> Any:
    for var in _ENVS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(stage_runner, "load_stage_module", lambda name: blogs)
    monkeypatch.setattr(blogs, "_sleep", lambda s: None)
    fake = FakeHttp()
    monkeypatch.setattr("stack.collect.base.urllib_send", fake)
    return tmp_path / "store", fake


def _go(store: pathlib.Path, **options: Any) -> Any:
    plan = Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.blogs", "prior": ["sources", "coverage"],
         "options": {"only": ["openai_alignment_blog"], **options}}]})
    rec = run_line(store, plan, "collect.blogs")
    assert rec.status == "ok", rec.error
    return rec


def _latest_cov(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    rows = sorted(read_view(resolve(store, sdir("coverage.blogs"))).to_pylist(),
                  key=lambda r: r["window_end_at"])
    return {r["query"]: r for r in rows}


@pytest.mark.slow  # real store + runner
def test_blogs_stage_feed_and_index_rows_unyeared_rerun(blogs_stage: Any) -> None:
    store, fake = blogs_stage
    fake.add(OA_FEED, body=FEED)
    fake.add(OA_INDEX, body=INDEX)

    # run 1: the feed dates the post; the index re-finds it (same run) and an undated one
    rec1 = _go(store)
    assert rec1.counts["minted_ids"] == 1 and rec1.counts["unyeared"] == 1
    (row,) = read_view(resolve(store, sdir("sources.blogs"))).to_pylist()
    url = "https://alignment.openai.com/deliberative-alignment"
    # canonical_url is the fetch form as listed (trailing slash kept), the key drops it (url-normalisation.md §2)
    assert (row["identity"]["channel"], row["identity"]["external_id"],
            row["identity"]["canonical_url"]) == (
        "openai_alignment_blog", "2024-deliberative-alignment", url + "/")
    assert row["identity"]["keys"] == [{"kind": "url", "value": url}]
    md = row["metadata"]
    assert (md["title"], md["published_at"], md["doc_type"]) == (
        "Deliberative alignment", dt.datetime(2024, 12, 20, tzinfo=dt.UTC), "lab_blog")
    assert row["inband"]["entries"] == []
    cov = _latest_cov(store)
    assert (cov["openai-alignment-feed"]["status"], cov["openai-alignment-feed"]["kept"]) == ("ok", 1)
    # attribution (D228): both surfaces that listed the post, the feed's digest first
    assert md["matched_by"] == [
        {"query_id": q, "query_digest": cov[q]["query_digest"], "label": "openai_alignment_blog"}
        for q in ("openai-alignment-feed", "openai-alignment-index")]
    assert md["query_digest"] == cov["openai-alignment-feed"]["query_digest"]
    # the index saw the post again (one row per post per run: not counted twice) and the
    # undated one was not observed
    assert (cov["openai-alignment-index"]["status"], cov["openai-alignment-index"]["seen"]) == ("ok", 0)

    # run 2: the feed is down; the archive index alone resolves the post from the own chain and
    # does not blank the feed's date (a changed anchor text is still one changed row at most)
    fake.add(OA_FEED, status=503)
    rec2 = _go(store, relist_after_h=0)  # re-list at once (default: not within 12 h, D226)
    assert rec2.counts["minted_ids"] == 0 and rec2.counts["unyeared"] == 1
    rows = read_view(resolve(store, sdir("sources.blogs"))).to_pylist()
    assert [r["source_id"] for r in rows] == [row["source_id"]]
    assert rows[0]["metadata"]["published_at"] == dt.datetime(2024, 12, 20, tzinfo=dt.UTC)
    assert rows[0]["metadata"]["title"] == "Deliberative alignment"
    cov = _latest_cov(store)
    assert (cov["openai-alignment-feed"]["status"], cov["openai-alignment-feed"]["error_class"]) \
        == ("failed_transient", "http_5xx")
    assert (cov["openai-alignment-index"]["status"], cov["openai-alignment-index"]["seen"]) == ("ok", 1)

    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


# ─────────────────────────────────────────────────────────────────────────────
# (D2) openai_blog: the sitemap has no titles and only `lastmod` (a modification time), so the
# news RSS feed (titles + pubDate, links without the trailing slash) is the first surface
# ─────────────────────────────────────────────────────────────────────────────
OAI_FEED = "https://openai.com/news/rss.xml"
OAI_SITEMAP = "https://openai.com/sitemap.xml"
OAI_CHILD = "https://openai.com/sitemap.xml/research/"
OAI_FEED_BODY = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title><![CDATA[Introducing SWE-bench Verified]]></title>
        <link>https://openai.com/index/introducing-swe-bench</link>
        <pubDate>Tue, 13 Aug 2024 10:00:00 GMT</pubDate></item>
  <item><title>Distill</title><link>https://openai.com/index/distill</link>
        <pubDate>Mon, 20 Mar 2017 10:00:00 GMT</pubDate></item>
  <item><title>OpenAI Academy</title><link>https://openai.com/academy/prompting</link>
        <pubDate>Tue, 13 Aug 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""
OAI_INDEX_BODY = f"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{OAI_CHILD}</loc></sitemap></sitemapindex>"""
OAI_CHILD_BODY = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://openai.com/index/introducing-swe-bench/</loc><lastmod>2026-10-10T14:31:39.564Z</lastmod></url>
  <url><loc>https://openai.com/index/dall-e-3/</loc><lastmod>2026-10-01T09:00:00Z</lastmod></url>
  <url><loc>https://openai.com/index/gpt-4o-system-card/</loc></url>
</urlset>"""


def _go_oai(store: pathlib.Path, **options: Any) -> Any:
    plan = Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.blogs", "prior": ["sources", "coverage"],
         "options": {"only": ["openai_blog"], **options}}]})
    rec = run_line(store, plan, "collect.blogs")
    assert rec.status == "ok", rec.error
    return rec


def _oai_rows(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    return {r["identity"]["external_id"]: r for r in read_view(resolve(store, sdir("sources.blogs"))).to_pylist()}


@pytest.mark.slow  # real store + runner
def test_openai_blog_titles_and_dates_from_the_news_feed(blogs_stage: Any) -> None:
    store, fake = blogs_stage
    fake.add(OAI_SITEMAP, body=OAI_INDEX_BODY)
    fake.add(OAI_CHILD, body=OAI_CHILD_BODY)

    # run 1: the feed is down, so only the sitemap lists the posts: slug titles, no date (lastmod
    # is a modification time), and the fallback is counted, not silent
    fake.add(OAI_FEED, status=503)
    rec1 = _go_oai(store)
    rows = _oai_rows(store)
    assert set(rows) == {"introducing-swe-bench", "dall-e-3"}
    assert {k: (r["metadata"]["title"], r["metadata"]["published_at"]) for k, r in rows.items()} == {
        "introducing-swe-bench": ("Introducing Swe Bench", None), "dall-e-3": ("Dall E 3", None)}
    assert (rec1.counts["titles_from_slug"], rec1.counts["undated"]) == (2, 2)

    # run 2: the feed answers; the known post takes the feed's title and pubDate (one changed row,
    # same source_id, same url key: the feed link's missing trailing slash is normalised in the
    # key; canonical_url is each listing's fetch form, url-normalisation.md §2)
    fake.add(OAI_FEED, body=OAI_FEED_BODY)
    rec2 = _go_oai(store, relist_after_h=0)  # re-list at once (default: not within 12 h, D226)
    rows2 = _oai_rows(store)
    swe, old = rows2["introducing-swe-bench"], rows["introducing-swe-bench"]
    assert swe["source_id"] == old["source_id"]
    assert swe["identity"]["keys"] == old["identity"]["keys"] == [  # with the moved-post-path alternates (D772)
        {"kind": "url", "value": "https://openai.com/index/introducing-swe-bench"},
        {"kind": "url", "value": "https://openai.com/blog/introducing-swe-bench"},
        {"kind": "url", "value": "https://openai.com/research/introducing-swe-bench"}]
    assert (old["identity"]["canonical_url"], swe["identity"]["canonical_url"]) == (
        "https://openai.com/index/introducing-swe-bench/", "https://openai.com/index/introducing-swe-bench")
    assert (swe["metadata"]["title"], swe["metadata"]["published_at"]) == (
        "Introducing SWE-bench Verified", dt.datetime(2024, 8, 13, tzinfo=dt.UTC))
    assert rows2["dall-e-3"]["metadata"]["title"] == "Dall E 3"
    # a feed title that happens to equal the slug's title-casing ("Distill") is not a fallback
    assert rows2["distill"]["metadata"]["title"] == "Distill"
    assert (rec2.counts["minted_ids"], rec2.counts["titles_from_slug"], rec2.counts["undated"]) == (1, 1, 1)
    cov = _latest_cov(store)
    assert (cov["openai-news-feed"]["status"], cov["openai-news-feed"]["kept"]) == ("ok", 1)

    # run 3: the feed is down again; the sitemap must not blank the feed's title or date
    fake.add(OAI_FEED, status=503)
    _go_oai(store)
    swe3 = _oai_rows(store)["introducing-swe-bench"]
    assert (swe3["metadata"]["title"], swe3["metadata"]["published_at"]) == (
        "Introducing SWE-bench Verified", dt.datetime(2024, 8, 13, tzinfo=dt.UTC))
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


# ─────────────────────────────────────────────────────────────────────────────
# (E) safety-org and research-org blogs (SR scope, 2026-10-12): the channel table, the include /
# exclude regexes on URL shapes taken from the sites' real listings, and one fake-HTTP stage
# run per surface method (feed, sitemap, html-index)
# ─────────────────────────────────────────────────────────────────────────────
SR_BLOG_CHANNELS = [
    "miri_blog", "metr_blog", "transluce_blog", "goodfire_blog", "uk_aisi_blog", "redwood_blog",
    "apollo_blog", "arc_blog", "epoch_blog", "forethought_blog", "far_ai_blog", "timaeus_blog",
    "clr_blog", "softmax_blog", "meaning_alignment_blog", "cooperative_ai_blog",
]


def test_blog_channel_table_and_site_specs_agree() -> None:
    """Every scrape channel in channels.yaml has a spec and every spec a scrape channel (a spec
    without a channel is never run; a channel without a spec is silently skipped by run())."""
    from stack.config import load

    scrape = {ch.id: ch for ch in load().channels.channels if ch.kind == "scrape"}
    assert set(scrape) == set(blogs.SITE_SPECS)
    assert set(SR_BLOG_CHANNELS) <= set(scrape)
    for cid in SR_BLOG_CHANNELS:
        assert blogs.SITE_SPECS[cid]["id_template"] == "{slug}"


def _passes(channel: str, url: str) -> bool:
    """Whether ANY of the channel's surfaces lists the URL."""
    return any(listing.url_passes(sp, url, "") for sp in blogs.SITE_SPECS[channel]["surfaces"])


@pytest.mark.parametrize(
    "channel, url, listed",
    [
        # metr.org/feed.xml mixes posts, notes and evaluation reports with translations, PDFs and
        # off-site links
        ("metr_blog", "https://metr.org/blog/2026-10-06-ai-systems-could-cover-up-misbehavior/", True),
        ("metr_blog", "https://metr.org/notes/2026-07-24-metrics-of-model-ability/", True),
        ("metr_blog", "https://metr.org/evaluations/gpt-5-report/", True),
        ("metr_blog", "https://metr.org/zh-Hans/notes/2026-01-29-frontier-ai-safety-regulations/", False),
        ("metr_blog", "https://metr.org/METR_ai_action_plan_comment.pdf", False),
        ("metr_blog", "https://arxiv.org/abs/2503.17354", False),
        # transluce.org's sitemap puts posts at top-level slugs next to the site's own pages
        ("transluce_blog", "https://transluce.org/agent-activity", True),
        ("transluce_blog", "https://transluce.org/docent/blog/swe-bench", True),
        ("transluce_blog", "https://transluce.org/about", False),
        ("transluce_blog", "https://transluce.org/docent", False),
        ("transluce_blog", "https://transluce.org/news", False),
        ("transluce_blog", "https://transluce.org/docent/blog", False),
        ("transluce_blog", "https://transluce.org/docent/changelog", False),
        ("transluce_blog", "https://transluce.org/elicitation-detail/qwen-sql-local", False),
        # far.ai's sitemap is mostly author and event-recording pages
        ("far_ai_blog", "https://www.far.ai/research/adversarial-circuit-evaluation", True),
        ("far_ai_blog", "https://www.far.ai/author/adam-gleave", False),
        ("far_ai_blog", "https://www.far.ai/event-recordings/some-talk", False),
        # apollo's sitemap drops the www.; press and team pages are not posts
        ("apollo_blog", "https://apolloresearch.ai/science/the-evals-gap", True),
        ("apollo_blog", "https://apolloresearch.ai/press/some-coverage", False),
        ("apollo_blog", "https://apolloresearch.ai/team/someone", False),
        # epoch: essays and publications, not chart notes or announcements
        ("epoch_blog", "https://epoch.ai/gradient-updates/is-a-compute-crunch-coming", True),
        ("epoch_blog", "https://epoch.ai/data-insights/cyber-incidents-flat-since-fable-5", False),
        ("epoch_blog", "https://epoch.ai/latest/announcing-frontiermath-erdos", False),
        # MIRI: dated permalinks only, not the day archive or the blog's pagination
        ("miri_blog", "https://intelligence.org/2026/09/30/miri-newsletter-127/", True),
        ("miri_blog", "https://intelligence.org/2026/09/30/", False),
        ("miri_blog", "https://intelligence.org/blog/2/", False),
        ("timaeus_blog", "https://timaeus.co/projects/some-project", False),
        # timaeus.co nests blog posts under a series; /blog/updates/ slugs collide ("hiring")
        ("timaeus_blog", "https://timaeus.co/blog/slt/2023-02-06-gradient-surfing/", True),
        ("timaeus_blog", "https://timaeus.co/blog/updates/2025-01-17-hiring/", False),
        ("uk_aisi_blog", "https://www.aisi.gov.uk/people/someone", False),
    ],
)
def test_sr_blog_include_exclude(channel: str, url: str, listed: bool) -> None:
    assert _passes(channel, url) is listed


def test_path_date_reads_wordpress_permalinks_only() -> None:
    assert blogs.path_date("https://intelligence.org/2026/09/30/miri-newsletter-127/") == "2026-09-30"
    assert blogs.path_date("https://intelligence.org/2026/13/30/x/") is None  # not a date
    assert blogs.path_date("https://alignment.anthropic.com/2025/automated-auditing/") is None


def _go_only(store: pathlib.Path, channel: str) -> Any:
    plan = Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": "collect.blogs", "prior": ["sources", "coverage"],
         "options": {"only": [channel]}}]})
    rec = run_line(store, plan, "collect.blogs")
    assert rec.status == "ok", rec.error
    return rec


def _rows_by_id(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    return {r["identity"]["external_id"]: r
            for r in read_view(resolve(store, sdir("sources.blogs"))).to_pylist()}


METR_FEED_BODY = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>AI systems could cover up misbehavior</title>
        <link>https://metr.org/blog/2026-10-06-ai-systems-could-cover-up-misbehavior/</link>
        <pubDate>Tue, 06 Oct 2026 00:00:00 +0000</pubDate></item>
  <item><title>Metrics of Agent Ability</title>
        <link>https://metr.org/notes/2026-07-24-metrics-of-model-ability/</link>
        <pubDate>Fri, 24 Jul 2026 00:00:00 +0000</pubDate></item>
  <item><title>Regulación de seguridad de IA de frontera</title>
        <link>https://metr.org/es/notes/2026-01-29-frontier-ai-safety-regulations/</link>
        <pubDate>Thu, 29 Jan 2026 00:00:00 +0000</pubDate></item>
  <item><title>HCAST</title><link>https://arxiv.org/abs/2503.17354</link>
        <pubDate>Mon, 17 Mar 2025 00:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_sr_feed_surface_metr(blogs_stage: Any) -> None:
    """Feed: dated, titled rows; the slug's date prefix leaves the id; translations and off-site
    links are not rows."""
    store, fake = blogs_stage
    fake.add("https://metr.org/feed.xml", body=METR_FEED_BODY)
    rec = _go_only(store, "metr_blog")
    rows = _rows_by_id(store)
    assert set(rows) == {"ai-systems-could-cover-up-misbehavior", "metrics-of-model-ability"}
    r = rows["ai-systems-could-cover-up-misbehavior"]
    assert (r["identity"]["channel"], r["identity"]["canonical_url"]) == (
        "metr_blog", "https://metr.org/blog/2026-10-06-ai-systems-could-cover-up-misbehavior/")
    assert (r["metadata"]["title"], r["metadata"]["published_at"]) == (
        "AI systems could cover up misbehavior", dt.datetime(2026, 10, 6, tzinfo=dt.UTC))
    assert (rec.counts["titles_from_slug"], rec.counts["undated"]) == (0, 0)


MIRI_INDEX = "https://intelligence.org/sitemap_index.xml"
MIRI_POSTS = "https://intelligence.org/post-sitemap1.xml"
MIRI_PAGES = "https://intelligence.org/page-sitemap.xml"


def test_sr_sitemap_surface_miri_path_dates(blogs_stage: Any) -> None:
    """Sitemap index → child sitemaps: only dated permalinks are posts, and the permalink's
    /YYYY/MM/DD/ is the publication date (lastmod is not). The feed's title wins for a post both
    list; a sitemap-only post keeps a slug title."""
    store, fake = blogs_stage
    fake.add("https://intelligence.org/feed/", body="""<?xml version="1.0"?><rss version="2.0">
      <channel><item><title>MIRI Newsletter #127</title>
        <link>https://intelligence.org/2026/09/30/miri-newsletter-127/</link>
        <pubDate>Wed, 30 Sep 2026 18:00:00 +0000</pubDate></item></channel></rss>""")
    fake.add(MIRI_INDEX, body=f"""<?xml version="1.0"?>
      <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>{MIRI_POSTS}</loc></sitemap><sitemap><loc>{MIRI_PAGES}</loc></sitemap>
      </sitemapindex>""")
    fake.add(MIRI_POSTS, body="""<?xml version="1.0"?>
      <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://intelligence.org/2026/09/30/miri-newsletter-127/</loc>
             <lastmod>2026-10-01T10:00:00+00:00</lastmod></url>
        <url><loc>https://intelligence.org/2013/05/15/when-will-ai-be-created/</loc>
             <lastmod>2025-02-01T10:00:00+00:00</lastmod></url>
      </urlset>""")
    fake.add(MIRI_PAGES, body="""<?xml version="1.0"?>
      <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://intelligence.org/research/</loc></url>
      </urlset>""")
    rec = _go_only(store, "miri_blog")
    rows = _rows_by_id(store)
    assert set(rows) == {"miri-newsletter-127", "when-will-ai-be-created"}
    assert rows["miri-newsletter-127"]["metadata"]["title"] == "MIRI Newsletter #127"
    old = rows["when-will-ai-be-created"]["metadata"]
    assert (old["title"], old["published_at"]) == (
        "When Will Ai Be Created", dt.datetime(2013, 5, 15, tzinfo=dt.UTC))
    assert (rec.counts["titles_from_slug"], rec.counts["undated"]) == (1, 0)
    cov = _latest_cov(store)
    assert (cov["miri-sitemap"]["status"], cov["miri-sitemap"]["kept"]) == ("ok", 1)


# cooperativeai.com/blog's Webflow cards, as served (image elided): the date and the title are
# sibling <div>s inside one anchor, so the anchor text reads "August 26, 2026Lessons …"
CAIF_INDEX_BODY = """<html><body><div role="list">
<div role="listitem" class="collection-item-11 w-dyn-item"><a href="/post/lessons-from-multi-agent-safety-incidents" class="post-wrapper w-inline-block"><div class="post-thumbnail-wrapper"><img src="x.jpg" alt=""/></div><div class="paragraph grey blog-date">August 26, 2026</div><div class="text-xl blog-title">Lessons from Recent Multi-Agent Safety Incidents</div></a></div>
<div role="listitem" class="collection-item-11 w-dyn-item"><a href="/post/delegating-deliberation-to-ai-representatives" class="post-wrapper w-inline-block"><div class="post-thumbnail-wrapper"><img src="y.jpg" alt=""/></div><div class="paragraph grey blog-date">August 4, 2026</div><div class="text-xl blog-title">Delegating Deliberation to AI Representatives</div></a></div>
<a href="/events">Events</a><a href="/blog">Blog</a>
</div></body></html>"""


def test_sr_html_index_surface_caif_title_strip(blogs_stage: Any) -> None:
    """html-index: /post/ links only; the date the cards glue onto the title is stripped from the
    title (title_strip_re)."""
    store, fake = blogs_stage
    fake.add("https://www.cooperativeai.com/blog", body=CAIF_INDEX_BODY)
    _go_only(store, "cooperative_ai_blog")
    rows = _rows_by_id(store)
    assert {k: r["metadata"]["title"] for k, r in rows.items()} == {
        "lessons-from-multi-agent-safety-incidents":
            "Lessons from Recent Multi-Agent Safety Incidents",
        "delegating-deliberation-to-ai-representatives":
            "Delegating Deliberation to AI Representatives",
    }
    assert rows["lessons-from-multi-agent-safety-incidents"]["metadata"]["published_at"] == \
        dt.datetime(2026, 8, 26, tzinfo=dt.UTC)
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


# ─────────────────────────────────────────────────────────────────────────────
# C9 (collection-yield.md §9.3, Tomáš 2026-10-13): google_research keeps a post only with a safety
# stem in its title; C10: no publication-date floor on any blog
# ─────────────────────────────────────────────────────────────────────────────
def test_title_include_re_matches_the_listing_title_case_insensitively() -> None:
    site = {"title_include_re": r"\bsafety\b|\bjailbreak"}
    assert blogs.title_included(_cand("https://x.test/a/", "Making models SAFETY-aware"), site)
    assert blogs.title_included(_cand("https://x.test/a/", "On Jailbreaking"), site)
    assert not blogs.title_included(_cand("https://x.test/a/", "Weather forecasting at scale"), site)
    # no anchor text: the slug title is matched
    assert blogs.title_included(_cand("https://x.test/llm-safety-notes/", ""), site)
    # the strip runs first (the stripped prefix cannot match), and no pattern keeps everything
    assert not blogs.title_included(_cand("https://x.test/a/", "Safety 2026 Update"),
                                    {**site, "title_strip_re": r"^Safety \d{4}"})
    assert blogs.title_included(_cand("https://x.test/a/", "Weather"), {})


def test_google_research_is_the_one_title_filtered_site() -> None:
    from stack.rules import safety_stem_regex

    assert {cid for cid, spec in blogs.SITE_SPECS.items() if spec.get("title_include_re")} == {
        "google_research"}
    assert blogs.SITE_SPECS["google_research"]["title_include_re"] == safety_stem_regex()


GOOGLE_FEED_BODY = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>Open and Emergent Problems in Agentic Privacy and Security</title>
        <link>https://research.google/blog/open-problems-agentic-privacy/</link>
        <pubDate>Tue, 06 Oct 2026 00:00:00 +0000</pubDate></item>
  <item><title>Red-teaming Gemini for prompt injection</title>
        <link>https://research.google/blog/red-teaming-gemini/</link>
        <pubDate>Mon, 05 Oct 2026 00:00:00 +0000</pubDate></item>
  <item><title>A new weather model for the tropics</title>
        <link>https://research.google/blog/weather-tropics/</link>
        <pubDate>Fri, 02 Oct 2026 00:00:00 +0000</pubDate></item>
  <item><title>Safety Gym, revisited</title>
        <link>https://research.google/blog/safety-gym-revisited/</link>
        <pubDate>Sat, 01 Jan 2011 00:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_google_research_title_filter_and_no_date_floor(blogs_stage: Any) -> None:
    """C9: posts without a safety stem in the title are not observed, counted
    `posts_title_filtered`, not in the window's `seen`; C10: a 2011 post is collected (no floor)."""
    store, fake = blogs_stage
    fake.add("https://research.google/blog/rss/", body=GOOGLE_FEED_BODY)
    rec = _go_only(store, "google_research")
    rows = _rows_by_id(store)
    assert set(rows) == {"red-teaming-gemini", "safety-gym-revisited"}
    assert rows["safety-gym-revisited"]["metadata"]["published_at"] == dt.datetime(
        2011, 1, 1, tzinfo=dt.UTC)
    assert (rec.counts["posts_title_filtered"], rec.counts["posts_seen"]) == (2, 2)
    cov = _latest_cov(store)["google-research-feed"]
    assert (cov["status"], cov["seen"], cov["kept"]) == ("ok", 2, 2)
