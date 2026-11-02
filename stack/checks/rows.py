"""Rows of one table as Python dicts, :data:`BATCH_ROWS` at a time (round 5 S3b).

The file-scope checks (S, V1-V6, V9) and V10 walk a whole ``data/`` file or release once, row by
row; converting the selected columns of the whole table to Python at once costs ~2-3x the Arrow
size of those columns per check (D280). :func:`iter_rows` converts one slice at a
time, in table order, so a check keeps only its own small state (counters, keys, findings).
:func:`row_slices` is the same walk for checks that work on Arrow arrays of a row range.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pyarrow as pa

#: Rows converted to Python at a time; the value of :data:`stack.checks.chain.BATCH_ROWS`, defined
#: here because :mod:`stack.checks.schema` imports this module and ``stack.store.chain`` imports
#: the schema checks (importing ``stack.checks.chain`` from here would be a cycle).
BATCH_ROWS = 4096

__all__ = ["BATCH_ROWS", "iter_rows", "iter_values", "row_slices"]


def row_slices(table: pa.Table, batch_rows: int | None = None) -> Iterator[tuple[int, pa.Table]]:
    """``(start, table.slice(start, n))`` over the table in order, ``n`` = ``batch_rows`` or
    :data:`BATCH_ROWS` (read at call time, so a test can lower it); nothing for an empty table."""
    n = batch_rows or BATCH_ROWS
    for start in range(0, table.num_rows, n):
        yield start, table.slice(start, n)


def iter_rows(table: pa.Table, columns: Sequence[str], batch_rows: int | None = None) -> Iterator[dict[str, Any]]:
    """The rows of ``table.select(columns)`` as dicts, in table order, converted a slice of
    ``batch_rows`` (default :data:`BATCH_ROWS`) at a time."""
    sel = table.select(list(columns))
    for _, part in row_slices(sel, batch_rows):
        yield from part.to_pylist()


def iter_values(arr: pa.Array | pa.ChunkedArray, batch_rows: int | None = None) -> Iterator[Any]:
    """The Python values of ``arr`` in order, converted a slice of ``batch_rows`` (default
    :data:`BATCH_ROWS`) at a time."""
    n = batch_rows or BATCH_ROWS
    for start in range(0, len(arr), n):
        yield from arr.slice(start, n).to_pylist()
