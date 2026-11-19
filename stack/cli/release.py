"""``stack release --in data/corpus.build/<series>/[<file>] [--config <name|path>] [--tag <tag>]``
(D18; D379): the alias of ``stack fold release --input corpus=<file> --release <config>``.

``--in`` is a series directory of fold.build corpora (its last published file) or an exact file of
one; nothing is selected by plan or build (D375, D379; ``stack latest`` is gone). The release's
``build_id`` is the build record naming that corpus file (one stderr line prints it and its status).
``--config`` defaults to the plan's
``fold.release.config``. Same run, refusals, output and exit status as ``stack fold release``
(:func:`stack.cli.fold.release_run`). Before the run, one line on stderr names the file released and
whether it is its directory's last file; an older file is released only when named exactly.

**No default plan** (R6-5): ``--plan`` or ``STACK_PLAN`` is required. **Which corpus** (D494,
:func:`stack.fold.release.release_gate`): one an ``ok`` build names; one only an ``incomplete`` build names
(verified, but a stage stopped short; ``stack.build``) only with ``--allow-incomplete``; one no build names
(a standalone ``fold.build`` run) only with ``--allow-unbuilt``; one only ``failed``, ``cancelled`` or
``running`` builds name is refused whatever the flags. A store root that does not exist is refused unless
``--create``.

``stack release`` writes only the store's ``releases/<tag>/`` (D695); ``--out`` is refused before anything
runs, naming ``stack export``.

``stack export --in … --out DIR [--config <name|path>]`` (D647) is the one local export: the release goes to
the local directory ``DIR`` (absent or empty, outside the store root; the tag is its name) instead of
``releases/<tag>/``: no run record, nothing pushed, ``release.json`` is the record. ``--config`` defaults to
``internal-v1``; a config with ``audience: internal`` that ships bytes is exported this way only. It needs no
plan. A dataset config (``format: dataset``: ``dataset-public``, ``dataset-internal``; :mod:`stack.fold.dataset`)
writes the dataset release bundle instead, from the corpus of an ``ok`` build (``--allow-incomplete`` admits an
``incomplete`` one); ``--release-id`` names the release (default the export's UTC date) and ``--blobs DIR`` reads
the contentful variant's inline texts from a local directory of ``<sha256>`` files instead of the store's blobs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from stack.cli.common import (
    CreateOpt,
    PlanOpt,
    StoreOpt,
    fail,
    load_plan_or_fail,
    require_store,
    resolve_globals,
)
from stack.cli.fold import release_run
from stack.fold import FoldError, data_file
from stack.store.chain import last_file
from stack.store.layout import parse_data_key, parse_dir_ref, series_dir
from stack.store.parquet import read_header


def _resolve_in(store: Path, in_: str) -> str:
    """``--in``: a series directory of fold.build corpora (its last published file) or an exact file;
    the data key of the file. One stderr line names it and whether it is its directory's last file."""
    key = in_.strip()
    try:
        if key.endswith(".parquet"):
            d = parse_data_key(key).dir
        else:
            d = series_dir(*parse_dir_ref(key))
            cf = last_file(store, d)
            if cf is None:
                fail(f"--in {in_}: {d}/ has no published file")
            key = f"{d}/{cf.name}"
        read_header(data_file(store, key, "corpus"))
    except (ValueError, FoldError) as exc:
        fail(f"--in {in_}: {exc}")
    last = last_file(store, d)
    typer.echo(f"release of {key}: " + ("its directory's last file" if last is not None and last.name == key.rsplit("/", 1)[1]
                                        else f"NOT its directory's last file ({last.name if last else 'none'})"), err=True)
    return key


InOpt = Annotated[str, typer.Option(
    "--in", help="The corpus to release: data/corpus.build/<series>/ (its last file) or an exact file.")]
IncompleteOpt = Annotated[bool, typer.Option(
    "--allow-incomplete", help="Release a corpus only an `incomplete` build names: some stage of it "
    "stopped short of its scope.")]
UnbuiltOpt = Annotated[bool, typer.Option(
    "--allow-unbuilt", help="Release a corpus no build record names (a standalone fold.build run).")]


def release(
    ctx: typer.Context,
    in_: InOpt,
    config: Annotated[str | None, typer.Option(
        "--config", help="Release config name or path (default the plan's fold.release.config).")] = None,
    tag: Annotated[str | None, typer.Option(
        "--tag", help="Release tag: releases/<tag>/ (default <config name>-<YYYYMMDD>).")] = None,
    out: Annotated[str | None, typer.Option(
        "--out", help="Not accepted: stack release writes releases/<tag>/ only; a local export is "
        "`stack export --in … --out DIR`.")] = None,
    allow_incomplete: IncompleteOpt = False,
    allow_unbuilt: UnbuiltOpt = False,
    create: CreateOpt = False,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Release a fold.build corpus file into the store's releases/<tag>/ (alias of `stack fold release`)."""
    if out is not None:  # D695: one export verb
        fail(f"stack release writes the store's releases/<tag>/ only (got --out {out!r}); a local export is "
             "`stack export --in <corpus.build file|dir> --out DIR [--config <name|path>]`")
    g = resolve_globals(ctx, store, plan_path)
    plan = load_plan_or_fail(g.plan)
    require_store(g.store, create)
    key = _resolve_in(g.store, in_)
    release_run(g.store, plan, key, config, tag, allow_incomplete=allow_incomplete, allow_unbuilt=allow_unbuilt)


def export(
    ctx: typer.Context,
    in_: InOpt,
    out: Annotated[str, typer.Option(
        "--out", help="The export directory: absent or empty; its name is the tag.")],
    config: Annotated[str, typer.Option(
        "--config", help="Release config name or path.")] = "internal-v1",
    allow_incomplete: IncompleteOpt = False,
    allow_unbuilt: UnbuiltOpt = False,
    release_id: Annotated[str | None, typer.Option(
        "--release-id", help="Dataset configs only: the release id (default the export's UTC date).")] = None,
    blobs: Annotated[str | None, typer.Option(
        "--blobs", help="Dataset configs only: read texts from this directory of <sha256> files.")] = None,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Export a fold.build corpus file to a local directory (no run record, not pushed; default config internal-v1)."""
    g = resolve_globals(ctx, store, plan_path)
    plan = load_plan_or_fail(g.plan) if g.plan is not None else None
    require_store(g.store, create=False)
    key = _resolve_in(g.store, in_)
    from stack.fold.dataset import is_dataset_config, run_dataset_export

    if is_dataset_config(config):
        if allow_unbuilt:
            fail("--allow-unbuilt: a dataset release is made from a build's corpus")
        try:
            res = run_dataset_export(g.store, key, config, out, release_id=release_id, blobs_dir=blobs,
                                     allow_incomplete=allow_incomplete)
        except FoldError as exc:
            fail(str(exc))
        doc = res.release_json
        for w in res.warnings:
            typer.echo(f"warning: {w}", err=True)
        typer.echo(f"dataset {doc['variant']} {doc['release_id']}: {doc['rows']['out']} rows, "
                   f"{len(doc['files'])} files -> {res.path}")
        return
    if release_id is not None or blobs is not None:
        fail("--release-id and --blobs apply to dataset configs only")
    release_run(g.store, plan, key, config, None, allow_incomplete=allow_incomplete, allow_unbuilt=allow_unbuilt,
                out=out)
