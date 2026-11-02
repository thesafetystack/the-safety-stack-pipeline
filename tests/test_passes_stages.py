"""``acquire``'s real lanes and its convert substage across runs minutes apart (round 4, passes:
"idempotent on successful entries"; the two-pass build itself is ``tests/test_passes_build.py``, whose
``acquire`` lane is a stand-in). Each case runs ``acquire`` through the runner over a per-test store
on the lane fakes of ``tests/test_acquire_lanes.py`` (``FakeHttp`` as the lanes' transport,
``FakeFetchpdf`` behind the OA lane), then again two minutes later, as the next pass of ``stack build
--passes`` would: the rows whose roles ended ``ok`` make no request and are not converted again; a
role that failed transiently is retried at once (``FIRST_RETRY``) and the run after that is quiet.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from stack.plan import Plan
from stack.stage_runner import run_stage
from tests.fakes.http import FakeHttp
from tests.fakes.lanes import FakeFetchpdf
from tests.synth.records import sdir
from tests.synth.rows import sid
from tests.test_acquire_lanes import (
    JATS,
    NOW,
    PAGE,
    aid_of,
    atom,
    by_role,
    checks_ok,
    doi_of,
    fake_web,
    full_role,
    go,
    no_capture,
    oa_go,
    oa_plan,
    oa_row,
    page_role,
    page_row,
    store,
    url_of,
    view,
)
from tests.test_acquire_lanes import arxiv_row as arxiv_doc
from workflows.acquire import lanes
from workflows.acquire.lanes.arxiv import LIMIT_GIVE_UP

pytestmark = [pytest.mark.slow, pytest.mark.e2e]  # runner-driven store

PASS = dt.timedelta(minutes=2)  # the gap to the next pass


def big_pdf() -> bytes:
    """A born-digital PDF above the lanes' ``PDF_MIN_BYTES`` (``stack.http.looks_like_pdf``)."""
    fitz = pytest.importorskip("fitz", reason="pymupdf (fitz) not installed")
    doc = fitz.open()
    for p in range(30):
        doc.new_page().insert_text((72, 72), "\n".join(f"Page {p} line {i}: born-digital text about oversight."
                                                       for i in range(50)), fontsize=8)
    data = doc.tobytes()
    doc.close()
    return data


def test_http_ok_page_is_not_refetched_and_a_5xx_is_retried_in_the_next_run(tmp_path: Path) -> None:
    ok_url, bad_url = url_of(1), url_of(2)
    root = store(tmp_path, [page_row(1, ok_url), page_row(2, bad_url)])
    fake = fake_web("blog.test")
    fake.add(ok_url, body=PAGE.format("A post."))
    fake.add(bad_url, status=503)
    no_capture(fake, bad_url)
    rec1 = go(root, fake, NOW)
    bad = page_role(view(root)[sid(2)])
    assert (bad["status"], bad["attempts"], bad["retry_after"]) == ("failed_transient", 1, NOW)  # due at once
    assert rec1.counts["lane_calls"] == 2

    fake.add(bad_url, body=PAGE.format("Back again."))
    n = len(fake.calls)
    rec2 = go(root, fake, NOW + PASS)
    assert ok_url not in fake.urls()[n:] and fake.urls()[n:].count(bad_url) == 1
    assert (rec2.counts["lane_calls"], rec2.counts["not_due"]) == (1, 1)
    assert (page_role(view(root)[sid(2)])["status"], page_role(view(root)[sid(2)])["attempts"]) == ("ok", 0)

    n = len(fake.calls)
    rec3 = go(root, fake, NOW + 2 * PASS)
    assert fake.calls[n:] == [] and (rec3.counts["lane_calls"], rec3.counts["rows_written"]) == (0, 0)
    assert rec3.counts["not_due"] == 2
    checks_ok(root)


def test_http_second_failure_backs_off_two_hours(tmp_path: Path) -> None:
    """Only the first retry is immediate: a second consecutive failure is due 2 h later, so the run
    after it makes no request."""
    url = url_of(1)
    root = store(tmp_path, [page_row(1, url)])
    fake = fake_web("blog.test")
    fake.add(url, status=503)
    no_capture(fake, url)
    go(root, fake, NOW)
    go(root, fake, NOW + PASS)
    role = page_role(view(root)[sid(1)])
    assert (role["status"], role["attempts"], role["retry_after"]) == (
        "failed_transient", 2, NOW + PASS + dt.timedelta(hours=2))
    n = len(fake.calls)
    go(root, fake, NOW + 2 * PASS)
    assert fake.calls[n:] == []


def test_arxiv_lane_with_convert_makes_no_request_and_no_conversion_next_run(tmp_path: Path) -> None:
    """The real arXiv lane (Atom sweep, PDF) and a real ``pymupdf4llm-v1`` conversion:
    the next run makes no request (the ``ok`` role's next check is a week away, its version held)
    and converts nothing (``ok`` text at the pin is final); nothing is written."""
    root = store(tmp_path, [arxiv_doc(1)])
    fake = FakeHttp()
    atom(fake, {1: 1})
    fake.add(f"https://arxiv.org/pdf/{aid_of(1)}v1", body=big_pdf())
    plan = Plan.model_validate({"plan": "test-lanes", "schema_version": 2,
                                "acquire": {"wants": {"arxiv": ["full_text"]}, "convert": {"config": "pymupdf4llm-v1"}},
                                "stages": [{"stage": "acquire", "options": {"allow_degraded_convert": True}}]})
    rec1 = go(root, fake, NOW, plan=plan)
    assert rec1.counts.get("convert.ok") == 1 and rec1.counts.get("arxiv.downloads") == 1, rec1.counts
    assert view(root)[sid(1)]["content"]["current_text_status"] == "ok"
    n = len(fake.calls)
    rec2 = go(root, fake, NOW + PASS, plan=plan)
    assert fake.calls[n:] == []
    assert not {k for k in rec2.counts if k.startswith("convert.")
                and k not in ("convert.entry_tool_absent", "convert.degraded_allowed")}
    assert (rec2.counts["lane_calls"], rec2.counts["rows_written"], rec2.counts["arxiv.history_fresh"]) == (0, 0, 1)
    assert full_role(view(root)[sid(1)])["status"] == "ok"
    checks_ok(root)


def test_oa_lane_ok_roles_are_not_retrieved_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_acquire_convert import pdf_bytes

    monkeypatch.setenv("STACK_MAILTO", "lane-tests@example.invalid")
    root = store(tmp_path, [oa_row(1)])
    fp = FakeFetchpdf({doi_of(1): [(JATS, "https://europepmc.test/jats/1", "europepmc"),
                                   (pdf_bytes(), "https://journal.test/1.pdf", "unpaywall")]})
    oa_go(root, fp, NOW, oa_plan("pymupdf4llm-v1"))
    assert {k: r["status"] for k, r in by_role(view(root)[sid(1)]).items()} == {"full_text": "ok", "xml": "ok"}
    n = len(fp.calls)
    rec2 = oa_go(root, fp, NOW + PASS, oa_plan("pymupdf4llm-v1"))
    assert fp.calls[n:] == [] and (rec2.counts["lane_calls"], rec2.counts["rows_written"]) == (0, 0)
    checks_ok(root)


def test_lane_halt_charges_nothing_and_the_next_run_fetches(tmp_path: Path) -> None:
    """A provider halt (the arXiv lane's 429 on the PDF URL, D107) is a run-level stop: the role is not
    charged (``attempts`` 0, ``not_attempted[halted]``), so the next run fetches it. (An Atom 429 no
    longer halts, D411: ``test_acquire_lanes.test_arxiv_atom_slow_down_charges_its_roles_and_the_lane_goes_on``.)
    Since D584 the lane halts at the fourth limited answer in a row (each one the session's four sends), so
    16 injected 429s; the 8 before D584 are two answers, which the lane now waits out."""
    root = store(tmp_path, [arxiv_doc(1)])
    fake = FakeHttp()
    ids = aid_of(1)

    atom(fake, {1: 1})
    fake.add(f"https://arxiv.org/pdf/{ids}v1", body=big_pdf())
    fake.inject(f"https://arxiv.org/pdf/{ids}v1", status=429, times=4 * LIMIT_GIVE_UP)
    plan = Plan.model_validate({"plan": "test-lanes", "schema_version": 2,
                                "acquire": {"wants": {"arxiv": ["full_text"]}}, "stages": [{"stage": "acquire"}]})
    with lanes.using(now=NOW, transport=fake, sleep=lambda _s: None):
        rec1 = run_stage(root, plan, "acquire", [sdir("documents")], series="main")
    assert rec1.status == "ok" and rec1.counts.get("halted") == 1 and rec1.counts.get("arxiv.halted") == 1, rec1.counts
    role = full_role(view(root)[sid(1)])
    assert (role["status"], role["attempts"]) == ("not_attempted", 0)
    fake.faults.clear()
    rec2: Any = go(root, fake, NOW + PASS, plan=plan)
    assert rec2.counts.get("arxiv.downloads") == 1, rec2.counts
    assert full_role(view(root)[sid(1)])["status"] == "ok"
