"""``acquire``'s ``convert`` substage (PLAN T3.5; D9, D39; §5.5, §7.9): the ladder in the
RLIMIT_DATA-capped child, the outcome mapping, the cap, the pin, and the PLAN case through the
runner — a converted snapshot, then a newer raw whose conversion crashes three times
(``quarantined[cap_reached]``, ``convert_failed``, ``has_text`` false, the old text never
returned), then a pin change that resets the attempts and re-converts. Runs after the first are
``--only convert`` with trap lanes and a trap transport: zero network.

Crashes and hangs are provoked through the worker's fault switches (``STACK_CONVERT_ABORT``,
``STACK_CONVERT_HANG``), never by patching our modules. The PDF case needs ``pymupdf`` (``fitz``)
and skips cleanly without it; HTML and plain text convert in any venv; the main-content case needs
``trafilatura`` (the ``convert`` group). The ``convert_workers`` tests replace
:func:`~workflows.acquire.convert.convert_one`, the boundary to the child process, with a slow fake
(T3.6-f3b): what they test is the parent's scheduling, not the ladder.
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib.metadata
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import pytest

from stack.checks.verify import run_checks
from stack.conversion.ladder import stamp_tier
from stack.plan import Plan
from stack.query import HAS_TEXT
from stack.schemas.structs import text_blob
from stack.stage_runner import StageError, run_stage
from stack.store.blobs import BlobStore, open_blobs
from stack.store.chain import read_view, resolve
from tests.fakes.lanes import FakeLane, TrapLane, TrapTransport
from tests.synth.records import sdir
from tests.synth.rows import AT, blob, document_row, rid, sid
from tests.synth.world import write_on_head
from workflows.acquire import convert as convert_mod
from workflows.acquire import lanes, scope
from workflows.acquire.convert import (
    CONFIGS,
    ConvertOutcome,
    config_named,
    convert_pin,
    pin_document,
    pin_of,
    run_convert,
)
from workflows.acquire.lanes import FETCH_LANES, LaneEnv
from workflows.acquire.main import acquire_rows, channel_group
from workflows.acquire.quality import Converted

Row = dict[str, Any]
NOW = AT + dt.timedelta(hours=30)
DAY = dt.timedelta(days=1)
CHANNEL = "openai_blog"  # channel group ``url``: an ``ok`` page role is rechecked weekly (D22)
# Long enough to pass the quality gate's fail level for every role (a paper body fails below
# 1,500 chars, round 3, Q).
BODY = "A genuine paragraph of page text, long enough to be complete. " * 30
BLOG_PAGE = Path(__file__).parent / "fixtures" / "convert" / "blog-nav-article.html"


def plan(config: str, wants: dict[str, list[str]] | None = None, **options: Any) -> Plan:
    """A one-stage acquire plan; the convert pin guard (R4-1) is lifted (the suite runs in any venv)
    unless ``allow_degraded_convert`` is passed."""
    options.setdefault("allow_degraded_convert", True)
    return Plan.model_validate({"plan": "test-convert", "schema_version": 2,
                                "acquire": {"convert": {"config": config}, "wants": wants or {CHANNEL: ["page"]}},
                                "stages": [{"stage": "acquire", "options": options}]})


def pdf_bytes() -> bytes:
    fitz = pytest.importorskip("fitz", reason="pymupdf (fitz) not installed: the PDF case is skipped")
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "\n".join(f"Line {i}: Hello PDF world, born-digital text."
                                                   for i in range(40)), fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def pending_row(blobs: BlobStore, i: int, body: bytes, media: str) -> Row:
    """An acquired row whose one raw snapshot holds ``body`` and has never been converted."""
    row = document_row(i, state="acquired")
    (snap,) = row["content"]["snapshots"]
    snap.update(raw=blob(blobs.put(body), len(body), media), text=None,
                text_status=None, text_error_class=None, convert_attempts=0, convert_pin=None)
    return row


def env_for(tmp_path: Path, config: str = "pymupdf4llm-v1", **options: Any) -> LaneEnv:
    acq = plan(config).acquire
    return LaneEnv(run_id=rid(60), now=NOW, blobs=open_blobs(tmp_path), options=options,
                   acquire=acq, convert_pin=convert_pin(acq), counts={})


def snap_of(row: Row) -> Row:
    return row["content"]["snapshots"][-1]


# ---------------------------------------------------------------- the pin
def test_pin_rule() -> None:
    """One pin per config and environment: config name, tier entry and heap cap all enter it;
    an unknown config is refused."""
    a, b = CONFIGS["pymupdf4llm-v1"], CONFIGS["docling-v2"]
    assert (a.tier, a.mem_gb, b.tier, b.mem_gb) == (2, 16.0, 1, 48.0)
    pins = {pin_of(a), pin_of(b), pin_of(a._replace(mem_gb=48.0)), pin_of(a._replace(tier=1))}
    assert len(pins) == 4 and pin_of(a) == pin_of(a)
    assert convert_pin(plan("pymupdf4llm-v1").acquire) == pin_of(a)
    assert convert_pin(Plan.model_validate({"plan": "p", "schema_version": 2, "stages": []}).acquire) is None
    with pytest.raises(StageError, match="not a convert config"):
        config_named("convert-config")


def test_pin_names_the_html_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTML light-path tool is in both configs' pin (T3.6-f3b): its version, or ``absent``
    when not importable, is in the pin document, and installing it or bumping it changes the pin."""
    have = importlib.util.find_spec("trafilatura") is not None
    for cfg in CONFIGS.values():
        assert "trafilatura" in cfg.tools
        assert pin_document(cfg)["tools"]["trafilatura"] == (
            importlib.metadata.version("trafilatura") if have else "absent")
    cfg = CONFIGS["pymupdf4llm-v1"]
    pins = {pin_of(cfg)}
    usable, version = convert_mod._usable, importlib.metadata.version
    monkeypatch.setattr(convert_mod, "_usable", lambda tool: tool == "trafilatura" or usable(tool))
    for fake in ("0.0.1+test", "0.0.2+test"):  # neither the real version nor "absent"
        monkeypatch.setattr(importlib.metadata, "version", lambda dist, v=fake: v if dist == "trafilatura" else version(dist))
        assert pin_document(cfg)["tools"]["trafilatura"] == fake
        pins.add(pin_of(cfg))
    assert len(pins) == 3


# ---------------------------------------------------------------- the ladder in the child
@pytest.mark.slow
@pytest.mark.parametrize("kind", ["pdf", "html", "plain"])
def test_ladder_converts_in_the_child(tmp_path: Path, kind: str) -> None:
    """PDF (pymupdf4llm, else pymupdf raw text), HTML and plain text convert in the child under
    the dev pin: text in ``blobs/text``, ``ok`` with one charged attempt, the conversion's facts
    reported (stamp, PDF page count), the tier's stamp counted."""
    e = env_for(tmp_path)
    body, media, needle, stamps = {
        "pdf": (b"", "application/pdf", "Hello PDF world", ("pymupdf4llm@", "pymupdf@")),
        "html": (f"<html><head><title>T</title></head><body><p>{BODY}</p></body></html>".encode(),
                 "text/html", "genuine paragraph", ("html-min@", "trafilatura@", "fetchpdf-md@")),
        "plain": (BODY.encode(), "text/plain", "genuine paragraph", ("plain@",)),
    }[kind]
    if kind == "pdf":
        body = pdf_bytes()
    row = pending_row(e.blobs, 1, body, media)
    converted: dict[tuple[str, str], Converted] = {}
    run_convert([row], e, converted)
    s = snap_of(row)
    assert (s["text_status"], s["text_error_class"], s["convert_attempts"], s["convert_pin"]) == (
        "ok", None, 1, e.convert_pin)
    assert needle in e.blobs.get(s["text"]["sha256"]).decode()
    (stamp,) = [k.removeprefix("convert.converter.") for k in e.counts if k.startswith("convert.converter.")]
    assert stamp.startswith(stamps), stamp
    assert converted == {(row["doc_id"], s["snapshot_id"]): Converted(stamp, 1 if kind == "pdf" else None, None)}
    assert e.counts["convert.ok"] == 1
    run_convert([row], e)  # converted at the pin: nothing to do
    assert snap_of(row)["convert_attempts"] == 1 and e.counts["convert.ok"] == 1


@pytest.mark.slow
def test_blog_page_converts_to_its_main_content(tmp_path: Path) -> None:
    """A saved-shape blog page (site menu, doubled nav bar, article, footer) converts in the child
    under the dev pin through ``trafilatura``: the text starts at the article, no menu survives.
    (Without trafilatura the stdlib floor keeps every menu, the T3.6 sample finding.)"""
    pytest.importorskip("trafilatura", reason="trafilatura (convert group) not installed")
    e = env_for(tmp_path)
    row = pending_row(e.blobs, 1, BLOG_PAGE.read_bytes(), "text/html")
    run_convert([row], e)
    s = snap_of(row)
    assert (s["text_status"], s["convert_attempts"]) == ("ok", 1)
    (stamp,) = [k.removeprefix("convert.converter.") for k in e.counts if k.startswith("convert.converter.")]
    assert stamp.startswith("trafilatura@"), stamp
    title, _, body = e.blobs.get(s["text"]["sha256"]).decode().partition("\n")
    assert title.startswith("# Calibrated abstention")
    assert body.lstrip().startswith("Calibrated abstention in retrieval agents\nWe introduce calibrated abstention")
    for menu in ("Explore our many areas", "Log in", "Earth observation", "Careers", "All rights reserved"):
        assert menu not in body, menu


@pytest.mark.slow
def test_child_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that dies is ``failed_transient[crashed]`` with its attempt charged; a hang past
    ``convert_timeout_s`` is ``[timeout]``; the third transient failure quarantines and a
    quarantined snapshot is not invoked again at the pin; bytes no PDF tier can read are
    ``failed_permanent[parse_error]``, final at the pin."""
    e = env_for(tmp_path, convert_timeout_s=2)
    row = pending_row(e.blobs, 1, BODY.encode(), "text/plain")
    monkeypatch.setenv("STACK_CONVERT_ABORT", "1")
    run_convert([row], e)
    assert (snap_of(row)["text_status"], snap_of(row)["text_error_class"], snap_of(row)["convert_attempts"]) == (
        "failed_transient", "crashed", 1)
    monkeypatch.delenv("STACK_CONVERT_ABORT")
    monkeypatch.setenv("STACK_CONVERT_HANG", "30")
    run_convert([row], e)
    assert (snap_of(row)["text_status"], snap_of(row)["text_error_class"], snap_of(row)["convert_attempts"]) == (
        "failed_transient", "timeout", 2)
    run_convert([row], e)
    assert (snap_of(row)["text_status"], snap_of(row)["text_error_class"], snap_of(row)["convert_attempts"]) == (
        "quarantined", "cap_reached", 3)
    monkeypatch.delenv("STACK_CONVERT_HANG")
    run_convert([row], e)
    assert snap_of(row)["convert_attempts"] == 3 and snap_of(row)["text_status"] == "quarantined"
    assert (e.counts["convert.failed_transient"], e.counts["convert.quarantined"]) == (2, 1)

    e = env_for(tmp_path)  # the default timeout: importing the PDF tiers can take seconds
    bad = pending_row(e.blobs, 2, b"%PDF-1.4 not really a pdf", "application/pdf")
    run_convert([bad], e)
    run_convert([bad], e)
    s = snap_of(bad)
    if importlib.util.find_spec("fitz") is not None:
        assert (s["text_status"], s["text_error_class"], s["convert_attempts"]) == ("failed_permanent", "parse_error", 1)
    else:  # no PDF tier importable: left pending, never charged
        assert (s["text_status"], s["convert_attempts"], e.counts["convert.no_tool"]) == (None, 0, 2)


# ---------------------------------------------------------------- the PLAN case, through the runner
def view(root: Path) -> dict[str, Row]:
    return {r["doc_id"]: r for r in read_view(resolve(root, sdir("documents"))).to_pylist()}


def has_text(root: Path) -> set[str]:
    con = duckdb.connect()
    con.register("d", read_view(resolve(root, sdir("documents"))))
    return {r[0] for r in con.execute(f"SELECT doc_id FROM d WHERE {HAS_TEXT}").fetchall()}


def by_body(row: Row) -> dict[str, Row]:
    return {s["snapshot_id"]: s for s in row["content"]["snapshots"] if s["kind"] == "raw"}


TRAPS = {name: TrapLane(name) for name in FETCH_LANES}


@pytest.mark.slow
def test_crash_after_success_then_pin_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert channel_group(CHANNEL) == "url"
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # docling-v2 below converts plain text only; belt and braces
    root = tmp_path / "store"
    row = document_row(1, state="triaged")
    row["identity"]["members"][0]["channel"] = row["identity"]["channels"][0] = CHANNEL
    write_on_head(root, "documents", "documents", [row], stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband"])
    d = sid(1)
    v1, v2 = (f"Version {n}. {BODY}".encode() for n in (1, 2))
    http = FakeLane.answering("http", {(d, "page"): [v1, v2]}, media_type="text/plain")
    dev = plan("pymupdf4llm-v1")

    def go(at: dt.datetime, p: Plan = dev, only: list[str] | None = None) -> Any:
        lane_map = {"http": http} if only is None else TRAPS
        with lanes.using(lane_map, now=at, transport=None if only is None else TrapTransport()):
            rec = run_stage(root, p, "acquire", [sdir("documents")], series="main", only=only)
        assert rec.status == "ok", rec.error
        return rec

    # 1: fetched and converted.
    go(NOW)
    c = view(root)[d]["content"]
    old_text = c["current_text_sha256"]
    assert (c["current_text_status"], c["current_text_quality"]) == ("ok", "ok") and has_text(root) == {d}

    # 2: a week later the page changed; the newer raw's conversion crashes (attempt charged).
    monkeypatch.setenv("STACK_CONVERT_ABORT", "1")
    go(NOW + 7 * DAY)
    c = view(root)[d]["content"]
    assert len(by_body(view(root)[d])) == 2
    cur = by_body(view(root)[d])[c["current_snapshot_id"]]
    assert (cur["text_status"], cur["text_error_class"], cur["convert_attempts"]) == ("failed_transient", "crashed", 1)
    assert (c["current_text_status"], c["current_text_sha256"], c["current_text_quality"]) == (
        "convert_failed", None, None)

    # 3, 4: `--only convert`, the page role due again at 4 (retry_after +7 d): no lane, no transport.
    go(NOW + 8 * DAY, only=["convert"])
    rec = go(NOW + 15 * DAY, only=["convert"])
    assert rec.substages["fetch"]["ran"] is False and rec.counts["convert.quarantined"] == 1
    v = view(root)[d]
    cur = by_body(v)[v["content"]["current_snapshot_id"]]
    assert (cur["text_status"], cur["text_error_class"], cur["convert_attempts"]) == ("quarantined", "cap_reached", 3)
    assert (v["content"]["current_text_status"], v["content"]["current_text_sha256"]) == ("convert_failed", None)
    assert old_text not in {v["content"]["current_text_sha256"]} and has_text(root) == set()  # never the old text

    # 5: the child would now succeed, but a quarantined snapshot is final at the pin: nothing to write.
    monkeypatch.delenv("STACK_CONVERT_ABORT")
    rec = go(NOW + 16 * DAY, only=["convert"])
    assert rec.outputs == {} and "convert.ok" not in rec.counts

    # 6: a pin change (another convert config) clears the current candidate's fields, resets its
    # attempts, re-converts it; the superseded snapshot keeps its old-pin fields (§5.5: never consulted).
    rec = go(NOW + 17 * DAY, p=plan("docling-v2"), only=["convert"])
    assert rec.counts["convert.reset"] == 1 and rec.counts["convert.ok"] == 1
    v = view(root)[d]
    pin = convert_pin(plan("docling-v2").acquire)
    cur = by_body(v)[v["content"]["current_snapshot_id"]]
    assert (cur["text_status"], cur["convert_attempts"], cur["convert_pin"]) == ("ok", 1, pin)
    (older,) = [s for k, s in by_body(v).items() if k != v["content"]["current_snapshot_id"]]
    assert (older["text"]["sha256"], older["convert_pin"]) == (old_text, convert_pin(dev.acquire))
    assert v["content"]["convert_pin"] == pin and v["content"]["current_text_status"] == "ok"
    assert "Version 2." in open_blobs(root).get(v["content"]["current_text_sha256"]).decode()
    assert has_text(root) == {d}
    assert http.keys() == [(d, "page"), (d, "page")]  # two fetches, in runs 1 and 2 only
    by = {r.check: r for r in run_checks(root, names=["attempts_monotone", "V-content", "status_pairing",
                                                       "block_ownership", "V-chain-linear"])}
    assert {n: r.status for n, r in by.items()} == dict.fromkeys(by, "ok"), {n: r.items for n, r in by.items()}


# ---------------------------------------------------------------- only the current candidate; stops
def two_raws(blobs: BlobStore, i: int) -> Row:
    """An acquired row with an older raw converted under another pin and a newer pending raw."""
    row = pending_row(blobs, i, f"Newer {i}. {BODY}".encode(), "text/plain")
    (newer,) = row["content"]["snapshots"]
    old_body = f"Older {i}. {BODY}".encode()
    old_raw = blob(blobs.put(old_body), len(old_body), newer["raw"]["media_type"])
    older = {**newer, "snapshot_id": f"{newer['snapshot_id']}-old", "captured_at": AT - DAY, "as_of": AT - DAY,
             "raw": old_raw, "text": text_blob("f" * 64, 64, old_raw),
             "text_status": "ok", "convert_attempts": 1, "convert_pin": "another-pin"}
    row["content"]["snapshots"] = [older, newer]
    return row


def test_only_the_current_candidate_converts(tmp_path: Path) -> None:
    """Of a row with an older raw (converted under another pin) and a newer pending raw, only the
    newer (the pointer's candidate) is converted; the older's conversion fields stay as they were."""
    e = env_for(tmp_path)
    row = two_raws(e.blobs, 1)
    before = dict(row["content"]["snapshots"][0])
    assert run_convert([row], e) is True
    older, newer = row["content"]["snapshots"]
    assert older == before and "convert.reset" not in e.counts
    assert (newer["text_status"], newer["convert_attempts"], newer["convert_pin"]) == ("ok", 1, e.convert_pin)
    assert "Newer 1." in e.blobs.get(newer["text"]["sha256"]).decode()
    assert e.counts["convert.ok"] == 1


@pytest.mark.slow
def test_stops_leave_rows_pending(tmp_path: Path) -> None:
    """``options.convert_max_rows`` through the runner (``--only convert``, trap lanes; ``max_rows`` capped
    convert too before D483, so this case used it): one row converted,
    the other left ``convert_pending`` and uncharged, the run ``ok`` with ``scope_complete = false``;
    the next run finishes it. ``budget.max_minutes`` exhausted before the first row converts nothing."""
    root = tmp_path / "store"
    blobs = open_blobs(root)
    rows = [pending_row(blobs, i, f"Row {i}. {BODY}".encode(), "text/plain") for i in (1, 2)]
    write_on_head(root, "documents", "documents", rows, stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband", "content"])
    wants = {"arxiv": ["full_text"]}

    def go(**options: Any) -> Any:
        with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
            rec = run_stage(root, plan("pymupdf4llm-v1", wants, **options), "acquire",
                            [sdir("documents")], series="main", only=["convert"])
        assert rec.status == "ok", rec.error
        return rec

    rec = go(convert_max_rows=1)
    assert not rec.scope_complete and rec.counts["convert.stopped.limit"] == 1 and rec.counts["convert.ok"] == 1
    assert rec.substages["convert"]["scope_complete"] is False
    v = view(root)
    a, b = (v[sid(i)]["content"] for i in (1, 2))
    assert a["current_text_status"] == "ok"
    assert (b["current_text_status"], b["snapshots"][0]["convert_attempts"]) == ("convert_pending", 0)
    rec = go(convert_max_rows=1)
    assert rec.scope_complete and rec.counts["convert.ok"] == 1 and "convert.stopped.limit" not in rec.counts
    assert view(root)[sid(2)]["content"]["current_text_status"] == "ok"

    e = env_for(tmp_path)
    row = pending_row(e.blobs, 3, BODY.encode(), "text/plain")
    assert run_convert([row], e, max_minutes=0) is False
    assert e.counts["convert.stopped.budget"] == 1 and "convert.ok" not in e.counts
    assert snap_of(row)["convert_attempts"] == 0


# ---------------------------------------------------------------- convert_workers
class SlowChild:
    """Stands in for :func:`convert_one` (the child boundary): checks the item was charged before
    it runs, holds it ``delay`` seconds, records the peak number of items in flight."""

    def __init__(self, rows: list[Row], pin: str, delay: float) -> None:
        self.by_raw = {r["doc_id"]: snap_of(r) for r in rows}
        self.pin, self.delay = pin, delay
        self.lock = threading.Lock()
        self.now = self.peak = self.calls = 0

    def __call__(self, raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
        doc_id = raw.decode().split("|", 1)[0]
        snap = self.by_raw[doc_id]
        assert (snap["convert_attempts"], snap["convert_pin"]) == (1, self.pin)  # charged first
        with self.lock:
            self.now += 1
            self.calls += 1
            self.peak = max(self.peak, self.now)
        time.sleep(self.delay)
        with self.lock:
            self.now -= 1
        return ConvertOutcome("ok", markdown=f"Converted {raw.decode()}", converter="fake@1+tier2")


def worker_rows(blobs: BlobStore, n: int) -> list[Row]:
    """``n`` pending rows whose raw bodies name their ``doc_id`` (for :class:`SlowChild`)."""
    return [pending_row(blobs, i, f"{sid(i)}|{BODY}".encode(), "text/plain") for i in range(1, n + 1)]


@pytest.mark.slow
def test_convert_workers_run_children_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``convert_workers = 3`` over six rows: three children in flight at once, and the rows,
    verdicts and counts (but the clocks) are those of one worker."""
    results = {}
    for workers in (1, 3):
        e = env_for(tmp_path / str(workers), convert_workers=workers)
        rows = worker_rows(e.blobs, 6)
        child = SlowChild(rows, e.convert_pin, delay=0.3)
        monkeypatch.setattr(convert_mod, "convert_one", child)
        converted: dict[tuple[str, str], Converted] = {}
        assert run_convert(rows, e, converted) is True
        assert child.calls == 6
        clocks = {k: e.counts.pop(k) for k in ("convert.seconds", "convert.wall_seconds")}
        results[workers] = (child.peak, copy.deepcopy(rows), converted, e.counts, clocks)
    (peak1, rows1, q1, c1, _), (peak3, rows3, q3, c3, clocks3) = results[1], results[3]
    assert (peak1, peak3) == (1, 3)
    assert rows3 == rows1 and q3 == q1 and c3 == c1
    c1.pop("convert.entry_tool_absent", None)  # pymupdf4llm is not in every venv
    assert c1 == {"convert.ok": 6, "convert.converter.fake@1+tier2": 6}
    assert all(snap_of(r)["text_status"] == "ok" for r in rows3)
    assert clocks3["convert.seconds"] >= 2 * clocks3["convert.wall_seconds"]  # ≈ 1.8 s of items in ≈ 0.6 s


def test_convert_workers_stops_end_submission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With three workers a budget stop ends submission: the three items in flight finish and are
    applied, the five never submitted stay pending and uncharged, and ``run_convert`` returns
    ``False``; ``max_rows`` caps the charged rows exactly as with one worker."""
    e = env_for(tmp_path / "budget", convert_workers=3)
    rows = worker_rows(e.blobs, 8)
    monkeypatch.setattr(convert_mod, "convert_one", SlowChild(rows, e.convert_pin, delay=1.0))
    assert run_convert(rows, e, max_minutes=0.5 / 60, started=time.monotonic()) is False
    assert [snap_of(r)["convert_attempts"] for r in rows] == [1, 1, 1, 0, 0, 0, 0, 0]
    assert [snap_of(r)["text_status"] for r in rows] == ["ok"] * 3 + [None] * 5
    assert (e.counts["convert.ok"], e.counts["convert.stopped.budget"]) == (3, 1)

    e = env_for(tmp_path / "limit", convert_workers=3)
    rows = worker_rows(e.blobs, 6)
    monkeypatch.setattr(convert_mod, "convert_one", SlowChild(rows, e.convert_pin, delay=0.1))
    assert run_convert(rows, e, max_rows=4) is False
    assert [snap_of(r)["text_status"] for r in rows] == ["ok"] * 4 + [None] * 2
    assert (e.counts["convert.ok"], e.counts["convert.stopped.limit"]) == (4, 1)


@pytest.mark.parametrize("bad", [0, -1, "2", 1.5, True])
def test_convert_workers_must_be_a_positive_int(tmp_path: Path, bad: Any) -> None:
    e = env_for(tmp_path, convert_workers=bad)
    with pytest.raises(StageError, match="convert_workers"):
        run_convert(worker_rows(e.blobs, 1), e)


# ---------------------------------------------------------------- convert_threads (T3.x-f4)
ORT_STUB = '''
class SessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0


class InferenceSession:
    made = []

    def __init__(self, path_or_bytes, sess_options=None, providers=None):
        so = sess_options or SessionOptions()
        InferenceSession.made.append((path_or_bytes, so.intra_op_num_threads, so.inter_op_num_threads))
'''


def ort_stub(tmp_path: Path) -> Path:
    """A directory holding a stand-in ``onnxruntime`` (the venv may or may not have the real one): sessions record
    the thread counts they were created with."""
    d = tmp_path / "ort-stub"
    d.mkdir()
    (d / "onnxruntime.py").write_text(ORT_STUB)
    return d


@pytest.mark.parametrize("threads", [2, 3])
def test_the_child_sees_the_thread_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threads: int) -> None:
    """The real child (``stack/conversion/worker.py``) caps every native pool before it imports a
    converter: each :data:`THREAD_ENV` variable reads ``threads`` in the child, and onnxruntime
    (here a stand-in on the child's path) is capped; the item still converts."""
    from stack.conversion.worker import THREAD_ENV

    report = tmp_path / "report.json"
    monkeypatch.setenv("STACK_CONVERT_REPORT", str(report))
    monkeypatch.setenv("PYTHONPATH", str(ort_stub(tmp_path)))
    for key in THREAD_ENV:
        monkeypatch.setenv(key, "64")  # the parent's value must not leak through
    out = convert_mod.convert_one(BODY.encode(), "text/plain", mem_gb=0, timeout=60, threads=threads)
    assert out.status == "ok"
    seen = json.loads(report.read_text())
    assert seen == {"threads": threads, "env": dict.fromkeys(THREAD_ENV, str(threads)), "onnxruntime": "capped"}


def test_onnxruntime_sessions_get_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session created without thread counts gets ``intra_op = n``, ``inter_op = 1``; explicit
    counts are kept; capping twice wraps once."""
    from stack.conversion import worker

    monkeypatch.syspath_prepend(str(ort_stub(tmp_path)))
    # setitem first so teardown restores the entry as it was, absent included: a bare
    # delitem(raising=False) records nothing for an absent key, and a later delitem of the stub
    # would then make teardown put the stub back (later imports of onnxruntime, e.g. pymupdf.layout
    # via the scheduled-run preflight, got the stand-in).
    monkeypatch.setitem(sys.modules, "onnxruntime", sys.modules.get("onnxruntime"))  # type: ignore[arg-type]
    monkeypatch.delitem(sys.modules, "onnxruntime")
    assert worker._cap_onnxruntime(2) == "capped" and worker._cap_onnxruntime(2) == "capped"
    import onnxruntime as ort  # the stand-in

    ort.InferenceSession("a.onnx")
    so = ort.SessionOptions()
    so.intra_op_num_threads, so.inter_op_num_threads = 4, 3
    ort.InferenceSession("b.onnx", so)
    assert ort.InferenceSession.made == [("a.onnx", 2, 1), ("b.onnx", 4, 3)]


def test_convert_threads_reaches_every_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``options.convert_threads`` is handed to each child; absent, the default is 2."""
    seen: list[int] = []

    def child(raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
        seen.append(kw["threads"])
        return ConvertOutcome("ok", markdown="text", converter="fake@1+tier2")

    monkeypatch.setattr(convert_mod, "convert_one", child)
    for opts, want in (({}, 2), ({"convert_threads": 3, "convert_workers": 2}, 3)):
        seen.clear()
        e = env_for(tmp_path / str(want), **opts)
        assert run_convert(worker_rows(e.blobs, 3), e) is True
        assert seen == [want] * 3


@pytest.mark.parametrize("bad", [0, -1, "2", 1.5, True])
def test_convert_threads_must_be_a_positive_int(tmp_path: Path, bad: Any) -> None:
    e = env_for(tmp_path, convert_threads=bad)
    with pytest.raises(StageError, match="convert_threads"):
        run_convert(worker_rows(e.blobs, 1), e)


# ---------------------------------------------------------------- a garbled PDF's second conversion (D8)
LADDER_TIER2 = "pymupdf4llm@1.28.0+tier2"


def garbled_row(blobs: BlobStore, i: int, pin: str | None, converter: str = LADDER_TIER2,
                error_class: str = "quality_garbled") -> Row:
    """An acquired row whose PDF converted at ``pin`` and failed the quality gate (``error_class``),
    its verdict made from the text of ``converter``."""
    row = pending_row(blobs, i, b"%PDF-1.4 a paper whose fonts map to U+FFFD", "application/pdf")
    text = ("\ufffd" * 40 + BODY).encode()
    snap = snap_of(row)
    snap.update(text=text_blob(blobs.put(text), len(text), snap["raw"]), text_status="failed_permanent",
                text_error_class=error_class, convert_attempts=1, convert_pin=pin,
                quality={"version": 2, "bucket": "failed", "flags": ["replacement"], "fails": ["replacement"],
                         "gate_digest": "g", "converter": converter, "tier": stamp_tier(converter),
                         "metrics": {}})
    return row


def test_a_garbled_pdf_is_converted_again_from_its_raw_text_once(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A PDF whose ladder text failed ``quality_garbled`` at the pin (fonts without a Unicode map:
    U+FFFD per glyph) is converted once more, by the raw-text tier alone (``pdf_raw``): charged
    (``convert_attempts`` 2), ``ok`` with the tier-3 stamp; the row is in scope for ``convert``
    without any other clause. Not again once a verdict was made from raw text, nor for another
    quality failure; an ordinary PDF still takes the ladder."""
    e = env_for(tmp_path)
    seen: list[tuple[bytes, bool]] = []

    def child(raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
        seen.append((raw, kw.get("pdf_raw", False)))
        tier = "pymupdf@1.28.0+tier3" if kw.get("pdf_raw") else LADDER_TIER2
        return ConvertOutcome("ok", markdown=f"Converted. {BODY}", converter=tier, pages=9)

    monkeypatch.setattr(convert_mod, "convert_one", child)
    retry = garbled_row(e.blobs, 1, e.convert_pin)
    done = garbled_row(e.blobs, 2, e.convert_pin, converter="pymupdf@1.28.0+tier3")
    other = garbled_row(e.blobs, 3, e.convert_pin, error_class="quality_too_short")
    fresh = pending_row(e.blobs, 4, b"%PDF-1.4 an ordinary paper", "application/pdf")
    assert [convert_mod.awaits_text(r, e.convert_pin) for r in (retry, done, other, fresh)] == [True, False, False, True]
    assert "convert" in scope.reasons(retry, NOW, True, pin=e.convert_pin)
    assert "convert" not in scope.reasons(done, NOW, True, pin=e.convert_pin)
    converted: dict[tuple[str, str], Converted] = {}
    assert run_convert([retry, done, other, fresh], e, converted) is True
    assert [flag for _raw, flag in seen] == [True, False]
    s = snap_of(retry)
    assert (s["text_status"], s["text_error_class"], s["convert_attempts"], s["quality"]) == ("ok", None, 2, None)
    assert converted[(retry["doc_id"], s["snapshot_id"])].converter == "pymupdf@1.28.0+tier3"
    assert snap_of(done)["text_status"] == snap_of(other)["text_status"] == "failed_permanent"
    assert e.counts["convert.garbled_retry"] == 1 and e.counts["convert.ok"] == 2


# ---------------------------------------------------------------- convert's own stops (D392, e2e-fix)
def inband_row(blobs: BlobStore, i: int) -> Row:
    """A triaged forum post whose in-band body is raw HTML only (it waits for ``convert``, D10), as a
    LessWrong/AF post from the GraphQL collectors: no fetch."""
    row = document_row(i, state="triaged")
    row["identity"]["members"][0].update(channel="lesswrong", key_kind="lesswrong", key_value=f"post{i}")
    row["identity"]["channels"] = ["lesswrong"]
    body = f"<html><body><p>{sid(i)}|{BODY}</p></body></html>".encode()
    row["inband"]["entries"] = [{"source_id": sid(i), "content_role": "post",
                                 "raw": blob(blobs.put(body), len(body), "text/html"),
                                 "text": None, "captured_at": AT}]
    return row


CAP_WANTS = {"arxiv": ["full_text"], "lesswrong": ["post"]}


def fake_child(raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
    return ConvertOutcome("ok", markdown=f"Converted {raw.decode()}", converter="fake@1+tier2")


def capped_world(tmp_path: Path) -> tuple[LaneEnv, list[Row], FakeLane]:
    """Three in-band posts (``sid(1..3)``) and two papers to fetch (``sid(4)``, ``sid(5)``), whose
    fetched bodies are plain text."""
    e = env_for(tmp_path)
    rows = [inband_row(e.blobs, i) for i in (1, 2, 3)] + [document_row(i, state="triaged") for i in (4, 5)]
    lane = FakeLane.answering("arxiv", {(sid(i), "full_text"): f"{sid(i)}|{BODY}".encode() for i in (4, 5)},
                              media_type="text/plain")
    return e, rows, lane


def text_status(res: Any) -> dict[str, str | None]:
    return {r["doc_id"]: r["content"]["current_text_status"] for r in res.rows}


def test_convert_max_rows_is_independent_of_the_fetch_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D392 (P3): ``max_rows`` caps the rows fetched, ``convert_max_rows`` the rows converted. Before it,
    ``max_rows: 1`` also capped convert at one row, which the first in-band post took: the fetched paper
    stayed ``convert_pending`` (in the e2e plan, ~8k in-band posts against the fetched PDFs)."""
    monkeypatch.setattr(convert_mod, "convert_one", fake_child)
    e, rows, lane = capped_world(tmp_path / "independent")
    with lanes.using({"arxiv": lane}, now=NOW):
        res = acquire_rows(rows, e, wants=CAP_WANTS, channel_group=channel_group, max_rows=1, convert_max_rows=None)
    assert [c.source_id for c in lane.calls] == [sid(4)] and res.unreached == [sid(5)]
    assert text_status(res) == dict.fromkeys([sid(1), sid(2), sid(3), sid(4)], "ok")
    assert e.counts["convert.ok"] == 4 and "convert.stopped.limit" not in e.counts
    assert e.counts["stopped.limit"] == 1 and not res.scope_complete

    e, rows, lane = capped_world(tmp_path / "capped")  # convert_max_rows: 2 of the four candidates
    with lanes.using({"arxiv": lane}, now=NOW):
        res = acquire_rows(rows, e, wants=CAP_WANTS, channel_group=channel_group, max_rows=1, convert_max_rows=2)
    assert [s for s in text_status(res).values()] == ["ok", "ok", "convert_pending", "convert_pending"]
    assert e.counts["convert.stopped.limit"] == 1

    e, rows, lane = capped_world(tmp_path / "default")  # absent: no cap (D483; before it, max_rows's 1)
    with lanes.using({"arxiv": lane}, now=NOW):
        res = acquire_rows(rows, e, wants=CAP_WANTS, channel_group=channel_group, max_rows=1)
    assert text_status(res) == dict.fromkeys([sid(1), sid(2), sid(3), sid(4)], "ok")
    assert e.counts["convert.ok"] == 4 and "convert.stopped.limit" not in e.counts


def test_convert_max_minutes_counts_from_the_start_of_convert(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """D392 (P3b): with ``convert_max_minutes`` convert's clock starts with convert: the in-band posts
    convert although the fetch spent the budget (``budget.max_minutes`` ~0: nothing fetched). The default
    (D483) is the same clock with the budget's minutes, here ~0 too, so convert stops at once."""
    monkeypatch.setattr(convert_mod, "convert_one", fake_child)
    e, rows, lane = capped_world(tmp_path / "budget-only")
    with lanes.using({"arxiv": lane}, now=NOW):
        res = acquire_rows(rows, e, wants=CAP_WANTS, channel_group=channel_group, max_minutes=1e-9)
    assert lane.calls == [] and e.counts["stopped.budget"] == 1
    assert e.counts["convert.stopped.budget"] == 1 and "convert.ok" not in e.counts

    e, rows, lane = capped_world(tmp_path / "own-clock")
    with lanes.using({"arxiv": lane}, now=NOW):
        res = acquire_rows(rows, e, wants=CAP_WANTS, channel_group=channel_group, max_minutes=1e-9,
                           convert_max_minutes=5)
    assert lane.calls == [] and e.counts["stopped.budget"] == 1 and res.unreached == [sid(4), sid(5)]
    assert text_status(res) == dict.fromkeys([sid(1), sid(2), sid(3)], "ok")
    assert "convert.stopped.budget" not in e.counts and not res.scope_complete



class FakeClock:
    """``time`` for :mod:`workflows.acquire.main` and :mod:`workflows.acquire.convert` only: ``monotonic``
    moves when a lane call or a conversion says so, never by itself (a budget test that does not
    depend on the machine's load)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class PacedLane(FakeLane):
    """A FakeLane whose every call takes 60 s of the fake clock (a host-paced lane)."""

    clock: FakeClock | None = None

    def fetch(self, request: Any, env: LaneEnv) -> Any:
        assert self.clock is not None
        self.clock.t += 60
        return super().fetch(request, env)


def paced_world(tmp_path: Path, clock: FakeClock) -> tuple[LaneEnv, list[Row], PacedLane]:
    """Three in-band posts (``sid(1..3)``) and ten papers to fetch (``sid(4..13)``), as the review's repro of D483."""
    e = env_for(tmp_path)
    rows = [inband_row(e.blobs, i) for i in (1, 2, 3)] + [document_row(i, state="triaged") for i in range(4, 14)]
    lane = PacedLane.answering("arxiv", {(sid(i), "full_text"): f"{sid(i)}|{BODY}".encode() for i in range(4, 14)},
                               media_type="text/plain")
    lane.clock = clock
    return e, rows, lane


@pytest.mark.parametrize(("options", "converted", "stop"), [
    ({}, 7, None),                                    # D483 default: convert's own clock of the budget's 3.5 min
    ({"convert_max_minutes": 2 / 60}, 2, "budget"),   # explicit options win: 2 s of convert
    ({"convert_max_rows": 3}, 3, "limit"),
    ({"convert_max_rows": None, "convert_max_minutes": None}, 7, None),  # explicit null = the default
])
def test_a_budget_stopped_fetch_still_converts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               options: dict[str, Any], converted: int, stop: str | None) -> None:
    """D483 (the review's repro, ``[defaults] fetched=4 convert.ok=0``): a fetch stopped by
    ``budget.max_minutes`` (3.5 min; each paper takes 60 s) leaves convert its own clock of the same
    budget, so the 4 fetched papers and the 3 in-band posts get text (each conversion takes 1 s).
    Before D483 convert's clock was the stage's and converted nothing. Explicit options still win."""
    clock = FakeClock()
    monkeypatch.setattr("workflows.acquire.main.time", clock)
    monkeypatch.setattr("workflows.acquire.convert.time", clock)

    def child(raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
        clock.t += 1
        return fake_child(raw, media_type, **kw)

    monkeypatch.setattr(convert_mod, "convert_one", child)
    e, rows, lane = paced_world(tmp_path, clock)
    with lanes.using({"arxiv": lane}, now=NOW):
        res = acquire_rows(rows, e, wants=CAP_WANTS, channel_group=channel_group, max_minutes=3.5, **options)
    assert len(lane.calls) == 4 and e.counts["stopped.budget"] == 1 and len(res.unreached) == 6
    assert e.counts.get("convert.ok", 0) == converted
    assert sum(s == "ok" for s in text_status(res).values()) == converted
    assert [k for k in e.counts if k.startswith("convert.stopped.")] == ([f"convert.stopped.{stop}"] if stop else [])
    assert not res.scope_complete


def merged(rows: list[Row], res: Any) -> list[Row]:
    """``rows`` with the run's written rows in place (the next run's input), in ``doc_id`` order."""
    by = {r["doc_id"]: r for r in rows} | {r["doc_id"]: copy.deepcopy(r) for r in res.rows}
    return [by[d] for d in sorted(by)]


def test_stopped_inband_conversions_come_back_into_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D403 (review L4-8): an in-band body a convert stop left ``convert_pending`` on a row triage does
    not let fetch matched no scope clause from the next run on (minted, current, nothing due, no text
    for ``quality``): idle for good. The ``convert`` clause brings it back, without any fetch. A text
    converted under an older pin (``sid(4)``, ``docling-v2``) does not count: that row stays idle."""
    monkeypatch.setattr(convert_mod, "convert_one", fake_child)
    rows = [inband_row(open_blobs(tmp_path / "s"), i) for i in (1, 2, 3, 4)]
    for r in rows:
        r["annotations"]["triage"]["payload"]["decision"] = "skip"
    old = env_for(tmp_path / "s", "docling-v2")
    with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
        res = acquire_rows(rows[3:], old, wants=CAP_WANTS, channel_group=channel_group)
    assert text_status(res) == {sid(4): "ok"}
    rows = merged(rows, res)
    e1 = env_for(tmp_path / "s")
    with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
        res = acquire_rows(rows[:3], e1, wants=CAP_WANTS, channel_group=channel_group, convert_max_rows=1)
    assert text_status(res) == {sid(1): "ok", sid(2): "convert_pending", sid(3): "convert_pending"}
    rows = merged(rows, res)
    e2 = env_for(tmp_path / "s")
    assert e2.convert_pin != old.convert_pin
    with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
        res = acquire_rows(rows, e2, wants=CAP_WANTS, channel_group=channel_group, convert_max_rows=None)
    assert text_status(res) == {sid(2): "ok", sid(3): "ok"}
    assert e2.counts["rows_in_scope"] == 2 and e2.counts["scope.convert"] == 2 and e2.counts["idle"] == 2
    assert e2.counts["convert.ok"] == 2 and e2.counts["lane_calls"] == 0 and res.scope_complete
    rows = merged(rows, res)
    assert [r["content"]["current_text_status"] for r in rows[:3]] == ["ok", "ok", "ok"]
    assert scope.reasons(rows[3], NOW, False, pin=e2.convert_pin) == []  # the old-pin text: no clause

@pytest.mark.slow
def test_convert_max_rows_through_the_runner(tmp_path: Path) -> None:
    """The stage line's ``options.convert_max_rows: null`` lifts the convert cap that ``max_rows``
    would set (``--only convert``, trap lanes: no fetch)."""
    root = tmp_path / "store"
    blobs = open_blobs(root)
    rows = [pending_row(blobs, i, f"Row {i}. {BODY}".encode(), "text/plain") for i in (1, 2)]
    write_on_head(root, "documents", "documents", rows, stage="gather", run=1, kind="full",
                  blocks_written=["identity", "metadata", "inband", "content"])
    with lanes.using(TRAPS, now=NOW, transport=TrapTransport()):
        rec = run_stage(root, plan("pymupdf4llm-v1", {"arxiv": ["full_text"]}, max_rows=1, convert_max_rows=None),
                        "acquire", [sdir("documents")], series="main", only=["convert"])
    assert rec.status == "ok", rec.error
    assert rec.scope_complete and rec.counts["convert.ok"] == 2 and "convert.stopped.limit" not in rec.counts


# ---------------------------------------------------------------- surrogates in a child's text (D586)
#: A child result whose markdown holds a lone high surrogate, an encoded pair and a lone low one,
#: as ``json.dumps`` (``ensure_ascii``) writes them: the parent's ``json.loads`` yields them as is.
SURROGATE_RESULT = json.dumps({"status": "ok", "converter": "fake@1+tier2",
                               "markdown": f"{BODY} lone \ud835 pair \ud835\udc00 low \udc00 end"})
FAKE_WORKER = """import json, sys
from pathlib import Path
req = json.loads(Path(sys.argv[1]).read_text())
Path(req["out_path"]).write_text(Path(__file__).with_suffix(".json").read_text())
"""


def fake_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str) -> None:
    """Points :func:`convert_one` at a child that writes ``result`` verbatim as its result file."""
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    script.with_suffix(".json").write_text(result)
    monkeypatch.setattr(convert_mod, "_WORKER", script)


def test_a_childs_lone_surrogates_are_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D586, the 2026-10-15 crash: a child's markdown holding surrogates (``\\ud835`` escapes in
    its result JSON) converts ``ok``: a pair becomes its character, a lone one U+FFFD; the text is
    stored as UTF-8. Before D586 the blob write raised ``UnicodeEncodeError`` out of the stage."""
    fake_worker(tmp_path, monkeypatch, SURROGATE_RESULT)
    e = env_for(tmp_path / "store")
    row = pending_row(e.blobs, 1, BODY.encode(), "text/plain")
    run_convert([row], e)
    s = snap_of(row)
    assert (s["text_status"], s["text_error_class"]) == ("ok", None)
    assert e.blobs.get(s["text"]["sha256"]).decode("utf-8").endswith(
        " lone � pair \U0001d400 low � end")


def test_an_unreadable_child_result_is_that_items_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D586: a result file that is not JSON (a child killed mid-write) is the item's
    ``failed_transient[crashed]``, as a missing one; the next row still converts."""
    fake_worker(tmp_path, monkeypatch, '{"status": "ok", "markdown": "trunc')
    e = env_for(tmp_path / "store")
    rows = [pending_row(e.blobs, i, BODY.encode(), "text/plain") for i in (1, 2)]
    out = convert_mod.convert_one(BODY.encode(), "text/plain", mem_gb=0, timeout=30)
    assert out.status == "crashed" and "result unreadable" in (out.error or "")
    run_convert(rows[:1], e)
    assert (snap_of(rows[0])["text_status"], snap_of(rows[0])["text_error_class"]) == ("failed_transient", "crashed")
    monkeypatch.setattr(convert_mod, "convert_one", fake_child)
    run_convert(rows[1:], e)
    assert snap_of(rows[1])["text_status"] == "ok"


def test_text_that_will_not_encode_fails_its_row_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D586 containment: markdown that still cannot be encoded as UTF-8 when it is stored (a
    boundary that missed :func:`~stack.core.hashes.utf8_safe`) ends that snapshot
    ``failed_permanent[parse_error]`` (``convert.encode_error``); the other rows convert and the
    run goes on. Before D586 it failed the run ``code_bug``."""
    def child(raw: bytes, media_type: str | None, **kw: Any) -> ConvertOutcome:
        doc_id = raw.decode().split("|", 1)[0]
        md = f"Converted {raw.decode()}" + ("\ud835" if doc_id == sid(2) else "")
        return ConvertOutcome("ok", markdown=md, converter="fake@1+tier2")

    e = env_for(tmp_path, convert_workers=2)
    rows = worker_rows(e.blobs, 3)
    monkeypatch.setattr(convert_mod, "convert_one", child)
    assert run_convert(rows, e) is True
    assert [(snap_of(r)["text_status"], snap_of(r)["text_error_class"]) for r in rows] == [
        ("ok", None), ("failed_permanent", "parse_error"), ("ok", None)]
    assert snap_of(rows[1])["text"] is None and snap_of(rows[1])["convert_attempts"] == 1
    assert (e.counts["convert.ok"], e.counts["convert.failed_permanent"], e.counts["convert.encode_error"]) == (2, 1, 1)
