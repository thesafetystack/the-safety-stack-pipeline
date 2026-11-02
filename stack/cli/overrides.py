"""``stack overrides add <file> [--comment C] [--dry-run]`` (D336-D338): add the rows of
one human-edited overrides file (``.yaml``/``.yml`` in today's entry format, or ``.csv`` with the
family's columns; ``docs/howto/overrides.md``) to the store's ``overrides`` chain as one standalone
``overrides.add`` run (:func:`stack.overrides.run_add`; no plan file, no build).

The file is validated against the chain first (:func:`stack.overrides.precheck`): a refusal (an
invalid entry, an ``override_id`` already in the file or the chain, a dangling revoke, a revoke of
a revoke, an under-tier revoke, an ``identity`` or ``fields`` entry naming an id the
series does not know: :func:`stack.overrides.check_known`) prints ``error: …`` and exits 1
before any run exists.
``--dry-run`` stops there and prints the rows it would add. Otherwise the run's final record is
printed as one JSON line; exit 0 when it is ``ok``. ``--comment`` (``[A-Za-z0-9_-]+``) names the
job: the record's ``comment`` and the end of its head (``runs/<utc>_<run_id>_<comment>.overrides.add.json.zst``,
D322).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated

import typer

from stack.cli.common import CreateOpt, StoreOpt, fail, require_store, resolve_globals
from stack.overrides import OverrideError, precheck, run_add
from stack.stage_runner import StageError

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Overrides: add human corrections to the store's overrides chain.")
_COMMENT_RE = re.compile(r"[a-z0-9-]+")


@app.command("add")
def add_cmd(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="A .yaml/.yml (today's entry format) or .csv overrides file.")],
    out: Annotated[str, typer.Option("--out", help="The overrides series directory to extend, "
                                     "data/overrides/<series>/ (D377).")],
    comment: Annotated[str | None, typer.Option("--comment", help="A short label ([a-z0-9-]+).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate and print the rows; write nothing.")] = False,
    create: CreateOpt = False,
    store: StoreOpt = None,
) -> None:
    """Add the rows of FILE to the overrides series directory --out (one recorded overrides.add run)."""
    g = resolve_globals(ctx, store, None)
    if comment is not None and not _COMMENT_RE.fullmatch(comment):
        fail(f"--comment {comment!r} must match {_COMMENT_RE.pattern}")
    if not file.is_file():
        fail(f"{file}: no such file")
    if not dry_run:
        require_store(g.store, create)
    try:
        rows = precheck(g.store, file, out)
    except OverrideError as exc:
        fail(str(exc))
    if dry_run:
        for o in rows:
            row = o.to_row()
            typer.echo(json.dumps({**row, "created_at": o.created_at.isoformat()}, ensure_ascii=False))
        return
    try:
        rec = run_add(g.store, file, out, comment=comment, warn=lambda m: typer.echo(f"warning: {m}", err=True))
    except StageError as exc:
        fail(str(exc))
    typer.echo(json.dumps({"run_id": rec.run_id, "status": rec.status, "error_class": rec.error_class,
                           "counts": rec.counts, "outputs": {k: o["path"] for k, o in rec.outputs.items()}}))
    if rec.status != "ok":
        if rec.error:
            typer.echo(rec.error, err=True)
        raise typer.Exit(1)
