"""T5.4: ``collect.v3_import`` over a synthetic v3 dump (D34, D139), in CSV and in parquet.

The dump: 10 ``metadata`` rows over 10 v3 sources (arXiv and LessWrong channels), 3
``snapshots``, 2 ``annotations``, written by the test with v3's DDL column names (CSV as
Postgres prints it: JSON text, ``+00`` timestamps). Run 1 yields 10 ``sources`` rows
(a ``v3_meta_id`` key = ``meta_id``, D524; ``origin = 'import'``, v3 ids kept); the file passes the writer's
S-checks and V1; run 2 over the same dump writes no ``sources`` row (idempotent); a grown dump
(two newer metadata rows of one source, one new source) is imported over runs 3 and 4, one
generation per run, extending the chain without minting an id. A dump breaking v3's snapshot
foreign key fails the run.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from stack.build import incomplete_reasons
from stack.checks.verify import run_checks
from stack.plan import Plan
from stack.stage_runner import WORKFLOWS_ROOT_ENV
from tests.synth.records import run_line, sdir
from stack.store.chain import read_view, resolve
from stack.store.parquet import check_family, read_family, read_header
from tests.synth.rows import mint

STAGE = "collect.v3_import"
T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)


def at(h: int) -> dt.datetime:
    return T0 + dt.timedelta(hours=h)


def v3_source(i: int) -> dict[str, Any]:
    arxiv = i % 2 == 0
    return {"source_id": mint("s", 100 + i), "channel": "arxiv" if arxiv else "lesswrong",
            "external_id": f"2401.{i:05d}" if arxiv else f"post{i}", "canonical_url": None if arxiv
            else f"https://www.lesswrong.com/posts/post{i}", "title_hint": None, "doc_type": "paper" if arxiv else None,
            "self_published": None, "license_class": "open", "sensitivity": None, "discovered_at": at(i),
            "discovery": {"via": "test"}}


def v3_meta(n: int, i: int, h: int) -> dict[str, Any]:
    return {"meta_id": mint("m", n), "source_id": mint("s", 100 + i), "workflow": "fetch.metadata",
            "workflow_version": 2, "payload": {"title": f"Paper {i} v{n}", "pub_date": "2024-05-0" + str(1 + i % 9),
                                               "authors": ["A. Author", "B. Author"]},
            "origin": "auto", "overrides": None, "run_id": mint("r", 900 + n), "created_at": at(h)}


def dump_tables(extra: bool = False) -> dict[str, list[dict[str, Any]]]:
    sources = [v3_source(i) for i in range(10)]
    meta = [v3_meta(i, i, 20 + i) for i in range(10)]
    if extra:  # source 0 gets two newer metadata rows; a new source 10 one
        sources.append(v3_source(10))
        meta += [v3_meta(11, 0, 41), v3_meta(12, 0, 40), v3_meta(13, 10, 42)]
    snaps = [{"snapshot_id": mint("n", j), "source_id": mint("s", 100 + j), "seq": 1, "run_id": mint("r", 950),
              "fetched_at": at(30 + j), "observed_at": at(30 + j), "fetch_meta": {"via": "live"},
              "raw_digest": None, "media_type": "application/pdf", "text_digest": None} for j in range(3)]
    anns = [{"ann_id": mint("a", j), "snapshot_id": mint("n", j), "source_id": mint("s", 100 + j),
             "workflow": "screen", "workflow_version": 3, "ann_type": "verdict", "item_key": "",
             "payload": {"verdict": "relevant"}, "origin": "ai", "stamps": {}, "run_id": mint("r", 960),
             "created_at": at(35 + j)} for j in range(2)]
    return {"metadata": meta, "sources": sources, "snapshots": snaps, "annotations": anns}


def write_dump(d: Path, fmt: str, tables: dict[str, list[dict[str, Any]]]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        for old in d.glob(f"{name}.*"):
            old.unlink()
        if fmt == "csv":
            with (d / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                for r in rows:  # as `\copy … CSV HEADER` prints it
                    w.writerow({k: "" if v is None else json.dumps(v) if isinstance(v, dict | list)
                                else v.strftime("%Y-%m-%d %H:%M:%S+00") if isinstance(v, dt.datetime) else v
                                for k, v in r.items()})
        else:  # JSON columns as text, timestamps native
            cols = {k: [json.dumps(r[k]) if isinstance(r[k], dict | list) else r[k] for r in rows] for k in rows[0]}
            pq.write_table(pa.table(cols), d / f"{name}.parquet")
    return d


def plan(dump: Path) -> Plan:
    return Plan.model_validate({"plan": "test", "schema_version": 2, "stages": [
        {"stage": STAGE, "prior": ["sources", "coverage"], "options": {"dump_dir": str(dump)}}]})


def meta_ids(row: dict[str, Any]) -> list[str]:
    """The row's ``v3_meta_id`` keys (D524: where ``metadata.record_ref`` held the ``meta_id``)."""
    return [k["value"] for k in row["identity"]["keys"] if k["kind"] == "v3_meta_id"]


def history(store: Path) -> list[dict[str, Any]]:
    """Every row of every file of the own sources chain, in chain order."""
    return [r for f in resolve(store, sdir("sources.v3_import")).files for r in read_family(f.path)[0].to_pylist()]


@pytest.mark.slow  # four stage runs per format, ~8-11 s
@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_v3_dump_yields_import_sources_rows(fmt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKFLOWS_ROOT_ENV, raising=False)
    store, tables = tmp_path / "store", dump_tables()
    dump = write_dump(tmp_path / "dump", fmt, tables)

    rec = run_line(store, plan(dump), STAGE)
    assert rec.status == "ok", rec.error
    assert rec.scope_complete is True
    assert (rec.counts["metadata_rows"], rec.counts["sources_rows"], rec.counts["snapshots_rows"],
            rec.counts["annotations_rows"]) == (10, 10, 3, 2)
    assert (rec.counts["metadata_imported"], rec.counts["metadata_pending"], rec.counts["minted_ids"]) == (10, 0, 0)
    view = {r["source_id"]: r for r in read_view(resolve(store, sdir("sources.v3_import"))).to_pylist()}
    meta = {m["source_id"]: m for m in tables["metadata"]}
    src = {s["source_id"]: s for s in tables["sources"]}
    assert set(view) == set(meta) and len(view) == 10  # the v3 ids, kept
    for sid, r in view.items():
        m, s = meta[sid], src[sid]
        assert (meta_ids(r), r["metadata"]["origin"]) == ([m["meta_id"]], "import")
        assert (r["state"], r["record_format"], r["status"], r["run_id"]) == ("observed", "record", "ok", rec.run_id)
        assert r["observed_at"] == m["created_at"]
        assert (r["identity"]["channel"], r["identity"]["external_id"], r["identity"]["canonical_url"]) == (
            s["channel"], s["external_id"], s["canonical_url"])
        assert r["identity"]["keys"]  # typed from the channel group (arxiv / fm_post)
        assert r["metadata"]["title"] == m["payload"]["title"] and r["metadata"]["provider_version"] == 2
    arx = view[mint("s", 100)]["identity"]["keys"]
    assert {"kind": "arxiv", "value": "2401.00000"} in arx
    # the file passes the writer's S-checks, V1 holds, nothing else fails
    out = rec.outputs["sources"]
    tbl, hdr = read_family(store / out["path"])
    assert hdr["chain_id"] == "sources.v3_import" and check_family(store / out["path"], tbl, hdr) == []
    results = {r.check: r for r in run_checks(store)}
    assert results["V-sources"].status == "ok"
    assert {n: r.items for n, r in results.items() if r.status in ("failed", "warn")} == {}
    # coverage: one row per table read
    cov = {json.loads(r["query"])["table"]: r for r in read_view(resolve(store, sdir("coverage.v3_import"))).to_pylist()}
    assert set(cov) == {"metadata", "sources", "snapshots", "annotations"}
    assert (cov["metadata"]["seen"], cov["metadata"]["kept"], cov["metadata"]["status"]) == (10, 10, "ok")
    assert (cov["snapshots"]["seen"], cov["snapshots"]["kept"]) == (3, 0)
    assert (cov["metadata"]["window_start_at"], cov["metadata"]["window_end_at"]) == (at(20), at(29))

    # run 2, same dump: nothing new to import; the coverage chain is re-stamped
    files = len(resolve(store, sdir("sources.v3_import")).files)
    rec2 = run_line(store, plan(dump), STAGE)
    assert rec2.status == "ok" and "sources" not in rec2.outputs
    assert (rec2.counts["metadata_imported"], rec2.counts["metadata_already_imported"]) == (0, 10)
    assert len(resolve(store, sdir("sources.v3_import")).files) == files
    cov2 = read_view(resolve(store, sdir("coverage.v3_import"))).to_pylist()
    assert {r["attempts"] for r in cov2} == {2}

    # a grown dump: one generation per source per run, the chain extends, nothing is minted
    write_dump(dump, fmt, dump_tables(extra=True))
    rec3 = run_line(store, plan(dump), STAGE, kind="part")
    assert rec3.status == "ok" and rec3.scope_complete is False
    assert incomplete_reasons(rec3.counts) == ["pending"]  # D492: the next slice, not a cap
    assert (rec3.counts["metadata_imported"], rec3.counts["metadata_pending"], rec3.counts["minted_ids"]) == (2, 1, 0)
    assert read_header(store / rec3.outputs["sources"]["path"])["kind"] == "part"
    rec4 = run_line(store, plan(dump), STAGE, kind="part")
    assert rec4.status == "ok" and rec4.scope_complete is True
    assert (rec4.counts["metadata_imported"], rec4.counts["metadata_pending"]) == (1, 0)
    hist = history(store)
    assert sorted(i for r in hist for i in meta_ids(r)) == sorted(mint("m", n) for n in range(14) if n != 10)
    s0 = [(*meta_ids(r), r["run_id"]) for r in hist if r["source_id"] == mint("s", 100)]
    # oldest first: m12 (created hour 40) before m11 (hour 41)
    assert s0 == [(mint("m", 0), rec.run_id), (mint("m", 12), rec3.run_id), (mint("m", 11), rec4.run_id)]
    assert {r["source_id"] for r in hist} == {mint("s", 100 + i) for i in range(11)}
    assert not {n: r.items for n, r in ((r.check, r) for r in run_checks(store)) if r.status in ("failed", "warn")}


def test_v3_dump_breaking_a_foreign_key_fails_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKFLOWS_ROOT_ENV, raising=False)
    tables = dump_tables()
    tables["annotations"][1]["source_id"] = mint("s", 105)  # its snapshot is source 101's
    dump = write_dump(tmp_path / "dump", "csv", tables)
    rec = run_line(tmp_path / "store", plan(dump), STAGE)
    assert rec.status == "failed" and not rec.outputs
    assert "annotations" in (rec.error or "") and mint("a", 1) in (rec.error or "")
