"""The fold (T1.5): B3's six rows through ``fold.corpus`` under the three named filters, the
fold header and chain of one, ``excluded_reason``, the ``acquire_pending``/``acquire_failed``
macros, and ``fold.compact`` of a four-file chain (V12: compact = union, stamps included).

B3's rows are ported from the v4 design mock ``v4f-B3-fold-annotations`` onto the real
``documents`` schema (mock state ``gathered`` = ``identified``; the mock's row ``status =
'skipped'`` for a masked row = ``identity.is_masked``):

====  ==========  ==================  =======  =======
row   state       current_text        triage   masked
====  ==========  ==================  =======  =======
doc1  acquired    ok (md)             fetch
doc2  acquired    ok (md)             fetch
doc3  acquired    convert_failed      fetch
doc4  triaged     (no content)        skip
doc5  identified  (no content)        (none)
doc6  acquired    ok (md)             fetch    yes
====  ==========  ==================  =======  =======
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pytest
from pydantic import ValidationError

from stack.fold import FoldError, run_fold, union
from stack.fold.configs import load_fold_plan
from stack.query import EVERYTHING, HAS_TEXT, PASSED_TRIAGE, register_macros
from stack.store.chain import HeaderFields, HeadMovedError, Published, resolve
from stack.store.parquet import read_family
from tests.synth.records import record_hook, sdir, write_series
from tests.synth.rows import AT, _ann, dg, document_row, rid, sid, table

UTC = dt.UTC
CHAIN = sdir("documents")  # data/documents/main/
CORPUS = sdir("corpus")  # fold.corpus's destination (D374)
GATHER_BLOCKS = ["identity", "metadata", "inband"]


# ---------------------------------------------------------------- B3's rows on the real schema
_INDEX: dict[str, int] = {sid(i): i for i in range(32)}


def name(doc_id: str) -> str:
    """``doc<i>`` (B3's name) for ``sid(i)``."""
    return f"doc{_INDEX[doc_id]}"


def b3_row(i: int, state: str, *, text: str | None = None, triage: str | None = "fetch",
           masked: bool = False, run: int = 1, **over: Any) -> dict[str, Any]:
    """A valid ``documents`` row from the synth factory with B3's fields; digests agree as §2.5
    requires (triage basis = ``fields_digest``, ``content.members_digest`` = identity's)."""
    base_state = "acquired" if text else ("triaged" if state in ("triaged", "retired") else state)
    row = document_row(i, run, state=base_state, **over)
    row["state"] = state
    ann = row["annotations"]
    if triage is None:
        ann["triage"] = None
    elif ann["triage"] is not None:
        ann["triage"]["basis_digest"] = row["metadata"]["fields_digest"]
        if triage != "fetch":
            ann["triage"]["payload"].update(decision=triage, wanted_by=[])
    c = row["content"]
    if c is not None and text != "ok":
        c.update(current_text_sha256=None, current_text_status=text, current_text_media=None,
                 current_text_quality=None)
        snap = c["snapshots"][0]
        if text == "convert_failed":  # the raw is there, conversion failed
            snap.update(text=None, text_status="failed_permanent", text_error_class="parse_error")
        elif text == "convert_pending":
            snap.update(text=None, text_status=None, text_error_class=None)
        elif text == "no_raw":
            c.update(snapshots=[], current_snapshot_id=None, current_source_id=None,
                     current_role=None, current_revision=None)
            c["roles"][0].update(snapshot_id=None)
    if masked:
        row["identity"].update(is_masked=True, mask_reason="operator")
    return row


def b3_rows() -> list[dict[str, Any]]:
    return [
        b3_row(1, "acquired", text="ok"),
        b3_row(2, "acquired", text="ok"),
        b3_row(3, "acquired", text="convert_failed"),
        b3_row(4, "triaged", triage="skip"),
        b3_row(5, "identified", triage=None),
        b3_row(6, "acquired", text="ok", masked=True),
    ]


def _retire(row: dict[str, Any]) -> dict[str, Any]:
    row["identity"]["merged_into"] = sid(1)
    return row


def role_failed(row: dict[str, Any], status: str, error_class: str,
                retry_after: dt.datetime | None = None) -> dict[str, Any]:
    row["content"]["roles"][0].update(status=status, error_class=error_class, error=error_class,
                                      last_outcome=status, retry_after=retry_after)
    return row


def acquisition_rows() -> list[dict[str, Any]]:
    """Six more rows for the derived macros: convert pending, a transient failure, a permanent
    failure, a permanent failure with its 90-day recheck due later (``retry_after``; R4-2: a
    recheck, not pending work), a tombstone carrying
    text and a ``fetch`` triage (X12: never in ``has_text``/``passed_triage``), and an ``ok``
    row whose ``ok`` role has a future recheck (D63: not pending)."""
    return [
        b3_row(7, "acquired", text="convert_pending"),
        role_failed(b3_row(8, "acquired", text="no_raw"), "failed_transient", "http_5xx"),
        role_failed(b3_row(9, "acquired", text="no_raw"), "failed_permanent", "paywalled"),
        role_failed(b3_row(10, "acquired", text="no_raw"), "failed_permanent", "dead",
                    retry_after=AT + dt.timedelta(days=30)),
        _retire(b3_row(11, "retired", text="ok")),
        recheck_due(b3_row(12, "acquired", text="ok")),
    ]


def recheck_due(row: dict[str, Any]) -> dict[str, Any]:
    """An ``ok`` role with a weekly recheck scheduled (D22): finished, never pending (D63)."""
    row["content"]["roles"][0]["retry_after"] = AT + dt.timedelta(days=7)
    return row


def gather_fields(run: int) -> HeaderFields:
    return HeaderFields(stage="gather", run_id=rid(run), config_digest=dg(("config", "gather")),
                        code_digest=dg("code"), plan="dev-v2", blocks_written=GATHER_BLOCKS)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


def put_documents(store: Path, rows: list[dict[str, Any]], run: int = 1) -> Published:
    """A ``documents`` series directory with one full file; returns that file."""
    return write_series(store, "documents", "documents", table("documents", rows), gather_fields(run),
                        finished_at=AT, on_published=record_hook(store))


def doc_ids(path: Path) -> list[str]:
    return [name(d) for d in read_family(path)[0].column("doc_id").to_pylist()]


# ---------------------------------------------------------------- (1) the three named filters
@pytest.mark.parametrize(("filt", "sql", "want"), [
    ("has_text", HAS_TEXT, ["doc1", "doc2"]),
    ("passed_triage", PASSED_TRIAGE, ["doc1", "doc2", "doc3"]),
    ("everything", EVERYTHING, ["doc1", "doc2", "doc3", "doc4", "doc5", "doc6"]),
])
def test_corpus_named_filters(store: Path, filt: str, sql: str, want: list[str]) -> None:
    head = put_documents(store, b3_rows()).file_id
    res = run_fold(store, "corpus", run_id=rid(20), plan_name="dev-v2",
                   inputs={"documents": CHAIN}, filter=filt, out=CORPUS,
                   finished_at=AT + dt.timedelta(hours=1), on_published=record_hook(store))
    got = doc_ids(res.path)
    print(f"fold.corpus filter={filt}: {len(got)} of 6 -> {got}")
    assert got == want
    assert (res.rows_in, res.rows_out) == (6, len(want))

    out, h = read_family(res.path)
    # Rows are the union's rows, byte for byte (stamps included).
    view = union(store, CHAIN)
    keep = pa.array([d in set(out.column("doc_id").to_pylist()) for d in view.column("doc_id").to_pylist()])
    assert out.equals(view.filter(keep))
    # The fold header (A.6, D14, D15): a full without a base, the first file of corpus/main/.
    assert {k: h[k] for k in ("stage", "writer", "fold_config", "filter", "annotations_folded",
                              "release_config", "blocks_written", "kind", "base", "after",
                              "chain_id", "series", "family", "plan")} == {
        "stage": "fold.corpus", "writer": "fold", "fold_config": "corpus", "filter": sql,
        "annotations_folded": "[]", "release_config": "none", "blocks_written": "*",
        "kind": "full", "base": "", "after": "", "chain_id": "corpus", "series": "main",
        "family": "corpus", "plan": "dev-v2"}
    assert h["inputs"] == f'{{"documents": "{head}"}}'
    assert res.fold == {"config": "corpus", "filter": sql, "joined": {}, "annotations_folded": [],
                        "release_config": "none"}
    assert resolve(store, CORPUS).version_chain == (res.file_id,)
    assert resolve(store, CHAIN).version_chain == (head,)  # the input chain is untouched


@pytest.mark.slow
def test_corpus_default_filter_sql_predicate_and_pinned_file(store: Path) -> None:
    put = put_documents(store, b3_rows())
    head, head_key = put.file_id, put.as_output()["path"]
    # Default filter is has_text; the input may be an exact file (`--input documents=<path>`).
    res = run_fold(store, "corpus", run_id=rid(21), plan_name="dev-v2", inputs={"documents": head_key},
                   out=CORPUS, on_published=record_hook(store))
    assert doc_ids(res.path) == ["doc1", "doc2"]
    assert read_family(res.path)[1]["filter"] == HAS_TEXT
    # Any SQL predicate over the row, macros included; fold.corpus never reads corpus/main/, so a
    # second base-less full into it needs no restart (D373, D380).
    res = run_fold(store, "corpus", run_id=rid(22), plan_name="dev-v2", inputs={"documents": CHAIN}, out=CORPUS,
                   filter="excluded_reason(d) IN ('state:identified', 'triage:skip') OR metadata.title = 'Paper 6'", on_published=record_hook(store))
    assert doc_ids(res.path) == ["doc4", "doc5", "doc6"]
    with pytest.raises(FoldError, match="does not evaluate"):
        run_fold(store, "corpus", run_id=rid(23), plan_name="dev-v2", inputs={"documents": CHAIN}, out=CORPUS,
                 filter="has_txt", on_published=record_hook(store))  # a typo is SQL, and fails loudly
    with pytest.raises(FoldError, match="name the destination directory"):
        run_fold(store, "corpus", run_id=rid(23), plan_name="dev-v2", inputs={"documents": CHAIN},
                 on_published=record_hook(store))
    # The directory moves on: an exact older file still folds that file's version (`docs/OPERATE.md`,
    # "Runs stand alone": reproducing an old state); compacting it into its own directory is refused
    # (not the last file).
    write_series(store, "documents", "documents", table("documents", [b3_row(1, "acquired", text="ok", run=2)]),
                 HeaderFields(stage="acquire", run_id=rid(2), config_digest=dg("c"), code_digest=dg("code"),
                              blocks_written=["content"]), kind="part", on_published=record_hook(store))
    old = run_fold(store, "corpus", run_id=rid(24), plan_name="dev-v2", inputs={"documents": head_key},
                   out=CORPUS, on_published=record_hook(store))
    assert old.inputs == {"documents": head} and doc_ids(old.path) == ["doc1", "doc2"]
    with pytest.raises(HeadMovedError):
        run_fold(store, "compact", run_id=rid(25), plan_name="dev-v2", inputs={"documents": head_key},
                 on_published=record_hook(store))


# ---------------------------------------------------------------- (2) excluded_reason
def test_excluded_reason_over_everything(store: Path) -> None:
    put_documents(store, b3_rows())
    res = run_fold(store, "corpus", run_id=rid(20), plan_name="dev-v2", inputs={"documents": CHAIN},
                   filter="everything", out=CORPUS, on_published=record_hook(store))
    con = register_macros(duckdb.connect())
    got = {name(d): (r, ht) for d, r, ht in con.execute(
        "SELECT doc_id, excluded_reason(d), has_text(d) FROM read_parquet($p) d ORDER BY doc_id",
        {"p": str(res.path)}).fetchall()}
    print("excluded_reason:", {k: v[0] for k, v in got.items()})
    assert {k: v[0] for k, v in got.items()} == {
        "doc1": None, "doc2": None, "doc3": "text:convert_failed", "doc4": "triage:skip",
        "doc5": "state:identified", "doc6": "masked:operator"}
    assert all((reason is None) == has_text for reason, has_text in got.values())


def inband_skip_row(i: int) -> dict[str, Any]:
    """The dev store's ``s7qjyj079w0s4`` shape (T5.5): a forum post acquired from its in-band
    body, text ``ok``, whose triage is ``ok``/``skip`` because its question was
    ``skipped[not_needed]`` (primary role already ``ok``): ``has_text`` keeps it."""
    row = b3_row(i, "acquired", text="ok", triage="skip")
    row["annotations"]["triage"]["payload"]["questions"][0].update(
        status="skipped", error_class="not_needed", verdict=None, confidence=None, evidence_id=None)
    return row


def test_excluded_reason_null_exactly_for_has_text_and_triage_token_matches_passed_triage(
        store: Path) -> None:
    """D64 (NULL exactly when ``has_text``) and D230: neither a ``skip`` triage nor a failed
    screen excludes a row ``has_text`` keeps; a ``triage:`` token names exactly the not yet
    acquired rows that ``passed_triage`` rejects."""
    failed_screen = b3_row(14, "acquired", text="ok")
    failed_screen["annotations"]["screen"] = {
        **_ann("screen", None, basis_kind="text"), "status": "failed_transient", "error_class": "http_5xx"}
    failed_triage = b3_row(15, "triaged")
    failed_triage["annotations"]["triage"].update(status="failed_transient", error_class="http_5xx")
    rows = [*b3_rows(), inband_skip_row(13), failed_screen, failed_triage,
            b3_row(16, "triaged"),                      # triage fetch, not yet acquired
            _retire(b3_row(11, "retired", text="ok"))]
    put_documents(store, rows)
    res = run_fold(store, "corpus", run_id=rid(20), plan_name="dev-v2", inputs={"documents": CHAIN},
                   filter="everything", out=CORPUS, on_published=record_hook(store))
    con = register_macros(duckdb.connect())
    got = {name(d): (r, ht, pt, judged) for d, r, ht, pt, judged in con.execute(
        "SELECT doc_id, excluded_reason(d), has_text(d), passed_triage(d),"
        " state NOT IN ('acquired', 'retired') AND NOT identity.is_masked AND annotations.triage IS NOT NULL"
        " FROM read_parquet($p) d ORDER BY doc_id", {"p": str(res.path)}).fetchall()}
    print("excluded_reason:", {k: v[0] for k, v in got.items()})
    assert {k: got[k][0] for k in ("doc11", "doc13", "doc14", "doc15", "doc16")} == {
        "doc11": "state:retired", "doc13": None, "doc14": None,
        "doc15": "triage:failed_transient", "doc16": "state:triaged"}
    assert got["doc13"][1:3] == (True, False)  # kept by has_text although triage said skip
    assert all((reason is None) == has_text for reason, has_text, _, _ in got.values())
    # Unmasked, live, not yet acquired, triaged: a triage token exactly when passed_triage is false.
    assert sorted(k for k, v in got.items() if v[3]) == ["doc15", "doc16", "doc4"]
    for k, (reason, _, passed, judged) in got.items():
        is_triage = (reason or "").startswith("triage:")
        assert (is_triage == (not passed)) if judged else not is_triage, k


# ---------------------------------------------------------------- (3) acquire_pending / acquire_failed
def test_acquire_macros(store: Path) -> None:
    put_documents(store, b3_rows() + acquisition_rows())
    con = register_macros(duckdb.connect())
    con.register("documents", union(store, CHAIN))

    def which(macro: str, as_of: dt.datetime | None = None) -> list[str]:
        arg = ", as_of := $t" if as_of else ""
        q = f"SELECT doc_id FROM documents d WHERE {macro}(d{arg}) ORDER BY doc_id"
        return [name(r[0]) for r in con.execute(q, {"t": as_of} if as_of else {}).fetchall()]

    six = {f"doc{i}" for i in range(1, 7)}
    pending, failed = which("acquire_pending", AT), which("acquire_failed", AT)
    print(f"as_of {AT:%Y-%m-%d}: pending {pending}, failed {failed}")
    assert [d for d in pending if d in six] == []
    assert [d for d in failed if d in six] == ["doc3"]  # convert_failed, its role is done
    # doc10: failed_permanent[dead] whose long recheck (R4-2) is due later is failed, not pending
    # (amends D63 the way D63 treats an ok role's recheck); before R4-2 it was pending.
    assert pending == ["doc7", "doc8"]  # convert_pending, failed_transient
    assert failed == ["doc3", "doc9", "doc10"]
    later = AT + dt.timedelta(days=60)  # doc10's recheck date has passed: same buckets
    assert which("acquire_pending", later) == ["doc7", "doc8"]
    assert which("acquire_failed", later) == ["doc3", "doc9", "doc10"]
    assert which("acquire_pending") == which("acquire_pending", dt.datetime.now(UTC))  # default: now
    # X12: the tombstone carries text and a fetch triage but passes neither filter.
    assert which("has_text") == ["doc1", "doc2", "doc12"]
    assert which("passed_triage") == ["doc1", "doc2", "doc3", "doc7", "doc8", "doc9", "doc10", "doc12"]
    reason = con.execute("SELECT excluded_reason(d) FROM documents d WHERE doc_id = $i",
                         {"i": sid(11)}).fetchone()
    assert reason == ("state:retired",)


# ---------------------------------------------------------------- (4) compaction (V12)
@pytest.mark.slow
def test_compact_equals_union(store: Path) -> None:
    t = [AT + dt.timedelta(hours=h) for h in range(5)]

    def stamped(row: dict[str, Any], run: int, at: dt.datetime) -> dict[str, Any]:
        row["run_id"], row["created_at"], row["updated_at"] = rid(run), AT, at
        return row

    # S0 gather full (8 rows, one a tombstone), S1 triage part, S2 acquire part, S3 acquire retry.
    s0 = [stamped(b3_row(i, "identified", triage=None, run=1), 1, t[0]) for i in range(7)]
    s0.append(stamped(_retire(b3_row(7, "retired", triage=None, run=1)), 1, t[0]))
    put_documents(store, s0, run=1)
    parts = [
        ("triage", 2, [stamped(b3_row(i, "triaged", run=2), 2, t[1]) for i in range(5)], ["annotations"]),
        ("acquire", 3, [stamped(b3_row(i, "acquired", text="ok", run=3), 3, t[2]) for i in range(3)]
         + [stamped(role_failed(b3_row(3, "acquired", text="no_raw", run=3), "failed_transient",
                                "timeout"), 3, t[2])], ["content"]),
        ("acquire", 4, [stamped(b3_row(3, "acquired", text="ok", run=4), 4, t[3])], ["content"]),
    ]
    for stage, run, rows, blocks in parts:
        write_series(store, "documents", "documents", table("documents", rows),
                     HeaderFields(stage=stage, run_id=rid(run), config_digest=dg((stage, run)),
                                  code_digest=dg("code"), blocks_written=blocks),
                     kind="part", finished_at=t[run - 1], on_published=record_hook(store))
    before = resolve(store, CHAIN)
    assert len(before.files) == 4
    view = union(store, CHAIN)

    res = run_fold(store, "compact", run_id=rid(20), plan_name="dev-v2", inputs={"documents": CHAIN},
                   finished_at=t[4], on_published=record_hook(store))
    got, h = read_family(res.path)
    assert got.equals(view)  # every column, row and block stamps included
    print("compact run_id column:", dict(Counter(got.column("run_id").to_pylist())))
    assert Counter(got.column("run_id").to_pylist()) == {rid(1): 3, rid(2): 1, rid(3): 3, rid(4): 1}
    assert rid(20) not in got.column("run_id").to_pylist()
    assert sid(7) in got.column("doc_id").to_pylist()  # the tombstone survives
    # compaction is an ordinary full on the head of its own directory (D373)
    assert {k: h[k] for k in ("stage", "writer", "run_id", "fold_config", "filter", "annotations_folded",
                              "release_config", "blocks_written", "kind", "base", "after",
                              "chain_id", "series", "family")} == {
        "stage": "fold.compact", "writer": "fold", "run_id": rid(20), "fold_config": "compact",
        "filter": "none", "annotations_folded": "[]", "release_config": "none", "blocks_written": "*",
        "kind": "full", "base": before.head_name, "after": before.head_name, "chain_id": "documents",
        "series": "main", "family": "documents"}
    assert res.path.parent == store / "data" / "documents" / "main"
    after = resolve(store, CHAIN)
    assert after.version_chain == (res.file_id,)
    assert union(store, CHAIN).equals(view)
    s5 = write_series(store, "documents", "documents", table("documents", [stamped(b3_row(5, "triaged", run=5), 5, t[4])]),
                      HeaderFields(stage="triage", run_id=rid(5), config_digest=dg("c"), code_digest=dg("code"),
                                   blocks_written=["annotations"]), kind="part", on_published=record_hook(store))
    assert resolve(store, CHAIN).version_chain == (res.file_id, s5.file_id)
    with pytest.raises(FoldError, match="no filter step"):
        run_fold(store, "compact", run_id=rid(21), plan_name="dev-v2", inputs={"documents": CHAIN},
                 filter="has_text", on_published=record_hook(store))


# ---------------------------------------------------------------- (5) configs and M5 stubs
def test_configs_and_m5_stubs(store: Path) -> None:
    put_documents(store, b3_rows())
    kw: dict[str, Any] = {"run_id": rid(30), "plan_name": "dev-v2"}
    # build and release are implemented since M5 (tests/test_fold_build.py); refusals before reading
    with pytest.raises(FoldError, match="no ok run record names that file id"):
        run_fold(store, "build", inputs={"corpus": f"{rid(20)}.corpus"}, **kw, on_published=record_hook(store))
    with pytest.raises(FoldError, match="run_release"):
        run_fold(store, "release", inputs={"corpus": f"{rid(20)}.corpus"}, **kw, on_published=record_hook(store))
    with pytest.raises(FoldError, match="joins no"):
        run_fold(store, "corpus", inputs={"documents": CHAIN}, joins={"screen": "x.parquet"}, **kw, on_published=record_hook(store))
    with pytest.raises(FoldError, match="takes inputs"):
        run_fold(store, "corpus", inputs={"corpus": "x"}, **kw, on_published=record_hook(store))
    plan = load_fold_plan({"corpus": {"filter": "passed_triage"},
                           "build": {"filter": "has_text", "annotations_folded": ["screen", "affiliation"]},
                           "release": {"config": "configs/releases/public-v1.yaml"}, "diagnostic": True})
    assert plan.filter_for("corpus") == "passed_triage" and plan.filter_for("compact") is None
    assert load_fold_plan(None).corpus.filter == "has_text"
    for bad in ({"corpus": {"filtre": "x"}}, {"build": {"annotations_folded": ["triage"]}},
                {"corpus": {"filter": " "}}, {"compact": {}}):
        with pytest.raises(ValidationError):
            load_fold_plan(bad)
