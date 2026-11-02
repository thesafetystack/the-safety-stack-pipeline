"""File-scope checks in record batches (round 5, T-SCALE S3b): :mod:`stack.checks.rows` and the
S/V checks that walk a file through it give the same findings, in the same order, whatever the
batch size.

Synthetic only: the per-check fixture stores of ``tests.fixtures.checks`` (each plants one fault),
their files run at batch sizes 1, 3 and one batch; the same files repeated five times (so findings
fall in many batches); V3's one cross-row input (a split's ``former_doc_ids``) across batches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from stack.checks import REGISTRY
from stack.checks import rows as checks_rows
from stack.checks import verify as V
from stack.checks.chain import StoreView
from stack.checks.rows import iter_rows, iter_values, row_slices
from tests.fixtures.checks import build_store, shared_stores, sid

pytestmark = pytest.mark.checks  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

#: The checks that walk rows through :mod:`stack.checks.rows`, each with its fixture.
BATCHED = ["status_pairing", "nested_unique", "id_formats", "state_blocks", "digest_agreements",
           "payload_typed", "V-sources", "V-coverage", "V-state", "V-digests", "V-annotations",
           "V-content", "V-fold"]
#: The batch-size cases kept in the fast tier: one file-scope S check, one V check (each case runs every file
#: check of a fixture store at three batch sizes, ~3-4 s at -n 2); the rest are ``slow`` (the test-tier work).
FAST_BATCHED = ("id_formats", "V-content")
BATCHED_CASES = [c if c in FAST_BATCHED else pytest.param(c, marks=pytest.mark.slow) for c in BATCHED]
FILE_CHECKS = sorted(n for n, cs in REGISTRY.items() if cs.fn is not None and cs.scope == "file")
SIZES = (1, 3, 10**9)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """``fixture -> root``: each fixture store written once per module, read-only (the tests here only read it)."""
    return shared_stores(tmp_path_factory)


def _as_data(findings: Any) -> Any:
    return [(f.check, f.level, f.message, f.n, f.items) for f in findings]


def _file_results(root: Path, monkeypatch: pytest.MonkeyPatch, batch: int) -> dict[str, Any]:
    monkeypatch.setattr(checks_rows, "BATCH_ROWS", batch)
    view = StoreView(root)
    return {df.path.relative_to(root).as_posix(): {k: _as_data(v) for k, v in V._file_findings(view, df, set(FILE_CHECKS)).items()}
            for df in view.files}


@pytest.mark.parametrize("batch", [1, 2, 3, 5, 6, 7, None])
def test_helpers_keep_table_order(batch: int | None) -> None:
    tbl = pa.table({"a": list(range(6)), "b": [str(i) for i in range(6)], "c": [None] * 6})
    assert list(iter_rows(tbl, ["b", "a"], batch)) == tbl.select(["b", "a"]).to_pylist()
    assert list(iter_values(tbl.column("b"), batch)) == tbl.column("b").to_pylist()
    parts = list(row_slices(tbl, batch))
    assert [s for s, _ in parts] == list(range(0, 6, batch or checks_rows.BATCH_ROWS))
    assert pa.concat_tables([p for _, p in parts]).equals(tbl)
    assert list(iter_rows(tbl.slice(0, 0), ["a"], batch)) == []


@pytest.mark.parametrize("check", BATCHED_CASES)
def test_fixture_findings_do_not_depend_on_batch_size(store: Any, monkeypatch: pytest.MonkeyPatch,
                                                      check: str) -> None:
    results = [_file_results(store(check), monkeypatch, b) for b in SIZES]
    hits = [f for per in results[-1].values() for f in per.get(check, [])]
    assert hits, f"the {check} fixture plants no finding of its check"
    assert results[0] == results[-1] and results[1] == results[-1]


@pytest.mark.parametrize("check", BATCHED_CASES)
def test_repeated_rows_findings_do_not_depend_on_batch_size(store: Any, monkeypatch: pytest.MonkeyPatch,
                                                            check: str) -> None:
    """Every file of the fixture store five times over: findings in many batches, order kept."""
    view = StoreView(store(check))
    seen = 0
    for df in view.files:
        spec = df.spec
        tbl = pq.read_table(df.path).replace_schema_metadata(None)
        if spec is None or not tbl.schema.equals(spec.schema, check_metadata=False):
            continue
        tbl = pa.concat_tables([tbl] * 5)
        got = []
        for b in SIZES:
            monkeypatch.setattr(checks_rows, "BATCH_ROWS", b)
            try:
                got.append(_as_data(REGISTRY[check].fn(tbl, df.header, spec)))
            except Exception as exc:  # noqa: BLE001 - compared, like run_checks reports it
                got.append(f"{type(exc).__name__}: {exc}")
        assert got[0] == got[2] and got[1] == got[2], df.path.name
        seen += bool(got[2])
    assert seen, f"no file of the {check} fixture has a finding of its check"


@pytest.mark.slow
def test_release_findings_do_not_depend_on_batch_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_store(tmp_path, "V-release")
    got = []
    for b in SIZES:
        monkeypatch.setattr(checks_rows, "BATCH_ROWS", b)
        got.append(_as_data(REGISTRY["V-release"].fn(StoreView(tmp_path))))
    assert got[2] and got[0] == got[2] and got[1] == got[2]


@pytest.mark.parametrize("batch", [1, 3, 10**9])
@pytest.mark.parametrize("retired_first", [False, True])
def test_v3_split_seen_across_batches(store: Any, monkeypatch: pytest.MonkeyPatch, batch: int,
                                      retired_first: bool) -> None:
    """A retired row without ``merged_into`` is a split when another row of the file lists it in
    ``former_doc_ids``, whichever batch either row falls in."""
    monkeypatch.setattr(checks_rows, "BATCH_ROWS", batch)
    df = next(d for d in StoreView(store()).files if d.family == "documents"
              and d.header.get("stage") == "gather" and d.header.get("kind") == "full")
    spec = df.spec
    rows = pq.read_table(df.path).replace_schema_metadata(None).to_pylist()
    retired = next(r for r in rows if r["doc_id"] == sid(5))
    lister = next(r for r in rows if r["doc_id"] == sid(4))
    assert retired["state"] == "retired" and lister["identity"]["former_doc_ids"] == [sid(5)]
    retired["identity"]["merged_into"] = None
    rows = [retired] + [r for r in rows if r is not retired] if retired_first else rows

    def v3(rs: list[dict[str, Any]]) -> list[Any]:
        return _as_data(REGISTRY["V-state"].fn(pa.Table.from_pylist(rs, schema=spec.schema), df.header, spec))

    assert v3(rows) == []
    lister["identity"]["former_doc_ids"] = []
    found = v3(rows)
    assert len(found) == 1 and found[0][3] == 1 and "not a split" in found[0][4][0]


@pytest.mark.parametrize("batch", [1, 3])
def test_s10_keeps_workflow_major_order(store: Any, monkeypatch: pytest.MonkeyPatch, batch: int) -> None:
    """S10 lists its findings workflow by workflow (then row order), not row by row."""
    df = next(d for d in StoreView(store("payload_typed")).files if d.family == "corpus")
    spec = df.spec
    rows = pq.read_table(df.path).replace_schema_metadata(None).to_pylist()
    assert rows[1]["annotations"]["triage"] is not None
    rows[1]["annotations"]["other"] = [{**rows[0]["annotations"]["other"][0], "workflow": "triage"}]
    tbl = pa.Table.from_pylist(rows * 3, schema=spec.schema)
    got = []
    for b in (batch, 10**9):
        monkeypatch.setattr(checks_rows, "BATCH_ROWS", b)
        got.append(_as_data(REGISTRY["payload_typed"].fn(tbl, df.header, spec)))
    (finding,) = got[1]
    assert finding[3] == 6 and {i.split(": ")[1].split()[0] for i in finding[4]} == {"screen", "triage"}
    assert got[0] == got[1]
