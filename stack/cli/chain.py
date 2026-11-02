"""``stack chain orphan`` (§7.6, D61, D375, D379): recover a series directory (``stack ls <dir>``
lists one; ``chain show`` is gone with chain ids).

* ``orphan <path> [--force] [--comment C] [--note "why"]``: take the published file ``data/<family>/<series>/<name>``
  out of its directory by a standalone
  ``chain.orphan`` run record with ``excludes: [<file_id>]`` (:func:`stack.store.chain.orphan`;
  fork and missed-file recovery; D345: nothing is moved). A file that is not its chain's head is
  refused without ``--force`` unless the chain does not resolve (R2-9).
  The record is a standalone job, uploaded on an S3 store (D352). ``unlock`` is gone with the
  chain locks (D356).

Refusals print ``error: …`` and exit 1.
"""

from __future__ import annotations

from typing import Annotated

import typer

from stack.cli.common import (
    CommentOpt,
    StoreOpt,
    fail,
    resolve_globals,
)
from stack.store import chain as chain_mod

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Orphan a published file (stack ls <dir> shows a chain).")


@app.command("orphan")
def orphan(
    ctx: typer.Context,
    path: Annotated[str, typer.Argument(help="Store path of a published file, data/<family>/<series>/<name>.parquet.")],
    force: Annotated[bool, typer.Option(
        "--force", help="Also take a file that is not its directory's last file.")] = False,
    comment: CommentOpt = None,
    note: Annotated[str | None, typer.Option("--note", help="Free text (why), recorded in the record.")] = None,
    store: StoreOpt = None,
) -> None:
    """Take PATH out of its series directory: a chain.orphan run record excluding it (D61, D345)."""
    g = resolve_globals(ctx, store, None)
    try:
        rec = chain_mod.orphan(g.store, path, force=force, comment=comment, note=note)
    except chain_mod.ChainError as exc:
        fail(str(exc))
    typer.echo(f"orphaned {path}: excluded by {rec.relative_to(g.store).as_posix()}")
