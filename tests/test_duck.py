"""``stack/duck.py`` (D732): every duckdb connection of the pipeline spills to the scratch directory, never to
duckdb's default ``.tmp`` under the working directory (the 2026-10-27 container export failure). A spilling query
in a read-only working directory runs on :func:`stack.duck.connect` and fails, as in production, on a plain
``duckdb.connect()``; the spill directory is per connection, under ``$STACK_TMP``, and gone after close; the
sandboxed connection keeps both properties; no module under ``stack/``, ``tools/`` or ``workflows/`` opens a
duckdb connection another way."""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from stack import duck
from stack.query import sql_connection

ROOT = Path(__file__).resolve().parents[1]

#: An ORDER BY over ~60 MB of strings under a 32 MB limit: the sort spills (measured: ~0.3 s).
SPILL_LIMIT = "SET memory_limit = '32MB'; SET threads = 1"
SPILL_SQL = ("SELECT count(*) FROM (SELECT x::VARCHAR || 'abcdefghijklmnopqrstuvwxyz' AS s "
             "FROM range(1500000) t(x) ORDER BY s DESC)")

#: duckdb's module-level functions that run on its default connection (default ``temp_directory`` too).
DEFAULT_CONNECTION = {"connect", "default_connection", "sql", "query", "execute", "executemany", "table", "view",
                      "values", "arrow", "df", "query_df", "from_arrow", "from_df", "from_query", "from_parquet",
                      "from_csv_auto", "read_parquet", "read_csv", "read_json"}


@pytest.fixture
def read_only_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The process's working directory is not writable (the image's /app); ``$STACK_TMP`` is a fresh directory."""
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    cwd = tmp_path / "app"
    cwd.mkdir()
    cwd.chmod(0o555)
    monkeypatch.chdir(cwd)
    scratch = tmp_path / "scratch"
    monkeypatch.setenv(duck.TMP_ENV, str(scratch))
    yield scratch
    cwd.chmod(0o755)


def _spill(con: duckdb.DuckDBPyConnection) -> int:
    con.execute(SPILL_LIMIT)
    row = con.execute(SPILL_SQL).fetchone()
    assert row is not None
    return int(row[0])


def test_a_plain_connection_cannot_spill_in_a_read_only_cwd(read_only_cwd: Path) -> None:
    """The incident: the failure this module exists for, reproduced (so the next test is not vacuous)."""
    con = duckdb.connect()
    with pytest.raises(duckdb.IOException, match=r'Failed to create directory "\.tmp": Permission denied'):
        _spill(con)


def test_connect_spills_under_stack_tmp_and_cleans_up(read_only_cwd: Path) -> None:
    con = duck.connect()
    tmp = Path(con.execute("SELECT current_setting('temp_directory')").fetchone()[0])  # type: ignore[index]
    assert tmp.parent == read_only_cwd and tmp.name.startswith(f"duckdb-{os.getpid()}-")
    assert not tmp.exists()  # named, not created: a connection that never spills leaves nothing
    assert _spill(con) == 1_500_000
    assert tmp.is_dir()
    other = duck.connect()
    assert other.execute("SELECT current_setting('temp_directory')").fetchone()[0] != str(tmp)  # type: ignore[index]
    other.close()
    con.close()
    assert not tmp.exists() and list(read_only_cwd.iterdir()) == []


def test_sandboxed_connection_spills_and_stays_sandboxed(read_only_cwd: Path, tmp_path: Path) -> None:
    """``external_access=False`` (SET after the connection opens, module docstring) still spills to the scratch
    directory and still refuses file access; :func:`stack.query.sql_connection` is that connection, locked."""
    con = duck.connect(external_access=False)
    assert _spill(con) == 1_500_000
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"COPY (SELECT 1) TO '{tmp_path / 'x.csv'}'")
    with pytest.raises(duckdb.PermissionException):
        con.execute("SET temp_directory = '.tmp'")
    con.close()
    sql = sql_connection()
    tmp = Path(sql.execute("SELECT current_setting('temp_directory')").fetchone()[0])  # type: ignore[index]
    assert tmp.parent == read_only_cwd
    for stmt in (f"COPY (SELECT 1) TO '{tmp_path / 'x.csv'}'", "SET temp_directory = '.tmp'",
                 "SET enable_external_access = true"):
        with pytest.raises(duckdb.Error):
            sql.execute(stmt)
    sql.close()
    assert not (tmp_path / "x.csv").exists()


def test_temp_base_defaults_to_tmpdir_and_refuses_bad_bases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    monkeypatch.delenv(duck.TMP_ENV, raising=False)
    assert duck.temp_base() == Path(tempfile.gettempdir())
    monkeypatch.setenv(duck.TMP_ENV, "scratch")
    with pytest.raises(ValueError, match="absolute"):
        duck.connect()
    monkeypatch.setenv(duck.TMP_ENV, str(tmp_path / "new" / "base"))
    duck.connect().close()
    assert (tmp_path / "new" / "base").is_dir()  # created when absent
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o555)
        monkeypatch.setenv(duck.TMP_ENV, str(locked))
        with pytest.raises(PermissionError, match="not writable"):
            duck.connect()


def _default_connection_uses(path: Path) -> list[str]:
    """``duckdb.<f>(`` calls and ``from duckdb import <f>`` of :data:`DEFAULT_CONNECTION` in one file."""
    tree = ast.parse(path.read_text("utf-8"), str(path))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "duckdb"
                and node.attr in DEFAULT_CONNECTION):
            hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: duckdb.{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == "duckdb":
            hits += [f"{path.relative_to(ROOT)}:{node.lineno}: from duckdb import {a.name}"
                     for a in node.names if a.name in DEFAULT_CONNECTION or a.name == "*"]
    return hits


def test_no_module_opens_duckdb_but_through_stack_duck() -> None:
    files = sorted(p for top in ("stack", "tools", "workflows") for p in (ROOT / top).rglob("*.py"))
    assert len(files) > 100
    hits = [h for p in files if p != ROOT / "stack" / "duck.py" for h in _default_connection_uses(p)]
    assert hits == [], "open duckdb connections with stack.duck.connect (D732):\n" + "\n".join(hits)
    own = _default_connection_uses(ROOT / "stack" / "duck.py")
    assert [h.split(": ", 1)[1] for h in own] == ["duckdb.connect"]  # the one place
