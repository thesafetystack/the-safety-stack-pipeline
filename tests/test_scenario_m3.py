"""M3 scenario (PLAN T3.1): the integration gate on the M1 synthetic world
(``tests/synth/world.py``) and ``stack run acquire --only point`` through the CLI.

The gate: gather (the world's full file) → triage (part) → acquire (part) → acquire (part), each
run through the real runner re-pinning the current head: a chain of four files, V11, X15, V6 and
the triage checks green, the second acquire touching only the row that was due. Lanes are
:class:`tests.fakes.lanes.FakeLane` (no network); the in-band blobs the world's collectors would
have minted are planted under the store's ``blobs/`` (the world records labelled digests only).

**Triage** is the real stage (``workflows/triage``, T2.9) under the test plan's ``sql:``-only gate
:data:`GATE_FETCH` (the world's ``fetch`` rows plus A3 and A6): every unmasked, non-retired row is
in scope (the world's annotations carry another ``config_digest``), A3 is re-decided ``skip`` →
``fetch`` and A6 goes ``identified`` → ``triaged`` ``fetch``, so acquire has two rows with fetch work.

**Gather** stays the world's hand-built full file. The real ``gather`` over the world's ``sources``
chains does not reproduce it: (a) it refuses the run, the X thread's channel ``x`` is not in
``configs/channels.yaml`` (``GatherError``); (b) with ``x`` patched in, it re-derives every row
(A4+O4 and LW+AF are not merged from the world's ``sources`` keys, every ``fields_digest`` moves,
so all 16 rows go back to ``identified``), so the world's ``content`` referents would no longer
apply. Running it here needs world/channel changes outside track B (reported, T3.1-f).
"""

from __future__ import annotations

import datetime as dt
import json
import types
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from stack import stage_runner
from stack.checks.verify import run_checks, summary
from stack.cli import app
from stack.conversion.quality import DEFAULT_GATE
from stack.plan import Plan, load_plan
from stack.store.backend import LocalBackend
from stack.store.chain import read_view, resolve
from stack.store.parquet import read_family, read_header
from tests.fakes.lanes import FakeLane
from tests.fixtures.checks import T, id_gate
from tests.synth.records import run_line, sdir
from tests.synth.rows import rid
from tests.synth.world import (
    CONVERT,
    FETCHED,
    INBAND,
    SOURCES,
    World,
    build_world,
    write_on_head,
)
from workflows.acquire import lanes
from workflows.acquire.convert import CONFIGS, convert_pin, pin_of
from workflows.acquire.quality import needs_verdict
from workflows.acquire.scope import SCOPE_STATES

pytestmark = [pytest.mark.slow, pytest.mark.e2e]

Row = dict[str, Any]
NOW = T(30)
#: The documents the triage gate of :data:`PLAN` says ``fetch``: the world's :data:`FETCHED` plus
#: A3 (the world's ``skip``) and A6 (``identified``), so acquire has two rows with fetch work.
GATE_FETCH = (*FETCHED, "A3", "A6")
PLAN: dict[str, Any] = {
    "plan": "test-m3", "schema_version": 2, "series": "main",
    "gate": id_gate({"screen": [SOURCES[lb].source_id for lb in GATE_FETCH]}).file_shape(),
    "acquire": {"convert": {"config": "pymupdf4llm-v1"},
                "wants": {"arxiv": ["full_text"], "lesswrong": ["post"]}},
    # Runs in any venv: the convert pin guard (R4-1) is lifted, the pin is this venv's either way.
    "stages": [{"stage": "triage", "out": {"kind": "part"}},
               {"stage": "acquire", "out": {"kind": "part"}, "options": {"allow_degraded_convert": True}}]}
GATE_CHECKS = ("V-chain-linear", "block_ownership", "V-content", "attempts_monotone", "created_preserved",
               "digest_agreements", "state_blocks", "V-state", "V-digests", "finished_files",
               "V-annotations", "gate_obeyed", "gate_union", "state_monotone", "triage_current")


def sid(label: str) -> str:
    return SOURCES[label].source_id


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = build_world(tmp_path / "store")
    blobs = LocalBackend(w.root / "blobs")
    for label, entries in INBAND.items():
        for _role, raw, text, _media, _at in entries:
            for bucket, digest in (("raw", raw), ("text", text)):
                if digest is not None:
                    blobs.put(digest, inband_body(label, bucket))  # flat blobs (D360)
    return w


def inband_body(label: str, bucket: str) -> bytes:
    """An in-band body of a real abstract's length, so the quality gate (round 3, Q) passes it
    without a flag (an abstract is flagged below 300 chars, a forum post below 500)."""
    return (f"{label} {bucket} body. " + "An abstract-length sentence about the document and what it reports. " * 12
            ).encode()


def first_verdicts(root: Path) -> set[str]:
    """Rows whose current text has no quality verdict yet (round 3, Q): the next acquire judges
    them (``quality`` scope clause) and writes them, once."""
    return {d for d, r in view(root).items()
            if r["state"] in SCOPE_STATES and needs_verdict(r, CONVERT, DEFAULT_GATE.digest())}


def view(root: Path) -> dict[str, Row]:
    return {r["doc_id"]: r for r in read_view(resolve(root, sdir("documents"))).to_pylist()}


def rec1_stamp(rec: Any) -> str:
    (stamp,) = [k.removeprefix("convert.converter.") for k in rec.counts if k.startswith("convert.converter.")]
    return stamp


def part_ids(root: Path, rec: Any) -> set[str]:
    tbl, _ = read_family(root / rec.outputs["documents"]["path"])
    return set(tbl.column("doc_id").to_pylist())


def role(row: Row, label: str, content_role: str = "full_text") -> Row:
    return next(r for r in row["content"]["roles"] if (r["source_id"], r["content_role"]) == (sid(label), content_role))


def page(label: str) -> bytes:
    """An HTML body the convert child turns into a paper-length text (≥ 8,000 chars: the quality
    gate neither fails nor flags it as a full text, round 3, Q)."""
    return (f"<html><head><title>{label}</title></head><body><p>"
            + f"Body text of {label}, a genuine paragraph. " * 220 + "</p></body></html>").encode()


def _frozen_dt(at: dt.datetime) -> types.SimpleNamespace:
    """The ``datetime`` module as ``workflows.triage.main`` uses it, with ``datetime.now`` fixed at ``at``."""

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            return at if tz is None else at.astimezone(tz)

    ns = types.SimpleNamespace(**{k: getattr(dt, k) for k in dir(dt) if not k.startswith("__")})
    ns.datetime = Frozen
    return ns


def test_chain_four_files(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    root, plan = world.root, Plan.model_validate(PLAN)
    assert convert_pin(plan.acquire) == pin_of(CONFIGS["pymupdf4llm-v1"]) == CONVERT, \
        "the test plan must pin the world's convert config"
    # Triage stamps rows with the wall clock; the acquire runs below use the fixed clock NOW = T(30)
    # (2026-10-13 07:30 UTC). Once the wall clock passed NOW, acquire's updated_at was earlier than
    # triage's and X17 (created_preserved) rightly failed the chain. Triage runs at T(29): after the
    # world's stamps (T(20) at most), before acquire's. (run_stage loads the stage module afresh on
    # every run, so the clock goes into the module it loads.)
    load = stage_runner.load_stage_module

    def load_triage_at_t29(stage: str) -> Any:
        mod = load(stage)
        if stage == "triage":
            mod.dt = _frozen_dt(T(29))
        return mod

    with monkeypatch.context() as m:
        m.setattr(stage_runner, "load_stage_module", load_triage_at_t29)
        rec0 = run_line(root, plan, "triage")
    assert rec0.status == "ok", rec0.error
    v = view(root)
    assert (rec0.counts["scoped"], rec0.counts["fetch"], rec0.counts["skip"]) == (13, 12, 1)  # not X, O4
    assert rec0.outputs["documents"]["kind"] == "part" and rec0.version["rows_touched"] == 13
    assert v[sid("A6")]["state"] == "triaged"
    assert {lb: v[sid(lb)]["annotations"]["triage"]["payload"]["decision"] for lb in ("A3", "A6", "O3")} \
        == {"A3": "fetch", "A6": "fetch", "O3": "skip"}
    lane = FakeLane.answering("arxiv", {(sid("A3"), "full_text"): page("A3"),
                                        (sid("A6"), "full_text"): ["429", page("A6")]}, default="dead",
                              media_type="text/html")

    # acquire 1: A3 fetched and converted (the ladder's HTML path in the child, any venv); A6 hits
    # a 429 (halt: no charge, scope incomplete); O3 (skip, content NULL) and the in-band abstracts
    # are minted whatever the gate said.
    judged = first_verdicts(root)
    assert judged and sid("A3") not in judged  # the world's in-band texts, never judged before
    with lanes.using({"arxiv": lane}, now=NOW):
        rec1 = run_line(root, plan, "acquire")
    assert rec1.status == "ok", rec1.error
    assert lane.keys() == [(sid("A3"), "full_text"), (sid("A6"), "full_text")]
    assert not rec1.scope_complete and rec1.counts["halted"] == 1
    assert part_ids(root, rec1) == {sid("A3"), sid("A6"), sid("O3")} | judged
    assert first_verdicts(root) == set(), {k: v for k, v in rec1.counts.items() if k.startswith("quality")}
    assert rec1.counts["quality.ok"] == rec1.counts["quality.measured"], {k: v for k, v in rec1.counts.items() if k.startswith("quality")}
    v = view(root)
    a3, a6, o3 = v[sid("A3")], v[sid("A6")], v[sid("O3")]
    assert {r["state"] for r in (a3, a6, o3)} == {"acquired"}
    assert role(a3, "A3")["status"] == "ok" and role(a3, "A3", "abstract")["lane"] == "inband"
    assert (a3["content"]["current_role"], a3["content"]["current_text_status"]) == ("full_text", "ok")
    cur = next(s for s in a3["content"]["snapshots"] if s["snapshot_id"] == a3["content"]["current_snapshot_id"])
    assert (cur["text_status"], cur["convert_attempts"], cur["convert_pin"]) == ("ok", 1, CONVERT)
    assert a3["content"]["current_text_quality"] == "ok" and rec1.counts["convert.ok"] == 1
    assert (cur["quality"]["bucket"], cur["quality"]["converter"]) == ("ok", rec1_stamp(rec1))
    r6 = role(a6, "A6")
    assert (r6["status"], r6["error_class"], r6["attempts"]) == ("not_attempted", "halted", 0)
    assert (a6["content"]["current_role"], a6["content"]["current_text_status"]) == ("abstract", "ok")
    assert (o3["content"]["current_text_status"], o3["content"]["roles"]) == ("no_raw", [])

    # acquire 2: only A6 is due; it alone is fetched and written.
    with lanes.using({"arxiv": lane}, now=T(31)):
        rec2 = run_line(root, plan, "acquire")
    assert rec2.status == "ok", rec2.error
    assert rec2.scope_complete and lane.keys()[2:] == [(sid("A6"), "full_text")]
    assert part_ids(root, rec2) == {sid("A6")} and rec2.version["rows_touched"] == 1
    a6 = view(root)[sid("A6")]
    assert role(a6, "A6")["status"] == "ok" and a6["content"]["current_role"] == "full_text"
    assert a6["content"]["current_text_status"] == "ok" and rec2.counts["convert.ok"] == 1

    chain = resolve(root, sdir("documents"))
    assert len(chain.version_chain) == 4 and [f.kind for f in chain.files] == ["full", "part", "part", "part"]
    assert [read_header(p)["stage"] for p in chain.paths] == ["gather", "triage", "acquire", "acquire"]
    assert chain.version_chain[0] == world.files["documents"]
    results = run_checks(root)
    by = {r.check: r for r in results}
    print(summary(results))
    for name in GATE_CHECKS:
        assert by[name].status == "ok", (name, by[name].items)


def test_only_point_through_the_cli(world: World, tmp_path: Path) -> None:
    """``stack run acquire --only point`` on the world head (after a gather masking LW's AF
    member): a part row for every in-scope document whose ``content`` changes (A3 and O3 get their
    first block, LW re-points to its own post), nothing for the rest, zero lane calls. Then
    ``--only convert`` makes zero lane calls too."""
    root = world.root
    rows = view(root)
    lw = rows[sid("LW")]
    for m in lw["identity"]["members"]:
        m["is_live"] = m["source_id"] != sid("AF")
    lw["identity"]["run_id"], lw["identity"]["updated_at"] = rid(22), T(22)
    lw["run_id"], lw["updated_at"] = rid(22), T(22)
    write_on_head(root, "documents", "documents", list(rows.values()), stage="gather", run=22, kind="full",
                  blocks_written=["identity", "metadata", "inband"])
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(PLAN))
    judged = first_verdicts(root)
    lane = FakeLane.answering("arxiv", {}, default=b"never")
    http = FakeLane.answering("http", {}, default=b"never")
    with lanes.using({"arxiv": lane, "http": http}, now=NOW):
        res = CliRunner().invoke(app, ["--store", str(root), "--plan", str(plan_path), "run", "acquire",
                                       "--in", sdir("documents"), "--part", "--only", "point"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output.strip().splitlines()[-1])
    assert out["status"] == "ok" and out["scope_complete"] is True
    tbl, _ = read_family(root / out["outputs"]["documents"])
    written = {r["doc_id"]: r for r in tbl.to_pylist()}
    assert set(written) == {sid("A3"), sid("O3"), sid("LW")} | judged
    assert written[sid("LW")]["content"]["current_snapshot_id"] == world.snaps["LW/post"]
    assert role(written[sid("A3")], "A3")["error_class"] == "gated"  # skip: never fetched
    assert lane.calls == [] and http.calls == []

    with lanes.using({"arxiv": lane, "http": http}, now=NOW):
        rec = run_line(root, load_plan(plan_path), "acquire", only=["convert"])
    assert rec.status == "ok" and rec.outputs == {} and lane.calls == [] and http.calls == []
    assert rec.substages["fetch"]["ran"] is False and rec.substages["history"]["ran"] is False
    assert [r.status for r in run_checks(root, names=["V-content", "block_ownership"])] == ["ok", "ok"]
