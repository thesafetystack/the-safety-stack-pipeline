"""V9 ``V-fold`` and V10 ``V-release`` (Appendix B; §3.2, D21, D24; T5.2).

**V9** (file scope; ``corpus`` files whose header ``fold_config`` is ``build``):

* every row satisfies the recorded ``filter`` (the resolved SQL predicate; a NULL result fails);
* ``annotations_folded`` covers the folded set: every typed workflow other than the carried
  ``triage`` that is non-NULL in some row, and every workflow in some row's ``other[]``, is in the
  header's ``annotations_folded`` (subset reading: a joined workflow may match no row after the
  filter, so the converse is not required);
* mapping M holds for every ``ANN`` present (typed, ``triage`` included, and ``other[]``):
  ``is_basis_current`` equals its recomputation against the row's current basis
  (``content.current_text_sha256`` for ``text``, ``metadata.fields_digest`` for ``fields``,
  :func:`stack.fold.chained_basis` over the row's ``annotations`` for ``chained``, D235;
  NULL when either side is NULL); a recomputed ``false``, or NULL on an ``ok``, is
  ``skipped[stale_basis]``.

**V10** (store scope; every ``releases/<tag>/`` directory, ``.``-prefixed staging directories
ignored) runs :func:`validate_release` on each; the writer runs it on its stage (both destinations) and
``stack verify --release DIR`` on any release directory (D647). The directory may be crafted: the validator
reads only regular files that are not symlinks (a symlink, directory or special file where a file belongs is
a finding, never followed or read; ``blobs/`` must be a real directory), hashes blobs by streaming, reports an
unreadable ``release.json`` or ``corpus.parquet`` as a finding, and evaluates the recorded ``filter`` on
:func:`stack.query.sql_connection` (no file access, one statement). ``release.json``'s ``release_format`` is
classified first: absent or below 2 (written before D647) is superseded: :func:`validate_release` reports
only that, and V10 lists it as a ``warn`` (not validated, not upcast; re-release it); above 2 is reported as
newer than this code reads (a problem; V10: a violation). Otherwise it checks: ``release.json`` has A.7's
keys and the D647 keys, its ``tag`` is the directory name and its
``config_digest`` the sha256 of the JCS of the embedded config **as stored** (D647: not re-dumped
through the current model, which would add defaulted keys), its ``filter`` the config's resolved filter, its
``build_status`` (D705; optional: releases before it lack it) an object whose ``status`` is ``ok``,
``incomplete`` or ``unbuilt``, ``unbuilt`` exactly when ``build_id`` is ``none``, and ``incomplete`` with
``incomplete`` entries or ``stopped`` (a drained build), its
``schema_digest`` the digest of the file's schema; ``corpus.parquet`` has the release schema
derived from that config (S1's comparison, :func:`stack.fold.release.release_spec`; a release written
under an older documents schema is checked as its additive upcast), a footer ``file_id`` and ``row_count`` equal to
``release.json``'s and the row count; per row the licence is read from the supplying member
(:func:`stack.fold.release.supplier_classes`): no row whose anchor class has ``rows: drop`` or that
``row_drops`` names (masked, retired, sensitivity); no abstract from a class that drops it; text from
a withholding class is ``withheld``, and a withheld row has no blob reference anywhere
(:data:`stack.fold.release.WITHHELD_REFS`); a snapshot whose member withholds has no reference, nor
do its in-band entries; every registry
``text_bearing`` value is within the text supplier's ``excerpt_cap_chars`` (none when ``null``);
``other[]`` is gone under ``other: drop``. Bytes (the release form: ``BLOB`` references ``raw``/``text``,
:func:`stack.fold.release.selected_refs`): ``snapshots: current`` leaves at most the current snapshot,
each BLOB's ``inline`` is in a selected element placed inline, hashes to its ``sha256`` and has
``bytes`` = its length, every selected element is inline, in ``blobs/`` or listed in ``missing-blobs.txt`` (only under ``missing: pointer``),
a selected element's BLOB placed in ``blobs/`` has ``bytes`` = that file's size, each ``blobs/`` file
hashes to its name and is referenced, each ``missing-blobs.txt`` line is a sha256 hex digest of a selected
element that is not in ``blobs/``, and ``release.json``'s ``content`` counters (``elements``,
``inline_count``, ``inline_bytes``, ``blobs_count``, ``blobs_bytes``, ``records``, ``missing``) recompute;
the ``filter`` is re-evaluated over the rows when ``filter_checkable``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from stack.checks import Finding, register
from stack.checks.chain import StoreView
from stack.checks.rows import iter_rows
from stack.checks.schema import schema_equal
from stack.fold import FoldError, chained_basis, filter_rows
from stack.fold.release import (
    BUILD_STATUSES,
    PARTS,
    RELEASE_FORMAT,
    RELEASE_JSON,
    WITHHELD,
    WITHHELD_REFS,
    ReleaseConfig,
    Ref,
    config_json_digest,
    ref_sha256,
    release_spec,
    selected_refs,
    supplier_classes,
    text_bearing_values,
)
from stack.query import one_statement, resolve_filter, sql_connection
from stack.schemas.families import CHAIN_WORKFLOWS, FamilySpec, schema_digest
from stack.schemas.payloads.registry import REGISTRY
from stack.schemas.upcasters import UpcastError, upcast_table
from stack.store.blobs import _file_sha256, blob_key
from stack.store.layout import NONE, RELEASES

Header = dict[str, str]
_EXAMPLES = 5
#: A.7's ``release.json`` keys.
RELEASE_JSON_KEYS: tuple[str, ...] = ("build_id", "tag", "config_digest", "config", "dropped")
#: The keys a ``release_format: 2`` ``release.json`` adds (D647).
RELEASE_JSON_KEYS_V2: tuple[str, ...] = (
    "corpus_file_id", "run_id", "row_count", "schema_digest", "release_format", "file_id", "audience", "filter",
    "filter_checkable", "source", "rows_in", "filtered_out", "classes", "content")
#: ``release.json`` ``content`` counters the validator recomputes (as :func:`stack.fold.release.preflight`).
CONTENT_COUNTS: tuple[str, ...] = ("elements", "inline_count", "inline_bytes", "blobs_count", "blobs_bytes",
                                   "records", "missing")
_NOT_READ = "not a regular file (a symlink, directory or special file is not read)"


def _finding(check: str, message: str, bad: list[str], level: str = "hard") -> list[Finding]:
    return [Finding(check, level, message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


def _typed_names(anns: dict[str, Any]) -> list[str]:
    return [k for k in anns if k != "other"]


# ---------------------------------------------------------------- V9
def _expected_basis(ann: dict[str, Any], text: str | None, fields: str | None,
                    annotations: dict[str, Any]) -> bool | None:
    if ann.get("basis_kind") == "chained":  # D235: over the row's (mapped) reads
        spec = REGISTRY.get(str(ann.get("workflow") or ""))
        cur = chained_basis(text, annotations, spec.reads) if spec is not None and spec.reads else None
    else:
        cur = {"text": text, "fields": fields}.get(str(ann.get("basis_kind")))
    if ann.get("basis_digest") is None or cur is None:
        return None
    return bool(ann["basis_digest"] == cur)


@register("V9", "V-fold")
def v_fold(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V9 (module docstring); other files pass."""
    if spec.kind != "corpus" or header.get("fold_config") != "build":
        return []
    bad: list[str] = []
    # rows satisfy the recorded filter
    pred = resolve_filter(header.get("filter") or "true")
    con = sql_connection()  # the header's filter is supplied SQL: no file access, one statement
    try:
        con.register("fold_file", table)
        try:
            out = con.execute(one_statement(f"SELECT doc_id FROM fold_file d WHERE NOT coalesce(({pred}), false) "
                                            "ORDER BY doc_id")).fetchall()
            bad += [f"{d}: does not satisfy the recorded filter" for (d,) in out]
        except duckdb.Error as exc:
            bad.append(f"recorded filter {pred!r} does not evaluate: {exc}")
    finally:
        con.close()
    # annotations_folded covers the folded set
    try:
        folded = set(json.loads(header.get("annotations_folded") or "[]"))
    except ValueError:
        folded = set()
        bad.append(f"annotations_folded {header.get('annotations_folded')!r} is not a JSON list")
    present: set[str] = set()
    for r in iter_rows(table, ["annotations"]):
        a = r["annotations"]
        present |= {w for w in _typed_names(a) if a[w] is not None and w not in CHAIN_WORKFLOWS}
        present |= {o["workflow"] for o in a["other"]}
    missing = sorted(present - folded)
    if missing:
        bad.append(f"workflows {missing} folded in rows but not in annotations_folded {sorted(folded)}")
    # mapping M
    for r in iter_rows(table, ["doc_id", "metadata", "content", "annotations"]):
        text = (r["content"] or {}).get("current_text_sha256")
        fields = r["metadata"]["fields_digest"]
        a = r["annotations"]
        anns = [(w, a[w]) for w in _typed_names(a) if a[w] is not None]
        anns += [(f"other:{o['workflow']}", o) for o in a["other"]]
        for name, ann in anns:
            want = _expected_basis(ann, text, fields, a)
            if ann.get("is_basis_current") != want:
                bad.append(f"{r['doc_id']}: {name}.is_basis_current {ann.get('is_basis_current')} "
                           f"recomputes to {want}")
            stale = want is False or (want is None and ann["status"] == "ok")
            if stale and (ann["status"], ann["error_class"]) != ("skipped", "stale_basis"):
                bad.append(f"{r['doc_id']}: {name} is {ann['status']}[{ann['error_class']}] on a "
                           f"stale basis (want skipped[stale_basis])")
    return _finding("V-fold", "{n} fold violation(s) in a build corpus", bad)


# ---------------------------------------------------------------- V10
def _get(obj: Any, path: str) -> list[Any]:
    """Values at a dotted path (lists transparent) under ``obj``."""
    vals = [obj]
    for part in path.split("."):
        nxt: list[Any] = []
        for v in vals:
            if isinstance(v, dict) and v.get(part) is not None:
                x = v[part]
                nxt.extend(x if isinstance(x, list) else [x])
        vals = nxt
    return vals


def _check_release_row(row: dict[str, Any], cfg: ReleaseConfig, bad: list[str]) -> None:
    d = row.get("doc_id")
    ident, meta, content = row.get("identity") or {}, row.get("metadata") or {}, row.get("content")
    try:
        cls = supplier_classes(row, cfg)
    except (KeyError, TypeError):
        bad.append(f"{d}: licence not checkable (identity.members / metadata.field_sources dropped)")
        return
    if cfg.rule(cls["anchor"]).rows == "drop":
        bad.append(f"{d}: anchor class {cls['anchor']} has rows: drop")
    if "retired" in cfg.row_drops and row.get("state") == "retired":
        bad.append(f"{d}: retired row shipped")
    if "masked" in cfg.row_drops and ident.get("is_masked"):
        bad.append(f"{d}: masked row shipped")
    if "sensitivity" in cfg.row_drops and meta.get("sensitivity"):
        bad.append(f"{d}: row with sensitivity {meta['sensitivity']} shipped")
    arule = cfg.rule(cls["abstract"])
    if meta.get("abstract") is not None and (arule.abstract == "drop" or arule.rows == "drop"):
        bad.append(f"{d}: abstract from class {cls['abstract']} shipped")
    members = {m["source_id"]: m.get("license_class") for m in ident.get("members") or []}

    def withholds(source_id: str | None) -> bool:
        return cfg.rule(cfg.class_of(members.get(source_id))).withholds

    if content is not None:
        if cfg.rule(cls["text"]).withholds and content.get("current_text_status") != WITHHELD:
            bad.append(f"{d}: text from class {cls['text']} is {content.get('current_text_status')!r}, "
                       f"not withheld")
        if content.get("current_text_status") == WITHHELD:
            leaked = [p for p in WITHHELD_REFS if any(v not in (None, []) for v in _get(row, p))]
            if leaked:
                bad.append(f"{d}: withheld text but references at {leaked}")
        else:
            for snap in content.get("snapshots") or []:
                if withholds(snap["source_id"]) and any(ref_sha256(snap, part) for part in PARTS):
                    bad.append(f"{d}: snapshot {snap.get('snapshot_id')} of a withholding member keeps "
                               f"its blob reference")
    if (content or {}).get("current_text_status") != WITHHELD:
        for e in (row.get("inband") or {}).get("entries") or []:
            if withholds(e.get("source_id")) and any(ref_sha256(e, part) for part in PARTS):
                bad.append(f"{d}: in-band entry of withholding member {e.get('source_id')} keeps its references")
    cap = cfg.rule(cls["text"]).excerpt_cap_chars
    anns = row.get("annotations") or {}
    for wf, spec in REGISTRY.items():
        a = anns.get(wf)
        if cap is None or not a or a.get("payload") is None:
            continue
        for path in spec.text_bearing:
            over = [v for v in text_bearing_values(a["payload"], path) if len(v) > cap]
            if over:
                bad.append(f"{d}: {wf}.payload.{path}: {len(over)} value(s) over the {cls['text']} cap "
                           f"{cap} (longest {max(map(len, over))})")
    if cfg.other == "drop" and anns.get("other"):
        bad.append(f"{d}: other[] shipped under other: drop")


def _check_blob(d: Any, what: str, b: dict[str, Any] | None, want: str | None, missing: set[str],
                bad: list[str]) -> None:
    """One BLOB: ``inline`` set only where ``want`` (the digest selected and placed inline), hashing to
    its ``sha256`` with ``bytes`` its length; a wanted one not inline must be in ``missing-blobs.txt``."""
    v = (b or {}).get("inline")
    if v is not None:
        if want is None:
            bad.append(f"{d}: {what}.inline set, but that element is not selected, eligible and placed inline")
        elif hashlib.sha256(v).hexdigest() != b.get("sha256"):  # type: ignore[union-attr]
            bad.append(f"{d}: {what}.inline does not hash to its sha256")
        elif b.get("bytes") != len(v):  # type: ignore[union-attr]
            bad.append(f"{d}: {what}.bytes {b.get('bytes')} is not the inline length {len(v)}")  # type: ignore[union-attr]
    elif want is not None and want not in missing:
        bad.append(f"{d}: {what}.inline is NULL but selected (not in missing-blobs.txt)")


def _ref_blob(row: dict[str, Any], r: Ref) -> tuple[str, dict[str, Any] | None]:
    """``(label, BLOB)`` of a selected ref's element (``None``: a ``current`` ref has no BLOB)."""
    if r.where == "snapshot":
        snaps = (row.get("content") or {}).get("snapshots") or []
        snap = snaps[r.index] if r.index is not None and r.index < len(snaps) else {}
        return f"snapshot {snap.get('snapshot_id')} {r.part}", snap.get(str(r.part))
    if r.where == "record":
        entries = (row.get("inband") or {}).get("entries") or []
        e = entries[r.index] if r.index is not None and r.index < len(entries) else {}
        return f"inband entry {r.index} {r.part}", e.get(str(r.part))
    return "current", None


def _check_bytes(row: dict[str, Any], cfg: ReleaseConfig, missing: set[str], blob_sizes: dict[str, int],
                 referenced: set[str], tally: dict[str, Any], bad: list[str]) -> None:
    """D647: snapshot pruning, BLOB inline fields, coverage of the selected elements, a blobs-placed BLOB's
    ``bytes``; adds the row's selection to ``tally`` (``selected``, ``to_blobs`` and the counters)."""
    d = row.get("doc_id")
    content = row.get("content") or {}
    snaps = content.get("snapshots") or []
    if cfg.snapshots == "current" and any(s.get("snapshot_id") != content.get("current_snapshot_id")
                                          for s in snaps):
        bad.append(f"{d}: snapshots: current, but {len(snaps)} snapshot(s) not all the current one")
    refs = selected_refs(row, cfg)
    want = {(r.where, r.index, r.part): r.digest for r in refs if r.inline(cfg)}
    for i, snap in enumerate(snaps):
        for part in PARTS:
            _check_blob(d, f"snapshot {snap.get('snapshot_id')} {part}", snap.get(part),
                        want.get(("snapshot", i, part)), missing, bad)
    for i, e in enumerate((row.get("inband") or {}).get("entries") or []):
        for part in PARTS:
            _check_blob(d, f"inband entry {i} {part}", e.get(part), want.get(("record", i, part)), missing, bad)
    tally["elements"] += len(refs)
    tally["records"] += sum(r.slot == "record" for r in refs)
    for r in refs:
        tally["selected"].add(r.digest)
        if r.inline(cfg):
            if r.digest not in missing:
                tally["inline_count"] += 1
                inline = (_ref_blob(row, r)[1] or {}).get("inline")
                tally["inline_bytes"] += len(inline) if inline is not None else 0
            continue
        referenced.add(r.digest)
        if r.digest in blob_sizes:
            tally["to_blobs"].add(r.digest)
            what, b = _ref_blob(row, r)
            if b is not None and b.get("bytes") != blob_sizes[r.digest]:
                bad.append(f"{d}: {what}.bytes {b.get('bytes')} is not the size of blobs/{r.digest} "
                           f"({blob_sizes[r.digest]})")
        elif r.digest not in missing:
            bad.append(f"{d}: selected {r.slot} {r.digest} neither in blobs/ nor in missing-blobs.txt")


def superseded(doc: dict[str, Any]) -> str | None:
    """Why a release with ``release.json`` ``doc`` is superseded (its ``release_format`` is below
    :data:`~stack.fold.release.RELEASE_FORMAT`; absent = 1, before D647), else ``None``. Such a release
    is not validated or upcast: the current format supersedes every older release (D647)."""
    fmt = doc.get("release_format") or 1
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt >= RELEASE_FORMAT:
        return None
    return (f"release_format {fmt} is superseded by release_format {RELEASE_FORMAT} (D647): not validated or "
            "upcast; re-release from a build instead")


def format_problem(doc: dict[str, Any]) -> str | None:
    """Why this code does not validate a release of ``doc``'s ``release_format``: superseded
    (:func:`superseded`), newer than :data:`~stack.fold.release.RELEASE_FORMAT`, or not an integer; else
    ``None``."""
    if (sup := superseded(doc)) is not None:
        return sup
    fmt = doc.get("release_format") or 1
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        return f"release_format {fmt!r} is not an integer"
    if fmt > RELEASE_FORMAT:
        return (f"release_format {fmt} is newer than this code reads (release_format {RELEASE_FORMAT}): not "
                "validated; read it with newer code")
    return None


def _regular(path: Path) -> bool:
    """A regular file that is not a symlink (a bundle's symlink may point anywhere, a FIFO blocks)."""
    return not path.is_symlink() and path.is_file()


def _present(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def _batches(pf: pq.ParquetFile, bad: list[str]) -> Iterator[pa.RecordBatch]:
    """``pf``'s batches; a read failure ends them with a finding."""
    it = pf.iter_batches(batch_size=1024)
    while True:
        try:
            batch = next(it)
        except StopIteration:
            return
        except (OSError, pa.ArrowException) as exc:
            bad.append(f"corpus.parquet unreadable: {type(exc).__name__}: {exc}")
            return
        yield batch


def _missing_list(path: Path, bad: list[str]) -> set[str]:
    """The digests ``missing-blobs.txt`` lists (a line that is not a sha256 hex digest is a finding)."""
    if not _regular(path):
        bad.append(f"missing-blobs.txt: {_NOT_READ}")
        return set()
    try:
        lines = path.read_text("utf-8").splitlines()
    except (OSError, ValueError) as exc:
        bad.append(f"missing-blobs.txt unreadable: {type(exc).__name__}")
        return set()
    out: set[str] = set()
    for ln in (x.strip() for x in lines):
        if not ln:
            continue
        try:
            out.add(blob_key(ln))
        except ValueError:
            bad.append(f"missing-blobs.txt: {ln[:80]!r} is not a sha256 hex digest")
    return out


def _blob_files(bdir: Path, bad: list[str]) -> dict[str, int]:
    """``{name: size}`` of the regular files in ``blobs/``; anything else there is a finding."""
    if not _present(bdir):
        return {}
    if bdir.is_symlink() or not bdir.is_dir():
        bad.append("blobs: not a directory (a symlink or file is not read)")
        return {}
    sizes: dict[str, int] = {}
    try:
        entries = sorted(bdir.iterdir())
    except OSError as exc:
        bad.append(f"blobs/ unreadable: {type(exc).__name__}")
        return {}
    for p in entries:
        if _regular(p):
            sizes[p.name] = p.stat().st_size
        else:
            bad.append(f"blobs/{p.name}: {_NOT_READ}")
    return sizes


def _build_status_problems(doc: dict[str, Any]) -> list[str]:
    """``release.json`` ``build_status`` (D705) against ``build_id`` (module docstring)."""
    bs = doc["build_status"]
    if not isinstance(bs, dict) or bs.get("status") not in BUILD_STATUSES:
        return [f"release.json build_status {bs!r}: an object with status one of {list(BUILD_STATUSES)}"]
    out = []
    if (bs["status"] == "unbuilt") != (doc.get("build_id") == NONE):
        out.append(f"release.json build_status {bs['status']!r} with build_id {doc.get('build_id')!r}")
    if bs["status"] == "incomplete" and not (bs.get("incomplete") or bs.get("stopped")):
        out.append("release.json build_status incomplete names neither incomplete entries nor stopped")
    return out


def validate_release(path: str | Path, *, tag: str | None = None) -> list[str]:
    """The problems of the release directory ``path`` (module docstring, V10; D647); ``[]`` when it is
    valid. ``tag``: the name ``release.json``'s ``tag`` must have (default the directory's name; the
    writer passes the final name when it checks its stage)."""
    d = Path(path)
    rj, cp = d / RELEASE_JSON, d / "corpus.parquet"
    if not _present(rj):
        return [f"{RELEASE_JSON} missing"]
    if not _regular(rj):
        return [f"{RELEASE_JSON}: {_NOT_READ}"]
    try:
        doc = json.loads(rj.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        return [f"release.json unreadable: {type(exc).__name__}: {exc}"]
    if not isinstance(doc, dict):
        return ["release.json is not a JSON object"]
    if (fmt := format_problem(doc)) is not None:  # classified before anything else (D647)
        return [fmt]
    if not _present(cp):
        return ["corpus.parquet missing"]
    if not _regular(cp):
        return [f"corpus.parquet: {_NOT_READ}"]
    try:
        cfg = ReleaseConfig.model_validate(doc["config"])
    except (ValueError, KeyError, TypeError) as exc:
        return [f"release.json unreadable or its config invalid: {exc}"]
    bad: list[str] = []
    missing_keys = [k for k in RELEASE_JSON_KEYS + RELEASE_JSON_KEYS_V2 if k not in doc]
    if missing_keys:
        bad.append(f"release.json lacks {missing_keys}")
    if doc.get("tag") != (tag if tag is not None else d.name):
        bad.append(f"release.json tag {doc.get('tag')!r}")
    if doc.get("config_digest") != config_json_digest(doc["config"]):
        bad.append("config_digest is not the embedded config's")
    if doc.get("filter") != (want_filter := resolve_filter(cfg.filter)):
        bad.append(f"release.json filter {doc.get('filter')!r} is not the config's ({want_filter!r})")
    if "build_status" in doc:
        bad += _build_status_problems(doc)
    try:
        pf = pq.ParquetFile(cp)
        schema = pf.schema_arrow
        nrows = pf.metadata.num_rows
    except (OSError, pa.ArrowException) as exc:
        return bad + [f"corpus.parquet unreadable: {type(exc).__name__}: {exc}"]
    footer = {k.decode(): v.decode() for k, v in (schema.metadata or {}).items()}
    if footer.get("stack.file_id") != doc.get("file_id"):
        bad.append(f"footer file_id {footer.get('stack.file_id')!r} is not release.json's {doc.get('file_id')!r}")
    if footer.get("stack.row_count") != str(nrows) or doc.get("row_count") != nrows:
        bad.append(f"row_count: footer {footer.get('stack.row_count')!r}, release.json {doc.get('row_count')!r}, "
                   f"file {nrows}")
    spec = release_spec(cfg)
    schema = schema.remove_metadata()
    if doc.get("schema_digest") != schema_digest(schema):
        bad.append(f"release.json schema_digest {doc.get('schema_digest')!r} is not the file schema's")
    upcast = not schema.equals(spec.schema, check_metadata=False)
    probe = schema.empty_table()
    if upcast:
        # A release written under an older schema that upcasts additively is checked as its upcast
        # (D43, D208 (4)): the new fields are NULL in it, so no row rule can newly fail.
        try:
            probe = upcast_table(probe, spec.schema)
        except UpcastError:
            upcast = False
    s1 = schema_equal(probe, {}, spec)
    if s1:
        return bad + [i for f in s1 for i in (f.items or (f.message,))]
    missing: set[str] = set()
    mpath = d / "missing-blobs.txt"
    if _present(mpath):
        missing = _missing_list(mpath, bad)
        if cfg.content.missing == "fail" and missing:
            bad.append(f"missing-blobs.txt lists {len(missing)} digest(s) under content.missing: fail")
    bdir = d / "blobs"
    blob_sizes = _blob_files(bdir, bad)
    referenced: set[str] = set()
    tally: dict[str, Any] = {k: 0 for k in CONTENT_COUNTS} | {"selected": set(), "to_blobs": set()}
    checkable = bool(doc.get("filter_checkable"))
    filt = str(doc.get("filter") or "true")
    for batch in _batches(pf, bad):
        tbl = pa.Table.from_batches([batch]).replace_schema_metadata(None)
        if upcast:
            tbl = upcast_table(tbl, spec.schema)
        if checkable:
            try:
                kept = filter_rows(tbl, filt).num_rows
            except FoldError as exc:
                bad.append(f"filter_checkable, but the filter does not evaluate: {exc}")
                checkable = False
            else:
                if kept != tbl.num_rows:
                    bad.append(f"{tbl.num_rows - kept} row(s) do not satisfy the recorded filter")
        for row in iter_rows(tbl, tbl.column_names):
            _check_release_row(row, cfg, bad)
            _check_bytes(row, cfg, missing, blob_sizes, referenced, tally, bad)
    for m in sorted(missing):
        if m not in tally["selected"]:
            bad.append(f"missing-blobs.txt: {m} is not a selected element")
        if m in blob_sizes:
            bad.append(f"missing-blobs.txt: {m} is also in blobs/")
    tally |= {"blobs_count": len(tally["to_blobs"]), "blobs_bytes": sum(blob_sizes[x] for x in tally["to_blobs"]),
              "missing": len(missing)}
    counts = doc.get("content")
    if not isinstance(counts, dict):
        bad.append("release.json content is not an object")
    else:
        bad += [f"content.{k} {counts.get(k)!r} is not the recomputed {tally[k]}" for k in CONTENT_COUNTS
                if counts.get(k) != tally[k]]
    for name in sorted(blob_sizes):
        if name not in referenced:
            bad.append(f"blobs/{name}: not referenced by a selected element")
        got = _file_sha256(bdir / name)
        if got != name:
            bad.append(f"blobs/{name}: hashes to {got}")
    return bad


@register("V10", "V-release", scope="store")
def v_release(view: StoreView) -> list[Finding]:
    """V10 (module docstring): :func:`validate_release` on every release directory; a store without
    ``releases/`` passes. A tag directory without ``release.json`` is an interrupted release, not a
    release (D346): skipped. A superseded release (:func:`superseded`) is a ``warn``, not validated."""
    bad: list[str] = []
    old: list[str] = []
    base = view.root / RELEASES
    for d in sorted(p for p in base.glob("*") if p.is_dir() and not p.name.startswith(".")):
        rj = d / RELEASE_JSON
        if not _present(rj):
            continue
        sup = None  # unreadable or not a regular file: validate_release reports it
        if _regular(rj):
            try:
                sup = superseded(json.loads(rj.read_text("utf-8")))
            except (OSError, ValueError, AttributeError):
                pass
        if sup is not None:
            old.append(f"{d.name}: {sup}")
            continue
        bad += [f"{d.name}: {b}" for b in validate_release(d)]
    return (_finding("V-release", "{n} release violation(s)", bad)
            + _finding("V-release", "{n} superseded release(s), not validated", old, "warn"))


__all__ = ["CONTENT_COUNTS", "RELEASE_JSON_KEYS", "RELEASE_JSON_KEYS_V2", "format_problem", "superseded", "v_fold",
           "v_release", "validate_release"]
