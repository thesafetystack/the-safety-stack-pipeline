"""Failing fixtures of V9 ``V-fold`` and V10 ``V-release`` (T5.2) and of the cross-build checks
X2, X3, X5, X9, X11, X12, X18, X21, X25 (T5.1b), merged into :data:`tests.fixtures.checks.FIXTURES`.

Each adds its own files to :func:`tests.fixtures.checks.clean_world` (left unchanged): a
``fold.build`` corpus file (run 20) made by the real fold steps from the view at F5 (filter
``has_text``) joined with the clean ``screen`` file. V9's fixture undoes mapping M on one row
(consumer F1's stale ``ok``); V10's file is clean, and its ``derive`` also writes
``releases/bad/`` from the real release rules with one 400-char quote put back (consumer F2).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa

from stack.fold import WRITER, filter_rows, join
from stack.fold.release import (
    RELEASE_FORMAT,
    Destination,
    apply_rules,
    load_release_config,
    release_schema,
    release_spec,
    write_release_dir,
)
from stack.query import HAS_TEXT
from stack.runs import RunRecord, write_final_record
from tests.fixtures.checks import FileSpec, T, World, fid, view_rows
from tests.synth.rows import _content, dg, rid, sid, table

Row = dict[str, Any]
_BUILD_HEADER: dict[str, Any] = {"stage": "fold.build", "writer": WRITER, "fold_config": "build",
                                 "filter": HAS_TEXT, "annotations_folded": ["screen"]}


def _built_rows(root: Path, screen: list[Row]) -> list[Row]:
    """The ``fold.build`` rows of the clean store: view at F5 → join ``screen`` → ``has_text``."""
    corpus = table("corpus", [r for r in view_rows(root, fid(5)) if r["state"] == "acquired"])
    out, _ = join(corpus, {"screen": table("screen", screen)})
    return filter_rows(out, HAS_TEXT).to_pylist()


def _add_build(w: World, run: int, after: Callable[[Path, list[Row]], None] | None = None) -> FileSpec:
    screen = copy.deepcopy(w.rows("screen"))

    def derive(root: Path) -> list[Row]:
        rows = _built_rows(root, screen)
        if after is not None:
            after(root, rows)
        return rows

    spec = FileSpec("build", "corpus", run, dict(_BUILD_HEADER), derive=derive)
    w.files.append(spec)
    return spec


def v9(w: World) -> None:  # a stale `ok` verdict in a build corpus (consumer F1)
    def stale_ok(rows: list[Row]) -> None:
        s = rows[0]["annotations"]["screen"]
        s["basis_digest"] = dg("an older text")
        s["is_basis_current"] = False  # recorded as stale, yet still `ok`: mapping M not applied

    _add_build(w, 20).edits.append(stale_ok)


def v10(w: World) -> None:  # a 400-char quote in a release file (consumer F2)
    def release(root: Path, rows: list[Row]) -> None:
        cfg = load_release_config("public-v1")
        kept, dropped, _ = apply_rules(rows, cfg)
        kept[0]["annotations"]["screen"]["payload"]["quotes"] = [{"text": "q" * 400, "offset": 0}]
        doc = {"build_id": "none", "tag": "bad", "config_digest": cfg.digest, "config": cfg.as_json(),
               "dropped": dropped, "corpus_file_id": f"{rid(21)}.corpus", "run_id": rid(21),
               "row_count": len(kept), "schema_digest": release_spec(cfg).schema_digest,
               "release_format": RELEASE_FORMAT, "file_id": f"{rid(21)}.release", "audience": cfg.audience,
               "filter": "true", "filter_checkable": False, "source": {}, "rows_in": len(rows),
               "filtered_out": 0, "classes": {}, "content": {}}
        write_release_dir(root, Destination("bad"), pa.Table.from_pylist(kept, schema=release_schema(cfg)), doc)

    _add_build(w, 21, after=release)


# ---------------------------------------------------------------- T5.1b: X2 … X25
_CORPUS_HEADER: dict[str, Any] = {"stage": "fold.corpus", "writer": WRITER, "fold_config": "corpus",
                                  "filter": HAS_TEXT, "annotations_folded": [],
                                  "inputs": {"documents": fid(5)}}


def _has_text_rows(root: Path) -> list[Row]:
    """``fold.corpus`` of the clean store: the view at F5 filtered ``has_text``."""
    return filter_rows(table("corpus", [r for r in view_rows(root, fid(5)) if r["state"] == "acquired"]),
                       HAS_TEXT).to_pylist()


def _add_corpus(w: World, run: int, key: str | None = None) -> FileSpec:
    """A ``fold.corpus`` file (run ``run``) over the view at F5; ``key`` names it in the build."""
    spec = FileSpec(f"corpus{run}", "corpus", run, copy.deepcopy(_CORPUS_HEADER), derive=_has_text_rows)
    w.files.append(spec)
    if key is not None:
        _name(w, key, run, "corpus")
    return spec


def _name(w: World, key: str, run: int, fam: str) -> None:
    assert w.build is not None
    w.build["files"][key] = {"file_id": fid(run, fam)}


def _on_write(w: World, fn: Callable[[Path], None]) -> None:
    """Run ``fn(root)`` while the store is written (wraps the last derived file's ``derive``):
    records with no files, which :class:`World` cannot describe."""
    spec = next(f for f in reversed(w.files) if f.derive is not None)
    derive = spec.derive
    assert derive is not None

    def wrapped(root: Path) -> list[Row]:
        fn(root)
        return derive(root)

    spec.derive = wrapped


def _record(root: Path, run: int, stage: str, **kw: Any) -> None:
    write_final_record(root, RunRecord(
        run_id=rid(run), stage=stage, writer=stage, plan="dev-v2", started_at=T(run), heartbeat_at=T(run),
        finished_at=T(run), code_digest=dg("code"), config_digest=dg("config"), **kw))


def x2(w: World) -> None:  # fold.build on another fold.corpus file than the build's corpus_input
    _add_corpus(w, 20, "corpus_input")
    _add_corpus(w, 21)
    screen = copy.deepcopy(w.rows("screen"))

    def built(root: Path) -> list[Row]:
        out, _ = join(table("corpus", _has_text_rows(root)), {"screen": table("screen", screen)})
        return filter_rows(out, HAS_TEXT).to_pylist()

    w.files.append(FileSpec("build", "corpus", 22, {
        **_BUILD_HEADER, "inputs": {"corpus": fid(21, "corpus"), "screen": fid(7, "screen")}}, derive=built))
    _name(w, "corpus", 22, "corpus")


def x3(w: World) -> None:  # require_complete: the build's last acquire run ended scope_complete = false
    assert w.build is not None
    w.build.update(runs=[rid(23)], require_complete=["documents"])
    _on_write(w, lambda root: _record(root, 23, "acquire", status="ok", scope_complete=False))


def x5(w: World) -> None:  # text under another config (doc 0's current snapshot)
    w.doc("F3", 0)["content"]["snapshots"][0]["convert_pin"] = dg("another convert config")


def x9(w: World) -> None:  # a dropped row: the build's fold.corpus file lacks one has_text row
    def drop(rows: list[Row]) -> None:
        del rows[0]

    _add_corpus(w, 20, "corpus_input").edits.append(drop)


def x11(w: World) -> None:  # one inflight key on two crashed acquire runs
    def crashed(root: Path) -> None:
        for run in (24, 25):
            _record(root, run, "acquire", status="failed", error_class="crashed",
                    inflight=[f"{sid(2)}:full_text"])

    _on_write(w, crashed)


def x12(w: World) -> None:  # a retired row in a corpus filtered has_text (not in the build)
    spec = _add_corpus(w, 20)
    spec.derive = lambda root: [*_has_text_rows(root),
                                next(r for r in view_rows(root, fid(5)) if r["doc_id"] == sid(5))]


def x18(w: World) -> None:  # merged_into dangling
    w.doc("F1", 5)["identity"]["merged_into"] = sid(9)


def x21(w: World) -> None:  # survivor (doc 4) missing the retired doc 5's snapshots
    d5 = w.doc("F1", 5)
    d5["content"] = _content(5, 1)
    d5["content"]["members_digest"] = d5["identity"]["members_digest"]


def x25(w: World) -> None:  # doc 1 holds doc 0's raw bytes; neither lists the other
    w.doc("F3", 1)["content"]["snapshots"][0]["raw"]["sha256"] = dg(("raw", 0))


#: Merged into ``tests.fixtures.checks.FIXTURES``.
FIXTURES_D: dict[str, Callable[[World], None]] = {
    "V-fold": v9,
    "V-release": v10,
    "lineage_equality": x2,
    "expected_scope": x3,
    "blob_resolution": x5,
    "fold_recompute": x9,
    "dead_runs_seen": x11,
    "state_propagation": x12,
    "tombstone_closure": x18,
    "carry_regrouped": x21,
    "same_raw_reported": x25,
}
