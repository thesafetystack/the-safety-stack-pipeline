"""The acquire option ``arxiv_pdf: always | on_miss | never`` (D480; ``workflows/acquire/carry.py``,
"arXiv PDF"): which ``full_text`` roles on lane ``arxiv`` are requested, whichever channel found the
paper.

Pure cases: the option's parsing and refusals, :func:`html_verdict` over every HTML state, the rule in
``carry`` (``never`` skips every arXiv PDF role, OpenAlex/S2-found preprints included; ``on_miss``
requests a PDF only on a miss; a switch to ``always`` reopens only the roles this rule skipped; the
one-fetch rule still gives one PDF per id). Fake-lane cases through :func:`acquire_rows`: a ``never``
run calls no PDF; ``on_miss`` makes the PDF of a failed HTML due after ``quality`` and fetches it in the
next run, the run before ending scope-incomplete. Runner cases (slow; the real arXiv lane on a fake
transport, the real converter): ``on_miss`` with an available, a missing (404) and a quality-failed
HTML; then ``always``, which fetches only the PDFs left, asks no HTML and moves the pointer only per
the HTML-before-PDF rule (``too_short`` included); the same switch from ``never``.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pytest

from stack.arxiv import ARXIV_HTML_MIN_BYTES
from stack.plan import Plan
from tests.fakes.lanes import FakeLane
from tests.synth.rows import AT, blob, rid, sid
from tests.test_acquire_core import (
    GROUPS,
    PIN,
    go_with,
    merge,
    new_content,
    new_role,
    paper,
    role,
    states,
    with_keys,
)
from workflows.acquire.carry import ARXIV_PDF_MODES, arxiv_pdf_of, carry, html_verdict
from workflows.acquire.scope import FETCH

Row = dict[str, Any]
ON = {"arxiv_html": True}
WANTS = {"arxiv": ["full_text", "html"], "openalex": ["full_text", "xml"], "semantic_scholar": ["full_text"]}
AID = "2609.36750"
ARXIV_DOI = f"10.48550/arxiv.{AID}"


def opts(mode: str | None, **more: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"structured": dict(ON), **more}
    if mode is not None:
        out["arxiv_pdf"] = mode
    return out


# ---------------------------------------------------------------- the option
def test_option_values_and_refusals() -> None:
    """Absent or null = ``always`` (the behaviour before the option); the three values; anything else,
    and ``on_miss``/``never`` without ``structured.arxiv_html``, is refused."""
    assert ARXIV_PDF_MODES == ("always", "on_miss", "never")
    assert arxiv_pdf_of({}) == arxiv_pdf_of(None) == arxiv_pdf_of({"arxiv_pdf": None}) == "always"
    assert arxiv_pdf_of({"arxiv_pdf": "always"}) == "always"  # no HTML needed for the old behaviour
    for mode in ("on_miss", "never"):
        assert arxiv_pdf_of(opts(mode)) == mode
        for off in ({}, {"structured": {"jats": True}}, {"structured": {"arxiv_html": False}}):
            with pytest.raises(ValueError, match=f"arxiv_pdf: {mode} needs options.structured.arxiv_html: true"):
                arxiv_pdf_of({**off, "arxiv_pdf": mode})
    for bad in ("Never", "on-miss", "html_only", True, 0, ["never"]):
        with pytest.raises(ValueError, match=r"options.arxiv_pdf must be one of \['always', 'on_miss', 'never'\]"):
            arxiv_pdf_of(opts(bad))  # type: ignore[arg-type]


# ---------------------------------------------------------------- html_verdict (pure)
T = AT


def hsnap(n: int, source: str, *, status: str | None, error_class: str | None = None, cfg: str | None = PIN,
          at: dt.datetime = T) -> Row:
    return {"snapshot_id": f"{rid(20)}.{n}", "source_id": source, "kind": "raw", "content_role": "html",
            "via": "live", "captured_at": at, "as_of": at, "revision": "v1",
            "raw": blob(f"d{n}", 1, "text/html"), "text": blob(f"t{n}", 1, "text/markdown") if status == "ok" else None,
            "text_status": status, "text_error_class": error_class, "convert_attempts": 1 if status else 0,
            "convert_pin": cfg if status else None, "quality": None}


def hrole(source: str, status: str, error_class: str | None = None) -> Row:
    return new_role("html", source, status=status, error_class=error_class, lane="arxiv")


def verdict(roles: list[Row], snaps: list[Row] | None = None, *, key_ids: dict[str, str] | None = None,
            live: dict[str, Row] | None = None) -> str:
    live = live or {sid(1): {"source_id": sid(1), "key_kind": "arxiv", "key_value": AID},
                    sid(2): {"source_id": sid(2), "key_kind": "doi", "key_value": ARXIV_DOI}}
    return html_verdict(AID, {(r["source_id"], r["content_role"]): r for r in roles}, live, list(snaps or []), PIN,
                        key_ids or {})


@pytest.mark.parametrize(("roles", "snaps", "want"), [
    ([], [], "miss:no_html_role"),
    ([hrole(sid(1), "empty")], [], "miss:html_empty"),
    ([hrole(sid(1), "failed_permanent", "dead")], [], "miss:html_failed_permanent"),
    ([hrole(sid(1), "quarantined", "cap_reached")], [], "miss:html_quarantined"),
    ([hrole(sid(1), "empty"), hrole(sid(2), "failed_permanent", "dead")], [], "miss:html_failed"),
    ([hrole(sid(1), "skipped", "not_requested")], [], "miss:html_skipped"),
    ([hrole(sid(1), "skipped", "withdrawn")], [], "withdrawn"),
    ([hrole(sid(1), "not_attempted", "limit")], [], "pending"),
    ([hrole(sid(1), "failed_transient", "http_5xx")], [], "pending"),
    ([hrole(sid(1), "skipped", "gated")], [], "pending"),
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status=None)], "pending"),  # fetched, not converted yet
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status="ok", cfg="q" * 64)], "pending"),  # another pin
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status="failed_transient", error_class="crashed")], "pending"),
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status="ok")], "ok"),
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status="failed_permanent", error_class="parse_error")],
     "miss:convert_failed"),
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status="failed_permanent", error_class="quality_too_short")],
     "miss:quality"),
    # the newest held HTML decides: v2's failed text after v1's ok one is a miss
    ([hrole(sid(1), "ok")], [hsnap(1, sid(1), status="ok"),
                             hsnap(2, sid(1), status="failed_permanent", error_class="quality_garbled",
                                   at=T + dt.timedelta(days=1))], "miss:quality"),
    # the role ended empty for the current version: a miss, whatever an older revision's text says
    ([hrole(sid(1), "empty")], [hsnap(1, sid(1), status="ok")], "miss:html_empty"),
    # the one-fetch rule's skipped[not_needed] roles do not count; the lead's state does
    ([hrole(sid(1), "ok"), hrole(sid(2), "skipped", "not_needed")], [hsnap(1, sid(1), status="ok")], "ok"),
    ([hrole(sid(1), "empty"), hrole(sid(2), "skipped", "not_needed")], [], "miss:html_empty"),
    # a held ok text wins over another member's HTML role skipped for another reason
    ([hrole(sid(1), "ok"), hrole(sid(2), "skipped", "not_requested")], [hsnap(1, sid(1), status="ok")], "ok"),
    ([hrole(sid(1), "not_attempted", "limit"), hrole(sid(2), "skipped", "not_requested")], [], "miss:html_skipped"),
])
def test_html_verdict(roles: list[Row], snaps: list[Row], want: str) -> None:
    assert verdict(roles, snaps) == want


def test_html_verdict_reads_only_its_own_id_and_identity_keys() -> None:
    """Another id's HTML is not this id's; an ``html`` role whose member's key names no arXiv id stands
    for the id ``identity.keys`` gives its source (R6 item 2b)."""
    live = {sid(1): {"source_id": sid(1), "key_kind": "arxiv", "key_value": "2609.00001"},
            sid(3): {"source_id": sid(3), "key_kind": "doi", "key_value": "10.1000/j.3"}}
    ok = [hrole(sid(1), "ok")], [hsnap(1, sid(1), status="ok")]
    assert verdict(*ok, live=live) == "miss:no_html_role"
    keyed = [hrole(sid(3), "ok")], [hsnap(1, sid(3), status="ok")]
    assert verdict(*keyed, live=live, key_ids={sid(3): AID}) == "ok"
    assert verdict(*keyed, live=live) == "miss:no_html_role"


# ---------------------------------------------------------------- the rule in carry (pure)
def three() -> Row:
    """One preprint found by arXiv, OpenAlex (arXiv DOI) and S2 (arXiv key), plus a publisher-DOI
    OpenAlex member of the same document (its ``full_text`` is on ``oa``, not this rule's)."""
    return with_keys(paper(1, (1, "arxiv", "arxiv", AID), (2, "openalex", "doi", ARXIV_DOI),
                           (3, "semantic_scholar", "arxiv", AID), (4, "openalex", "doi", "10.1000/j.4")))


def carried(row: Row, mode: str, counts: dict[str, int] | None = None, permit: str = FETCH) -> Row:
    out = copy.deepcopy(row)
    out["content"] = carry(out, {out["doc_id"]: out}, wants=WANTS, channel_group=GROUPS.get, permit=permit,
                           pin=PIN, counts=counts, arxiv_html=True, arxiv_pdf=mode)
    return out


def pdfs(row: Row) -> dict[str, tuple[str, str | None, str]]:
    return {k[0]: v for k, v in states(row).items() if k[1] == "full_text"}


def test_never_skips_every_arxiv_pdf_role_whichever_channel_found_it() -> None:
    """``never``: the arXiv, OpenAlex (arXiv DOI) and S2 members' PDF roles are all ``skipped[not_requested]``
    (none due, the error names the option); the publisher DOI's ``full_text`` on ``oa`` is untouched; the
    ``html`` role is the one fetch for the id. ``always`` (and absent) is the behaviour before the option."""
    counts: dict[str, int] = {}
    row = carried(three(), "never", counts)
    a, o, s2, pub = sid(1), sid(2), sid(3), sid(4)
    assert pdfs(row) == {a: ("skipped", "not_requested", "arxiv"), o: ("skipped", "not_requested", "arxiv"),
                         s2: ("skipped", "not_requested", "arxiv"), pub: ("not_attempted", "limit", "oa")}
    assert {role(row, s)["error"] for s in (a, o, s2)} == {"arxiv_pdf: never"}
    assert {role(row, s)["rule"] for s in (a, o, s2)} == {"arxiv_pdf"} and role(row, pub)["rule"] is None  # D690
    assert states(row)[(a, "html")] == ("not_attempted", "limit", "arxiv")
    assert counts["arxiv_pdf.not_requested"] == 3 and "arxiv_pdf.requested" not in counts

    before = carried(three(), "always")
    assert pdfs(before) == {a: ("not_attempted", "limit", "arxiv"), o: ("skipped", "not_needed", "arxiv"),
                            s2: ("skipped", "not_needed", "arxiv"), pub: ("not_attempted", "limit", "oa")}
    plain = copy.deepcopy(three())
    plain["content"] = carry(plain, {plain["doc_id"]: plain}, wants=WANTS, channel_group=GROUPS.get, permit=FETCH,
                             pin=PIN, arxiv_html=True)  # no arxiv_pdf argument: the default
    assert plain["content"] == before["content"]


def test_never_skips_held_and_failed_pdf_roles_and_keeps_their_fields() -> None:
    """A PDF role with progress (``ok`` from an earlier ``always`` build, a dead one) is skipped too, its
    other fields kept; held snapshots stay. Roles skipped for another reason (withdrawn) are left alone.
    On a row triage does not let fetch nothing moves."""
    row = paper(1, (1, "arxiv", "arxiv", AID), (2, "arxiv", "arxiv", "2609.00002"), (3, "arxiv", "arxiv", "2609.00003"))
    row["content"] = new_content(row["identity"]["members_digest"], PIN)
    ok = {**new_role("full_text", sid(1), status="ok", error_class=None, lane="arxiv"), "attempts": 1,
          "etag": "e1", "snapshot_id": "r.1", "retry_after": AT + dt.timedelta(days=7)}
    dead = {**new_role("full_text", sid(2), status="failed_permanent", error_class="dead", lane="arxiv"),
            "attempts": 4}
    gone = new_role("full_text", sid(3), status="skipped", error_class="withdrawn", lane="arxiv")
    row["content"]["roles"] = [ok, dead, gone]
    row["content"]["snapshots"] = [{**hsnap(1, sid(1), status="ok"), "content_role": "full_text"}]
    out = carried(row, "never")
    r1, r2, r3 = role(out, sid(1)), role(out, sid(2)), role(out, sid(3))
    assert (r1["status"], r1["error_class"], r1["attempts"], r1["etag"], r1["snapshot_id"], r1["retry_after"]) == (
        "skipped", "not_requested", 1, "e1", "r.1", None)
    assert (r2["status"], r2["error_class"], r2["attempts"]) == ("skipped", "not_requested", 4)
    assert r1["rule"] == r2["rule"] == "arxiv_pdf"  # D690: the rule names itself
    assert r3 == gone
    assert out["content"]["snapshots"] == row["content"]["snapshots"]
    gated = carried(row, "never", permit="skip")
    assert role(gated, sid(1))["status"] == "ok" and role(gated, sid(2))["status"] == "failed_permanent"


def with_html(row: Row, html_status: str, snap_status: str | None = None, error_class: str | None = None) -> Row:
    """``row`` carried once under ``on_miss``, then its arXiv member's ``html`` role set to
    ``html_status`` (and an HTML snapshot with ``snap_status`` when given), as a run would leave it."""
    out = carried(row, "on_miss")
    for i, r in enumerate(out["content"]["roles"]):
        if (r["source_id"], r["content_role"]) == (sid(1), "html"):
            out["content"]["roles"][i] = {**r, "status": html_status,
                                          "error_class": {"failed_permanent": "dead"}.get(html_status),
                                          "attempts": 1}
    if snap_status is not None:
        out["content"]["snapshots"].append(hsnap(9, sid(1), status=snap_status, error_class=error_class))
    return out


def test_on_miss_requests_the_pdf_only_on_a_miss() -> None:
    """``on_miss``: while the HTML is pending the PDF roles wait (``skipped[not_requested]``, "waiting");
    an ``ok`` HTML text keeps them so ("the arXiv HTML is the text"); on a miss (404/unacceptable page,
    a dead role, a failed conversion, a quality failure) the lead's PDF role becomes due and the
    one-fetch rule keeps one PDF per id; a withdrawn HTML requests nothing."""
    a, o, s2 = sid(1), sid(2), sid(3)
    counts: dict[str, int] = {}
    first = carried(three(), "on_miss", counts)
    assert {s: pdfs(first)[s][:2] for s in (a, o, s2)} == dict.fromkeys((a, o, s2), ("skipped", "not_requested"))
    assert role(first, a)["error"] == "arxiv_pdf: on_miss, waiting for the arXiv HTML"
    assert counts["arxiv_pdf.not_requested"] == 3

    ok = carried(with_html(three(), "ok", "ok"), "on_miss")
    assert pdfs(ok)[a][:2] == ("skipped", "not_requested")
    assert role(ok, a)["error"] == "arxiv_pdf: on_miss, the arXiv HTML is the text"

    for html, snap, cls, why in [("empty", None, None, "html_empty"), ("failed_permanent", None, None,
                                                                         "html_failed_permanent"),
                                 ("ok", "failed_permanent", "parse_error", "convert_failed"),
                                 ("ok", "failed_permanent", "quality_stub", "quality")]:
        counts = {}
        out = carried(with_html(three(), html, snap, cls), "on_miss", counts)
        assert {s: pdfs(out)[s] for s in (a, o, s2)} == {
            a: ("not_attempted", "limit", "arxiv"), o: ("skipped", "not_needed", "arxiv"),
            s2: ("skipped", "not_needed", "arxiv")}, why
        assert counts["arxiv_pdf.requested"] == 3 and counts[f"arxiv_pdf.miss.{why}"] == 3, (why, counts)

    gone = carried(with_html(three(), "skipped"), "on_miss")  # error_class None: set withdrawn below
    for i, r in enumerate(gone["content"]["roles"]):
        if (r["source_id"], r["content_role"]) == (a, "html"):
            gone["content"]["roles"][i] = {**r, "status": "skipped", "error_class": "withdrawn"}
    gone = carried(gone, "on_miss")
    assert pdfs(gone)[a][:2] == ("skipped", "not_requested")
    assert role(gone, a)["error"] == "arxiv_pdf: on_miss, the arXiv version is withdrawn"


def test_on_miss_keeps_an_ok_pdf_while_the_html_is_pending() -> None:
    """D643 (amends D480; VERIFY-2A): an ``ok`` PDF role is not set aside while its id's HTML is
    ``pending`` (a merged member's ``html`` role not fetched yet, the first member's settled ``empty``),
    its fields kept and counted ``arxiv_pdf.kept_pending``; a waiting role that is not ``ok`` is set
    aside as before. Once the HTML says ``ok`` or ``withdrawn`` the ``ok`` role is set aside (D480's
    default); on a miss it stays ``ok`` (requested); ``never`` sets it aside whatever the HTML."""
    s2, oa = sid(3), sid(2)
    row = with_keys(paper(1, (3, "semantic_scholar", "arxiv", AID), (2, "openalex", "doi", ARXIV_DOI)))
    row["content"] = new_content(row["identity"]["members_digest"], PIN)
    pdf = {**new_role("full_text", s2, status="ok", error_class=None, lane="arxiv"), "attempts": 1,
           "snapshot_id": "r.1", "etag": "e1", "retry_after": AT + dt.timedelta(days=7)}
    gone = {**hrole(s2, "empty"), "attempts": 1, "retry_after": AT + dt.timedelta(days=180)}
    row["content"]["roles"] = [pdf, gone]
    row["content"]["snapshots"] = [{**hsnap(1, s2, status="ok"), "snapshot_id": "r.1", "content_role": "full_text"}]
    counts: dict[str, int] = {}
    out = carried(row, "on_miss", counts)
    assert states(out)[(oa, "html")] == ("not_attempted", "limit", "arxiv")  # the merged member's: pending
    assert role(out, s2) == pdf and counts["arxiv_pdf.kept_pending"] == 1
    assert pdfs(out)[oa][:2] == ("skipped", "not_requested")  # the new member's PDF waits, as before
    assert role(out, oa)["error"] == "arxiv_pdf: on_miss, waiting for the arXiv HTML"
    assert out["content"]["snapshots"] == row["content"]["snapshots"]

    def html_now(row_: Row, status: str, error_class: str | None = None, snap: str | None = None) -> Row:
        out_ = copy.deepcopy(row_)
        for i, r in enumerate(out_["content"]["roles"]):
            if (r["source_id"], r["content_role"]) == (oa, "html"):
                out_["content"]["roles"][i] = {**r, "status": status, "error_class": error_class, "attempts": 1}
        if snap is not None:
            out_["content"]["snapshots"].append(hsnap(9, oa, status=snap))
        return out_

    missed = carried(html_now(out, "empty"), "on_miss")
    assert role(missed, s2) == pdf  # a miss requests the PDF: the held one is the answer
    for status, cls, snap, why in [("ok", None, "ok", "the arXiv HTML is the text"),
                                   ("skipped", "withdrawn", None, "the arXiv version is withdrawn")]:
        settled = carried(html_now(out, status, cls, snap), "on_miss")
        r = role(settled, s2)
        assert (r["status"], r["error_class"], r["error"]) == ("skipped", "not_requested", f"arxiv_pdf: on_miss, {why}")
        assert (r["attempts"], r["snapshot_id"], r["etag"]) == (1, "r.1", "e1")
    assert role(carried(row, "never"), s2)["status"] == "skipped"


def test_switching_to_always_reopens_only_the_roles_this_rule_skipped() -> None:
    """``on_miss`` (or ``never``) → ``always``: the rule's ``skipped[not_requested]`` roles become
    ``not_attempted[limit]`` (the arXiv member's due, the others ``not_needed`` behind it); the HTML roles
    are not touched; a ``not_requested`` role the rule did not write (a lane's answer) and a withdrawn one
    stay; an ``ok`` role skipped under ``never`` keeps its fields when reopened."""
    a, o, s2 = sid(1), sid(2), sid(3)
    for mode in ("on_miss", "never"):
        before = carried(with_html(three(), "ok", "ok"), mode)
        counts: dict[str, int] = {}
        after = carried(before, "always", counts)
        assert {s: pdfs(after)[s] for s in (a, o, s2)} == {
            a: ("not_attempted", "limit", "arxiv"), o: ("skipped", "not_needed", "arxiv"),
            s2: ("skipped", "not_needed", "arxiv")}, mode
        assert role(after, a)["error"] == "not yet fetched" and counts["arxiv_pdf.requested"] == 3
        assert [role(after, s)["rule"] for s in (a, o, s2)] == [None, None, None], mode  # D690: reopened
        assert [r for r in after["content"]["roles"] if r["content_role"] == "html"] == [
            r for r in before["content"]["roles"] if r["content_role"] == "html"]

    row = paper(1, (1, "arxiv", "arxiv", AID), (2, "arxiv", "arxiv", "2609.00002"))
    row["content"] = new_content(row["identity"]["members_digest"], PIN)
    lane_said = new_role("full_text", sid(1), status="skipped", error_class="not_requested", lane="arxiv",
                         error="the arXiv lane fetches full_text …")
    held = {**new_role("full_text", sid(2), status="ok", error_class=None, lane="arxiv"), "attempts": 1, "etag": "e"}
    row["content"]["roles"] = [lane_said, held]
    after = carried(carried(row, "never"), "always")
    assert role(after, sid(1)) == lane_said
    # D690: the rule knows its own roles by ``rule``, not by the error text: a lane's answer whose error
    # happens to read like the rule's stays too.
    lookalike = {**lane_said, "error": "arxiv_pdf: never"}
    row["content"]["roles"] = [lookalike, held]
    assert role(carried(carried(row, "never"), "always"), sid(1)) == lookalike
    assert (role(after, sid(2))["status"], role(after, sid(2))["attempts"], role(after, sid(2))["etag"]) == (
        "not_attempted", 1, "e")


# ---------------------------------------------------------------- fake lanes through acquire_rows
def rows3() -> list[Row]:
    """Three preprints: an arXiv paper, an OpenAlex paper on an arXiv DOI, an S2 paper on an arXiv key."""
    return [with_keys(paper(1, (1, "arxiv", "arxiv", "2609.00001"))),
            with_keys(paper(2, (2, "openalex", "doi", "10.48550/arxiv.2609.00002"))),
            with_keys(paper(3, (3, "semantic_scholar", "arxiv", "2609.00003")))]


def test_never_run_calls_no_pdf(tmp_path: Path) -> None:
    """A ``never`` run asks the arXiv lane for the three HTMLs and no PDF, whichever channel found the
    paper; its scope is complete; a second run calls nothing."""
    arxiv = FakeLane.answering("arxiv", {}, default=b"<html>latexml</html>")
    res, e = go_with(rows3(), tmp_path, {"arxiv": arxiv}, opts("never"), wants=WANTS)
    assert sorted(arxiv.keys()) == [(sid(1), "html"), (sid(2), "html"), (sid(3), "html")]
    assert res.scope_complete and e.counts["arxiv_pdf.not_requested"] == 3
    assert "arxiv_pdf.due_next_run" not in e.counts  # the second pass is on_miss's only
    for r in res.rows:
        assert pdfs(r) == {r["doc_id"]: ("skipped", "not_requested", "arxiv")}
    n = len(arxiv.calls)
    res2, e2 = go_with(merge(rows3(), res), tmp_path, {"arxiv": arxiv}, opts("never"), wants=WANTS)
    assert len(arxiv.calls) == n and e2.counts["lane_calls"] == 0 and res2.scope_complete


def test_on_miss_makes_the_pdf_due_after_the_run_and_fetches_it_next(tmp_path: Path) -> None:
    """Run 1 under ``on_miss``: the HTML of paper 2 is dead, papers 1 and 3 have one. No PDF is asked;
    the second pass after ``quality`` makes paper 2's PDF due in the written row, and the run ends
    scope-incomplete (``arxiv_pdf.due_next_run`` 1). Run 2: the one PDF, no HTML. (The fake lane's HTML
    is never converted here, so papers 1 and 3 stay "waiting".)"""
    arxiv = FakeLane.answering("arxiv", {(sid(2), "html"): "dead"}, default=b"<html>latexml</html>")
    res, e = go_with(rows3(), tmp_path, {"arxiv": arxiv}, opts("on_miss"), wants=WANTS)
    assert sorted(arxiv.keys()) == [(sid(1), "html"), (sid(2), "html"), (sid(3), "html")]
    assert not res.scope_complete
    assert e.counts["arxiv_pdf.due_next_run"] == 1 and e.counts["arxiv_pdf.miss.html_failed_permanent"] == 1
    by = {r["doc_id"]: r for r in res.rows}
    assert pdfs(by[sid(2)]) == {sid(2): ("not_attempted", "limit", "arxiv")}
    assert pdfs(by[sid(1)]) == {sid(1): ("skipped", "not_requested", "arxiv")}
    n = len(arxiv.calls)
    res2, e2 = go_with(merge(rows3(), res), tmp_path, {"arxiv": arxiv}, opts("on_miss"), wants=WANTS)
    assert arxiv.keys()[n:] == [(sid(2), "full_text")]
    assert res2.scope_complete and e2.counts["arxiv_pdf.due_next_run"] == 0


def test_absent_option_is_the_old_behaviour(tmp_path: Path) -> None:
    """No ``arxiv_pdf``: the same calls and rows as before the option (PDF and HTML of every id)."""
    a1 = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 x")
    res, e = go_with(rows3(), tmp_path, {"arxiv": a1}, opts(None), wants=WANTS)
    a2 = FakeLane.answering("arxiv", {}, default=b"%PDF-1.7 x")
    res2, _ = go_with(rows3(), tmp_path / "always", {"arxiv": a2}, opts("always"), wants=WANTS)
    assert sorted(a1.keys()) == sorted(a2.keys()) == sorted(
        (sid(i), r) for i in (1, 2, 3) for r in ("full_text", "html"))
    assert [r["content"]["roles"] for r in res.rows] == [r["content"]["roles"] for r in res2.rows]
    assert not any(k.startswith("arxiv_pdf.") for k in e.counts)


# ================================================================ runner cases (the real arXiv lane)
from tests.test_acquire_lanes import (  # noqa: E402 — the runner helpers, as test_acquire_structured imports them
    NOW,
    aid_of,
    arxiv_row,
    atom,
    by_role,
    checks_ok,
    go,
    snaps_of,
    store,
    view,
)
from tests.test_acquire_structured import HTML_URL, LATEXML, needs_fetchpdf, pdf_v  # noqa: E402

H = dt.timedelta(hours=1)
#: An acceptable LaTeXML page (above the lane's size floor) whose text the quality gate fails (too short).
THIN = (b'<html><body><article class="ltx_document"><p class="ltx_p">Only a stub.</p>'
        + b"<!-- " + b"x" * ARXIV_HTML_MIN_BYTES + b" --></article></body></html>")


def run_plan(mode: str) -> Plan:
    return Plan.model_validate({
        "plan": "test-arxiv-pdf", "schema_version": 2,
        "acquire": {"wants": {"arxiv": ["full_text", "html"], "openalex": ["full_text"]},
                    "convert": {"config": "pymupdf4llm-v1"}},
        "stages": [{"stage": "acquire", "options": {"allow_degraded_convert": True, "structured": dict(ON),
                                                    "arxiv_pdf": mode}}]})


def long_pdf() -> bytes:
    """A v1 PDF whose text is over twice the LaTeXML fixture's (~9.9k chars): its HTML is ``too_short``."""
    fitz = pytest.importorskip("fitz", reason="pymupdf (fitz) not installed")
    doc = fitz.open()
    for page in range(14):
        doc.new_page().insert_text((72, 72), "\n".join(f"v1 page {page} line {i}: born-digital text of the paper."
                                                        for i in range(45)), fontsize=9)
    data = doc.tobytes(expand=255)
    doc.close()
    return data


def world(fake: Any, papers: dict[int, tuple[bytes, int, bytes]]) -> None:
    """Per paper ``i``: (HTML body, its HTTP status, the PDF), all at v1; Atom answers for every subset of
    the ids (the history sweeps the ids it needs, which differ from run to run)."""
    for n in range(1, len(papers) + 1):
        for ids in itertools.combinations(sorted(papers), n):
            atom(fake, dict.fromkeys(ids, 1))
    for i, (html, status, pdf) in papers.items():
        fake.add(f"https://arxiv.org/pdf/{aid_of(i)}v1", body=pdf)
        fake.add(HTML_URL.format(aid=aid_of(i), v=1), status=status, body=html,
                 headers={"Content-Type": "text/html; charset=utf-8"})


def asked(fake: Any, n: int, kind: str) -> list[str]:
    """The ``/pdf/`` or ``/html/`` URLs asked since call ``n``."""
    return sorted(u for u in fake.urls()[n:] if f"arxiv.org/{kind}/" in u)


def pdf_url(i: int) -> str:
    return f"https://arxiv.org/pdf/{aid_of(i)}v1"


def current(root: Path) -> dict[str, tuple[str | None, str]]:
    return {d: (r["content"]["current_role"], r["content"]["current_text_status"]) for d, r in view(root).items()}


@pytest.mark.slow
@needs_fetchpdf
def test_on_miss_then_always_through_the_arxiv_lane(tmp_path: Path) -> None:
    """Four arXiv papers under ``on_miss``: (1) HTML available, (2) arXiv's 404, (3) an acceptable page
    whose text fails the quality gate, (4) HTML available but under half its PDF's length.
    Run 1 asks the four HTMLs and no PDF; the misses (2, 3) make their PDFs due after ``quality``; the
    run is scope-incomplete. Run 2 fetches exactly those two PDFs and no HTML; their PDF is current.
    Then ``always``: run 3 fetches only the PDFs of 1 and 4, asks no HTML, and moves the pointer only
    by the HTML-before-PDF rule: 1 keeps its HTML, 4's is now ``too_short`` against its PDF."""
    from tests.fakes.http import FakeHttp

    fake = FakeHttp()
    world(fake, {1: (LATEXML, 200, pdf_v(1)), 2: (b"No HTML", 404, pdf_v(1)), 3: (THIN, 200, pdf_v(1)),
                 4: (LATEXML, 200, long_pdf())})
    root = store(tmp_path, [arxiv_row(i) for i in (1, 2, 3, 4)])

    rec = go(root, fake, NOW, plan=run_plan("on_miss"))
    assert asked(fake, 0, "pdf") == [] and len(asked(fake, 0, "html")) == 4
    assert not rec.scope_complete and rec.counts["arxiv_pdf.due_next_run"] == 2
    assert rec.counts["arxiv_pdf.miss.html_empty"] == 1 and rec.counts["arxiv_pdf.miss.quality"] == 1
    rows = view(root)
    assert {d: (by_role(r)["full_text"]["status"], by_role(r)["full_text"]["error_class"]) for d, r in rows.items()} == {
        sid(1): ("skipped", "not_requested"), sid(2): ("not_attempted", "limit"),
        sid(3): ("not_attempted", "limit"), sid(4): ("skipped", "not_requested")}
    assert by_role(rows[sid(1)])["full_text"]["error"] == "arxiv_pdf: on_miss, the arXiv HTML is the text"
    assert current(root) == {sid(1): ("html", "ok"), sid(2): (None, "no_raw"), sid(3): (None, "no_raw"),
                             sid(4): ("html", "ok")}
    checks_ok(root)

    n = len(fake.urls())
    rec = go(root, fake, NOW + H, plan=run_plan("on_miss"))
    assert asked(fake, n, "pdf") == [pdf_url(2), pdf_url(3)] and asked(fake, n, "html") == []
    assert rec.scope_complete and rec.counts["arxiv_pdf.due_next_run"] == 0
    assert current(root) == {sid(1): ("html", "ok"), sid(2): ("full_text", "ok"), sid(3): ("full_text", "ok"),
                             sid(4): ("html", "ok")}
    checks_ok(root)

    before = view(root)
    n = len(fake.urls())
    rec = go(root, fake, NOW + 2 * H, plan=run_plan("always"))
    assert asked(fake, n, "pdf") == [pdf_url(1), pdf_url(4)] and asked(fake, n, "html") == []
    assert rec.counts["arxiv_pdf.requested"] == 2 and rec.scope_complete
    after = view(root)
    assert current(root) == {sid(1): ("html", "ok"), sid(2): ("full_text", "ok"), sid(3): ("full_text", "ok"),
                             sid(4): ("full_text", "ok")}
    assert rec.counts["structured.html.too_short"] == 1
    for i in (1, 2, 3, 4):  # the HTML snapshots and roles are as they were: nothing re-fetched
        assert snaps_of(after[sid(i)], "html") == snaps_of(before[sid(i)], "html")
        assert by_role(after[sid(i)])["html"] == by_role(before[sid(i)])["html"]
    for i in (2, 3):  # the on_miss PDFs are not fetched again
        assert snaps_of(after[sid(i)], "full_text") == snaps_of(before[sid(i)], "full_text")
    checks_ok(root)


def openalex_preprint(i: int) -> Row:
    """An arXiv paper found only through OpenAlex: its member is keyed by the arXiv DOI."""
    row = arxiv_row(i)
    m = row["identity"]["members"][0]
    m.update(channel="openalex", key_kind="doi", key_value=f"10.48550/arXiv.{aid_of(i)}")
    row["identity"]["channels"] = ["openalex"]
    row["identity"]["keys"] = [{"kind": "doi", "value": m["key_value"], "source_id": m["source_id"]}]
    return row


@pytest.mark.slow
@needs_fetchpdf
def test_never_then_always_through_the_arxiv_lane(tmp_path: Path) -> None:
    """``never`` over an arXiv paper with HTML (1), one without (2) and an OpenAlex-found preprint with
    HTML (3): no PDF is asked, the scope is complete, the HTMLs are current (2 has no text). Then
    ``always`` fetches the three PDFs (3's through the arXiv lane), asks no HTML; 1 and 3 keep their
    HTML (not ``too_short``), 2's PDF becomes current."""
    from tests.fakes.http import FakeHttp

    fake = FakeHttp()
    world(fake, {1: (LATEXML, 200, pdf_v(1)), 2: (b"", 404, pdf_v(1)), 3: (LATEXML, 200, pdf_v(1))})
    root = store(tmp_path, [arxiv_row(1), arxiv_row(2), openalex_preprint(3)])

    rec = go(root, fake, NOW, plan=run_plan("never"))
    assert asked(fake, 0, "pdf") == [] and len(asked(fake, 0, "html")) == 3
    assert rec.scope_complete and rec.counts["arxiv_pdf.not_requested"] == 3
    assert {d: by_role(r)["full_text"]["error"] for d, r in view(root).items()} == dict.fromkeys(
        (sid(1), sid(2), sid(3)), "arxiv_pdf: never")
    assert current(root) == {sid(1): ("html", "ok"), sid(2): (None, "no_raw"), sid(3): ("html", "ok")}
    checks_ok(root)
    n = len(fake.urls())
    rec = go(root, fake, NOW + H, plan=run_plan("never"))
    assert asked(fake, n, "pdf") == asked(fake, n, "html") == [] and rec.scope_complete

    before = view(root)
    n = len(fake.urls())
    rec = go(root, fake, NOW + 2 * H, plan=run_plan("always"))
    assert asked(fake, n, "pdf") == [pdf_url(1), pdf_url(2), pdf_url(3)] and asked(fake, n, "html") == []
    after = view(root)
    assert by_role(after[sid(3)])["full_text"]["lane"] == "arxiv"
    assert current(root) == {sid(1): ("html", "ok"), sid(2): ("full_text", "ok"), sid(3): ("html", "ok")}
    for i in (1, 2, 3):  # no HTML episode: attempts, last attempt, validator and status as they were
        assert unstamped(by_role(after[sid(i)])["html"]) == unstamped(by_role(before[sid(i)])["html"])
    # the history's "no HTML" memory (180 days from the one attempt) now stamps 2's role; it asked nothing
    assert by_role(after[sid(2)])["html"]["retry_after"] == NOW + dt.timedelta(days=180)
    checks_ok(root)


@pytest.mark.slow
@needs_fetchpdf
def test_an_html_only_paper_is_listed_and_still_learns_a_new_version(tmp_path: Path) -> None:
    """D807: the history lists an HTML-only paper's Atom answer in ``content.revisions`` (as the PDF's), and a
    listed paper's due ``html`` role still joins the sweep: past the recheck, v2 is learnt, listed and fetched."""
    from tests.fakes.http import FakeHttp

    fake = FakeHttp()
    world(fake, {1: (LATEXML, 200, pdf_v(1))})
    root = store(tmp_path, [arxiv_row(1)])
    go(root, fake, NOW, plan=run_plan("never"))
    assert [r["revision"] for r in view(root)[sid(1)]["content"]["revisions"]] == ["v1"]
    atom(fake, {1: 2})
    fake.add(HTML_URL.format(aid=aid_of(1), v=2), body=LATEXML + b"<!-- v2 -->", headers={"Content-Type": "text/html; charset=utf-8"})
    n = len(fake.urls())
    go(root, fake, NOW + 8 * 24 * H, plan=run_plan("never"))
    assert asked(fake, n, "html") == [HTML_URL.format(aid=aid_of(1), v=2)] and asked(fake, n, "pdf") == []
    row = view(root)[sid(1)]
    assert [r["revision"] for r in row["content"]["revisions"]] == ["v1", "v2"]
    assert [s["revision"] for s in snaps_of(row, "html")] == ["v1", "v2"]


def unstamped(r: Row) -> Row:
    """A role without ``retry_after`` (the history's re-check stamp, which changes without a request)."""
    return {k: v for k, v in r.items() if k != "retry_after"}
