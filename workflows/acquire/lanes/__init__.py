"""The lane protocol and registry of ``acquire`` (§3.1, §5.6; D11, D25, D26). Interface pinned by
T3.1; the lanes themselves are ``lanes/{http,arxiv,oa}.py`` (T3.2-T3.4).

**Lookup.** A lane is found by name (``arxiv | oa | http``, D11 ``ROLE.lane``): a registered
override first (:func:`register_lane`, :func:`using`; tests register fakes here, no
monkeypatching), else the module ``workflows.acquire.lanes.<name>`` imported lazily and its
module attribute ``LANE``. A lane whose module does not exist yet is ``None``: the skeleton then
leaves the role ``not_attempted`` (not charged) and counts ``counts.no_lane``.

**Choice.** :func:`lane_for` is the one rule: in-band roles → ``inband`` (minted from
``inband.entries[]``, never fetched); channel ``arxiv`` → ``arxiv``; a ``paper``-group member
whose key is an arXiv id (:func:`arxiv_key`: key ``arxiv``, or a DOI ``10.48550/arxiv.<id>``)
→ ``arxiv`` too, whatever its channel (T3.6-f3a: the OA route answered an arXiv DOI with arXiv's
abstract landing page, which then outranked the abstract as "full text"); group ``paper``
otherwise → ``oa``; groups ``url``/``forum`` → ``http``; anything else → ``None`` (no lane).

**One work item per due role.** The skeleton calls ``lane.fetch(request, env)`` with a
:class:`FetchRequest` (the document, the role's member, its current ``ROLE`` dict and the row's
snapshots of that ``(source_id, content_role)``) and a :class:`LaneEnv`; the lane returns a
:class:`FetchOutcome`. A lane:

* puts the raw bytes it fetched into ``env.blobs`` (bucket ``raw``) and returns one ``SNAPSHOT``
  dict per minted body (``kind = 'raw'``; the skeleton assigns ``snapshot_id = <run_id>.<n>``,
  fills the conversion fields' defaults, and drops a body whose digest equals the newest raw
  snapshot of the same ``(source_id, content_role)``: equal digest mints nothing);
* reports the episode as ``status`` + ``error_class`` + ``error`` in the §2.6 vocabulary:
  ``ok``/``empty`` (settled), ``failed_transient``/``failed_permanent``, ``skipped[…]`` (e.g.
  ``withdrawn``; never charged);
* may return ``role_patch`` with episode-learned ``ROLE`` keys: ``etag``, ``last_modified``,
  ``validator_url``, ``last_outcome`` (e.g. ``unchanged`` on a 304), ``last_source_id`` and
  ``tried_urls`` (added to the role's, order kept); ``final_url`` belongs on the snapshot (``ROLE``
  has no such field) and is ignored here;
* may return ``revisions`` (``REVISION`` dicts; ``None`` = leave ``content.revisions``);
* sets ``halt_run`` on a provider-level stop (a 429, an auth failure): the skeleton stops
  calling **this** lane for the rest of the run (other lanes go on), charges no row (this role
  and every later due role of the lane keep their state; a ``not_attempted`` role reads
  ``not_attempted[halted]``), and the run ends ``ok`` with ``scope_complete = false``;
  ``retry_after_hint`` is a provider's ``Retry-After`` (honoured on ``failed_transient``).

**History hook** (optional, T3.3): a lane may also define ``history(rows, env) -> rows``; the
``history`` substage (:func:`workflows.acquire.history.run_history`) calls it over the in-scope
working rows before ``fetch``, in :data:`FETCH_LANES` order (``arxiv``: the version listing, a
new revision made due). A lane without it (a test fake) makes no history request.

**Prepare hook** (optional, T3.x-f4): a lane may define ``prepare(requests, env)``; the skeleton
calls it once per run, before the first ``fetch``, with every due :class:`FetchRequest` of that
lane (all due rows, also those a ``max_rows``/``max_minutes`` stop will leave unreached). It may
batch lookups the lane's calls then reuse (``oa``: one ID Converter request per 200 DOIs instead of
one per paper, v3's priming). It returns nothing and charges no role; a raise is counted
``prepare_failed.<lane>`` and ignored.

**The skeleton, not the lane, owns** ``attempts``, ``first_attempt_at``/``last_attempt_at``, the
backoff ``1h·2^(attempts-1)`` capped at 7 d, the cap of 4 per role → ``quarantined[cap_reached]``
(a ``failed_transient[rate_limit]`` backs off but is not counted toward it, D484),
``retry_after``, and the weekly recheck of ``ok`` roles of channel group ``url`` (D22) and of a
lane whose ``LANE`` sets ``recheck_ok = True`` (``arxiv``: its history skips a role checked
within the period):
:mod:`stack.fetchcycle`'s ``role_*`` builders.

**Environment.** :class:`LaneEnv` is built once per run by ``main.py``: the run's ``now`` (one
clock per run), the blob store, an injectable HTTP ``transport`` (``None`` = the lane builds its
default over ``stack/http.py``/``provider_transport``, D26), the contact ``mailto`` (env
``STACK_MAILTO``, read by name, never logged), the stage line's ``options`` and the plan's
``acquire`` block, the convert pin, the run's ``counts`` dict, the ``sleep`` the lanes wait with
(HTTP retry backoff, host pacing), the fetch budget's end ``deadline`` on the ``monotonic`` clock
(``None`` without ``budget.max_minutes``; set by the fetch substage, so a lane can decline a wait the
budget would cut, D584) and ``lane_state``, a dict where a lane keeps its run-scoped
state (a host pacer, a robots cache) under its own name. The same object is the ``env`` of the
history and convert seams. :func:`using` also fixes ``now``, ``transport`` and ``sleep`` for runs
inside its block (tests drive the clock this way and never wait; real runs read
``datetime.now(UTC)`` once and sleep for real).

**Fetch groups** (``options.fetch_workers``, T-RUN B6): ``main.py`` may run lane calls of
different hosts at once, at most one call per group in flight. :func:`fetch_group` is a request's
group: the lane's optional ``host_key(request, env) -> str`` (``http``: the member URL's host,
:func:`stack.http` pacing's key; ``arxiv``: ``arxiv.org``, which an ``http`` URL on an arXiv host
joins too; ``oa``: one of its slots ``oa:<k>``, by the member's DOI, since its every call starts at
the same resolver APIs: :mod:`.oa` "Slots"), else ``lane:<name>`` (one group for the whole lane, a
test fake's). Lanes therefore keep their run-scoped state in :meth:`LaneEnv.state` (created once
under a lock, whichever thread asks first) and count with :meth:`LaneEnv.count` (locked); a lane's
own state is touched by one group at a time unless it spans hosts or groups (``http``:
:class:`~stack.http.HostPacer` and :class:`~stack.http.RobotsCache` are thread-safe, and
:class:`~stack.http.PoliteFetcher` holds a per-host lock around each request, so a secondary host,
such as the Wayback Machine, sees one request at a time too; ``oa``: its
:class:`~stack.oa_adapter.RequestGate` holds the same per-host lock around each request).

**Channel config.** :func:`channel_entry` is a channel's ``channels.yaml`` entry (its
``channel_group``, ``politeness``), read once per process from ``STACK_CONFIG_DIR`` else
``<repo>/configs``.
"""

from __future__ import annotations

import datetime as dt
import functools
import importlib
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from stack import yamlio
from stack.arxiv import normalize_arxiv_id

#: Lane names (§2.6 ``lane``), ``inband`` excluded: it is minted, never fetched.
FETCH_LANES: tuple[str, ...] = ("arxiv", "oa", "http")
INBAND = "inband"


@dataclass(frozen=True)
class FetchRequest:
    """One due role of one document. ``member`` is the role's ``MEMBER`` dict, ``role`` its current
    ``ROLE`` dict (``None`` never reaches a lane: the skeleton creates the role first),
    ``snapshots`` the row's ``SNAPSHOT`` dicts of this ``(source_id, content_role)`` (what is
    already held: a lane decides conditional requests and fallbacks on it)."""

    doc_id: str
    source_id: str
    content_role: str
    channel: str
    channel_group: str | None
    member: Mapping[str, Any]
    role: Mapping[str, Any] | None
    now: dt.datetime
    lane: str
    snapshots: tuple[Mapping[str, Any], ...] = ()


@dataclass
class FetchOutcome:
    """What a lane returns for one :class:`FetchRequest` (module docstring)."""

    status: str
    error_class: str | None = None
    error: str | None = None
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    role_patch: dict[str, Any] = field(default_factory=dict)
    revisions: list[dict[str, Any]] | None = None
    halt_run: bool = False
    retry_after_hint: dt.datetime | None = None


@dataclass
class LaneEnv:
    """The run's environment for lanes and the history/convert seams (module docstring)."""

    run_id: str
    now: dt.datetime
    blobs: Any
    transport: Any = None
    mailto: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    acquire: Any = None  # stack.plan.AcquireConfig
    convert_pin: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    deadline: float | None = None  # the fetch budget's end on ``monotonic`` (main.py sets it, D584)
    lane_state: dict[str, Any] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:  # lane calls of different hosts run in threads (fetch_workers)
            self.counts[key] = self.counts.get(key, 0) + n

    def state(self, name: str, make: Callable[[], Any]) -> Any:
        """``lane_state[name]``, created by ``make()`` on first use under the env's lock (so two
        threads never build two of a run's pacers or sessions). ``make`` may itself call
        :meth:`state` (the lock is re-entrant)."""
        with self._lock:
            got = self.lane_state.get(name)
            if got is None:
                got = self.lane_state[name] = make()
            return got


@runtime_checkable
class Lane(Protocol):
    """A fetch lane: ``LANE`` in ``workflows/acquire/lanes/<name>.py``."""

    name: str

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome: ...


# ---------------------------------------------------------------- registry
_REGISTERED: dict[str, Lane] = {}
_OVERRIDES: dict[str, Any] = {}


def register_lane(name: str, lane: Lane) -> None:
    """Use ``lane`` for ``name`` in this process (until :func:`unregister_lane`)."""
    if name not in FETCH_LANES:
        raise ValueError(f"lane {name!r} not in {FETCH_LANES}")
    if not isinstance(lane, Lane):
        raise TypeError(f"lane {name!r}: {lane!r} has no name/fetch(request, env)")
    _REGISTERED[name] = lane


def unregister_lane(name: str) -> None:
    _REGISTERED.pop(name, None)


def get_lane(name: str) -> Lane | None:
    """The registered lane, else ``workflows.acquire.lanes.<name>.LANE``, else ``None`` when that
    module does not exist (yet). An import error *inside* an existing lane module propagates."""
    if name in _REGISTERED:
        return _REGISTERED[name]
    if name not in FETCH_LANES:
        return None
    modname = f"{__name__}.{name}"
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as exc:
        if exc.name == modname:
            return None
        raise
    lane = getattr(mod, "LANE", None)
    if lane is None:
        raise AttributeError(f"{modname} defines no LANE")
    return lane


@contextmanager
def using(lanes: Mapping[str, Lane] | None = None, *, now: dt.datetime | None = None,
          transport: Any = None, sleep: Callable[[float], None] | None = None) -> Iterator[None]:
    """Within the block: ``lanes`` registered, the acquire clock fixed at ``now``, the HTTP
    ``transport`` and the lanes' ``sleep`` injected (each only when given); restored on exit."""
    saved_lanes, saved_over = dict(_REGISTERED), dict(_OVERRIDES)
    try:
        for name, lane in (lanes or {}).items():
            register_lane(name, lane)
        if now is not None:
            if now.tzinfo is None:
                raise ValueError("using(now=…): a timezone-aware datetime")
            _OVERRIDES["now"] = now
        if transport is not None:
            _OVERRIDES["transport"] = transport
        if sleep is not None:
            _OVERRIDES["sleep"] = sleep
        yield
    finally:
        _REGISTERED.clear()
        _REGISTERED.update(saved_lanes)
        _OVERRIDES.clear()
        _OVERRIDES.update(saved_over)


def clock_now() -> dt.datetime:
    """The acquire clock: the fixed ``now`` of :func:`using`, else ``datetime.now(UTC)``. Read once
    per run by ``main.py``."""
    fixed = _OVERRIDES.get("now")
    return fixed if fixed is not None else dt.datetime.now(dt.UTC)


def transport() -> Any:
    """The injected HTTP transport (``None`` = each lane's default)."""
    return _OVERRIDES.get("transport")


def sleeper() -> Callable[[float], None]:
    """The lanes' wait: the injected ``sleep`` of :func:`using`, else ``time.sleep``."""
    return _OVERRIDES.get("sleep") or time.sleep


def fetch_group(lane: Lane, request: FetchRequest, env: LaneEnv) -> str:
    """The fetch group of ``request`` (module docstring, Fetch groups): ``lane.host_key(request,
    env)`` when the lane defines it, else ``lane:<request.lane>``."""
    key = getattr(lane, "host_key", None)
    if key is None:
        return f"lane:{request.lane}"
    return str(key(request, env))


#: The fetch group of every arXiv host (the arXiv lane's, and an ``http`` URL's on arXiv).
ARXIV_GROUP = "arxiv.org"


def host_group(url: str | None, fallback: str) -> str:
    """A URL's fetch group: its host (lowercased netloc; any arXiv host is :data:`ARXIV_GROUP`),
    else ``fallback``."""
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower()
    except ValueError:
        return fallback
    if not host:
        return fallback
    if host == ARXIV_GROUP or host.endswith("." + ARXIV_GROUP):
        return ARXIV_GROUP
    return host


# ---------------------------------------------------------------- channel config
_CONFIGS = Path(__file__).resolve().parents[3] / "configs"


@functools.cache
def _channels(config_dir: Path) -> dict[str, dict[str, Any]]:
    data = yamlio.load((config_dir / "channels.yaml").read_text())
    return {str(ch["id"]): dict(ch) for ch in data.get("channels") or []}


def channel_entry(channel: str) -> Mapping[str, Any]:
    """The channel's ``channels.yaml`` entry (``{}`` for an unknown channel). Reads only that file,
    the way ``stack.config`` finds it (``STACK_CONFIG_DIR``, else ``<repo>/configs``), once per
    process: the full ``stack.config.load`` validates all seven files and costs seconds per run."""
    env = os.environ.get("STACK_CONFIG_DIR")
    return _channels(Path(env) if env else _CONFIGS).get(channel, {})


# ---------------------------------------------------------------- lane choice
#: The DOI prefix arXiv registers its papers under (DataCite), compared lowercased.
ARXIV_DOI_PREFIX = "10.48550/arxiv."


def arxiv_key(key_kind: str | None, key_value: str | None) -> str:
    """The bare arXiv id (no ``vN``) named by a member's key: an ``arxiv`` key, or a DOI
    ``10.48550/arxiv.<id>`` (any case, bare or as ``doi:``/``https://doi.org/``); ``""`` for any
    other key."""
    kind = str(key_kind or "").lower()
    value = str(key_value or "").strip()
    if kind == "arxiv":
        return normalize_arxiv_id(value)
    doi = value.lower().removeprefix("https://doi.org/").removeprefix("doi:")
    if (kind == "doi" or doi.startswith("10.")) and doi.startswith(ARXIV_DOI_PREFIX):
        return normalize_arxiv_id(value[len(value) - len(doi) + len(ARXIV_DOI_PREFIX):])
    return ""


#: A fetch request's member field holding an arXiv id known only from ``identity.keys`` (R6 item 2b;
#: set by acquire's request builder for an ``html`` role on lane ``arxiv``, read by the arXiv lane).
KEY_ARXIV_ID = "arxiv_id"


def keys_arxiv_ids(identity: Mapping[str, Any]) -> dict[str, str]:
    """``source_id`` → the bare arXiv id an ``identity.keys`` entry of that source names (an
    ``arxiv`` key or an arXiv DOI, :func:`arxiv_key`; a key adder's key such as OpenAlex's arXiv
    location, not only the member's joining key); the smallest id when a source names several."""
    out: dict[str, str] = {}
    for k in identity.get("keys") or []:
        aid = arxiv_key(k.get("kind"), k.get("value"))
        sid = k.get("source_id")
        if aid and sid and (sid not in out or aid < out[sid]):
            out[sid] = aid
    return out


def lane_for(channel: str, channel_group: str | None, content_role: str, *,
             inband: bool = False, key_kind: str | None = None, key_value: str | None = None) -> str | None:
    """The lane of a role (D11, amended T3.6-f3a): in-band → ``inband``; channel ``arxiv`` →
    ``arxiv``; group ``paper`` with an arXiv key (:func:`arxiv_key` of ``key_kind``/``key_value``)
    → ``arxiv``; group ``paper`` otherwise → ``oa``; groups ``url``/``forum`` → ``http``; else
    ``None`` (no lane)."""
    if inband:
        return INBAND
    if channel == "arxiv":
        return "arxiv"
    if channel_group == "paper":
        return "arxiv" if arxiv_key(key_kind, key_value) else "oa"
    if channel_group in ("url", "forum"):
        return "http"
    return None


__all__ = [
    "ARXIV_DOI_PREFIX",
    "ARXIV_GROUP",
    "FETCH_LANES",
    "INBAND",
    "KEY_ARXIV_ID",
    "FetchOutcome",
    "FetchRequest",
    "Lane",
    "LaneEnv",
    "arxiv_key",
    "channel_entry",
    "clock_now",
    "fetch_group",
    "get_lane",
    "host_group",
    "keys_arxiv_ids",
    "lane_for",
    "register_lane",
    "sleeper",
    "transport",
    "unregister_lane",
    "using",
]
