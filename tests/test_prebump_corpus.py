"""R1-2 / R8-2 (round 5, T-RUN B9; D295): a build's ``corpus`` file of an older format version.

``stack release``/``stack fold release`` read it upcast through the one family reader (a note on
stderr), or, when it does not read, refuse it before any run with the operator's next step (never a
``failed[code_bug]`` run with a traceback). (``stack latest`` and its ``format`` line went with
D375.) The older version is the build's own file under a registered next version of ``corpus``
(:func:`tests.test_family_format.future_version`): ``ADDITIVE`` (it reads upcast) or ``Incompatible`` (it does
not read). Since D690 no real corpus version before the current one reads (D693 removed the one-off upcast).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import zstandard

from stack.fold import FoldError, corpus_format, corpus_input
from stack.runs import file_digest, find_run_record, read_run_records
from stack.schemas.families import family, schema_digest
from stack.schemas.upcasters import Incompatible
from stack.store.layout import RELEASES
from stack.store.parquet import read_family, read_header
from tests.fixtures.checks import build_store, fid
from tests.synth.records import build_json_path, rewrite_record, sdir
from tests.synth.rows import AT, mint
from tests.test_family_format import FUTURE_FIELD, drop_schema, future_version, write_raw
from tests.test_fold_build import cli

pytestmark = pytest.mark.slow  # recorded runs over a fixture store


def _built(tmp_path: Path) -> tuple[Path, str, str]:
    """A store with a ``fold.build`` corpus file and an ``ok`` build record (A.7 shape, before
    R1-2: no ``schema_digest`` in ``files.corpus``) naming it; ``(store, file id, build id)``."""
    store = tmp_path / "store"
    build_store(store)
    out = json.loads(cli(store, "fold", "corpus", "--input", f"documents={sdir('documents')}", "--out", sdir("corpus")))
    corpus = out["outputs"]["corpus"]
    out = json.loads(cli(store, "fold", "build", "--input", f"corpus={corpus}", "--join", f"screen={fid(7, 'screen')}",
                         "--out", sdir("corpus.build")))
    built = read_header(store / out["outputs"]["corpus"])
    bid = mint("b", 9)
    build_json_path(store, AT + dt.timedelta(hours=30), bid, "dev-v2").write_bytes(
        zstandard.ZstdCompressor().compress(json.dumps({
            "build_id": bid, "plan": "dev-v2", "status": "ok", "finished_at": (AT + dt.timedelta(hours=30)).isoformat(),
            "version_chain": [], "files": {"corpus": {"file_id": built["file_id"], "path": out["outputs"]["corpus"]}},
        }).encode()))
    return store, built["file_id"], bid


def test_older_corpus_reads_upcast_with_a_note_and_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, built, bid = _built(tmp_path)
    future_version(monkeypatch, "corpus")
    path = next(p for p in (store / "data").rglob("*.parquet") if read_header(p)["file_id"] == built)
    fmt = corpus_format(path)
    assert (fmt.version, fmt.is_current) == (family("corpus").version - 1, False)
    tbl, _h, _ = corpus_input(store, built, fold_config="build")
    assert tbl.schema.equals(family("corpus").schema, check_metadata=False)  # the field the older version lacks
    assert set(tbl.column(FUTURE_FIELD.name).is_null().to_pylist()) == {True}

    out = cli(store, "release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "p1")
    assert f"note: {path.name} is corpus v{fmt.version} (current v{fmt.current}, upcast on read)" in out, out
    doc = json.loads((store / RELEASES / "p1" / "release.json").read_text())
    assert (doc["build_id"], doc["corpus_file_id"]) == (bid, built)
    assert pq.read_metadata(store / RELEASES / "p1" / "corpus.parquet").num_rows == doc["row_count"]


def test_incompatible_corpus_is_refused_before_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corpus behind an ``Incompatible`` step (as every corpus before v6 is, D690): ``stack release`` and
    ``stack fold release`` refuse it with the step's reason, before any run, no traceback."""
    store, built, _ = _built(tmp_path)
    future_version(monkeypatch, "corpus", step=Incompatible("D999: test; re-run stack build"))
    path = next(p for p in (store / "data").rglob("*.parquet") if read_header(p)["file_id"] == built)
    with pytest.raises(FoldError, match="IncompatibleFormat.*D999: test"):
        corpus_format(path)
    runs = len(read_run_records(store))
    for args in (("release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "p0"),
                 ("fold", "release", "--input", f"corpus={built}", "--release", "public-v1", "--tag", "p0")):
        out = cli(store, *args, code=1)
        assert "error: " in out and "D999: test" in out and "Traceback" not in out, out
    assert len(read_run_records(store)) == runs  # refused before any run
    assert not (store / RELEASES / "p0").exists()


def test_unreadable_corpus_is_refused_before_a_run(tmp_path: Path) -> None:
    """A corpus file that does not read (its schema no version of corpus, though it declares the
    current one): ``stack release`` and ``stack fold release`` refuse with the next step and leave
    no ``failed[code_bug]`` run."""
    store, built, _ = _built(tmp_path)
    path = next(p for p in (store / "data").rglob("*.parquet") if read_header(p)["file_id"] == built)
    tbl, h = read_family(path)
    odd = drop_schema(tbl.schema, ["identity"])
    path.unlink()
    # honest about its own schema, but declares the current version
    write_raw(path, tbl.select(odd.names), {**h, "schema_digest": schema_digest(odd)})
    rec = find_run_record(store, h["run_id"])  # its record follows the rewritten file (checked downloads)
    assert rec is not None
    rec.outputs["corpus"] = {**rec.outputs["corpus"], "file_digest": file_digest(path),
                             "schema_digest": schema_digest(odd)}
    rewrite_record(store, rec)
    with pytest.raises(FoldError, match="is not v[0-9]+'s") as exc:
        corpus_format(path)
    assert "stack build" in str(exc.value)
    with pytest.raises(FoldError, match="is not v[0-9]+'s"):
        corpus_input(store, built, fold_config="build")
    runs = len(read_run_records(store))
    for args in (("release", "--in", sdir("corpus.build"), "--config", "public-v1", "--tag", "p2"),
                 ("fold", "release", "--input", f"corpus={built}", "--release", "public-v1", "--tag", "p3")):
        out = cli(store, *args, code=1)
        assert "error: " in out and "SchemaMismatch" in out and "Traceback" not in out, out
    assert len(read_run_records(store)) == runs  # refused before any run: no code_bug record
    assert not (store / RELEASES / "p2").exists()
