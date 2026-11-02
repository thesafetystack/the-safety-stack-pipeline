"""``run_checks``: the library entry point of ``stack verify`` (T1.6 prints its table).

Runs every registered check of Appendix B in :data:`~stack.checks.CHECK_TABLE` order and
returns one :class:`~stack.checks.CheckResult` per name: ``ok``, ``failed``, ``warn`` (X11) or
``skipped`` with ``error_class`` ``not_implemented`` (D33) or the check's own reason
(``no_builds``). A check that raises is ``failed`` with ``error_class = 'check_error'`` and the
exception in its items (a crash is never a pass).

File-scope checks (S1-S10, V3, V4) run over every ``data/`` file of the selected families; S1
failing on a file stops the other file checks for that file (they read columns S1 has not
vouched for), except S2 when the difference is nullability alone. Every file is read through the
one family reader (:func:`stack.store.parquet.read_family`, D295): a file of an older format
version of its family is checked **as its upcast**, its header's ``schema_digest``,
``family_version`` and ``schema_version`` read as the current ones (D208 (4)'s rule, after the
reader validated the originals against the file); a file that does not read (newer, unknown, its
digest not its declared version's, behind an ``Incompatible`` step) is S1's finding with the
reader's message, and so is a file a step's guard refuses (:class:`~stack.schemas.upcasters.UpcastRefused`,
e.g. D591's ``model_mentions`` drop on a file holding data), which store-scope checks then read as
unreadable; any other ``UpcastBug`` stays a ``check_error``. Store-scope checks get one :class:`~stack.checks.chain.StoreView`, whose table
and view cache is emptied after each of them (round 5: a check's reads do not outlive it). X1 always runs
over all run records and all of ``data/`` (D38), whatever ``families`` says.

**Parts.** A verify runs some of three **parts**, or all of everything (**full**):

* ``structural`` (:data:`STRUCTURAL_CHECKS`; ``stack verify --structural``, and every build at its
  start and its finish): file damage over the whole store, reading footers, records, headers, the S3
  listing, the chains' key columns and blob digests, never a data file in full: S1 from each file's
  footer (:func:`stack.store.parquet.file_format`), V11 (each chain resolves: linear, bases present,
  and on the files from its newest full file to its head the keys unique and ``ids_digest``
  recomputing), X23 (each base a published file of its directory), X1 (presence, footer row count,
  a mirror copy's size) and X5 (blob presence, raw snapshot sizes).
* ``files``: the files the build's runs wrote (or ``runs``'), the **scope**: every file-scope check,
  and the per-file store checks (:data:`PER_FILE_STORE`: the pair checks against each file's base
  view, V7, V8) on them only. ``skipped[no_builds]`` without a build or ``runs``.
* ``build_set``: every other store-scope check (X2-X25 less the structural ones), on the build.

``stack verify`` runs ``files`` and ``build_set`` on the newest finished (``ok`` or ``incomplete``) build (``--build``, ``--series``;
``--no-build-set`` drops ``build_set``); a build's finish runs all three parts. **Full** (``parts=None``;
``stack verify --full``, ``stack build --verify-full``, later a scheduled run between builds): every
check on every file of the selected families, every local copy of a recorded data file hashed against
its record (:class:`~stack.checks.chain.StoreView`, X1). A check of a part not run is
``skipped[<part>]``. Nothing is carried from one verify to the next: no record of earlier verdicts
decides what runs, and the one cache a verify keeps beyond a check, of parquet footers
(:func:`stack.store.parquet.footer_cache`, D731), is keyed by each footer's bytes read afresh on
every lookup and dropped when the verify returns. A file a later change to a check would flag, or a
file corrupted with its size unchanged, is found by the next full verify.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa

# Importing the implementing modules registers their checks.
from stack.checks import (
    CHECK_TABLE,
    REGISTRY,
    CheckResult,
    Finding,
    result,
    skipped,
)
from stack.checks import annotate as _annotate  # noqa: F401
from stack.checks import blocks as _blocks  # noqa: F401
from stack.checks import build_checks as _build_checks  # noqa: F401
from stack.checks import content as _content  # noqa: F401
from stack.checks import family as _family  # noqa: F401
from stack.checks import fold_checks as _fold_checks  # noqa: F401
from stack.checks import lookup as _lookup  # noqa: F401
from stack.checks import schema as _schema
from stack.checks import triage as _triage  # noqa: F401
from stack.checks.chain import (
    PAIR_CHECKS,
    DataFile,
    SkipCheck,
    StoreView,
)
from stack.schemas.upcasters import UpcastRefused
from stack.store.index import store_index
from stack.store.layout import STORE_SCHEMA_VERSION
from stack.store.parquet import (
    SchemaMismatch,
    file_format,
    footer_cache,
    read_family,
    read_raw,
)

_S1, _S2 = "schema_equal", "nonnull"
#: The parts of a verify that is not full (module docstring), in this order.
STRUCTURAL, FILES, BUILD_SET = "structural", "files", "build_set"
PARTS: tuple[str, ...] = (STRUCTURAL, FILES, BUILD_SET)
#: ``stack verify``'s parts, and a build's at its finish.
CHECK_PARTS: frozenset[str] = frozenset({FILES, BUILD_SET})
BUILD_FINISH_PARTS: frozenset[str] = frozenset(PARTS)
#: The structural part's checks, over the whole store (module docstring), by code.
STRUCTURAL_CHECKS: frozenset[str] = frozenset({"S1", "V11", "X1", "X5", "X23"})
#: Store-scope checks that judge one file at a time (with its base view or its same-run partner),
#: by code: the ``files`` part runs them on the scope's files only.
PER_FILE_STORE: frozenset[str] = frozenset({*PAIR_CHECKS, "V7", "V8"})


def _relaxed_type(t: pa.DataType) -> pa.DataType:
    if pa.types.is_struct(t):
        return pa.struct([pa.field(f.name, _relaxed_type(f.type), True) for f in t])
    if pa.types.is_list(t):
        return pa.list_(pa.field("item", _relaxed_type(t.value_type), True))
    return t


def _relaxed_schema(schema: pa.Schema) -> pa.Schema:
    """``schema`` with every field nullable and list items named ``item``."""
    return pa.schema([pa.field(f.name, _relaxed_type(f.type), True) for f in schema])


def _file_findings(view: StoreView, df: DataFile, names: set[str]) -> dict[str, list[Finding]]:
    """Findings of every selected file-scope check on one file, messages prefixed by its name."""
    out: dict[str, list[Finding]] = {}

    def add(check: str, fs: list[Finding]) -> None:
        out.setdefault(check, []).extend(
            Finding(f.check, f.level, f"{df.key}: {f.message}", f.n, f.items) for f in fs)

    spec = df.spec
    if spec is None:
        if "kv_complete" in names:
            add("kv_complete", [Finding("kv_complete", "hard",
                                        f"header family {df.family!r} is not registered")])
        return out
    header = df.header
    try:
        table, _ = read_family(df.path)
        s1 = _schema.schema_equal(table, header, spec)
        if not file_format(df.path, header).is_current:  # an older version, upcast (module docstring)
            header = {**header, "schema_digest": spec.schema_digest, "family_version": str(spec.version),
                      "schema_version": STORE_SCHEMA_VERSION}
    except UpcastRefused as exc:  # a property of the file, not a bug (module docstring)
        if _S1 in names:
            add(_S1, [Finding(_S1, "hard", f"{spec.name}: {type(exc).__name__}: {exc}", 1, (str(exc),))])
        return out
    except SchemaMismatch as exc:
        table = read_raw(df.path)
        s1 = [Finding(_S1, "hard", f"{spec.name}: {type(exc).__name__}: {exc}", 1, (str(exc),)),
              *_schema.schema_equal(table, header, spec)]
    if s1:
        if _S1 in names:
            add(_S1, s1)
        # A NULL in a `!` column can only reach data/ through a writer that declared the column
        # nullable (pyarrow refuses it on write), so S2 also runs when S1's difference is
        # nullability alone: it names the columns that actually hold NULLs.
        if _S2 in names and _relaxed_schema(table.schema) == _relaxed_schema(spec.schema):
            add(_S2, _schema.nonnull(table, df.header, spec))
        return out
    for name in names:
        cs = REGISTRY[name]
        if cs.fn is None or cs.scope != "file" or name == _S1:
            continue
        add(name, cs.fn(table, header, spec))
    return out


def _prefixed(df: DataFile, fs: list[Finding]) -> list[Finding]:
    return [Finding(f.check, f.level, f"{df.key}: {f.message}", f.n, f.items) for f in fs]


def _footer_s1(df: DataFile) -> list[Finding]:
    """S1 from the footer alone (the structural part): the file's format version is one this code reads at
    the current version (:func:`stack.store.parquet.file_format`: the header's ``schema_digest`` is
    the file's own schema's and its declared version's)."""
    if df.spec is None:
        return [Finding(_S1, "hard", f"{df.key}: header family {df.family!r} is not registered")]
    try:
        file_format(df.path, df.header)
    except SchemaMismatch as exc:
        return [Finding(_S1, "hard", f"{df.key}: {df.spec.name}: {type(exc).__name__}: {exc}", 1, (str(exc),))]
    return []


def written_files(store_root: str | Path, runs: Sequence[str]) -> frozenset[str]:
    """The store keys of the files the run records ``runs`` name as outputs (the ``files`` part's
    scope); an unknown run id raises ``KeyError``."""
    idx = store_index(store_root)
    keys: set[str] = set()
    for run_id in runs:
        hit = idx.run(str(run_id))
        if hit is None:
            raise KeyError(f"no run record of run {run_id!r}")
        keys |= {str(o["path"]) for o in hit.record.outputs.values()}
    return frozenset(keys)


class VerifyResults(list[CheckResult]):
    """:func:`run_checks`' results (Appendix B order), with ``mode`` (``full``, or the parts run joined
    by ``+``, e.g. ``files+build_set``) and ``scope`` (the store keys of the files the ``files`` part
    checked, ``None`` when it did not run or in full mode)."""

    mode: str
    scope: frozenset[str] | None


def run_checks(store_root: str | Path, *, families: Sequence[str] | None = None,
               dirs: Sequence[str] | None = None, build: str | Path | Mapping[str, Any] | None = None,
               names: Sequence[str] | None = None, parts: Collection[str] | None = None,
               runs: Sequence[str] | None = None) -> VerifyResults:
    """Every check of Appendix B over the store at ``store_root``, one result per name in
    Appendix B order (:class:`VerifyResults`).

    ``families`` restricts the files and chains checked (``None`` = all), ``dirs`` (series
    directory keys, ``verify --in``, D379) to those directories' files; ``build`` is the
    reference build record (a path to a ``.build.json.zst``, or the record as a mapping) for
    X2/X10/X14 — default the newest finished (``ok`` or ``incomplete``) build of the store index (with ``dirs``, of their one
    series; several series raise :class:`stack.checks.chain.MixedSeriesError`), ``skipped[no_builds]``
    when there is none; ``names`` restricts the checks run (unknown names raise ``KeyError``).
    ``parts``: the parts to run (module docstring; ``None``: full); the ``files`` part's scope is
    the files ``runs`` wrote, else the build's ``runs``' (an unknown run raises ``KeyError``).
    ``runs`` without the ``files`` part is refused (``ValueError``). Each distinct parquet footer is
    parsed once per call (:func:`stack.store.parquet.footer_cache`, D731).
    """
    with footer_cache():
        return _run_checks(store_root, families=families, dirs=dirs, build=build, names=names, parts=parts,
                           runs=runs)


def _run_checks(store_root: str | Path, *, families: Sequence[str] | None, dirs: Sequence[str] | None,
                build: str | Path | Mapping[str, Any] | None, names: Sequence[str] | None,
                parts: Collection[str] | None, runs: Sequence[str] | None) -> VerifyResults:
    if parts is not None and not set(parts) <= set(PARTS):
        raise ValueError(f"unknown verify part(s) {sorted(set(parts) - set(PARTS))}; parts: {list(PARTS)}")
    if runs is not None and (parts is None or FILES not in parts):
        raise ValueError("runs scope the files part only; a full verify checks every file")
    wanted = [n for _, n, _ in CHECK_TABLE] if names is None else list(names)
    for n in wanted:
        if n not in REGISTRY:
            raise KeyError(f"unknown check {n!r}; checks: {[c[1] for c in CHECK_TABLE]}")
    wanted_set = set(wanted)
    view = StoreView(store_root, families=list(families) if families is not None else None,
                     dirs=dirs, build=build, rehash=parts is None)
    if parts is not None and FILES in parts:
        if runs is None and view.build is not None:
            runs = [str(r) for r in view.build.get("runs") or []]
        view.scope = written_files(store_root, runs) if runs is not None else None

    def skip_why(n: str) -> str | None:
        """Why check ``n`` does not run with these parts (module docstring), else ``None``."""
        if parts is None:
            return None
        cs = REGISTRY[n]
        if cs.code in STRUCTURAL_CHECKS and STRUCTURAL in parts:
            return None
        if cs.scope == "file" or cs.code in PER_FILE_STORE:
            return (None if view.scope is not None else "no_builds") if FILES in parts else FILES
        if cs.code in STRUCTURAL_CHECKS:
            return STRUCTURAL
        return None if BUILD_SET in parts else BUILD_SET

    findings: dict[str, list[Finding]] = {n: [] for n in wanted}
    errors: dict[str, str] = {}
    skips: dict[str, str] = {n: why for n in wanted if REGISTRY[n].fn is not None and (why := skip_why(n))}
    file_names = {n for n in wanted if REGISTRY[n].fn is not None and REGISTRY[n].scope == "file"
                  and n not in skips}
    if file_names:
        for df in view.files:
            try:
                if parts is None or (view.scope is not None and view.in_scope(df)):
                    for check, fs in _file_findings(view, df, file_names).items():
                        findings[check].extend(fs)
                elif _S1 in file_names and STRUCTURAL in parts:
                    findings[_S1].extend(_footer_s1(df))
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                for n in file_names:
                    errors.setdefault(n, f"{df.key}: {type(exc).__name__}: {exc}")
    for n in wanted:
        cs = REGISTRY[n]
        if cs.fn is None or cs.scope != "store" or n in skips:
            continue
        try:
            findings[n].extend(cs.fn(view))
        except SkipCheck as why:
            skips[n] = str(why.args[0])
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            errors[n] = f"{type(exc).__name__}: {exc}"
        finally:
            view.drop_cache()

    out: list[CheckResult] = []
    order = {name: i for i, (_, name, _) in enumerate(CHECK_TABLE)}
    for n in sorted(wanted_set, key=order.__getitem__):
        cs = REGISTRY[n]
        if cs.fn is None:
            out.append(skipped(n, "not_implemented"))
        elif n in errors:
            out.append(CheckResult(n, cs.level, "failed",
                                   (Finding(n, cs.level, errors[n], 1, (errors[n],)),),
                                   "check_error", cs.code))
        elif n in skips:
            out.append(skipped(n, skips[n]))
        else:
            out.append(result(n, findings[n]))
    res = VerifyResults(out)
    res.mode = "full" if parts is None else "+".join(p for p in PARTS if p in parts)
    res.scope = view.scope
    return res


def mode_line(results: VerifyResults) -> str:
    """The line ``stack verify`` prints after the summary: what the verify covered."""
    if results.mode == "full":
        return "mode: full (every check on every file, data files re-hashed)"
    said = []
    if STRUCTURAL in results.mode.split("+"):
        said.append("structural: footers, records, chains and blobs of the whole store")
    if FILES in results.mode.split("+"):
        said.append("files: no build or run" if results.scope is None
                    else f"files: {len(results.scope)} file(s) the build or run(s) wrote")
    if BUILD_SET in results.mode.split("+"):
        said.append("build set: the build checks")
    return f"mode: {results.mode} ({'; '.join(said)}; --full checks every file)"


def summary(results: Sequence[CheckResult]) -> str:
    """One line: counts per status, ``skipped`` split by ``error_class``."""
    counts: dict[str, int] = {}
    for r in results:
        key = f"skipped[{r.error_class}]" if r.status == "skipped" else r.status
        counts[key] = counts.get(key, 0) + 1
    return f"{len(results)} checks: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))


__all__ = ["BUILD_FINISH_PARTS", "BUILD_SET", "CHECK_PARTS", "FILES", "PARTS", "PER_FILE_STORE", "STRUCTURAL",
           "STRUCTURAL_CHECKS", "VerifyResults", "mode_line", "run_checks", "summary", "written_files"]
