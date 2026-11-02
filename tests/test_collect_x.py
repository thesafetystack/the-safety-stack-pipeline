"""``collect.x-lookup`` (``stack/collect/xapi.py``): pure helpers (rendering, self-thread, keys,
relayed URLs, the X ledger's cap) and the stage end to end through ``run_stage`` on a temporary
store with ``tests/fakes/http.py``. The canned answers have the shapes the live API returned on
2026-10-12 (RUNLOG (X)): ``data`` + ``includes.tweets`` + per-id ``errors``, ``note_tweet`` for long
posts, ``entities.urls``/``mentions``, search ``meta.next_token``; ids, handles and texts are
synthetic. No test reaches api.x.com: ``SEND`` is the fake and the token is a dummy.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from stack.build import incomplete_reasons
from stack.checks.verify import run_checks
from stack.collect import xapi
from stack.collect.base import CollectError
from stack.llm.ledger import forget_run
from stack.plan import Plan
from stack.runs import RunRecord
from stack.stage_runner import run_stage
from stack.collect.inband import json_record
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from tests.fakes.http import FakeHttp
from tests.synth.records import run_line, sdir
from tests.test_collect_lookup import doc, publish_documents
from tests.test_gather_identity import Store, src

Row = dict[str, Any]
TOKEN = "test-bearer-not-a-real-token"
NOW = dt.datetime.now(dt.UTC)


def snow(when: dt.datetime, seq: int = 0) -> str:
    """A snowflake post id created at ``when``."""
    return str(((int(when.timestamp() * 1000) - 1288834974657) << 22) + seq)


# The world: alice's recent self-thread A1-A3 (+ A9, her reply under someone else's post in the same
# conversation, not part of the chain); bob's old reply B to carol quoting dave's Q; erin's old
# self-thread C1-C3 (searched in the full archive, two pages); D deleted.
A1, A2, A3, A9 = (snow(NOW - dt.timedelta(days=1), s) for s in (1, 2, 3, 9))
Z = snow(NOW - dt.timedelta(days=1), 5)
B, P, Q = (snow(NOW - dt.timedelta(days=40), s) for s in (1, 2, 3))
C1, C2, C3 = (snow(NOW - dt.timedelta(days=30), s) for s in (1, 2, 3))
D = snow(NOW - dt.timedelta(days=3), 1)
# alice's thread grows later by A4; frank answers carol's P (F1) and then himself (F2): F2 replies to
# its own author inside someone else's conversation
A4 = snow(NOW - dt.timedelta(days=1), 4)
F1, F2 = (snow(NOW - dt.timedelta(days=39), s) for s in (1, 2))


def post(pid: str, author: str, text: str, *, conv: str | None = None, reply_to: tuple[str, str] | None = None,
         quote: str | None = None, retweet: str | None = None, urls: list[tuple[str, str]] = (),
         mentions: list[tuple[str, str]] = (), note: str | None = None) -> Row:
    refs = ([{"id": reply_to[0], "type": "replied_to"}] if reply_to else []) + (
        [{"id": quote, "type": "quoted"}] if quote else []) + ([{"id": retweet, "type": "retweeted"}] if retweet else [])
    p: Row = {"id": pid, "author_id": author, "conversation_id": conv or pid, "created_at":
              xapi.post_time(pid).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "edit_history_tweet_ids": [pid],
              "lang": "en", "text": text, "public_metrics": {"like_count": 5, "retweet_count": 1}}
    if refs:
        p["referenced_tweets"] = refs
    if reply_to:
        p["in_reply_to_user_id"] = reply_to[1]
    ents: Row = {}
    if urls:
        ents["urls"] = [{"url": short, "expanded_url": exp, "display_url": exp[8:30]} for short, exp in urls]
    if mentions:
        ents["mentions"] = [{"id": uid, "username": name} for uid, name in mentions]
    if ents:
        p["entities"] = ents
    if note:
        p["note_tweet"] = {"text": note}
    return p


POSTS = {
    A1: post(A1, "100", "Our new interpretability paper https://t.co/a1 https://t.co/m1", urls=[
        ("https://t.co/a1", "https://arxiv.org/abs/2610.00001"),
        ("https://t.co/m1", f"https://x.com/alice/status/{A1}/photo/1")]),
    A2: post(A2, "100", "Second, the method.", conv=A1, reply_to=(A1, "100")),
    A3: post(A3, "100", "Third, code at https://t.co/a3", conv=A1, reply_to=(A2, "100"),
             urls=[("https://t.co/a3", "https://github.com/alice/interp")]),
    A9: post(A9, "100", "@zed thanks!", conv=A1, reply_to=(Z, "900"), mentions=[("900", "zed")]),
    B: post(B, "200", "@carol Agree, see https://t.co/b1 https://t.co/q1", conv=P, reply_to=(P, "300"), quote=Q,
            urls=[("https://t.co/b1", "https://arxiv.org/abs/2610.00002"),
                  ("https://t.co/q1", f"https://x.com/dave/status/{Q}")], mentions=[("300", "carol")]),
    P: post(P, "300", "Is scalable oversight solved?"),
    Q: post(Q, "400", "Debate works in our new evals, details soon."),
    C1: post(C1, "500", "A long thread start, truncated at 280 chars https://t.co/c1",
             note="A long thread start with the whole note text that runs past the old limit, about agent "
                  "control evaluations and what they miss.", urls=[("https://t.co/c1", "https://example.org/x")]),
    C2: post(C2, "500", "Part two.", conv=C1, reply_to=(C1, "500")),
    C3: post(C3, "500", "Part three.", conv=C1, reply_to=(C2, "500")),
    A4: post(A4, "100", "Fourth, a late addition.", conv=A1, reply_to=(A3, "100")),
    F1: post(F1, "700", "@carol Not solved, see below.", conv=P, reply_to=(P, "300")),
    F2: post(F2, "700", "The details.", conv=P, reply_to=(F1, "700")),
}


def plan(budget: float = 1.0, **options: Any) -> Plan:
    line: Row = {"stage": "collect.x-lookup", "prior": ["sources", "coverage"], "keys": ["x_post"],
                 "miss_cap": 2, "out": {"kind": "part"}, "budget": {"max_usd": budget},
                 "options": {"ids": [f"https://twitter.com/erin/status/{C1}?s=20"], **options}}
    return Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [line]})


DOCS = [doc(1, "manual", [("x_post", A1)]), doc(2, "manual", [("x_post", B)]), doc(4, "manual", [("x_post", D)])]
for _d, _h, _p in ((DOCS[0], "alice", A1), (DOCS[1], "bob", B), (DOCS[2], "nobody", D)):
    _d["identity"]["members"][0]["canonical_url"] = f"https://x.com/{_h}/status/{_p}"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "store"
    publish_documents(root, DOCS)
    monkeypatch.setattr(xapi, "_sleep", lambda s: None)
    monkeypatch.setenv(xapi.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(xapi.CAP_ENV, raising=False)
    monkeypatch.delenv(xapi.LIMIT_ENV, raising=False)
    return root


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    f = FakeHttp()
    monkeypatch.setattr(xapi, "SEND", f)
    return f


def rl(limit: int, remaining: int) -> dict[str, str]:
    return {"x-rate-limit-limit": str(limit), "x-rate-limit-remaining": str(remaining),
            "x-rate-limit-reset": str(int(NOW.timestamp()) + 900)}


def add_usage(fake: FakeHttp, n: int) -> None:
    fake.add(xapi.USAGE_URL, {}, json={"data": {"project_usage": str(n), "project_cap": "3000000"}},
             headers=rl(50, 49))


def add_lookup(fake: FakeHttp, ids: list[str], *, includes: list[str] = (), missing: list[str] = ()) -> None:
    body: Row = {"data": [POSTS[i] for i in ids if i in POSTS and i not in missing]}
    if includes:
        body["includes"] = {"tweets": [POSTS[i] for i in includes]}
    errs = [i for i in ids if i in missing or i not in POSTS]
    if errs:
        body["errors"] = [{"value": i, "detail": f"Could not find tweet with ids: [{i}].", "title": "Not Found Error",
                           "resource_type": "tweet", "parameter": "ids", "resource_id": i,
                           "type": "https://api.twitter.com/2/problems/resource-not-found"} for i in errs]
    fake.add(xapi.LOOKUP_URL, xapi.lookup_params(ids, False), json=body, headers=rl(3500, 3499))


def add_threads(fake: FakeHttp) -> None:
    fake.add(xapi.SEARCH_RECENT_URL, xapi.search_params(A1, "100", 25, None, handle="alice"), headers=rl(450, 449),
             json={"data": [POSTS[i] for i in (A9, A3, A2, A1)], "meta": {"result_count": 4}})
    fake.add(xapi.SEARCH_ALL_URL, xapi.search_params(C1, "500", 25, None, handle="erin"), headers=rl(300, 299),
             json={"data": [POSTS[C3], POSTS[C2]], "meta": {"result_count": 2, "next_token": "t1"}})
    fake.add(xapi.SEARCH_ALL_URL, xapi.search_params(C1, "500", 25, "t1", handle="erin"), headers=rl(300, 298),
             json={"data": [POSTS[C1]], "meta": {"result_count": 1}})


def view(root: Path, chain_id: str) -> dict[str, Row]:
    rows = read_view(resolve(root, sdir(chain_id))).to_pylist()
    if chain_id.startswith("sources"):
        return {r["identity"]["external_id"]: r for r in rows}
    return {r["query"]: r for r in rows}


def record_of(root: Path, row: Row) -> Row:
    rec = json_record(open_blobs(root), row)
    assert isinstance(rec, dict)
    return rec


def text_of(root: Path, row: Row) -> str:
    entries = {e["content_role"]: e for e in row["inband"]["entries"]}
    assert set(entries) == {"post", "provider_record"}
    entry = entries["post"]
    # D690: the raw is the JSON record, the text its Markdown rendering, each a BLOB with its own media type
    assert (entry["content_role"], entry["raw"]["media_type"], entry["text"]["media_type"]) == (
        "post", "application/json", "text/markdown")
    # D522: the provider_record is the same canonical JSON as the post's raw (one blob)
    assert entry["raw"] == entries["provider_record"]["raw"]
    text = open_blobs(root).get(entry["text"]["sha256"])
    assert entry["text"]["bytes"] == len(text)
    return text.decode()


def x_run(root: Path, p: Plan | None = None) -> RunRecord:
    rec = run_line(root, p or plan(), "collect.x-lookup")
    assert rec is not None
    return rec


# ─────────────────────────────────────────────────────────────── pure helpers
def test_render_expands_links_marks_media_and_quotes() -> None:
    rec = xapi.build_record(POSTS[B], includes={P: POSTS[P], Q: POSTS[Q]},
                            handles={"200": "bob", "300": "carol", "400": "dave"})
    text = xapi.render(rec)
    assert text.startswith(f"@bob on X, {POSTS[B]['created_at'][:10]}")
    assert f"https://x.com/bob/status/{B}" in text
    assert "see https://arxiv.org/abs/2610.00002 https://x.com/dave/status/" in text and "t.co" not in text
    assert "Quoting @dave" in text and "> Debate works" in text
    assert "In reply to @carol" in text and "> Is scalable oversight solved?" in text
    assert xapi.title_of(rec).startswith("@bob: @carol Agree, see https://arxiv.org/abs/2610.00002")
    a = xapi.build_record(POSTS[A1], handles={"100": "alice"})
    assert xapi.expanded_text(POSTS[A1]) == "Our new interpretability paper https://arxiv.org/abs/2610.00001 [media]"
    assert "@alice" in xapi.render(a)
    long = xapi.build_record(POSTS[C1])
    assert "whole note text" in xapi.render(long) and "X user 500" in xapi.render(long)


def test_self_thread_follows_the_authors_reply_chain() -> None:
    assert xapi.search_params(A1, "100", 25, "tok", handle="alice") == {
        "query": f"conversation_id:{A1} from:100 (to:alice OR -is:reply)", "max_results": 25,
        "tweet.fields": xapi.TWEET_FIELDS, "expansions": "referenced_tweets.id", "next_token": "tok"}
    # no handle: every post of the author in the conversation (full-archive search ignores to:<id>)
    assert xapi.search_params(A1, "100", 25, None)["query"] == f"conversation_id:{A1} from:100"
    found = [POSTS[i] for i in (A9, A3, A1, A2)]
    assert [p["id"] for p in xapi.self_thread(found, "100", A1, [A1])] == [A1, A2, A3]
    # the root is outside the search window: the chain still grows from the linked post
    assert [p["id"] for p in xapi.self_thread([POSTS[A3], POSTS[A2]], "100", A1, [A2])] == [A2, A3]
    # a seed whose parent is not in the chain stays (a linked or earlier member); duplicates collapse
    assert [p["id"] for p in xapi.self_thread([POSTS[A1], POSTS[A3], POSTS[A3]], "100", A1, [A3])] == [A1, A3]
    assert xapi.is_thread_candidate(POSTS[A1]) and xapi.is_thread_candidate(POSTS[A2])
    assert not xapi.is_thread_candidate(POSTS[B])  # a reply to carol
    assert xapi.is_own_conversation(found, A1, "100") and not xapi.is_own_conversation(found, A1, "900")
    assert not xapi.is_own_conversation([POSTS[A3]], A1, "100")  # root absent: someone else's (or deleted)


def test_thread_record_renders_once_in_order_with_member_keys() -> None:
    q = post(snow(NOW - dt.timedelta(days=2), 7), "400", "Quoted context.")
    a2 = dict(POSTS[A2], referenced_tweets=[*POSTS[A2]["referenced_tweets"], {"id": q["id"], "type": "quoted"}])
    rec = xapi.build_thread_record([POSTS[A1], a2, POSTS[A3]], cid=A1, author_id="100",
                                   quotes={q["id"]: q}, handles={"100": "alice", "400": "dave"})
    assert xapi.thread_external_id(A1, "100") == f"thread:{A1}:100"
    assert xapi.row_keys(rec) == [("x_post", A1), ("x_post", A2), ("x_post", A3), ("conversation_id", A1),
                                  ("quote_of", q["id"])]
    text = xapi.render(rec)
    assert text.startswith("Thread by @alice on X (3 posts)") and f"https://x.com/alice/status/{A1}" in text
    order = [text.index(s) for s in ("[1/3] Our new", "[2/3] Second", "Quoting @dave", "[3/3] Third")]
    assert order == sorted(order) and text.count("Second, the method.") == 1
    assert xapi.title_of(rec).startswith("@alice (thread, 3 posts): Our new")
    one = xapi.build_thread_record([POSTS[A1]], cid=A1, author_id="100", handles={"100": "alice"})
    assert xapi.render(one) == xapi.render(xapi.build_record(POSTS[A1], handles={"100": "alice"}))
    assert xapi.title_of(one).startswith("@alice: Our new")


def test_row_keys_and_repost_collapse() -> None:
    rec = xapi.build_record(POSTS[B], includes={Q: POSTS[Q]})
    assert xapi.row_keys(rec) == [("x_post", B), ("conversation_id", P), ("reply_to_id", P), ("quote_of", Q)]
    rt = post(snow(NOW, 77), "600", "RT @alice: Our new ...", retweet=A1)
    keys = xapi.row_keys(xapi.build_record(rt, includes={A1: POSTS[A1]}))
    assert sorted(k for k in keys if k[0] == "x_post") == sorted([("x_post", rt["id"]), ("x_post", A1)])
    assert ("repost_of", A1) in keys


def test_relayed_urls_skip_media_and_own_posts() -> None:
    rec = xapi.build_thread_record([POSTS[A1], POSTS[A2], POSTS[A3]], cid=A1, author_id="100")
    assert xapi.relayed_urls(rec) == [{"url": "https://arxiv.org/abs/2610.00001", "post_id": A1},
                                      {"url": "https://github.com/alice/interp", "post_id": A3}]
    rec_b = xapi.build_record(POSTS[B], includes={Q: POSTS[Q]})
    assert [u["url"] for u in xapi.relayed_urls(rec_b)] == ["https://arxiv.org/abs/2610.00002"]


def test_post_time_and_ledger_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The X ledger is the run's own (in memory), capped per run at CAP_USD, which the env may only lower."""
    assert xapi.post_time("2108108695876092323").isoformat()[:19] == "2026-10-08T08:14:14"
    monkeypatch.delenv(xapi.CAP_ENV, raising=False)
    try:
        assert xapi.x_ledger("t-x1").cap_usd == xapi.CAP_USD == 10.0
        assert xapi.x_ledger("t-x1") is xapi.x_ledger("t-x1") and xapi.x_ledger("t-x2") is not xapi.x_ledger("t-x1")
        monkeypatch.setenv(xapi.CAP_ENV, "1")
        assert xapi.x_ledger("t-x3").cap_usd == 1.0
        monkeypatch.setenv(xapi.CAP_ENV, "11")
        with pytest.raises(CollectError, match="may only lower"):
            xapi.x_ledger("t-x4")
    finally:
        for r in ("t-x1", "t-x2", "t-x3", "t-x4"):
            forget_run(r)


# ─────────────────────────────────────────────────────────────── the stage
@pytest.mark.slow  # real store + runner
def test_lookup_threads_quotes_misses_cost_and_rerun(store: Path, fake: FakeHttp, tmp_path: Path,
                                                     run_ledgers: Any) -> None:
    add_usage(fake, 10)
    add_lookup(fake, [A1, B, D, C1], includes=[P, Q], missing=[D])
    add_threads(fake)
    rec = x_run(store)
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is True
    n = rec.counts
    assert (n["keys_due"], n["x_threads_ok"], n["x_quoted_rows"], n["x_miss_not_found_error"]) == (4, 2, 1, 1)
    assert n["x_project_usage_before"] == 10
    # cost: lookup 3 posts + 2 includes; thread A adds A2, A3, A9; thread C adds C2, C3 = 10 posts
    assert (n["x_posts_counted"], n["x_cost_micro_usd"]) == (10, 50_000)
    assert rec.spend == {"usd_micro": {"x": 50_000}, "reserved_open_usd_micro": 0, "batch_ids": [],
                         "abandoned_batch_ids": []}
    assert (n["x_ratelimit_limit_lookup"], n["x_ratelimit_remaining_search_all"]) == (3500, 298)
    lines = run_ledgers(rec.run_id, "x").lines()
    assert {ln["kind"] for ln in lines} == {"reserve", "reconcile"}
    assert round(sum(ln["cost_usd"] for ln in lines), 6) == 0.05
    # every request carried the bearer header; the token is in no row, count or record
    assert all(c.headers.get("Authorization") == f"Bearer {TOKEN}" for c in fake.calls)
    src = view(store, "sources.x-lookup")
    assert TOKEN not in json.dumps([rec.counts, list(src.values())], default=str)

    # alice's and erin's posts are thread candidates: one self-thread row each, no post row
    assert A1 not in src and C1 not in src and n["x_thread_rows"] == 2
    a = src[f"thread:{A1}:100"]
    assert (a["state"], a["identity"]["channel"], a["identity"]["canonical_url"]) == (
        "observed", "x", f"https://x.com/alice/status/{A1}")
    assert a["identity"]["keys"] == [{"kind": "x_post", "value": v} for v in (A1, A2, A3)] + [
        {"kind": "conversation_id", "value": A1}]
    md = a["metadata"]
    assert (md["doc_type"], md["license_class"], md["lookup_of_source_ids"]) == ("social_post", "api_tos", [DOCS[0]["doc_id"]])
    assert [x["name"] for x in md["authors"]] == ["@alice"] and md["title"].startswith("@alice (thread, 3 posts): Our new")
    ra = record_of(store, a)
    assert ([p["id"] for p in ra["thread"]], ra["thread_status"], ra["kind"]) == ([A1, A2, A3], "ok", "thread")
    ta = text_of(store, a)
    assert ta.startswith("Thread by @alice on X (3 posts)") and "[1/3] Our new" in ta and "@zed thanks" not in ta
    b = src[B]
    assert {(k["kind"], k["value"]) for k in b["identity"]["keys"]} == {
        ("x_post", B), ("conversation_id", P), ("reply_to_id", P), ("quote_of", Q)}
    tb = text_of(store, b)
    assert "Quoting @dave" in tb and "In reply to @carol" in tb and record_of(store, b)["thread_status"] is None
    assert src[Q]["metadata"]["title"].startswith("@dave: Debate works")  # the quoted post's own row
    c = src[f"thread:{C1}:500"]
    assert c["identity"]["canonical_url"] == f"https://x.com/erin/status/{C1}"  # handle from the ids option
    assert [p["id"] for p in record_of(store, c)["thread"]] == [C1, C2, C3]
    assert "whole note text" in text_of(store, c)
    d = src[D]
    assert (d["state"], d["metadata"]["miss_count"]) == ("missing", 1)
    assert P not in src  # replied-to posts are context, not documents
    cov = view(store, "coverage.x-lookup")
    # attribution (D228): the batch lookup that asked for the post (the quoted post: its quoter's)
    lookup_digest = {r["query_digest"] for q, r in cov.items() if "x_thread" not in json.loads(q)}
    for pid, label in ((B, B), (Q, B), (D, D)):
        assert [(m["query_id"], m["label"]) for m in src[pid]["metadata"]["matched_by"]] == [
            ("lookup:x:ids", label)], pid
        assert {m["query_digest"] for m in src[pid]["metadata"]["matched_by"]} == lookup_digest
    # a thread row: the search that returned it and the lookup that asked for its member
    assert sorted((m["query_id"], m["label"]) for m in a["metadata"]["matched_by"]) == [
        ("lookup:x:ids", A1), (f"thread:{A1}", f"{A1}:100")]
    assert sorted((json.loads(q).get("x_thread") or "lookup", r["status"], r["seen"], r["kept"])
                  for q, r in cov.items()) == sorted([("lookup", "ok", 4, 3), (A1, "ok", 4, 3), (C1, "ok", 3, 3)])
    assert [r.check for r in run_checks(store) if r.status in ("failed", "warn")] == []

    # run 2: only the miss is asked again (relist_after_h 0: a same-day pass waits 12 h, D226, see
    # tests/test_passes_collect.py); nothing else is requested (no usage call either side)
    calls = len(fake.calls)
    add_lookup(fake, [D], missing=[D])
    rec2 = x_run(store, plan(relist_after_h=0))
    assert rec2.status == "ok" and rec2.counts["keys_due"] == 1 and rec2.counts["minted_ids"] == 0
    assert [c.url.split("?")[0] for c in fake.calls[calls:]] == [xapi.USAGE_URL, xapi.LOOKUP_URL, xapi.USAGE_URL]
    assert view(store, "sources.x-lookup")[D]["state"] == "retracted"  # miss_cap 2, miss_means withdrawn
    # run 3: nothing due, no request at all
    calls = len(fake.calls)
    rec3 = x_run(store)
    assert rec3.status == "ok" and rec3.counts["keys_due"] == 0 and rec3.outputs == {}
    assert len(fake.calls) == calls


@pytest.mark.slow  # real store + runner
def test_budget_stop_leaves_keys_due_and_the_thread_is_retried(store: Path, fake: FakeHttp, run_ledgers: Any) -> None:
    """The pending thread is resumed from the post row's ``provider_record`` (D524)."""
    add_usage(fake, 0)
    add_lookup(fake, [A1])
    rec = x_run(store, plan(budget=0.01))  # one id fits (0.01 reserved per id); its thread search does not
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is False and rec.counts["x_budget_stop"] == 1
    assert rec.counts["x_cost_micro_usd"] == 5_000
    lines = run_ledgers(rec.run_id, "x").lines()
    assert sum(ln["cost_usd"] for ln in lines) <= 0.01 + 1e-9
    src = view(store, "sources.x-lookup")
    assert set(src) == {A1} and record_of(store, src[A1])["thread_status"] == "pending"
    [thread_cov] = [r for q, r in view(store, "coverage.x-lookup").items() if "x_thread" in q]
    assert thread_cov["status"] == "partial"
    assert incomplete_reasons(rec.counts) == ["budget"]  # D492: a budget stop, not a cap

    add_lookup(fake, [B, D, C1], includes=[P, Q], missing=[D])
    add_threads(fake)
    rec2 = x_run(store)
    assert rec2.status == "ok" and rec2.scope_complete is True, rec2.error
    assert (rec2.counts["x_threads_to_retry"], rec2.counts["x_thread_rows"]) == (1, 2)
    src = view(store, "sources.x-lookup")
    t = src[f"thread:{A1}:100"]  # the retried search writes the thread row; the pending post row stays
    assert [p["id"] for p in record_of(store, t)["thread"]] == [A1, A2, A3]
    assert t["metadata"]["lookup_of_source_ids"] == [DOCS[0]["doc_id"]]  # carried from the pending row
    assert text_of(store, t).startswith("Thread by @alice on X (3 posts)")
    assert record_of(store, src[A1])["thread_status"] == "pending"
    calls = len(fake.calls)
    add_lookup(fake, [D], missing=[D])
    rec3 = x_run(store, plan(relist_after_h=0))  # the thread is done now, D's miss is the only due key
    assert rec3.counts["x_threads_to_retry"] == 0 and rec3.counts["keys_due"] == 1
    assert [c.url.split("?")[0] for c in fake.calls[calls:]].count(xapi.SEARCH_RECENT_URL) == 0


@pytest.mark.slow  # real store + runner
def test_no_token_429_and_refusals(store: Path, fake: FakeHttp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(xapi.TOKEN_ENV)
    rec = x_run(store)
    assert rec.status == "ok" and rec.scope_complete is False and rec.counts["x_no_credentials"] == 1
    assert fake.calls == [] and rec.outputs == {}

    monkeypatch.setenv(xapi.TOKEN_ENV, TOKEN)
    add_usage(fake, 0)
    fake.add(xapi.LOOKUP_URL, xapi.lookup_params([A1, B, D, C1], False), status=429,
             headers={"x-rate-limit-reset": str(int(NOW.timestamp()) + 600)})
    rec2 = x_run(store)
    assert rec2.status == "ok" and rec2.scope_complete is False and rec2.counts["halted"] == 1
    assert [c.url.split("?")[0] for c in fake.calls] == [xapi.USAGE_URL, xapi.LOOKUP_URL]  # no usage after a halt
    assert "sources" not in rec2.outputs  # every key still due
    [cov] = view(store, "coverage.x-lookup").values()
    assert (cov["status"], cov["error_class"], cov["attempts"]) == ("failed_transient", "rate_limit", 0)

    no_budget = Plan.model_validate({"plan": "test", "schema_version": 2, "series": "main", "stages": [
        {"stage": "collect.x-lookup", "prior": ["sources", "coverage"], "miss_cap": 2}]})
    rec3 = x_run(store, no_budget)
    assert rec3.status == "failed" and "budget.max_usd" in (rec3.error or "")
    monkeypatch.setenv(xapi.CAP_ENV, "20")
    rec4 = x_run(store)
    assert rec4.status == "failed" and "may only lower" in (rec4.error or "")
    assert len(fake.calls) == 2


# ─────────────────────────────────────────────────────────────── one document per thread
@pytest.fixture
def xenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(xapi, "_sleep", lambda s: None)
    monkeypatch.setenv(xapi.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(xapi.CAP_ENV, raising=False)
    monkeypatch.delenv(xapi.LIMIT_ENV, raising=False)
    return tmp_path


def link(i: int, pid: str, handle: str = "alice") -> Row:
    """A ``links_social`` row (``collect.links``' tweet target) for post ``pid``."""
    row = src(i, [("x_post", pid)], channel="links_social")
    row["identity"]["canonical_url"] = f"https://x.com/{handle}/status/{pid}"
    return row


def linked_ids(store: Store) -> list[str]:
    """The ``x_post`` keys of the documents head in x-lookup's order (doc id, then key order)."""
    return [k["value"] for _, d in sorted(store.docs().items()) for k in d["identity"]["keys"]
            if k["kind"] == "x_post"]


def x_docs(store: Store) -> list[set[str]]:
    """Per live document: its ``x_post`` keys."""
    return sorted(({k["value"] for k in d["identity"]["keys"] if k["kind"] == "x_post"}
                   for d in store.docs().values() if d["state"] != "retired"), key=sorted)


def thread_search(fake: FakeHttp, ids: list[str]) -> None:
    fake.add(xapi.SEARCH_RECENT_URL, xapi.search_params(A1, "100", 25, None, handle="alice"), headers=rl(450, 449),
             json={"data": [POSTS[i] for i in reversed(ids)], "meta": {"result_count": len(ids)}})


@pytest.mark.slow  # real store + runner + gather
def test_two_linked_members_are_one_document_and_a_grown_thread_extends_it(xenv: Path) -> None:
    """Links to A1 and A2 (two documents) → one lookup, one search, one thread row whose keys are
    every member → gather makes one document; a re-run asks nothing. Later A4 is linked: only A4
    is due, the search is repeated and the same thread row (same source) grows to four posts;
    gather keeps one document."""
    store = Store(xenv / "store")
    store.add("sources.links", [link(1, A1), link(2, A2)])
    store.gather()
    assert x_docs(store) == [{A1}, {A2}]
    fake = FakeHttp()
    xapi.SEND = fake
    try:
        add_usage(fake, 0)
        ids = linked_ids(store)
        add_lookup(fake, ids)
        thread_search(fake, [A1, A2, A3, A9])
        rec = run_line(store.root, plan(ids=[]), "collect.x-lookup")
        assert rec is not None
        assert rec.status == "ok" and rec.scope_complete is True, rec.error
        n = rec.counts
        assert (n["keys_due"], n["x_threads_ok"], n["x_thread_rows"], n["x_thread_posts"]) == (2, 1, 1, 3)
        assert [c.url.split("?")[0] for c in fake.calls].count(xapi.SEARCH_RECENT_URL) == 1
        srcs = view(store.root, "sources.x-lookup")
        assert set(srcs) == {f"thread:{A1}:100"}
        t = srcs[f"thread:{A1}:100"]
        assert t["metadata"]["lookup_of_source_ids"] == sorted([store_sid(store, A1), store_sid(store, A2)])
        text = text_of(store.root, t)
        assert text.count("Second, the method.") == 1 and "[3/3] Third" in text and "@zed" not in text
        store.chains.append("sources.x-lookup")
        store.gather()
        assert x_docs(store) == [{A1, A2, A3}]
        [d] = [d for d in store.docs().values() if d["state"] != "retired"]
        assert len(d["identity"]["members"]) == 3  # two links and the thread

        calls = len(fake.calls)
        rec = run_line(store.root, plan(ids=[]), "collect.x-lookup")
        assert rec is not None
        # the document's keys per member: A1, A2 (links) and A1, A2, A3 (thread): all in the thread
        assert (rec.counts["keys_due"], rec.counts["keys_in_thread"]) == (0, 5) and len(fake.calls) == calls

        # the thread grows: A4 is linked later
        store.add("sources.links", [link(3, A4)])
        store.gather()
        assert x_docs(store) == [{A1, A2, A3}, {A4}]
        add_lookup(fake, [A4])
        thread_search(fake, [A1, A2, A3, A4, A9])
        rec = run_line(store.root, plan(ids=[]), "collect.x-lookup")
        assert rec is not None
        assert rec.status == "ok", rec.error
        n = rec.counts
        assert (n["keys_due"], n["keys_in_thread"], n["x_thread_rows"], n["x_threads_extended"]) == (1, 5, 1, 1)
        t2 = view(store.root, "sources.x-lookup")[f"thread:{A1}:100"]
        assert t2["source_id"] == t["source_id"] and t2["run_id"] == rec.run_id
        assert [k["value"] for k in t2["identity"]["keys"] if k["kind"] == "x_post"] == [A1, A2, A3, A4]
        assert t2["metadata"]["lookup_of_source_ids"] == sorted(store_sid(store, p) for p in (A1, A2, A4))
        assert "[4/4] Fourth, a late addition." in text_of(store.root, t2)
        store.gather()
        assert x_docs(store) == [{A1, A2, A3, A4}]
        assert [r.check for r in run_checks(store.root) if r.status in ("failed", "warn")] == []
    finally:
        xapi.SEND = None


def store_sid(store: Store, pid: str) -> str:
    """The source id of the link row for post ``pid``."""
    rows = read_view(resolve(store.root, sdir("sources.links"))).to_pylist()
    return next(r["source_id"] for r in rows if r["identity"]["keys"][0]["value"] == pid)


def test_a_self_reply_in_someone_elses_conversation_is_a_post(xenv: Path, fake: FakeHttp) -> None:
    """F2 answers its own author inside carol's conversation: a candidate, searched (full archive,
    40 days old; given as a bare id, so frank's handle is unknown and the query has no ``to:``),
    but the root is not frank's, so F2 is a post row (``foreign``) with its replied-to F1 as
    context, and no thread row."""
    root = xenv / "store"
    add_usage(fake, 0)
    add_lookup(fake, [F2], includes=[F1])
    fake.add(xapi.SEARCH_ALL_URL, xapi.search_params(P, "700", 25, None), headers=rl(300, 299),
             json={"data": [POSTS[F2], POSTS[F1]], "meta": {"result_count": 2}})
    rec = run_stage(root, plan(ids=[F2]), "collect.x-lookup", [], series="main", kind="part")
    assert rec.status == "ok" and rec.scope_complete is True, rec.error
    assert (rec.counts["x_threads_ok"], rec.counts["x_threads_foreign"], rec.counts["x_thread_no_handle"]) == (1, 1, 1)
    assert "x_thread_rows" not in rec.counts
    srcs = view(root, "sources.x-lookup")
    assert set(srcs) == {F2}
    r = record_of(root, srcs[F2])
    assert (r["thread_status"], r["thread"], r["replied_to"]["id"]) == ("foreign", [], F1)
    assert "In reply to X user 700" in text_of(root, srcs[F2]) and "Not solved" in text_of(root, srcs[F2])

