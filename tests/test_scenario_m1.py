"""M1 scenario (PLAN T1.7): the v4e mock's steps 3, 8, 9 and 12, its mutation rows M1 and M9,
and the B2 chain scenario, run against the real ``stack`` modules over the
synthetic world of ``tests/synth/world.py``.

M1 has one stage, ``echo_test``; gather, triage and acquire arrive in M2/M3. Where a mock step
ran one of those, this file seeds the rows with the world's factories, or writes a toy stage
under a private workflows root (``stage_dir``) that the real runner dispatches. Assertions go
through ``run_checks`` where a named check exists, otherwise through the library functions and
the CLI (typer's ``CliRunner``, no subprocess). Mutation M2 (a dropped check) is the clean
world's inventory assertion against ``IMPLEMENTED_M1``; M3 as X10 is the ``ordering_pinned``
fixture of ``tests/test_checks_fixtures.py``.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import textwrap
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
import zstandard
from typer.testing import CliRunner

from stack import stage_runner
from stack.checks import REGISTRY, CheckResult
from stack.checks.chain import last_build
from stack.checks.verify import run_checks, summary
from stack.cli import app
from stack.cli.status import collect_status
from stack.plan import load_plan
from stack.runs import (
    RunRecord,
    build_record_path,
    files_without_ok_record,
    find_run_record,
    read_run_records,
    run_record_path,
    write_running_record,
)
from stack.schemas.families import family
from stack.schemas.vocab import POINTER_RANK
from stack.stage_runner import StageError, run_stage
from stack.store import chain as chain_mod
from stack.store.chain import head_of, read_view, resolve
from stack.store.index import store_index
from stack.store.layout import BUILDS_DIR, DATA
from stack.store.parquet import SchemaRefused, read_family, read_header
from tests.synth.records import run_line, sdir
from tests.synth.rows import AT, Row, dg, mint, rid
from tests.synth.world import SOURCES, World, build_world, write_on_head
from workflows.acquire.point import point as acquire_point

#: scenario world: skipped while iterating (`-m "not slow"`), run at milestone gates (D90; `docs/SETUP.md` §7).
pytestmark = [pytest.mark.slow, pytest.mark.e2e]

REPO = Path(__file__).resolve().parents[1]
DEV_PLAN = REPO / "configs" / "plans" / "dev-v2.yaml"
#: The checks M1 implements (D78): the inventory a dropped check breaks (mutation M2). Later
#: milestones register more checks; the clean world must pass every registered one, so the verify
#: line is computed from the registry.
IMPLEMENTED_M1 = frozenset({
    "schema_equal", "nonnull", "key_unique", "status_pairing", "nested_unique", "id_formats",
    "kv_complete", "state_blocks", "digest_agreements", "payload_typed", "V-state", "V-digests",
    "V-chain-linear", "V-compaction-equal", "finished_files", "cardinalities", "ordering_pinned",
    "block_ownership", "created_preserved", "full_covers_base", "head_descends", "attempts_monotone"})
runner = CliRunner()


def _clean_line() -> str:
    k = sum(cs.fn is not None for cs in REGISTRY.values())
    return f"47 checks: {k} ok" + (f", {47 - k} skipped[not_implemented]" if k < 47 else "")  # 47: T4.2


CLEAN = _clean_line()


# ---------------------------------------------------------------- fixtures and helpers
@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path / "store")


@pytest.fixture
def plan() -> Any:
    return load_plan(DEV_PLAN)


@pytest.fixture
def stage_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private workflows root for the toy stages of this file (echo_test copied in)."""
    root = tmp_path / "workflows"
    (root / "echo_test").mkdir(parents=True)
    (root / "echo_test" / "main.py").write_text((REPO / "workflows" / "echo_test" / "main.py").read_text())
    monkeypatch.setattr(stage_runner, "WORKFLOWS_ROOT", root)
    return root


def toy_stage(root: Path, path: str, body: str) -> None:
    """``workflows/<path>/main.py`` under the private root (``collect/arxiv`` = ``collect.arxiv``)."""
    d = root / path
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(textwrap.dedent(body))


def cli(store: Path, *args: str, code: int | None = 0) -> str:
    res = runner.invoke(app, ["--store", str(store), "--plan", str(DEV_PLAN), *args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    print(f"$ stack {' '.join(args)}  -> {res.exit_code}\n{res.output}")
    if code is not None:
        assert res.exit_code == code, res.output
    return res.output


def red(results: list[CheckResult]) -> dict[str, list[str]]:
    return {r.check: list(r.items) for r in results if r.status in ("failed", "warn")}


def by_check(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.check: r for r in results}


def data_names(store: Path) -> list[str]:
    return sorted(p.relative_to(store).as_posix() for p in (store / DATA).rglob("*.parquet"))


def view_rows(store: Path, family_dir: str = "documents") -> dict[str, Row]:
    return {r["doc_id"]: r for r in read_view(resolve(store, sdir(family_dir))).to_pylist()}


DOCS = sdir("documents")  # data/documents/main/, the world's documents series


def docs_head(store: Path) -> str:
    return head_of(store, DOCS.rstrip("/"))


def run_on_docs(store: Path, plan: Any, stage: str, ins: list[str] | None = None) -> RunRecord:
    """``stage`` with ``--in`` ``ins`` (default the documents series directory), its part on
    the directory it read (the world's series ``main``)."""
    return run_stage(store, plan, stage, ins or [DOCS], series="main", kind="part")


def build_with(w: World, version_chain: list[str], **files: str) -> dict[str, Any]:
    """The world's build record with ``version_chain`` and the given ``files`` heads replaced."""
    b: dict[str, Any] = json.loads(json.dumps(w.build))
    b["version_chain"] = version_chain
    for key, file_id in files.items():
        b["files"][key] = {"file_id": file_id}
    return b


class _Clock(dt.datetime):
    """``datetime`` whose ``now()`` returns :attr:`at` when set (the runner's and the chain
    writer's clock in the B2 scenario: names stamped out of chain order, as clock skew does)."""

    at: ClassVar[dt.datetime | None] = None

    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
        return cls.at if cls.at is not None else dt.datetime.now(tz)


# ---------------------------------------------------------------- the clean world
def test_clean_world_verifies(world: World) -> None:
    results = run_checks(world.root)
    assert summary(results) == CLEAN, red(results)
    assert {r.check for r in results if r.status == "ok"} == {
        n for n, cs in REGISTRY.items() if cs.fn is not None} >= IMPLEMENTED_M1
    rows = view_rows(world.root)
    states = {world.label(d): r["state"] for d, r in rows.items()}
    assert sorted(set(states.values())) == ["acquired", "identified", "retired", "triaged"]
    assert len(rows) == 15 and len(SOURCES) == 16
    a4, o4 = rows[world.doc_id("A4")], rows[world.doc_id("O4")]
    assert o4["identity"]["merged_into"] == a4["doc_id"] and a4["identity"]["former_doc_ids"] == [o4["doc_id"]]
    assert rows[world.doc_id("X")]["identity"]["mask_reason"] == "operator"
    assert rows[world.doc_id("W2")]["content"]["same_raw_doc_ids"] == [world.doc_id("W3")]
    assert resolve(world.root, sdir("sources.arxiv")).version_chain == (
        world.files["sources.arxiv"], world.files["sources.arxiv@part"])


# ---------------------------------------------------------------- step 3: the pointer rule (§5.5)
def point(row: Row) -> tuple[str | None, str | None, str]:
    """§5.5 over one ``documents`` row through ``workflows.acquire.point`` (D83: the test-local
    helper was swapped for the real pointer in M3, T3.1): ``(current_snapshot_id,
    current_text_sha256, current_text_status)`` at the content block's pin."""
    got = acquire_point(row["content"], row["identity"]["members"], row["content"]["convert_pin"])
    return got["current_snapshot_id"], got["current_text_sha256"], got["current_text_status"]


#: The decision table of step 3: anchor label → (the snapshot the pointer takes, status).
POINTER_EXPECTED: dict[str, tuple[str | None, str]] = {
    "A1": ("A1/full_text/live", "ok"),        # full text over the abstract; the xml snapshot never
    "A2": ("A2/full_text/live", "ok"),        # a failed xml role is irrelevant to the pointer
    "A4": ("A4/full_text/live", "ok"),        # the merged member's abstract ranks below
    "A7": (None, "no_raw"),                   # withdrawn, nothing fetched
    "O2": ("O2/full_text/live", "convert_failed"),  # E4 guard: never falls back to the abstract
    "O5": ("O5/abstract", "ok"),              # paywalled: the in-band abstract is current
    "LW": ("AF/post", "ok"),                  # two live members' posts: the newer capture
    "W1": ("W1/page/live", "ok"),             # no wayback over a live candidate, though newer
    "W2": ("W2/post/live", "ok"),
    "W3": ("W3/post/live", "ok"),
}


def test_step3_pointer_rule(world: World) -> None:
    assert POINTER_RANK == ("full_text", "post", "page", "readme", "abstract", "fields")
    rows = view_rows(world.root)
    acquired = {world.label(d) for d, r in rows.items() if r["state"] == "acquired"}
    assert acquired == set(POINTER_EXPECTED)
    for label, (snap, status) in POINTER_EXPECTED.items():
        row = rows[world.doc_id(label)]
        c = row["content"]
        recorded = (c["current_snapshot_id"], c["current_text_sha256"], c["current_text_status"])
        assert point(row) == recorded, label
        assert c["current_snapshot_id"] == (world.snaps[snap] if snap else None), label
        assert c["current_text_status"] == status, label

    # Variations on the world's rows (each one rule of §5.5).
    def variant(label: str, edit: Any) -> tuple[str | None, str | None, str]:
        row = copy.deepcopy(rows[world.doc_id(label)])
        edit(row)
        return point(row)

    def only_xml(r: Row) -> None:
        r["content"]["snapshots"] = [s for s in r["content"]["snapshots"] if s["content_role"] == "xml"]

    assert variant("A1", only_xml) == (None, None, "no_raw")  # xml is never text
    w1_wayback = world.snaps["W1/page/wayback"]
    assert variant("W1", lambda r: r["content"].update(snapshots=[
        s for s in r["content"]["snapshots"] if s["via"] == "wayback"]))[0] == w1_wayback
    lw_post = world.snaps["LW/post"]
    assert variant("LW", lambda r: r["identity"].update(members=[
        m | {"is_live": m["source_id"] != SOURCES["AF"].source_id} for m in r["identity"]["members"]]))[0] == lw_post

    def unconverted(r: Row) -> None:
        r["content"]["snapshots"] = [s | {"text_status": None, "text": None}
                                     if s["kind"] == "raw" else s for s in r["content"]["snapshots"]]

    assert variant("A2", unconverted) == (world.snaps["A2/full_text/live"], None, "convert_pending")
    a2_readme = {**rows[world.doc_id("A2")]["content"]["snapshots"][1], "content_role": "readme",
                 "snapshot_id": f"{rid(20)}.99"}
    assert variant("A2", lambda r: r["content"].update(snapshots=[
        s for s in r["content"]["snapshots"] if s["content_role"] != "full_text"] + [a2_readme]))[0] == (
        f"{rid(20)}.99")  # readme ranks over abstract


# ---------------------------------------------------------------- step 8: failed run, dead records
_ACQUIRE_BOOM = """
    from stack.stage_runner import StageResult
    def run(ctx):
        rows = ctx.view("documents").slice(0, 2)
        ctx.heartbeat(inflight_keys=rows.column("doc_id").to_pylist())
        ctx.emit_part("documents", rows, ["content"])
        raise RuntimeError("injected failure mid-acquire")
"""


def test_step8_failed_run_and_dead_records(world: World, plan: Any, stage_dir: Path) -> None:
    root = world.root
    toy_stage(stage_dir, "acquire", _ACQUIRE_BOOM)
    names, head, listing = data_names(root), docs_head(root), cli(root, "ls")
    rec = run_on_docs(root, plan, "acquire")
    assert (rec.status, rec.error_class, rec.outputs) == ("failed", "code_bug", {})
    assert "injected failure mid-acquire" in (rec.error or "")
    on_disk = find_run_record(root, rec.run_id)
    assert on_disk is not None and on_disk.status == "failed"
    assert data_names(root) == names and docs_head(root) == head
    assert cli(root, "ls") == listing  # every series directory is untouched (`stack latest` is gone)
    assert files_without_ok_record(root) == [] and summary(run_checks(root)) == CLEAN

    # Two dead `running` acquire records sharing one inflight key (the mock's A1/full_text).
    key = f"{world.doc_id('A1')}:full_text"
    # X11 reads liveness from the store index (D344): each record gets a `-running.json` marker
    # last touched an hour ago (older than DEAD_AFTER), as a job killed then would leave it
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).timestamp()
    for n, keys in ((50, [key, "other"]), (51, [key])):
        dead_rec = RunRecord(
            run_id=rid(n), stage="acquire", writer="acquire", plan="dev-v2", status="running",
            started_at=AT, heartbeat_at=AT, code_digest=dg("code"), config_digest=dg("config"),
            inflight=keys)
        path = write_running_record(root, dead_rec)
        marker = path.with_name(path.name.removesuffix(".json.zst") + "-running.json")
        marker.write_text("{}")
        os.utime(marker, (old, old))
        assert run_record_path(root, dead_rec) == path
    assert [e.run_id for e in store_index(root).runs if e.liveness == "dead"] == [rid(50), rid(51)]
    assert collect_status(root)["repeated_inflight"] == [{"key": key, "runs": [rid(50), rid(51)]}]
    watched = ("data", "blobs", "overrides")

    def snapshot() -> list[tuple[str, int]]:
        return sorted((str(p), p.stat().st_mtime_ns) for k in watched for p in (root / k).rglob("*"))

    before = snapshot()
    out = cli(root, "status")
    dead = [ln.split()[2:4] for ln in out.splitlines() if "DEAD" in ln]
    assert dead == [[rid(51), "acquire"], [rid(50), "acquire"]], out  # newest first
    assert f"repeated_inflight (X11): key '{key}' inflight in dead runs {rid(50)}, {rid(51)}" in out
    assert snapshot() == before  # the detector writes nothing (no charge: D1)
    # X11 (T5.1b) warns on the shared key; a warning, never a failure
    x11 = by_check(run_checks(root, names=["dead_runs_seen"]))["dead_runs_seen"]
    assert (x11.status, x11.level) == ("warn", "warn") and key in x11.items[0], x11.items
    # nothing finalises a dead run (gc --finalise-dead is gone): the warning stays, nothing fails
    assert set(red(run_checks(root))) == {"dead_runs_seen"}


# ---------------------------------------------------------------- step 9: broken sets
def test_step9_broken_sets(world: World, plan: Any) -> None:
    root = world.root
    a1 = world.doc_id("A1")
    base = world.files["documents"]

    # X2-style lineage: a part on an exact file that is no longer its directory's last file would
    # fork the directory, and is refused at dispatch (D381; X2
    # `lineage_equality` itself, T5.1b, finds nothing: the refused run left no record).
    rec = run_on_docs(root, plan, "echo_test")
    assert rec.status == "ok"
    n_records = len(read_run_records(root))
    base_path = store_index(root).output(base)[2]["path"]
    with pytest.raises(StageError, match="would fork the directory"):
        run_on_docs(root, plan, "echo_test", [base_path])
    assert len(read_run_records(root)) == n_records and len(list((root / "runs").glob("*.json.zst"))) == n_records
    assert by_check(run_checks(root, names=["lineage_equality"]))["lineage_equality"].status == "ok"
    assert summary(run_checks(root)) == CLEAN

    # S3: a duplicated key is refused by the writer; nothing is published.
    names, head = data_names(root), docs_head(root)
    row = view_rows(root)[a1]
    with pytest.raises(SchemaRefused) as exc:
        write_on_head(root, "documents", "documents", [row, row], stage="triage", run=30,
                      blocks_written=["annotations"])
    assert exc.value.checks == {"key_unique"}
    assert data_names(root) == names and docs_head(root) == head

    # X15: a triage part may rewrite `annotations` (green) but not `content` (red, X15 only).
    ok_row = dict(row, annotations=dict(row["annotations"], triage=dict(
        row["annotations"]["triage"], decided_at=AT + dt.timedelta(days=2))), updated_at=AT + dt.timedelta(days=2))
    write_on_head(root, "documents", "documents", [ok_row], stage="triage", run=31,
                  blocks_written=["annotations"])
    assert summary(run_checks(root)) == CLEAN
    bad = dict(ok_row, content=dict(ok_row["content"], current_text_quality="flagged"))
    pub = write_on_head(root, "documents", "documents", [bad], stage="triage", run=32,
                        blocks_written=["annotations"])
    results = run_checks(root)
    assert set(red(results)) == {"block_ownership"}
    assert list(red(results)["block_ownership"]) == [f"{pub.file_id}: {a1} changes content (writes ['annotations'])"]


# ---------------------------------------------------------------- step 12: store kinds
def test_step12_store_kinds(world: World) -> None:
    root = world.root
    for p in sorted((root / DATA).rglob("*.parquet")):
        tbl, h = read_family(p)
        assert tbl.schema.equals(family(h["family"]).schema, check_metadata=False), p.name
        assert h["schema_digest"] == family(h["family"]).schema_digest
    listing = json.loads(cli(root, "ls", "--json"))  # every series directory (D375)
    by_dir = {c["dir"]: c for c in listing}
    want = {"documents", "coverage.arxiv", "coverage.openalex",
            *(f"sources.{ch}" for ch in ("arxiv", "openalex", "lesswrong", "alignmentforum", "x", "web"))}
    assert set(by_dir) == {sdir(f).rstrip("/") for f in want}
    for key, c in by_dir.items():
        assert c["error"] is None and c["head"] == resolve(root, key + "/").head_name, key
    arxiv = resolve(root, sdir("sources.arxiv"))
    assert by_dir[sdir("sources.arxiv").rstrip("/")]["version"] == [f.name for f in arxiv.files]
    assert arxiv.version_chain == (world.files["sources.arxiv"], world.files["sources.arxiv@part"])

    out = json.loads(cli(root, "fold", "corpus", "--input", f"documents={DOCS}", "--out", sdir("corpus")))
    assert out["status"] == "ok"
    path = root / out["outputs"]["corpus"]
    tbl, h = read_family(path)
    assert (h["chain_id"], h["series"], h["base"], h["kind"], h["writer"], h["stage"]) == (
        "corpus", "main", "", "full", "fold", "fold.corpus")
    assert resolve(root, sdir("corpus")).version_chain == (h["file_id"],)  # the first file of corpus/main/
    has_text = sorted(world.label(d) for d in tbl.column("doc_id").to_pylist())
    assert has_text == ["A1", "A2", "A4", "LW", "W1", "W2", "W3"]  # O5 is txt, O2 failed, A7 no_raw
    corpus = [c for c in json.loads(cli(root, "ls", "--json")) if c["dir"].startswith("data/corpus/")]
    assert [(c["dir"], c["head"]) for c in corpus] == [("data/corpus/main", path.name)]
    assert summary(run_checks(root)) == CLEAN  # the fold run is a run to X1
    assert sorted(p.name for p in root.iterdir()) == ["blobs", "builds", "cache", "data", "releases", "runs"]


def test_build_order_by_finished_at(world: World) -> None:
    """D80: the checks' reference build (X10, X23) is the newest ``ok``
    build by ``finished_at``, not by file name: here the later-named record finished earlier,
    and a ``failed`` build that finished last (and is named last) is never the reference."""
    root = world.root
    # the build directory's name carries started_at (D344): days 4, 5, 6
    early_name_late_finish = dict(world.build, build_id=mint("b", 2),
                                  started_at=(AT + dt.timedelta(days=4)).isoformat(),
                                  finished_at=(AT + dt.timedelta(days=3)).isoformat())
    late_name_early_finish = dict(world.build, build_id=mint("b", 3),
                                  started_at=(AT + dt.timedelta(days=5)).isoformat(),
                                  finished_at=(AT + dt.timedelta(days=1)).isoformat(),
                                  version_chain=["broken"])
    failed_last = dict(world.build, build_id=mint("b", 4), status="failed",
                       started_at=(AT + dt.timedelta(days=6)).isoformat(),
                       finished_at=(AT + dt.timedelta(days=6)).isoformat(), version_chain=["broken"])
    for rec in (early_name_late_finish, late_name_early_finish, failed_last):
        path = build_record_path(root, rec)
        path.parent.mkdir(parents=True)
        path.write_bytes(zstandard.ZstdCompressor().compress(json.dumps(rec).encode()))
    names = sorted(p.name for p in (root / BUILDS_DIR).iterdir() if p.is_dir())
    assert [n.split("_")[1].split(".")[0] for n in names[-3:]] == [mint("b", i) for i in (2, 3, 4)]
    assert names[-1].endswith(f"_{mint('b', 4)}.dev-v2")
    newest = last_build(root)
    assert newest is not None and newest["build_id"] == mint("b", 2)
    # (`stack latest` went with D375)
    assert summary(run_checks(root)) == CLEAN  # X10 reads b2's version_chain, not b3's "broken"


# ---------------------------------------------------------------- mutation rows
_COLLECT_NULL = """
    from stack.stage_runner import StageResult
    from tests.synth.rows import table
    from tests.synth.world import sources_row
    def run(ctx):
        row = sources_row("A1", 30)
        row["metadata"]["origin"] = None  # M1: a NULL into a non-nullable column
        ctx.emit_full("sources", table("sources", [row]), chain_id="sources.arxiv")
        return StageResult()
"""


def test_mutation_m1_null_in_non_nullable(world: World, plan: Any, stage_dir: Path) -> None:
    """M1: the collector writes ``metadata.origin = NULL``: the family writer refuses the file
    (S2 ``nonnull``), the collect run fails, ``data/`` is unchanged."""
    root = world.root
    toy_stage(stage_dir, "collect/arxiv", _COLLECT_NULL)
    names = data_names(root)
    rec = run_line(root, plan, "collect.arxiv")
    assert (rec.status, rec.error_class) == ("failed", "code_bug")
    assert "SchemaRefused" in (rec.error or "") and "nonnull" in (rec.error or "")
    assert "metadata.origin" in (rec.error or "")
    assert data_names(root) == names and summary(run_checks(root)) == CLEAN


_ACQUIRE_DUP_ROLE = """
    import pyarrow as pa
    from stack.stage_runner import StageResult
    def run(ctx):
        view = ctx.view("documents")
        rows = [r for r in view.to_pylist() if r["doc_id"] == {doc!r}]
        rows[0]["content"]["roles"] = rows[0]["content"]["roles"] + rows[0]["content"]["roles"][:1]
        ctx.emit_part("documents", pa.Table.from_pylist(rows, schema=view.schema), ["content"])
        return StageResult()
"""


def test_mutation_m9_duplicate_role(world: World, plan: Any, stage_dir: Path) -> None:
    """M9: a role entry duplicated in ``content.roles``: S5 ``nested_unique`` refuses the file,
    the acquire run fails, ``data/`` is unchanged."""
    root = world.root
    toy_stage(stage_dir, "acquire", _ACQUIRE_DUP_ROLE.replace("{doc!r}", repr(world.doc_id("A1"))))
    names = data_names(root)
    rec = run_on_docs(root, plan, "acquire")
    assert (rec.status, rec.error_class) == ("failed", "code_bug")
    assert "SchemaRefused" in (rec.error or "") and "nested_unique" in (rec.error or "")
    assert data_names(root) == names and summary(run_checks(root)) == CLEAN


# ---------------------------------------------------------------- B2 through the runner
#: A toy stage editing the triage decision time (and ``updated_at``) of the keys in ``EDITS``
#: (doc_id → ISO time); one part file per run.
_EDIT_PART = """
    import datetime as dt
    import pyarrow as pa
    from stack.stage_runner import StageResult
    EDITS = {edits!r}
    def run(ctx):
        view = ctx.view("documents")
        rows = [r for r in view.to_pylist() if r["doc_id"] in EDITS]
        assert len(rows) == len(EDITS)
        for r in rows:
            r["annotations"]["triage"]["decided_at"] = r["updated_at"] = dt.datetime.fromisoformat(EDITS[r["doc_id"]])
        ctx.emit_part("documents", pa.Table.from_pylist(rows, schema=view.schema), "*")
        return StageResult(scope_complete=True, rows_touched=len(rows))
"""


def test_b2_chain_through_the_runner(world: World, plan: Any, stage_dir: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """The B2 mock's shape through the real runner: the world's ``documents`` full file W, part
    files S1-S3 of a toy stage that edits rows (S2's clock is skewed: it starts earlier than S1
    by the clock), ``stack fold compact`` → C1, one more part → S5. Per key, the view and the
    compacted file carry the value of the newest file in chain order (§7.3, D82); the store
    verifies. In a series directory the chain is the name order: the tie guard stamps S2 one
    second after S1, its input, whatever the clock says (D372).

    S1 edits A1, A2, A3; S2 edits A1, A2; S3 edits A1; S5 edits A3. Newest per key in chain
    order: A1 from S3, A2 from S2, A3 from S1 (then S5); the oldest row per key would give W's
    values."""
    root = world.root
    clock = types.SimpleNamespace(datetime=_Clock, timedelta=dt.timedelta, UTC=dt.UTC)
    monkeypatch.setattr(stage_runner, "dt", clock)
    monkeypatch.setattr(chain_mod, "dt", clock)
    day = AT + dt.timedelta(days=1)
    expected = {r["doc_id"]: r for r in read_view(resolve(root, DOCS)).to_pylist()}
    w = world.files["documents"]

    def value(n: int) -> dt.datetime:  # the part's edit value, distinct per part
        return AT + dt.timedelta(hours=12, minutes=n)

    def edit_part(n: int, labels: str) -> str:
        edits = {world.doc_id(lb): value(n).isoformat() for lb in labels.split()}
        toy_stage(stage_dir, "edit_test", _EDIT_PART.replace("{edits!r}", repr(edits)))
        rec = run_on_docs(root, plan, "edit_test")
        assert rec.status == "ok", rec.error
        for d in edits:  # the expected view: this part's rows over everything before it
            expected[d] = copy.deepcopy(expected[d])
            expected[d]["annotations"]["triage"]["decided_at"] = expected[d]["updated_at"] = value(n)
        return rec.outputs["documents"]["file_id"]

    def decided(rows: list[Row]) -> dict[str, dt.datetime]:
        return {world.label(r["doc_id"]): r["annotations"]["triage"]["decided_at"]
                for r in rows if world.label(r["doc_id"]) in ("A1", "A2", "A3")}

    def want() -> list[Row]:
        return [expected[d] for d in sorted(expected)]

    parts = []
    for n, (minute, labels) in enumerate(((5, "A1 A2 A3"), (1, "A1 A2"), (7, "A1")), start=1):
        _Clock.at = day.replace(hour=12, minute=minute)  # S2 runs "earlier" by the clock than S1
        parts.append(edit_part(n, labels))
    _Clock.at = None
    s1, s2, s3 = parts
    ch = resolve(root, DOCS)
    assert ch.version_chain == (w, s1, s2, s3)
    names = sorted((root / DATA / "documents" / "main").glob("*.parquet"))
    assert tuple(read_header(p)["file_id"] for p in names) == ch.version_chain  # the name order is the chain
    stamp = {read_header(p)["file_id"]: dt.datetime.strptime(p.name[:16], "%Y%m%dT%H%M%SZ") for p in names}
    assert stamp[s2] == stamp[s1] + dt.timedelta(seconds=1)  # the tie guard over S2's skewed clock
    view = read_view(ch).to_pylist()
    assert decided(view) == {"A1": value(3), "A2": value(2), "A3": value(1)}
    assert view == want()
    walk = build_with(world, [*ch.version_chain, *world.build["version_chain"][1:]], documents=s3)
    swapped = build_with(world, [w, s2, s1, s3, *world.build["version_chain"][1:]], documents=s3)
    assert summary(run_checks(root, build=walk)) == CLEAN
    assert set(red(run_checks(root, build=swapped))) == {"ordering_pinned"}  # X10 catches a wrong order

    out = json.loads(cli(root, "fold", "compact", "--input", f"documents={DOCS}"))
    assert out["status"] == "ok"
    compacted, c1_header = read_family(root / out["outputs"]["documents"])
    c1 = c1_header["file_id"]
    assert compacted.to_pylist() == want()  # the pre-compaction union, newest per key
    s5 = edit_part(5, "A3")
    ch = resolve(root, DOCS)
    assert ch.version_chain == (c1, s5)
    view = read_view(ch).to_pylist()
    assert decided(view) == {"A1": value(3), "A2": value(2), "A3": value(5)}
    assert view == want()
    assert {s1, s2, s3} <= {read_header(p)["file_id"] for p in (root / DATA).rglob("*.parquet")}  # kept, not read
    results = run_checks(root, build=build_with(world, [c1, s5, *world.build["version_chain"][1:]], documents=s5))
    assert summary(results) == CLEAN
    checked = by_check(results)
    for name in ("V-chain-linear", "V-compaction-equal", "finished_files", "ordering_pinned",
                 "created_preserved", "full_covers_base", "head_descends"):
        assert checked[name].status == "ok", name
