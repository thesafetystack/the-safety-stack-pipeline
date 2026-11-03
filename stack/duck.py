"""The one way the pipeline opens a duckdb connection (D732): :func:`connect`.

duckdb's default ``temp_directory`` for an in-memory database is ``.tmp`` relative to the process's working
directory, created the first time an operator spills past ``memory_limit``. In the image (``Dockerfile``) the
working directory is ``/app``, owned by root, and the cycle runs as ``stack``: the production test cycle of
2026-10-27 built ``ok``, then ``stack export --config dataset-public`` failed in
``observed_values`` with ``IOException: IO Error: Failed to create directory ".tmp": Permission denied``.
Local runs never hit it: a checkout is writable, and the directory is created only on a spill and removed on
close.

:func:`connect` sets ``temp_directory`` to a directory of its own under :func:`temp_base`: ``$STACK_TMP``, else
``$TMPDIR`` (``/work/scratch`` in the image), else :func:`tempfile.gettempdir`. ``$TMPDIR`` is read directly, not
through :func:`tempfile.gettempdir`, which skips a ``$TMPDIR`` that is missing or not writable and silently returns
``/tmp``, a small tmpfs on some machines (a local export of 2026-11-03, with neither variable set, ran out of temp
space on a 2 GiB ``/tmp``; D806). The directory is named, not created: duckdb creates it on the first spill and removes it when the connection
closes, so connections (in one process or several) never share one and a run that does not spill leaves
nothing. The base must be an absolute, writable directory, checked at connect time: a bad ``STACK_TMP`` fails
when the connection opens, not when a query first spills. Memory and thread limits stay with the call sites
(a ``SET memory_limit`` where one was set before; none added).

``external_access=False`` is the sandbox of :func:`stack.query.sql_connection`: ``enable_external_access``
cannot be passed in the same ``config`` as ``temp_directory`` (duckdb then refuses the ``temp_directory``
option), so it is SET right after the connection opens, before any supplied SQL runs.

``tests/test_duck.py`` refuses any other ``duckdb.connect`` (or duckdb's module-level default connection:
``duckdb.sql``, ``duckdb.execute``, ...) under ``stack/``, ``tools/`` and ``workflows/``.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import duckdb

TMP_ENV = "STACK_TMP"


def temp_base() -> Path:
    """``$STACK_TMP``, else ``$TMPDIR``, else :func:`tempfile.gettempdir`; created when absent. Refuses a relative
    base (it would be relative to the working directory again) and a directory this process cannot write.
    ``$TMPDIR`` is read here, not through :mod:`tempfile` (module docstring)."""
    var = next((v for v in (TMP_ENV, "TMPDIR") if os.environ.get(v, "").strip()), None)
    raw = os.environ[var].strip() if var else ""
    base = Path(raw) if raw else Path(tempfile.gettempdir())
    if not base.is_absolute():
        raise ValueError(f"${var} must be an absolute path, not {raw!r}")
    base.mkdir(parents=True, exist_ok=True)
    if not os.access(base, os.W_OK | os.X_OK):
        raise PermissionError(f"duckdb temp base {base} is not writable (set ${TMP_ENV} or $TMPDIR)")
    return base


def connect(*, external_access: bool = True) -> duckdb.DuckDBPyConnection:
    """An in-memory duckdb connection whose spill directory is ``<temp_base>/duckdb-<pid>-<random>`` (module
    docstring); ``external_access=False``: ``SET enable_external_access = false`` before it is returned."""
    tmp = temp_base() / f"duckdb-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    con = duckdb.connect(config={"temp_directory": str(tmp)})
    if not external_access:
        try:
            con.execute("SET enable_external_access = false")
        except BaseException:
            con.close()
            raise
    return con
