"""``acquire`` core (PLAN T3.1): the §5.5 pointer table, carry by member and regroup after a merge
(X21) or a split, the per-role accounting (attempts monotone, backoff, cap 4 →
``quarantined[cap_reached]``), a halted lane charging no row and halting only itself
(T3.6-f2), the weekly recheck of an ``ok`` page role (D22), the ``max_rows`` stop, "the part file holds only touched rows" and the run's
``--filter`` through the real runner, ``carry.no_wants``, dev-v2's ``wants`` keyed by real
channels (T3.6-f1), and carry reaching the rows a stop leaves unreached (D587). Lanes are
:class:`tests.fakes.lanes.FakeLane`; no network.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
import yaml

from stack.build import BuildFailed, preflight
from stack.checks.verify import run_checks
from stack.plan import Plan, load_plan
from stack.runs import read_run_records
from stack.schemas.structs import text_blob
from stack.stage_runner import StageError, run_stage
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from stack.store.parquet import read_header
from tests.fakes.lanes import FakeLane
from tests.synth.records import sdir
from tests.synth.rows import AT, CONVERT_PIN, blob, dg, document_row, rid, sid
from tests.synth.world import write_on_head
from workflows.acquire import lanes
from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv
from workflows.acquire.carry import carry, fetch_key, new_content, new_role
from workflows.acquire.lanes.arxiv import arxiv_id_of
from workflows.acquire.main import OPTIONS, AcquireResult, acquire_rows, check_options
from workflows.acquire.point import point
from workflows.acquire.scope import FETCH, is_due

Row = dict[str, Any]
NOW = AT + dt.timedelta(hours=30)
H = dt.timedelta(hours=1)
PIN = CONVERT_PIN
GROUPS = {"arxiv": "paper", "openalex": "paper", "semantic_scholar": "paper", "lesswrong": "forum", "web": "url"}
WANTS = {"arxiv": ["full_text"], "web": ["page"]}


# ---------------------------------------------------------------- helpers
def snap(n: int, source: str, *, kind: str = "raw", role: str = "full_text", via: str = "live",
         at: dt.datetime = AT, text: str | None = None, status: str | None = None,
         cfg: str | None = PIN, media: str = "application/pdf") -> Row:
    raw = blob(dg(("raw", n)), 10, media)
    return {"snapshot_id": f"{rid(20)}.{n}", "source_id": source, "kind": kind, "content_role": role,
            "via": via, "url": None, "final_url": None, "http_status": None,
            "etag": None, "last_modified": None, "captured_at": at, "as_of": at, "revision": None,
            "raw": raw, "text": text_blob(text, 20, raw) if text else None, "text_status": status,
            "text_error_class": None, "convert_attempts": 1 if status else 0,
            "convert_pin": cfg if status else None}


def members(*ids: str, dead: tuple[str, ...] = ()) -> list[Row]:
    return [{"source_id": s, "is_live": s not in dead} for s in ids]


def doc(i: int, *, channel: str = "arxiv", state: str = "triaged") -> Row:
    row = document_row(i, state=state)
    row["identity"]["members"][0]["channel"] = channel
    return row


def env(tmp_path: Path, now: dt.datetime = NOW, run: int = 50) -> LaneEnv:
    return LaneEnv(run_id=rid(run), now=now, blobs=open_blobs(tmp_path), convert_pin=PIN,
                   counts={})


def go(rows: list[Row], tmp_path: Path, lane_map: dict[str, FakeLane], *, now: dt.datetime = NOW,
       run: int = 50, **kw: Any) -> tuple[AcquireResult, LaneEnv]:
    e = env(tmp_path, now, run)
    with lanes.using(lane_map):
        res = acquire_rows(rows, e, wants=kw.pop("wants", WANTS), channel_group=GROUPS.get, **kw)
    return res, e


def merge(rows: list[Row], res: AcquireResult) -> list[Row]:
    """The chain view after publishing ``res.rows`` on ``rows``."""
    new = {r["doc_id"]: r for r in rows} | {r["doc_id"]: r for r in res.rows}
    return [new[d] for d in sorted(new)]


def role(row: Row, source: str, content_role: str = "full_text") -> Row:
    return next(r for r in row["content"]["roles"]
                if (r["source_id"], r["content_role"]) == (source, content_role))


# ---------------------------------------------------------------- the pointer (§5.5)
A, B = sid(1), sid(2)
POINTER_TABLE = [
    ("none", [], members(A), (None, None, "no_raw")),
    ("inband", [snap(1, A, kind="inband", role="abstract", via="inband", text=dg("t1"), cfg=None,
                     status="ok", media="text/plain")], members(A), (f"{rid(20)}.1", dg("t1"), "ok")),
    ("raw ok at pin", [snap(1, A, text=dg("t1"), status="ok")], members(A), (f"{rid(20)}.1", dg("t1"), "ok")),
    ("raw failed", [snap(1, A, status="failed_permanent")], members(A), (f"{rid(20)}.1", None, "convert_failed")),
    ("raw unconverted", [snap(1, A)], members(A), (f"{rid(20)}.1", None, "convert_pending")),
    ("newer raw failed: never the older text",
     [snap(1, A, at=AT - H, text=dg("t1"), status="ok"), snap(2, A, at=AT, status="quarantined")],
     members(A), (f"{rid(20)}.2", None, "convert_failed")),
    ("ok at another pin is not text", [snap(1, A, text=dg("t1"), status="ok", cfg=dg("old pin"))],
     members(A), (f"{rid(20)}.1", None, "convert_pending")),
    ("full text over a newer abstract",
     [snap(1, A, text=dg("t1"), status="ok", at=AT - H),
      snap(2, A, kind="inband", role="abstract", via="inband", text=dg("t2"), cfg=None, status="ok")],
     members(A), (f"{rid(20)}.1", dg("t1"), "ok")),
    ("live over a newer wayback",
     [snap(1, A, role="page", at=AT - H, text=dg("t1"), status="ok"),
      snap(2, A, role="page", via="wayback", text=dg("t2"), status="ok")],
     members(A), (f"{rid(20)}.1", dg("t1"), "ok")),
    ("masked member is no candidate",
     [snap(1, A, text=dg("t1"), status="ok"), snap(2, B, kind="inband", role="abstract", via="inband",
                                                   text=dg("t2"), cfg=None, status="ok")],
     members(A, B, dead=(A,)), (f"{rid(20)}.2", dg("t2"), "ok")),
    ("xml is never text", [snap(1, A, role="xml")], members(A), (None, None, "no_raw")),
]


@pytest.mark.parametrize("snaps,mems,want", [t[1:] for t in POINTER_TABLE], ids=[t[0] for t in POINTER_TABLE])
def test_pointer_rule_table(snaps: list[Row], mems: list[Row], want: tuple[Any, ...]) -> None:
    got = point({"snapshots": snaps}, mems, PIN)
    assert (got["current_snapshot_id"], got["current_text_sha256"], got["current_text_status"]) == want
    assert (got["current_text_media"] is None) == (want[2] != "ok")


# ---------------------------------------------------------------- stable supplier (D807)
def licensed(*pairs: tuple[str, str], dead: tuple[str, ...] = ()) -> list[Row]:
    """Members ``(source_id, license_class)``, live unless in ``dead``."""
    return [{"source_id": s, "is_live": s not in dead, "license_class": lic} for s, lic in pairs]


def repoint(snaps: list[Row], mems: list[Row], prev: str | None, structured: tuple[str, ...] = ()) -> str | None:
    return point({"snapshots": snaps, "current_snapshot_id": prev}, mems, PIN, structured)["current_snapshot_id"]


OLD, NEW = f"{rid(20)}.1", f"{rid(20)}.2"
STABLE_TABLE = [
    # (label, A's class, B's class, B's snapshot overrides, A live, expected current)
    ("identical text: the previous supplier stays", "open", "open", {}, True, OLD),
    ("different text: the newer one wins", "open", "open", {"text": dg("t2")}, True, NEW),
    ("licence gain restricted -> open: the looser one wins", "restricted", "open", {}, True, NEW),
    ("a stricter newcomer does not displace", "open", "restricted", {}, True, OLD),
    ("tied classes (restricted, api_tos) keep", "restricted", "api_tos", {}, True, OLD),
    ("the previous member is no longer live", "open", "open", {}, False, NEW),
    ("another arXiv version with the same text moves", "open", "open", {"revision": "v2"}, True, NEW),
]


@pytest.mark.parametrize("lic_a,lic_b,over,a_live,want", [t[1:] for t in STABLE_TABLE],
                         ids=[t[0] for t in STABLE_TABLE])
def test_pointer_keeps_the_supplier_of_an_unchanged_text(lic_a: str, lic_b: str, over: dict[str, Any],
                                                         a_live: bool, want: str) -> None:
    """D807: B's newer snapshot sorts first; A's, the previous current, stays current when B changes nothing but
    the supplier (same digest, role, revision, media, wayback flag) and is not strictly looser in licence."""
    text_b = over.pop("text", dg("t1"))
    a, b = snap(1, A, at=AT - H, text=dg("t1"), status="ok"), snap(2, B, text=text_b, status="ok")
    a["revision"] = "v1"
    b["revision"] = over.get("revision", "v1")
    mems = licensed((A, lic_a), (B, lic_b), dead=() if a_live else (A,))
    assert repoint([a, b], mems, None) == NEW  # no previous pointer: the order decides, as before
    assert repoint([a, b], mems, OLD) == want
    assert repoint([a, b], mems, want) == want  # idempotent: pointing again keeps what it chose


def test_pointer_stability_needs_both_texts_ok_and_no_wayback_over_live() -> None:
    """D807 leaves the order alone when either text is not ``ok`` at the pin (a failed or pending newcomer is
    current as before; a failed previous text is no reason to stay), and never keeps a wayback capture over a
    live one (V6's rule)."""
    mems = licensed((A, "open"), (B, "open"))
    ok_a = snap(1, A, at=AT - H, text=dg("t1"), status="ok")
    assert repoint([ok_a, snap(2, B, status="quarantined")], mems, OLD) == NEW
    assert repoint([ok_a, snap(2, B)], mems, OLD) == NEW  # B not converted yet: convert_pending, as before
    failed_a = snap(1, A, at=AT - H, text=dg("t1"), status="failed_permanent")
    assert repoint([failed_a, snap(2, B, text=dg("t1"), status="ok")], mems, OLD) == NEW
    way_a = snap(1, A, role="page", via="wayback", text=dg("t1"), status="ok")
    live_b = snap(2, B, role="page", at=AT - H, text=dg("t1"), status="ok")
    assert repoint([way_a, live_b], mems, OLD) == NEW
    other_pin = snap(1, A, at=AT - H, text=dg("t1"), status="ok", cfg=dg("old pin"))
    assert repoint([other_pin, snap(2, B, text=dg("t1"), status="ok")], mems, OLD) == NEW


def test_pointer_stability_in_the_structured_order() -> None:
    """With ``structured`` on: A's arXiv HTML stays current over B's same-text HTML of the same revision, while
    the too-short rule (B9) still hands over to the PDF partner (another text, so D807 does not apply)."""
    mems = licensed((A, "open"), (B, "open"))
    html_a = snap(1, A, role="html", at=AT - H, text=dg("h1"), status="ok", media="text/html")
    html_b = snap(2, B, role="html", text=dg("h1"), status="ok", media="text/html")
    for s_, rev in ((html_a, "v1"), (html_b, "v1")):
        s_["revision"] = rev
    assert repoint([html_a, html_b], mems, NEW, ("html",)) == NEW
    assert repoint([html_a, html_b], mems, OLD, ("html",)) == OLD
    short = snap(1, A, role="html", text=dg("h1"), status="ok", media="text/html")
    pdf = snap(2, A, at=AT - H, text=dg("p1"), status="ok")
    short["revision"] = pdf["revision"] = "v1"
    short["quality"], pdf["quality"] = {"metrics": {"chars": 10}}, {"metrics": {"chars": 100}}
    assert repoint([short, pdf], licensed((A, "open")), OLD, ("html",)) == NEW


def test_quality_judges_the_same_text_previous_current_too() -> None:
    """D807: the quality substage judges the kept previous snapshot as well as the order's first when both hold
    the same text, so ``current_text_quality`` reads this gate's verdict; an older, different text is not judged."""
    from workflows.acquire.quality import judged_snapshots

    a, b = snap(1, A, at=AT - H, text=dg("t1"), status="ok"), snap(2, B, text=dg("t1"), status="ok")
    row = {"content": {"snapshots": [a, b], "current_snapshot_id": OLD},
           "identity": {"members": licensed((A, "open"), (B, "open"))}}
    assert [x["snapshot_id"] for x in judged_snapshots(row, PIN)] == [NEW, OLD]
    row["content"]["snapshots"][1] = snap(2, B, text=dg("t2"), status="ok")
    assert [x["snapshot_id"] for x in judged_snapshots(row, PIN)] == [NEW]


# ---------------------------------------------------------------- carry (X21, §6)
def _with_member(row: Row, i: int) -> None:
    m = copy.deepcopy(row["identity"]["members"][0])
    m.update(source_id=sid(i), key_value=f"2610.{i:05d}", edge_kind="key:arxiv", edge_to_source_id=row["doc_id"])
    row["identity"]["members"].append(m)
    row["identity"]["member_ids"].append(sid(i))
    row["identity"]["members_digest"] = dg(("members", "merged", row["doc_id"]))


def test_regroup_after_merge_and_split(tmp_path: Path) -> None:
    """A merge: the survivor (members 1, 2; content stale) takes the absorbed row's entries
    through ``former_doc_ids``; the role both rows hold for member 2 is unioned, not summed. A
    split: a part takes only its member's entries. ``members_digest`` regroups (X21)."""
    surv, gone = doc(1, state="acquired"), doc(2, state="acquired")
    gone["state"], gone["identity"]["merged_into"] = "retired", surv["doc_id"]
    g_role = gone["content"]["roles"][0]
    gone["content"]["snapshots"][0]["snapshot_id"] = g_role["snapshot_id"] = f"{rid(2)}.0"
    s_role = {**copy.deepcopy(g_role), "status": "failed_transient", "error_class": "http_5xx", "attempts": 2,
              "first_attempt_at": AT - H, "tried_urls": ["u-new"], "snapshot_id": None}
    surv["content"]["roles"].append(s_role)  # the survivor's own (failed) state for member 2
    _with_member(surv, 2)
    surv["identity"]["former_doc_ids"] = [gone["doc_id"]]
    for r in (surv, gone):
        r["inband"]["entries"] = []
    lane = FakeLane.answering("arxiv", {}, default="transient")
    res, _ = go([surv, gone], tmp_path, {"arxiv": lane})
    (out,) = res.rows
    c = out["content"]
    assert c["members_digest"] == out["identity"]["members_digest"] and lane.calls == []
    assert {s["source_id"] for s in c["snapshots"]} == {sid(1), sid(2)}
    r2 = role(out, sid(2))
    assert r2["status"] == "ok" and r2["snapshot_id"] == f"{rid(2)}.0"  # ok wins
    assert [x for x in c["roles"] if x["source_id"] == sid(2)] == [r2]  # one role per key

    # Two failed states of one key: most attempts wins, not their sum; first/last and urls unioned.
    g_role.update(status="failed_transient", error_class="http_5xx", attempts=3,
                  first_attempt_at=AT - 5 * H, tried_urls=["u-old"])
    res, _ = go([surv, gone], tmp_path, {"arxiv": lane})
    r2 = role(res.rows[0], sid(2))
    assert (r2["attempts"], r2["first_attempt_at"], r2["tried_urls"]) == (3, AT - 5 * H, ["u-new", "u-old"])  # own urls first

    # Split: part 3 (member 2 only) takes member 2's entries from the retired row it lists.
    whole = doc(4, state="acquired")
    _with_member(whole, 5)
    whole["content"]["snapshots"].append({**whole["content"]["snapshots"][0], "snapshot_id": f"{rid(3)}.9",
                                          "source_id": sid(5), "raw": blob(dg("raw5"), 1234, "application/pdf")})
    whole["state"], whole["inband"]["entries"] = "retired", []
    part = doc(5, state="triaged")
    part["identity"]["former_doc_ids"] = [whole["doc_id"]]
    res, _ = go([whole, part], tmp_path, {"arxiv": FakeLane.answering("arxiv", {}, default="dead")})
    (p,) = res.rows
    assert {s["source_id"] for s in p["content"]["snapshots"]} == {sid(5)}
    assert p["content"]["current_snapshot_id"] == f"{rid(3)}.9"


# ---------------------------------------------------------------- accounting (D25, X24)
def test_attempts_backoff_cap(tmp_path: Path) -> None:
    rows = [doc(1)]
    lane = FakeLane.answering("arxiv", {}, default="transient")
    now, seen = NOW, []
    for k in range(1, 5):
        res, _ = go(rows, tmp_path, {"arxiv": lane}, now=now, run=50 + k)
        rows = merge(rows, res)
        r = role(rows[0], sid(1))
        seen.append(r["attempts"])
        if k < 4:
            assert (r["status"], r["error_class"]) == ("failed_transient", "http_5xx")
            # the first retry is due at once (round 4, passes), then 2h, 4h
            assert r["retry_after"] - now == (dt.timedelta(0) if k == 1 else H * 2 ** (k - 1))
            if k > 1:
                early, _ = go(rows, tmp_path, {"arxiv": lane}, now=r["retry_after"] - dt.timedelta(minutes=1))
                assert early.rows == [] and len(lane.calls) == k  # not due yet: no call, nothing written
            now = r["retry_after"]
        else:
            # R4-2: quarantined with the long (90 d) recheck, not forever
            assert (r["status"], r["error_class"], r["retry_after"]) == (
                "quarantined", "cap_reached", now + dt.timedelta(days=90))
    assert seen == [1, 2, 3, 4]  # monotone
    assert rows[0]["state"] == "acquired" and r["first_attempt_at"] == NOW
    later, _ = go(rows, tmp_path, {"arxiv": lane}, now=now + dt.timedelta(days=60))
    assert later.rows == [] and len(lane.calls) == 4  # quarantined: not due before the recheck
    recheck = now + dt.timedelta(days=91)
    res, _ = go(rows, tmp_path, {"arxiv": lane}, now=recheck, run=60)
    assert len(lane.calls) == 5  # the recheck asks once; still failing: quarantined again at once
    r = role(merge(rows, res)[0], sid(1))
    assert (r["status"], r["attempts"], r["retry_after"]) == ("quarantined", 5, recheck + dt.timedelta(days=90))


def test_rate_limits_back_off_but_never_quarantine(tmp_path: Path) -> None:
    """D484: four runs that each meet a rate limit (``failed_transient[rate_limit]``, a 1 h
    ``Retry-After``) leave the role ``failed_transient[rate_limit]`` with ``attempts`` 0, due at the
    hint, never ``quarantined``; ``last_attempt_at`` follows every episode. The fifth run fetches it."""
    rows = [doc(1)]
    lane = FakeLane.answering("arxiv", {(sid(1), "full_text"): ["rate_limit"] * 4 + [b"pdf"]})
    now = NOW
    for k in range(1, 5):
        res, e = go(rows, tmp_path, {"arxiv": lane}, now=now, run=50 + k)
        rows = merge(rows, res)
        r = role(rows[0], sid(1))
        assert (r["status"], r["error_class"], r["attempts"], r["last_attempt_at"], r["retry_after"]) == (
            "failed_transient", "rate_limit", 0, now, now + H), k
        assert "quarantined" not in e.counts and is_due(r, now + H) and not is_due(r, now + H / 2)
        now += H
    res, _ = go(rows, tmp_path, {"arxiv": lane}, now=now, run=55)
    r = role(merge(rows, res)[0], sid(1))
    assert len(lane.calls) == 5 and (r["status"], r["attempts"]) == ("ok", 0)


def test_mixed_failures_count_only_the_non_rate_limit_ones(tmp_path: Path) -> None:
    """D484: http_5xx, rate limit, rate limit, http_5xx, rate limit, http_5xx, http_5xx: the role is
    quarantined at the fourth counted failure (the seventh episode), not at the fourth episode;
    ``attempts`` counts 1, 1, 1, 2, 2, 3, 4. A rate limit after counted failures keeps their backoff
    (here 2 h after two) or the hint, whichever is later."""
    rows = [doc(1)]
    script = ["transient", "rate_limit", "rate_limit", "transient", "rate_limit", "transient", "transient"]
    lane = FakeLane.answering("arxiv", {(sid(1), "full_text"): script})
    now, seen = NOW, []
    for k in range(1, len(script) + 1):
        res, _ = go(rows, tmp_path, {"arxiv": lane}, now=now, run=50 + k)
        rows = merge(rows, res)
        r = role(rows[0], sid(1))
        seen.append((r["status"], r["attempts"]))
        if k == 5:  # a rate limit after two counted failures: backoff(2) = 2 h beats the 1 h hint
            assert r["retry_after"] == now + 2 * H
        now = max(now, r["retry_after"])
    assert seen == [("failed_transient", 1), ("failed_transient", 1), ("failed_transient", 1),
                    ("failed_transient", 2), ("failed_transient", 2), ("failed_transient", 3),
                    ("quarantined", 4)]
    assert (r["error_class"], r["error"].split(":")[0]) == ("cap_reached", "http_5xx")


def test_halted_lane_charges_no_row(tmp_path: Path) -> None:
    """A lane's ``halt_run`` halts that lane for the run (T3.6-f2): the halting role and the
    lane's later roles are not charged and read ``not_attempted[halted]``; their rows are written
    (not unreached); the run is incomplete and the next one fetches them."""
    rows = [doc(1), doc(2), doc(3)]
    lane = FakeLane.answering("arxiv", {(sid(1), "full_text"): b"pdf 1", (sid(2), "full_text"): ["429", b"pdf 2"]},
                              default=b"pdf")
    res, e = go(rows, tmp_path, {"arxiv": lane})
    assert lane.keys() == [(sid(1), "full_text"), (sid(2), "full_text")]  # stops at the 429
    assert not res.scope_complete and res.unreached == []
    assert (e.counts["halted"], e.counts["halted.arxiv"], e.counts["halted_roles"], e.counts["unreached"]) == (
        1, 1, 1, 0)
    by = {r["doc_id"]: r for r in res.rows}
    assert set(by) == {sid(1), sid(2), sid(3)}  # doc 3 written with its halted mark, not unreached
    for d in (sid(2), sid(3)):
        r = role(by[d], d)
        assert (r["status"], r["error_class"], r["attempts"], r["last_attempt_at"]) == (
            "not_attempted", "halted", 0, None)
        assert r["error"].endswith(f"/arxiv/{sid(2)}/full_text: 429")  # the halting outcome's error
    rows = merge(rows, res)
    res2, _ = go(rows, tmp_path, {"arxiv": lane}, run=51)
    assert res2.scope_complete and {r["doc_id"] for r in res2.rows} == {sid(2), sid(3)}
    assert all(role(r, r["doc_id"])["status"] == "ok" for r in res2.rows)


def test_halt_stops_only_its_own_lane(tmp_path: Path) -> None:
    """The T3.6 real run: arXiv's 429 on the first row must not starve the other lanes. Rows in
    ``doc_id`` order alternate ``arxiv`` and ``http`` (channel ``web``); the first ``arxiv`` role
    halts: every ``http`` role is still fetched (``ok``), later ``arxiv`` roles are not called and
    read ``not_attempted[halted]``, nothing is unreached, the scope is incomplete; the next run
    (no halt) fetches the ``arxiv`` roles and leaves the ``http`` ones (settled) alone."""
    rows = [doc(1), doc(2, channel="web"), doc(3), doc(4, channel="web")]
    arxiv = FakeLane.answering("arxiv", {(sid(1), "full_text"): ["429", b"pdf 1"]}, default=b"pdf")
    http = FakeLane.answering("http", {}, default="ok", media_type="text/html")
    res, e = go(rows, tmp_path, {"arxiv": arxiv, "http": http})
    assert arxiv.keys() == [(sid(1), "full_text")]
    assert http.keys() == [(sid(2), "page"), (sid(4), "page")]  # after the arxiv halt
    assert not res.scope_complete and res.unreached == []
    assert (e.counts["halted"], e.counts["halted.arxiv"], e.counts["stopped.halted"],
            e.counts["halted_roles"], e.counts["lane_calls.http"]) == (1, 1, 1, 1, 2)
    assert "halted.http" not in e.counts and "stopped.limit" not in e.counts
    by = {r["doc_id"]: r for r in res.rows}
    assert set(by) == {sid(1), sid(2), sid(3), sid(4)}
    for d in (sid(1), sid(3)):
        r = role(by[d], d)
        assert (r["status"], r["error_class"], r["attempts"], r["retry_after"]) == (
            "not_attempted", "halted", 0, None)
    for d in (sid(2), sid(4)):
        r = role(by[d], d, "page")
        assert (r["status"], r["attempts"], r["lane"]) == ("ok", 0, "http") and r["snapshot_id"]
    res2, e2 = go(merge(rows, res), tmp_path, {"arxiv": arxiv, "http": http}, run=51)
    assert res2.scope_complete and "halted" not in e2.counts
    assert arxiv.keys()[1:] == [(sid(1), "full_text"), (sid(3), "full_text")] and len(http.calls) == 2
    assert {r["doc_id"] for r in res2.rows} == {sid(1), sid(3)}
    assert all(role(r, r["doc_id"])["status"] == "ok" for r in res2.rows)


def test_max_rows_stop(tmp_path: Path) -> None:
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    res, e = go([doc(1), doc(2)], tmp_path, {"arxiv": lane}, max_rows=1)
    assert [r["doc_id"] for r in res.rows] == [sid(1)] and res.unreached == [sid(2)]
    assert not res.scope_complete and e.counts["stopped.limit"] == 1


def test_weekly_recheck_on_ok_page_role(tmp_path: Path) -> None:
    page, paper = doc(1, channel="web"), doc(2)
    http = FakeLane.answering("http", {}, default=b"<html>same</html>", media_type="text/html")
    arxiv = FakeLane.answering("arxiv", {}, default=b"pdf")
    res, _ = go([page, paper], tmp_path, {"http": http, "arxiv": arxiv})
    rows = merge([page, paper], res)
    pr = role(rows[0], sid(1), "page")
    assert (pr["status"], pr["last_attempt_at"], pr["retry_after"]) == ("ok", NOW, NOW + dt.timedelta(days=7))
    assert pr["lane"] == "http" and role(rows[1], sid(2))["retry_after"] is None  # group paper: no recheck
    res, e = go(rows, tmp_path, {"http": http, "arxiv": arxiv}, now=NOW + dt.timedelta(days=7), run=51)
    assert [c.source_id for c in http.calls] == [sid(1), sid(1)] and len(arxiv.calls) == 1
    (again,) = res.rows
    pr = role(again, sid(1), "page")
    assert pr["last_outcome"] == "unchanged" and e.counts["unchanged"] == 1
    assert len([s for s in again["content"]["snapshots"] if s["kind"] == "raw"]) == 1  # equal digest
    assert pr["retry_after"] == NOW + dt.timedelta(days=14)


def test_every_due_role_of_a_document_in_one_pass(tmp_path: Path) -> None:
    """§3.1 (scope "∪ roles due"), §3.5 (``wants`` lists several roles per channel) and §5.6
    (OpenAlex: the OA PDF and the ``xml``): one run calls the lane once per due role, every due
    role of the document (two roles of one member, plus the other member's role), not one role
    per run. The two members carry different identifiers (a publisher DOI, an arXiv id): members
    on one identifier fetch once (T3.6-f3a, ``test_one_fetch_per_paper_identifier``)."""
    row = doc(1, channel="openalex")
    other = {**copy.deepcopy(row["identity"]["members"][0]), "source_id": sid(9), "channel": "arxiv"}
    row["identity"]["members"][0].update(key_kind="doi", key_value="10.1000/journal.1")
    row["identity"]["members"].append(other)
    oa = FakeLane.answering("oa", {(sid(1), "xml"): b"<?xml version='1.0'?><article/>"}, default=b"%PDF-1.7 x")
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 y")
    res, e = go([row], tmp_path, {"oa": oa, "arxiv": arxiv},
                wants={"openalex": ["full_text", "xml"], "arxiv": ["full_text"]})
    assert sorted(oa.keys()) == [(sid(1), "full_text"), (sid(1), "xml")] and arxiv.keys() == [(sid(9), "full_text")]
    (out,) = res.rows
    assert {(r["source_id"], r["content_role"]): r["status"] for r in out["content"]["roles"]} == {
        (sid(1), "full_text"): "ok", (sid(1), "xml"): "ok", (sid(9), "full_text"): "ok"}
    assert e.counts["lane_calls"] == 3 and res.scope_complete


# ---------------------------------------------------------------- one fetch per paper identifier (T3.6-f3a)
ARXIV_DOI = "10.48550/arxiv.2609.36750"
PAPER_WANTS = {"arxiv": ["full_text"], "openalex": ["full_text", "xml"], "semantic_scholar": ["full_text"]}


def paper(i: int, *mems: tuple[int, str, str, str]) -> Row:
    """A triaged row whose members are ``(source i, channel, key_kind, key_value)``."""
    row = doc(i)
    base = row["identity"]["members"][0]
    row["identity"]["members"] = [{**copy.deepcopy(base), "source_id": sid(n), "channel": ch, "key_kind": kk,
                                   "key_value": kv} for n, ch, kk, kv in mems]
    row["identity"]["member_ids"] = [sid(n) for n, *_ in mems]
    return row


def states(row: Row) -> dict[tuple[str, str], tuple[str, str | None, str]]:
    return {(r["source_id"], r["content_role"]): (r["status"], r["error_class"], r["lane"])
            for r in row["content"]["roles"]}


def test_fetch_key_normalises_the_identifier() -> None:
    assert fetch_key({"key_kind": "doi", "key_value": "10.48550/ARXIV.2609.36750"}) == "arxiv:2609.36750"
    assert fetch_key({"key_kind": "arxiv", "key_value": "2609.36750v3"}) == "arxiv:2609.36750"
    assert fetch_key({"key_kind": "doi", "key_value": "10.1000/Journal.X"}) == "doi:10.1000/journal.x"
    assert fetch_key({"key_kind": "pmid", "key_value": "123"}) == "pmid:123"
    for kk, kv in (("openalex", "W1"), ("s2", "abc"), ("url", "https://x.test/"), ("fm_post", "p1")):
        assert fetch_key({"key_kind": kk, "key_value": kv}) is None


def test_one_fetch_per_paper_identifier(tmp_path: Path) -> None:
    """(a) arXiv, OpenAlex and Semantic Scholar members on one arXiv DOI: one lane call (the arXiv
    member's PDF); the other ``full_text`` roles and the OpenAlex ``xml`` read ``skipped
    [not_needed]``, uncharged and never due; the arXiv-keyed paper members' roles are on the
    ``arxiv`` lane (D11 amended), the OA lane is never called. (b) The arXiv PDF ends dead: the
    other members' roles are on the same lane and identifier, so the next run calls nothing and
    they stay ``skipped[not_needed]`` naming the dead role (T3.x-f4; D165b re-opened them one
    member per run, each repeating the same dead request)."""
    a, o, s2 = sid(1), sid(2), sid(3)
    row = paper(1, (1, "arxiv", "doi", ARXIV_DOI), (2, "openalex", "doi", ARXIV_DOI),
                (3, "semantic_scholar", "doi", ARXIV_DOI.upper()))
    arxiv = FakeLane.answering("arxiv", {(a, "full_text"): "dead", (o, "full_text"): "dead"}, default=b"%PDF-1.7 a")
    oa = FakeLane.answering("oa", {}, default=b"%PDF-1.7 o")
    res, e = go([row], tmp_path, {"arxiv": arxiv, "oa": oa}, wants=PAPER_WANTS)
    assert arxiv.keys() == [(a, "full_text")] and oa.keys() == [] and e.counts["lane_calls"] == 1
    (out,) = res.rows
    assert states(out) == {(a, "full_text"): ("failed_permanent", "dead", "arxiv"),
                           (o, "full_text"): ("skipped", "not_needed", "arxiv"),
                           (o, "xml"): ("skipped", "not_needed", "arxiv"),
                           (s2, "full_text"): ("skipped", "not_needed", "arxiv")}
    assert role(out, o)["error"] == f"same arxiv:2609.36750 as {a} (arxiv)"
    assert role(out, o, "xml")["error"] == "arXiv serves no JATS XML"
    skipped = [r for r in out["content"]["roles"] if r["error_class"] == "not_needed"]
    assert all(r["attempts"] == 0 and not is_due(r, NOW + 30 * 24 * H) for r in skipped)
    assert (e.counts["carry.same_key_skipped"], e.counts["carry.arxiv_xml_skipped"]) == (2, 1)

    rows = merge([row], res)
    res, e = go(rows, tmp_path, {"arxiv": arxiv, "oa": oa}, wants=PAPER_WANTS, now=NOW + H, run=51)
    assert arxiv.keys() == [(a, "full_text")] and oa.keys() == [] and e.counts.get("lane_calls", 0) == 0
    (out,) = res.rows
    dead = f"same arxiv:2609.36750 as {a} (arxiv), which ended failed_permanent[dead] in lane arxiv"
    for m in (o, s2):
        assert states(out)[(m, "full_text")] == ("skipped", "not_needed", "arxiv")
        assert role(out, m)["error"] == dead and role(out, m)["attempts"] == 0
    assert states(out)[(o, "xml")] == ("skipped", "not_needed", "arxiv")
    assert e.counts["carry.same_key_lane_terminal"] == 2 and "carry.same_key_reopened" not in e.counts

    rows = merge(rows, res)
    res, e = go(rows, tmp_path, {"arxiv": arxiv, "oa": oa}, wants=PAPER_WANTS, now=NOW + 2 * H, run=52)
    assert arxiv.keys() == [(a, "full_text")] and e.counts.get("lane_calls", 0) == 0


def held_paper(revs: tuple[str, ...] = ("v1", "v2"), held: str = "v2", *, oa_live: bool = True,
               retry_after: dt.datetime | None = NOW + 72 * H) -> Row:
    """An OpenAlex member (source 1) whose arXiv PDF role is ``ok`` holding ``held``, and an arXiv member
    (source 2, sorts first) that a merge just added, with no role yet; ``content.revisions`` lists ``revs``."""
    row = paper(1, (1, "openalex", "doi", ARXIV_DOI), (2, "arxiv", "arxiv", "2609.36750"))
    row["identity"]["members"][0]["is_live"] = oa_live
    c = new_content(row["identity"]["members_digest"], PIN)
    s_ = snap(1, sid(1), text=dg("t1"), status="ok")
    s_["revision"] = held
    c["snapshots"] = [s_]
    c["roles"] = [{**new_role("full_text", sid(1), status="ok", error_class=None, lane="arxiv"),
                   "attempts": 1, "last_attempt_at": AT, "retry_after": retry_after, "snapshot_id": s_["snapshot_id"]}]
    c["revisions"] = [{"revision": r, "published_at": AT, "title": None} for r in revs]
    row["content"] = c
    return row


def carried_states(row: Row) -> tuple[dict[tuple[str, str], tuple[str, str | None, str]], dict[str, int]]:
    counts: dict[str, int] = {}
    c = carry(row, {row["doc_id"]: row}, wants=PAPER_WANTS, channel_group=GROUPS.get, permit=FETCH, pin=PIN,
              counts=counts)
    return states({"content": c}), counts


def test_a_held_arxiv_version_keeps_its_fetch_role() -> None:
    """D807: the ``ok`` role that holds the newest listed version (any held version when nothing is listed) is the
    group's fetch role, so the arXiv member a merge adds (first in the order) is ``skipped[not_needed]`` instead
    of fetching the same version again; also while the held role is due for its recheck (its own call asks arXiv
    for the current version, not the newcomer's)."""
    for due, revs in ((NOW + 72 * H, ("v1", "v2")), (NOW - H, ("v1", "v2")), (NOW - H, ())):
        st, counts = carried_states(held_paper(revs, retry_after=due))  # nothing listed: the held version
        assert st[(sid(1), "full_text")] == ("ok", None, "arxiv")
        assert st[(sid(2), "full_text")] == ("skipped", "not_needed", "arxiv")
        assert counts["carry.same_key_held"] == 1 and counts["carry.same_key_skipped"] == 1


@pytest.mark.parametrize("label,kw", [
    ("a newer version is listed but not held", {"revs": ("v1", "v2", "v3")}),
    ("only an older version is held", {"held": "v1"}),
    ("the held role's member is no longer live", {"oa_live": False}),
])
def test_without_the_held_version_the_order_decides(label: str, kw: dict[str, Any]) -> None:
    """D807 applies only to the version the lane would fetch, held by a live member; otherwise the arXiv member
    leads and is fetched, as before."""
    st, counts = carried_states(held_paper(**kw))
    assert st[(sid(2), "full_text")] == ("not_attempted", "limit", "arxiv"), label
    assert "carry.same_key_held" not in counts


def test_a_held_arxiv_version_makes_no_lane_call(tmp_path: Path) -> None:
    """Through ``acquire_rows``: the newcomer is not fetched and the pointer stays on the held snapshot."""
    row = held_paper()
    row["content"]["current_snapshot_id"] = row["content"]["snapshots"][0]["snapshot_id"]
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 again")
    res, e = go([row], tmp_path, {"arxiv": arxiv}, wants=PAPER_WANTS)
    assert arxiv.keys() == [] and e.counts.get("lane_calls", 0) == 0 and e.counts["carry.same_key_held"] == 1
    (out,) = res.rows
    assert states(out)[(sid(2), "full_text")] == ("skipped", "not_needed", "arxiv")
    assert out["content"]["current_source_id"] == sid(1)


def test_a_skipped_same_key_role_reopens_when_its_group_loses_the_leader(tmp_path: Path) -> None:
    """OpenAlex + Semantic Scholar on one DOI: the OpenAlex member's call fails transiently, S2 stays
    ``skipped[not_needed]``; the OpenAlex member is withdrawn from the document (no longer live), so
    the next run re-opens S2's role and fetches it (the same lane, but nothing ended terminal)."""
    row = paper(1, (1, "openalex", "doi", "10.1000/j.1"), (2, "semantic_scholar", "doi", "10.1000/j.1"))
    oa = FakeLane.answering("oa", {(sid(1), "full_text"): "transient"}, default=b"%PDF-1.7 o")
    wants = {"openalex": ["full_text"], "semantic_scholar": ["full_text"]}
    res, e = go([row], tmp_path, {"oa": oa}, wants=wants)
    assert oa.keys() == [(sid(1), "full_text")]
    assert states(res.rows[0])[(sid(2), "full_text")] == ("skipped", "not_needed", "oa")
    rows = merge([row], res)
    rows[0]["identity"]["members"][0]["is_live"] = False
    res, e = go(rows, tmp_path, {"oa": oa}, wants=wants, now=NOW + 3 * H, run=51)
    assert oa.keys()[1:] == [(sid(2), "full_text")] and e.counts["carry.same_key_reopened"] == 1
    assert states(res.rows[0])[(sid(2), "full_text")] == ("ok", None, "oa")


def test_one_oa_retrieval_per_publisher_doi(tmp_path: Path) -> None:
    """(c) OpenAlex + Semantic Scholar on one publisher DOI (case differs): one OA call, for the
    OpenAlex member; (d) members on different DOIs are not grouped: one call each."""
    one = paper(1, (1, "openalex", "doi", "10.1000/Journal.1"), (2, "semantic_scholar", "doi", "10.1000/journal.1"))
    two = paper(3, (3, "openalex", "doi", "10.1000/journal.3"), (4, "semantic_scholar", "doi", "10.1000/journal.4"))
    oa = FakeLane.answering("oa", {}, default=b"%PDF-1.7 o")
    wants = {"openalex": ["full_text"], "semantic_scholar": ["full_text"]}
    res, e = go([one, two], tmp_path, {"oa": oa}, wants=wants)
    assert oa.keys() == [(sid(1), "full_text"), (sid(3), "full_text"), (sid(4), "full_text")]
    by = {r["doc_id"]: r for r in res.rows}
    assert states(by[sid(1)]) == {(sid(1), "full_text"): ("ok", None, "oa"),
                                  (sid(2), "full_text"): ("skipped", "not_needed", "oa")}
    assert role(by[sid(1)], sid(2))["error"] == f"same doi:10.1000/journal.1 as {sid(1)} (openalex)"
    assert {v[0] for v in states(by[sid(3)]).values()} == {"ok"}
    assert e.counts["carry.same_key_skipped"] == 1


def test_arxiv_doi_of_a_paper_member_goes_to_the_arxiv_lane(tmp_path: Path) -> None:
    """D11 amended (T3.6-f3a): an OpenAlex-only paper whose DOI is an arXiv DOI fetches its PDF on
    the ``arxiv`` lane (the OA route answered arXiv's abstract landing page); its ``xml`` role is
    skipped in carry, no lane call."""
    row = paper(1, (1, "openalex", "doi", ARXIV_DOI))
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    oa = FakeLane.answering("oa", {}, default=b"%PDF-1.7 o")
    res, e = go([row], tmp_path, {"arxiv": arxiv, "oa": oa}, wants=PAPER_WANTS)
    assert arxiv.keys() == [(sid(1), "full_text")] and oa.keys() == []
    assert states(res.rows[0]) == {(sid(1), "full_text"): ("ok", None, "arxiv"),
                                   (sid(1), "xml"): ("skipped", "not_needed", "arxiv")}
    assert "carry.relaned" not in e.counts  # born on the arxiv lane (lane_for), not re-laned
    assert lanes.lane_for("openalex", "paper", "full_text", key_kind="doi", key_value=ARXIV_DOI) == "arxiv"
    assert lanes.lane_for("semantic_scholar", "paper", "full_text", key_kind="arxiv", key_value="2609.36750") == "arxiv"
    assert lanes.lane_for("openalex", "paper", "full_text", key_kind="doi", key_value="10.1000/j.1") == "oa"
    assert lanes.lane_for("openalex", "paper", "full_text") == "oa"
    assert lanes.lane_for("web", "url", "page", key_kind="doi", key_value=ARXIV_DOI) == "http"


def test_carry_relanes_unprogressed_arxiv_keyed_roles() -> None:
    """Roles created before the D11 amendment: an arXiv-keyed paper member's ``not_attempted``
    role on ``oa`` moves to ``arxiv``; one with progress (``ok``) keeps its lane; a publisher-DOI
    member's stays on ``oa``."""
    row = paper(1, (1, "openalex", "doi", ARXIV_DOI), (2, "openalex", "doi", "10.48550/arxiv.2609.00002"),
                (3, "openalex", "doi", "10.1000/journal.3"))
    row["content"] = new_content(row["identity"]["members_digest"], PIN)
    row["content"]["roles"] = [
        new_role("full_text", sid(1), status="not_attempted", error_class="limit", lane="oa"),
        {**new_role("full_text", sid(2), status="ok", error_class=None, lane="oa"), "attempts": 0},
        new_role("full_text", sid(3), status="not_attempted", error_class="limit", lane="oa")]
    counts: dict[str, int] = {}
    c = carry(row, {row["doc_id"]: row}, wants={"openalex": ["full_text"]}, channel_group=GROUPS.get,
              permit=FETCH, pin=PIN, counts=counts)
    assert states({"content": c}) == {(sid(1), "full_text"): ("not_attempted", "limit", "arxiv"),
                                      (sid(2), "full_text"): ("ok", None, "oa"),
                                      (sid(3), "full_text"): ("not_attempted", "limit", "oa")}
    assert counts["carry.relaned"] == 1


HTML_ON = {"structured": {"arxiv_html": True}}
STD_V3_WANTS = {"arxiv": ["full_text", "html"], "openalex": ["full_text", "xml"], "semantic_scholar": ["full_text"]}


def go_with(rows: list[Row], tmp_path: Path, lane_map: dict[str, FakeLane], options: dict[str, Any],
            **kw: Any) -> tuple[AcquireResult, LaneEnv]:
    """:func:`go` with the acquire line's ``options`` on the run's env."""
    e = LaneEnv(run_id=rid(50), now=NOW, blobs=open_blobs(tmp_path), convert_pin=PIN, counts={},
                options=options)
    with lanes.using(lane_map):
        res = acquire_rows(rows, e, wants=kw.pop("wants", STD_V3_WANTS), channel_group=GROUPS.get, **kw)
    return res, e


def test_every_arxiv_id_wants_html_whichever_channel_found_it(tmp_path: Path) -> None:
    """R6 item 2 (std-v3's ``wants``: ``html`` only under ``arxiv``): with ``structured.arxiv_html``
    an OpenAlex-only paper on an arXiv DOI and an S2-only paper on an ``arxiv`` key each get one
    ``html`` role on the arXiv lane, fetched once; a publisher-DOI paper gets none; a withdrawn
    arXiv-keyed member's is ``skipped[withdrawn]``; on a row triage does not let fetch it is
    ``skipped[gated]``. With the option off nothing changes: no ``html`` role at all."""
    oa_only = paper(1, (1, "openalex", "doi", ARXIV_DOI))
    s2_only = paper(2, (2, "semantic_scholar", "arxiv", "2609.00002v2"))
    publisher = with_keys(paper(3, (3, "openalex", "doi", "10.1000/journal.3")))  # no arXiv id anywhere
    withdrawn = paper(4, (4, "openalex", "doi", "10.48550/arxiv.2609.00004"))
    withdrawn["identity"]["members"][0]["withdrawn_at"] = AT
    gated = paper(5, (5, "openalex", "doi", "10.48550/arxiv.2609.00005"))
    gated["annotations"]["triage"]["payload"]["decision"] = "skip"
    rows = [oa_only, s2_only, publisher, withdrawn, gated]
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    oa = FakeLane.answering("oa", {}, default=b"%PDF-1.7 o")
    res, e = go_with(copy.deepcopy(rows), tmp_path, {"arxiv": arxiv, "oa": oa}, HTML_ON)
    assert sorted(k for k in arxiv.keys() if k[1] == "html") == [(sid(1), "html"), (sid(2), "html")]
    by = {r["doc_id"]: states(r) for r in res.rows}
    assert by[sid(1)][(sid(1), "html")] == ("ok", None, "arxiv")
    assert by[sid(2)][(sid(2), "html")] == ("ok", None, "arxiv")
    assert not any(k[1] == "html" for k in by[sid(3)])
    assert by[sid(4)][(sid(4), "html")] == ("skipped", "withdrawn", "arxiv")
    assert by[sid(5)][(sid(5), "html")] == ("skipped", "gated", "arxiv")
    assert e.counts["carry.arxiv_html_added"] == 4

    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    res, e = go_with(copy.deepcopy(rows), tmp_path / "off", {"arxiv": arxiv, "oa": oa}, {})
    assert not any(k[1] == "html" for r in res.rows for k in states(r))
    assert not any(k[1] == "html" for k in arxiv.keys()) and "carry.arxiv_html_added" not in e.counts


def test_one_html_fetch_per_arxiv_id(tmp_path: Path) -> None:
    """A paper found by arXiv, OpenAlex and S2 (one arXiv id): every member wants ``html``, the arXiv
    member's role is the one fetch, the others ``skipped[not_needed]`` naming it; never two ``html``
    fetches of one id. A second run calls nothing."""
    a, o, s2 = sid(1), sid(2), sid(3)
    row = paper(1, (1, "arxiv", "arxiv", "2609.36750"), (2, "openalex", "doi", ARXIV_DOI),
                (3, "semantic_scholar", "doi", ARXIV_DOI.upper()))
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    res, e = go_with([row], tmp_path, {"arxiv": arxiv}, HTML_ON)
    assert arxiv.keys() == [(a, "full_text"), (a, "html")]
    (out,) = res.rows
    st = states(out)
    assert st[(a, "html")] == ("ok", None, "arxiv")
    assert st[(o, "html")] == st[(s2, "html")] == ("skipped", "not_needed", "arxiv")
    assert role(out, o, "html")["error"] == f"same arxiv:2609.36750 as {a} (arxiv)"
    assert e.counts["carry.arxiv_html_added"] == 2  # the arXiv member's html comes from its channel's wants
    res, e = go_with(merge([row], res), tmp_path, {"arxiv": arxiv}, HTML_ON)
    assert arxiv.keys() == [(a, "full_text"), (a, "html")] and e.counts.get("lane_calls", 0) == 0


def with_keys(row: Row, *keys: tuple[int, str, str]) -> Row:
    """The row's ``identity.keys``: the members' own keys plus ``(source i, kind, value)`` (a key adder's)."""
    own = [{"kind": m["key_kind"], "value": m["key_value"], "source_id": m["source_id"]}
           for m in row["identity"]["members"]]
    row["identity"]["keys"] = [*own, *({"kind": k, "value": v, "source_id": sid(n)} for n, k, v in keys)]
    return row


def test_an_arxiv_id_only_in_identity_keys_wants_one_html(tmp_path: Path) -> None:
    """R6 item 2b: an OpenAlex paper joined by a publisher DOI whose arXiv id is only in ``identity.keys``
    (OpenAlex's arXiv location) gets one ``html`` role on the arXiv lane; the lane's request carries the
    id (``KEY_ARXIV_ID``); its ``full_text`` stays on ``oa``. Two members holding the same id get one role
    (the OpenAlex one, by channel id). A paper whose S2 member's key already carries the id gets no second
    ``html`` role on the OpenAlex member. Option off: no ``html`` role."""
    one = with_keys(paper(1, (1, "openalex", "doi", "10.1000/j.1")), (1, "arxiv", "2609.00011"))
    two = with_keys(paper(2, (2, "semantic_scholar", "doi", "10.1000/j.2"), (3, "openalex", "doi", "10.1000/j.2")),
                    (2, "arxiv", "2609.00012"), (3, "doi", "10.48550/arXiv.2609.00012"))
    keyed = with_keys(paper(4, (4, "openalex", "doi", "10.1000/j.4"), (5, "semantic_scholar", "arxiv", "2609.00014")),
                      (4, "arxiv", "2609.00014"))
    rows = [one, two, keyed]
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    oa = FakeLane.answering("oa", {}, default=b"%PDF-1.7 o")
    res, e = go_with(copy.deepcopy(rows), tmp_path, {"arxiv": arxiv, "oa": oa}, HTML_ON)
    html_calls = [c for c in arxiv.calls if c.content_role == "html"]
    assert sorted(c.source_id for c in html_calls) == [sid(1), sid(3), sid(5)]
    assert {c.source_id: arxiv_id_of(c.member) for c in html_calls} == {
        sid(1): "2609.00011", sid(3): "2609.00012", sid(5): "2609.00014"}
    by = {r["doc_id"]: states(r) for r in res.rows}
    assert by[sid(1)] == {(sid(1), "full_text"): ("ok", None, "oa"), (sid(1), "xml"): ("ok", None, "oa"),
                          (sid(1), "html"): ("ok", None, "arxiv")}
    assert [k for k in by[sid(2)] if k[1] == "html"] == [(sid(3), "html")]
    assert [k for k in by[sid(4)] if k[1] == "html"] == [(sid(5), "html")]
    assert e.counts["carry.arxiv_html_from_keys"] == 2

    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    res, _ = go_with(copy.deepcopy(rows), tmp_path / "off", {"arxiv": arxiv, "oa": oa}, {})
    assert not any(k[1] == "html" for r in res.rows for k in states(r))


def test_a_publisher_doi_member_fetches_the_html_of_its_documents_arxiv_key(tmp_path: Path) -> None:
    """D580 (the 2026-10 fetch smoke): a paper joined by an ACM DOI with dotted digits
    (``10.1145/3847353.3847499``, once read as arXiv id ``7353.38474``) whose arXiv id is in
    ``identity.keys`` asks the arXiv lane for that id's HTML; a DOI member with no arXiv key in the
    document gets no ``html`` role."""
    keyed = with_keys(paper(1, (1, "openalex", "doi", "10.1145/3847353.3847499")), (1, "arxiv", "2609.00011"))
    bare = with_keys(paper(2, (2, "openalex", "doi", "10.1109/TPAMI.2025.3561234")))
    arxiv = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 a")
    oa = FakeLane.answering("oa", {}, default=b"%PDF-1.7 o")
    res, _ = go_with([keyed, bare], tmp_path, {"arxiv": arxiv, "oa": oa}, HTML_ON)
    html_calls = [c for c in arxiv.calls if c.content_role == "html"]
    assert {c.source_id: arxiv_id_of(c.member) for c in html_calls} == {sid(1): "2609.00011"}
    assert not any(k[1] == "html" for k in states(next(r for r in res.rows if r["doc_id"] == sid(2))))


def test_a_keys_only_html_role_groups_with_a_later_member_key() -> None:
    """The keys-only ``html`` role groups under ``arxiv:<id>``: an arXiv member carrying the id joins
    while the keys-only role is still ``not_attempted``; the arXiv member's role leads (``arxiv`` channel
    first) and the keys-only one becomes ``skipped[not_needed]``: one fetch for the id. (A role that
    already made progress keeps it, as for every role.)"""
    row = with_keys(paper(1, (1, "openalex", "doi", "10.1000/j.1"), (9, "arxiv", "arxiv", "2609.00011")),
                    (1, "arxiv", "2609.00011"))
    row["content"] = new_content(row["identity"]["members_digest"], PIN)
    row["content"]["roles"] = [new_role("html", sid(1), status="not_attempted", error_class="limit", lane="arxiv")]
    c = carry(row, {row["doc_id"]: row}, wants={"arxiv": ["full_text", "html"], "openalex": ["full_text"]},
              channel_group=GROUPS.get, permit=FETCH, pin=PIN, arxiv_html=True)
    st = states({"content": c})
    assert st[(sid(9), "html")] == ("not_attempted", "limit", "arxiv")
    assert st[(sid(1), "html")] == ("skipped", "not_needed", "arxiv")


def test_same_key_rule_leaves_a_gated_row_alone() -> None:
    """A row triage does not let fetch keeps today's behaviour: every wanted role
    ``skipped[gated]``, none ``not_needed``."""
    row = paper(1, (1, "arxiv", "doi", ARXIV_DOI), (2, "openalex", "doi", ARXIV_DOI))
    c = carry(row, {row["doc_id"]: row}, wants=PAPER_WANTS, channel_group=GROUPS.get, permit="skip", pin=PIN)
    assert {v[:2] for v in states({"content": c}).values()} == {("skipped", "gated")}


@pytest.mark.slow
def test_same_key_skips_pass_the_checks(tmp_path: Path) -> None:
    """Through ``run_stage``: the (a) world's ``skipped[not_needed]`` roles pass S4 (status
    pairing), V-content, attempts_monotone and block ownership."""
    root = tmp_path / "store"
    row = paper(1, (1, "arxiv", "doi", ARXIV_DOI), (2, "openalex", "doi", ARXIV_DOI),
                (3, "semantic_scholar", "doi", ARXIV_DOI))
    write_on_head(root, "documents", "documents", [row], stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband"])
    plan = Plan.model_validate({"plan": "test-acquire", "schema_version": 2,
                                "acquire": {"convert": {"config": "pymupdf4llm-v1"}, "wants": PAPER_WANTS},
                                "stages": [{"stage": "acquire", "options": {"allow_degraded_convert": True}}]})
    arxiv = FakeLane.answering("arxiv", {}, default=b"pdf")
    with lanes.using({"arxiv": arxiv, "oa": FakeLane.answering("oa", {})}, now=NOW):
        rec = run_stage(root, plan, "acquire", [sdir("documents")], series="main")
    assert rec.status == "ok", rec.error
    assert rec.counts["lane_calls"] == 1 and rec.counts["carry.same_key_skipped"] == 2
    c = view_of(root)[sid(1)]["content"]
    assert sorted(r["error_class"] or "" for r in c["roles"]) == ["", "not_needed", "not_needed", "not_needed"]
    by = {r.check: r for r in run_checks(root, names=["status_pairing", "V-content", "attempts_monotone",
                                                       "block_ownership"])}
    assert {n: r.status for n, r in by.items()} == dict.fromkeys(by, "ok"), {n: r.items for n, r in by.items()}


# ---------------------------------------------------------------- through the runner
@pytest.mark.slow
def test_part_file_holds_only_touched_rows(tmp_path: Path) -> None:
    """Through ``run_stage``: a gather full file with a row to fetch (1), an acquired row with
    nothing to do (2) and an identified row (3, out of scope). The acquire part holds row 1
    alone, ``blocks_written = ['content']``; V6, V11, X15 stay green."""
    root = tmp_path / "store"
    rows = [doc(1), document_row(2, state="acquired"), document_row(3)]
    rows[1]["inband"]["entries"] = []
    gather = write_on_head(root, "documents", "documents", rows, stage="gather", run=1, kind="full",
                           blocks_written=["identity", "metadata", "inband"])
    plan = Plan.model_validate({"plan": "test-acquire", "schema_version": 2,
                                "acquire": {"convert": {"config": "pymupdf4llm-v1"}, "wants": WANTS},
                                "stages": [{"stage": "acquire", "options": {"allow_degraded_convert": True}}]})
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    with lanes.using({"arxiv": lane}, now=NOW):
        rec = run_stage(root, plan, "acquire", [sdir("documents")], series="main", kind="part")
    assert rec.status == "ok", rec.error
    assert rec.counts["rows_in_scope"] == 2 and rec.counts["out_of_scope"] == 1
    path = root / rec.outputs["documents"]["path"]
    h = read_header(path)
    assert (h["kind"], h["blocks_written"], h["base"]) == ("part", '["content"]', gather.path.name)
    view = {r["doc_id"]: r for r in read_view(resolve(root, sdir("documents"))).to_pylist()}
    assert rec.version["rows_touched"] == 1 and int(h["row_count"]) == 1
    assert view[sid(1)]["run_id"] == rec.run_id and view[sid(2)]["run_id"] == rid(1)
    # b"pdf" opens in no PDF tier: failed_permanent[parse_error] at the pin (T3.5), no older text
    assert view[sid(1)]["content"]["current_text_status"] == "convert_failed"
    by = {r.check: r for r in run_checks(root, names=["V-content", "V-chain-linear", "block_ownership",
                                                       "attempts_monotone", "created_preserved"])}
    assert {n: r.status for n, r in by.items()} == dict.fromkeys(by, "ok"), {n: r.items for n, r in by.items()}


# ---------------------------------------------------------------- the run's filter, wants by channel (T3.6-f1)
REPO = Path(__file__).resolve().parents[1]
DESIGN_ONLY_WANTS = {"blogs", "repos", "web"}  # design names in wants that are no channels.yaml id


def _filter_world(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    write_on_head(root, "documents", "documents", [doc(i) for i in (1, 2, 3, 4)], stage="gather", run=1,
                  kind="full", blocks_written=["identity", "metadata", "inband"])
    return root


def _part_ids(root: Path, rec: Any) -> list[str]:
    return pq.read_table(root / rec.outputs["documents"]["path"], columns=["doc_id"]).column("doc_id").to_pylist()


@pytest.mark.slow
def test_filter_narrows_the_scope(tmp_path: Path) -> None:
    """``--filter`` (§3.3): the rows it rejects are not in scope — no lane call, not in the part
    file — and are counted ``scope.filtered_out``; the run record keeps the predicate and the
    filtered scope completes. An unfiltered run then fetches the rest."""
    root = _filter_world(tmp_path)
    plan = Plan.model_validate({"plan": "test-acquire", "schema_version": 2, "acquire": {"wants": WANTS},
                                "stages": [{"stage": "acquire"}]})
    pred = f"doc_id IN ('{sid(1)}', '{sid(3)}')"
    # distinct bodies: equal ones would list each other in same_raw_doc_ids (C29) and re-touch rows 1, 3
    lane = FakeLane.answering("arxiv", {(sid(i), "full_text"): f"pdf {i}".encode() for i in (1, 2, 3, 4)})
    with lanes.using({"arxiv": lane}, now=NOW):
        rec = run_stage(root, plan, "acquire", [sdir("documents")], series="main", kind="part", filter=pred)
    assert rec.status == "ok", rec.error
    assert lane.keys() == [(sid(1), "full_text"), (sid(3), "full_text")]
    assert _part_ids(root, rec) == [sid(1), sid(3)]
    assert rec.filter == pred and rec.scope_complete
    assert (rec.counts["scope.filtered_out"], rec.counts["rows_in_scope"], rec.counts["lane_calls"]) == (2, 2, 2)
    with lanes.using({"arxiv": lane}, now=NOW + H):
        rec2 = run_stage(root, plan, "acquire", [sdir("documents")], series="main", kind="part")
    assert rec2.status == "ok", rec2.error
    assert lane.keys()[2:] == [(sid(2), "full_text"), (sid(4), "full_text")]
    assert _part_ids(root, rec2) == [sid(2), sid(4)] and rec2.filter is None
    assert "scope.filtered_out" not in rec2.counts and rec2.scope_complete
    assert {d: r["state"] for d, r in view_of(root).items()} == dict.fromkeys([sid(i) for i in (1, 2, 3, 4)],
                                                                             "acquired")


def test_filter_that_does_not_evaluate_is_refused(tmp_path: Path) -> None:
    root = _filter_world(tmp_path)
    plan = Plan.model_validate({"plan": "test-acquire", "schema_version": 2, "acquire": {"wants": WANTS},
                                "stages": [{"stage": "acquire"}]})
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    with lanes.using({"arxiv": lane}, now=NOW):
        rec = run_stage(root, plan, "acquire", [sdir("documents")], series="main", filter="no_such_column = 1")
    assert rec.status == "failed" and "StageError" in rec.error
    assert "--filter 'no_such_column = 1' does not evaluate" in rec.error
    assert lane.calls == [] and rec.outputs == {}


def view_of(root: Path) -> dict[str, Row]:
    return {r["doc_id"]: r for r in read_view(resolve(root, sdir("documents"))).to_pylist()}


def test_carry_counts_live_members_without_wants(tmp_path: Path) -> None:
    """A live member of a row triage lets fetch whose channel has no ``wants`` key and no in-band
    entry gets no role: ``carry.no_wants`` counts it (not a wanted channel, not one with an
    in-band entry, not a row triage does not let fetch)."""
    wanted, bare, with_inband, not_fetched = doc(1), doc(2, channel="lesswrong"), doc(3, channel="lesswrong"), \
        doc(4, channel="lesswrong")
    with_inband["inband"]["entries"] = [{"source_id": sid(3), "content_role": "abstract", "raw": None,
                                         "text": blob(dg(("abstract", 3)), 120, "text/plain"),
                                         "captured_at": AT}]
    not_fetched["annotations"]["triage"]["payload"]["decision"] = "skip"
    res, e = go([wanted, bare, with_inband, not_fetched], tmp_path,
                {"arxiv": FakeLane.answering("arxiv", {}, default=b"pdf")})
    assert e.counts["carry.no_wants"] == 1
    by = {r["doc_id"]: r for r in res.rows}
    assert by[sid(2)]["content"]["roles"] == [] and by[sid(2)]["content"]["current_text_status"] == "no_raw"


def test_dev_plan_wants_is_keyed_by_channels() -> None:
    """D11: ``acquire.wants`` is keyed by channel id. Every key of dev-v2's is a channel of
    ``configs/channels.yaml`` (or a design-only name), and every ``url`` channel of kind scrape or
    watch has an entry (``card_dates`` is a date lookup, not a document channel)."""
    wants = load_plan(REPO / "configs" / "plans" / "dev-v2.yaml").acquire.wants
    channels = {c["id"]: c for c in yaml.safe_load((REPO / "configs" / "channels.yaml").read_text())["channels"]}
    assert set(wants) - set(channels) <= DESIGN_ONLY_WANTS, sorted(set(wants) - set(channels) - DESIGN_ONLY_WANTS)
    pages = {c for c, e in channels.items()
             if e["channel_group"] == "url" and e["kind"] in ("scrape", "watch") and c != "card_dates"}
    assert pages, "no url page channels"
    assert sorted(pages - set(wants)) == [], "url channels without wants"
    for c in ("semantic_scholar", "europepmc", "alignmentforum", "arxiv", "openalex", "lesswrong"):
        assert wants.get(c), c


# ---------------------------------------------------------------- order and options (D391, e2e-fix)
DAY = dt.timedelta(days=1)


def dated(i: int, at: dt.datetime | None) -> Row:
    row = doc(i)
    row["metadata"]["published_at"] = at
    return row


def dated_rows() -> list[Row]:
    """``sid(2)`` and ``sid(4)`` newest (a tie), then ``sid(1)``; ``sid(3)`` undated."""
    return [dated(1, AT - 3 * DAY), dated(2, AT - DAY), dated(3, None), dated(4, AT - DAY)]


def test_newest_first_order(tmp_path: Path) -> None:
    """``order: newest_first``: the scope (and so fetch and its ``max_rows`` stop) by ``published_at``
    descending, ties by ``doc_id``, undated last; the default stays ``doc_id`` order. The part file
    is in ``doc_id`` order either way."""
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    res, _ = go(dated_rows(), tmp_path / "newest", {"arxiv": lane}, order="newest_first", max_rows=3)
    assert [c.source_id for c in lane.calls] == [sid(2), sid(4), sid(1)] and res.unreached == [sid(3)]
    assert [r["doc_id"] for r in res.rows] == [sid(1), sid(2), sid(4)]
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    res, _ = go(dated_rows(), tmp_path / "default", {"arxiv": lane}, max_rows=3)
    assert [c.source_id for c in lane.calls] == [sid(1), sid(2), sid(3)] and res.unreached == [sid(4)]


def _dated_world(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    write_on_head(root, "documents", "documents", dated_rows(), stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband"])
    return root


def _acquire_plan(**options: Any) -> Plan:
    return Plan.model_validate({"plan": "test-acquire", "schema_version": 2, "acquire": {"wants": WANTS},
                                "stages": [{"stage": "acquire", "options": options}]})


def test_newest_first_through_the_runner(tmp_path: Path) -> None:
    """The stage line's ``options.order: newest_first`` reaches the scope: ``max_rows: 1`` fetches the
    newest row (before D391 the option was ignored and the first ``doc_id`` was fetched)."""
    root = _dated_world(tmp_path)
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    with lanes.using({"arxiv": lane}, now=NOW):
        rec = run_stage(root, _acquire_plan(order="newest_first", max_rows=1), "acquire", [sdir("documents")],
                        series="main", kind="part")
    assert rec.status == "ok", rec.error
    assert lane.keys() == [(sid(2), "full_text")] and rec.counts["stopped.limit"] == 1


@pytest.mark.parametrize(("options", "match", "unknown"), [
    ({"max_row": 1}, r"unknown options 'max_row' \(did you mean 'max_rows'\?\)", True),
    ({"ordr": "newest_first"}, r"unknown options 'ordr' \(did you mean 'order'\?\)", True),
    ({"order": "newest"}, r"options.order must be one of \['doc_id', 'newest_first'\]", False),
    ({"convert_max_rows": "10"}, r"options.convert_max_rows must be an integer >= 0 or null", False),
    ({"max_rows": -1}, r"options.max_rows must be an integer >= 0 or null", False),
    ({"convert_max_minutes": 0}, r"options.convert_max_minutes must be a number > 0 or null", False),
    ({"arxiv_pdf": "html_only"}, r"options.arxiv_pdf must be one of \['always', 'on_miss', 'never'\]", False),
    ({"arxiv_pdf": "on_miss", "structured": {"jats": True}},
     r"options.arxiv_pdf: on_miss needs options.structured.arxiv_html: true", False),
    ({"arxiv_pdf": "never"}, r"options.arxiv_pdf: never needs options.structured.arxiv_html: true", False),
    ({"arxiv_pdfs": "never"}, r"unknown options 'arxiv_pdfs' \(did you mean 'arxiv_pdf'\?\)", True),
])
def test_bad_options_are_refused_before_the_run(tmp_path: Path, options: dict[str, Any], match: str,
                                                unknown: bool) -> None:
    """An unknown, misspelt or malformed acquire option is a ``StageError`` before any substage (no lane
    call, no output), and a build refuses it at its start (``stage_refused``, from the preflight). An
    unknown key is the runner's refusal before any run record (D472); a malformed value fails the run."""
    root = _dated_world(tmp_path)
    plan = _acquire_plan(**options)
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    with lanes.using({"arxiv": lane}, now=NOW):
        if unknown:
            runs = [r.run_id for r in read_run_records(root)]
            with pytest.raises(StageError, match=match):
                run_stage(root, plan, "acquire", [sdir("documents")], series="main", kind="part")
            assert [r.run_id for r in read_run_records(root)] == runs
        else:
            rec = run_stage(root, plan, "acquire", [sdir("documents")], series="main", kind="part")
            assert rec.status == "failed" and "StageError" in (rec.error or ""), rec.error
            assert re.search(match, rec.error or ""), rec.error
            assert rec.outputs == {}
    assert lane.calls == []
    with pytest.raises(BuildFailed, match=match) as exc:
        preflight(root, plan)
    assert exc.value.error_class == "stage_refused"


def test_every_option_the_stage_reads_is_known() -> None:
    """:data:`workflows.acquire.main.OPTIONS` names every ``options`` key the acquire code reads (a key
    read but not listed would be refused in a plan): a grep of ``options.get("…")``/``opts.get("…")``,
    ``options["…"]`` and the ``_positive_int`` keys over ``workflows/acquire/``."""
    src = "\n".join(p.read_text() for p in (REPO / "workflows" / "acquire").rglob("*.py"))
    read = set(re.findall(r"\b(?:options|opts)(?:\.get\(|\[)\"([a-z_]+)\"", src))
    read |= set(re.findall(r"_positive_int\(options, \"([a-z_]+)\"", src))
    read |= {"structured", "quality"}  # point.structured_of, stack.conversion.quality.gate_from_options
    assert read - {"key"} <= OPTIONS, sorted(read - OPTIONS)
    check_options(dict.fromkeys(OPTIONS - {"max_rows", "convert_max_rows", "convert_max_minutes", "order"}))


# ---------------------------------------------------------------- carry reaches unreached rows (D587)
CARRY_WANTS = {"arxiv": ["full_text"], "openalex": ["full_text"]}
_POINTED = ("current_snapshot_id", "current_source_id", "current_role", "current_text_sha256",
            "current_text_status", "current_text_media", "current_text_quality", "current_revision",
            "convert_pin")


class _Clock:
    """A stand-in for acquire's ``time`` module: the fetch's budget clock, moved by :class:`_TickLane`."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@dataclass
class _TickLane(FakeLane):
    """A :class:`FakeLane` whose every call takes a (fake) minute and a second of ``clock``."""

    clock: _Clock = field(default_factory=_Clock)

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        out = super().fetch(request, env)
        self.clock.now += 61.0
        return out


def _gains_member(row: Row, i: int) -> Row:
    """``row`` after gather joined an OpenAlex member keyed by a DOI (``sid(i)``) to it and triage
    passed it again: the identity moved on, acquire's ``content`` still names the old members."""
    ident = row["identity"]
    m = copy.deepcopy(ident["members"][0])
    m.update(source_id=sid(i), channel="openalex", key_kind="doi", key_value=f"10.5555/paper.{i}",
             edge_kind="key:doi", edge_to_source_id=row["doc_id"])
    ident["members"].append(m)
    ident["member_ids"].append(sid(i))
    ident["channels"] = [*ident["channels"], "openalex"]
    ident["members_digest"] = dg(("members", "gained", row["doc_id"]))
    row["state"] = "triaged"
    return row


def _carried_rows() -> list[Row]:
    """Doc 1: new, due (reached first). Doc 2: acquired with its text, then gained member 5 (its
    in-band abstract not minted yet). Doc 3: acquired, current, its role due again (a transient
    failure past its ``retry_after``). Doc 4: new, due, no ``content`` yet."""
    waiting = document_row(3, state="acquired")
    waiting["inband"]["entries"] = []
    waiting["content"]["roles"][0].update(status="failed_transient", error_class="http_5xx", retry_after=AT)
    return [doc(1), _gains_member(document_row(2, state="acquired"), 5), waiting, doc(4)]


def test_a_budget_stop_still_carries_an_unreached_stale_row(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """D587: the fetch budget bounds fetching only. Doc 1's call spends the budget; docs 2-4 are
    unreached. Doc 2's content is stale (X21), so it is written with carry's result alone:
    ``members_digest`` current, the new member's role ``not_attempted[limit]`` (due, fetched by the
    next run), its own role, snapshots and pointer as they were (no in-band snapshot minted, nothing
    fetched or converted), its state ``triaged`` (not ``acquired``: acquire has not finished it).
    Doc 3 (carry changes nothing) and doc 4 (no content to re-base) stay unwritten, as before."""
    import workflows.acquire.main as acquire_main

    clock = _Clock()
    monkeypatch.setattr(acquire_main, "time", clock)
    rows = _carried_rows()
    stored = copy.deepcopy(rows[1]["content"])
    arxiv = _TickLane.answering("arxiv", {}, default=b"pdf", clock=clock)
    oa = FakeLane.answering("oa", {}, default=b"pdf")
    res, e = go(rows, tmp_path, {"arxiv": arxiv, "oa": oa}, wants=CARRY_WANTS, max_minutes=1)
    assert arxiv.keys() == [(sid(1), "full_text")] and oa.calls == []
    assert e.counts["stopped.budget"] == 1 and not res.scope_complete
    assert res.unreached == [sid(2), sid(3), sid(4)]
    by = {r["doc_id"]: r for r in res.rows}
    assert sorted(by) == [sid(1), sid(2)]
    assert (e.counts["unreached"], e.counts["unreached.carried"]) == (3, 1)
    out = by[sid(2)]
    c = out["content"]
    assert c["members_digest"] == out["identity"]["members_digest"] != stored["members_digest"]
    new = role(out, sid(5))
    assert (new["status"], new["error_class"], new["attempts"], new["lane"]) == ("not_attempted", "limit", 0, "oa")
    assert new["first_attempt_at"] is None and is_due(new, NOW)
    assert role(out, sid(2)) == stored["roles"][0] and len(c["roles"]) == 2
    assert c["snapshots"] == stored["snapshots"]
    assert {k: c[k] for k in (*_POINTED, "same_raw_doc_ids", "revisions")} == {
        k: stored[k] for k in (*_POINTED, "same_raw_doc_ids", "revisions")}
    assert (out["state"], out["run_id"], c["run_id"]) == ("triaged", rid(50), rid(50))  # still waits for acquire

    # The next run, with no stop, reaches doc 2 and fetches the new member's role.
    clock.now = 0.0
    res2, _ = go(merge(rows, res), tmp_path, {"arxiv": arxiv, "oa": oa}, wants=CARRY_WANTS, now=NOW + H, run=51)
    assert oa.keys() == [(sid(5), "full_text")] and res2.unreached == []
    assert role({d["doc_id"]: d for d in res2.rows}[sid(2)], sid(5))["status"] == "ok"


@pytest.mark.parametrize("workers", [1, 4])
def test_an_unreached_row_rebased_by_carry_is_regrouped_for_same_raw_and_the_pointer(tmp_path: Path,
                                                                                    workers: int) -> None:
    """D587, a member dropped: doc 2 held member 6's raw snapshot (its current text, and the bytes
    doc 7 also holds); gather moved member 6 away. A ``max_rows`` stop leaves doc 2 unreached; its
    carry drops member 6's snapshot, the pointer moves to member 2's text (``point`` over the carried
    block, at the block's own pin), and ``same_raw_doc_ids`` is recomputed on the carried block: doc
    2 and doc 7 no longer list each other (doc 7, outside the scope, is written for it). The same under
    ``fetch_workers`` (doc 2 never admitted)."""
    two, seven = document_row(2, state="acquired"), document_row(7, state="acquired")
    for r in (two, seven):
        r["inband"]["entries"] = []
    six = {**copy.deepcopy(two["content"]["snapshots"][0]), "snapshot_id": f"{rid(1)}.6", "source_id": sid(6),
           "captured_at": AT + H, "as_of": AT + H, "raw": dict(seven["content"]["snapshots"][0]["raw"])}
    two["content"]["snapshots"].append(six)
    two["content"].update(current_snapshot_id=six["snapshot_id"], current_source_id=sid(6),
                          same_raw_doc_ids=[sid(7)])
    seven["content"]["same_raw_doc_ids"] = [sid(2)]
    two["content"]["roles"][0].update(status="failed_transient", error_class="http_5xx", retry_after=AT)
    two["identity"]["members_digest"] = dg(("members", "split", sid(2)))  # gather moved member 6 away
    two["state"] = "triaged"
    lane = FakeLane.answering("arxiv", {}, default=b"pdf")
    res, e = go([doc(1), two, seven], tmp_path, {"arxiv": lane}, max_rows=1, fetch_workers=workers)
    assert lane.keys() == [(sid(1), "full_text")] and res.unreached == [sid(2)]
    by = {r["doc_id"]: r for r in res.rows}
    assert sorted(by) == [sid(1), sid(2), sid(7)]
    assert e.counts["stopped.limit"] == 1 and e.counts["unreached.carried"] == 1
    c = by[sid(2)]["content"]
    assert [s["source_id"] for s in c["snapshots"]] == [sid(2)]
    assert (c["current_snapshot_id"], c["current_text_status"]) == (f"{rid(1)}.0", "ok")
    assert c["same_raw_doc_ids"] == [] and by[sid(7)]["content"]["same_raw_doc_ids"] == []
    assert role(by[sid(2)], sid(2))["status"] == "failed_transient"  # still due, nothing charged


@pytest.mark.slow
def test_a_stopped_run_leaves_no_carry_fault_through_the_runner(tmp_path: Path) -> None:
    """D587 through ``run_stage`` and the checks, the e2e store's shape: an acquired row gains an
    OpenAlex DOI member (a gather part after the acquire that wrote its content, then triage); the next acquire
    run's fetch stops (``max_rows: 1``) before it. X21 ``carry_regrouped`` (hard) holds after that
    run, with V-content, block ownership, attempts and state monotone; the row is still due, and the
    next run fetches its new member."""
    root = tmp_path / "store"
    two = document_row(2, state="acquired")
    two["inband"]["entries"] = []
    write_on_head(root, "documents", "documents", [doc(1), two], stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband"])
    gained = _gains_member(copy.deepcopy(two), 5)
    write_on_head(root, "documents", "documents", [{**gained, "state": "identified"}], stage="gather", run=2,
                  blocks_written=["identity", "metadata", "inband"])
    write_on_head(root, "documents", "documents", [gained], stage="triage", run=3, blocks_written=["annotations"])

    def plan(**options: Any) -> Plan:
        return Plan.model_validate({
            "plan": "test-acquire", "schema_version": 2,
            "acquire": {"convert": {"config": "pymupdf4llm-v1"}, "wants": CARRY_WANTS},
            "stages": [{"stage": "acquire", "options": {"allow_degraded_convert": True, **options}}]})

    arxiv = FakeLane.answering("arxiv", {}, default=b"pdf")
    oa = FakeLane.answering("oa", {}, default=b"pdf")
    with lanes.using({"arxiv": arxiv, "oa": oa}, now=NOW):
        rec = run_stage(root, plan(max_rows=1), "acquire", [sdir("documents")], series="main", kind="part")
    assert rec.status == "ok", rec.error
    assert arxiv.keys() == [(sid(1), "full_text")] and oa.calls == []
    checks = ["carry_regrouped", "V-content", "block_ownership", "attempts_monotone", "state_monotone",
              "created_preserved", "same_raw_reported"]
    by = {r.check: r for r in run_checks(root, names=checks)}
    assert {n: r.status for n, r in by.items()} == dict.fromkeys(checks, "ok"), {n: r.items for n, r in by.items()}
    assert rec.counts["stopped.limit"] == 1 and rec.counts["unreached"] == 1 and not rec.scope_complete
    assert _part_ids(root, rec) == [sid(1), sid(2)]
    row = view_of(root)[sid(2)]
    assert row["content"]["members_digest"] == row["identity"]["members_digest"]
    new = role(row, sid(5))
    assert (new["status"], new["error_class"], new["attempts"]) == ("not_attempted", "limit", 0)
    assert row["content"]["current_text_status"] == "ok"  # the text it held stays current
    assert row["state"] == "triaged"

    with lanes.using({"arxiv": arxiv, "oa": oa}, now=NOW + H):
        rec2 = run_stage(root, plan(), "acquire", [sdir("documents")], series="main", kind="part")
    assert rec2.status == "ok", rec2.error
    assert oa.keys() == [(sid(5), "full_text")] and rec2.counts["unreached"] == 0
    assert view_of(root)[sid(2)]["state"] == "acquired"


def test_an_unreached_row_still_lists_a_new_same_raw_holder(tmp_path: Path) -> None:
    """D587 (same class, X25): doc 1, reached, fetches the bytes doc 2 already holds; doc 2 (acquired,
    current, due again) is unreached and carry leaves it as it is. Doc 1 lists doc 2 in
    ``same_raw_doc_ids``, and doc 2 is written with doc 1 in its own list (its block otherwise as
    stored, its state ``acquired``), as a row outside the scope would be; X25 needs both."""
    body = b"the same pdf"
    two = document_row(2, state="acquired")
    two["inband"]["entries"] = []
    two["content"]["snapshots"][0]["raw"] = blob(open_blobs(tmp_path / "digest").put(body), len(body), "application/pdf")
    two["content"]["roles"][0].update(status="failed_transient", error_class="http_5xx", retry_after=AT)
    stored = copy.deepcopy(two["content"])
    lane = FakeLane.answering("arxiv", {(sid(1), "full_text"): body}, default=b"pdf")
    res, e = go([doc(1), two], tmp_path, {"arxiv": lane}, max_rows=1)
    assert lane.keys() == [(sid(1), "full_text")] and res.unreached == [sid(2)]
    view = {r["doc_id"]: r for r in merge([doc(1), two], res)}
    assert view[sid(1)]["content"]["same_raw_doc_ids"] == [sid(2)]
    assert view[sid(2)]["content"]["same_raw_doc_ids"] == [sid(1)]
    c = view[sid(2)]["content"]
    assert {k: v for k, v in c.items() if k not in ("same_raw_doc_ids", "run_id", "updated_at")} == {
        k: v for k, v in stored.items() if k not in ("same_raw_doc_ids", "run_id", "updated_at")}
    assert view[sid(2)]["state"] == "acquired" and e.counts["unreached.carried"] == 0


def test_a_due_role_of_a_row_triage_does_not_let_fetch_is_not_due(tmp_path: Path) -> None:
    """D698: a rate-limited role (``failed_transient[rate_limit]``, due at its hint) of a row whose
    re-triage turned ``fetch`` into ``skip`` is not due: the row is ``idle`` (no ``scope.due``, not in
    scope), as the fetch substage would never call it; triage ``fetch`` again makes it due and fetched.
    The same for a masked row. Before, such a row matched ``due`` on every run and nothing fetched it."""
    rows = [doc(1), doc(2)]
    lane = FakeLane.answering("arxiv", {(sid(1), "full_text"): ["rate_limit", b"pdf"],
                                        (sid(2), "full_text"): ["rate_limit", b"pdf"]})
    res, _ = go(rows, tmp_path, {"arxiv": lane})
    rows = merge(rows, res)
    assert [role(r, r["doc_id"])["error_class"] for r in rows] == ["rate_limit", "rate_limit"]
    skip = copy.deepcopy(rows[0])
    skip["annotations"]["triage"]["payload"]["decision"] = "skip"
    masked = copy.deepcopy(rows[1])
    masked["identity"]["is_masked"] = True
    later = NOW + 2 * H
    assert is_due(role(skip, sid(1)), later)  # the role itself is due at its hint
    res, e = go([skip, masked], tmp_path, {"arxiv": lane}, now=later, run=51)
    assert len(lane.calls) == 2 and "scope.due" not in e.counts
    assert (e.counts["idle"], e.counts["rows_in_scope"], res.rows) == (1, 1, [])  # masked: in scope by fetch
    res, e = go(rows, tmp_path, {"arxiv": lane}, now=later, run=52)  # triage fetch, unmasked again
    assert e.counts["scope.due"] == 2 and len(lane.calls) == 4
    assert [role(r, r["doc_id"])["status"] for r in merge(rows, res)] == ["ok", "ok"]


def test_a_per_host_recheck_interval_dates_the_next_check(tmp_path: Path) -> None:
    """D701: ``acquire.recheck.hosts`` sets the recheck of an ``ok`` episode that started on that host
    (or a subdomain; the longest key wins): ``openai.com: 30d`` dates an openai.com page's next check
    30 days out, another host's stays at ``default`` (weekly). Before, every page was weekly."""
    from stack.plan import AcquireConfig, Recheck
    from workflows.acquire.lanes.http import HttpLane

    rc = Recheck(hosts={"openai.com": "30d", "blog.openai.com": "2d"})
    assert rc.for_host("www.openai.com") == dt.timedelta(days=30) and rc.for_host("notopenai.com") == dt.timedelta(days=7)
    assert rc.for_host("blog.openai.com") == dt.timedelta(days=2) and rc.for_host(None) == dt.timedelta(days=7)
    with pytest.raises(ValueError):
        Recheck(hosts={"OpenAI.com": "30d"})
    with pytest.raises(ValueError):
        Recheck(hosts={"openai.com": "fortnightly"})
    page, other = doc(1, channel="web"), doc(2, channel="web")
    page["identity"]["members"][0]["canonical_url"] = "https://www.openai.com/index/a-post"
    other["identity"]["members"][0]["canonical_url"] = "https://example.org/b"
    http = FakeLane.answering("http", {}, default=b"<html>x</html>", media_type="text/html")
    http.host_key = HttpLane.host_key  # type: ignore[attr-defined]  # the real lane's fetch group
    e = env(tmp_path)
    e.acquire = AcquireConfig(recheck=rc)
    with lanes.using({"http": http}):
        res = acquire_rows([page, other], e, wants=WANTS, channel_group=GROUPS.get)
    got = {r["doc_id"]: role(r, r["doc_id"], "page")["retry_after"] for r in res.rows}
    assert got == {sid(1): NOW + dt.timedelta(days=30), sid(2): NOW + dt.timedelta(days=7)}


def test_the_recheck_periods_enter_no_digest_but_the_plans() -> None:
    """D701: ``acquire.recheck`` (``hosts`` included) is in no acquire line digest or convert pin, so
    setting a host's period re-fetches and re-converts nothing by itself; only the plan digest moves."""
    import yaml

    from stack.plan import Plan
    from workflows.acquire.convert import convert_pin
    from workflows.acquire.main import structured_of

    data = yaml.safe_load(Path("configs/plans/batch-2026-10a.yaml").read_text())
    assert data["acquire"]["recheck"]["hosts"] == {"openai.com": "30d"}
    plain = copy.deepcopy(data)
    plain["acquire"]["recheck"] = {"default": "weekly"}
    a, b = Plan.model_validate(data), Plan.model_validate(plain)
    la, lb = a.stage_line("acquire"), b.stage_line("acquire")
    assert la is not None and lb is not None and la.digest() == lb.digest()
    assert convert_pin(a.acquire, structured_of(la.options)) == convert_pin(b.acquire, structured_of(lb.options))
    assert a.plan_digest != b.plan_digest
