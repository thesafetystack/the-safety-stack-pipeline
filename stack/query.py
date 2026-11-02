"""The SQL vocabulary over a ``documents``/``corpus`` row (§2.5, §2.8, §3.2; D2, D18): the fold's
named filters and the derived macros, as SQL strings and as duckdb macros.

**Filters** (§3.2's table) are predicates over the *unqualified* row columns, so a fold can put
them in a ``WHERE`` as written; any other SQL predicate over the row is accepted by the fold too.

* ``has_text``: acquired, unmasked, current text converted to markdown (the default corpus).
* ``passed_triage``: live, unmasked, triage decided ``fetch``.
* ``everything``: ``true`` (masked and retired rows included; the diagnostic fold).
* ``screen_relevant``: the screen record is ``ok`` and its verdict ``relevant`` (borderline excluded;
  the default of an internal export, D647).

**Macros** take the row as a struct (``excluded_reason(d)`` over ``FROM corpus_everything d``):
duckdb macros cannot see the calling query's columns, so the design's ``acquire_pending()`` is
``acquire_pending(d)`` here (T1.5 builder; proposed decision).

* ``acquire_pending(d[, as_of])``: ``acquired`` and either ``convert_pending`` or some role that
  can still succeed (``failed_transient``/``not_attempted``, or a non-``ok`` role with
  ``retry_after > as_of``; D63: an ``ok`` role's ``retry_after`` is a recheck, not pending work,
  and so is the 90-day recheck of a ``failed_permanent``/``quarantined`` role, R4-2).
* ``acquire_failed(d[, as_of])``: ``acquired``, text neither ``ok`` nor ``convert_pending``, and no
  role that can still succeed. Disjoint from ``acquire_pending``; an ``ok`` role whose raw did not
  convert counts as terminal, so a ``convert_failed`` row is failed rather than in no bucket.
* ``excluded_reason(d)``: why a row is not in a default build; NULL exactly when the row passes
  ``has_text`` (D64). Precedence ``is_masked``, ``state`` (``retired``; then, for a row not yet
  ``acquired``, its triage when triage did not pass, i.e. ``passed_triage`` is false, else its
  state), ``current_text_status``, ``current_text_media``. Tokens: ``masked:<mask_reason>``,
  ``state:<state>``, ``triage:<status or decision>``, ``text:<current_text_status>``,
  ``media:<current_text_media>``. Neither triage nor screen excludes a row ``has_text`` keeps: an
  acquired row whose triage says ``skip`` (an in-band post, its question ``skipped[not_needed]``)
  and a row whose screen failed are both in the build (D230).

``as_of`` defaults to ``current_timestamp``; tests pass a fixed instant.

**Annotation-record macros** (track S, S1; :mod:`stack.annotations` has the python helpers): ``a``
is the ``annotations`` column (or ``d.annotations``), ``name`` a **string literal** naming a typed
field (duckdb resolves a struct key only from a constant; a column or expression as ``name`` is a
binder error, as is an unregistered name):

* ``ann_ok(a, name)``: the record is present and ``ok`` (false, never NULL, otherwise);
* ``ann_verdict(a, name)``: ``payload.verdict`` when the record is ``ok``, else NULL (records
  with a ``verdict`` payload field: ``screen``, ``stack_relevance``, ``sr_relevance``,
  ``text_usability``);
* ``ann_pinned(a, name, digest)``: the record when its ``config_digest = digest`` (the
  plan-pinned version, :func:`stack.annotations.pinned_digest`), else NULL.

Example: ``SELECT doc_id FROM corpus WHERE ann_verdict(annotations, 'stack_relevance') =
'relevant' AND ann_pinned(annotations, 'stack_relevance', '<digest>') IS NOT NULL``.

**Release-row macros** (D647; ``d`` the row as a struct, ``FROM read_parquet('<export>/corpus.parquet') d``):

* ``current_snapshot(d)``: the element of ``content.snapshots`` that ``content.current_snapshot_id``
  names (NULL when none; under a release's default ``snapshots: current`` it is the only one);
* ``current_text(d)``: ``decode(current_snapshot(d).text.inline)``, the current text as a string
  when the export placed it inline (``content.place: inline``), else NULL: then
  ``content.current_text_sha256`` names ``<export>/blobs/<sha256>`` (:func:`stack.store.blobs.resolve`).

**Supplied SQL** (a fold or release filter, a gate clause, a ``links`` rule: text from a config, a plan, a
file header or a release bundle) runs on :func:`sql_connection` and through :func:`one_statement`: no file
system or network access, settings locked, one statement (``stack verify --release DIR`` evaluates a
bundle's ``release.json`` ``filter``; D647).
"""

from __future__ import annotations

import duckdb

from stack import duck

# Templates: ``{d}`` is ``''`` for the unqualified filter form and ``'d.'`` for the row macro.
_HAS_TEXT = ("{d}state = 'acquired' AND NOT {d}identity.is_masked"
             " AND {d}content.current_text_status = 'ok' AND {d}content.current_text_media = 'md'")
_PASSED_TRIAGE = ("{d}state <> 'retired' AND NOT {d}identity.is_masked"
                  " AND {d}annotations.triage.status = 'ok'"
                  " AND {d}annotations.triage.payload.decision = 'fetch'")
#: A role that can still succeed: not yet decided, or not ``ok`` and due again later (D63: an
#: ``ok`` role is finished; its ``retry_after`` is a recheck, D22, not a pending attempt; the same
#: holds for a terminal role's long recheck, R4-2).
_ROLE_OPEN = ("lambda r: r.status IN ('failed_transient', 'not_attempted')"
              " OR (r.status NOT IN ('ok', 'failed_permanent', 'quarantined') AND r.retry_after > {as_of})")
_N_OPEN_ROLES = "len(list_filter(coalesce({d}content.roles, []), " + _ROLE_OPEN + "))"
_ACQUIRE_PENDING = ("coalesce({d}state = 'acquired' AND ({d}content.current_text_status = 'convert_pending'"
                    " OR " + _N_OPEN_ROLES + " > 0), false)")
_ACQUIRE_FAILED = ("coalesce({d}state = 'acquired'"
                   " AND {d}content.current_text_status NOT IN ('ok', 'convert_pending')"
                   " AND " + _N_OPEN_ROLES + " = 0, false)")
_EXCLUDED_REASON = """CASE
    WHEN {d}identity.is_masked THEN 'masked:' || coalesce({d}identity.mask_reason, 'none')
    WHEN {d}state = 'retired' THEN 'state:retired'
    WHEN {d}state <> 'acquired' AND {d}annotations.triage IS NOT NULL
         AND {d}annotations.triage.status <> 'ok'
        THEN 'triage:' || {d}annotations.triage.status
    WHEN {d}state <> 'acquired' AND {d}annotations.triage IS NOT NULL
         AND {d}annotations.triage.payload.decision IS DISTINCT FROM 'fetch'
        THEN 'triage:' || coalesce({d}annotations.triage.payload.decision, 'none')
    WHEN {d}state <> 'acquired' THEN 'state:' || {d}state
    WHEN {d}content.current_text_status IS DISTINCT FROM 'ok'
        THEN 'text:' || coalesce({d}content.current_text_status, 'none')
    WHEN {d}content.current_text_media IS DISTINCT FROM 'md'
        THEN 'media:' || coalesce({d}content.current_text_media, 'none')
END"""

_SCREEN_RELEVANT = "{d}annotations.screen.status = 'ok' AND {d}annotations.screen.payload.verdict = 'relevant'"
_CURRENT_SNAPSHOT = ("list_filter(d.content.snapshots,"
                     " lambda s: s.snapshot_id = d.content.current_snapshot_id)[1]")

_NOW = "current_timestamp"

#: Named filters (§3.2), unqualified.
HAS_TEXT = _HAS_TEXT.format(d="")
PASSED_TRIAGE = _PASSED_TRIAGE.format(d="")
EVERYTHING = "true"
SCREEN_RELEVANT = _SCREEN_RELEVANT.format(d="")
#: The derived macros as unqualified predicates (usable as a fold filter), evaluated at now.
ACQUIRE_PENDING = _ACQUIRE_PENDING.format(d="", as_of=_NOW)
ACQUIRE_FAILED = _ACQUIRE_FAILED.format(d="", as_of=_NOW)
EXCLUDED_REASON = _EXCLUDED_REASON.format(d="")

NAMED_FILTERS: dict[str, str] = {
    "has_text": HAS_TEXT,
    "passed_triage": PASSED_TRIAGE,
    "everything": EVERYTHING,
    "screen_relevant": SCREEN_RELEVANT,
}

#: ``name -> (parameters, body)`` of the duckdb macros :func:`register_macros` creates.
MACROS: dict[str, tuple[str, str]] = {
    "has_text": ("d", _HAS_TEXT.format(d="d.")),
    "passed_triage": ("d", _PASSED_TRIAGE.format(d="d.")),
    "screen_relevant": ("d", _SCREEN_RELEVANT.format(d="d.")),
    "acquire_pending": (f"d, as_of := {_NOW}", _ACQUIRE_PENDING.format(d="d.", as_of="as_of")),
    "acquire_failed": (f"d, as_of := {_NOW}", _ACQUIRE_FAILED.format(d="d.", as_of="as_of")),
    "excluded_reason": ("d", _EXCLUDED_REASON.format(d="d.")),
    "ann_ok": ("a, name", "coalesce(struct_extract(a, name).status = 'ok', false)"),
    "ann_verdict": ("a, name", ("CASE WHEN struct_extract(a, name).status = 'ok'"
                                " THEN struct_extract(a, name).payload.verdict END")),
    "ann_pinned": ("a, name, digest", ("CASE WHEN struct_extract(a, name).config_digest = digest"
                                       " THEN struct_extract(a, name) END")),
    "current_snapshot": ("d", _CURRENT_SNAPSHOT),
    "current_text": ("d", "decode(current_snapshot(d).text.inline)"),
}


def named_filter(name: str) -> str:
    """The SQL predicate of a named filter; ``KeyError`` names the known ones."""
    try:
        return NAMED_FILTERS[name]
    except KeyError:
        raise KeyError(f"unknown named filter {name!r}; named filters: {list(NAMED_FILTERS)}") from None


def resolve_filter(spec: str) -> str:
    """A named filter's SQL, or ``spec`` itself as a SQL predicate over the row."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"a fold filter is a named filter or a SQL predicate, got {spec!r}")
    return NAMED_FILTERS.get(spec.strip(), spec.strip())


def register_macros(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Create :data:`MACROS` on ``con`` as temporary macros (idempotent); returns ``con``."""
    for name, (params, body) in MACROS.items():
        con.execute(f"CREATE OR REPLACE TEMP MACRO {name}({params}) AS ({body})")
    return con


def sql_connection() -> duckdb.DuckDBPyConnection:
    """An in-memory duckdb connection for supplied SQL (module docstring; :func:`stack.duck.connect`, so a spill
    goes to the scratch directory): ``enable_external_access = false`` (``COPY ... TO``, ``read_text``/
    ``read_parquet`` of a path, ``ATTACH``, extension installs and loads are permission errors; registered arrow tables stay readable), ``TimeZone = 'UTC'``,
    :data:`MACROS` registered, then the configuration locked (no statement can re-enable access)."""
    con = duck.connect(external_access=False)
    try:
        con.execute("SET TimeZone = 'UTC'")
        register_macros(con)
        con.execute("SET lock_configuration = true")
    except BaseException:
        con.close()
        raise
    return con


def one_statement(sql: str) -> str:
    """``sql`` when duckdb parses it as exactly one statement (a predicate spliced into a ``WHERE`` cannot
    close it and append more), else :class:`duckdb.InvalidInputException`; a parse error is duckdb's
    ``ParserException``. Both are :class:`duckdb.Error`."""
    n = len(duckdb.extract_statements(sql))
    if n != 1:
        raise duckdb.InvalidInputException(f"supplied SQL makes {n} statements, not one")
    return sql


__all__ = [
    "ACQUIRE_FAILED",
    "ACQUIRE_PENDING",
    "EVERYTHING",
    "EXCLUDED_REASON",
    "HAS_TEXT",
    "MACROS",
    "NAMED_FILTERS",
    "PASSED_TRIAGE",
    "SCREEN_RELEVANT",
    "named_filter",
    "one_statement",
    "register_macros",
    "resolve_filter",
    "sql_connection",
]
