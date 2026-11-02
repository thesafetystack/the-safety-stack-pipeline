"""``acquire``'s fetch lanes through the runner on a fake transport (PLAN T3.2-T3.4): each case
runs ``stack run acquire`` over a per-test store with ``lanes.using(now=…, transport=FakeHttp,
sleep=…)`` and checks what landed in the part files — snapshots, roles, the pointer. No network:
an unrouted URL is a ``LookupError`` in the fake (the skeleton then records the lane crash, which
the assertions catch). Shared among the lane tasks: each lane adds its own section.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from stack.checks.verify import run_checks
from stack.plan import Plan
from stack.stage_runner import run_stage
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from tests.fakes.http import FakeHttp
from tests.fakes.lanes import FakeFetchpdf
from tests.synth.records import sdir
from tests.synth.rows import AT, document_row, sid
from tests.synth.world import write_on_head
from workflows.acquire import lanes
from workflows.acquire.lanes.arxiv import LIMIT_GIVE_UP
from workflows.acquire.lanes.http import WAYBACK_API

pytestmark = pytest.mark.slow

Row = dict[str, Any]
DAY = dt.timedelta(days=1)
NOW = AT + dt.timedelta(hours=30)
CHECKS = ["attempts_monotone", "V-content", "status_pairing", "block_ownership", "V-chain-linear"]


def view(root: Path) -> dict[str, Row]:
    return {r["doc_id"]: r for r in read_view(resolve(root, sdir("documents"))).to_pylist()}


def checks_ok(root: Path) -> None:
    by = {r.check: r for r in run_checks(root, names=CHECKS)}
    assert {n: r.status for n, r in by.items()} == dict.fromkeys(by, "ok"), {n: r.items for n, r in by.items()}


# ================================================================ lane http (T3.2)
BLOG = "openai_blog"  # channel group ``url`` (D22 weekly recheck), politeness respect_robots, 0.5 rps
PAGE = "<!doctype html><html><body><article>{}</article></body></html>"


def url_of(i: int, host: str = "blog.test") -> str:
    return f"https://{host}/p/{i}"


def page_row(i: int, url: str, channel: str = BLOG) -> Row:
    row = document_row(i, state="triaged")
    row["identity"]["members"][0].update(channel=channel, canonical_url=url)
    row["identity"]["channels"] = [channel]
    return row


def store(tmp_path: Path, rows: list[Row]) -> Path:
    root = tmp_path / "store"
    write_on_head(root, "documents", "documents", rows, stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband"])
    return root


def http_plan(channel: str = BLOG, role: str = "page", **options: Any) -> Plan:
    """No convert config: the lane cases check snapshots, roles and the pointer, not text."""
    return Plan.model_validate({"plan": "test-lanes", "schema_version": 2,
                                "acquire": {"wants": {channel: [role]}},
                                "stages": [{"stage": "acquire", "options": options}]})


def fake_web(*hosts: str) -> FakeHttp:
    fake = FakeHttp()
    for host in hosts:
        fake.add(f"https://{host}/robots.txt", status=404)  # respect_robots: allow-all
    return fake


def no_capture(fake: FakeHttp, url: str) -> None:
    fake.add(WAYBACK_API, {"url": url}, json={"archived_snapshots": {}})


def go(root: Path, fake: FakeHttp, at: dt.datetime, plan: Plan | None = None,
       sleeps: list[float] | None = None) -> Any:
    with lanes.using(now=at, transport=fake, sleep=sleeps.append if sleeps is not None else (lambda s: None)):
        rec = run_stage(root, plan or http_plan(), "acquire", [sdir("documents")], series="main")
    assert rec.status == "ok", rec.error
    assert "lane_crashed" not in rec.counts, "a lane crashed (an unrouted URL in the fake?)"
    return rec


def page_role(row: Row) -> Row:
    (r,) = [r for r in row["content"]["roles"] if r["content_role"] == "page"]
    return r


def raws(row: Row) -> list[Row]:
    return [s for s in row["content"]["snapshots"] if s["kind"] == "raw"]


def test_http_unchanged_mints_nothing(tmp_path: Path) -> None:
    """A 200 mints one live snapshot and the ``ok`` page role carries ``retry_after =
    last_attempt_at + 7 d`` (D22); a week later the replayed ETag gets a 304 and a week after that
    an equal-digest 200: neither mints a snapshot, both stamp ``last_attempt_at``, and
    ``last_outcome`` says which (``not_modified`` / ``unchanged``)."""
    url, d = url_of(1), sid(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = fake_web("blog.test")
    body = PAGE.format("First version of the post.")
    fake.add(url, body=body, headers={"ETag": '"e1"', "Content-Type": "text/html; charset=utf-8"})

    rec = go(root, fake, NOW)
    assert rec.counts["http.via.live"] == 1 and rec.counts["snapshots_minted"] == 1
    row = view(root)[d]
    (snap,) = raws(row)
    role = page_role(row)
    assert (snap["via"], snap["raw"]["media_type"], snap["url"], snap["http_status"], snap["etag"]) == (
        "live", "text/html", url, 200, '"e1"')
    assert open_blobs(root).get(snap["raw"]["sha256"]) == body.encode()
    assert (role["status"], role["lane"], role["last_outcome"], role["etag"], role["validator_url"]) == (
        "ok", "http", "ok", '"e1"', url)
    assert role["retry_after"] == role["last_attempt_at"] + 7 * DAY == NOW + 7 * DAY
    assert row["content"]["current_snapshot_id"] == snap["snapshot_id"]

    fake.add(url, status=304, headers={"ETag": '"e1"'})
    rec = go(root, fake, NOW + 7 * DAY)
    assert fake.calls[-1].headers.get("If-None-Match") == '"e1"'
    assert rec.counts["http.not_modified"] == 1 and rec.counts["snapshots_minted"] == 0
    row = view(root)[d]
    role = page_role(row)
    assert len(raws(row)) == 1 and row["content"]["current_snapshot_id"] == snap["snapshot_id"]
    assert (role["status"], role["last_outcome"], role["last_attempt_at"], role["retry_after"]) == (
        "ok", "not_modified", NOW + 7 * DAY, NOW + 14 * DAY)

    fake.add(url, body=body)  # same bytes, no validators this time
    rec = go(root, fake, NOW + 14 * DAY)
    assert rec.counts["unchanged"] == 1 and rec.counts["snapshots_minted"] == 0
    row = view(root)[d]
    role = page_role(row)
    assert len(raws(row)) == 1
    assert (role["last_outcome"], role["last_attempt_at"], role["etag"]) == ("unchanged", NOW + 14 * DAY, None)
    assert not any("archive.org" in u for u in fake.urls())
    checks_ok(root)


@pytest.mark.parametrize("status", [404, 410])
def test_http_dead_keeps_the_pointer(tmp_path: Path, status: int) -> None:
    """A page that dies after a good fetch: role ``failed_permanent[dead]`` (terminal, with the
    90-day recheck of R4-2: not called again within 30 days), the pointer stays on the last good
    snapshot, and archive.org is not asked (a live body is held)."""
    url, d = url_of(1), sid(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = fake_web("blog.test")
    fake.add(url, body=PAGE.format("The post."))
    go(root, fake, NOW)
    (good,) = raws(view(root)[d])

    fake.add(url, status=status)
    rec = go(root, fake, NOW + 7 * DAY)
    row = view(root)[d]
    role = page_role(row)
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_permanent", "dead", 1, NOW + 97 * DAY)
    assert f"HTTP {status}" in role["error"]
    assert raws(row) == [good] and row["content"]["current_snapshot_id"] == good["snapshot_id"]
    assert rec.counts["outcome.failed_permanent"] == 1
    assert not any("archive.org" in u for u in fake.urls())

    n = len(fake.calls)
    rec = go(root, fake, NOW + 30 * DAY)
    assert len(fake.calls) == n and rec.counts.get("lane_calls", 0) == 0  # dead is terminal
    checks_ok(root)


def test_http_wayback_never_outranks_live(tmp_path: Path) -> None:
    """Live fails on a page we never held → the closest Wayback capture is minted (``via =
    'wayback'``, ``as_of`` = capture time) and is current; the role is ``ok`` (rechecked
    weekly). A week later live answers: its snapshot is current although its ``as_of``
    (``Last-Modified``) is older than the capture's.

    D589 (changed from the replay URL): the capture's bytes are the original ones
    (``web.archive.org/web/<ts>id_/<url>``, the snapshot's ``final_url``), never the replay page
    with the toolbar; the archived ``Content-Type`` breaks the sniff's tie (a bare HTML fragment is
    ``text/html``); the archive's ``ETag``/``Last-Modified`` are stored nowhere, so the live
    revalidation a week later sends no conditional headers."""
    url, d = url_of(1), sid(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = fake_web("blog.test")
    fake.add(url, status=503)
    replay = f"http://web.archive.org/web/20250301120000/{url}"
    fake.add(WAYBACK_API, {"url": url}, json={"archived_snapshots": {"closest": {
        "status": "200", "available": True, "url": replay, "timestamp": "20250301120000"}}})
    fake.add(replay, body=PAGE.format('<div id="wm-ipp">Wayback toolbar</div>'
                                      '<a href="/web/20250301120000/https://blog.test/">rewritten</a>'))
    capture = f"https://web.archive.org/web/20250301120000id_/{url}"
    archived = "<div><p>Archived copy.</p></div>"  # a fragment: the sniff says nothing
    fake.add(capture, body=archived, headers={"Content-Type": "text/html; charset=utf-8", "ETag": '"wb"',
                                              "Last-Modified": "Sat, 01 Mar 2025 12:00:00 GMT"})

    rec = go(root, fake, NOW)
    assert rec.counts["http.via.wayback"] == 1
    assert replay not in fake.urls()
    row = view(root)[d]
    (way,) = raws(row)
    assert (way["via"], way["url"], way["final_url"], way["as_of"], way["captured_at"]) == (
        "wayback", url, capture, dt.datetime(2025, 3, 1, 12, tzinfo=dt.UTC), NOW)
    assert open_blobs(root).get(way["raw"]["sha256"]) == archived.encode()
    assert (way["raw"]["media_type"], way["etag"], way["last_modified"]) == ("text/html", None, None)
    assert row["content"]["current_snapshot_id"] == way["snapshot_id"]
    role = page_role(row)
    assert (role["status"], role["retry_after"], role["etag"], role["last_modified"], role["validator_url"]) == (
        "ok", NOW + 7 * DAY, None, None, None)

    fake.add(url, body=PAGE.format("Live again."), headers={"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})
    go(root, fake, NOW + 7 * DAY)
    (live_call,) = [c for c in fake.calls if c.url == url][-1:]
    assert not {"If-None-Match", "If-Modified-Since"} & set(live_call.headers)
    row = view(root)[d]
    live = next(s for s in raws(row) if s["via"] == "live")
    assert live["as_of"] == dt.datetime(2024, 1, 1, tzinfo=dt.UTC) < way["as_of"]
    assert row["content"]["current_snapshot_id"] == live["snapshot_id"] and len(raws(row)) == 2
    assert page_role(row)["validator_url"] == url
    checks_ok(root)


def test_http_archived_copy_is_not_refreshed_on_a_blip(tmp_path: Path) -> None:
    """D589 (Dan Elton's v3 rule): a role that holds only an archived copy does not ask the archive
    again on a transient live failure (a 503 after the retries): the role fails
    ``failed_transient[http_5xx]`` and the pointer stays on the copy. A page that is gone (404)
    still gets the archive's newest capture (§5.6 "archived-only: newest capture")."""
    url, d = url_of(1), sid(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = fake_web("blog.test")
    fake.add(url, status=503)

    def closest(ts: str) -> str:
        fake.add(WAYBACK_API, {"url": url}, json={"archived_snapshots": {"closest": {
            "status": "200", "available": True, "url": f"http://web.archive.org/web/{ts}/{url}", "timestamp": ts}}})
        raw = f"https://web.archive.org/web/{ts}id_/{url}"
        fake.add(raw, body=PAGE.format(f"Copy of {ts}."))
        return raw

    closest("20250301120000")
    go(root, fake, NOW)
    (first,) = raws(view(root)[d])
    assert first["via"] == "wayback"

    n = len(fake.calls)
    rec = go(root, fake, NOW + 7 * DAY)
    assert not any("archive.org" in c.url for c in fake.calls[n:])
    assert rec.counts.get("http.via.wayback", 0) == 0
    row = view(root)[d]
    role = page_role(row)
    assert (role["status"], role["error_class"], role["attempts"]) == ("failed_transient", "http_5xx", 1)
    assert raws(row) == [first] and row["content"]["current_snapshot_id"] == first["snapshot_id"]

    fake.add(url, status=404)
    newer = closest("20260101000000")
    rec = go(root, fake, NOW + 7 * DAY + dt.timedelta(hours=3))
    assert rec.counts["http.via.wayback"] == 1
    row = view(root)[d]
    second = next(s for s in raws(row) if s["snapshot_id"] != first["snapshot_id"])
    assert (second["via"], second["final_url"], second["as_of"]) == (
        "wayback", newer, dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert row["content"]["current_snapshot_id"] == second["snapshot_id"]
    checks_ok(root)


def test_http_5xx_backs_off(tmp_path: Path) -> None:
    """A persistent 503: four requests with 2/4/8 s backoff inside the episode (the injected
    sleep), no capture on the archive, role ``failed_transient[http_5xx]`` with ``retry_after =
    now`` (the skeleton's first retry is due at once, round 4 passes); the next run retries and
    fails again (attempt 2, due 2 h later), a run before then does not call the lane."""
    url, d = url_of(1), sid(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = fake_web("blog.test")
    fake.add(url, status=503)
    no_capture(fake, url)
    sleeps: list[float] = []
    rec = go(root, fake, NOW, sleeps=sleeps)
    assert [s for s in sleeps if s in (2.0, 4.0, 8.0)] == [2.0, 4.0, 8.0]
    assert fake.urls().count(url) == 4 and rec.counts["http.requests"] == 5
    role = page_role(view(root)[d])
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_transient", "http_5xx", 1, NOW)
    assert raws(view(root)[d]) == [] and view(root)[d]["content"]["current_text_status"] == "no_raw"

    go(root, fake, NOW + dt.timedelta(minutes=2))
    role = page_role(view(root)[d])
    assert (role["attempts"], role["retry_after"]) == (2, NOW + dt.timedelta(minutes=2, hours=2))
    n = len(fake.calls)
    go(root, fake, NOW + dt.timedelta(minutes=30))
    assert len(fake.calls) == n
    fake.add(url, body=PAGE.format("Back."))
    go(root, fake, NOW + dt.timedelta(hours=3))
    role = page_role(view(root)[d])
    assert (role["status"], role["attempts"]) == ("ok", 0)


def test_http_429_is_transient_for_that_role(tmp_path: Path) -> None:
    """A host answering 429 fails only its own role (``failed_transient[rate_limit]``, the
    ``Retry-After`` honoured as ``retry_after``, not counted toward the cap: ``attempts`` 0, D484);
    another host's page in the same run is fetched,
    and the run is complete (no halt: a provider-level halt is the OA/arXiv lanes' business). The
    7200 s ``Retry-After`` is over D588's 120 s cap, so the session does not retry (before: four
    requests 2/4/8 s apart)."""
    slow, fast = url_of(1, "busy.test"), url_of(2, "calm.test")
    root = store(tmp_path, [page_row(1, slow), page_row(2, fast)])
    fake = fake_web("busy.test", "calm.test")
    fake.add(slow, status=429, headers={"Retry-After": "7200"})
    no_capture(fake, slow)
    fake.add(fast, body=PAGE.format("Calm page."))
    rec = go(root, fake, NOW)
    assert rec.scope_complete and "halted" not in rec.counts
    v = view(root)
    busy, calm = page_role(v[sid(1)]), page_role(v[sid(2)])
    assert (busy["status"], busy["error_class"], busy["attempts"]) == ("failed_transient", "rate_limit", 0)
    assert busy["retry_after"] == NOW + dt.timedelta(seconds=7200)  # the hint beats the 1 h backoff
    assert fake.urls().count(slow) == 1  # D588: a stated wait over the 120 s cap stands at once
    assert calm["status"] == "ok" and len(raws(v[sid(2)])) == 1
    checks_ok(root)


def test_http_403_is_transient_until_the_cap(tmp_path: Path) -> None:
    """A page answering 403 (bot-block or refusal): ``failed_transient[forbidden]`` with the
    skeleton's backoff on attempts 1-3, ``failed_permanent[blocked]`` on attempt 4 (never
    ``quarantined``: the reason stays readable); after that the lane is not called again. A page
    that recovers before the cap is fetched and its attempts reset. A 451 is permanent at once."""
    url, ok_url, legal = url_of(1), url_of(2), url_of(3)
    root = store(tmp_path, [page_row(1, url), page_row(2, ok_url), page_row(3, legal)])
    fake = fake_web("blog.test")
    for u in (url, ok_url, legal):
        no_capture(fake, u)
    fake.add(url, status=403)
    fake.add(ok_url, status=403)
    fake.add(legal, status=451)
    at = NOW
    for attempt in (1, 2, 3):
        go(root, fake, at)
        v = view(root)
        role = page_role(v[sid(1)])
        assert (role["status"], role["error_class"], role["attempts"]) == ("failed_transient", "forbidden", attempt)
        backoff = dt.timedelta(0) if attempt == 1 else dt.timedelta(hours=2 ** (attempt - 1))  # first retry at once
        assert role["retry_after"] == at + backoff and "HTTP 403" in role["error"]
        if attempt == 1:
            assert fake.urls().count(url) == 1  # a 403 is an answer: no retry inside the episode
            lg = page_role(v[sid(3)])
            assert (lg["status"], lg["error_class"], lg["retry_after"]) == ("failed_permanent", "blocked", at + 90 * DAY)
            fake.add(ok_url, body=PAGE.format("Unblocked."))
        if attempt == 2:
            good = page_role(v[sid(2)])
            assert (good["status"], good["attempts"], len(raws(v[sid(2)]))) == ("ok", 0, 1)
        at = role["retry_after"]
    go(root, fake, at)
    role = page_role(view(root)[sid(1)])
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_permanent", "blocked", 4, at + 90 * DAY)  # R4-2: rechecked after 90 days
    n = len(fake.calls)
    rec = go(root, fake, at + 30 * DAY)
    assert fake.urls()[n:].count(url) == 0 and rec.counts.get("lane_calls.http", 0) <= 1
    checks_ok(root)


def test_http_card_landing_page_resolves_its_pdf(tmp_path: Path) -> None:
    """A ``*_cards`` member expects a PDF: the landing page's ``citation_pdf_url`` is fetched and
    its PDF is the snapshot (``url`` the PDF's), the role's validators stay the page's."""
    url, d = "https://lab.test/cards/opus", sid(1)
    root = store(tmp_path, [page_row(1, url, channel="anthropic_cards")])
    fake = fake_web("lab.test")
    pdf_url = "https://lab.test/files/opus-card.pdf"
    fake.add(url, body='<!doctype html><html><head><meta name="citation_pdf_url" content="/files/opus-card.pdf">'
             "</head><body>card</body></html>", headers={"ETag": '"page"', "Content-Type": "text/html"})
    pdf = b"%PDF-1.7\n" + b"x" * 20_000
    fake.add(pdf_url, body=pdf)
    rec = go(root, fake, NOW, plan=http_plan("anthropic_cards"))
    assert rec.counts["http.landing_resolved"] == 1
    row = view(root)[d]
    (snap,) = raws(row)
    assert (snap["raw"]["media_type"], snap["url"], snap["raw"]["bytes"]) == ("application/pdf", pdf_url, len(pdf))
    role = page_role(row)
    assert (role["etag"], role["validator_url"], role["tried_urls"]) == ('"page"', url, [url, pdf_url])


def test_http_google_doc_link_fetches_its_text_export(tmp_path: Path) -> None:
    """Round 4 L4: a Google Doc link row's ``canonical_url`` is ``stack.urls.fetch_url``'s plain-text
    export; the http lane takes the ``text/plain`` body as it is (stamped ``application/octet-stream``,
    :mod:`stack.mediatype`) and convert passes it through as text, not the editor's UI chrome."""
    from stack.conversion.ladder import classify, convert
    from stack.urls import fetch_url

    doc = "1" + "SyntheticDocA" * 3 + "x" * 4  # a synthetic document id (D796)
    url = fetch_url(f"https://docs.google.com/document/d/{doc}/edit?usp=sharing")
    assert url == f"https://docs.google.com/document/d/{doc}/export?format=txt"
    root = store(tmp_path, [page_row(1, url, channel="links_url")])
    fake = fake_web("docs.google.com")
    text = "\ufeffAn agenda for control evaluations\r\n\r\nWe propose three protocols ...\r\n"
    fake.add(url, body=text.encode(), headers={"Content-Type": "text/plain; charset=utf-8"})
    rec = go(root, fake, NOW, plan=http_plan("links_url"))
    assert rec.counts["snapshots_minted"] == 1 and fake.urls().count(url) == 1
    (snap,) = raws(view(root)[sid(1)])
    assert (snap["raw"]["media_type"], snap["url"]) == ("application/octet-stream", url)
    raw = open_blobs(root).get(snap["raw"]["sha256"])
    assert classify(snap["raw"]["media_type"], raw) == "text"
    res = convert(raw, snap["raw"]["media_type"], workdir=tmp_path, origin="acquire")
    assert res.status == "ok" and "We propose three protocols" in res.markdown


# ================================================================ lane arxiv + history (T3.3)
ATOM_API = "https://export.arxiv.org/api/query"


def aid_of(i: int) -> str:
    return f"2610.{i:05d}"  # document_row's arXiv key


def arxiv_plan() -> Plan:
    return http_plan("arxiv", "full_text")


def atom_entry(i: int, v: int) -> str:
    """Row ``i``'s Atom entry at current version ``v``: ``<published>`` is v1's date (2026-10-05),
    ``<updated>`` the current version's (v1: the same; v``n``: 2026-10-1``n``), as ``id_list``
    answers a bare id."""
    updated = "2026-10-05T10:00:00Z" if v == 1 else f"2026-10-1{v}T09:00:00Z"
    return (f"<entry><id>http://arxiv.org/abs/{aid_of(i)}v{v}</id><published>2026-10-05T10:00:00Z</published>"
            f"<updated>{updated}</updated><title>Paper {i}</title></entry>")


def atom(fake: FakeHttp, versions: dict[int, int]) -> None:
    """The Atom answer for one batch of ``{row i: current version}`` (ids in sorted order)."""
    ids = sorted(aid_of(i) for i in versions)
    entries = "".join(atom_entry(i, v) for i, v in versions.items())
    fake.add(ATOM_API, {"id_list": ",".join(ids), "max_results": len(ids)},
             body=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>')


def utc_day(day: int) -> dt.datetime:
    return dt.datetime(2026, 10, day, tzinfo=dt.UTC)


def no_oai(fake: FakeHttp) -> None:
    """No request went to arXiv's OAI-PMH host (the history's per-paper listing is gone)."""
    assert not any("oaipmh" in u for u in fake.urls()), fake.urls()


def pdf_of(i: int, v: int) -> bytes:
    return b"%PDF-1.7\n" + f"paper {i} v{v} ".encode() * 2_000


def full_role(row: Row) -> Row:
    (r,) = [r for r in row["content"]["roles"] if r["content_role"] == "full_text"]
    return r


def arxiv_row(i: int) -> Row:
    return document_row(i, state="triaged")  # channel arxiv, key 2610.0000i, content NULL


def test_arxiv_v2_moves_the_pointer(tmp_path: Path) -> None:
    """Run 1: the Atom answer lists v1 (its date, no title), the lane mints the v1 PDF
    (``revision = 'v1'``); the ``ok`` role's next check is a week later. Run 2, a day later (v2 is
    out, but the role was checked within the week and v1 is the newest listed and held: v3's skip
    rule): no request at all, nothing written. Run 3, past the week: the Atom sweep says v2 (listed
    from the same answer), the role is made due, the lane mints v2 and the pointer moves to it
    (``current_revision = 'v2'``). Run 4, a day later: again no request. No OAI-PMH request."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    atom(fake, {1: 1})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v1", body=pdf_of(1, 1), headers={"ETag": '"v1"'})
    sleeps: list[float] = []
    rec = go(root, fake, NOW, plan=arxiv_plan(), sleeps=sleeps)
    assert rec.counts["arxiv.downloads"] == 1 and rec.counts["arxiv.atom_requests"] == 1
    c = view(root)[d]["content"]
    (v1,) = raws(view(root)[d])
    assert (v1["revision"], v1["url"], v1["raw"]["media_type"], v1["etag"]) == (
        "v1", f"https://arxiv.org/pdf/{aid_of(1)}v1", "application/pdf", '"v1"')
    assert (c["current_snapshot_id"], c["current_revision"]) == (v1["snapshot_id"], "v1")
    assert c["revisions"] == [{"revision": "v1", "published_at": utc_day(5), "title": None}]
    role = full_role(view(root)[d])
    assert (role["status"], role["lane"], role["retry_after"], role["validator_url"]) == (
        "ok", "arxiv", NOW + 7 * DAY, v1["url"])

    atom(fake, {1: 2})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v2", body=pdf_of(1, 2))
    n = len(fake.calls)
    rec = go(root, fake, NOW + DAY, plan=arxiv_plan())
    assert fake.calls[n:] == [] and rec.counts["arxiv.history_fresh"] == 1
    assert rec.counts["rows_written"] == 0 and rec.counts.get("lane_calls", 0) == 0

    later = NOW + 8 * DAY
    rec = go(root, fake, later, plan=arxiv_plan())
    assert rec.counts["arxiv.requeued"] == 1 and rec.counts["arxiv.downloads"] == 1
    row = view(root)[d]
    c = row["content"]
    v2 = next(s for s in raws(row) if s["revision"] == "v2")
    assert len(raws(row)) == 2 and v2["raw"]["sha256"] != v1["raw"]["sha256"]
    assert (c["current_snapshot_id"], c["current_revision"]) == (v2["snapshot_id"], "v2")
    assert [(r["revision"], r["published_at"], r["title"]) for r in c["revisions"]] == [
        ("v1", utc_day(5), None), ("v2", utc_day(12), None)]
    role = full_role(row)
    assert (role["status"], role["attempts"], role["retry_after"], role["snapshot_id"]) == (
        "ok", 0, later + 7 * DAY, v2["snapshot_id"])

    n = len(fake.calls)
    rec = go(root, fake, later + DAY, plan=arxiv_plan())
    assert fake.calls[n:] == [] and rec.counts["arxiv.history_fresh"] == 1
    assert rec.counts["rows_written"] == 0 and rec.counts.get("lane_calls", 0) == 0
    no_oai(fake)
    checks_ok(root)


def test_arxiv_history_rechecks_after_the_week(tmp_path: Path) -> None:
    """The other side of v3's skip rule: an ``ok`` role whose week has passed (or which carries no
    next check, e.g. imported) is swept again; when ``vN`` is still current, held and listed, the
    sweep stamps the role checked (``retry_after = now + 7 d``, nothing fetched, one Atom
    request), and the next run within the week asks nothing. A role whose newest listed revision
    is not held is never skipped, however recent."""
    from workflows.acquire.lanes.arxiv import history_fresh

    d = sid(1)
    root = store(tmp_path, [document_row(1, state="acquired")])  # v1 held, revisions [v1]
    assert full_role(view(root)[d])["retry_after"] is None
    fake = FakeHttp()
    atom(fake, {1: 1})
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert [c.url.split("?")[0] for c in fake.calls] == [ATOM_API]
    assert rec.counts["arxiv.history_checked"] == 1 and rec.counts.get("lane_calls", 0) == 0
    role = full_role(view(root)[d])
    assert (role["status"], role["retry_after"]) == ("ok", NOW + 7 * DAY)

    n = len(fake.calls)
    go(root, fake, NOW + 6 * DAY, plan=arxiv_plan())
    assert fake.calls[n:] == []
    go(root, fake, NOW + 7 * DAY, plan=arxiv_plan())
    assert [c.url.split("?")[0] for c in fake.calls[n:]] == [ATOM_API]

    c = view(root)[d]["content"]
    role = full_role(view(root)[d])
    assert history_fresh(role, c, NOW + 8 * DAY)
    unheld = {**c, "revisions": [*c["revisions"], {"revision": "v2", "published_at": None, "title": None}]}
    assert not history_fresh(role, unheld, NOW + 8 * DAY)
    assert not history_fresh(role, {**c, "revisions": []}, NOW + 8 * DAY)
    assert not history_fresh({**role, "status": "failed_transient"}, c, NOW + 8 * DAY)
    checks_ok(root)


#: Requests one limited 503 answer takes on the fake transport: the session's first try and its three
#: 5xx retries (``stack.http.MAX_HTTP_RETRIES``).
SENDS = 4
#: Requests one limited 429 answer takes: one, the session returns a 429 to the arXiv lane at once
#: (``wait_limits=False``, D588; before, its three retries met the Gate's cooldown on the real
#: transport and sent nothing, and on this fake transport took three more 429s).
SENDS_429 = 1


def limit_waits(sleeps: list[float]) -> list[float]:
    """The lane's limit waits among the run's sleeps (D584: >= 60 s; the session's retry backoff
    and the pacer's waits are shorter)."""
    return [s for s in sleeps if s >= 60]


@pytest.mark.parametrize("where", ["pdf"])
def test_arxiv_slow_down_halts_the_lane(tmp_path: Path, where: str) -> None:
    """A 429 or 503 from an arXiv PDF or HTML URL (after the session's retries) is waited out and
    asked again (D584); the fourth such answer in a row halts the ``arxiv`` lane for the run (T3.6-f2:
    that lane only): no row is charged (roles stay ``not_attempted``, ``attempts = 0``, marked
    ``halted``), nothing later is asked of arXiv, no row is unreached, the run is ``ok`` with
    ``scope_complete = false``; the next run, unthrottled, fetches both. (Atom's 429/503 does not
    halt, D411: :func:`test_arxiv_atom_slow_down_charges_its_roles_and_the_lane_goes_on`.) Before
    D584 the first 503 halted; this test injected four 503s (one answer) and now injects four answers."""
    root = store(tmp_path, [arxiv_row(1), arxiv_row(2)])
    fake = FakeHttp()
    atom(fake, {1: 1, 2: 1})
    for i in (1, 2):
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf_of(i, 1))
    fake.inject(f"https://arxiv.org/pdf/{aid_of(1)}v1", status=503, times=SENDS * LIMIT_GIVE_UP)
    sleeps: list[float] = []
    rec = go(root, fake, NOW, plan=arxiv_plan(), sleeps=sleeps)
    assert not rec.scope_complete and rec.counts["halted"] == 1 and rec.counts["arxiv.halted"] == 1
    assert rec.counts["halted.arxiv"] == 1 and rec.counts["unreached"] == 0
    assert "arxiv.downloads" not in rec.counts
    assert limit_waits(sleeps) == [60.0, 120.0, 240.0] and rec.counts["arxiv.limited_waits.web"] == 3
    asked = [c.url.split("?")[0] for c in fake.calls]
    assert asked.count(f"https://arxiv.org/pdf/{aid_of(1)}v1") == SENDS * LIMIT_GIVE_UP  # no tight loop
    assert asked.count(f"https://arxiv.org/pdf/{aid_of(2)}v1") == 0
    v = view(root)
    for i in (1, 2):  # both written (the halt is not a stop), neither charged
        role = full_role(v[sid(i)])
        assert (role["status"], role["error_class"], role["attempts"]) == ("not_attempted", "halted", 0), role

    rec = go(root, fake, NOW + DAY, plan=arxiv_plan())
    assert rec.scope_complete and rec.counts["arxiv.downloads"] == 2
    assert {full_role(r)["status"] for r in view(root).values()} == {"ok"}
    checks_ok(root)


def test_arxiv_pdf_403_is_transient(tmp_path: Path) -> None:
    """arXiv's PDF host answering 403 (a bot-block in front of a real paper): the role is
    ``failed_transient[forbidden]`` with backoff (not ``failed_permanent[blocked]`` at once); the
    run is not halted (a 403 is not arXiv's "slow down")."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    atom(fake, {1: 1})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v1", status=403)
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert rec.scope_complete and "halted" not in rec.counts
    role = full_role(view(root)[d])
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_transient", "forbidden", 1, NOW)  # the first retry is due at once
    checks_ok(root)


EMPTY_FEED = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'


def empty_atom(fake: FakeHttp, *rows: int) -> None:
    ids = sorted(aid_of(i) for i in rows)
    fake.add(ATOM_API, {"id_list": ",".join(ids), "max_results": len(ids)}, body=EMPTY_FEED)


def test_arxiv_atom_unknown_is_transient_then_rechecked(tmp_path: Path) -> None:
    """R4-2 (the reviewer's case): a healthy feed without the id is ``failed_transient[network]``
    (``atom_unknown``), not dead at once; at the cap (attempt 4) it is ``failed_permanent[dead]``
    with the 90-day recheck: not called within 30 days, called again after 90, and a feed that now
    lists the paper gives ``ok`` with the PDF."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    empty_atom(fake, 1)
    at = NOW
    for attempt in (1, 2, 3, 4):
        go(root, fake, at, plan=arxiv_plan())
        role = full_role(view(root)[d])
        if attempt < 4:
            assert (role["status"], role["error_class"], role["attempts"]) == ("failed_transient", "network", attempt)
            assert "atom_unknown" in role["error"]
            at = role["retry_after"]
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_permanent", "dead", 4, at + 90 * DAY)
    n = len(fake.calls)
    rec = go(root, fake, at + 30 * DAY, plan=arxiv_plan())
    assert fake.calls[n:] == [] and rec.counts.get("lane_calls", 0) == 0
    atom(fake, {1: 1})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v1", body=pdf_of(1, 1))
    go(root, fake, at + 91 * DAY, plan=arxiv_plan())
    row = view(root)[d]
    role = full_role(row)
    assert (role["status"], role["attempts"], len(raws(row))) == ("ok", 0, 1)
    checks_ok(root)


def test_arxiv_empty_batch_is_not_dead(tmp_path: Path) -> None:
    """R4-2: one empty answer to a batch of several ids is an unusable page (one retry, then
    ``atom_unavailable``: ``failed_transient[network]`` for each), never ``dead`` for all."""
    root = store(tmp_path, [arxiv_row(1), arxiv_row(2)])
    fake = FakeHttp()
    empty_atom(fake, 1, 2)
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert rec.counts["arxiv.atom_unresolved"] == 2 and rec.counts["arxiv.atom_retries"] == 1
    for i in (1, 2):
        role = full_role(view(root)[sid(i)])
        assert (role["status"], role["error_class"], role["attempts"]) == ("failed_transient", "network", 1)
        assert "atom_unavailable" in role["error"]
    checks_ok(root)


def test_arxiv_200_non_pdf_is_transient_until_the_cap(tmp_path: Path) -> None:
    """R4-2: arXiv answering 200 with an HTML interstitial in place of the PDF is
    ``failed_transient[forbidden]`` for attempts 1-3 and ``failed_permanent[dead]`` (with the
    90-day recheck) at the cap; before the cap a real PDF is taken."""
    d = sid(1)
    root = store(tmp_path, [arxiv_row(1)])
    fake = FakeHttp()
    atom(fake, {1: 1})
    pdf = f"https://arxiv.org/pdf/{aid_of(1)}v1"
    fake.add(pdf, body="<html><body>Please verify you are a human</body></html>")
    at = NOW
    for attempt in (1, 2, 3, 4):
        go(root, fake, at, plan=arxiv_plan())
        role = full_role(view(root)[d])
        assert "HTTP 200 but not a PDF" in role["error"]
        if attempt < 4:
            assert (role["status"], role["error_class"], role["attempts"]) == ("failed_transient", "forbidden", attempt)
            at = role["retry_after"]
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_permanent", "dead", 4, at + 90 * DAY)
    checks_ok(root)


def test_http_dns_failure_is_transient(tmp_path: Path) -> None:
    """R4-2: "Name or service not known" (the getter's ``dns`` error) is ``failed_transient[network]``
    (a resolver outage looks the same as a lapsed domain), not ``failed_permanent[dead]``."""
    url, d = url_of(1, "gone.test"), sid(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = FakeHttp()
    fake.inject("https://gone.test/robots.txt", status=None, error="dns", times=100)
    fake.inject(url, status=None, error="dns", times=100)
    no_capture(fake, url)
    go(root, fake, NOW)
    role = page_role(view(root)[d])
    assert (role["status"], role["error_class"], role["attempts"], role["retry_after"]) == (
        "failed_transient", "network", 1, NOW)
    assert "dns" in role["error"]
    checks_ok(root)


def atom_in_order(fake: FakeHttp, order: list[int]) -> None:
    """The Atom answer (every paper at v1) for one batch asked in exactly ``order``."""
    ids = [aid_of(i) for i in order]
    entries = "".join(atom_entry(i, 1) for i in order)
    fake.add(ATOM_API, {"id_list": ",".join(ids), "max_results": len(ids)},
             body=f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>')


def test_arxiv_history_sweeps_only_the_rows_the_run_fetches_newest_first(tmp_path: Path) -> None:
    """D410: 250 new arXiv papers, ``max_rows`` 5, ``order: newest_first``: the history covers one
    round of :data:`~stack.arxiv.ATOM_BATCH` rows in the run's order, i.e. one Atom request whose
    ``id_list`` starts with the five newest ids; the five newest are fetched, the other rows are
    unreached and nothing is marked unresolved (before D410: three requests over the whole scope,
    in ascending id order)."""
    n = 250
    rows = [arxiv_row(i) for i in range(1, n + 1)]
    for i, row in enumerate(rows, 1):
        row["metadata"]["published_at"] = AT + i * DAY  # paper 250 is the newest
    root = store(tmp_path, rows)
    fake = FakeHttp()
    newest = list(range(n, 0, -1))
    atom_in_order(fake, newest[:100])
    for lo in range(1, n + 1, 100):  # the whole-scope sweep's batches, so the old code runs to the end
        atom(fake, {i: 1 for i in range(lo, min(lo + 100, n + 1))})
    for i in newest[:5]:
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf_of(i, 1))
    rec = go(root, fake, NOW, plan=http_plan("arxiv", "full_text", max_rows=5, order="newest_first"))
    asked = [c.url.split("?")[0] for c in fake.calls]
    assert asked.count(ATOM_API) == 1 and rec.counts["arxiv.atom_requests"] == 1, rec.counts
    assert fake.calls[0].url.split("id_list=")[1].startswith("%2C".join(aid_of(i) for i in newest[:5]))
    assert sorted(asked[1:]) == sorted(f"https://arxiv.org/pdf/{aid_of(i)}v1" for i in newest[:5])
    assert "arxiv.atom_unresolved" not in rec.counts and rec.counts["unreached"] == n - 5
    v = view(root)
    assert {d for d, r in v.items() if r["content"] is not None} == {sid(i) for i in newest[:5]}
    assert all(full_role(v[sid(i)])["status"] == "ok" for i in newest[:5])


def test_arxiv_episode_sweeps_a_batch_of_the_runs_due_ids(tmp_path: Path) -> None:
    """D410: under ``--only fetch`` (no history) the first episode asks Atom for its id and the run's
    next due ids in one request (``ArxivLane.prepare``), not one request per paper."""
    root = store(tmp_path, [arxiv_row(i) for i in (1, 2, 3)])
    fake = FakeHttp()
    atom(fake, {1: 1, 2: 1, 3: 1})
    for i in (1, 2, 3):
        atom(fake, {i: 1})  # the one-id requests of the old code, so it runs to the end
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf_of(i, 1))
    with lanes.using(now=NOW, transport=fake, sleep=lambda s: None):
        rec = run_stage(root, arxiv_plan(), "acquire", [sdir("documents")], series="main", only=["fetch"])
    assert rec.status == "ok", rec.error
    assert rec.counts["arxiv.atom_requests"] == 1 and rec.counts["arxiv.downloads"] == 3, rec.counts


def test_arxiv_atom_slow_down_charges_its_roles_and_the_lane_goes_on(tmp_path: Path) -> None:
    """D411: 101 new papers; Atom answers the first batch (100 ids) and 429s the second (one id, after
    ``Retry-After: 7200``). The lane does not halt: the 100 answered papers
    are fetched; the unanswered one is ``failed_transient[rate_limit]``, not counted toward the cap
    (``attempts`` 0, D484; charged before it, D402's precedent), due at the hint; no Atom request after the 429 (the sweep stops for the run). The next run,
    unthrottled, fetches it."""
    n = 101
    root = store(tmp_path, [arxiv_row(i) for i in range(1, n + 1)])
    fake = FakeHttp()
    atom(fake, {i: 1 for i in range(1, 101)})
    atom(fake, {101: 1})
    fake.inject(ATOM_API, {"id_list": aid_of(101), "max_results": 1}, status=429, times=SENDS_429, retry_after=7200)
    for i in range(1, n + 1):
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf_of(i, 1))
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert "halted" not in rec.counts and "arxiv.halted" not in rec.counts, rec.counts
    assert rec.counts["arxiv.atom_limited"] == 1 and rec.counts["arxiv.downloads"] == 100
    asked = [c.url.split("?")[0] for c in fake.calls]
    assert ATOM_API not in asked[asked.index(f"https://arxiv.org/pdf/{aid_of(1)}v1"):]
    assert f"https://arxiv.org/pdf/{aid_of(101)}v1" not in asked
    late = full_role(view(root)[sid(101)])
    assert (late["status"], late["error_class"], late["attempts"], late["retry_after"]) == (
        "failed_transient", "rate_limit", 0, NOW + dt.timedelta(hours=2)), late
    assert full_role(view(root)[sid(100)])["status"] == "ok"

    rec = go(root, fake, NOW + 3 * dt.timedelta(hours=1), plan=arxiv_plan())
    assert rec.counts["arxiv.downloads"] == 1 and full_role(view(root)[sid(101)])["status"] == "ok"
    checks_ok(root)


@pytest.mark.parametrize("retry_after", [None, 90], ids=["bare-429", "retry-after-90"])
def test_arxiv_one_atom_429_is_waited_out_and_the_sweep_resumes(tmp_path: Path, retry_after: int | None) -> None:
    """D584 (a): one limited Atom answer (a 429, returned by the session at once, D588) is waited out (60 s, or
    the stated ``Retry-After`` when longer) and the batch asked again: every paper is fetched, no
    ``atom_limited``, no role failed; ``arxiv.limited_waits.atom`` 1. Before D584 the first Atom 429
    stopped the sweeps for the run and failed every unanswered role without a request."""
    root = store(tmp_path, [arxiv_row(i) for i in (1, 2, 3)])
    fake = FakeHttp()
    atom(fake, {1: 1, 2: 1, 3: 1})
    ids = ",".join(aid_of(i) for i in (1, 2, 3))
    fake.inject(ATOM_API, {"id_list": ids, "max_results": 3}, status=429, times=SENDS_429, retry_after=retry_after)
    for i in (1, 2, 3):
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf_of(i, 1))
    sleeps: list[float] = []
    rec = go(root, fake, NOW, plan=arxiv_plan(), sleeps=sleeps)
    assert limit_waits(sleeps) == [float(retry_after or 60)]
    assert rec.counts["arxiv.limited_waits.atom"] == 1 and rec.counts["arxiv.limited_wait_s"] == (retry_after or 60)
    assert "arxiv.atom_limited" not in rec.counts and "arxiv.limited_roles" not in rec.counts
    assert rec.counts["arxiv.downloads"] == 3 and rec.scope_complete
    assert [c.url.split("?")[0] for c in fake.calls].count(ATOM_API) == SENDS_429 + 1
    assert {full_role(r)["status"] for r in view(root).values()} == {"ok"}


def test_arxiv_atom_limited_four_times_gives_up_for_the_run(tmp_path: Path) -> None:
    """D584 (b): four limited Atom answers in a row (60, 120, 240 s waited between them) give up
    Atom for the run (D411): every role is ``failed_transient[rate_limit]`` without a request, not
    counted (D484), ``arxiv.limited_roles`` 3, no halt; exactly four answers' requests, no tight loop.
    The next run, unthrottled, fetches them."""
    root = store(tmp_path, [arxiv_row(i) for i in (1, 2, 3)])
    fake = FakeHttp()
    atom(fake, {1: 1, 2: 1, 3: 1})
    ids = ",".join(aid_of(i) for i in (1, 2, 3))
    fake.inject(ATOM_API, {"id_list": ids, "max_results": 3}, status=429, times=SENDS_429 * LIMIT_GIVE_UP)
    for i in (1, 2, 3):
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf_of(i, 1))
    sleeps: list[float] = []
    rec = go(root, fake, NOW, plan=arxiv_plan(), sleeps=sleeps)
    assert limit_waits(sleeps) == [60.0, 120.0, 240.0]
    assert rec.counts["arxiv.atom_limited"] == 1 and rec.counts["arxiv.limited_roles"] == 3
    assert "halted" not in rec.counts and "arxiv.downloads" not in rec.counts
    assert [c.url.split("?")[0] for c in fake.calls] == [ATOM_API] * SENDS_429 * LIMIT_GIVE_UP
    for r in view(root).values():
        role = full_role(r)
        assert (role["status"], role["error_class"], role["attempts"]) == ("failed_transient", "rate_limit", 0)
        assert "4 in a row" in role["error"]

    rec = go(root, fake, NOW + DAY, plan=arxiv_plan())
    assert rec.counts["arxiv.downloads"] == 3 and {full_role(r)["status"] for r in view(root).values()} == {"ok"}
    checks_ok(root)


def html_only_plan() -> Plan:
    """The arXiv ``html`` role alone (``arxiv_pdf: never``: the PDF role is never requested); the
    convert config ``options.structured`` needs, run without convert (:func:`go_fetch`)."""
    return Plan.model_validate({"plan": "test-lanes", "schema_version": 2,
                                "acquire": {"wants": {"arxiv": ["full_text", "html"]},
                                            "convert": {"config": "pymupdf4llm-v1"}},
                                "stages": [{"stage": "acquire", "options": {
                                    "structured": {"arxiv_html": True}, "arxiv_pdf": "never",
                                    "allow_degraded_convert": True}}]})


def go_fetch(root: Path, fake: FakeHttp, at: dt.datetime, plan: Plan, sleeps: list[float] | None = None) -> Any:
    """:func:`go` without convert (history and fetch, and the pure substages)."""
    with lanes.using(now=at, transport=fake, sleep=sleeps.append if sleeps is not None else (lambda s: None)):
        rec = run_stage(root, plan, "acquire", [sdir("documents")], series="main", only=["history", "fetch"])
    assert rec.status == "ok", rec.error
    assert "lane_crashed" not in rec.counts, "a lane crashed (an unrouted URL in the fake?)"
    return rec


def latexml(aid: str | None = None, v: int | None = None) -> bytes:
    """An acceptable LaTeXML page (>= 20 KB of HTML); with ``aid``/``v`` its ``<base href>`` names
    ``/html/<aid>v<v>/`` as arXiv's pages do."""
    base = f'<base href="/html/{aid}v{v}/">' if aid else ""
    return (f'<!DOCTYPE html><html><head>{base}<title>P</title></head><body><article class="ltx_document">'
            + "<p>text</p>" * 3000 + "</article></body></html>").encode()


def html_role(row: Row) -> Row:
    (r,) = [r for r in row["content"]["roles"] if r["content_role"] == "html"]
    return r


def test_arxiv_html_429_once_is_waited_out_and_the_lane_goes_on(tmp_path: Path) -> None:
    """D584 (c): one limited answer to an HTML probe is waited out (60 s) and the probe asked again;
    the lane does not halt (before D584 the first one halted every arXiv role of the run)."""
    root = store(tmp_path, [arxiv_row(1), arxiv_row(2)])
    fake = FakeHttp()
    atom(fake, {1: 1, 2: 1})
    for i in (1, 2):
        fake.add(f"https://arxiv.org/html/{aid_of(i)}v1", body=latexml())
    fake.inject(f"https://arxiv.org/html/{aid_of(1)}v1", status=429, times=SENDS_429)
    sleeps: list[float] = []
    rec = go_fetch(root, fake, NOW, html_only_plan(), sleeps=sleeps)
    assert limit_waits(sleeps) == [60.0] and rec.counts["arxiv.limited_waits.web"] == 1
    assert "halted" not in rec.counts and "arxiv.halted" not in rec.counts
    assert rec.counts["arxiv.html.downloads"] == 2
    v = view(root)
    assert [html_role(v[sid(i)])["status"] for i in (1, 2)] == ["ok", "ok"]


def test_arxiv_html_without_atom_probes_the_unversioned_url(tmp_path: Path) -> None:
    """D585 (d): Atom gives up (four limited answers); each ``html`` role then asks the bare
    ``arxiv.org/html/<id>``. Row 1's page names its version in ``<base href>`` (v2): minted as
    ``revision = 'v2'``, the URL asked as ``url``, the versioned URL as ``validator_url`` (so the next
    run's version check is the usual one). Row 2's page names none: nothing minted,
    ``failed_transient[rate_limit]``, and the bare URL is not asked again in the run, so row 3 is
    ``failed_transient[rate_limit]`` without a request (``arxiv.limited_roles`` 2)."""
    root = store(tmp_path, [arxiv_row(i) for i in (1, 2, 3)])
    fake = FakeHttp()
    ids = ",".join(aid_of(i) for i in (1, 2, 3))
    fake.inject(ATOM_API, {"id_list": ids, "max_results": 3}, status=429, times=SENDS_429 * LIMIT_GIVE_UP)
    atom(fake, {1: 2, 2: 1, 3: 1})
    fake.add(f"https://arxiv.org/html/{aid_of(1)}", body=latexml(aid_of(1), 2))
    fake.add(f"https://arxiv.org/html/{aid_of(2)}", body=latexml())
    rec = go_fetch(root, fake, NOW, html_only_plan())
    assert rec.counts["arxiv.atom_limited"] == 1 and rec.counts["arxiv.html.unversioned"] == 2
    assert rec.counts["arxiv.html.unversioned_unresolved"] == 1 and rec.counts["arxiv.limited_roles"] == 2
    assert rec.counts["arxiv.html.downloads"] == 1 and "halted" not in rec.counts
    asked = [c.url.split("?")[0] for c in fake.calls]
    assert f"https://arxiv.org/html/{aid_of(3)}" not in asked and not any("/pdf/" in u for u in asked)
    v = view(root)
    (snap,) = [s for s in raws(v[sid(1)]) if s["content_role"] == "html"]
    assert (snap["revision"], snap["url"]) == ("v2", f"https://arxiv.org/html/{aid_of(1)}")
    r1 = html_role(v[sid(1)])
    assert (r1["status"], r1["validator_url"]) == ("ok", f"https://arxiv.org/html/{aid_of(1)}v2")
    for i in (2, 3):
        r = html_role(v[sid(i)])
        assert (r["status"], r["error_class"], r["attempts"]) == ("failed_transient", "rate_limit", 0), r
    assert not raws(v[sid(2)]) or all(s["content_role"] != "html" for s in raws(v[sid(2)]))
    checks_ok(root)


def test_arxiv_history_is_one_atom_request_per_100_ids(tmp_path: Path) -> None:
    """250 new arXiv papers: the history asks exactly three Atom ``id_list`` pages (100 + 100 + 50
    ids) and nothing per paper (no OAI-PMH); each row's ``content.revisions`` lists its current
    ``vN`` (and v1 when N > 1, dated from the same answer); the lane mints every ``vN``."""
    n = 250
    version = {i: 1 + i % 3 for i in range(1, n + 1)}
    root = store(tmp_path, [arxiv_row(i) for i in version])
    fake = FakeHttp()
    for lo in range(1, n + 1, 100):
        atom(fake, {i: version[i] for i in range(lo, min(lo + 100, n + 1))})
    for i, v in version.items():
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v{v}", body=pdf_of(i, v))
    rec = go(root, fake, NOW, plan=arxiv_plan())
    asked = [c.url.split("?")[0] for c in fake.calls]
    assert asked.count(ATOM_API) == 3 and rec.counts["arxiv.atom_requests"] == 3
    assert len(asked) == 3 + n and rec.counts["arxiv.downloads"] == n
    no_oai(fake)
    v = view(root)
    for i, vn in version.items():
        c = v[sid(i)]["content"]
        want = [("v1", utc_day(5))] + ([(f"v{vn}", utc_day(10 + vn))] if vn > 1 else [])
        assert [(r["revision"], r["published_at"]) for r in c["revisions"]] == want, (i, c["revisions"])
        assert c["current_revision"] == f"v{vn}"


def test_arxiv_new_version_is_due_and_minted(tmp_path: Path) -> None:
    """v1 held (an acquired row, ``revisions`` [v1] with its finer date), Atom says v2: one Atom
    request, the role is made due (``arxiv.requeued``), the lane mints v2 and the pointer moves;
    ``revisions`` gains v2 (``<updated>``'s day) and keeps v1's own date."""
    d = sid(1)
    root = store(tmp_path, [document_row(1, state="acquired")])
    (v1,) = raws(view(root)[d])
    fake = FakeHttp()
    atom(fake, {1: 2})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v2", body=pdf_of(1, 2))
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert [c.url.split("?")[0] for c in fake.calls] == [ATOM_API, f"https://arxiv.org/pdf/{aid_of(1)}v2"]
    assert rec.counts["arxiv.requeued"] == 1 and rec.counts["arxiv.downloads"] == 1
    row = view(root)[d]
    c = row["content"]
    v2 = next(s for s in raws(row) if s["revision"] == "v2")
    assert (c["current_snapshot_id"], c["current_revision"]) == (v2["snapshot_id"], "v2") and v1 in raws(row)
    assert [(r["revision"], r["published_at"]) for r in c["revisions"]] == [("v1", AT), ("v2", utc_day(12))]
    assert full_role(row)["retry_after"] == NOW + 7 * DAY
    checks_ok(root)


#: A withdrawal as the Atom ``id_list`` API answers it (live probe 2026-10-13 of 2609.39891,
#: shape kept, id of row 1): the comment is the only trace; title, abstract and PDF link are the
#: paper's.
WITHDRAWN_ENTRY = (
    '<entry><id>http://arxiv.org/abs/{aid}v2</id><title>Improved existence time for a Boussinesq system</title>'
    '<updated>2026-10-02T01:57:55Z</updated><link href="https://arxiv.org/abs/{aid}v2" rel="alternate" '
    'type="text/html"/><link href="https://arxiv.org/pdf/{aid}v2" rel="related" type="application/pdf" '
    'title="pdf"/><summary>We consider a strongly dispersive Boussinesq-type system.</summary>'
    '<published>2026-09-30T14:58:39Z</published><arxiv:comment>Withdrawn by the authors because this version '
    'was submitted before both authors had approved the final manuscript</arxiv:comment></entry>')
#: arXiv's 404 page for a withdrawn version, as served for ``/pdf/2609.39891v2`` and
#: ``/html/2609.39891v2`` (byte-identical) on 2026-10-13.
WITHDRAWN_PAGE = Path(__file__).parent / "fixtures" / "arxiv_withdrawn" / "pdf-2609.39891v2.html"


def withdrawn_page(aid: str, v: int) -> bytes:
    """The saved withdrawal page, naming ``<aid>v<v>`` instead of the probed paper."""
    return WITHDRAWN_PAGE.read_bytes().replace(b"2609.39891v2", f"{aid}v{v}".encode())


def withdrawn_atom(fake: FakeHttp, i: int) -> None:
    fake.add(ATOM_API, {"id_list": aid_of(i), "max_results": 1},
             body='<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
                  + WITHDRAWN_ENTRY.format(aid=aid_of(i)) + '</feed>')


def test_arxiv_withdrawal_page_skips_and_keeps_the_text(tmp_path: Path) -> None:
    """An acquired paper whose v2 is a withdrawal: the Atom answer carries no marker the history
    reads, so the role is made due like any new version; the versioned PDF answers 404 with arXiv's
    "has been withdrawn and is unavailable" page, so the role ends ``skipped[withdrawn]``
    (``arxiv.withdrawn``), nothing is minted, the pointer stays on v1 and ``revisions`` lists v2.
    ``skipped`` is left alone afterwards: no request a day later, nor after 91 days (a reinstating
    v3 would not be noticed for this role)."""
    d = sid(1)
    root = store(tmp_path, [document_row(1, state="acquired")])  # v1 held, revisions [v1]
    before = view(root)[d]["content"]["current_snapshot_id"]
    fake = FakeHttp()
    pdf = f"https://arxiv.org/pdf/{aid_of(1)}v2"
    withdrawn_atom(fake, 1)
    fake.add(pdf, status=404, body=withdrawn_page(aid_of(1), 2), headers={"Content-Type": "text/html; charset=utf-8"})
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert [c.url.split("?")[0] for c in fake.calls] == [ATOM_API, pdf]
    assert rec.counts["arxiv.requeued"] == 1 and rec.counts["arxiv.withdrawn"] == 1
    assert "arxiv.downloads" not in rec.counts and rec.counts["snapshots_minted"] == 0
    row = view(root)[d]
    role = full_role(row)
    assert (role["status"], role["error_class"]) == ("skipped", "withdrawn")
    assert "has been withdrawn" in role["error"]
    assert row["content"]["current_snapshot_id"] == before and row["content"]["current_revision"] == "v1"
    assert [(r["revision"], r["published_at"]) for r in row["content"]["revisions"]] == [
        ("v1", AT), ("v2", dt.datetime(2026, 10, 2, tzinfo=dt.UTC))]

    n = len(fake.calls)
    for later in (NOW + DAY, NOW + 91 * DAY):
        rec = go(root, fake, later, plan=arxiv_plan())
        assert fake.calls[n:] == [] and rec.counts.get("lane_calls", 0) == 0
    no_oai(fake)
    checks_ok(root)


@pytest.mark.parametrize("page", ["plain", "other_version"])
def test_arxiv_other_404_is_dead(tmp_path: Path, page: str) -> None:
    """Only arXiv's withdrawal page naming the asked version is a withdrawal: a plain 404, or the
    withdrawal page of another version, leaves the role ``failed_permanent[dead]`` (90-day
    recheck), nothing withdrawn, the pointer on v1."""
    d = sid(1)
    root = store(tmp_path, [document_row(1, state="acquired")])
    before = view(root)[d]["content"]["current_snapshot_id"]
    fake = FakeHttp()
    pdf = f"https://arxiv.org/pdf/{aid_of(1)}v2"
    withdrawn_atom(fake, 1)
    body = b"<html><body>Not Found</body></html>" if page == "plain" else withdrawn_page(aid_of(1), 1)
    fake.add(pdf, status=404, body=body, headers={"Content-Type": "text/html; charset=utf-8"})
    rec = go(root, fake, NOW, plan=arxiv_plan())
    assert "arxiv.withdrawn" not in rec.counts
    row = view(root)[d]
    role = full_role(row)
    assert (role["status"], role["error_class"], role["retry_after"]) == ("failed_permanent", "dead", NOW + 90 * DAY)
    assert "HTTP 404" in role["error"] and row["content"]["current_snapshot_id"] == before
    checks_ok(root)


def test_arxiv_only_convert_makes_no_network(tmp_path: Path) -> None:
    """``--only convert`` over a row the history and the lane would both ask about (no revisions,
    a due role): the real lanes stay registered, the transport is a trap, nothing is asked."""
    from tests.fakes.lanes import TrapTransport

    root = store(tmp_path, [arxiv_row(1)])
    with lanes.using(now=NOW, transport=TrapTransport(), sleep=lambda s: None):
        rec = run_stage(root, arxiv_plan(), "acquire", [sdir("documents")], series="main", only=["convert"])
    assert rec.status == "ok", rec.error
    assert not rec.scope_complete and rec.counts["fetch_not_run"] == 1
    assert not any(k.startswith("arxiv.") for k in rec.counts)


# ================================================================ lane oa + xml (T3.4)
OA_CHANNEL = "openalex"  # channel group ``paper`` → lane ``oa`` (D11)
JATS = (b'<?xml version="1.0" encoding="UTF-8"?><article xmlns:xlink="http://www.w3.org/1999/xlink">'
        b"<front><article-meta><title-group><article-title>Paper</article-title></title-group></article-meta>"
        b"</front><body><sec><p>" + b"Structured body text. " * 400 + b"</p></sec></body></article>")


def doi_of(i: int) -> str:
    return f"10.5555/oa.{i}"


def oa_row(i: int) -> Row:
    row = document_row(i, state="triaged")
    row["identity"]["members"][0].update(channel=OA_CHANNEL, key_kind="doi", key_value=doi_of(i),
                                         canonical_url=f"https://doi.org/{doi_of(i)}")
    row["identity"]["channels"] = [OA_CHANNEL]
    return row


def oa_plan(convert: str | None = None, route: str = "walk") -> Plan:
    """The OA cases below drive fetchpdf's walk (``oa_route: walk``, every role through one
    ``retrieve_tiered``); the default ``direct`` route's runner cases pass ``route="direct"``."""
    acquire: dict[str, Any] = {"wants": {OA_CHANNEL: ["full_text", "xml"]}}
    options: dict[str, Any] = {"oa_route": route}
    if convert is not None:
        acquire["convert"] = {"config": convert}
        options["allow_degraded_convert"] = True  # any venv: the convert pin guard (R4-1) is lifted
    return Plan.model_validate({"plan": "test-lanes", "schema_version": 2, "acquire": acquire,
                                "stages": [{"stage": "acquire", "options": options}]})


def oa_go(root: Path, fetchpdf: Any, at: dt.datetime, plan: Plan | None = None,
          transport: FakeHttp | None = None, sleeps: list[float] | None = None) -> Any:
    from workflows.acquire.lanes.oa import OaLane

    with lanes.using({"oa": OaLane(load=fetchpdf.loader)}, now=at, transport=transport or FakeHttp(),
                     sleep=sleeps.append if sleeps is not None else (lambda s: None)):
        rec = run_stage(root, plan or oa_plan(), "acquire", [sdir("documents")], series="main")
    assert rec.status == "ok", rec.error
    assert "lane_crashed" not in rec.counts, "the OA lane crashed"
    return rec


def by_role(row: Row) -> dict[str, Row]:
    return {r["content_role"]: r for r in row["content"]["roles"]}


def snaps_of(row: Row, content_role: str) -> list[Row]:
    return [s for s in raws(row) if s["content_role"] == content_role]


@pytest.fixture
def mailto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACK_MAILTO", "lane-tests@example.invalid")  # a dummy; never a real address


def test_oa_pdf_is_full_text_and_xml_is_never_text(tmp_path: Path, mailto: None) -> None:
    """One retrieval serves both roles: the PDF lands as a ``full_text`` raw snapshot, is converted
    and is the pointer's text; the JATS lands as role ``xml``, ``ok``, never converted, never
    current. The resolver was asked once, by DOI."""
    from tests.test_acquire_convert import pdf_bytes

    d = sid(1)
    root = store(tmp_path, [oa_row(1)])
    pdf = pdf_bytes()
    fp = FakeFetchpdf({doi_of(1): [(JATS, "https://europepmc.test/jats/1", "europepmc"),
                                   (pdf, "https://journal.test/1.pdf", "unpaywall")]})
    rec = oa_go(root, fp, NOW, oa_plan("pymupdf4llm-v1"))
    assert [c["raw"] for c in fp.calls] == [doi_of(1)] and fp.calls[0]["doi"] == doi_of(1)
    assert fp.calls[0]["get_xml_or_html"] is True and rec.counts["oa.retrievals"] == 1
    row = view(root)[d]
    roles = by_role(row)
    assert {k: (r["status"], r["lane"]) for k, r in roles.items()} == {"full_text": ("ok", "oa"), "xml": ("ok", "oa")}
    (ft,) = snaps_of(row, "full_text")
    (xml,) = snaps_of(row, "xml")
    assert (ft["raw"]["media_type"], ft["url"], xml["raw"]["media_type"], xml["url"]) == (
        "application/pdf", "https://journal.test/1.pdf", "application/xml", "https://europepmc.test/jats/1")
    assert open_blobs(root).get(xml["raw"]["sha256"]) == JATS
    c = row["content"]
    assert (c["current_snapshot_id"], c["current_role"], c["current_text_status"]) == (
        ft["snapshot_id"], "full_text", "ok")
    assert (xml["text_status"], xml["convert_attempts"], xml["text"]) == (None, 0, None)
    assert roles["xml"]["snapshot_id"] == xml["snapshot_id"] and roles["full_text"]["snapshot_id"] == ft["snapshot_id"]
    checks_ok(root)


def test_oa_xml_alone_gives_no_text_pointer(tmp_path: Path, mailto: None) -> None:
    """Only a structured copy found: role ``xml`` is ``ok`` with its snapshot, ``full_text`` is
    ``failed_permanent[no_fetchable_url]`` (the resolver's reason kept), the pointer has no
    candidate (``no_raw``): an XML is never the text."""
    d = sid(1)
    root = store(tmp_path, [oa_row(1)])
    fp = FakeFetchpdf({doi_of(1): [(JATS, "https://europepmc.test/jats/1", "europepmc")]})
    rec = oa_go(root, fp, NOW)
    assert rec.counts["oa.no_artifact.full_text"] == 1
    row = view(root)[d]
    roles = by_role(row)
    assert (roles["xml"]["status"], len(snaps_of(row, "xml"))) == ("ok", 1)
    assert (roles["full_text"]["status"], roles["full_text"]["error_class"]) == ("failed_permanent", "no_fetchable_url")
    c = row["content"]
    assert (c["current_snapshot_id"], c["current_text_status"]) == (None, "no_raw")
    checks_ok(root)


def test_oa_licence_guard_refuses_then_a_good_build_fetches(tmp_path: Path, mailto: None) -> None:
    """A fetchpdf build that keeps a last-resort source reachable: no retrieval at all, both roles
    ``skipped[gated]`` (never charged) with the guard's reason. The next run on an open build
    re-opens them (``carry``: a gated role of a row triage lets fetch) and fetches."""
    d = sid(1)
    root = store(tmp_path, [oa_row(1)])
    fp = FakeFetchpdf({doi_of(1): [(b"%PDF-1.7\n" + b"x" * 20_000, "https://journal.test/1.pdf", "unpaywall")]},
                      build="stuck")
    rec = oa_go(root, fp, NOW)
    assert fp.calls == [] and rec.counts["oa.licence_refused"] == 2
    roles = by_role(view(root)[d])
    for r in roles.values():
        assert (r["status"], r["error_class"], r["attempts"]) == ("skipped", "gated", 0)
        assert "licence guard" in r["error"] and "last-resort" in r["error"]

    fp.build = "open"
    oa_go(root, fp, NOW + DAY)
    roles = by_role(view(root)[d])
    assert roles["full_text"]["status"] == "ok" and roles["xml"]["error_class"] == "no_fetchable_url"
    assert len(fp.calls) == 1
    checks_ok(root)


def test_oa_without_mailto_halts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``STACK_MAILTO`` unset: Unpaywall refuses anonymous callers, so the ``oa`` lane halts for
    the run before loading fetchpdf (no row charged, ``scope_complete = false``)."""
    monkeypatch.delenv("STACK_MAILTO", raising=False)
    root = store(tmp_path, [oa_row(1)])
    fp = FakeFetchpdf()
    rec = oa_go(root, fp, NOW)
    assert (fp.loads, fp.calls) == (0, [])
    assert not rec.scope_complete and rec.counts["halted"] == 1 and rec.counts["oa.no_mailto"] == 1
    assert rec.counts["halted.oa"] == 1


def test_oa_default_lane_never_reaches_real_fetchpdf_in_a_test_world(tmp_path: Path, mailto: None) -> None:
    """The registered default lane with an injected transport refuses to load the real fetchpdf:
    a loud lane crash, not a network call."""
    root = store(tmp_path, [oa_row(1)])
    with lanes.using(now=NOW, transport=FakeHttp(), sleep=lambda s: None):
        rec = run_stage(root, oa_plan(), "acquire", [sdir("documents")], series="main")
    assert rec.status == "ok" and rec.counts["lane_crashed"] == 2
    assert "OaLane(load=" in by_role(view(root)[sid(1)])["full_text"]["error"]


def test_oa_429_is_charged_as_transient_and_retried_later(tmp_path: Path, mailto: None) -> None:
    """fetchpdf's download of the PDF meets a 429 asking 7200 s, over D588's 120 s cap, so the gate
    does not retry it (before D588: 2/4/8 s backoff): ``full_text`` is ``failed_transient[rate_limit]``, not counted (``attempts = 0``, D484) with
    the ``Retry-After`` as ``retry_after``; ``xml`` came through and is ``ok``; the run is not
    halted (a publisher host, not a provider). After the hint the next run fetches the PDF."""
    d = sid(1)
    root = store(tmp_path, [oa_row(1)])
    fake = FakeHttp()
    fake.add("https://europepmc.test/jats/1", body=JATS)
    fake.add("https://journal.test/1.pdf", body=b"%PDF-1.7\n" + b"x" * 20_000)
    fake.inject("https://journal.test/1.pdf", status=429, times=1, retry_after=7200)
    fp = FakeFetchpdf({doi_of(1): [(b"", "https://europepmc.test/jats/1", "europepmc"),
                                   (b"", "https://journal.test/1.pdf", "unpaywall")]}, over_http=True)
    sleeps: list[float] = []
    rec = oa_go(root, fp, NOW, transport=fake, sleeps=sleeps)
    assert rec.scope_complete and "halted" not in rec.counts and rec.counts["oa.rate_limited"] == 1
    assert [s for s in sleeps if s in (2.0, 4.0, 8.0)] == []  # D588: 7200 s is over the cap, no retry
    roles = by_role(view(root)[d])
    ft = roles["full_text"]
    assert (ft["status"], ft["error_class"], ft["attempts"], ft["retry_after"]) == (
        "failed_transient", "rate_limit", 0, NOW + dt.timedelta(seconds=7200))
    assert roles["xml"]["status"] == "ok"
    checks_ok(root)

    oa_go(root, fp, NOW + dt.timedelta(hours=1), transport=fake)
    assert len(fp.calls) == 1  # not due before the hint
    oa_go(root, fp, NOW + dt.timedelta(hours=3), transport=fake)
    roles = by_role(view(root)[d])
    assert (roles["full_text"]["status"], roles["full_text"]["attempts"]) == ("ok", 0)
    checks_ok(root)


def test_oa_provider_429_halts_without_charging(tmp_path: Path, mailto: None) -> None:
    """Unpaywall still answering 429 after the retries and no artifact found: the ``oa`` lane
    halts for the run (no row charged: roles keep ``not_attempted``, ``attempts = 0``), the second
    paper is not asked; its row is written with ``not_attempted[halted]`` roles, not unreached."""
    root = store(tmp_path, [oa_row(1), oa_row(2)])
    fake = FakeHttp()
    for i in (1, 2):
        fake.add(f"https://api.unpaywall.org/v2/{doi_of(i)}", status=429)
    fp = FakeFetchpdf({}, over_http=True, lookups={doi_of(i): [f"https://api.unpaywall.org/v2/{doi_of(i)}"]
                                                     for i in (1, 2)})
    rec = oa_go(root, fp, NOW, transport=fake)
    assert not rec.scope_complete and rec.counts["halted"] == 1 and [c["raw"] for c in fp.calls] == [doi_of(1)]
    assert rec.counts["halted.oa"] == 1 and rec.counts["unreached"] == 0
    written = [row for row in view(root).values() if row["content"] is not None]
    assert [row["doc_id"] for row in written] == [sid(1), sid(2)]
    for row in written:
        for r in row["content"]["roles"]:
            assert (r["status"], r["attempts"]) == ("not_attempted", 0), r
        assert by_role(row)["full_text"]["error_class"] == "halted"



def test_oa_lane_sends_our_s2_key_to_the_s2_api_only(tmp_path: Path, mailto: None,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """D592 through a run: with ``SEMANTIC_SCHOLAR_API_KEY`` in the ``$STACK_ENV_FILE`` file, the lane's
    S2 lookup carries it as ``x-api-key`` while its OpenAlex and Unpaywall lookups carry no credential;
    the run record counts ``oa.http.s2_keyed``; the key is in no file the run wrote."""
    from tests.test_provider_keys import S2_KEY, _grep_tree

    keys = tmp_path / "keys.env"
    keys.write_text(f"SEMANTIC_SCHOLAR_API_KEY={S2_KEY}\nOPENALEX_API_KEY=fake-oa-not-sent\n")
    monkeypatch.setenv("STACK_ENV_FILE", str(keys))
    root = store(tmp_path, [oa_row(1)])
    s2 = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi_of(1)}"
    oax = f"https://api.openalex.org/works/doi:{doi_of(1)}"
    upw = f"https://api.unpaywall.org/v2/{doi_of(1)}"
    fake = FakeHttp()
    for url in (s2, oax, upw):
        fake.add(url, json={})
    fp = FakeFetchpdf({}, over_http=True, lookups={doi_of(1): [s2, oax, upw]})
    rec = oa_go(root, fp, NOW, transport=fake)
    sent = {c.url: {k.lower(): v for k, v in c.headers.items()} for c in fake.calls}
    assert set(sent) == {s2, oax, upw} and sent[s2]["x-api-key"] == S2_KEY
    for url in (oax, upw):
        assert not set(sent[url]) & {"authorization", "x-api-key", "apikey"}, url
    assert rec.counts["oa.http.s2_keyed"] == 1
    assert _grep_tree(tmp_path, S2_KEY, skip={keys}) == []


def test_oa_direct_route_through_the_runner(tmp_path: Path, mailto: None) -> None:
    """The default ``direct`` route in a run (T3.x-f4): the skeleton's ``prepare`` hook primes the
    resolver once with the run's DOIs before the first call (one batch, not one per paper); a Zenodo
    DOI is read from its record (``full_text`` ok, ``xml`` ended without a request), two DOIs
    Unpaywall knows as closed end without fetchpdf; four requests in all, the walk never runs."""
    from tests.fakes.oa_world import OaWorld
    from tests.test_fetch_oa import PmcFetchpdf

    world = OaWorld()
    rows = [oa_row(1), oa_row(2), oa_row(3)]
    rows[0]["identity"]["members"][0].update(key_value="10.5281/zenodo.11", canonical_url=None)
    world.add("10.5281/zenodo.11", "zenodo")
    world.add(doi_of(2), "miss")
    world.add(doi_of(3), "miss")
    root = store(tmp_path, rows)
    fp = PmcFetchpdf()
    rec = oa_go(root, fp, NOW, oa_plan(route="direct"), transport=world)  # type: ignore[arg-type]
    assert fp.primes == [[doi_of(2), doi_of(3)]] and fp.calls == [] and rec.counts["oa.primed"] == 1
    assert rec.counts["oa.http.requests"] == 4 and len(world.calls) == 4  # record, PDF, Unpaywall × 2
    one, two = (by_role(view(root)[sid(i)]) for i in (1, 2))
    assert (one["full_text"]["status"], one["xml"]["status"]) == ("ok", "failed_permanent")
    assert (two["full_text"]["error_class"], two["xml"]["error_class"]) == ("no_fetchable_url", "no_fetchable_url")
    assert "not in PubMed Central" in two["xml"]["error"] and "Unpaywall" in two["full_text"]["error"]
    checks_ok(root)


# ================================================================ Article in Press → version of record (round 4, P)
AIP_DOI = "10.1038/s41598-026-72982-1"
AIP_URL = "https://www.nature.com/articles/s41598-026-72982-1_reference.pdf"
VOR_URL = "https://www.nature.com/articles/s41598-026-72982-1.pdf"
AIP_TITLE = "A multilevel meta-analysis of visual reasoning in multimodal large language models"


class NatureWorld:
    """Unpaywall and nature.com for one Scientific Reports paper (``stack.http.HttpGetter``): Unpaywall
    keeps listing the Article-in-Press PDF (as it does until it refreshes), the version-of-record URL
    answers 404 until :attr:`vor` is set. Every request is logged."""

    def __init__(self, aip: bytes) -> None:
        self.aip, self.vor, self.calls = aip, None, []  # type: ignore[var-annotated]

    def get(self, url: str, *, headers: Any, timeout: float, max_bytes: int) -> Any:
        from stack.http import HttpResponse

        self.calls.append(url)
        bare = url.split("?")[0]
        if bare == f"https://api.unpaywall.org/v2/{AIP_DOI}":
            loc = {"url": AIP_URL, "url_for_pdf": AIP_URL, "host_type": "publisher", "license": "cc-by"}
            body = {"doi": AIP_DOI, "title": AIP_TITLE, "is_oa": True, "best_oa_location": loc, "oa_locations": [loc]}
            return HttpResponse(200, {"content-type": "application/json"}, json.dumps(body).encode(), url)
        pdf = {AIP_URL: self.aip, VOR_URL: self.vor}.get(bare)
        if pdf is None:
            return HttpResponse(404, {"content-type": "text/html"}, b"<html>Not found</html>", url)
        return HttpResponse(200, {"content-type": "application/pdf"}, pdf, url)

    __call__ = get


def article_pdf(banner: bool) -> bytes:
    """A 12-page born-digital article; with ``banner`` page 1 carries Nature's Article-in-Press banner."""
    fitz = pytest.importorskip("fitz", reason="pymupdf (fitz) not installed")
    doc = fitz.open()
    head = (["ARTICLE | Article in Press", "This Article in Press is shared early to give you faster access."]
            if banner else ["Scientific Reports | Article | Published"])
    for p in range(12):
        lines = [*head, AIP_TITLE, f"doi:{AIP_DOI}"] if p == 0 else []
        lines += [f"Page {p} line {i}: models are compared on standardised visual reasoning benchmarks."
                  for i in range(45)]
        doc.new_page().insert_text((50, 60), "\n".join(lines), fontsize=8)
    data = doc.tobytes()
    doc.close()
    return data


def test_oa_article_in_press_is_refetched_until_the_version_of_record(tmp_path: Path, mailto: None) -> None:
    """D206 follow-up (D227: Article in Press → version of record through the history/refresh mechanism):
    the AiP text is kept and flagged ``article_in_press``, its role due again 14 days on
    (``acquire.recheck.article_in_press``); a run minutes later asks nothing; at the due time the lane
    tries the version-of-record URL first and, while it is not out, gets the same AiP bytes: nothing
    minted, only the next due time written; once it is out, a new snapshot, the pointer moves to it,
    the flag is gone from the current text and the refresh stops."""
    row = oa_row(1)
    row["identity"]["members"][0].update(key_value=AIP_DOI, canonical_url=f"https://doi.org/{AIP_DOI}")
    root = store(tmp_path, [row])
    world = NatureWorld(article_pdf(banner=True))
    plan = Plan.model_validate({"plan": "test-lanes", "schema_version": 2,
                                "acquire": {"wants": {OA_CHANNEL: ["full_text"]}, "convert": {"config": "pymupdf4llm-v1"}},
                                "stages": [{"stage": "acquire", "options": {"allow_degraded_convert": True}}]})
    unpaywall = f"https://api.unpaywall.org/v2/{AIP_DOI}"

    def run(at: dt.datetime) -> tuple[Any, list[str]]:
        n = len(world.calls)
        rec = oa_go(root, FakeFetchpdf(), at, plan, transport=world)  # type: ignore[arg-type]
        return rec, [u.split("?")[0] for u in world.calls[n:]]

    def state() -> tuple[Row, Row, Row]:
        r = view(root)[sid(1)]
        cur = next(s for s in r["content"]["snapshots"] if s["snapshot_id"] == r["content"]["current_snapshot_id"])
        return r, by_role(r)["full_text"], cur

    rec, calls = run(NOW)
    assert calls == [unpaywall, VOR_URL, AIP_URL], calls  # the VoR URL first (404), then the listed AiP PDF
    r, role, cur = state()
    assert (cur["url"], r["content"]["current_text_status"], r["content"]["current_text_quality"]) == (
        AIP_URL, "ok", "flagged")
    assert "article_in_press" in cur["quality"]["flags"] and not cur["quality"]["fails"]
    assert (role["status"], role["retry_after"]) == ("ok", NOW + 14 * DAY)
    assert rec.counts["refresh.article_in_press"] == 1 and rec.counts["quality.flag.article_in_press"] == 1
    aip_snapshot = cur["snapshot_id"]

    rec, calls = run(NOW + dt.timedelta(minutes=2))  # the next pass: not due, nothing asked or written
    assert calls == [] and (rec.counts["lane_calls"], rec.counts["rows_written"]) == (0, 0)

    t1 = NOW + 14 * DAY + dt.timedelta(minutes=1)  # due; the VoR is not out: the same AiP bytes come back
    rec, calls = run(t1)
    assert calls == [unpaywall, VOR_URL, AIP_URL]
    assert (rec.counts["unchanged"], rec.counts["snapshots_minted"], rec.counts["rows_written"]) == (1, 0, 1)
    assert not {k for k in rec.counts if k.startswith("convert.")
                and k not in ("convert.entry_tool_absent", "convert.degraded_allowed")}
    r, role, cur = state()
    assert cur["snapshot_id"] == aip_snapshot and len(raws(r)) == 1
    assert (role["status"], role["retry_after"]) == ("ok", t1 + 14 * DAY)

    rec, calls = run(t1 + dt.timedelta(minutes=2))
    assert calls == [] and rec.counts["rows_written"] == 0

    world.vor = article_pdf(banner=False)  # the version of record is published
    t2 = t1 + 14 * DAY + dt.timedelta(minutes=1)
    rec, calls = run(t2)
    assert calls == [unpaywall, VOR_URL]  # found at the derived URL; the AiP PDF is not asked
    assert rec.counts["snapshots_minted"] == 1 and rec.counts.get("convert.ok") == 1, rec.counts
    r, role, cur = state()
    assert cur["snapshot_id"] != aip_snapshot and cur["url"] == VOR_URL and len(raws(r)) == 2
    assert r["content"]["current_text_status"] == "ok"
    assert "article_in_press" not in cur["quality"]["flags"]
    assert (role["status"], role["retry_after"]) == ("ok", None)  # no refresh of a version of record
    assert "refresh.article_in_press" not in rec.counts

    rec, calls = run(t2 + 30 * DAY)
    assert calls == [] and (rec.counts["lane_calls"], rec.counts["rows_written"]) == (0, 0)
    checks_ok(root)
