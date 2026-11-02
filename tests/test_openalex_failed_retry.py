"""Failed-only selection never silently expands into a full sweep.

v3 read the ``runs`` and ``coverage`` tables; the selection now reads the own ``coverage.openalex``
view (rows built with ``tests/synth/rows.py``, D46). The end-to-end retry is in
``tests/test_collect_openalex.py`` (F)."""
from typing import Any

import pytest
from test_collect_openalex import openalex

from stack.core.hashes import jcs
from tests.synth.rows import coverage_row, rid


def cov(label: str, status: str, run: int = 1, i: int = 0) -> dict[str, Any]:
    """A coverage row this collector writes: ``query`` = JCS of ``{query, filter}``."""
    error_class = {"ok": None, "partial": None, "failed_transient": "http_5xx",
                   "failed_permanent": "query_invalid"}[status]
    return coverage_row(i, run, channel="openalex", status=status, error_class=error_class,
                        is_exhausted=status == "ok",
                        query=jcs({"query": label, "filter": f"f:{label}"}))


def test_selects_only_failures_in_config_order() -> None:
    rows = [cov("b", "failed_transient", i=0), cov("a", "failed_permanent", i=1),
            cov("complete", "ok", i=2), cov("truncated", "partial", i=3),
            cov("c", "failed_transient", run=2, i=4)]  # another run's failure: not selected
    plan = [{"query": q} for q in ["a", "complete", "b", "truncated", "c"]]
    assert openalex.failed_only_plan(rows, plan, rid(1)) == [plan[0], plan[2]]


@pytest.mark.parametrize("rows", [[], [cov("a", "failed_transient", run=2)]])
def test_rejects_a_run_without_current_rows(rows: list[dict[str, Any]]) -> None:
    """An unknown or unfinished run (rows are emitted at a run's end) fails closed."""
    with pytest.raises(ValueError, match="finished collect.openalex run"):
        openalex.failed_only_plan(rows, [{"query": "a"}], rid(1))


def test_missing_query_fails_closed() -> None:
    with pytest.raises(ValueError, match="no longer configured"):
        openalex.failed_only_plan([cov("removed", "failed_transient")], [], rid(1))


def test_no_failures_means_no_work() -> None:
    assert openalex.failed_only_plan([cov("a", "ok")], [{"query": "a"}], rid(1)) == []


def test_query_label_reads_the_coverage_query() -> None:
    assert openalex.query_label(jcs({"query": "group:rlhf", "filter": "x"})) == "group:rlhf"
    assert openalex.query_label("group:rlhf") == "group:rlhf"  # a bare label passes
