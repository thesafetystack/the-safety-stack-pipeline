"""``collect.feeds`` (round 4, track L): the four newsletter feed channels over frozen fixtures
(``tests/fixtures/feeds/``: the probed feeds of 2026-10-12, Concordia AI's of 2026-10-30 (the Chinese-coverage work), a few items
each, bodies trimmed),
served by a fake sender at the harness transport (no network).

One scenario through ``stack.stage_runner.run_stage`` (``run_line``) on a temp store: run 1 with ``since`` →
one row per item from ``since`` on, the Substack and WordPress bodies minted in-band (``post``,
raw ``text/html``), Paradigm 3 listing-only with its ``/research/`` item excluded, one ``ok``
coverage row per feed, checks clean; run 2 over unchanged feeds writes no ``sources`` row; run 3
with an edited body writes one row and replaces its ``post`` entry; run 4 without ``since`` adds
the older items (a teaser body stays listing-only); failed and non-feed reads. Plus the Atom shape
of the item parser and the channel config contract (a feed is one ``channels.yaml`` entry).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

import pytest

from stack import stage_runner
from stack.checks.verify import run_checks
from stack.config import load
from stack.http import HttpResponse
from stack.plan import Plan
from stack.collect.inband import json_record
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from tests.synth.records import run_line, sdir
from tests.synth.rows import content_entries

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "feeds"
_spec = importlib.util.spec_from_file_location("_collect_feeds_under_test",
                                               REPO / "workflows" / "collect" / "feeds" / "main.py")
assert _spec is not None and _spec.loader is not None
feeds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(feeds)

FEED_URLS = {"zvi_substack": "https://thezvi.substack.com/feed",
             "import_ai": "https://jack-clark.net/feed/",
             "paradigm3_news": "https://paradigm3.org/feed.xml",
             "aisafetychina_substack": "https://aisafetychina.substack.com/feed"}


def fixture_bodies() -> dict[str, bytes]:
    """Feed URL → the frozen fixture bytes."""
    return {url: (FIXTURES / f"{ch}.xml").read_bytes() for ch, url in FEED_URLS.items()}


def sender(served: Mapping[str, Any], requested: list[str] | None = None) -> Any:
    """A fake ``urllib_send``: ``served[url]`` is the body (bytes, status 200) or an int status."""
    def send(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
             timeout: float, max_bytes: int) -> HttpResponse:
        assert method == "GET"
        if requested is not None:
            requested.append(url)
        got = served.get(url, 404)
        if isinstance(got, int):
            return HttpResponse(got, {}, b"", url)
        return HttpResponse(200, {"content-type": "application/rss+xml"}, got, url)
    return send


def plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.feeds", "prior": ["sources", "coverage"], "out": {"kind": "part"},
         "options": options}]})


@pytest.fixture
def stage(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(stage_runner, "load_stage_module", lambda name: feeds)
    monkeypatch.setattr(feeds, "_sleep", lambda s: None)
    requested: list[str] = []

    def serve(served: Mapping[str, Any]) -> list[str]:
        requested.clear()
        monkeypatch.setattr("stack.collect.base.urllib_send", sender(served, requested))
        return requested

    return tmp_path / "store", serve


def rows(store: pathlib.Path) -> dict[str, dict[str, Any]]:
    return {r["identity"]["external_id"]: r
            for r in read_view(resolve(store, sdir("sources.feeds"))).to_pylist()}


def coverage(store: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = read_view(resolve(store, sdir("coverage.feeds"))).to_pylist()
    return out


def checks_clean(store: pathlib.Path) -> None:
    results = {r.check: r for r in run_checks(store)}
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}


@pytest.mark.slow  # real store + runner, five runs
def test_feeds_rows_inband_since_idempotent_edit(stage: Any) -> None:
    store, serve = stage
    served = fixture_bodies()
    serve(served)
    rec = run_line(store, plan(since="2026-09-12"), "collect.feeds")
    assert rec.status == "ok", rec.error
    got = rows(store)
    by_channel: dict[str, list[str]] = {}
    for ext, r in got.items():
        by_channel.setdefault(r["identity"]["channel"], []).append(ext)
    # Zvi: 3 items from 09-19 on; Import AI: 10-12 only (08-03 is before since); Paradigm 3: the two
    # /news/ items from since on (/research/ excluded, 02-22 before since); Concordia AI (the Chinese-coverage work): 10-29 and
    # 10-15 (2025-11-11 before since).
    assert {ch: len(v) for ch, v in by_channel.items()} == {"zvi_substack": 3, "import_ai": 1,
                                                            "paradigm3_news": 2, "aisafetychina_substack": 2}
    assert (rec.counts["items_excluded"], rec.counts["before_since"]) == (1, 3)
    assert (rec.counts["bodies_inband"], rec.counts["listing_only"], rec.counts["inband_minted"]) == (6, 2, 6)

    blobs = open_blobs(store)
    zvi = got["https://thezvi.substack.com/p/openai-fires-three-safety-researchers"]
    assert zvi["identity"]["canonical_url"] == "https://thezvi.substack.com/p/openai-fires-three-safety-researchers"
    assert zvi["identity"]["keys"] == [{"kind": "url",
                                        "value": "https://thezvi.substack.com/p/openai-fires-three-safety-researchers"}]
    md = zvi["metadata"]
    assert (md["title"], md["doc_type"], md["origin"], md["license_class"]) == (
        "OpenAI Fires Three Safety Researchers", "lab_blog", "provider", "public_web")
    assert [a["name"] for a in md["authors"]] == ["Zvi Mowshowitz"]
    assert md["published_at"] == dt.datetime(2026, 10, 12, 21, 0, 53, tzinfo=dt.UTC)
    assert md["abstract"].startswith("Three OpenAI safety researchers")
    (entry,) = content_entries(zvi)
    assert (entry["content_role"], entry["raw"]["media_type"], entry["text"]) == ("post", "text/html", None)
    body = blobs.get(entry["raw"]["sha256"]).decode()
    assert entry["raw"]["bytes"] == len(body.encode())
    assert body.startswith("<p>Three OpenAI safety researchers")
    payload = json_record(blobs, zvi)
    assert payload["link"] == zvi["identity"]["external_id"] and "body" not in payload and payload["body_sha256"]
    assert [(m["query_id"], m["label"]) for m in md["matched_by"]] == [
        ("feed:zvi_substack", "https://thezvi.substack.com/feed")]  # attribution (D228)

    p3 = got["https://paradigm3.org/news/newsletter-10-09"]
    assert content_entries(p3) == [] and len(p3["metadata"]["authors"]) == 9  # nine dc:creator
    assert p3["metadata"]["abstract"].startswith("TL;DR;")
    covs = coverage(store)
    assert sorted((c["channel"], c["status"], c["seen"], c["kept"]) for c in covs) == [
        ("aisafetychina_substack", "ok", 2, 2), ("import_ai", "ok", 1, 1), ("paradigm3_news", "ok", 2, 2),
        ("zvi_substack", "ok", 3, 3)]
    assert json.loads(covs[0]["query"])["since"] == "2026-09-12"
    checks_clean(store)

    # Run 2: a second pass within relist_after_h makes no request and writes nothing.
    requested = serve(served)
    rec2 = run_line(store, plan(since="2026-09-12"), "collect.feeds")
    assert rec2.status == "ok", rec2.error
    assert requested == [] and rec2.counts["not_due"] == 4 and "sources" not in rec2.outputs

    # Run 2b: forced re-read of unchanged feeds: no sources row (skip_unchanged), only coverage.
    requested = serve(served)
    rec2b = run_line(store, plan(since="2026-09-12", relist_after_h=0), "collect.feeds")
    assert rec2b.status == "ok", rec2b.error
    assert len(requested) == 4 and rec2b.counts["sources_unchanged"] == 8
    assert "sources" not in rec2b.outputs and rec2b.counts["inband_minted"] == 0

    # Run 3: one Zvi body edited → one new row, its post entry replaced; ids stable.
    ids = {k: r["source_id"] for k, r in rows(store).items()}
    edited = dict(served)
    edited[FEED_URLS["zvi_substack"]] = served[FEED_URLS["zvi_substack"]].replace(
        b"Three OpenAI safety researchers, <a", b"Three OpenAI safety researchers (edited), <a", 1)
    serve(edited)
    rec3 = run_line(store, plan(since="2026-09-12", relist_after_h=0), "collect.feeds")
    assert rec3.status == "ok", rec3.error
    assert (rec3.counts["sources_written"], rec3.counts["minted_ids"], rec3.counts["inband_minted"]) == (1, 0, 1)
    after = rows(store)
    assert {k: r["source_id"] for k, r in after.items()} == ids
    (e2,) = content_entries(after[zvi["identity"]["external_id"]])
    assert e2["raw"]["sha256"] != entry["raw"]["sha256"]
    assert b"(edited)" in blobs.get(e2["raw"]["sha256"])

    # Run 4: no since (a new coverage query, so due): the older items arrive; Import AI 467's
    # trimmed body is an image tag (under MIN_BODY_CHARS of text): listing-only.
    serve(edited)
    rec4 = run_line(store, plan(), "collect.feeds")
    assert rec4.status == "ok", rec4.error
    assert rec4.counts["sources_written"] == 3 and rec4.counts["before_since"] == 0  # + Concordia's 2025-11-11
    old = rows(store)["https://jack-clark.net/2026/08/03/import-ai-467-self-sustaining-ai-viruses-pacing-ai-progress-confusion-about-ai-and-creativity/"]
    assert content_entries(old) == []
    checks_clean(store)


def test_failed_and_non_feed_reads_fail_their_window(stage: Any) -> None:
    store, serve = stage
    served: dict[str, Any] = fixture_bodies()
    served[FEED_URLS["import_ai"]] = 404
    served[FEED_URLS["paradigm3_news"]] = b"<html><body>403 Forbidden</body></html>"
    serve(served)
    rec = run_line(store, plan(only=["import_ai", "paradigm3_news"]), "collect.feeds")
    assert rec.status == "ok", rec.error
    assert sorted((c["channel"], c["status"], c["error_class"]) for c in coverage(store)) == [
        ("import_ai", "failed_permanent", "dead"), ("paradigm3_news", "failed_permanent", "parse_error")]
    assert rec.counts["feeds_failed"] == 2 and "sources" not in rec.outputs
    bad = run_line(store, plan(only=["openai_blog"]), "collect.feeds")
    assert bad.status == "failed" and "non-feed channels ['openai_blog']" in (bad.error or "")


def test_atom_items_and_namespaced_noise() -> None:
    """Atom: link rel=alternate (not self/replies), id, published (else updated), author/name,
    summary, content type=html; RSS: media:content is not the body, itunes:author not an author."""
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title type="html">A &lt;b&gt;post&lt;/b&gt;</title>
        <link rel="replies" href="https://ex.org/p/1#comments"/><link href="https://ex.org/p/1"/>
        <id>tag:ex.org,2026:1</id><updated>2026-10-01T10:00:00+02:00</updated>
        <author><name>Ann</name></author><author><name>Bob</name></author>
        <summary>Short teaser of the post that is long enough.</summary>
        <content type="html">&lt;p&gt;""" + "body text " * 60 + """&lt;/p&gt;</content></entry>
    </feed>"""
    (it,) = feeds.parse_items(atom)
    assert (it.link, it.guid, it.title, it.authors) == ("https://ex.org/p/1", "tag:ex.org,2026:1", "A post", ["Ann", "Bob"])
    assert it.published == "2026-10-01T08:00:00+00:00"
    assert feeds.body_of(it) is not None and feeds.abstract_of(it) == "Short teaser of the post that is long enough."
    rss = """<rss xmlns:media="http://search.yahoo.com/mrss/" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
      version="2.0"><channel><item><title>T</title><link>https://ex.org/a</link>
      <pubDate>Tue, 06 Oct 2026 23:30:00 -0300</pubDate><itunes:author>Pod</itunes:author>
      <media:content url="https://ex.org/a.png"/><description>d</description></item></channel></rss>"""
    (r,) = feeds.parse_items(rss)
    assert (r.authors, r.body, r.published) == ([], "", "2026-10-07T02:30:00+00:00")
    assert feeds.body_of(r) is None and feeds.abstract_of(r) is None
    with pytest.raises(feeds.NotAFeed):
        feeds.parse_items("<html><body>blocked</body></html>")


def test_feed_channels_are_one_config_entry_each() -> None:
    """Every ``kind: feed`` channel names its feed URL, is in the ``url`` group, and its
    ``include_re`` compiles; the three newsletter channels exist and Paradigm 3 keeps /news/ only."""
    chans = {c.id: c for c in load().channels.channels if c.kind == "feed"}
    assert {k: c.location for k, c in chans.items() if k in FEED_URLS} == FEED_URLS
    assert all(c.channel_group == "url" and c.location for c in chans.values())
    inc = chans["paradigm3_news"].include_re
    assert inc is not None
    items = feeds.parse_items((FIXTURES / "paradigm3_news.xml").read_text("utf-8"))
    assert sorted(bool(re.search(inc, i.link)) for i in items) == [False, True, True, True]
