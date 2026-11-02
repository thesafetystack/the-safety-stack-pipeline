"""``stack ls [<dir>]`` (D375): with a series directory ``data/<family>/<series>/``, its chain:
the published files in name order (kind, base, run, digest, rows) and which of them make the head's
version; without one, every series directory of the store with its head and file count. From the run
records alone (D345; no file is read): a file no ``ok`` record lists, or that an ``excludes`` record
takes out, is not listed. A directory whose head does not resolve (a fork, a missed file) is listed
with its error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from stack.cli.common import StoreOpt, echo_json, fail, resolve_globals, shown
from stack.store.chain import ChainError, dir_files, resolve_records
from stack.store.index import store_index
from stack.store.layout import parse_dir_ref, series_dir


def dir_key(ref: str) -> str:
    """``data/<family>/<series>`` of a ``--in``-style series directory reference; ``ValueError``
    for anything else (a file without a series is not a chain)."""
    fam_dir, series = parse_dir_ref(ref)
    if series is None:
        raise ValueError(f"{ref!r} is not a series directory data/<family>/<series>/")
    return series_dir(fam_dir, series)


def listing(store_root: Path, key: str) -> dict[str, Any]:
    """One directory's chain: ``{dir, head, version, error, files: [{name, kind, base, after, run_id,
    file_digest, row_count, in_version}]}``."""
    files = dir_files(store_root, key)
    out: dict[str, Any] = {"dir": key, "head": None, "version": [], "error": None}
    if files:
        try:
            ch = resolve_records(store_root, key + "/")
            out["head"], out["version"] = ch.head_name, [f.name for f in ch.files]
        except (ChainError, ValueError) as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
    out["files"] = [{"name": f.name, "kind": f.kind, "base": f.base, "after": f.after, "run_id": f.run_id,
                     "file_digest": (f.output or {}).get("file_digest"), "row_count": f.row_count,
                     "in_version": f.name in out["version"]} for f in files]
    return out


def ls(
    ctx: typer.Context,
    directory: Annotated[str | None, typer.Argument(
        help="A series directory data/<family>/<series>/; omitted: every series directory.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the listing as JSON.")] = False,
    store: StoreOpt = None,
) -> None:
    """List a series directory's published files in chain order, or every series directory."""
    g = resolve_globals(ctx, store, None)
    if directory is None:
        dirs = [listing(g.store, d) for d in store_index(g.store).series_dirs()]
        if as_json:
            echo_json(dirs)
            return
        if not dirs:
            typer.echo(f"no published series directory in {shown(g.store)}")
        for d in dirs:
            state = f"head {d['head']}" if d["error"] is None else f"ERROR {d['error']}"
            typer.echo(f"{d['dir']}/: {len(d['files'])} file(s), {len(d['version'])} in version; {state}")
        return
    try:
        key = dir_key(directory)
    except ValueError as exc:
        fail(str(exc))
    d = listing(g.store, key)
    if as_json:
        echo_json(d)
        return
    if not d["files"]:
        fail(f"{key}/: no published file in {g.store}")
    typer.echo(f"{key}/: " + (f"head {d['head']}" if d["error"] is None else f"ERROR {d['error']}"))
    for f in d["files"]:
        mark = "*" if f["in_version"] else " "
        typer.echo(f"  {mark} {f['name']}  {f['kind']:4}  base {f['base'] or '-'}  run {f['run_id']}  "
                   f"rows {f['row_count']}  sha256 {str(f['file_digest'])[:12]}")
