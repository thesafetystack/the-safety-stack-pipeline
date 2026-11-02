"""``stack show`` (D541-D543): one document from the series' heads, read-only, on the synthetic world
(``tests/synth/world.py``) plus a ``documents`` part and a ``sources.lesswrong`` part that give the
LW crosspost (two members) a ``provider_record`` in-band entry, ``matched_by`` queries, a screen
and an affiliation answer, and one source no document holds. An S3 mirror case runs over the
in-memory fake (``tests/fakes/s3.py``): no network."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stack.cli import app
from stack.cli.status import human
from stack.runs import (
    RunRecord,
    write_build_record,
    write_final_record,
    write_running_record,
)
from stack.store import remote as R
from stack.store.blobs import open_blobs
from stack.store.index import forget_indexes
from tests.fakes.s3 import FAKE_ENV, SECRET, FakeS3
from tests.fixtures.checks import T
from tests.synth.rows import AT, _ann, affiliation_row, blob, dg, mint, rid, screen_row, sid, triage_evidence_row
from tests.synth.world import SIZE, SOURCES, World, build_world, sources_row, write_on_head

pytestmark = pytest.mark.e2e  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

runner = CliRunner()
LW, AF, A1, A2 = (SOURCES[x].source_id for x in ("LW", "AF", "A1", "A2"))
LONE = sid(99)
RECORD = {"provider": "lesswrong", "postId": "abc123", "title": "Sandbagging evals"}
ARXIV_RECORD = b"<entry><id>http://arxiv.org/abs/2410.00002v1</id></entry>"
BUCKET = "inspect-bucket-q7"
BLOCKS = ["identity", "metadata", "inband", "content", "annotations"]


def show(store: Path | str, *args: str, code: int = 0, env: dict[str, Any] | None = None) -> str:
    res = runner.invoke(app, ["--store", str(store), "show", *args],
                        env={"STACK_PLAN": None, "STACK_STORE": None, **(env or {})})
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    print(f"$ stack show {' '.join(args)} -> {res.exit_code}\n{res.output}")
    assert res.exit_code == code, res.output
    return res.output


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = build_world(tmp_path / "store")
    blobs = open_blobs(w.root)
    rec = blobs.put(json.dumps(RECORD).encode())
    entry = {"source_id": LW, "content_role": "provider_record",
             "raw": blob(rec, len(json.dumps(RECORD).encode()), "application/json"), "text": None, "captured_at": T(30)}
    stale = b'{"provider": "lesswrong", "stale": true}'
    older = {**entry, "raw": blob(blobs.put(stale), len(stale), "application/json"), "captured_at": T(20)}
    lw = copy.deepcopy(w.docs["LW"])
    lw["inband"]["entries"] += [older, entry]  # --record prints the newest provider_record (D545)
    lw["annotations"]["screen"] = _ann("screen", {
        "verdict": "relevant", "basis_role": "post", "buckets": ["evals"], "quotes": [{"text": "we test", "offset": 3}],
        "off_topic_reason": None, "confidence": 0.85, "reasoning": "evals", "adjudication_tier": 1,
        "tier1_verdict": None, "tier1_model": None}, basis_kind="text")
    lw["annotations"]["affiliation"] = _ann("affiliation", {"items": [{
        "item_key": "a. author", "author_name": "A. Author", "author_surface": "A. Author", "byline_index": 0,
        "affiliations": [{"surface": "Example Lab", "org_slugs": ["example-lab"]}], "orcid": None,
        "resolution": "resolved"}]}, basis_kind="text")
    write_on_head(w.root, "documents", "documents", [lw], stage="gather", run=30, blocks_written=BLOCKS)
    src = sources_row("LW", 31, observed_at=T(31))
    src["metadata"]["matched_by"] = [{"query_id": "kw:group:evals", "query_digest": dg("q"), "label": "evals"},
                                     {"query_id": "feed:lesswrong", "query_digest": dg("f"), "label": None}]
    src["inband"]["entries"].append(entry)
    lone = sources_row("LW", 31, observed_at=T(31), source_id=LONE)
    lone["identity"] = {**lone["identity"], "source_id": LONE, "external_id": "zzz999"}
    lone["metadata"]["title"] = "A post nobody gathered yet"
    lone["inband"] = {**lone["inband"], "entries": []}
    write_on_head(w.root, "sources.lesswrong", "sources", [src, lone], stage="collect.lesswrong", run=31)
    a2 = sources_row("A2", 32, observed_at=T(32))
    a2["inband"]["entries"] = [*a2["inband"]["entries"], {  # A2's record only on its sources row
        "source_id": A2, "content_role": "provider_record", "text": None, "captured_at": T(32),
        "raw": blob(blobs.put(ARXIV_RECORD), len(ARXIV_RECORD), "application/json")}]
    write_on_head(w.root, "sources.arxiv", "sources", [a2], stage="collect.arxiv", run=32)
    return w


def test_show_a_document_by_a_member_with_its_sources_inband_roles_and_annotations(world: World) -> None:
    out = show(world.root, AF)  # AF is LW's second member
    lines = out.splitlines()
    assert lines[0].startswith("data/documents/main/ head ") and f"query {AF} (matched by member)" in lines[0]
    assert f"document {LW}  state acquired" in out
    assert "channels alignmentforum,lesswrong" in out and "keys forum_post:abc123" in out
    assert "title: Sandbagging evals: what we found" in out and "authors: A. Author" in out
    assert "members (2)" in out
    assert f"  {LW}  lesswrong  external_id abc123  anchor  live" in out
    assert f"  {AF}  alignmentforum  external_id abc123  key:forum_post->{LW}  live" in out
    assert "matched_by kw:group:evals, feed:lesswrong  source row data/sources.lesswrong/main/" in out
    assert "matched_by -  source row data/sources.alignmentforum/main/" in out  # AF's row has no queries
    member_lw = out[out.index(f"  {LW}  lesswrong"):out.index(f"  {AF}  alignmentforum")]
    rec_size = len(json.dumps(RECORD).encode())
    # per entry its raw and text BLOBs (media type, size as the row records it, digest; D690)
    assert f"provider_record  raw application/json {rec_size} B " in member_lw
    assert f"post             raw text/html {human(SIZE)} " in member_lw
    assert f"  text text/markdown {human(SIZE)} " in member_lw
    assert "fetch roles (2)" in out and f"  {AF}  post         lane inband   ok" in out
    assert "current text: ok  media md" in out and f"source {AF}  role post" in out
    assert "annotations (3)" in out
    assert "triage           ok                     decision fetch; wanted_by screen; safety_relevance=relevant (0.80)" in out
    assert "screen           ok                     verdict relevant (0.85); buckets evals" in out
    assert "affiliation      ok                     1 item(s): A. Author -> example-lab" in out
    assert "--- " not in out  # no --record, no --text: no blob is printed


@pytest.fixture
def annotated(world: World) -> World:
    """``world`` plus what the annotate and triage stages write beside the documents rows (D581): A1's
    triage question's evidence row asked on the title (D527), a ``screen`` head and an item-grain
    ``affiliation`` head for A1 (two authors) and A2 (no items: the ``item_key = ''`` status row)."""
    (q,) = world.docs["A1"]["annotations"]["triage"]["payload"]["questions"]
    a1, a2 = int(A1[-3:]), int(A2[-3:])
    ev = triage_evidence_row(a1, 40, evidence_id=q["evidence_id"], doc_id=A1, source_id=A1, basis="title")
    write_on_head(world.root, "triage_evidence", "triage_evidence", [ev], stage="triage", run=40, kind="full",
                  blocks_written=["annotations"])
    scr = screen_row(a1, 41, doc_id=A1, source_id=A1, verdict="borderline", confidence=0.62,
                     buckets=["evals", "control"])
    write_on_head(world.root, "screen", "screen", [scr], stage="annotate.screen", run=41, kind="full",
                  basis_kind="text")
    aff = [affiliation_row(a1, "b. author", 42, doc_id=A1, source_id=A1),
           affiliation_row(a1, "a. author", 42, doc_id=A1, source_id=A1,
                           affiliations=[{"surface": "Lab Two", "org_slugs": ["lab-two", "lab-one"]}]),
           affiliation_row(a2, "", 42, doc_id=A2, source_id=A2)]
    write_on_head(world.root, "affiliation", "affiliation", aff, stage="annotate.affiliation", run=42, kind="full",
                  basis_kind="text")
    return world


@pytest.mark.slow
def test_show_reads_triage_basis_and_the_annotate_heads(annotated: World) -> None:
    """D581: the triage answer with its basis; screen's verdict, confidence and buckets and the affiliation
    items from their ``data/<wf>/main/`` heads, in text and in ``--json``."""
    out = show(annotated.root, A1)
    lines = out.splitlines()
    tri = next(ln for ln in lines if ln.startswith("  triage "))
    assert tri.endswith("decision fetch; wanted_by screen; safety_relevance=relevant (0.80)  basis title")
    assert ("  screen           ok                     verdict borderline (0.62); buckets evals,control  "
            "(data/screen/main/)") in lines
    assert ("  affiliation      ok                     2 item(s): A. Author -> lab-one,lab-two; B. Author -> "
            "example-lab  (data/affiliation/main/)") in lines
    assert "annotations (3)" in out
    (d,) = json.loads(show(annotated.root, A1, "--json"))["documents"]
    by = {a["workflow"]: a for a in d["annotations"]}
    assert by["triage"]["basis"] == {"safety_relevance": "title"} and by["triage"]["from"] == "documents"
    assert by["screen"]["from"] == "data/screen/main"
    assert (by["screen"]["payload"]["verdict"], by["screen"]["payload"]["confidence"],
            by["screen"]["payload"]["buckets"]) == ("borderline", 0.62, ["evals", "control"])
    items = by["affiliation"]["payload"]["items"]
    assert [(i["item_key"], [a["org_slugs"] for a in i["affiliations"]]) for i in items] == [
        ("a. author", [["lab-two", "lab-one"]]), ("b. author", [["example-lab"]])]
    # A2: no items (status row only); its triage question's evidence row is not in the store.
    (d2,) = json.loads(show(annotated.root, A2, "--json"))["documents"]
    by2 = {a["workflow"]: a for a in d2["annotations"]}
    assert (by2["affiliation"]["status"], by2["affiliation"]["payload"]) == ("empty", {"items": []})
    assert all(b is None for b in by2["triage"]["basis"].values())
    assert "0 item(s): " in show(annotated.root, A2) and "basis ?" in show(annotated.root, A2)


def test_show_prefers_the_documents_rows_annotation_and_needs_no_annotate_head(world: World) -> None:
    """A store with no ``data/<wf>/`` and no ``triage_evidence`` head: the row's own annotations, all
    ``from: documents``; a triage question whose evidence is not in the store has basis ``None``."""
    (d,) = json.loads(show(world.root, LW, "--json"))["documents"]
    assert [(a["workflow"], a["from"]) for a in d["annotations"]] == [
        ("triage", "documents"), ("screen", "documents"), ("affiliation", "documents")]
    assert set(d["annotations"][0]["basis"].values()) == {None}


def test_show_by_doc_id_json_and_the_fetched_roles(world: World) -> None:
    rep = json.loads(show(world.root, A1, "--json"))
    assert (rep["matched"], rep["series"], rep["dir"]) == ("doc_id", "main", "data/documents/main")
    (d,) = rep["documents"]
    assert d["doc_id"] == A1 and d["state"] == "acquired"
    roles = {(r["content_role"], r["lane"], r["status"]) for r in d["roles"]}
    assert roles == {("full_text", "arxiv", "ok"), ("xml", "arxiv", "ok"), ("abstract", "inband", "ok")}
    kinds = sorted((s["kind"], s["content_role"]) for s in d["snapshots"])
    assert kinds == [("inband", "abstract"), ("raw", "full_text"), ("raw", "xml")]
    assert d["current"]["role"] == "full_text" and d["current"]["text_status"] == "ok"
    assert d["current"]["text_sha256"] == dg(("text", "A1", "full_text")) and d["current"]["size"] == SIZE
    (m,) = d["members"]
    assert (m["source_id"], m["channel"], m["external_id"], m["sources_dir"]) == (
        A1, "arxiv", "2410.00001", "data/sources.arxiv/main")
    assert [a["workflow"] for a in d["annotations"]] == ["triage"]
    assert d["annotations"][0]["payload"]["decision"] == "fetch"
    text = show(world.root, A2)
    assert "  s000000000002  xml          lane arxiv    failed_permanent  attempts 1  dead" in text


@pytest.mark.slow
def test_show_record_and_text_print_only_the_blob_before_and_after_provider_record(world: World) -> None:
    """D545: ``--record``/``--text`` print the blob's bytes and nothing else (pipeable)."""
    assert json.loads(show(world.root, LW, "--record")) == RECORD  # the anchor's in-band provider_record
    assert show(world.root, LW, "--record") == json.dumps(RECORD)  # as stored, no newline added
    cur = json.loads(show(world.root, LW, "--json"))["documents"][0]["current"]["text_sha256"]
    assert show(world.root, AF, "--text") == (world.root / "blobs" / cur).read_text()  # a member names its doc
    old = show(world.root, A2, "--record")  # the record on the member's sources row (D546)
    assert old == ARXIV_RECORD.decode()
    (m,) = json.loads(show(world.root, A2, "--json"))["documents"][0]["members"]
    assert [r["sha256"] for r in m["provider_records"]] == [open_blobs(world.root).put(ARXIV_RECORD)]
    assert show(world.root, AF, "--record", code=1) == (  # AF's own record: it has none
        f"error: {AF}: no provider record (no provider_record entry)\n")
    assert "no provider record" in show(world.root, A1, "--record", code=1)
    assert "give one" in show(world.root, LW, "--record", "--text", code=1)
    assert "not JSON" in show(world.root, LW, "--json", "--text", code=1)
    assert "no document holds it" in show(world.root, LONE, "--text", code=1)
    assert show(world.root, LONE, "--record", code=1).startswith(f"error: {LONE}: no provider record")  # a lone source


@pytest.mark.slow
def test_show_masked_retired_lone_source_and_refusals(world: World) -> None:
    x = show(world.root, SOURCES["X"].source_id)
    assert "state identified" in x and "MASKED: operator  overrides ov-mask-x" in x
    o4 = show(world.root, SOURCES["O4"].source_id)  # O4 is a doc id (retired) before it is A4's member
    assert "state retired" in o4 and f"merged into {SOURCES['A4'].source_id}" in o4 and "NOT LIVE" in o4
    lone = show(world.root, LONE)
    assert f"query {LONE} (matched by source)" in lone and f"source {LONE} (in no document)" in lone
    assert "external_id zzz999" in lone and "title: A post nobody gathered yet" in lone
    err = show(world.root, sid(77), code=1)
    assert err.startswith(f"error: {sid(77)}: no document (doc_id or member) in data/documents/main/")
    nt = show(world.root, SOURCES["A6"].source_id, "--text", code=1)
    assert "no current text (text status none)" in nt
    assert "no current text (text status convert_failed)" in show(world.root, SOURCES["O2"].source_id, "--text", code=1)
    assert show(world.root / "nope", LW, code=1).startswith("error: no store at ")


def test_show_series_default_and_several_series(world: World) -> None:
    other = copy.deepcopy(world.docs["A3"])
    write_on_head(world.root, "documents", "documents", [other], stage="gather", run=40, kind="full",
                  series="other", blocks_written=BLOCKS)
    out = show(world.root, LW, code=1)
    assert "error: several documents series (main, other): pass --series" in out
    assert f"document {LW}" in show(world.root, LW, "--series", "main")
    assert "data/documents/other/ head " in show(world.root, SOURCES["A3"].source_id, "--series", "other")
    assert "no published file" in show(world.root, LW, "--series", "nope", code=1)
    a3 = SOURCES["A3"].source_id
    twins = [{**copy.deepcopy(world.docs["A3"]), "doc_id": sid(n)} for n in (97, 98)]  # both hold A3 as a member
    write_on_head(world.root, "documents", "documents", twins, stage="gather", run=41, kind="full", series="third",
                  blocks_written=BLOCKS)
    out = show(world.root, a3, "--series", "third", "--text", code=1)
    assert f"error: {a3}: 2 documents hold it ({sid(97)}, {sid(98)}): pass one doc_id" in out


@pytest.mark.slow
def test_show_on_a_fresh_mirror_pulls_data_and_blobs_and_never_names_the_bucket(
        world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pull on read: a fresh mirror downloads the head version's data files and the blobs it prints
    (``--text``, ``--record``) through the store; a size is the one the row records (D690: no HEAD, no
    download). No output names the bucket (D540)."""
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STACK_ENV_FILE", raising=False)
    fake = FakeS3(page=1000)
    for p in sorted(world.root.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            fake.put_object(Bucket=BUCKET, Key=f"stack-v4/{p.relative_to(world.root).as_posix()}", Body=p.read_bytes())
    text = str(("post-text", "AF")).encode()  # the world's digest is sha256 of this label; its local
    fake.put_object(Bucket=BUCKET, Key=f"stack-v4/blobs/{dg(('post-text', 'AF'))}", Body=text)  # blob is a placeholder
    env = {"STACK_MIRROR": str(tmp_path / "mirror")}
    uri = f"s3://{BUCKET}/stack-v4"
    forget_indexes()
    fake.calls.clear()
    with R.using_s3_client(fake):
        out = show(uri, AF, env=env)
        rep = json.loads(show(uri, AF, "--json", env=env))
        rec = show(uri, LW, "--record", env=env)
        txt = show(uri, AF, "--text", env=env)
    assert f"document {LW}" in out and json.loads(rec) == RECORD
    assert txt == text.decode()  # sha256-checked on download
    gets = fake.ops("get")
    assert any("/data/documents/main/" in k for k in gets) and any("/data/sources.lesswrong/main/" in k for k in gets)
    cur = rep["documents"][0]["current"]["text_sha256"]
    assert f"stack-v4/blobs/{cur}" in gets  # --text downloaded it
    assert not fake.ops("head")  # sizes are the rows' BLOB.bytes: no blob is asked for its size
    rec_blob = open_blobs(world.root).put(json.dumps(RECORD).encode())
    assert {k for k in gets if "/blobs/" in k} == {f"stack-v4/blobs/{cur}", f"stack-v4/blobs/{rec_blob}"}
    assert all(BUCKET not in o and SECRET not in o for o in (out, rec, txt, json.dumps(rep)))


# ---------------------------------------------------------------- show runs
def _run(n: int, stage: str, status: str, start_min: int, minutes: float | None, build: dict[str, Any],
         **kw: Any) -> RunRecord:
    at = AT + dt.timedelta(minutes=start_min)
    fin = None if minutes is None else at + dt.timedelta(minutes=minutes)
    return RunRecord(run_id=rid(n), stage=stage, writer=stage, plan=build["plan"], status=status, started_at=at,
                     heartbeat_at=fin or at, finished_at=fin, code_digest=dg("code"), config_digest=dg("config"),
                     build_id=build["build_id"], **kw)


def _build(store: Path, n: int, series: str, start_min: int, runs: list[dict[str, Any]],
           status: str = "ok") -> dict[str, Any]:
    b = {"build_id": mint("w", n), "plan": "dev-v2", "series": series, "status": status,
         "started_at": (AT + dt.timedelta(minutes=start_min)).isoformat(),
         "finished_at": (AT + dt.timedelta(minutes=start_min + 20)).isoformat() if status != "running" else None,
         "upload": False, "runs": [], "incomplete": [{"stage": "triage", "run_id": rid(62), "reasons": ["limit"]}]}
    bp = write_build_record(store, b)
    for spec in runs:
        rec = _run(build=b, **spec)
        if rec.status == "running":
            p = write_running_record(store, rec, build=bp.parent.name)
            marker = p.with_name(p.name.removesuffix(".json.zst") + "-running.json")
            marker.write_text("{}")
            last = (rec.started_at + dt.timedelta(minutes=1)).timestamp()  # its last tick: a minute in
            os.utime(marker, (last, last))
        else:
            write_final_record(store, rec, build=bp.parent.name)
    return b


@pytest.fixture
def builds(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    store.mkdir()
    _build(store, 1, "other", 0, [
        {"n": 50, "stage": "gather", "status": "ok", "start_min": 1, "minutes": 1.0, "scope_complete": True,
         "version": {"rows_touched": 3}}])
    _build(store, 2, "main", 60, [
        {"n": 61, "stage": "collect.arxiv", "status": "ok", "start_min": 61, "minutes": 2.0, "scope_complete": True,
         "version": {"rows_touched": 120}, "counts": {"requests": 10, "http.200": 10, "materialized.sources": 120},
         "spend": {"usd_micro": {"openalex": 20_000}, "reserved_open_usd_micro": 0}},
        {"n": 62, "stage": "triage", "status": "ok", "start_min": 64, "minutes": 5.0, "scope_complete": False,
         "version": {"rows_touched": 50}, "counts": {"ask.limit": 1, "asked": 50, "answered": 48},
         "spend": {"usd_micro": {"openrouter/m/a": 1_250_000, "openrouter/m/b": 250_000},
                   "reserved_open_usd_micro": 0}},
        {"n": 63, "stage": "annotate.screen", "status": "failed", "start_min": 70, "minutes": 1.5,
         "error_class": "code_bug", "error": "Traceback (most recent call last):\n  ...\nValueError: boom",
         "spend": {"usd_micro": {"openrouter/m/a": 500_000}, "reserved_open_usd_micro": 100_000}},
        {"n": 64, "stage": "acquire", "status": "running", "start_min": 72, "minutes": None},
    ], status="running")
    return store


def test_show_runs_table_reasons_rates_spend_and_totals(builds: Path) -> None:
    out = show(builds, "runs")  # the newest build of the store
    lines = out.splitlines()
    assert lines[0].startswith(f"build {mint('w', 2)}  plan dev-v2  series main  running  started 2026-10-12T02:30:00")
    assert lines[1] == f"  incomplete: triage run {rid(62)} (limit)"
    rows = {ln.split()[1]: ln for ln in lines if ln.startswith("  ") and len(ln.split()) > 1 and ln.split()[1][:1] == "r"}
    assert rows[rid(61)].split()[2:8] == ["ok", "yes", "2.0m", "120", "60.0", "$0.02"], rows[rid(61)]
    assert rows[rid(62)].split()[2:9] == ["ok", "no", "5.0m", "50", "10.0", "$1.50", "limit"], rows[rid(62)]
    assert rows[rid(63)].split()[2:10] == ["failed[code_bug]", "no", "1.5m", "-", "-", "$0.50", "+$0.10", "open"]
    assert rows[rid(63)].endswith("code_bug")
    assert rows[rid(64)].split()[0] == "acquire" and "running, DEAD" in rows[rid(64)]  # wall: to its last tick
    assert rows[rid(64)].split()[5:7] == ["no", "1.0m"], rows[rid(64)]
    assert "      error: ValueError: boom" in out
    assert "      counts: answered=48, asked=50" in out  # ask.limit: no default prefix
    assert "      counts: http.200=10, requests=10" in out  # materialized.*: not by default
    assert "      spend: openrouter/m/a $1.25, openrouter/m/b $0.25" in out
    assert lines.index(next(ln for ln in lines if ln.startswith("totals:"))) == len(lines) - 4
    assert ("totals: 4 run(s) (1 failed[code_bug], 2 ok, 1 running); runs' wall 9.5m; build wall -; rows 170; "
            "spend $2.02 (+$0.10 open)") in out
    assert lines[-3:] == [f"  {'openalex':<40} {'$0.02':>10}", f"  {'openrouter/m/a':<40} {'$1.75':>10}",
                          f"  {'openrouter/m/b':<40} {'$0.25':>10}"]
    every = show(builds, "runs", mint("w", 2), "--counts", "all")
    assert "materialized.sources=120" in every and "ask.limit=1" in every
    assert "      counts: ask.limit=1\n" in show(builds, "runs", "--counts", "ask.")


def test_show_runs_selects_the_build_and_prints_json(builds: Path) -> None:
    other = show(builds, "runs", "--series", "other")
    assert other.startswith(f"build {mint('w', 1)}  plan dev-v2  series other  ok") and "wall 20.0m" in other
    assert "totals: 1 run(s) (1 ok); runs' wall 1.0m; build wall 20.0m; rows 3; spend $0.00" in other
    rep = json.loads(show(builds, "runs", mint("w", 2), "--json"))
    assert rep["build"]["build_id"] == mint("w", 2) and [r["run_id"] for r in rep["runs"]] == [
        rid(61), rid(62), rid(63), rid(64)]
    t = rep["totals"]
    assert t["spend_by_model"] == {"openalex": 20_000, "openrouter/m/a": 1_750_000, "openrouter/m/b": 250_000}
    assert (t["spend_usd_micro"], t["open_usd_micro"], t["rows"]) == (2_020_000, 100_000, 170)
    r62 = rep["runs"][1]
    assert r62["reasons"] == ["limit"] and r62["counts"]["ask.limit"] == 1 and r62["rows_per_min"] == 10.0
    assert show(builds, "runs", "nope", code=1).startswith("error: no build 'nope'")
    assert "no build record of series 'x'" in show(builds, "runs", "--series", "x", code=1)
    assert "--record and --text are for a document" in show(builds, "runs", "--text", code=1)
    assert "takes one id" in show(builds, LW, "extra", code=1)
