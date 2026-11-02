"""Scope of an ``annotate.<wf>`` run (§3.1, §3.3, D72): which ``corpus`` rows get a ``<wf>`` row,
and how many may be asked.

* Input: the build's ``fold.corpus`` file (``--in <this build's corpus file>``; never a union, never
  the ``documents`` head; the runner pins it, X2 checks the lineage), with the run's joined reads
  (D235; registry ``reads`` plus the line's ``reads:``, :mod:`stack.annotate.inputs`) already in
  its rows' ``annotations``, so a filter may read them
  (``ann_verdict(annotations, 'stack_relevance') = 'relevant'``).
* **Filter**: the run's filter (``--filter``, else the stage line's ``filter:``; a named filter
  or a SQL predicate over the row, evaluated by :func:`stack.fold.filter_rows`); none = every
  row. The ``<wf>`` file holds exactly the rows in scope.
* **Guard**: a masked or ``retired`` row never gets a ``<wf>`` row even when the filter lets it
  through (the default ``fold.corpus`` filter ``has_text`` already drops them; a diagnostic
  ``everything`` corpus does not); counted ``guard.masked`` / ``guard.retired``.
* **Limit**: ``options.max_rows`` of the stage line (D72) caps the rows that are asked in one
  run (cache hits included), taken in ``doc_id`` order (random ids, so a uniform sample of the rows
  not carried); rows past it are ``not_attempted[limit]``, counted ``not_attempted.limit``, and the
  run ends ``ok`` with ``scope_complete = false`` (build reason ``limit``, D709). ``max_rows`` is in
  no config digest or work key, so a later run without it carries the answered rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from stack.fold import filter_rows

Row = dict[str, Any]


def in_scope(corpus: pa.Table, filter: str | None, counts: dict[str, int] | None = None) -> list[Row]:
    """The ``corpus`` rows a run annotates, in ``doc_id`` order (module docstring)."""
    tbl = filter_rows(corpus, filter) if filter else corpus
    if counts is not None:
        counts["scope.filtered_out"] = corpus.num_rows - tbl.num_rows
    out: list[Row] = []
    for r in tbl.to_pylist():
        why = ("masked" if r["identity"]["is_masked"] else
               "retired" if r["state"] == "retired" else None)
        if why is not None:
            if counts is not None:
                counts[f"guard.{why}"] = counts.get(f"guard.{why}", 0) + 1
            continue
        out.append(r)
    return sorted(out, key=lambda r: r["doc_id"])


@dataclass
class Limit:
    """``options.max_rows`` as a counter: :meth:`take` admits one asked row or refuses."""

    max_rows: int | None
    taken: int = 0
    refused: list[str] = field(default_factory=list)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Limit:
        raw = options.get("max_rows")
        if raw is None:
            return cls(None)
        n = int(raw)
        if n < 0:
            raise ValueError(f"options.max_rows must be >= 0, got {raw!r}")
        return cls(n)

    def take(self, doc_id: str) -> bool:
        if self.max_rows is not None and self.taken >= self.max_rows:
            self.refused.append(doc_id)
            return False
        self.taken += 1
        return True


__all__ = ["Limit", "in_scope"]
