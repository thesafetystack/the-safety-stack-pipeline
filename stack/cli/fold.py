"""``stack fold <config> --input documents=<series dir|file>|corpus=<file> [--out data/<family>/<series>/]
[--restart] [--comment C] [--filter SQL|name] [--join <wf>=<file>]… [--release <config>] [--tag <tag>]
[--allow-incomplete] [--allow-unbuilt]``
(§3.2, D65, D67; D373): one fold as one run.

``--out`` is the destination directory (D373): ``corpus`` and ``build`` need it (e.g.
``data/corpus/<series>/``, ``data/corpus.build/<series>/``), ``compact`` defaults to its input's
directory; ``--restart`` allows a full without a base into a non-empty directory the fold reads.

Wraps :func:`stack.fold.run_fold` in :func:`stack.stage_runner.run_recorded`: a ``run_id`` is
minted, the provisional record written, ``run_fold`` publishes with the runner's publish hook
writing the final ``ok`` record (D38), so a fold run is a run like any other to ``status`` and
``verify``. Filter precedence: ``--filter``, the plan's ``fold.<config>.filter`` (D66), the
config's default. ``stack run fold.<config>`` reaches the same fold through the stage adapter
(:mod:`stack.fold.stage`).

``build`` takes ``--input corpus=<fold.corpus file>`` and ``--join <wf>=<file>`` per workflow
(a registered workflow's ``data/`` file id or path; an unregistered workflow's parquet path, folded
into ``annotations.other[]``). ``release`` takes ``--input corpus=<fold.build file>``,
``--release <name|path>`` (default the plan's ``fold.release.config``) and ``--tag`` (default
``<config name>-<YYYYMMDD>``), and writes ``releases/<tag>/`` (D21) through
:func:`stack.fold.release.run_release`; its run record (stage ``fold.release``) has ``outputs.release``
with the path ``releases/<tag>/corpus.parquet``. It takes no ``--out``: a local export directory is
``stack export`` only (D647, D694, D695; :mod:`stack.cli.release`), where the printed line
has ``run_id`` (minted, no run record), ``status: ok`` and ``outputs.release`` = the absolute path of
``DIR/corpus.parquet``. ``stack release`` is its alias (D18). The destination is checked once, before
the run (:func:`stack.fold.release.resolve_destination`). Bad inputs, an unknown release config, an existing tag and a corpus
:func:`stack.fold.release.release_gate` refuses (no ``ok`` build names it; ``--allow-incomplete`` for an
``incomplete`` build's, ``--allow-unbuilt`` for one no build names, D494) are refused before any run exists;
one stderr line names the build the release records and its status. Exit status as ``stack run``: 0 when the record is ``ok``, else 1; the
record's status and outputs are printed as one JSON line.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from stack.cli.common import (
    CommentOpt,
    PlanOpt,
    StoreOpt,
    fail,
    load_plan_or_fail,
    pairs,
    require_store,
    resolve_globals,
)
from stack.core import ids
from stack.fold import (
    WRITER,
    FoldError,
    check_restart,
    corpus_format,
    data_file,
    run_fold,
)
from stack.fold.configs import FOLD_CONFIGS, fold_config
from stack.fold.release import (
    load_release_config,
    release_gate,
    release_spec,
    resolve_destination,
    run_release,
)
from stack.plan import Plan
from stack.runs import RunRecord, file_digest
from stack.stage_runner import run_recorded
from stack.store.chain import Published
from stack.store.layout import NONE, parse_data_key, parse_dir_ref, series_dir
from stack.store.parquet import read_header


def _line(run_id: str, status: str, outputs: dict[str, str], error_class: str | None = None,
          error: str | None = None) -> None:
    """The one JSON result line (``run_id``, ``status``, ``error_class``, ``outputs``: name → path); exit 1
    unless ``ok``, the error on stderr."""
    typer.echo(json.dumps({"run_id": run_id, "status": status, "error_class": error_class, "outputs": outputs}))
    if status != "ok":
        if error:
            typer.echo(error, err=True)
        raise typer.Exit(1)


def _emit(rec: RunRecord) -> None:
    _line(rec.run_id, rec.status, {k: o["path"] for k, o in rec.outputs.items()}, rec.error_class, rec.error)


def corpus_schema_gate(path: Path, header: dict[str, str]) -> None:
    """Before a fold or release run (R1-2, R8-2, D295): a corpus of an older format version is
    read upcast (a note on stderr); one that does not read is refused here with the next step, so
    it never becomes a ``failed[code_bug]`` run with a traceback."""
    try:
        fmt = corpus_format(path, header)
    except FoldError as exc:
        fail(str(exc))
    if not fmt.is_current:
        typer.echo(f"note: {path.name} is {fmt.describe()}", err=True)


def release_run(store: Path, plan: Plan | None, corpus_ref: str, release: str | None, tag: str | None, *,
                allow_incomplete: bool = False, allow_unbuilt: bool = False, out: str | None = None) -> None:
    """``fold.release`` (shared by ``stack fold release``, ``stack release`` and ``stack export``), its result
    line printed. Refusals before anything runs: :func:`stack.cli.common.fail`, the destination's
    (:func:`stack.fold.release.resolve_destination`, once) and the release gate's included
    (:func:`stack.fold.release.release_gate`, D494); one stderr line names the build released and its status.
    Into the store: one recorded run (:func:`~stack.stage_runner.run_recorded`). With ``out`` (D647): a local
    export, no run record, no push; the line has a minted ``run_id`` and the directory's absolute path."""
    ref = release or (plan.fold.release.config if plan is not None and plan.fold.release is not None else None)
    if ref is None:
        fail("fold.release needs --release <config> (the plan has no fold.release.config)")
    try:
        cfg = load_release_config(ref)
        path = data_file(store, corpus_ref, "corpus")
    except FoldError as exc:
        fail(str(exc))
    h = read_header(path)
    if h.get("family") != "corpus" or h.get("fold_config") != "build":
        fail(f"input corpus={corpus_ref}: {path.name} is not a fold.build corpus file "
             f"(family {h.get('family')!r}, fold_config {h.get('fold_config')!r})")
    corpus_schema_gate(path, h)
    try:
        dest = resolve_destination(store, cfg, out, tag)
    except FoldError as exc:
        fail(str(exc))
    if dest.out is None and plan is None:
        fail("a release into the store is a recorded run: it needs --plan or STACK_PLAN")
    try:
        bid, status = release_gate(store, h["file_id"], allow_incomplete=allow_incomplete, allow_unbuilt=allow_unbuilt)
    except FoldError as exc:
        fail(str(exc))
    typer.echo(f"release of corpus {h['file_id']}: " + (f"build {bid} ({status})" if bid != NONE
                                                        else "no build names it (--allow-unbuilt)"), err=True)
    if dest.out is not None:  # D647: a local export is not a run; release.json is its record
        typer.echo(f"export of corpus {h['file_id']} to {dest.out} (no run record, not pushed)", err=True)
        rid = ids.mint("run")
        try:
            res = run_release(store, run_id=rid, inputs={"corpus": h["file_id"]}, release=ref, dest=dest,
                              allow_incomplete=allow_incomplete, allow_unbuilt=allow_unbuilt)
        except FoldError as exc:
            fail(str(exc))
        _line(rid, "ok", {"release": str(res.corpus_path)})
        return

    def body(rec: RunRecord, on_published: Callable[[list[Published]], None]) -> None:
        res = run_release(store, run_id=rec.run_id, inputs={"corpus": h["file_id"]}, release=ref, dest=dest,
                          allow_incomplete=allow_incomplete, allow_unbuilt=allow_unbuilt)
        rel = res.corpus_path.relative_to(store).as_posix()
        rec.inputs = {"corpus": res.corpus_file_id}
        rec.config_digest = res.config.digest
        rec.fold = res.fold
        rec.counts = {"rows_in": res.rows_in, "rows_out": res.rows_out, "filtered_out": res.filtered_out,
                      **{f"dropped.{k}": v for k, v in res.dropped.items()}}
        rec.outputs = {"release": {"file_id": f"{rec.run_id}.release", "path": rel,
                                   "file_digest": file_digest(res.corpus_path), "row_count": res.rows_out,
                                   "schema_digest": release_spec(res.config).schema_digest,
                                   # a release is a chain of one (D322)
                                   "chain_id": f"{rec.run_id}.release", "kind": "full"}}

    assert plan is not None  # checked before the gate
    _emit(run_recorded(store, stage="fold.release", writer=WRITER, plan_name=plan.plan,
                       inputs={"corpus": h["file_id"]}, body=body))


def fold(
    ctx: typer.Context,
    config: Annotated[str, typer.Argument(help=f"Fold config: {', '.join(FOLD_CONFIGS)}.")],
    input: Annotated[list[str] | None, typer.Option(
        "--input", help="documents=<data/documents/<series>/ or an exact file> (corpus, compact) or "
        "corpus=<file_id|path> (build: a fold.corpus file; release: a fold.build file).")] = None,
    out: Annotated[str | None, typer.Option(
        "--out", help="Destination directory data/<family>/<series>/ (corpus, build: required; compact: default "
        "the input's directory). Not for release: a local export is `stack export`.")] = None,
    restart: Annotated[bool, typer.Option(
        "--restart", help="A full without a base into a non-empty directory the fold reads (D373).")] = False,
    comment: CommentOpt = None,
    filter: Annotated[str | None, typer.Option(
        "--filter", help="Named filter (has_text, passed_triage, everything) or a SQL predicate over the "
        "row; default the plan's fold.<config>.filter.")] = None,
    join: Annotated[list[str] | None, typer.Option(
        "--join", help="<wf>=<file_id|path>: fold that workflow's file into annotations (build).")] = None,
    release: Annotated[str | None, typer.Option(
        "--release", help="Release config name or path (release; default the plan's fold.release.config).")] = None,
    tag: Annotated[str | None, typer.Option(
        "--tag", help="Release tag: releases/<tag>/ (release; default <config name>-<YYYYMMDD>).")] = None,
    allow_incomplete: Annotated[bool, typer.Option(
        "--allow-incomplete", help="Release a corpus only an `incomplete` build names (release).")] = False,
    allow_unbuilt: Annotated[bool, typer.Option(
        "--allow-unbuilt", help="Release a corpus no build names: a standalone fold.build run (release).")] = False,
    store: StoreOpt = None,
    plan_path: PlanOpt = None,
) -> None:
    """Run fold CONFIG as one recorded run (union, join, filter, release, write)."""
    g = resolve_globals(ctx, store, plan_path)
    require_store(g.store, create=False)  # R6-5: a fold reads a store's files; it never creates one
    try:
        spec = fold_config(config)
    except KeyError as exc:
        fail(str(exc.args[0]))
    inputs = dict(pairs(input, "--input"))
    if set(inputs) != {spec.input_kind}:
        fail(f"fold.{config} takes --input {spec.input_kind}=<…> only (got {sorted(inputs) or 'none'})")
    joins = dict(pairs(join, "--join"))
    if joins and not spec.joins:
        fail(f"fold.{config} joins no <wf> files (got --join {sorted(joins)})")
    if release is not None and not spec.release:
        fail(f"fold.{config} applies no release config (got --release {release!r})")
    if tag is not None and not spec.release:
        fail(f"fold.{config} writes no release (got --tag {tag!r})")
    if (allow_incomplete or allow_unbuilt) and not spec.release:
        fail(f"fold.{config} writes no release (got --allow-incomplete/--allow-unbuilt)")
    if filter is not None and spec.default_filter is None:
        fail(f"fold.{config} has no filter step (got --filter {filter!r})")
    if out is not None and spec.output == "release_dir":  # D694, D695: one export verb
        fail(f"fold.{config} writes the store's releases/<tag>/ only (got --out {out!r}); a local export is "
             "`stack export --in <corpus.build file|dir> --out DIR [--config <name|path>]`")
    plan = load_plan_or_fail(g.plan)
    if spec.output == "release_dir":
        release_run(g.store, plan, inputs["corpus"], release, tag, allow_incomplete=allow_incomplete,
                    allow_unbuilt=allow_unbuilt)
        return
    if spec.input_kind == "corpus":
        try:
            cpath = data_file(g.store, inputs["corpus"], "corpus")
        except FoldError as exc:
            fail(str(exc))
        h = read_header(cpath)
        if h.get("family") != "corpus" or h.get("fold_config") != "corpus":
            fail(f"fold.{config}: input corpus={inputs['corpus']} is not a fold.corpus file "
                 f"(family {h.get('family')!r}, fold_config {h.get('fold_config')!r})")
        corpus_schema_gate(cpath, h)
    if out is None and config in ("corpus", "build"):
        fail(f"fold.{config} needs --out data/{spec.output_family}[.<x>]/<series>/ (D373)")
    if out is not None and spec.input_kind == "documents":  # the restart guard, before the run (usage error)
        ref = inputs["documents"].strip()
        try:
            src = parse_data_key(ref).dir if ref.endswith(".parquet") else series_dir(*parse_dir_ref(ref))
        except ValueError as exc:
            fail(f"--input documents={ref}: {exc}")
        if src.strip("/") != out.strip("/"):
            try:
                check_restart(g.store, config, out, restart=restart)
            except FoldError as exc:
                fail(str(exc))
    filt = filter if filter is not None else plan.fold.filter_for(config)

    def body(rec: RunRecord, on_published: Callable[[list[Published]], None]) -> None:
        run_fold(g.store, config, run_id=rec.run_id, plan_name=plan.plan, inputs=inputs, filter=filt,
                 joins=joins or None, header_extra={"code_digest": rec.code_digest},
                 run_at=rec.started_at, out=out, comment=comment, restart=restart, on_published=on_published)

    _emit(run_recorded(g.store, stage=spec.stage, writer=WRITER, plan_name=plan.plan, inputs=inputs,
                       filter=filt, body=body))
