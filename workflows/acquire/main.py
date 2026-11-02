"""Stage ``acquire`` (§3.1, §3.3, §5.4-§5.6; D5, D15, D22, D25, D32, D53): ``documents`` rows →
snapshots, roles and the text pointer of the ``content`` block, published as one ``documents``
**part file** holding exactly the rows whose ``content`` changed (``blocks_written =
['content']``), plus rows going ``triaged → acquired`` with an unchanged block (a pure re-point,
D5).

Run: ``stack run acquire --in data/documents/<series>/ [--filter <name|sql>] [--only <substage>…]``.

**Substages** (D32), in order: ``carry`` → ``inband`` → ``history`` → ``fetch`` → ``convert`` →
``quality`` → ``point``. ``carry``, ``inband``, ``quality``, ``point`` are pure
(:data:`PURE_SUBSTAGES`: deterministic from the row and blobs, no network, no conversion) and
always run; ``history`` and ``fetch`` are the only network substages, so ``--only convert`` (or
``--only point``) makes zero network calls.

* scope (:mod:`.scope`, D5): ``triaged``/``acquired`` rows that triage lets fetch, have a due role,
  an unminted in-band entry, or a stale/NULL ``content``; ``identified`` rows are out of scope.
  The run's filter (``--filter``, else the stage line's ``filter:``; a named filter or a SQL
  predicate over the ``documents`` view row, :func:`stack.fold.filter_rows`) narrows it: a row the
  filter rejects is not in scope (never carried, fetched, converted or re-pointed; counted
  ``scope.filtered_out``); ``scope_complete`` is about the filtered scope; a predicate that does
  not evaluate is a :class:`~stack.stage_runner.StageError` before any lane call;
* ``carry`` (:mod:`.carry`): regroup by member (X21), wanted roles from ``acquire.wants``, gate;
* ``inband`` (:mod:`.inband`): in-band snapshots (C20, D8);
* ``history`` (:mod:`.history`, T3.3) and ``convert`` (:mod:`.convert`, T3.5): seams; with
  ``max_rows`` the history covers only the rows the fetch can reach, in the scope's order (D410,
  :func:`_history`): rounds of at least :data:`stack.arxiv.ATOM_BATCH` rows up to the ``max_rows``-th
  row with fetchable due work, repeated while the history's answers (a role stamped checked) move that
  row on; later rows are not touched (their re-check waits for a run that reaches them);
* ``quality`` (:mod:`.quality`, round 3, Q): the LLM-free quality verdict of each current text
  under the gate of ``options.quality`` (:mod:`stack.conversion.quality`); a text below the fail
  thresholds becomes ``failed_permanent[quality_<reason>]`` and the pointer does not use it, one
  between fail and ok stays ``ok`` with ``quality.flags``; ``content.current_text_quality`` is the
  current snapshot's bucket;
* ``fetch``: one :class:`~workflows.acquire.lanes.FetchRequest` per due role of a row triage lets
  fetch (not masked, live member, a lane module present), rows in ``doc_id`` order; the skeleton
  owns the accounting (:mod:`stack.fetchcycle` ``role_*``: attempts, backoff, cap 4 per role, a rate
  limit backed off but not counted toward it (D484),
  ``retry_after``, the weekly recheck of ``ok`` roles of channel group ``url``, D22, and of a lane
  that sets ``recheck_ok``: ``arxiv``, whose history reads it as "checked" (per host:
  ``acquire.recheck.hosts``, D701, e.g. ``openai.com: 30d``); and, set by the
  ``quality`` substage, of a role whose current text is a publisher's Article in Press,
  ``acquire.recheck.article_in_press``, default 14 days, until the version of record replaces it);
  equal digest mints nothing;
* ``point`` (:mod:`.point`): the §5.5 pointer at the convert pin, ``content.convert_pin :=
  pin``, and ``same_raw_doc_ids`` recomputed over the whole view (C29/X25: documents sharing a raw
  digest list each other; never merged). A row out of scope (the filter's included) whose
  ``same_raw_doc_ids`` changes is written too.

**Structured text** (``options.structured: {jats: bool, arxiv_html: bool}`` on this line, default off;
round 5, T-RUN B8): JATS (role ``xml``, the OA lane) and arXiv's LaTeXML HTML (role ``html``, the
arXiv lane, which fetches it only with ``arxiv_html``; every member whose key carries an arXiv id
wants ``html`` then, whichever channel found it, :mod:`.carry`) are converted and judged beside the PDF
and stand in for their source's PDF once their text is ``ok``; the PDF stays the fallback. The
option changes the convert pin (:mod:`.convert`); :mod:`.point` has the ranking, :mod:`.quality` the
comparison counts, :mod:`.lanes.arxiv` the ``html`` episode and its 180-day "no HTML" memory.

**arXiv PDF** (``options.arxiv_pdf: always | on_miss | never``, D480-D482; :mod:`.carry`, "arXiv PDF",
has the rule): which ``full_text`` roles on lane ``arxiv`` are fetched, for every member whose PDF
that lane serves (an arXiv paper, and an OpenAlex or S2 paper keyed by an arXiv id or DOI).
``always`` (the default; absent or null): every one, beside the HTML, as before the option.
``on_miss``: the HTML first; the PDF only for an id whose HTML is a miss (no HTML role, arXiv has no
acceptable page, the role failed for good, the text failed conversion or the quality gate).
``never``: no arXiv PDF. ``on_miss`` and ``never`` need ``structured.arxiv_html: true``. A PDF not
requested is ``skipped[not_requested]`` (error ``arxiv_pdf: …``), so it is never due and the
history asks nothing for it; switching to ``always`` later makes only those roles due (the HTML
roles are not touched, so no HTML is asked again; a held revision costs no PDF request). Under
``on_miss`` the rule runs twice: in ``carry``, and again after ``quality`` over the reached rows
(:func:`_arxiv_pdf_after_quality`), so an HTML this run found missing makes its PDF due in the row
it writes; that run ends ``scope_complete = false`` (``arxiv_pdf.due_next_run``, build reason
``pending``) and the next run (the build's acquire repeat) fetches the PDF. Counts
``arxiv_pdf.not_requested``, ``arxiv_pdf.requested``, ``arxiv_pdf.miss.<why>``. The option is not in
the convert pin.

**Reopen** (``options.reopen: {lane, error_class?, since?}``; round 5, R4-2; set by ``stack acquire
reopen``, not by a plan): before the scope is read, every failed role of that lane
(``failed_transient``, ``failed_permanent``, ``quarantined``; :class:`stack.fetchcycle.Reopen`) is
made due now (``retry_after := now``), so the run fetches it like any due role; counts
``reopen.roles`` and ``reopen.rows``. Otherwise a ``cap_reached`` role and a ``failed_permanent`` one of
:data:`stack.fetchcycle.RECHECKED_PERMANENT` are rechecked 90 days after they ended (``TERMINAL_RECHECK``).

**Stops** (§3.3): ``options.max_rows`` (rows that make lane calls) and ``budget.max_minutes`` stop
the fetch early; these stop every lane. The convert substage has its own two (D392 (e2e-fix), defaults
D483): ``options.convert_max_rows`` (rows whose candidate it converts; an int >= 0; absent or
``null``: no cap, whatever ``max_rows`` is) and ``options.convert_max_minutes`` (a number > 0; absent
or ``null``: ``budget.max_minutes``, or no cap without a budget), both counted on convert's **own
clock**, started when convert starts, after the fetch, :mod:`.convert`. So a fetch stopped by
``max_rows`` or by the budget still leaves convert its full share, in-band bodies (which take no
fetch) included, and **a run can take up to twice** ``budget.max_minutes`` (std-v3: 90 min of fetch
plus up to 90 min of convert per run, up to 12 h per build at ``max_runs: 4``); set
``convert_max_minutes`` or ``convert_max_rows`` to bound convert tighter (an explicit value wins). A lane's ``halt_run`` (a provider's 429, an
auth failure) halts **that lane** for the rest of the run: its later roles are not called (not
charged; a ``not_attempted`` one reads ``not_attempted[halted]``, ``counts.halted_roles``) while
the other lanes go on; ``counts.halted`` is the number of halted lanes, ``halted.<lane>`` marks
each, ``stopped.halted`` is set (T3.6-f2: one provider's 429 burst no longer starves the other
lanes). The run ends ``ok`` with ``scope_complete = false`` after a stop or a halt; rows with due
fetch work after a stop are **unreached** (``counts.unreached``): no in-band mint, history answer,
fetch, conversion or quality verdict of the run is written for them. The stops bound fetching, not
carry (D587): an unreached row with a ``content`` block that ``carry`` changes (a membership change
since the block was written, X21; a gate move; …) is written with carry's block alone, pointed at the
block's own ``convert_pin``, its state kept (``triaged`` stays ``triaged``: acquire has not finished
it, and its new roles stay ``not_attempted``, due next run; ``counts["unreached.carried"]``); an
unreached ``acquired`` row whose ``same_raw_doc_ids`` this run changes is written with the new list
(X25), as a row outside the scope is; any other unreached row is not written at all. A row
whose due roles were only on halted lanes is written with its progress (carry, in-band, the
``halted`` marks). ``scope_complete`` is also false when ``fetch`` did not run
(``--only``) while fetchable due roles exist. A role with no lane module yet stays
``not_attempted`` (not charged, ``counts.no_lane``) and does not make the scope incomplete.

**Drain** (D570, D571; :mod:`stack.stage_runner`, "Drain"): once the process drains, ``history``
and ``fetch`` admit no new row or role (counted ``stopped.drain``; rows with due work not reached are
unreached, as after a stop); under ``fetch_workers > 1`` the lane calls in flight get the drain's
bounded wait (``wait_s``, 120 s by default), then are abandoned as not attempted (``drain.abandoned``:
their roles stay as they were, still due; their threads end on their own timeouts and are not
applied). With one worker the call in flight ends on its own timeout. ``convert`` does not start
after a drained fetch: what this run fetched keeps its raw snapshots without a text, and the next run
converts it (scope clause ``convert``, D403); a drain during ``convert`` starts no new conversion and
gives those in flight the same bounded wait, an abandoned one left as if never charged
(``convert.stopped.drain``, ``convert.abandoned``). ``quality`` and ``point`` run as usual and the
run publishes its rows.

**Fetch across hosts** (``options.fetch_workers``, an int 1-32, default 1 = the sequential loop
above; T-RUN B6). With ``fetch_workers > 1`` the due rows are admitted in row order (``max_rows``:
up to ``max_rows`` rows holding a slot, below), every lane call becomes one item, and items are grouped by
:func:`workflows.acquire.lanes.fetch_group` (the host the call starts at; the arXiv hosts one group;
``oa`` in ``fetch_workers - 2`` slots by DOI, :mod:`.lanes.oa` "Slots"). At most one item per group
is in flight and up to ``fetch_workers`` groups at once, so per-host pacing, robots, the 429 backoff
and arXiv's 3 s rule are what they are under one worker (and :class:`stack.http.PoliteFetcher` and
the OA lane's :class:`stack.oa_adapter.RequestGate` hold each host's in-flight lock, so a host the
calls reach later, such as the Wayback Machine or Unpaywall, also sees one request at a time). An idle slot goes to the idle
group whose next item comes first in row order. A lane's ``halt_run`` stops admitting its items
(items of that lane already in flight finish and count) and frees the ``max_rows`` slot of every
admitted row left with no item started or startable; admission refills the freed slots in row
order, so a halted lane's rows do not count toward ``max_rows``, as in the sequential loop (D412);
``budget.max_minutes`` stops starting items (those in flight finish) and admitting rows. Outcomes
are applied in the calling thread after the loop, in row
then role order, so snapshot ids and the part file are those of one worker for the same outcomes.
A row none of whose lane calls started is unreached (as above); a row with some calls made is
written with that progress (its other due roles stay as they were, still due), and the scope is
incomplete. Counts ``fetch.workers`` and ``fetch.groups`` (groups with work).

**Order** (``options.order``: ``doc_id``, the default, or ``newest_first``; D391 (e2e-fix)): the order
of the scope (:func:`workflows.acquire.scope.select`), which every substage walks, so a stop keeps
the rows that come first: with ``newest_first`` the most recently published (``metadata.published_at``
descending, ties by ``doc_id``, undated rows last). The part file stays in ``doc_id`` order.

**Options** (D391 (e2e-fix)): the stage line's ``options`` keys must be in :data:`OPTIONS` (the keys this
stage, its substages and its lanes read); an unknown or misspelt key, a bad ``order``, ``max_rows``,
``convert_max_rows``, ``convert_max_minutes`` or ``arxiv_pdf`` (also ``on_miss``/``never`` without
``structured.arxiv_html``) is a :class:`~stack.stage_runner.StageError`
(:func:`check_options`) before any substage (an unknown key: the runner's own check, before any run
record, D472), and at a build's start (:func:`preflight`: the build fails ``stage_refused`` before
its first run).

**Call counts** (passes, ``stack build --passes``): ``lane_calls`` (always set; ``lane_calls.<lane>``
per lane) counts the lane ``fetch`` calls of the run, the history hooks count their own requests
(``arxiv.history.requests``, ``arxiv.atom_requests``); ``not_due`` counts the rows in the scope states
(``idle`` ones included) that had no due fetch work, so a pass that only carries reads ``lane_calls
0``, ``not_due`` = every such row.

**Stamps**: written rows get ``state = 'acquired'`` (an unreached row carry re-based keeps its state,
D587) and flat ``run_id``/``updated_at`` from this run; ``content.run_id``/``updated_at`` too when
the block changed, while a pure re-point (a row back at ``triaged`` after gather reset it, its block
unchanged) keeps the block's stamps (C6); nothing else changes (X15, X17).

**Clock**: one ``now`` per run, :func:`workflows.acquire.lanes.clock_now` (``datetime.now(UTC)``;
tests fix it with ``lanes.using(now=…)``), on the run's :class:`~workflows.acquire.lanes.LaneEnv`.

:func:`acquire_rows` is the stage body over a list of rows, usable without the runner (tests).
"""

from __future__ import annotations

import copy
import datetime as dt
import functools
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, NamedTuple, cast

import pyarrow as pa

from stack.arxiv import ATOM_BATCH
from stack.fetchcycle import (
    Reopen,
    role_failed,
    role_reopened,
    role_settled,
    role_skipped,
)
from stack.fold import FoldError, filter_rows
from stack.plan import AcquireConfig, Plan, StageLine, parse_duration
from stack.schemas.families import family
from stack.stage_runner import (
    NO_DRAIN,
    DrainCheck,
    DrainPool,
    EnvironmentRefused,
    StageContext,
    StageError,
    StageResult,
    Substage,
)
from stack.stage_runner import check_options as stage_check_options
from workflows.acquire import lanes
from workflows.acquire.carry import arxiv_pdf_of, carry
from workflows.acquire.convert import guard_pin, run_convert
from workflows.acquire.history import run_history
from workflows.acquire.inband import mint_inband
from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv
from workflows.acquire.point import point, structured_of
from workflows.acquire.quality import Converted, current_quality, gate_of, run_quality
from workflows.acquire.scope import FETCH, ORDERS, fetch_items, permit, select

log = logging.getLogger(__name__)

SUBSTAGES: tuple[str, ...] = ("carry", "inband", "history", "fetch", "convert", "quality", "point")
PURE_SUBSTAGES: tuple[str, ...] = ("carry", "inband", "quality", "point")
BLOCKS_WRITTEN: list[str] = ["content"]
#: ``ROLE`` keys a lane's ``role_patch`` may set (``tried_urls`` is unioned, not set).
ROLE_PATCH_KEYS: frozenset[str] = frozenset({"etag", "last_modified", "validator_url", "last_outcome",
                                             "last_source_id"})
#: ``options.fetch_workers`` bounds (module docstring, Fetch across hosts).
MAX_FETCH_WORKERS = 32
#: The acquire line's ``options`` keys (module docstring, Options): read here (``max_rows``,
#: ``convert_max_rows``, ``convert_max_minutes``, ``order``, ``fetch_workers``, ``reopen``), by
#: :mod:`.convert` (``convert_workers``, ``convert_threads``, ``convert_timeout_s``,
#: ``allow_degraded_convert``), :mod:`.point` (``structured``), :mod:`.quality` (``quality``),
#: :mod:`.carry` (``arxiv_pdf``) and the
#: lanes (:mod:`.lanes.http`: ``http_timeout_s``, ``max_bytes``, ``wayback``, ``headless``;
#: :mod:`.lanes.oa`: ``max_bytes``, ``oa_route``, ``oa_target_task``, ``oa_playwright``).
#: A new option is added here with its reader.
OPTIONS: frozenset[str] = frozenset({
    "max_rows", "convert_max_rows", "convert_max_minutes", "order", "fetch_workers", "reopen",
    "convert_workers", "convert_threads", "convert_timeout_s", "allow_degraded_convert",
    "structured", "quality", "arxiv_pdf",
    "http_timeout_s", "max_bytes", "wayback", "headless", "oa_route", "oa_target_task", "oa_playwright",
})
#: A lane's snapshot dict (its own shape: ``digest``, ``bytes``, ``media_type``) becomes the row's ``raw`` BLOB in
#: :func:`_snapshot` (D690).
_SNAPSHOT_REQUIRED = ("digest", "bytes", "media_type")
_STAMPS = ("run_id", "updated_at")

Row = dict[str, Any]
SubstageFn = Callable[[str], AbstractContextManager[Substage]]


class AcquireResult(NamedTuple):
    """What :func:`acquire_rows` produced: the rows to write (stamped, ``doc_id`` order), whether
    the scope was completed, and the ``doc_id``s left unreached by a stop. (A ``NamedTuple``, not a
    dataclass: written when the runner loaded this file by path without registering it in
    ``sys.modules``; it registers it since D435.)"""

    rows: list[Row]
    scope_complete: bool
    unreached: list[str]


def local_substages(only: Sequence[str] | None) -> SubstageFn:
    """``ctx.substage``'s ``--only`` semantics for library callers (pure substages always run)."""

    @contextmanager
    def sub(name: str) -> Iterator[Substage]:
        yield Substage(name, only is None or name in only or name in PURE_SUBSTAGES)

    return sub


def _content_eq(a: Row | None, b: Row | None) -> bool:
    if a is None or b is None:
        return a is b
    return {k: v for k, v in a.items() if k not in _STAMPS} == {k: v for k, v in b.items() if k not in _STAMPS}


class _Ids:
    def __init__(self, run_id: str) -> None:
        self.run_id, self.n = run_id, 0

    def __call__(self) -> str:
        sid = f"{self.run_id}.{self.n}"
        self.n += 1
        return sid


def acquire_rows(rows: Sequence[Row], env: LaneEnv, *, wants: Mapping[str, list[str]],
                 channel_group: Callable[[str], str | None], no_basis_fetches: bool = True,
                 recheck: dt.timedelta = dt.timedelta(days=7), max_rows: int | None = None,
                 max_minutes: float | None = None, substage: SubstageFn | None = None,
                 heartbeat: Callable[[], None] | None = None,
                 only_ids: Collection[str] | None = None, fetch_workers: int = 1, order: str = "doc_id",
                 convert_max_rows: int | None = None,
                 convert_max_minutes: float | None = None, drain: DrainCheck = NO_DRAIN) -> AcquireResult:
    """The stage body over the ``documents`` view ``rows`` (dicts, not modified). Counts go to
    ``env.counts``. ``only_ids`` (the run's filter, resolved): rows whose ``doc_id`` is not in it
    are out of scope (:func:`~workflows.acquire.scope.select`); ``same_raw_doc_ids`` still sees all
    of ``rows``. ``order``: the scope's order (module docstring, Order). ``convert_max_rows``:
    convert's row cap (``None``, the default: none); ``convert_max_minutes``: convert's time cap
    from the start of convert (``None``, the default: ``max_minutes``, on convert's own clock);
    module docstring, Stops (D483). ``drain``: the run's (module docstring, Drain)."""
    sub_cm = substage or local_substages(None)
    now, counts = env.now, env.counts
    gate = gate_of(env.options)
    try:
        structured = structured_of(env.options).roles
        arxiv_pdf = arxiv_pdf_of(env.options)
    except ValueError as exc:
        raise StageError(f"acquire: {exc}") from exc
    stored = {r["doc_id"]: r for r in rows}  # as read: a reopened role is a change to write
    if env.options.get("reopen") is not None:
        try:
            rows = reopen_rows(rows, Reopen.parse(env.options["reopen"]), now, counts, only_ids)
        except ValueError as exc:
            raise StageError(f"acquire: {exc}") from exc
    by_id = {r["doc_id"]: r for r in rows}
    try:
        scope_ids = select(rows, now, no_basis_fetches, counts, only=only_ids, pin=env.convert_pin,
                           quality_gate=gate.digest(), structured=structured, order=order)
    except ValueError as exc:
        raise StageError(f"acquire: {exc}") from exc
    work: dict[str, Row] = {d: copy.deepcopy(by_id[d]) for d in scope_ids}
    permits = {d: permit(by_id[d], no_basis_fetches) for d in scope_ids}
    next_id = _Ids(env.run_id)

    carry_row = functools.partial(carry, by_id=by_id, wants=wants, channel_group=channel_group,
                                  pin=env.convert_pin, arxiv_html="html" in structured, arxiv_pdf=arxiv_pdf)
    with sub_cm("carry"):
        for d, row in work.items():
            row["content"] = carry_row(by_id[d], permit=permits[d], counts=counts)

    with sub_cm("inband"):
        for row in work.values():
            mint_inband(row, row["content"], env, next_id)

    ordered = [work[d] for d in scope_ids]
    with sub_cm("history") as sub:
        if sub.active and not drain.stopping():  # D570: no new history batch after a drain
            ordered = _history(ordered, env, permits, channel_group, max_rows)

    unreached: list[str] = []
    stopped = False
    with sub_cm("fetch") as sub:
        due_rows = [r for r in ordered if _fetch_items(r, permits[r["doc_id"]], now, channel_group)]
        counts["lane_calls"] = counts.get("lane_calls", 0)
        counts["not_due"] = counts.get("idle", 0) + len(ordered) - len(due_rows)
        if sub.active:
            fetch = _fetch if fetch_workers <= 1 else functools.partial(_fetch_parallel, workers=fetch_workers)
            unreached, stopped = fetch(due_rows, env, permits, channel_group, recheck, next_id,
                                       max_rows=max_rows, max_minutes=max_minutes, heartbeat=heartbeat,
                                       drain=drain)
            sub.scope_complete = not stopped
        else:
            waiting = [r for r in due_rows if any(
                lanes.get_lane(r["content"]["roles"][i]["lane"]) is not None
                for i, _ in _fetch_items(r, permits[r["doc_id"]], now, channel_group))]
            if waiting:  # fetchable due work left unfetched: the scope is not complete (§3.3)
                counts["fetch_not_run"] = len(waiting)
                stopped = True
    missed = set(unreached)
    rebased = _rebase_unreached(ordered, missed, by_id, permits, carry_row)
    skip = missed - rebased  # unreached and not written (D587: carry left them as they were)
    counts["unreached.carried"] = len(rebased)

    converted: dict[tuple[str, str], Converted] = {}  # (doc_id, snapshot_id) -> a conversion of this run
    with sub_cm("convert") as sub:
        if sub.active and drain.stopping():  # D570: the next run converts what this one fetched (D403)
            sub.scope_complete = False
            stopped = True
        elif sub.active:  # rows unreached by fetch: not written, or written with carry's block only (D587)
            # D483: convert's own caps, on its own clock from here (absent: no row cap, the budget's minutes)
            complete = run_convert([r for r in ordered if r["doc_id"] not in missed], env, converted,
                                   max_rows=convert_max_rows,
                                   max_minutes=convert_max_minutes if convert_max_minutes is not None else max_minutes,
                                   drain=drain)
            sub.scope_complete = complete
            stopped = stopped or not complete

    with sub_cm("quality"):
        run_quality([r for r in ordered if r["doc_id"] not in missed], env, gate, converted)

    if arxiv_pdf == "on_miss":  # D480: an HTML this run found missing makes its PDF due now, fetched next run
        stopped = _arxiv_pdf_after_quality(ordered, missed, by_id, permits, env, wants=wants,
                                           channel_group=channel_group) or stopped

    with sub_cm("point"):
        for row in ordered:
            c = row["content"]
            pin = env.convert_pin
            if row["doc_id"] in rebased and c["convert_pin"] is not None:
                pin = c["convert_pin"]  # D587: carry re-bases, it does not re-pin (convert did not run)
            fields = point(c, row["identity"]["members"], pin, structured)
            c.update(fields, convert_pin=pin,
                     current_text_quality=current_quality(row, fields["current_snapshot_id"],
                                                          fields["current_text_status"]))
        extra = _same_raw(rows, {r["doc_id"]: r for r in ordered}, skip)

    out: list[Row] = []
    for row in [*(r for r in ordered if r["doc_id"] not in skip), *extra]:  # extra: skip's rows too (D587)
        d = row["doc_id"]
        old = stored[d]
        same = _content_eq(old.get("content"), row["content"])
        if same and old["state"] == "acquired":
            continue
        if d not in rebased:  # D587: a rebased row keeps its state; acquire has not finished it
            row["state"] = "acquired"
        row["run_id"], row["updated_at"] = env.run_id, now
        if same:  # a pure re-point (D5): the untouched block keeps its stamps (C6, X17)
            row["content"]["run_id"], row["content"]["updated_at"] = (old["content"][s] for s in _STAMPS)
        else:
            row["content"]["run_id"], row["content"]["updated_at"] = env.run_id, now
        out.append(row)
    out.sort(key=lambda r: r["doc_id"])
    counts["rows_written"] = len(out)
    counts["unreached"] = len(unreached)
    counts["refresh_pending"] = sum(1 for r in out for x in r["content"]["roles"]
                                    if x.get("retry_after") is not None and x["retry_after"] > now)
    return AcquireResult(out, not stopped, unreached)


def _rebase_unreached(ordered: list[Row], missed: set[str], by_id: Mapping[str, Row],
                      permits: Mapping[str, str], carry_row: Callable[..., Row]) -> set[str]:
    """D587 (module docstring, Stops): the fetch's stops bound fetching, not carry. Each row of
    ``missed`` (unreached by the fetch) that has a ``content`` block which carry changes gets carry's
    block alone in ``ordered``, in place: no in-band mint, history answer, fetch, conversion or
    quality verdict of this run, so its new roles stay ``not_attempted`` (due) and nothing claims a
    retrieval. Returns those ``doc_id``s (written, pointed at the block's own pin, in
    ``same_raw_doc_ids``). Carry is pure, so it is run again here rather than kept for every row;
    counts are not added twice. A row carry leaves as it was, or with no block to re-base, is not
    written, as before D587."""
    rebased: set[str] = set()
    for n, row in enumerate(ordered):
        d = row["doc_id"]
        old = by_id[d].get("content")
        if d not in missed or old is None:
            continue
        c = carry_row(by_id[d], permit=permits[d], counts=None)
        if not _content_eq(old, c):
            ordered[n] = {**row, "content": c}
            rebased.add(d)
    return rebased


def _history(ordered: list[Row], env: LaneEnv, permits: Mapping[str, str],
             channel_group: Callable[[str], str | None], max_rows: int | None) -> list[Row]:
    """The history hooks over the rows this run's fetch can reach (D410; module docstring, Stops):
    without ``max_rows`` every row; with it, rounds over the scope's order: each round covers the rows
    up to the ``max_rows``-th row with fetchable due work (at least :data:`~stack.arxiv.ATOM_BATCH`
    rows, so a round costs about one Atom request) and is repeated while the history's answers
    (a role stamped checked is no longer due) move that row further on. Rows past the last round
    are not touched."""
    if max_rows is None:
        return run_history(ordered, env)
    done = 0
    while done < len(ordered):
        seen, end = 0, len(ordered)
        for n, row in enumerate(ordered):
            if any(lanes.get_lane(row["content"]["roles"][i]["lane"]) is not None
                   for i, _ in _fetch_items(row, permits[row["doc_id"]], env.now, channel_group)):
                seen += 1
                if seen > max_rows:
                    end = n
                    break
        if end <= done:
            break
        end = min(len(ordered), max(end, done + ATOM_BATCH))
        ordered[done:end] = run_history(ordered[done:end], env)
        done = end
    return ordered


def _arxiv_pdf_after_quality(ordered: list[Row], unreached: set[str], by_id: Mapping[str, Row],
                             permits: Mapping[str, str], env: LaneEnv, *, wants: Mapping[str, list[str]],
                             channel_group: Callable[[str], str | None]) -> bool:
    """``on_miss``'s second pass (module docstring, arXiv PDF): ``carry`` again over the reached rows
    triage lets fetch, after this run's fetch, convert and quality, so an arXiv HTML they found
    missing or unacceptable makes its PDF role due in the row this run writes. Returns whether any
    did (the run is then scope-incomplete: ``arxiv_pdf.due_next_run``, build reason ``pending``).
    ``carry`` is idempotent on a carried row, so nothing else moves; its other counts are not
    added again."""
    due = 0
    for row in ordered:
        d = row["doc_id"]
        if d in unreached or permits[d] != FETCH:
            continue
        seen: dict[str, int] = {}
        row["content"] = carry(row, by_id, wants=wants, channel_group=channel_group, permit=permits[d],
                               pin=env.convert_pin, counts=seen, arxiv_html=True, arxiv_pdf="on_miss")
        for k, n in seen.items():
            if k.startswith("arxiv_pdf."):
                env.count(k, n)
        due += seen.get("arxiv_pdf.requested", 0)
    env.count("arxiv_pdf.due_next_run", due)
    return due > 0


def _fetch_items(row: Row, permit_: str, now: dt.datetime,
                 channel_group: Callable[[str], str | None]) -> list[tuple[int, Row]]:
    """``(index into roles, member)`` of the row's fetchable due roles
    (:func:`workflows.acquire.scope.fetch_items`, the scope's ``due`` clause; D698)."""
    return cast(list[tuple[int, Row]], fetch_items(row, permit_, now))  # the row's own member dicts


def _fetch(due_rows: list[Row], env: LaneEnv, permits: Mapping[str, str],
           channel_group: Callable[[str], str | None], recheck: dt.timedelta, next_id: _Ids, *,
           max_rows: int | None, max_minutes: float | None,
           heartbeat: Callable[[], None] | None, drain: DrainCheck = NO_DRAIN) -> tuple[list[str], bool]:
    """The fetch pass; returns ``(unreached doc_ids, incomplete)``: incomplete after a stop
    (``max_rows``, ``max_minutes``, a drain; every lane) or when a lane halted (that lane only). A
    drain mid-row leaves the row's later roles as they were (still due); the row is written."""
    t0 = time.monotonic()
    env.deadline = None if max_minutes is None else t0 + max_minutes * 60  # D584: lanes see the budget's end
    fetched_rows = 0
    unreached: list[str] = []
    halted: dict[str, str] = {}  # lane -> the halting outcome's error, for the run
    _prepare(due_rows, env, permits, channel_group)
    stop_reason: str | None = None
    for row in due_rows:
        d = row["doc_id"]
        items = _fetch_items(row, permits[d], env.now, channel_group)
        with_lane = [(i, m, lanes.get_lane(row["content"]["roles"][i]["lane"])) for i, m in items]
        if stop_reason is None and any(lane is not None for _, _, lane in with_lane):
            if max_rows is not None and fetched_rows >= max_rows:
                stop_reason = "limit"
            elif max_minutes is not None and time.monotonic() - t0 >= max_minutes * 60:
                stop_reason = "budget"
            elif drain.stopping():
                stop_reason = "drain"
        if stop_reason is not None:
            if any(lane is not None for _, _, lane in with_lane):
                unreached.append(d)
            continue
        called = False
        for i, member, lane in with_lane:
            role = row["content"]["roles"][i]
            if lane is not None and called and drain.stopping():  # D570: no new role; it stays due
                stop_reason = "drain"
                continue
            if lane is None:
                env.count("no_lane")
                if role["status"] == "not_attempted":
                    row["content"]["roles"][i] = {**role, "error": f"no lane module {role['lane']!r} yet"}
                continue
            if role["lane"] in halted:
                env.count("halted_roles")
                if role["status"] == "not_attempted":
                    row["content"]["roles"][i] = {**role, "error_class": "halted", "error": halted[role["lane"]]}
                continue
            called = True
            env.count("lane_calls")
            env.count(f"lane_calls.{role['lane']}")
            req = _request(row, i, member, channel_group, env)
            try:
                outcome = lane.fetch(req, env)
            except Exception as exc:  # noqa: BLE001 — a lane crash is the role's transient failure
                env.count("lane_crashed")
                outcome = FetchOutcome("failed_transient", "crashed", f"{type(exc).__name__}: {exc}"[:500])
            if not isinstance(outcome, FetchOutcome):
                raise StageError(f"lane {role['lane']!r} returned {type(outcome).__name__}, not FetchOutcome")
            if outcome.halt_run:
                halted[role["lane"]] = outcome.error or "provider halt"
                env.count("halted")
                env.count(f"halted.{role['lane']}")
                if role["status"] == "not_attempted":
                    row["content"]["roles"][i] = {**role, "error_class": "halted", "error": halted[role["lane"]]}
                continue
            _apply(row, i, req, outcome, env, recheck, next_id)
        if called:
            fetched_rows += 1
        if heartbeat is not None:
            heartbeat()
    if stop_reason is not None:
        env.count(f"stopped.{stop_reason}")
    if halted:
        env.count("stopped.halted")
    return unreached, stop_reason is not None or bool(halted)


class _Item(NamedTuple):
    """One lane call of the parallel fetch: the row, the role index, its lane, request and group."""

    row: Row
    i: int
    lane: Any
    req: FetchRequest
    group: str


def _call(lane: Any, req: FetchRequest, env: LaneEnv) -> tuple[Any, bool]:
    """One lane call in a worker thread: ``(outcome, crashed)``; a crash is the role's transient
    failure, as in the sequential loop."""
    try:
        return lane.fetch(req, env), False
    except Exception as exc:  # noqa: BLE001 — a lane crash is the role's transient failure
        return FetchOutcome("failed_transient", "crashed", f"{type(exc).__name__}: {exc}"[:500]), True


def _fetch_parallel(due_rows: list[Row], env: LaneEnv, permits: Mapping[str, str],
                    channel_group: Callable[[str], str | None], recheck: dt.timedelta, next_id: _Ids, *,
                    max_rows: int | None, max_minutes: float | None,
                    heartbeat: Callable[[], None] | None, workers: int,
                    drain: DrainCheck = NO_DRAIN) -> tuple[list[str], bool]:
    """:func:`_fetch` with lane calls of different fetch groups in flight at once (module
    docstring, Fetch across hosts); same return value. A drain starts no item and admits no row;
    the items in flight get its bounded wait, then are abandoned (module docstring, Drain)."""
    t0 = time.monotonic()
    env.deadline = None if max_minutes is None else t0 + max_minutes * 60  # D584: lanes see the budget's end
    unreached: list[str] = []
    halted: dict[str, str] = {}  # lane -> the halting outcome's error, for the run
    _prepare(due_rows, env, permits, channel_group)
    stop_reason: str | None = None
    admitted: list[tuple[Row, list[tuple[int, Any, Any]]]] = []  # (row, [(role index, member, lane)])
    items: list[_Item] = []
    queues: dict[str, deque[int]] = {}
    results: dict[int, tuple[Any, bool] | None] = {}  # item -> (outcome, crashed); None = its lane had halted
    # D412: a row holds a max_rows slot while one of its items has started or can still start (its
    # lane not halted); a lane halt frees the slots of rows left with nothing to call, and admission
    # refills them in row order, as the sequential loop counts only rows with a lane call.
    holders: dict[int, list[int]] = {}  # admitted row (index into admitted) holding a slot -> its items
    started: set[int] = set()  # items submitted to the pool
    pos = 0  # the next row of due_rows to admit

    def holds(items_of: list[int]) -> bool:
        return any(n in started or n not in results and items[n].req.lane not in halted for n in items_of)

    def admit() -> None:
        nonlocal pos
        for k in [k for k, ns in holders.items() if not holds(ns)]:
            del holders[k]
        while pos < len(due_rows) and (max_rows is None or len(holders) < max_rows):
            row = due_rows[pos]
            pos += 1
            with_lane = [(i, m, lanes.get_lane(row["content"]["roles"][i]["lane"]))
                         for i, m in _fetch_items(row, permits[row["doc_id"]], env.now, channel_group)]
            mine: list[int] = []
            for i, member, lane in with_lane:
                if lane is not None:
                    req = _request(row, i, member, channel_group, env)
                    group = lanes.fetch_group(lane, req, env)
                    mine.append(len(items))
                    queues.setdefault(group, deque()).append(len(items))
                    items.append(_Item(row, i, lane, req, group))
            if mine and holds(mine):
                holders[len(admitted)] = mine
            admitted.append((row, with_lane))

    admit()
    env.counts["fetch.workers"] = workers
    busy: set[str] = set()
    active: dict[Future[tuple[Any, bool]], tuple[int, str]] = {}
    with DrainPool(max_workers=workers, thread_name_prefix="acquire-fetch") as pool:
        while True:
            if stop_reason is None and max_minutes is not None and time.monotonic() - t0 >= max_minutes * 60:
                stop_reason = "budget"
            if stop_reason in (None, "limit") and drain.stopping():
                stop_reason = "drain"
            while stop_reason not in ("budget", "drain") and len(active) < pool.workers:  # may be capped (D786)
                ready = [(q[0], g) for g, q in queues.items() if q and g not in busy]
                if not ready:
                    break
                n, g = min(ready)  # the idle group whose next item comes first in row order
                queues[g].popleft()
                it = items[n]
                if it.req.lane in halted:
                    results[n] = None
                    continue
                env.count("lane_calls")
                env.count(f"lane_calls.{it.req.lane}")
                busy.add(g)
                started.add(n)
                active[pool.submit(_call, it.lane, it.req, env)] = (n, g)
            if not active:
                break
            if stop_reason == "drain" and drain.expired():  # D571: abandoned as not attempted
                env.count("drain.abandoned", len(active))
                for fut in active:
                    fut.cancel()
                active.clear()
                pool.abandoned = True
                break
            done, _ = wait(active, timeout=drain.remaining(1.0), return_when=FIRST_COMPLETED)
            for fut in done:
                n, g = active.pop(fut)
                busy.discard(g)
                outcome, crashed = fut.result()
                results[n] = (outcome, crashed)
                lane_name = items[n].req.lane
                if isinstance(outcome, FetchOutcome) and outcome.halt_run and lane_name not in halted:
                    halted[lane_name] = outcome.error or "provider halt"
                    env.count("halted")
                    env.count(f"halted.{lane_name}")
                    if stop_reason not in ("budget", "drain"):
                        admit()  # D412: refill the slots of rows the halt left with nothing to call
            if done and heartbeat is not None:
                heartbeat()
    env.counts["fetch.groups"] = len(queues)
    for row in due_rows[pos:]:  # never admitted: max_rows (the sequential loop's limit stop)
        if any(lanes.get_lane(row["content"]["roles"][i]["lane"]) is not None
               for i, _ in _fetch_items(row, permits[row["doc_id"]], env.now, channel_group)):
            unreached.append(row["doc_id"])
            stop_reason = stop_reason or "limit"

    # Apply in the calling thread, in row then role order (the sequential loop's order).
    by_pos = {(id(it.row), it.i): n for n, it in enumerate(items)}
    for row, with_lane in admitted:
        started = False
        for i, _member, lane in with_lane:
            role = row["content"]["roles"][i]
            if lane is None:
                env.count("no_lane")
                if role["status"] == "not_attempted":
                    row["content"]["roles"][i] = {**role, "error": f"no lane module {role['lane']!r} yet"}
                continue
            n = by_pos[(id(row), i)]
            if n not in results:
                continue  # never started (budget.max_minutes, a drain) or abandoned: the role stays as it was, still due
            started = True
            got = results[n]
            if got is None or (isinstance(got[0], FetchOutcome) and got[0].halt_run):
                if got is None:
                    env.count("halted_roles")
                if role["status"] == "not_attempted":
                    row["content"]["roles"][i] = {**role, "error_class": "halted", "error": halted[role["lane"]]}
                continue
            outcome, crashed = got
            if crashed:
                env.count("lane_crashed")
            if not isinstance(outcome, FetchOutcome):
                raise StageError(f"lane {role['lane']!r} returned {type(outcome).__name__}, not FetchOutcome")
            _apply(row, i, items[n].req, outcome, env, recheck, next_id)
        if not started and any(lane is not None for _, _, lane in with_lane):
            unreached.append(row["doc_id"])
    if stop_reason is not None:
        env.count(f"stopped.{stop_reason}")
    if halted:
        env.count("stopped.halted")
    return unreached, stop_reason is not None or bool(halted)


def _request(row: Row, i: int, member: Row, channel_group: Callable[[str], str | None], env: LaneEnv) -> FetchRequest:
    role = row["content"]["roles"][i]
    held = tuple(s for s in row["content"]["snapshots"]
                 if (s["source_id"], s["content_role"]) == (role["source_id"], role["content_role"]))
    if role["content_role"] == "html" and role["lane"] == "arxiv" and not lanes.arxiv_key(
            member.get("key_kind"), member.get("key_value")):  # an id from identity.keys only (R6 2b, .carry)
        aid = lanes.keys_arxiv_ids(row["identity"]).get(role["source_id"])
        if aid:
            member = {**member, lanes.KEY_ARXIV_ID: aid}
    return FetchRequest(doc_id=row["doc_id"], source_id=role["source_id"], content_role=role["content_role"],
                        channel=member["channel"], channel_group=channel_group(member["channel"]),
                        member=member, role=dict(role), now=env.now, lane=role["lane"], snapshots=held)


def _prepare(due_rows: list[Row], env: LaneEnv, permits: Mapping[str, str],
             channel_group: Callable[[str], str | None]) -> None:
    """The optional lane hook ``prepare(requests, env)`` (``lanes/__init__.py``): every due fetch role
    of the run, per lane, once before the first ``fetch`` (the OA lane primes fetchpdf's resolver
    with the run's DOIs in one batched ID Converter pass, as v3 did). A hook that raises is counted
    ``prepare_failed.<lane>`` and ignored: it is an optimisation, never a gate."""
    by_lane: dict[str, list[FetchRequest]] = {}
    for row in due_rows:
        for i, member in _fetch_items(row, permits[row["doc_id"]], env.now, channel_group):
            req = _request(row, i, member, channel_group, env)
            by_lane.setdefault(req.lane, []).append(req)
    for name, reqs in by_lane.items():
        prep = getattr(lanes.get_lane(name), "prepare", None)
        if prep is None:
            continue
        try:
            prep(reqs, env)
        except Exception as exc:  # noqa: BLE001 — an optimisation, never a gate
            env.count(f"prepare_failed.{name}")
            log.warning("acquire: lane %s prepare failed: %s: %s", name, type(exc).__name__, exc)


def _recheck_for(req: FetchRequest, lane: Any, env: LaneEnv, default: dt.timedelta) -> dt.timedelta:
    """The recheck period of an ``ok`` episode (D701): the plan's ``acquire.recheck.hosts`` entry
    for the host its fetch started on (:func:`workflows.acquire.lanes.fetch_group`), else
    ``default`` (the run's ``acquire.recheck.default``)."""
    acq = env.acquire
    recheck_cfg = getattr(acq, "recheck", None)
    if lane is None or not getattr(recheck_cfg, "hosts", None):
        return default
    return cast(dt.timedelta, recheck_cfg.for_host(lanes.fetch_group(lane, req, env)))


def _apply(row: Row, i: int, req: FetchRequest, out: FetchOutcome, env: LaneEnv,
           recheck: dt.timedelta, next_id: _Ids) -> None:
    """One lane outcome onto the row: mint snapshots, then the role's accounting."""
    c = row["content"]
    role = c["roles"][i]
    minted: list[Row] = []
    for raw in out.snapshots:
        snap = _snapshot(raw, req, env.now)
        newest = max((s for s in c["snapshots"] if s["kind"] == "raw" and s["source_id"] == snap["source_id"]
                      and s["content_role"] == snap["content_role"]),
                     key=lambda s: (s["captured_at"], s["snapshot_id"]), default=None)
        if newest is not None and newest["raw"]["sha256"] == snap["raw"]["sha256"]:
            env.count("unchanged")
            continue
        snap["snapshot_id"] = next_id()
        c["snapshots"].append(snap)
        minted.append(snap)
    env.count("snapshots_minted", len(minted))
    if out.status in ("ok", "empty"):
        lane = lanes.get_lane(req.lane)
        rechecked = req.channel_group == "url" or bool(getattr(lane, "recheck_ok", False))
        if rechecked:
            recheck = _recheck_for(req, lane, env, recheck)
        role = role_settled(role, env.now, status=out.status,
                            outcome="ok" if minted or out.status == "empty" else "unchanged",
                            recheck=recheck if rechecked else None)
    elif out.status in ("failed_transient", "failed_permanent"):
        if out.error_class is None:
            raise StageError(f"lane {req.lane!r}: {out.status} without an error_class")
        role = role_failed(role, env.now, status=out.status, error_class=out.error_class,
                           error=out.error, retry_after_hint=out.retry_after_hint)
        if role["status"] == "quarantined":
            env.count("quarantined")
    elif out.status == "skipped":
        if out.error_class is None:
            raise StageError(f"lane {req.lane!r}: skipped without an error_class")
        role = role_skipped(role, env.now, error_class=out.error_class, error=out.error)
    else:
        raise StageError(f"lane {req.lane!r} answered status {out.status!r} (expected ok, empty, "
                         f"failed_transient, failed_permanent or skipped)")
    env.count(f"outcome.{role['status']}")
    patch = {k: v for k, v in out.role_patch.items() if k in ROLE_PATCH_KEYS}
    role.update(patch)
    role["last_source_id"] = role.get("last_source_id") or req.source_id
    role["tried_urls"] = list(dict.fromkeys([*(role.get("tried_urls") or []),
                                             *out.role_patch.get("tried_urls", [])]))
    role["lane"] = req.lane
    if minted:
        role["snapshot_id"] = max(minted, key=lambda s: (s["captured_at"], s["snapshot_id"]))["snapshot_id"]
    c["roles"][i] = role
    if out.revisions is not None:
        c["revisions"] = list(out.revisions)


def _snapshot(raw: Mapping[str, Any], req: FetchRequest, now: dt.datetime) -> Row:
    missing = [k for k in _SNAPSHOT_REQUIRED if raw.get(k) is None]
    if missing:
        raise StageError(f"lane {req.lane!r}: snapshot for {req.source_id}/{req.content_role} "
                         f"without {missing}")
    captured = raw.get("captured_at") or now
    return {"snapshot_id": "", "source_id": raw.get("source_id") or req.source_id,
            "kind": raw.get("kind") or "raw", "content_role": raw.get("content_role") or req.content_role,
            "via": raw.get("via") or "live", "url": raw.get("url"),
            "final_url": raw.get("final_url"), "http_status": raw.get("http_status"),
            "etag": raw.get("etag"), "last_modified": raw.get("last_modified"), "captured_at": captured,
            "as_of": raw.get("as_of") or captured, "revision": raw.get("revision"),
            "raw": {"sha256": raw["digest"], "bytes": int(raw["bytes"]), "media_type": raw["media_type"]},
            "text": None,
            "text_status": None, "text_error_class": None, "convert_attempts": 0,
            "convert_pin": None, "quality": None}


def _same_raw(rows: Sequence[Row], work: Mapping[str, Row], unreached: set[str]) -> list[Row]:
    """Recompute ``same_raw_doc_ids`` (C29/X25) on the working rows; return copies of rows outside
    the scope, or ``unreached`` (in scope, not written otherwise; D587), whose list changed (to be
    written too, their stored block with the new list)."""
    def content_of(r: Row) -> Row | None:
        d = r["doc_id"]
        return (work[d] if d in work and d not in unreached else r).get("content")

    holders: dict[str, set[str]] = {}
    for r in rows:
        c = content_of(r)
        if r["state"] == "retired" or c is None:
            continue
        for s in c["snapshots"] or []:
            if s["kind"] == "raw":
                holders.setdefault(s["raw"]["sha256"], set()).add(r["doc_id"])
    extra: list[Row] = []
    for r in rows:
        d = r["doc_id"]
        c = content_of(r)
        if r["state"] == "retired" or c is None:
            continue
        held = d in work and d not in unreached
        if not held and r["state"] != "acquired":
            continue  # a stale carried block (identified row) waits for its own acquire
        dups = sorted({o for s in c["snapshots"] or [] if s["kind"] == "raw"
                       for o in holders.get(s["raw"]["sha256"], ()) if o != d})
        if held:
            work[d]["content"]["same_raw_doc_ids"] = dups
        elif dups != list(c["same_raw_doc_ids"] or []):
            row = copy.deepcopy(r)
            row["content"]["same_raw_doc_ids"] = dups
            extra.append(row)
    return extra


# ---------------------------------------------------------------- the stage
def channel_group(channel: str) -> str | None:
    """The channel's group from ``channels.yaml`` (``None`` for an unknown channel), through
    :func:`workflows.acquire.lanes.channel_entry`; ``channel_group`` is a required, validated key
    of every channel there (D56)."""
    group = lanes.channel_entry(channel).get("channel_group")
    return str(group) if group else None


def run(ctx: StageContext) -> StageResult:
    if "documents" not in ctx.inputs:
        raise StageError("acquire: pin the documents chain (--in data/documents/<series>/)")
    acq: AcquireConfig = ctx.plan.acquire
    opts = check_options(ctx.plan_stage.options)
    # Before any substage (--only point included): a venv that lacks the convert config's tools
    # would hide every converted text and re-convert at degraded tiers (R4-1, convert.guard_pin).
    pin = guard_pin(acq, ctx.plan_stage.options, ctx.counts)
    view = ctx.view("documents")
    only_ids = filtered_ids(view, ctx.filter) if ctx.filter is not None else None
    rows = view.to_pylist()
    env = LaneEnv(run_id=ctx.run_id, now=lanes.clock_now(), blobs=ctx.blobs, transport=lanes.transport(),
                  mailto=os.environ.get("STACK_MAILTO"), options=dict(ctx.plan_stage.options), acquire=acq,
                  convert_pin=pin, counts=ctx.counts, sleep=lanes.sleeper())
    budget = ctx.plan_stage.budget
    fetch_workers = fetch_workers_of(ctx.plan_stage.options)
    result = acquire_rows(
        rows, env, wants=acq.wants, channel_group=channel_group,
        no_basis_fetches=ctx.plan.gate.no_basis_fetches, recheck=parse_duration(acq.recheck.default),
        max_rows=opts.max_rows, max_minutes=budget.max_minutes if budget is not None else None,
        substage=ctx.substage, heartbeat=ctx.heartbeat, only_ids=only_ids, fetch_workers=fetch_workers,
        order=opts.order, convert_max_rows=opts.convert_max_rows, convert_max_minutes=opts.convert_max_minutes,
        drain=ctx.drain)
    if result.rows:
        ctx.emit_part("documents", to_table(result.rows), BLOCKS_WRITTEN)
    return StageResult(scope_complete=result.scope_complete, rows_touched=len(result.rows))


def preflight(plan: Plan, plan_stage: StageLine, store_root: Path) -> list[str]:
    """The build-start check (:func:`stack.build.preflight`): the lines :func:`run` would refuse
    ``failed[environment]`` with before its first substage, found without the network and without
    reading rows: the convert pin guard (:func:`~workflows.acquire.convert.guard_pin`: a tool
    absent, ``expect_pin``). A malformed ``options`` (:func:`check_options`) raises
    :class:`~stack.stage_runner.StageError`: the build fails ``stage_refused`` before any run."""
    check_options(plan_stage.options)
    out: list[str] = []
    try:
        guard_pin(plan.acquire, plan_stage.options, {})
    except EnvironmentRefused as exc:
        out.append(str(exc))
    return out


def reopen_rows(rows: Sequence[Row], spec: Reopen, now: dt.datetime, counts: dict[str, int],
                only_ids: Collection[str] | None = None) -> list[Row]:
    """``rows`` with every role ``spec`` matches made due at ``now`` (module docstring, Reopen);
    changed rows are copies, the others the same objects. Counts ``reopen.roles``,
    ``reopen.rows`` (always set). ``only_ids`` (the run's filter) limits it to those rows."""
    out: list[Row] = []
    n_roles = n_rows = 0
    for row in rows:
        c = row.get("content")
        hits = [] if c is None or (only_ids is not None and row["doc_id"] not in only_ids) else [
            i for i, r in enumerate(c["roles"] or []) if spec.matches(r)]
        if not hits:
            out.append(row)
            continue
        row = {**row, "content": {**c, "roles": list(c["roles"])}}
        for i in hits:
            row["content"]["roles"][i] = role_reopened(row["content"]["roles"][i], now)
        out.append(row)
        n_roles += len(hits)
        n_rows += 1
    counts["reopen.roles"] = counts.get("reopen.roles", 0) + n_roles
    counts["reopen.rows"] = counts.get("reopen.rows", 0) + n_rows
    return out


class Options(NamedTuple):
    """The stops and order of :func:`check_options` (module docstring, Stops, Order)."""

    max_rows: int | None
    convert_max_rows: int | None
    convert_max_minutes: float | None
    order: str


def check_options(options: Mapping[str, Any]) -> Options:
    """The acquire line's ``options`` checked (module docstring, Options): every key in
    :data:`OPTIONS` (an unknown one is named, with the closest known key), ``order`` in
    :data:`~workflows.acquire.scope.ORDERS`, ``max_rows`` and ``convert_max_rows`` ints >= 0 or
    ``null``, ``convert_max_minutes`` a number > 0 or ``null``, ``arxiv_pdf`` per
    :func:`~workflows.acquire.carry.arxiv_pdf_of`; else :class:`StageError`. Returns
    those four (absent = ``None``: module docstring, Stops)."""
    stage_check_options("acquire", options, OPTIONS)  # the runner's own check (D434), for library callers

    def count(key: str) -> int | None:
        n = options.get(key)
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
            raise StageError(f"acquire: options.{key} must be an integer >= 0 or null, got {n!r}")
        return n

    order = options.get("order", ORDERS[0])
    if order not in ORDERS:
        raise StageError(f"acquire: options.order must be one of {list(ORDERS)}, got {order!r}")
    minutes = options.get("convert_max_minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int | float) or minutes <= 0):
        raise StageError(f"acquire: options.convert_max_minutes must be a number > 0 or null, got {minutes!r}")
    try:
        arxiv_pdf_of(options)
    except ValueError as exc:
        raise StageError(f"acquire: {exc}") from exc
    return Options(count("max_rows"), count("convert_max_rows"), None if minutes is None else float(minutes), order)


def fetch_workers_of(options: Mapping[str, Any]) -> int:
    """``options.fetch_workers`` (module docstring, Fetch across hosts): an int from 1 to
    :data:`MAX_FETCH_WORKERS`, default 1; anything else is refused."""
    raw = options.get("fetch_workers", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_FETCH_WORKERS:
        raise StageError(f"acquire: options.fetch_workers must be an integer between 1 and "
                         f"{MAX_FETCH_WORKERS}, got {raw!r}")
    return raw


def filtered_ids(view: pa.Table, predicate: str) -> set[str]:
    """``doc_id``s of the ``documents`` view rows the run's filter keeps (a named filter or a SQL
    predicate, :func:`stack.fold.filter_rows`); one that does not evaluate is a ``StageError``."""
    try:
        kept = filter_rows(view, predicate)
    except (FoldError, ValueError) as exc:
        raise StageError(f"acquire: --filter {predicate!r} does not evaluate: {exc}") from exc
    return set(kept.column("doc_id").to_pylist())


def to_table(rows: list[Row]) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=family("documents").schema)


__all__ = ["BLOCKS_WRITTEN", "MAX_FETCH_WORKERS", "OPTIONS", "PURE_SUBSTAGES", "SUBSTAGES", "AcquireResult",
           "Options", "acquire_rows", "channel_group", "check_options", "fetch_workers_of", "filtered_ids",
           "local_substages", "preflight", "reopen_rows", "run",
           "to_table"]
