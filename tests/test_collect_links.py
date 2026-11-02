"""``collect.links`` (``stack/collect/links.py``) end to end through ``run_stage`` on a temporary
store: a synthetic ``documents`` head with a Substack-like in-band body (share, subscribe, comment,
CDN-image and self-link noise; tweets with lead-ins and blockquotes; arXiv, DOI, LessWrong, GitHub,
news, wiki, video, homepage and tag links) and a flagged manual page (nav/footer chrome, a relative
self-link, a shop link), and the selector through a fake model on the LLM client's transport
protocol (``stack.llm.Transport``; installed as ``tests.fakes.llm.INSTALLED``) that selects by
target and records every request. Scenarios: rows, relay keys, stored decisions and drops;
idempotency (a second run makes zero calls and writes nothing); recomputation (an edited body
re-asks only the changed item and retracts removed targets; an unflagged linking document drops
its relay); a budget stop (partial records, then completed without re-asking); refusals; and
gather ranking a link row's anchor title after a collected page's title.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

import tests.fakes.llm as fake_llm
from stack.build import incomplete_reasons, stage_inputs
from stack.collect import links
from stack.llm import LlmRequest, LlmResponse
from stack.plan import Plan
from stack.runs import RunRecord, write_final_record
from stack.collect.inband import json_record
from stack.store.blobs import open_blobs
from stack.store.chain import HeaderFields, Published, read_view, resolve
from tests.synth.records import run_line, sdir, write_series
from tests.synth.rows import (
    AT,
    _content,
    blob,
    dg,
    document_row,
    rid,
    sid,
    table,
    with_digests,
)
from tests.test_gather_identity import Store, src

Row = dict[str, Any]

ZVI_URL = "https://thezvi.substack.com/p/ai-189-new-math"
HUB_URL = "https://hub.example.net/reading"
ALICE = "1900000000000000001"
BOB = "1900000000000000002"
LW_POST = "AbCdEfGh123456789"


def zvi_body(*, lead: str = "Alice Researcher:", with_lw: bool = True, with_github: bool = True) -> bytes:
    lw = ('<p>See also <a href="https://www.lesswrong.com/posts/AbCdEfGh123456789/some-slug">this LessWrong post '
          'on scheming evaluations</a> and <a href="https://doi.org/10.1145/3442188.3445922">the stochastic parrots '
          'paper</a>.</p>') if with_lw else ""
    gh = ', and the code is <a href="https://github.com/alice/steer">on GitHub</a>' if with_github else ""
    return f"""
<p>Last week <a href="https://thezvi.substack.com/p/ai-188?r=67wny">I wrote</a> about this.</p>
<h4>Table of Contents</h4>
<ol><li><a href="https://thezvi.substack.com/i/218365775/language-models">Language Models.</a></li></ol>
<h4>Interpretability Is Back</h4>
<p>{lead} <a href="https://x.com/alice/status/{ALICE}?s=20">We found steering vectors that remove sycophancy</a></p>
<blockquote><p>Our new paper shows activation steering cuts sycophancy by half in frontier models.</p></blockquote>
<p>The paper is <a href="https://arxiv.org/abs/2610.01234v2">Steering Away From Sycophancy In Frontier Models</a>{gh}.</p>
{lw}
<h4>Other News</h4>
<p>The <a href="https://www.nytimes.com/2026/10/01/technology/ai-chips.html">NYT reports on chip exports</a>, see
<a href="https://en.wikipedia.org/wiki/Export_control">Wikipedia</a> and <a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">this
video</a>.</p>
<p>Bob: <a href="https://twitter.com/bob/status/{BOB}">lol</a></p>
<p>Visit <a href="https://example.org/">Example Org</a> or their <a href="https://example.org/tag/ai">AI tag page</a>.</p>
<div class="captioned-image-container"><figure><a class="image-link image2" href="https://substackcdn.com/image/fetch/x.png"><img src="https://substackcdn.com/x.png" alt=""></a><button>expand</button></figure></div>
<p><a href="https://twitter.com/intent/tweet?url=https://thezvi.substack.com/p/x">Share</a></p>
<p class="button-wrapper"><a class="button primary" href="https://thezvi.substack.com/subscribe?">Subscribe now</a></p>
<p><a href="https://thezvi.substack.com/p/ai-189-new-math/comments">Leave a comment</a></p>
<p><a href="#footnote-1">1</a> <a href="mailto:zvi@example.org">mail</a> Café ’quotes’ stay UTF-8.</p>
""".encode()


PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>AI Safety Hub</title></head><body>
<header><nav><a href="https://hub.example.net/about">About</a> <a href="https://hub.example.net/">Home</a></nav></header>
<main><article><h1>Reading list</h1>
<p>Start with <a href="https://arxiv.org/pdf/2610.01234">the steering paper</a> and <a href="/guides/corrigibility">our
corrigibility guide</a>.</p>
<p>Gifts: <a href="https://www.amazon.com/dp/0393635821">buy the book</a>.</p>
<aside><a href="https://promo.example.com/offer/123">A sponsored offer inside the article</a></aside>
</article></main>
<footer><a href="https://twitter.com/hub">Twitter</a></footer></body></html>"""

#: What the fake model selects (substrings of the item's ``target``); everything else is rejected.
SELECT = ("arxiv.org", "lesswrong.com", "github.com", f"x.com/alice/status/{ALICE}", "doi.org")


class FakeSelector:
    """A model on the client's transport protocol: answers every link line of the user message,
    ``select`` when its target matches :data:`SELECT`; records each request."""

    def __init__(self) -> None:
        self.requests: list[LlmRequest] = []

    def post(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        lines = [json.loads(ln) for ln in fake_llm.prompt_text(request).splitlines() if ln.startswith('{"i":')]
        items = [{"i": it["i"], "decision": "select" if any(s in it["target"] for s in SELECT) else "reject",
                  "kind": "other", "reason": f"fake: {it['target']}"} for it in lines]
        return fake_llm.ok({"items": items}, cost=0.0005)

    def items(self) -> list[Row]:
        return [json.loads(ln) for r in self.requests for ln in fake_llm.prompt_text(r).splitlines()
                if ln.startswith('{"i":')]


def zvi_doc(root: Path, body: bytes, *, title: str = "AI #189: New Math", channel: str = "zvi_substack") -> Row:
    row = document_row(1)
    raw = open_blobs(root).put(body)
    m = row["identity"]["members"][0]
    m.update(channel=channel, key_kind="url", key_value="https://thezvi.substack.com/p/ai-189-new-math",
             canonical_url=ZVI_URL, title=title, doc_type="lab_blog", license_class="public_web")
    row["identity"].update(channels=[channel],
                           keys=[{"kind": "url", "value": "https://thezvi.substack.com/p/ai-189-new-math",
                                  "source_id": sid(1)}])
    row["metadata"].update(title=title, abstract=None, doc_type="lab_blog",
                           published_at=AT - dt.timedelta(days=1))
    row["inband"]["entries"] = [{"source_id": sid(1), "content_role": "post", "raw": blob(raw, len(body), "text/html"),
                                 "text": None, "captured_at": AT}]
    return with_digests(row)


def hub_doc(root: Path, *, flagged: bool = True, page: bytes = PAGE) -> Row:
    row = document_row(2, state="acquired")
    raw = open_blobs(root).put(page)
    m = row["identity"]["members"][0]
    m.update(channel="manual_url", key_kind="url", key_value="https://hub.example.net/reading",
             canonical_url=HUB_URL, title=None, doc_type=None, license_class="public_web")
    keys = [{"kind": "url", "value": "https://hub.example.net/reading", "source_id": sid(2)}]
    if flagged:
        keys.insert(0, {"kind": "flag", "value": "collect_links", "source_id": sid(2)})
    row["identity"].update(channels=["manual_url"], keys=keys)
    row["metadata"].update(title="AI Safety Hub", abstract=None, doc_type=None, published_at=None)
    row["inband"]["entries"] = []
    content = _content(2, 1)
    snap = content["snapshots"][0]
    snap.update(content_role="page", raw=blob(raw, len(page), "text/html"), url=HUB_URL, final_url=HUB_URL)
    content["roles"][0].update(content_role="page", lane="http", tried_urls=[HUB_URL])
    content["current_role"] = "page"
    row["content"] = content
    return with_digests(row)


def paper_doc() -> Row:
    return with_digests(document_row(3))  # an arxiv paper: no rule makes it a linking document


def publish(root: Path, rows: list[Row], n: int) -> None:
    """A gather-like full ``documents`` file over the current head (run ``rid(90 + n)``)."""
    run_id = rid(90 + n)
    at = AT + dt.timedelta(minutes=n)
    fields = HeaderFields(stage="gather", run_id=run_id, config_digest=dg("cfg"), code_digest=dg("code"),
                          plan="test", blocks_written=["identity", "metadata", "inband", "content"])

    def hook(pubs: list[Published]) -> None:
        write_final_record(root, RunRecord(
            run_id=run_id, stage="gather", writer="gather", plan="test", status="ok", started_at=at,
            heartbeat_at=at, finished_at=at, code_digest=dg("code"), config_digest=dg("cfg"), scope_complete=True,
            version={"rows_touched": len(rows)},
            outputs={"documents": pubs[0].as_output()}))

    write_series(root, "documents", "documents", table("documents", rows), fields, kind="full", at=at,
                 finished_at=at, on_published=hook)


def plan(budget: float = 1.0, prompts: Row | None = None, miss_cap: int | None = 1, **options: Any) -> Plan:
    line: Row = {"stage": "collect.links", "prior": ["sources", "coverage"], "out": {"kind": "part"}, "budget": {"max_usd": budget},
                 "options": options}
    if miss_cap is not None:
        line["miss_cap"] = miss_cap
    return Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main",
                                "prompts": {"collect.links": {"name": "link-select-v1"}} if prompts is None else prompts,
                                "stages": [line]})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSelector:
    f = FakeSelector()
    monkeypatch.setattr(fake_llm, "INSTALLED", f)
    return f


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    publish(root, [zvi_doc(root, zvi_body()), hub_doc(root), paper_doc()], 1)
    return root


def run(root: Path, p: Plan | None = None) -> RunRecord:
    rec = run_line(root, p or plan(), "collect.links")
    assert rec is not None and rec.status == "ok", rec.error
    return rec


def sources(root: Path) -> dict[str, Row]:
    """Latest row per ``external_id`` of ``sources.links``."""
    return {r["identity"]["external_id"]: r for r in read_view(resolve(root, sdir("sources.links"))).to_pylist()}


def record_of(root: Path, row: Row) -> Row:
    rec = json_record(open_blobs(root), row)
    assert isinstance(rec, dict)
    return rec


def keys_of(row: Row, kind: str | None = None) -> list[str]:
    return sorted(f"{k['kind']}:{k['value']}" for k in row["identity"]["keys"] if kind in (None, k["kind"]))


ARXIV = "doi:10.48550/arxiv.2610.01234"
SELECTED_KEYS = {f"x_post:{ALICE}", ARXIV, "url:https://github.com/alice/steer", f"fm_post:{LW_POST}",
                 "doi:10.1145/3442188.3445922"}


# ─────────────────────────────────────────────────────────────── scenarios
def test_first_run_rows_relays_decisions_and_drops(store: Path, fake: FakeSelector) -> None:
    rec = run(store, plan(batch_size=4))
    c = rec.counts
    # two linking documents (the arxiv paper is none); the Zvi body's 7 asked links in 2 batches, the hub's 1
    assert c["linking_eligible"] == 2 and c["linking_processed"] == 2 and c["linking_recorded"] == 2
    assert len(fake.requests) == 3 and c["selector_calls"] == 3 and c["selector_items_asked"] == 8
    assert {r.model for r in fake.requests} == {"openai/gpt-6-luna"}
    asked = {it["target"] for it in fake.items()}
    assert asked == {f"x.com/alice/status/{ALICE}", f"twitter.com/bob/status/{BOB}", "arxiv.org/abs/2610.01234",
                     "github.com/alice/steer", f"lesswrong.com/posts/{LW_POST}", "doi.org/10.1145/3442188.3445922",
                     "nytimes.com/2026/10/01/technology/ai-chips.html"}
    alice = next(it for it in fake.items() if it["target"].startswith("x.com/alice"))
    # the lead-in paragraph is joined by the blockquote that carries the tweet's substance
    assert "activation steering cuts sycophancy" in alice["context"] and alice["section"] == "Interpretability Is Back"
    assert c["rule_wiki"] == 1 and c["rule_video"] == 1 and c["rule_homepage"] == 1
    assert c["rule_navigation"] == 1 and c["rule_shop"] == 1
    assert c["drop_self"] == 3 and c["drop_share"] == 1 and c["drop_button"] == 1 and c["drop_comment"] == 1
    assert c["drop_asset"] == 1 and c["drop_chrome"] == 1 and c["drop_fragment"] == 1 and c["drop_nonhttp"] == 1
    assert rec.scope_complete is True

    rows = sources(store)
    assert set(rows) == SELECTED_KEYS
    chans = {k: r["identity"]["channel"] for k, r in rows.items()}
    assert chans == {f"x_post:{ALICE}": "links_social", ARXIV: "links_paper",
                     "url:https://github.com/alice/steer": "links_url", f"fm_post:{LW_POST}": "links_forum",
                     "doi:10.1145/3442188.3445922": "links_paper"}
    paper = rows[ARXIV]
    assert keys_of(paper) == ["arxiv:2610.01234", ARXIV, "relay:https://hub.example.net/reading",
                              "relay:https://thezvi.substack.com/p/ai-189-new-math"]
    assert paper["metadata"]["title"] == "Steering Away From Sycophancy In Frontier Models"
    assert paper["identity"]["canonical_url"] == "https://arxiv.org/pdf/2610.01234"  # the hub (undated) is first
    tweet = rows[f"x_post:{ALICE}"]
    assert keys_of(tweet, "relay") == ["relay:https://thezvi.substack.com/p/ai-189-new-math"]
    assert tweet["metadata"]["doc_type"] == "tweet" and tweet["metadata"]["origin"] == "provider"
    record = record_of(store, paper)
    assert [lf["source_id"] for lf in record["linked_from"]] == [sid(2), sid(1)]
    zvi_lf = record["linked_from"][1]
    assert zvi_lf["decision"] == "selected" and zvi_lf["by"] == "llm" and zvi_lf["prompt"] == "link-select-v1"
    assert zvi_lf["reason"] == "fake: arxiv.org/abs/2610.01234"
    assert zvi_lf["occurrences"][0]["href"] == "https://arxiv.org/abs/2610.01234v2"

    # every decision and drop is stored: one coverage row per linking document, its record in blobs/raw
    flat = links.decision_rows(store, sdir("coverage.links"))
    by = {(r["linker_source_id"], r["key"] or r["url"]): r for r in flat}
    assert by[(sid(1), "url:https://nytimes.com/2026/10/01/technology/ai-chips.html")]["decision"] == "rejected"
    assert by[(sid(1), "url:https://en.wikipedia.org/wiki/Export_control")]["reason"] == "rule:wiki"
    assert by[(sid(1), "url:https://example.org")]["reason"] == "rule:homepage"
    assert by[(sid(2), "url:https://amazon.com/dp/0393635821")]["reason"] == "rule:shop"
    assert by[(sid(1), "https://substackcdn.com/image/fetch/x.png")]["reason"] == "asset"
    assert by[(sid(2), "https://hub.example.net/guides/corrigibility")]["reason"] == "self"
    assert {r["decision"] for r in flat} == {"selected", "rejected", "dropped"}
    cov = read_view(resolve(store, sdir("coverage.links"))).to_pylist()
    assert sorted((r["channel"], r["status"], r["seen"], r["kept"]) for r in cov) == [
        ("manual_url", "ok", 2, 1), ("zvi_substack", "ok", 11, 5)]

    # a second run over the same head: no call, no row, no file
    again = run(store, plan(batch_size=4))
    assert len(fake.requests) == 3
    assert again.counts["selector_cached"] == 8 and again.counts.get("selector_calls", 0) == 0
    assert again.counts["linking_unchanged"] == 2 and again.counts["sources_unchanged"] == 5
    assert again.counts["sources_written"] == 0 and again.counts["coverage_written"] == 0
    assert again.outputs == {}


@pytest.mark.slow
def test_recomputation_reasks_only_changed_items_and_retracts(store: Path, fake: FakeSelector) -> None:
    run(store)
    first = sources(store)
    n = len(fake.requests)
    # an edited body: Alice's lead-in reworded, the LessWrong/DOI paragraph gone
    publish(store, [zvi_doc(store, zvi_body(lead="Alice Researcher, of the steering team:", with_lw=False)),
                    hub_doc(store), paper_doc()], 2)
    rec = run(store)
    assert len(fake.requests) == n + 1 and rec.counts["selector_items_asked"] == 1
    assert [it["target"] for it in fake.items()[-1:]] == [f"x.com/alice/status/{ALICE}"]
    rows = sources(store)
    assert rows[f"fm_post:{LW_POST}"]["state"] == "retracted"
    assert rows["doi:10.1145/3442188.3445922"]["state"] == "retracted"
    assert keys_of(rows["doi:10.1145/3442188.3445922"]) == ["doi:10.1145/3442188.3445922"]  # no relay left
    assert rows[f"x_post:{ALICE}"]["run_id"] == rec.run_id  # its occurrence changed: a new row
    assert rows[ARXIV]["run_id"] == first[ARXIV]["run_id"]  # unchanged: no row
    assert rec.counts["rows_retracted"] == 2 and rec.counts["rows_observed"] == 1

    # the hub loses its collect_links flag: no longer a linking document; the paper keeps one relay
    publish(store, [zvi_doc(store, zvi_body(lead="Alice Researcher, of the steering team:", with_lw=False)),
                    hub_doc(store, flagged=False), paper_doc()], 3)
    rec = run(store)
    assert len(fake.requests) == n + 1 and rec.counts["linking_dropped"] == 1
    rows = sources(store)
    assert keys_of(rows[ARXIV], "relay") == ["relay:https://thezvi.substack.com/p/ai-189-new-math"]
    assert rows[ARXIV]["state"] == "observed" and rows[ARXIV]["run_id"] == rec.run_id
    # its first occurrence (the hub's) is gone, but canonical_url stays the first-minted one (round 4 L4)
    assert rows[ARXIV]["identity"]["canonical_url"] == "https://arxiv.org/pdf/2610.01234"

    # the GitHub link removed too: retracted; then an identical run is a no-op
    publish(store, [zvi_doc(store, zvi_body(lead="Alice Researcher, of the steering team:", with_lw=False,
                                            with_github=False)), hub_doc(store, flagged=False), paper_doc()], 4)
    rec = run(store)
    rows = sources(store)
    assert rows["url:https://github.com/alice/steer"]["state"] == "retracted"
    # the paper's context sentence lost its GitHub clause: its item is asked again, its row changes
    assert len(fake.requests) == n + 2 and [it["target"] for it in fake.items()[-1:]] == ["arxiv.org/abs/2610.01234"]
    assert rows[ARXIV]["run_id"] == rec.run_id
    rec = run(store)
    assert rec.outputs == {} and len(fake.requests) == n + 2


#: The hub page with a title-like anchor on the paper link (it is undated, so it sorts first).
PAGE_TITLED = PAGE.replace(b">the steering paper<", b">A Reading Guide To Steering Papers<")


@pytest.mark.slow
def test_title_and_url_are_first_minted(tmp_path: Path, fake: FakeSelector) -> None:
    """Round 4 L4 (the L3 run's churn, the X17 trigger): a second linking document adds a relay key and
    occurrences, but the row keeps the first-minted title and canonical_url, also once the first
    occurrence is gone; only a retracted target is minted afresh. The selector cache is untouched."""
    root = tmp_path / "store"
    zvi = zvi_doc(root, zvi_body())
    publish(root, [zvi, hub_doc(root, flagged=False), paper_doc()], 1)
    run(root)
    first = sources(root)[ARXIV]
    minted = ("Steering Away From Sycophancy In Frontier Models", "https://arxiv.org/abs/2610.01234v2")
    assert (first["metadata"]["title"], first["identity"]["canonical_url"]) == minted
    n = len(fake.requests)

    # the hub starts linking the paper, earlier in the sort and with a title-like anchor of its own
    publish(root, [zvi, hub_doc(root, page=PAGE_TITLED), paper_doc()], 2)
    rec = run(root)
    assert len(fake.requests) == n + 1 and rec.counts["selector_items_asked"] == 1  # the hub's item only
    assert rec.counts["selector_cached"] == 7
    row = sources(root)[ARXIV]
    assert row["run_id"] == rec.run_id and len(record_of(root, row)["linked_from"]) == 2
    assert keys_of(row, "relay") == ["relay:https://hub.example.net/reading",
                                     "relay:https://thezvi.substack.com/p/ai-189-new-math"]
    assert (row["metadata"]["title"], row["identity"]["canonical_url"]) == minted

    # Zvi's post is gone: the hub's occurrence is the only one, the first-minted values stay
    publish(root, [hub_doc(root, page=PAGE_TITLED), paper_doc()], 3)
    rec = run(root)
    row = sources(root)[ARXIV]
    assert row["run_id"] == rec.run_id and keys_of(row, "relay") == ["relay:https://hub.example.net/reading"]
    assert (row["metadata"]["title"], row["identity"]["canonical_url"]) == minted

    # retracted, then selected again: minted afresh from the hub's occurrence
    publish(root, [hub_doc(root, flagged=False, page=PAGE_TITLED), paper_doc()], 4)
    run(root)
    assert sources(root)[ARXIV]["state"] == "retracted"
    publish(root, [hub_doc(root, page=PAGE_TITLED), paper_doc()], 5)
    run(root)
    row = sources(root)[ARXIV]
    assert row["state"] == "observed"
    assert (row["metadata"]["title"], row["identity"]["canonical_url"]) == (
        "A Reading Guide To Steering Papers", "https://arxiv.org/pdf/2610.01234")
    assert len(fake.requests) == n + 1  # every later decision came from the cache


@pytest.mark.slow  # a full collect.links run per case
@pytest.mark.parametrize("title, channel, linking", [
    ("Monthly Roundup #40: October 2026", "zvi_substack", False),
    ("Childhood and Education #14: Screens", "zvi_substack", False),
    ("Housing Roundup #12", "zvi_substack", False),
    ("AI #189: New Math", "zvi_substack", True),
    ("Monthly Roundup #40: October 2026", "import_ai", True),  # the exclusion is Zvi's only
])
def test_zvi_non_ai_series_are_not_linking_documents(tmp_path: Path, fake: FakeSelector, title: str,
                                                      channel: str, linking: bool) -> None:
    """Round 4 L4: ``newsletter_links`` excludes Zvi's non-AI series by title (``ZVI_NON_AI_SERIES``)."""
    root = tmp_path / "store"
    publish(root, [zvi_doc(root, zvi_body(), title=title, channel=channel), hub_doc(root), paper_doc()], 1)
    rec = run(root)
    assert rec.counts["linking_eligible"] == (2 if linking else 1)
    assert ({r["linker_source_id"] for r in links.decision_rows(root, sdir("coverage.links"))} == {sid(1), sid(2)}) is linking


def test_budget_stop_leaves_partial_records_then_completes(store: Path, fake: FakeSelector) -> None:
    rec = run(store, plan(budget=0.000001))
    assert len(fake.requests) == 0 and rec.scope_complete is False
    assert rec.counts["selector_budget_stop"] >= 1 and rec.counts["linking_partial"] == 2
    assert "sources" not in rec.outputs  # no complete record: no target row yet
    cov = read_view(resolve(store, sdir("coverage.links"))).to_pylist()
    assert {r["status"] for r in cov} == {"partial"}
    assert "limit" not in incomplete_reasons(rec.counts)  # D492: undecided candidates are not a cap
    flat = links.decision_rows(store, sdir("coverage.links"))
    assert any(r["decision"] == "undecided" for r in flat) and any(r["by"] == "rule" for r in flat)
    rec = run(store)
    assert rec.scope_complete is True and len(fake.requests) == 2
    assert set(sources(store)) == SELECTED_KEYS


def test_limits_and_refusals(store: Path, fake: FakeSelector) -> None:
    rec = run(store, plan(only=[ZVI_URL]))
    assert rec.counts["linking_processed"] == 1 and rec.counts["linking_not_named"] == 1
    assert {r["linker_source_id"] for r in links.decision_rows(store, sdir("coverage.links"))} == {sid(1)}
    # the hub was never processed: its targets are not touched (the paper has only Zvi's relay)
    assert keys_of(sources(store)[ARXIV], "relay") == ["relay:https://thezvi.substack.com/p/ai-189-new-math"]
    # a build pins the documents head for collect.links as for the *-lookup lines
    p = plan()
    (call,) = stage_inputs(store, p, p.stages[0], {}, lambda _w: None)
    assert sdir("documents") in call.ins
    for bad, msg in ((plan(miss_cap=None), "miss_cap: 1"), (plan(prompts={}), "prompt pin"),
                     (plan(exclude_kinds=["nonsense"]), "exclude_kinds"), (plan(rules=["zvi_posts"]), "triage rule")):
        rec = run_line(store, bad, "collect.links")
        assert rec is not None and rec.status == "failed" and msg in (rec.error or ""), (msg, rec.error)


def test_gather_ranks_a_link_anchor_after_a_collected_title(tmp_path: Path) -> None:
    """An anchor text never overwrites a real title: a page collected earlier and a link row seen later
    share the url key; the page's title wins (before PROVIDER_LAST the newer link row won the rank tie)."""
    st = Store(tmp_path)
    page = src(1, [("url", "https://example.org/post")], channel="anthropic_alignment_blog")
    page["metadata"]["title"] = "The Real Title"
    link = src(2, [("url", "https://example.org/post"), ("relay", "https://thezvi.substack.com/p/x")],
               channel="links_url", t=90)
    link["metadata"]["title"] = "an anchor text that reads like a title"
    st.add("sources.blogs", [page])
    st.add("sources.links", [link])
    st.gather()
    (doc,) = st.docs().values()
    assert doc["metadata"]["title"] == "The Real Title"
    assert {"relay"} <= {k["kind"] for k in doc["identity"]["keys"]}


# ─────────────────────────────────────────────────────────────── same-host PDFs of flagged documents (D665)
MIRI_URL = "https://intelligence.org/2013/09/12/new-paper-tiling-agents/"
#: A MIRI-like announcement post (no article/main: the region is ``body``): the paper on the post's
#: own host, absolute, relative and ``www.``; a PDF in the header nav; same-host category, author,
#: dated-post, site-page and non-PDF file links; an off-host arXiv paper.
MIRI_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>New paper</title></head><body>
<header><nav><a href="https://intelligence.org/files/Brochure.pdf">Brochure</a>
<a href="https://intelligence.org/category/news/">News</a></nav></header>
<h1>New paper: Tiling agents</h1>
<p>Today we release <a href="https://intelligence.org/files/TilingAgents.pdf">Tiling Agents for Self-Modifying AI</a>,
with <a href="/files/ProgramEquilibrium.pdf">a companion report on program equilibrium</a> and
<a href="https://www.intelligence.org/files/Botworld.pdf">Botworld</a>.</p>
<p>See also <a href="https://arxiv.org/abs/1312.3626">the arXiv preprint</a>, our
<a href="https://intelligence.org/2013/07/24/miris-december-2013-workshop/">December workshop</a>, the
<a href="https://intelligence.org/technical-agenda/">technical agenda</a>, the
<a href="https://intelligence.org/files/TilingAgents.pptx">slides</a>, posts by
<a href="https://intelligence.org/author/lukeprog/">Luke</a> and in <a href="https://intelligence.org/category/analysis/">Analysis</a>.</p>
</body></html>"""
MIRI_PDFS = {"url:https://intelligence.org/files/TilingAgents.pdf",
             "url:https://intelligence.org/files/ProgramEquilibrium.pdf",
             "url:https://intelligence.org/files/Botworld.pdf"}


@pytest.mark.parametrize("flagged", [True, False])
def test_flagged_document_keeps_same_host_pdfs_only(flagged: bool) -> None:
    ex = links.extract(MIRI_PAGE, base_url=MIRI_URL, fragment=False, self_keys=[f"url:{MIRI_URL}"],
                       flagged=flagged)
    assert ex.counts["region_body"] == 1 and ex.counts["drop_chrome"] == 2  # the nav's PDF stays chrome
    assert set(ex.candidates) == (MIRI_PDFS if flagged else set()) | {"doi:10.48550/arxiv.1312.3626"}
    assert all(ex.candidates[k].target.kind == "pdf" for k in MIRI_PDFS if flagged)
    self_dropped = sorted(r["url"] for r in ex.dropped if r["reason"] == "self")
    still_self = ["https://intelligence.org/2013/07/24/miris-december-2013-workshop/",
                  "https://intelligence.org/author/lukeprog/", "https://intelligence.org/category/analysis/",
                  "https://intelligence.org/files/TilingAgents.pptx", "https://intelligence.org/technical-agenda/"]
    pdfs = ["https://intelligence.org/files/Botworld.pdf", "https://intelligence.org/files/ProgramEquilibrium.pdf",
            "https://intelligence.org/files/TilingAgents.pdf"]
    assert sorted(u.replace("www.", "") for u in self_dropped) == sorted(still_self + ([] if flagged else pdfs))


def test_flagged_document_never_keeps_its_own_url() -> None:
    """The linking document's own keys stay ``self`` even when they name a same-host PDF."""
    own = "https://intelligence.org/files/TilingAgents.pdf"
    ex = links.extract(MIRI_PAGE, base_url=MIRI_URL, fragment=False, self_keys=[f"url:{own}"], flagged=True)
    assert f"url:{own}" not in ex.candidates and len(ex.candidates) == 3
    assert {r["url"] for r in ex.dropped if r["reason"] == "self"} >= {own}


def test_newsletter_extraction_is_unchanged_and_keeps_dropping_same_host_pdfs() -> None:
    """A newsletter is never flagged: its own hosts' PDFs stay ``self`` (body host and LINK_SELF_HOSTS)."""
    ex = links.extract(zvi_body(), base_url=ZVI_URL, fragment=True,
                       self_hosts=("thezvi.substack.com", "thezvi.wordpress.com"), self_keys=[f"url:{ZVI_URL}"])
    # the counts the extractor gave this fixture before D665 (checked against the previous code)
    assert dict(ex.counts) == {"region_fragment": 1, "anchors": 19, "drop_self": 2, "drop_asset": 1, "drop_share": 1,
                               "drop_button": 1, "drop_comment": 1, "drop_fragment": 1, "drop_nonhttp": 1,
                               "candidates": 11}
    pdf = (b'<p><a href="https://thezvi.substack.com/files/notes.pdf">my notes</a> and '
           b'<a href="https://thezvi.wordpress.com/2020/report.pdf">an old report</a></p>')
    ex2 = links.extract(zvi_body() + pdf, base_url=ZVI_URL, fragment=True,
                        self_hosts=("thezvi.substack.com", "thezvi.wordpress.com"), self_keys=[f"url:{ZVI_URL}"])
    assert set(ex2.candidates) == set(ex.candidates) and ex2.counts["drop_self"] == 4


def test_flagged_page_selects_a_same_host_pdf_end_to_end(tmp_path: Path, fake: FakeSelector,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the stage: the flagged hub's same-host PDF is asked and becomes a ``links_url`` row
    keyed by its URL; the newsletter's same-host PDF (unflagged) is dropped as ``self``."""
    monkeypatch.setattr(__import__(__name__, fromlist=["SELECT"]), "SELECT", (*SELECT, "/files/"))
    page = PAGE.replace(b"</article>", b'<p>Our <a href="/files/report.pdf">annual safety report</a>.</p></article>')
    zvi = zvi_body() + b'<p><a href="https://thezvi.substack.com/files/notes.pdf">my notes</a></p>'
    root = tmp_path / "store"
    publish(root, [zvi_doc(root, zvi), hub_doc(root, page=page), paper_doc()], 1)
    rec = run(root)
    asked = {it["target"] for it in fake.items()}
    assert "hub.example.net/files/report.pdf" in asked and not any("notes.pdf" in t for t in asked)
    row = sources(root)["url:https://hub.example.net/files/report.pdf"]
    assert row["identity"]["channel"] == "links_url"
    assert keys_of(row) == ["relay:https://hub.example.net/reading", "url:https://hub.example.net/files/report.pdf"]
    flat = links.decision_rows(root, sdir("coverage.links"))
    by = {(r["linker_source_id"], r["key"] or r["url"]): r for r in flat}
    assert by[(sid(1), "https://thezvi.substack.com/files/notes.pdf")]["reason"] == "self"
    assert by[(sid(2), "https://hub.example.net/guides/corrigibility")]["reason"] == "self"  # non-PDF same-host
    assert rec.counts["drop_self"] == 4


def test_page_bodies_are_decoded_by_their_charset() -> None:
    """A linking page in GBK: anchors and context reach the selector as text (the converter's decoding,
    GB2312/GBK read as GB18030)."""
    page = ('<html><head><meta charset="gb2312"><title>TC260</title></head><body><main>'
            '<a href="https://example.org/framework.pdf">人工智能安全治理框架 朱镕基</a></main></body></html>')
    root = links.parse_body(page.encode("gb18030"), fragment=False)
    assert "人工智能安全治理框架 朱镕基" in root.text_content()
