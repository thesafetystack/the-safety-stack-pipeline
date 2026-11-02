"""Stage ``overrides.add`` (D336-D338): add the rows of one human-edited file to the
``overrides`` chain (:mod:`stack.overrides`). Standalone (``stack overrides add <file>``, or
``stack run overrides.add`` with a stage line naming ``options.file``); no build needed.

Stage-line options: ``file`` (required; a ``.yaml``/``.yml`` file in today's entry format or a
``.csv`` with the family's columns, :func:`stack.overrides.read_file`). The file's path and
sha256 are the run's ``config_digest`` together with the stage line's.

Reads the series directory ``data/overrides/<series>/`` it extends (``stack overrides add --out``
passes it as both ``--in`` and ``--out``, D377; an empty directory is the empty
prior of the series' first add; a run that did not read a non-empty directory is refused by the
runner's restart guard, so no add can silently start the series over). Refuses (the run ends
``failed``, nothing written) a file that does not validate, an ``override_id`` already in the file
or the directory's version, a dangling revoke (one naming no row of the version or the file), a
revoke of a revoke, an under-tier revoke and a revoke naming another target's row
(:func:`stack.overrides.check_add`). The stage emits its new rows as a part on the version it
read; without ``--part`` (the default kind, and what ``stack overrides add`` runs) the runner
writes the full file that part makes, with the directory's previous file as its base; the first
add into an empty directory writes a full without a base. An empty file writes nothing.
Counts: ``rows``, ``revokes``, per target ``rows.<target>``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from stack.checks import digests
from stack.overrides import CHAIN_ID, FAMILY, OverrideError, check_add, read_file, rows_of, to_table
from stack.stage_runner import StageContext, StageError, StageResult

STAGE = "overrides.add"
OPTIONS: frozenset[str] = frozenset({"file"})  # D434


def run(ctx: StageContext) -> StageResult:
    path = ctx.plan_stage.options.get("file")
    if not path:
        raise StageError(f"{ctx.stage}: the stage line names no options.file (stack overrides add <file>)")
    p = Path(str(path))
    pinned = [ch for ch in (*ctx.inputs.values(), *ctx.prior.values()) if ch.family == FAMILY]
    chain = rows_of(ctx.view(FAMILY)) if pinned else []
    try:
        new = read_file(p, now=dt.datetime.now(dt.UTC).replace(microsecond=0))
        rows = check_add(new, chain)
    except OverrideError as exc:
        raise StageError(f"{ctx.stage}: {exc}") from None
    ctx.config_digest = digests.H({"stage": ctx.config_digest, "file": str(p),
                                   "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    ctx.counts["rows"] = len(rows)
    ctx.counts["revokes"] = sum(o.kind == "revoke" for o in rows)
    for o in rows:
        ctx.counts[f"rows.{o.target}"] = ctx.counts.get(f"rows.{o.target}", 0) + 1
    if rows:
        if pinned:
            ctx.emit_part(FAMILY, to_table(rows), "*")
        else:
            ctx.emit_full(FAMILY, to_table(rows), "*", chain_id=CHAIN_ID)
    return StageResult(scope_complete=True, rows_touched=len(rows))
