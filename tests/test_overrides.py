"""The ``overrides`` family (design v4.1 §8, D336-D338): the input formats (today's YAML entries,
CSV), ``overrides.add``'s checks and chain start, the effective rule (tier, time, id; revokes and
their rules), the scope check and field-over-row composition in ``select``, and the CLI verb."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from stack.cli import app
from stack.overrides import (
    Override,
    OverrideError,
    check_add,
    drop_revoked,
    effective,
    read_file,
    rows_of,
    run_add,
    winners,
)
from stack.runs import find_run_record
from stack.stage_runner import run_stage
from stack.store.chain import EmptyChainError, read_view, resolve
from stack.store.parquet import read_header

NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)
#: The overrides series directory the tests add to (``stack overrides add --out``).
OUT = "data/overrides/main/"


def row(oid: str, *, tier: str = "human", at: int = 0, key: str = "sa", field: str = "title",
        target: str = "fields", kind: str = "field", revokes: str | None = None, **more: Any) -> Override:
    return Override(override_id=oid, target=target, key=key, item_key="doc_id" if target == "fields" else "",
                    kind=kind, field=field if kind != "revoke" else "", payload=more.get("payload", {"value": oid}),
                    tier=tier, created_at=NOW + dt.timedelta(hours=at), revokes=revokes)


def ids(rows: Any) -> list[str]:
    return [o.override_id for o in rows]


# ---------------------------------------------------------------- input formats
def test_yaml_entries_become_rows(tmp_path: Path) -> None:
    """Today's entry format, a mapping of targets (or one target named by the file's stem):
    ``revokes`` and ``supersedes`` become revoke rows, keys and payloads as the family says."""
    p = tmp_path / "add.yaml"
    p.write_text(textwrap.dedent("""
        identity:
        - {override_id: ov1, value: merge, key: {source_ids: [sb, sa, sb]}, author: tomas}
        - {override_id: ov2, value: mask, key: {source_id: sc}, mask_reason: review_required}
        - {override_id: ov3, revokes: ov2, note: wrong one}
        fields:
        - {override_id: f1, doc_id: sa, field: published_at, value: 2026-01-01}
        screen:
        - {override_id: w1, key: {doc_id: sa}, row: true, value: {verdict: relevant}, scope: {members: [sa]}}
        - {override_id: w2, key: {doc_id: sb}, quarantine: true, origin: ai_check, created_at: 2026-10-01T10:00:00}
        - {override_id: w3, supersedes: w2, key: {doc_id: sb, item_key: x}, field: verdict, value: not_relevant}
    """))
    rows = check_add(read_file(p, now=NOW), [])
    by = {o.override_id: o for o in rows}
    assert ids(rows) == ["ov1", "ov2", "ov3", "f1", "w1", "w2", "w3.supersedes", "w3"]
    assert (by["ov1"].key, by["ov1"].payload, by["ov1"].author) == ('["sa","sb"]', {"value": "merge"}, "tomas")
    assert (by["ov2"].key, by["ov2"].payload) == ("sc", {"value": "mask", "mask_reason": "review_required"})
    # a revoke copies the revoked row's keys for reading
    assert (by["ov3"].kind, by["ov3"].revokes, by["ov3"].key, by["ov3"].payload) == ("revoke", "ov2", "sc",
                                                                                      {"note": "wrong one"})
    assert (by["f1"].key, by["f1"].item_key, by["f1"].field, by["f1"].value) == ("sa", "doc_id", "published_at",
                                                                                 "2026-01-01")
    assert (by["w1"].kind, by["w1"].scope_members, by["w1"].value) == ("row", ("sa",), {"verdict": "relevant"})
    assert (by["w2"].tier, by["w2"].created_at) == ("ai_check", dt.datetime(2026, 10, 1, 10, tzinfo=dt.UTC))
    assert by["w1"].created_at == NOW  # no created_at: the add's time
    assert (by["w3.supersedes"].revokes, by["w3"].kind, by["w3"].item_key) == ("w2", "field", "x")
    eff = effective(rows)
    assert sorted(ids(eff.rows)) == ["f1", "ov1", "w1", "w3"] and eff.warnings == ()
    # a file of one target, named by its stem, reads the same
    q = tmp_path / "screen.yaml"
    q.write_text("- {override_id: w9, key: {doc_id: sa}, quarantine: true}\n")
    assert [(o.target, o.kind) for o in read_file(q, now=NOW)] == [("screen", "quarantine")]


@pytest.mark.parametrize(("text", "needle"), [
    ("identity:\n- {override_id: x, value: merge, key: {source_ids: [sa]}}\n", "two or more"),
    ("screen:\n- {override_id: x, key: {doc_id: sa}, row: true, quarantine: true, value: {}}\n", "exactly one of"),
    ("fields:\n- {override_id: x, doc_id: sa, source_id: sb, field: title, value: y}\n", "exactly one of"),
    ("screan:\n- {override_id: x, key: {doc_id: sa}, quarantine: true}\n", "unknown override target"),
    ("triage:\n- {override_id: x, key: {doc_id: sa}, quarantine: true}\n", "unknown override target"),
    ("identity:\n- {override_id: x, value: mask, key: {source_id: sa}}\n- {override_id: x, revokes: y}\n",
     "listed twice"),
])
def test_invalid_input_is_refused(tmp_path: Path, text: str, needle: str) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(text)
    with pytest.raises(OverrideError, match=needle):
        check_add(read_file(p, now=NOW), [])


def test_csv_reads_as_the_yaml(tmp_path: Path) -> None:
    """A CSV with the family's columns is validated through the same entry models and gives the
    same rows as its YAML twin."""
    y = tmp_path / "in.yaml"
    y.write_text(textwrap.dedent("""
        identity:
        - {override_id: m1, value: merge, key: {source_ids: [sa, sb]}, created_at: 2026-10-01T00:00:00Z}
        - {override_id: k1, value: mask, key: {source_id: sc}, created_at: 2026-10-01T00:00:00Z}
        fields:
        - {override_id: f1, source_id: sa, field: title, value: T, created_at: 2026-10-01T00:00:00Z}
        screen:
        - {override_id: w1, key: {doc_id: sa}, field: verdict, value: relevant, origin: ai_check,
           scope: {members: [sa]}, created_at: 2026-10-01T00:00:00Z}
        - {override_id: w2, revokes: w1, origin: ai_check, created_at: 2026-10-01T00:00:00Z}
        manual_sources:
        - {override_id: ms1, url: 'https://arxiv.org/abs/2409.15747', flags: [accept], note: n, added_by: tomas,
           added_at: 2026-10-01T00:00:00Z, title: T, authors: [A. Author], published_at: 2024-09-24}
    """))
    c = tmp_path / "in.csv"
    c.write_text(textwrap.dedent("""\
        override_id,target,key,item_key,kind,field,payload,scope_members,scope_basis_digest,tier,author,created_at,revokes
        m1,identity,"[""sa"",""sb""]",,row,,"{""value"":""merge""}",,,human,,2026-10-01T00:00:00Z,
        k1,identity,sc,,row,,"{""value"":""mask""}",,,,,2026-10-01T00:00:00Z,
        f1,fields,sa,source_id,field,title,"{""value"":""T""}",,,human,,2026-10-01T00:00:00Z,
        w1,screen,sa,,field,verdict,"{""value"":""relevant""}","[""sa""]",,ai_check,,2026-10-01T00:00:00Z,
        w2,screen,,,revoke,,,,,ai_check,,2026-10-01T00:00:00Z,w1
        ms1,manual_sources,,,row,,"{""url"":""https://arxiv.org/abs/2409.15747"",""flags"":[""accept""],""note"":""n"",""title"":""T"",""authors"":[""A. Author""],""published_at"":""2024-09-24""}",,,human,tomas,2026-10-01T00:00:00Z,
    """))
    from_yaml = check_add(read_file(y, now=NOW), [])
    from_csv = check_add(read_file(c, now=NOW), [])
    assert from_csv == from_yaml
    assert from_csv[-1].key == "doi=10.48550/arxiv.2409.15747"  # the first typed key
    assert (from_csv[-1].payload["title"], from_csv[-1].payload["published_at"]) == ("T", "2024-09-24")
    bad = tmp_path / "bad.csv"
    bad.write_text("override_id,target,kind,colour\nx,identity,row,red\n")
    with pytest.raises(OverrideError, match="unknown columns"):
        read_file(bad, now=NOW)


# ---------------------------------------------------------------- the effective rule
def test_order_is_tier_then_time_then_id() -> None:
    """Per (target, key, item_key, field): human above ai_check whatever the time; within a tier
    the later created_at; at equal time the larger override_id."""
    assert ids(winners([row("h-old", at=0), row("ai-new", tier="ai_check", at=5)])) == ["h-old"]
    assert ids(winners([row("b", at=1), row("a", at=2)])) == ["a"]
    assert ids(winners([row("a", at=1), row("b", at=1)])) == ["b"]
    # other groups do not compete: another field, another key, a quarantine beside a row
    got = winners([row("t", at=1), row("d", field="doc_type"), row("k", key="sb"),
                   row("r", target="screen", kind="row", field=""), row("q", target="screen", kind="quarantine", field="")])
    assert sorted(ids(got)) == ["d", "k", "q", "r", "t"]


def test_revoking_the_winner_falls_back() -> None:
    rows = [row("a", at=1), row("b", at=2), row("x", kind="revoke", revokes="b", at=3)]
    assert ids(effective(rows).rows) == ["a"]
    assert ids(effective([*rows, row("y", kind="revoke", revokes="a", at=4)]).rows) == []


def test_revoke_rules_in_the_view() -> None:
    """A dangling revoke, a revoke of a revoke and an under-tier revoke drop nothing and warn."""
    rows = [row("h", at=1), row("ai", tier="ai_check", at=2, field="doc_type"),
            row("r1", kind="revoke", revokes="h", tier="ai_check"),       # under tier: ignored
            row("r2", kind="revoke", revokes="ai"),                        # human revokes ai_check: fine
            row("r3", kind="revoke", revokes="r2"),                        # a revoke of a revoke: ignored
            row("r4", kind="revoke", revokes="gone")]                      # dangling
    live, warnings = drop_revoked(rows)
    assert ids(live) == ["h"]
    assert [w.split(":")[0] for w in warnings] == ["r1", "r3", "r4"]
    assert "dangling" in warnings[2]


def test_add_refuses_bad_revokes_and_duplicate_ids() -> None:
    chain = [row("h", at=1), row("r", kind="revoke", revokes="h"), row("i", target="identity", kind="row", field="")]
    for new, needle in [
        ([row("x", kind="revoke", revokes="r")], "revokes the revoke"),
        ([row("x", kind="revoke", revokes="h", tier="ai_check")], "cannot revoke"),
        ([row("x", kind="revoke", revokes="i")], "a fields revoke names i, a identity override"),
        ([row("h")], "already in the overrides chain"),
        ([row("x"), row("x", at=1)], "listed twice"),
        ([row("x", kind="revoke", revokes="nope")], "names no row"),
    ]:
        with pytest.raises(OverrideError, match=needle):
            check_add(new, chain)
    # a revoke may name a row of the same file
    out = check_add([row("n", at=2), row("x", kind="revoke", revokes="n", at=3)], chain)
    assert ids(out) == ["n", "x"]


def test_a_dangling_revoke_cannot_cancel_a_later_row() -> None:
    """F1: a revoke naming no row (a typo for ov-120) is refused at add, so a row added weeks later
    under that id is not silently revoked."""
    chain = [row("ov-120", at=1)]
    with pytest.raises(OverrideError, match="names no row"):
        check_add([row("rv-1", kind="revoke", revokes="ov-12", at=2)], chain)
    chain += check_add([row("ov-12", at=30)], chain)
    assert ids(effective(chain).rows) == ["ov-12"]


# ---------------------------------------------------------------- the stage
def _yaml(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text))
    return path


def test_first_add_starts_the_series_then_adds_extend_its_head_and_ids_stay_unique(tmp_path: Path) -> None:
    """The first add starts ``data/overrides/main/`` (a full without a base); a later add reads the
    directory and writes on its last file (a full, the default kind, with ``base`` = that file)."""
    store = tmp_path / "store"
    store.mkdir()
    a = _yaml(tmp_path / "identity.yaml", "- {override_id: k1, value: mask, key: {source_id: sa}}\n")
    r1 = run_add(store, a, OUT)
    assert r1.status == "ok", r1.error
    h1 = read_header(store / r1.outputs["overrides"]["path"])
    assert (h1["kind"], h1["base"], h1["series"]) == ("full", "", "main")
    b = _yaml(tmp_path / "fields.yaml", "- {override_id: f1, doc_id: sa, field: title, value: T}\n")
    r2 = run_add(store, b, OUT, comment="fix-title")
    assert r2.status == "ok", r2.error
    o2 = r2.outputs["overrides"]
    assert (o2["kind"], o2["base"]) == ("full", Path(r1.outputs["overrides"]["path"]).name)
    assert r2.prior == [r1.outputs["overrides"]["file_id"]]
    assert ids(rows_of(read_view(resolve(store, OUT)))) == ["f1", "k1"]
    # the same id again: refused, nothing written
    r3 = run_add(store, a, OUT)
    assert r3.status == "failed" and "already in the overrides chain" in (r3.error or "")
    assert resolve(store, OUT).head == o2["file_id"]
    # a run that does not read the non-empty directory is refused (no second series start)
    from stack.overrides import add_plan

    c = _yaml(tmp_path / "screen.yaml", "- {override_id: w1, key: {doc_id: sa}, quarantine: true}\n")
    r4 = run_stage(store, add_plan(c), "overrides.add", series="main")
    assert r4.status == "failed" and "is not empty and the run read none of it" in (r4.error or "")
    rec = find_run_record(store, r2.run_id)
    assert rec is not None and rec.plan == "overrides"
    # --comment goes into the record and the job's head, not into the stage options
    assert rec.comment == "fix-title" and r1.comment is None
    assert (store / "runs" / f"{rec.started_at:%Y%m%dT%H%MZ}_{rec.run_id}_fix-title.overrides.add.json.zst").is_file()


def test_cli_overrides_add(tmp_path: Path) -> None:
    store = tmp_path / "store"
    f = _yaml(tmp_path / "identity.yaml", """
        - {override_id: k1, value: mask, key: {source_id: sa}}
        - {override_id: r1, revokes: k1}
    """)
    runner = CliRunner()
    res = runner.invoke(app, ["--store", str(store), "overrides", "add", str(f), "--out", OUT, "--dry-run"])
    assert res.exit_code == 0, res.output
    assert not store.exists()  # a dry run writes nothing
    res = runner.invoke(app, ["--store", str(store), "overrides", "add", str(f), "--out", OUT, "--create"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["status"] == "ok" and out["counts"]["rows"] == 2 and out["counts"]["revokes"] == 1
    dangling = _yaml(tmp_path / "fields.yaml", "- {override_id: r2, revokes: nope}\n")
    res = runner.invoke(app, ["--store", str(store), "overrides", "add", str(dangling), "--out", OUT])
    assert res.exit_code == 1 and "error: r2: revokes 'nope', which names no row" in res.stderr
    res = runner.invoke(app, ["--store", str(store), "overrides", "add", str(f), "--out", OUT])
    assert res.exit_code == 1 and "error: override ids already in the overrides chain" in res.stderr
    res = runner.invoke(app, ["--store", str(store), "overrides", "add", str(f), "--out", OUT, "--comment", "a.b"])
    assert res.exit_code == 1 and "--comment" in res.stderr


def test_cli_add_refuses_ids_the_series_does_not_know(tmp_path: Path) -> None:
    """S25-56 / L3-11: an ``identity`` or ``fields`` row naming a source or document id the series
    does not have is refused by ``stack overrides add`` and by its ``--dry-run`` (gather would refuse
    every later run until a revoke); known source ids, head doc ids and ``former_doc_ids`` pass, and
    a row revoked in the same file names nothing; a doc id must be a head document's."""
    from tests.synth.rows import sid
    from tests.test_gather_identity import Store, src

    st = Store(tmp_path / "store")
    st.root.mkdir()
    st.add("sources.arxiv_sweep", [src(1, [("doi", "d")]), src(2, [("doi", "e")])])
    st.gather()
    st.add("sources.arxiv_sweep", [src(3, [("doi", "f")])])  # not gathered yet: a source, no document
    store = st.root
    runner = CliRunner()

    def add(text: str, *more: str) -> Any:
        f = _yaml(tmp_path / f"in{len(list(tmp_path.glob('in*')))}.yaml", text)
        return runner.invoke(app, ["--store", str(store), "overrides", "add", str(f), "--out", OUT, *more])

    bad = f"""
        identity:
          - {{override_id: m1, value: merge, key: {{source_ids: [{sid(1)}, {sid(99)}]}}}}
          - {{override_id: k1, value: mask, key: {{source_id: {sid(98)}}}}}
        fields:
          - {{override_id: f1, doc_id: {sid(97)}, field: title, value: T}}
    """
    for extra in (("--dry-run",), ()):
        res = add(bad, *extra)
        assert res.exit_code == 1, res.output
        assert (f"ids no source or document of series 'main' has: {sid(97)} (in f1); {sid(98)} (in k1); "
                f"{sid(99)} (in m1)") in res.stderr
    with pytest.raises(EmptyChainError):
        resolve(store, OUT)  # nothing written
    ok = f"""
        identity:
          - {{override_id: m2, value: merge, key: {{source_ids: [{sid(1)}, {sid(2)}]}}}}
          - {{override_id: k2, value: mask, key: {{source_id: {sid(96)}}}}}
          - {{override_id: r2, revokes: k2}}
        fields:
          - {{override_id: f2, doc_id: {sid(1)}, field: title, value: T}}
          - {{override_id: f4, source_id: {sid(3)}, field: title, value: T}}
    """
    res = add(f"fields: [{{override_id: f3, doc_id: {sid(3)}, field: title, value: T}}]\n", "--dry-run")
    assert res.exit_code == 1 and f"{sid(3)} (in f3)" in res.stderr  # a source has no document before a gather
    res = add(ok)
    assert res.exit_code == 0, res.output
    st.gather()  # sid(2) merges into sid(1)'s document: sid(2) is a former doc id from now on
    assert sid(2) in st.docs()[sid(1)]["identity"]["former_doc_ids"]
    res = add(f"fields: [{{override_id: f5, doc_id: {sid(2)}, field: title, value: U}}]\n", "--dry-run")
    assert res.exit_code == 0, res.output


def test_build_pins_the_overrides_head_for_its_readers(tmp_path: Path) -> None:
    """``stack build`` passes ``overrides/<series>/`` to gather, collect.manual and annotate.<wf> once
    the directory has a file, and to no other stage (D338)."""
    from stack.build import stage_inputs
    from stack.plan import Plan, StageLine

    store = tmp_path / "store"
    store.mkdir()
    plan = Plan.model_validate({"plan": "t", "schema_version": 2, "series": "main"})

    def inputs(stage: str) -> list[str]:
        built = {"corpus_input": {"path": "data/corpus/main/20261001T120000Z_rxxxxxxxxxxxx.full.parquet"}}
        calls = stage_inputs(store, plan, StageLine.model_validate({"stage": stage}), built, lambda m: None)
        return [i for c in calls for i in c.ins if i.startswith("data/overrides/")]

    readers = ("gather", "collect.manual", "annotate.screen")
    assert all(inputs(s) == [] for s in (*readers, "collect.arxiv", "acquire", "triage"))
    run_add(store, _yaml(tmp_path / "identity.yaml", "- {override_id: k1, value: mask, key: {source_id: sa}}\n"), OUT)
    assert all(inputs(s) == [OUT] for s in readers)
    assert all(inputs(s) == [] for s in ("collect.arxiv", "acquire", "triage", "fold.corpus"))
