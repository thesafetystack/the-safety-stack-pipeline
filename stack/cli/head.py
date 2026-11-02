"""``stack head <dir>`` (D375; replaces ``stack latest <plan>``): the last published
file of a series directory ``data/<family>/<series>/`` and its version (the newest full file, then
its parts), resolved from the run records (D345) with the walk's checks (a fork or a missed file is
an error, exit 1). Consumers name a series, not a plan; ``--json`` prints ``{dir, head, path,
version: [{name, kind, file_digest}]}``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from stack.cli.common import StoreOpt, echo_json, fail, resolve_globals
from stack.cli.ls import dir_key
from stack.store.chain import ChainError, EmptyChainError, resolve_records


def head(
    ctx: typer.Context,
    directory: Annotated[str, typer.Argument(help="A series directory data/<family>/<series>/.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print as JSON.")] = False,
    store: StoreOpt = None,
) -> None:
    """Print the head file of DIRECTORY and its version (newest full <- parts)."""
    g = resolve_globals(ctx, store, None)
    try:
        key = dir_key(directory)
        ch = resolve_records(g.store, key + "/")
    except EmptyChainError:
        fail(f"{directory}: no published file in {g.store}")
    except (ChainError, ValueError) as exc:
        fail(f"{directory}: {type(exc).__name__}: {exc}")
    version = [{"name": f.name, "kind": f.kind, "file_digest": (f.output or {}).get("file_digest")}
               for f in ch.files]
    if as_json:
        echo_json({"dir": key, "head": ch.head_name, "path": ch.head_key, "version": version})
        return
    typer.echo(f"head\t{ch.head_key}")
    for v in version:
        typer.echo(f"version\t{v['kind']}\t{v['name']}")
