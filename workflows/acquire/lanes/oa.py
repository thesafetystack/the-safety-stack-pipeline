"""Lane ``oa`` (§3.1, §5.6 "OpenAlex"; D11, D25, D26, D35): scholarly full text of a ``paper``-group
member that is not arXiv — the open-access PDF as ``full_text`` and the structured copy (JATS/TEI
XML) as role ``xml`` (never text: never converted, never the pointer; unless the acquire line sets
``options.structured.jats``, round 5, T-RUN B8: then a JATS that converts and passes the quality gate
stands in for the same member's PDF, :mod:`workflows.acquire.point`).

ADAPTed in place from v3's ``fetch.oa`` (``workflows/fetch/oa/main.py``, ``OaFetchEngine``; it
never ran in production, D35). Kept: the identifier rule (a DOI, else a PMID; no other key is
handed to the resolver), ``retrieve_tiered`` with ``get_xml_or_html=True``/``want_provenance=True``
over the run's shared ``fetchpdf`` session (:func:`stack.oa_adapter._load_fetchpdf`: one resolver,
one per-host limiter), the resolver priming with the run's identifiers (one batched ID Converter
pass; :meth:`OaLane.prepare`), the media-type stamp from the bytes, the size cap, the mandatory
contact address for Unpaywall (``STACK_MAILTO``), and the open-access-only licence guard
(:func:`stack.oa_adapter._require_open_access_only`, verbatim). Dropped with the database:
selection and ownership (the skeleton's scope, :func:`~workflows.acquire.lanes.lane_for`), the
``lookup_keys`` preload, ``fetch_log``/``fetch_state``, the crash-window check, the worker pool,
the ``derived``-bucket companion PDF (the PDF is now the ``full_text`` role in its own right), the
provenance summary on ``fetch_log`` (counts keep the source tallies).

**Steps, cheapest first** (T3.x-f4). T3.6's broader run made 1,732 requests for 81 fetchpdf
retrievals (≈ 21 each; 62 found no copy) and found no copy of any Zenodo or MDPI DOI; a replay of
its mix through the real fetchpdf (``tests/fakes/oa_world.py``) gave the same 21, of which a paper
with no copy cost 20. A member's first role call runs the steps its role still needs, in this
order, and stops at the first usable artifact; the member's other role reuses what was found
(``env.lane_state['oa']``, the newest member per OA slot: the skeleton calls a row's roles back to
back, and both are in the member's slot, Slots below):

* ``zenodo`` — a DOI ``10.5281/zenodo.<id>``: the Zenodo record API (``/api/records/<id>``); the
  largest PDF of an open record (``access.files = public`` or ``access_right = open``) is
  downloaded. Crossref and Unpaywall hold no DataCite DOI and fetchpdf's own Zenodo branch reads
  the same record, so a conclusive answer here ends the member's search.
* ``mdpi`` — a DOI ``10.3390/…``: the DOI resolver's redirect (not followed) names the article page
  on ``mdpi.com``; ``<page>/pdf`` is downloaded (MDPI publishes every article open access).
* ``html`` — a DOI of a journal that publishes HTML only (:data:`HTML_ARTICLE_HOSTS`: Distill,
  ``10.23915``): the DOI resolver's redirect (not followed) names the article page on the journal's
  host; that page (redirects followed) is ``full_text`` as ``text/html`` when it names the DOI
  (convert reads it as publisher markup). The walk finds no PDF of such an article and took a
  cited paper's arXiv PDF for five of eight Distill articles; a page that does not name the DOI is
  inconclusive (the next steps run), a resolver 404 is negative (the DOI is not registered).
* ``unpaywall`` — another DOI: one Unpaywall record; the PDF URLs of its OA locations (best
  first, at most :data:`MAX_CANDIDATES`) are downloaded until one is this paper's PDF
  (:func:`identity_ok`: fetchpdf's check; a PDF it finds to be another article or a fragment is
  refused). A publisher's Article-in-Press URL (Nature's ``<id>_reference.pdf``) goes last, after
  the version-of-record URL derived from it (``<id>.pdf``, tried first, not counted against the
  cap; :func:`vor_first`), so a refresh of an Article-in-Press text (round 4, P;
  :mod:`workflows.acquire.quality`) reaches the version of record once it is out even while
  Unpaywall still lists the old URL. The ``walk`` cannot be steered this way: fetchpdf picks its
  own source. A record Unpaywall knows with no OA location ends ``full_text`` as
  ``failed_permanent[no_fetchable_url]`` without the walk (OpenAlex's OA locations are built
  from the same Unpaywall data, so asking it as well adds little).
* ``walk`` — fetchpdf's whole ladder (``retrieve_tiered``, ``get_xml_or_html=True``), only when
  the steps above were inconclusive for ``full_text``: Unpaywall does not know the DOI (a
  DataCite DOI other than Zenodo is not asked at all), an OA location named no usable PDF (a
  landing page, HTML, a refused download), a prefix route met an unexpected answer, or the member
  has only a PMID. Option ``oa_route: walk`` makes the walk the only step for every role (v3's
  and T3.6's behaviour, with the request gate's run cache).
* ``xml`` — the ``xml`` role takes the walk's XML when the walk ran; otherwise a structured-only
  walk (``xml_only=True``) runs only for a paper in PubMed Central (a PMCID from the primed ID
  Converter answer) or a bioRxiv/medRxiv DOI (``10.1101``); else, and always for Zenodo and MDPI
  DOIs, ``failed_permanent[no_fetchable_url]`` with no request; likewise for an HTML-article DOI. Crossref's text-mining XML links
  (``crossref_tdm``) are not tried on this path (T3.6: 2 XML artifacts in 79 walks).

**Negative results.** A step that answered "nothing here" for a DOI is remembered for the run
(``OaState.negatives``: another member with the same DOI does not ask again, ``oa.negative_reused``)
and every repeated metadata request inside fetchpdf is answered from the run's earlier answer
(:class:`stack.oa_adapter.RequestGate`'s run cache, ``oa.http.cached``; Crossref and Unpaywall
are never asked about a DataCite DOI, ``oa.http.skipped``). Across runs the role's own state
carries them: a ``failed_permanent`` role is terminal, and ``carry`` does not re-open another
member's role on the same identifier and lane once one ended terminal (it would repeat the same
steps; :mod:`workflows.acquire.carry`).

=============================================  ==============================================
episode                                        :class:`~workflows.acquire.lanes.FetchOutcome`
=============================================  ==============================================
no DOI and no PMID on the member               ``failed_permanent[no_fetchable_url]``
no artifact for this role (no OA copy found)   ``failed_permanent[no_fetchable_url]`` (each
                                               step's reason in ``error``)
no artifact for this role and a 429 stood      ``failed_transient[rate_limit]`` (the skeleton's
after the gate's retries, or a ``Gate``        backoff, not counted toward the cap, D484;
                                               ``Retry-After`` as the hint);
cooldown, on a host that is one rung of the    the lane goes on (D402, D413: arxiv.org, S2,
member's search                                OpenAlex, Crossref, Europe PMC, a publisher ...)
the same on a resolver API every member needs  ``failed_transient[rate_limit]`` + ``halt_run``
(``LANE_WIDE_APIS``: Unpaywall, the ID         (no row charged; the lane halts for the run,
Converter)                                     D164)
no artifact, a step met a 403                  D150: ``failed_transient[forbidden]`` until the
                                               attempt that reaches the cap, then
                                               ``failed_permanent[blocked]``
no artifact, a step met a 5xx or a transport   ``failed_transient[http_5xx|network]``
failure
an artifact                                    ``ok``, one raw snapshot (equal digest to the
                                               newest held → the skeleton mints nothing)
artifact over ``max_bytes``                    ``failed_permanent[too_large]``
the licence guard cannot prove open-access-    ``skipped[gated]``, no request, never charged;
only (``fetchpdf`` reaches, or may reach, a    ``carry`` re-opens a ``skipped[gated]`` role of
last-resort source)                            a row triage lets fetch, so the next run asks
                                               again (v3 refused the whole run)
``STACK_MAILTO`` unset                         ``halt_run`` (``provider_auth``): Unpaywall
                                               refuses anonymous callers; no row charged
``retrieve`` raises                            the skeleton's ``failed_transient[crashed]``
=============================================  ==============================================

**Transport** (D26; D149). Every request of every step — the lane's own and fetchpdf's — passes
the run's :class:`stack.oa_adapter.RequestGate` (``OaState.gate``): the shared
:class:`~stack.http.HostPacer` of the http and arXiv lanes
(:class:`~workflows.acquire.lanes.http.HttpSession` in ``env.lane_state['http']``; hosts it has
no rate for get fetchpdf's ladder ``rate_limits``, :func:`stack.oa_adapter.seed_rates`), the
cross-process ``Gate`` for provider families, the 429 retry loop with ``env.sleep``, the run
cache; with an injected ``env.transport`` the gate answers from it. A 429 never reads as "no
open-access copy": when the role found no artifact and a 429 stood, the episode is ``rate_limit``
(table above); when the artifact was found anyway, the role is ``ok``.

**SSRF** (D680, superseding D651). Each request of the lane's own steps (:meth:`OaLane._get`: the
Zenodo, DOI-resolver and Unpaywall APIs, the article pages and the PDF URLs a provider record
names) starts with :func:`stack.http.check_url_safety` on its URL, before any request; a refused URL
is one inconclusive candidate (``url_unsafe:<verdict>`` in the step's reason; the next OA location,
step or the walk is still tried; with no artifact the role ends ``no_fetchable_url``). Redirect
hops, fetchpdf's own requests (the walk) and the browser contexts fetchpdf opens are not checked.

**Slots** (round 6, T-R6 OA; ``options.fetch_workers``, :mod:`workflows.acquire.main` "Fetch
across hosts"). :meth:`OaLane.host_key` puts a call in fetch group ``oa:<k>``, ``k`` = CRC-32 of
the member's identifier (:attr:`Identifiers.raw`, lowercased; the source id when it has none)
modulo :func:`slots` = ``fetch_workers - 2`` (at least 1: one worker left for the arXiv slot and
one for the http hosts). Calls of different slots run at once; one slot's calls run one at a
time in row order, so a member's two roles stay back to back on one retrieval, and members sharing
a DOI (or PMID) share a slot, which keeps the run's negative results (``OaState.negatives``)
applied in the same order as under one worker. Politeness stays per host: every request of every
slot passes the one :class:`~stack.oa_adapter.RequestGate`, which holds the host's in-flight lock
of the run's shared pacer around pace, send and body (one request per host in flight in the
process, the http and arXiv lanes included), the pacer's per-host rate (fetchpdf's ``rate_limits``
for the resolver APIs, 1 rps elsewhere), the cross-process ``Gate`` (its per-family interval and
slots) and fetchpdf's own per-host token buckets. What the slots share is thread-safe: the
fetchpdf session (its resolver, resolution cache and limiter are built for its own thread pool),
the gate (run cache; 429 records per thread), :meth:`~workflows.acquire.lanes.LaneEnv.count`;
the run's negatives are written per DOI by its one slot; each thread has its own ``requests``
session for the lane's steps and each walk its own scratch directory.

**Injection** (tests): ``OaLane(load=…)`` replaces :func:`stack.oa_adapter._load_fetchpdf`
(``(scratch_dir, email) -> (fetchpdf chain module, FetchpdfSession)``); the lane applies the
guard to whatever chain it gets. Register it with ``lanes.using({'oa': OaLane(load=fake)})``.
The default lane refuses to reach the real ``fetchpdf`` when the run's HTTP transport is
injected (a test world): that raises, and the skeleton records a lane crash.

**Options**: ``max_bytes`` (default 64 MiB), ``oa_target_task`` (``extraction`` | ``screening``,
fetchpdf's ladder; default ``extraction``), ``oa_playwright`` (bool, default false), ``oa_route``
(``direct`` | ``walk``, default ``direct``; above).

**Counts**: ``oa.retrievals`` (members with a lane call), ``oa.walks``, ``oa.step.<step>``,
``oa.media.<type>``, ``oa.source.<source>`` (``zenodo_api``, ``mdpi``, ``html_article``, ``unpaywall`` or a
fetchpdf source), ``oa.no_artifact.<role>``, ``oa.unused.<type>`` (an artifact no role in
``acquire.wants`` takes), ``oa.negative_reused``, ``oa.primed`` (ID Converter requests of the
priming), ``oa.licence_refused``, ``oa.no_mailto``, ``oa.http.requests``, ``oa.http.retries``,
``oa.http.http_429``, ``oa.http.cached``, ``oa.http.skipped``, ``oa.http.s2_keyed`` (requests sent to the S2
API with our key, D592), ``oa.rate_limited``.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from stack.conversion.quality import in_press_url, version_of_record_url
from stack.http import DEFAULT_HEADERS, DEFAULT_MAX_BYTES, Resolver, check_url_safety
from stack.mediatype import media_type_for
from stack.oa_adapter import (
    FetchpdfSession,
    Limited,
    RequestGate,
    _load_fetchpdf,
    _require_open_access_only,
    import_fetchpdf,
    seed_rates,
)
from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv
from workflows.acquire.lanes.http import HttpSession, forbidden

Loader = Callable[[str, str], tuple[Any, FetchpdfSession]]

STATE = "oa"
PDF = "application/pdf"
XML = "application/xml"
HTML = "text/html"
#: Media types each role takes, in preference order.
ROLE_MEDIA: dict[str, tuple[str, ...]] = {"full_text": (PDF, HTML), "xml": (XML,)}
TARGET_TASKS = ("extraction", "screening")
#: The steps whose verdict speaks for each role (module docstring, Steps).
ROLE_STEPS: dict[str, frozenset[str]] = {"full_text": frozenset({"zenodo", "mdpi", "html", "unpaywall", "walk"}),
                                         "xml": frozenset({"xml", "walk", "walk_xml"})}
ROUTES = ("direct", "walk")
#: PDF downloads tried per step (Unpaywall's OA locations, a Zenodo record's PDFs).
MAX_CANDIDATES = 2
#: DOI prefixes registered with DataCite (fetchpdf's ``DATACITE_REPOSITORY_PREFIXES``): Crossref
#: and Unpaywall hold no record of them.
DATACITE_PREFIXES = frozenset({"10.5281", "10.6084", "10.17605", "10.31234", "10.31235", "10.23668",
                               "10.25384", "10.48550"})
MDPI_PREFIX = "10.3390"
#: DOI prefixes of journals that publish HTML only → the host the DOI resolver must name (the
#: ``html`` step; module docstring, Steps).
HTML_ARTICLE_HOSTS: Mapping[str, str] = {"10.23915": "distill.pub"}
PREPRINT_PREFIX = "10.1101"
_ZENODO = re.compile(r"^10\.5281/zenodo\.(\d+)$", re.IGNORECASE)
#: Hosts that answer by DOI and never hold a DataCite DOI: path prefix of the DOI-keyed request.
_CROSSREF_ONLY = {"api.crossref.org": "/works/", "api.unpaywall.org": "/v2/"}
_TIMEOUT_S = 30.0
#: Smallest body taken as a PDF (fetchpdf's "implausibly small" floor; a one-page abstract is ~2 KB).
PDF_FLOOR = 1024


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Identifiers:
    """The scholarly identifiers of one member."""

    doi: str = ""
    pmid: str = ""

    @property
    def raw(self) -> str:
        """What to hand the resolver when it is given no typed identifier: prefer the DOI."""
        return self.doi or self.pmid

    @property
    def prefix(self) -> str:
        return self.doi.split("/", 1)[0].lower() if self.doi.startswith("10.") else ""


def identifiers_of(member: Mapping[str, Any]) -> Identifiers:
    """A DOI from the member's key (``doi``) or a ``doi.org`` URL; a PMID from a ``pmid`` key.
    Other keys (``openalex`` ``W…``, ``s2``) are not handed to the resolver (v3's rule)."""
    kind = str(member.get("key_kind") or "").lower()
    value = str(member.get("key_value") or "").strip()
    doi = pmid = ""
    if kind == "doi" or value.startswith("10."):
        doi = value.removeprefix("https://doi.org/").removeprefix("doi:")
    elif kind == "pmid":
        pmid = value
    if not doi:
        url = str(member.get("canonical_url") or "")
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/"):
            if url.lower().startswith(prefix) and url[len(prefix):].startswith("10."):
                doi = url[len(prefix):]
    return Identifiers(doi=doi, pmid=pmid)


def route_of(ids: Identifiers) -> str | None:
    """The prefix route of a DOI (``zenodo`` | ``mdpi`` | ``html``), else ``None``."""
    if _ZENODO.match(ids.doi):
        return "zenodo"
    if ids.prefix == MDPI_PREFIX:
        return "mdpi"
    if ids.prefix in HTML_ARTICLE_HOSTS:
        return "html"
    return None


def pick(artifacts: list[tuple[str, bytes, Any]], content_role: str) -> tuple[str, bytes, Any] | None:
    """The artifact a role takes (:data:`ROLE_MEDIA`), ``(media_type, body, artifact)``."""
    for want in ROLE_MEDIA.get(content_role, ()):
        for typed in artifacts:
            if typed[0] == want:
                return typed
    return None


def unknown_to(url: str) -> str | None:
    """The request gate's ``skip``: Crossref and Unpaywall are not asked about a DataCite DOI."""
    parts = urllib.parse.urlsplit(url)
    path = _CROSSREF_ONLY.get((parts.hostname or "").lower())
    if path is None:
        return None
    rest = urllib.parse.unquote(parts.path)
    if not rest.startswith(path):
        return None
    prefix = rest[len(path):].split("/", 1)[0].lower()
    return f"DataCite DOI ({prefix}): {parts.hostname} holds no record" if prefix in DATACITE_PREFIXES else None


def zenodo_files(record: Mapping[str, Any]) -> list[tuple[int, str]]:
    """``(size, download URL)`` of a Zenodo record's PDF files, largest first. Reads both the
    legacy serialisation (``files: [{key, size, links: {self}}]``) and InvenioRDM's
    (``files: {entries: {key: {size, links: {content}}}}``)."""
    files: Any = record.get("files") or []
    if isinstance(files, Mapping):
        entries = files.get("entries") or {}
        files = list(entries.values()) if isinstance(entries, Mapping) else list(entries)
    out = []
    for f in files:
        if not isinstance(f, Mapping) or not str(f.get("key") or "").lower().endswith(".pdf"):
            continue
        links = f.get("links") or {}
        url = links.get("content") or links.get("download") or links.get("self")
        if url:
            out.append((int(f.get("size") or 0), str(url)))
    return sorted(out, key=lambda x: -x[0])


def zenodo_open(record: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether a Zenodo record's files are open, and the access it states."""
    access = record.get("access") or {}
    files = str(access.get("files") or "") if isinstance(access, Mapping) else ""
    right = str((record.get("metadata") or {}).get("access_right") or "")
    if files:
        return files == "public", f"files {files}"
    if right:
        return right == "open", f"access_right {right}"
    return True, "access not stated"  # restricted records list files without links (fetchpdf's rule)


def unpaywall_pdfs(record: Mapping[str, Any]) -> list[str]:
    """The PDF URLs of an Unpaywall record's OA locations, best location first, deduplicated."""
    locs = [record.get("best_oa_location") or {}, *(record.get("oa_locations") or [])]
    urls = [str(loc.get("url_for_pdf") or "") for loc in locs if isinstance(loc, Mapping)]
    return list(dict.fromkeys(u for u in urls if u))


def vor_first(urls: Iterable[str]) -> tuple[list[str], int]:
    """``urls`` with each Article-in-Press URL replaced by its version-of-record URL first and
    itself last (:func:`stack.conversion.quality.version_of_record_url`); the other URLs keep their
    order between. Returns ``(ordered, n_derived)``: the derived URLs are extra candidates."""
    urls = list(dict.fromkeys(urls))
    aip = [u for u in urls if in_press_url(u)]
    derived = [v for u in aip if (v := version_of_record_url(u)) is not None and v not in urls]
    derived = list(dict.fromkeys(derived))
    return [*derived, *(u for u in urls if u not in aip), *aip], len(derived)


def identity_ok(body: bytes, doi: str, title: str) -> tuple[bool, str]:
    """fetchpdf's PDF identity verdict (``verify_pdf_identity``): only a positive finding that the
    PDF is another article, or a fragment, refuses it (fetchpdf's own keep rule); without fetchpdf
    every PDF passes."""
    try:
        import_fetchpdf()  # no .env.local, no keys (R8-1)
        from fetchpdf.retrieval.pdf_identity import (  # type: ignore[import-untyped]
            TRUNCATED,
            WRONG,
            verify_pdf_identity,
        )
    except ImportError:
        return True, "not checked"
    verdict = verify_pdf_identity(body, doi or None, title or None)
    return verdict.state not in (WRONG, TRUNCATED), str(verdict.reason)


# ─────────────────────────────────────────────────────────────────────────────
# The run's state
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Found:
    """An artifact found by one of the lane's own steps (fetchpdf's ``Artifact`` shape)."""

    content: bytes
    url: str
    source: str
    http_status: int = 200


@dataclass
class Retrieved:
    """One member's search: typed artifacts, what each step said, the steps run."""

    source_id: str
    artifacts: list[tuple[str, bytes, Any]] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)        # step -> why it found nothing
    transient: dict[str, tuple[str, str]] = field(default_factory=dict)  # step -> (class, error)
    limited: list[Limited] = field(default_factory=list)  # 429s standing after the gate's retries
    steps: set[str] = field(default_factory=set)

    def reason(self, content_role: str) -> str:
        return "; ".join(f"{k}: {v}" for k, v in self.reasons.items() if k in ROLE_STEPS[content_role])

    def transient_for(self, content_role: str) -> tuple[str, str] | None:
        return next((v for k, v in self.transient.items() if k in ROLE_STEPS[content_role]), None)


@dataclass
class OaState:
    """``env.lane_state['oa']``: the session (or why there is none), the member cache, the run's
    negative results, the request gate with its run cache, and the resolver of the steps' URL check
    (the http lane's: ``socket.getaddrinfo`` on the network, none with an injected transport)."""

    session: FetchpdfSession | None = None
    refused: str | None = None
    scratch: Any = None  # tempfile.TemporaryDirectory: discarded with the run
    last: dict[int, Retrieved] = field(default_factory=dict)  # slot -> its newest member's search
    gate: RequestGate | None = None
    local: threading.local = field(default_factory=threading.local)  # .http: the thread's requests.Session
    negatives: dict[tuple[str, str], str] = field(default_factory=dict)  # (doi, step) -> reason
    cache: dict[Any, Any] = field(default_factory=dict)  # the gate's run cache
    lock: threading.Lock = field(default_factory=threading.Lock)  # building the session
    resolver: Resolver | None = None

    @classmethod
    def of(cls, env: LaneEnv) -> OaState:
        st: OaState = env.state(STATE, cls)  # shared by the slots (module docstring, Slots)
        return st

    @property
    def http(self) -> Any:
        """The calling thread's ``requests.Session`` for the lane's own steps (through the gate)."""
        got = getattr(self.local, "http", None)
        if got is None:
            import requests

            got = self.local.http = requests.Session()
        return got


class _Step(Exception):
    """A step's verdict short of an artifact: ``kind`` ∈ ``negative`` (nothing there),
    ``inconclusive`` (the next step may know more), ``transient`` (``error_class`` set)."""

    def __init__(self, kind: str, reason: str, error_class: str | None = None) -> None:
        super().__init__(reason)
        self.kind, self.reason, self.error_class = kind, reason, error_class


def _from_status(what: str, status: int) -> _Step:
    """A non-success HTTP status of a lane step as its verdict."""
    if status in (404, 410):
        return _Step("negative", f"{what}: HTTP {status}")
    if status == 403:
        return _Step("transient", f"{what}: HTTP 403", "forbidden")
    if status == 429:
        return _Step("inconclusive", f"{what}: HTTP 429")  # the gate recorded it: rate_limit
    if status >= 500:
        return _Step("transient", f"{what}: HTTP {status}", "http_5xx")
    return _Step("inconclusive", f"{what}: HTTP {status}")


# ─────────────────────────────────────────────────────────────────────────────
# The lane
# ─────────────────────────────────────────────────────────────────────────────
class OaLane:
    """``LANE``: the OA episode of one ``paper``-group role (module docstring)."""

    name = "oa"

    def __init__(self, load: Loader | None = None) -> None:
        self._load = load

    def host_key(self, request: FetchRequest, env: LaneEnv) -> str:
        """The call's fetch group, ``oa:<slot>`` (module docstring, Slots)."""
        return f"oa:{slot_of(request, env)}"

    # ── the run's session ───────────────────────────────────────────────────
    def _session(self, env: LaneEnv, st: OaState) -> FetchpdfSession | None:
        """The run's session, built once; ``None`` with ``st.refused`` set when the licence guard
        refuses. The guard runs on whatever chain the loader returns (the real loader also runs it
        before building anything)."""
        with st.lock:  # the slots' first calls may arrive together
            return self._build(env, st)

    def _build(self, env: LaneEnv, st: OaState) -> FetchpdfSession | None:
        if st.session is not None or st.refused is not None:
            return st.session
        load = self._load
        if load is None:
            if env.transport is not None:
                raise RuntimeError("OA lane: the run's transport is injected (a test world) but fetchpdf "
                                   "is the real one; register OaLane(load=…)")
            load = _load_without_session_gate
        st.scratch = tempfile.TemporaryDirectory(prefix="stack-acquire-oa-")
        try:
            chain, session = load(st.scratch.name, str(env.mailto))
            _require_open_access_only(chain)
        except RuntimeError as exc:  # the guard: a property of the build, not of any record
            st.refused = f"licence guard: {exc}"
            return None
        transport = env.transport
        http = HttpSession.of(env)
        pacer, st.resolver = http.pacer, http.resolver
        seed_rates(pacer, session.ladder_path)  # fetchpdf's per-host API rates, where none is set yet
        st.gate = RequestGate(pacer=pacer, sleep=env.sleep,
                              transport=None if transport is None else getattr(transport, "get", transport),
                              count=lambda key, n: env.count(f"oa.http.{key}", n), cache=st.cache, skip=unknown_to,
                              max_bytes=int(env.options.get("max_bytes") or DEFAULT_MAX_BYTES))
        st.session = session  # last: another thread reads a session only with its gate in place
        return session

    def prepare(self, requests_: list[FetchRequest], env: LaneEnv) -> None:
        """Prime fetchpdf's resolver with the run's DOIs/PMIDs (v3's batched ID Converter pass:
        one request per 200 identifiers; the PMCIDs it learns decide the ``xml`` step and spare the
        walk its own lookup). Prefix-routed DOIs are left out; nothing happens without a contact
        address, under ``oa_route: walk`` as in v3 all identifiers are primed."""
        if not (env.mailto or "").strip() or (self._load is None and env.transport is not None):
            return
        walk_all = _route(env) == "walk"
        ids = []
        for req in requests_:
            got = identifiers_of(req.member)
            if got.raw and (walk_all or route_of(got) is None):
                ids.append(got.raw)
        ids = list(dict.fromkeys(ids))
        if not ids:
            return
        st = OaState.of(env)
        session = self._session(env, st)
        if session is None or session.prime is None:
            return
        assert st.gate is not None
        with st.gate.active():
            env.count("oa.primed", int(session.prime(ids)))
        st.gate.take()  # a 429 on the priming is not any member's

    # ── one role ────────────────────────────────────────────────────────────
    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        if request.content_role not in ROLE_MEDIA:
            return FetchOutcome("skipped", "not_requested",
                                f"the OA lane fetches {sorted(ROLE_MEDIA)}, not {request.content_role!r}")
        ids = identifiers_of(request.member)
        if not ids.raw:
            return FetchOutcome("failed_permanent", "no_fetchable_url",
                                f"member {request.source_id} carries no DOI or PMID")
        if not (env.mailto or "").strip():
            env.count("oa.no_mailto")
            return FetchOutcome("failed_transient", "provider_auth",
                                "STACK_MAILTO is not set: Unpaywall refuses anonymous callers", halt_run=True)
        st = OaState.of(env)
        session = self._session(env, st)
        if session is None:
            env.count("oa.licence_refused")
            return FetchOutcome("skipped", "gated", (st.refused or "licence guard")[:500])
        got = self._search(request, env, st, session, ids)
        chosen = pick(got.artifacts, request.content_role)
        if chosen is None and got.limited:
            return rate_limited(got.limited, ids, request.content_role, env)
        if chosen is None:
            env.count(f"oa.no_artifact.{request.content_role}")
            why = got.reason(request.content_role) or "no open-access copy"
            error = f"{ids.raw}: no {request.content_role} artifact ({why})"[:500]
            transient = got.transient_for(request.content_role)
            if transient is not None:
                cls, msg = transient
                if cls == "forbidden":
                    return forbidden(request.role, f"{ids.raw}: {msg}"[:500], {})
                return FetchOutcome("failed_transient", cls, f"{ids.raw}: {msg}"[:500])
            return FetchOutcome("failed_permanent", "no_fetchable_url", error)
        media_type, body, art = chosen
        url = str(getattr(art, "url", "") or "") or None
        tried = [url] if url else []
        max_bytes = int(env.options.get("max_bytes") or DEFAULT_MAX_BYTES)
        if len(body) > max_bytes:
            return FetchOutcome("failed_permanent", "too_large",
                                f"{url}: {len(body)} bytes over the {max_bytes}-byte cap",
                                role_patch={"tried_urls": tried})
        digest = env.blobs.put(body)
        env.count(f"oa.media.{media_type}")
        snap = {"digest": digest, "bytes": len(body), "media_type": media_type, "url": url, "final_url": url,
                "http_status": int(getattr(art, "http_status", 200) or 200), "via": "live"}
        return FetchOutcome("ok", snapshots=[snap], role_patch={"tried_urls": tried})

    # ── the search ──────────────────────────────────────────────────────────
    def _search(self, request: FetchRequest, env: LaneEnv, st: OaState, session: FetchpdfSession,
                ids: Identifiers) -> Retrieved:
        """The steps the role still needs (module docstring), on the member's cached search."""
        slot = slot_of(request, env)
        got = st.last.get(slot)
        if got is None or got.source_id != request.source_id:
            got = st.last[slot] = Retrieved(request.source_id)
            env.count("oa.retrievals")
        assert st.gate is not None
        st.gate.take()  # nothing from an earlier member carries over
        role = request.content_role
        if pick(got.artifacts, role) is not None:
            return got
        walk_only = _route(env) == "walk"
        if role == "full_text" and not walk_only:
            self._full_text(request, env, st, session, ids, got)
        elif role == "xml" and not walk_only and "walk" not in got.steps:
            self._xml(request, env, st, session, ids, got)
        elif "walk" not in got.steps:
            self._walk(request, env, st, session, ids, got, xml_only=False)
        got.limited.extend(st.gate.take())
        return got

    def _full_text(self, request: FetchRequest, env: LaneEnv, st: OaState, session: FetchpdfSession,
                   ids: Identifiers, got: Retrieved) -> None:
        route = route_of(ids)
        steps = [route] if route else []
        if ids.doi and ids.prefix not in DATACITE_PREFIXES:
            steps.append("unpaywall")
        for step in steps:
            verdict = self._run(step, env, st, ids, got)
            if verdict in ("found", "negative", "transient") or (st.gate is not None and st.gate.limited):
                return  # a conclusive answer (the walk would ask the same sources), or a standing 429
        self._walk(request, env, st, session, ids, got, xml_only=False)

    def _xml(self, request: FetchRequest, env: LaneEnv, st: OaState, session: FetchpdfSession,
             ids: Identifiers, got: Retrieved) -> None:
        route = route_of(ids)
        if route is not None:
            got.reasons["xml"] = f"{route}: no JATS route"
            return
        known = session.lookup(ids.raw) if session.lookup is not None else None
        if known is None and session.prime is not None and session.lookup is not None:
            with st.gate.active():  # type: ignore[union-attr]
                env.count("oa.primed", int(session.prime([ids.raw])))
            known = session.lookup(ids.raw)
        pmcid = str((known or {}).get("pmcid") or "")
        if st.gate is not None and st.gate.limited:
            got.reasons["xml"] = "ID Converter rate-limited"
            return
        if pmcid or ids.prefix == PREPRINT_PREFIX or session.lookup is None:
            self._walk(request, env, st, session, ids, got, xml_only=True)
            return
        got.reasons["xml"] = "not in PubMed Central (no PMCID) and no preprint JATS route"

    def _run(self, step: str, env: LaneEnv, st: OaState, ids: Identifiers, got: Retrieved) -> str:
        """One of the lane's own steps; returns its verdict (``found`` | ``negative`` |
        ``inconclusive`` | ``transient``) and records it on ``got``."""
        got.steps.add(step)
        key = (ids.doi.lower(), step)
        if key in st.negatives:
            env.count("oa.negative_reused")
            got.reasons[step] = st.negatives[key]
            return "negative"
        env.count(f"oa.step.{step}")
        try:
            found = {"zenodo": self._zenodo, "mdpi": self._mdpi, "html": self._html,
                     "unpaywall": self._unpaywall}[step](env, st, ids)
        except _Step as v:
            got.reasons[step] = v.reason
            if v.kind == "negative":
                st.negatives[key] = v.reason
            elif v.kind == "transient":
                got.transient[step] = (str(v.error_class), v.reason)
            return v.kind
        media = media_type_for(found.content)
        got.artifacts.append((media, found.content, found))
        env.count(f"oa.source.{found.source}")
        return "found"

    def _get(self, st: OaState, url: str, *, redirects: bool = True, accept: str | None = None) -> Any:
        """One request of a lane step, through the run's gate; a URL the SSRF check refuses is an
        inconclusive verdict with no request (module docstring, SSRF), a transport failure a
        transient one."""
        import requests

        unsafe = check_url_safety(url, resolver=st.resolver)
        if unsafe is not None:
            raise _Step("inconclusive", f"{url}: refused (url_unsafe:{unsafe})")
        headers = {"User-Agent": DEFAULT_HEADERS["User-Agent"], "Accept": accept or DEFAULT_HEADERS["Accept"]}
        assert st.gate is not None and st.http is not None
        try:
            with st.gate.active():
                return st.http.get(url, headers=headers, timeout=_TIMEOUT_S, allow_redirects=redirects)
        except (requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema) as exc:
            # a redirect to file:/data:/ftp: (or no scheme) dies in requests before any adapter: a bad
            # URL, not a network failure retried every run (review-2 N1)
            raise _Step("inconclusive", f"{url}: {type(exc).__name__}") from None
        except requests.RequestException as exc:
            raise _Step("transient", f"{url}: {type(exc).__name__}", "network") from None

    def _pdf(self, st: OaState, urls: Iterable[str], ids: Identifiers, title: str, source: str,
             what: str, limit: int = MAX_CANDIDATES) -> Found:
        """The first of ``urls`` (at most ``limit``) that serves this paper's PDF; else the most
        telling verdict (a 403 over a 404 over a non-PDF)."""
        worst: _Step | None = None
        for url in list(urls)[:limit]:
            try:
                r = self._get(st, url, accept="application/pdf,*/*;q=0.8")
            except _Step as v:
                worst = worst if worst is not None and worst.kind == "transient" else v
                continue
            body = r.content if r.status_code == 200 else b""
            if r.status_code != 200:
                v = _from_status(f"{what} PDF {url}", r.status_code)
            elif not body.startswith(b"%PDF") or len(body) < PDF_FLOOR:
                v = _Step("inconclusive", f"{what} PDF {url}: not a PDF ({len(body)} bytes)")
            else:
                ok, why = identity_ok(body, ids.doi, title)
                if ok:
                    return Found(body, str(r.url or url), source)
                v = _Step("inconclusive", f"{what} PDF {url}: {why}")
            if worst is None or (v.kind == "transient" and worst.kind != "transient"):
                worst = v
        raise worst or _Step("inconclusive", f"{what}: no PDF URL")

    def _zenodo(self, env: LaneEnv, st: OaState, ids: Identifiers) -> Found:
        m = _ZENODO.match(ids.doi)
        assert m is not None
        api = f"https://zenodo.org/api/records/{m.group(1)}"
        r = self._get(st, api, accept="application/json")
        if r.status_code != 200:
            raise _from_status(f"Zenodo record {m.group(1)}", r.status_code)
        try:
            record = r.json()
        except ValueError:
            raise _Step("inconclusive", f"Zenodo record {m.group(1)}: not JSON") from None
        is_open, access = zenodo_open(record)
        if not is_open:
            raise _Step("negative", f"Zenodo record {m.group(1)}: {access}")
        files = zenodo_files(record)
        if not files:
            raise _Step("negative", f"Zenodo record {m.group(1)}: no PDF file")
        title = str((record.get("metadata") or {}).get("title") or "")
        return self._pdf(st, [u for _, u in files], ids, title, "zenodo_api", f"Zenodo record {m.group(1)}")

    def _mdpi(self, env: LaneEnv, st: OaState, ids: Identifiers) -> Found:
        r = self._get(st, f"https://doi.org/{ids.doi}", redirects=False)
        if r.status_code not in (301, 302, 303, 307, 308):
            raise _from_status(f"doi.org {ids.doi}", r.status_code) if r.status_code != 200 else _Step(
                "inconclusive", f"doi.org {ids.doi}: no redirect")
        page = urllib.parse.urljoin(f"https://doi.org/{ids.doi}", r.headers.get("Location") or "")
        host = (urllib.parse.urlsplit(page).hostname or "").lower()
        if not (host == "mdpi.com" or host.endswith(".mdpi.com")):
            raise _Step("inconclusive", f"doi.org {ids.doi}: resolves to {host or 'nothing'}, not mdpi.com")
        return self._pdf(st, [page.split("?")[0].rstrip("/") + "/pdf"], ids, "", "mdpi", "MDPI")

    def _html(self, env: LaneEnv, st: OaState, ids: Identifiers) -> Found:
        want = HTML_ARTICLE_HOSTS[ids.prefix]
        r = self._get(st, f"https://doi.org/{ids.doi}", redirects=False)
        if r.status_code not in (301, 302, 303, 307, 308):
            raise _from_status(f"doi.org {ids.doi}", r.status_code) if r.status_code != 200 else _Step(
                "inconclusive", f"doi.org {ids.doi}: no redirect")
        page = urllib.parse.urljoin(f"https://doi.org/{ids.doi}", r.headers.get("Location") or "")
        host = (urllib.parse.urlsplit(page).hostname or "").lower()
        if not (host == want or host.endswith("." + want)):
            raise _Step("inconclusive", f"doi.org {ids.doi}: resolves to {host or 'nothing'}, not {want}")
        r = self._get(st, page, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
        if r.status_code != 200:
            raise _from_status(f"{want} page {page}", r.status_code)
        body = r.content or b""
        if media_type_for(body) != HTML:
            raise _Step("inconclusive", f"{want} page {page}: not HTML ({len(body)} bytes)")
        if ids.doi.lower().encode() not in body.lower():
            raise _Step("inconclusive", f"{want} page {page}: does not name the DOI")
        return Found(body, str(r.url or page), "html_article")

    def _unpaywall(self, env: LaneEnv, st: OaState, ids: Identifiers) -> Found:
        url = f"https://api.unpaywall.org/v2/{ids.doi}?{urllib.parse.urlencode({'email': env.mailto})}"
        r = self._get(st, url, accept="application/json")
        if r.status_code == 404:
            raise _Step("inconclusive", "Unpaywall does not know the DOI")
        if r.status_code != 200:
            raise _from_status("Unpaywall", r.status_code)
        try:
            record = r.json()
        except ValueError:
            raise _Step("inconclusive", "Unpaywall: not JSON") from None
        urls, derived = vor_first(unpaywall_pdfs(record))
        if not urls:
            if record.get("is_oa") or record.get("oa_locations"):
                raise _Step("inconclusive", "Unpaywall: OA location without a PDF URL")
            raise _Step("negative", "Unpaywall: no open-access location")
        try:
            return self._pdf(st, urls, ids, str(record.get("title") or ""), "unpaywall", "Unpaywall",
                             limit=MAX_CANDIDATES + derived)
        except _Step as v:  # the walk may still read the landing page or another route
            raise _Step("inconclusive", v.reason) from None

    def _walk(self, request: FetchRequest, env: LaneEnv, st: OaState, session: FetchpdfSession,
              ids: Identifiers, got: Retrieved, *, xml_only: bool) -> None:
        """One ``retrieve_tiered`` for the member: the whole ladder (``get_xml_or_html``), or the
        structured rung only (``xml_only``)."""
        step = "walk" if not xml_only else "walk_xml"
        got.steps.add(step)
        key = (ids.raw.lower(), step)
        if key in st.negatives:
            env.count("oa.negative_reused")
            got.reasons[step] = st.negatives[key]
            return
        task = str(env.options.get("oa_target_task") or "extraction")
        if task not in TARGET_TASKS:
            raise ValueError(f"OA lane: options.oa_target_task {task!r} not in {TARGET_TASKS}")
        workdir = tempfile.mkdtemp(prefix="walk-", dir=st.scratch.name)  # one per walk: slots walk at once
        env.count("oa.walks")
        assert st.gate is not None
        flags = {"xml_only": True} if xml_only else {"get_xml_or_html": True}
        try:
            with st.gate.active():
                result = session.retrieve(
                    ids.raw, doi=ids.doi or None, pmid=ids.pmid or None,
                    save_path=os.path.join(workdir, "artifact.pdf"), target_task=task, want_provenance=True,
                    use_playwright=bool(env.options.get("oa_playwright", False)), **flags, **dict(session.kwargs))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)  # the bytes come back in the result
        arts = list(getattr(result, "artifacts", None) or [])
        typed = []
        for art in arts:
            body = bytes(getattr(art, "content", b"") or b"")
            if body:
                typed.append((media_type_for(body), body, art))
                env.count(f"oa.source.{getattr(art, 'source', '') or 'unknown'}")
        wants = (env.acquire.wants if env.acquire is not None else {}).get(request.channel) or [request.content_role]
        taken = {p[0] for r in wants if (p := pick(typed, r)) is not None}
        for media, _body, _art in typed:
            if media not in taken:
                env.count(f"oa.unused.{media}")
        got.artifacts.extend(typed)
        if not typed:
            reason = str(getattr(result, "reason", "") or "") or "no tier produced a validated artifact"
            got.reasons[step] = reason
            if not st.gate.limited:
                st.negatives[key] = reason


def slots(env: LaneEnv) -> int:
    """The number of OA slots (module docstring, Slots): ``fetch_workers - 2``, at least 1."""
    workers = env.options.get("fetch_workers", 1)
    return max(1, (workers if isinstance(workers, int) and not isinstance(workers, bool) else 1) - 2)


def slot_of(request: FetchRequest, env: LaneEnv) -> int:
    """The request's OA slot: CRC-32 of the member's identifier (else its source id) modulo
    :func:`slots`; members sharing a DOI share a slot."""
    key = identifiers_of(request.member).raw.lower() or request.source_id
    return zlib.crc32(key.encode()) % slots(env)


def _route(env: LaneEnv) -> str:
    route = str(env.options.get("oa_route") or "direct")
    if route not in ROUTES:
        raise ValueError(f"OA lane: options.oa_route {route!r} not in {ROUTES}")
    return route


def _load_without_session_gate(scratch: str, email: str) -> tuple[Any, FetchpdfSession]:
    """The real loader for the lane: the lane's :class:`~stack.oa_adapter.RequestGate` gates every
    request, so the session-mounted gate is left off (a nested ``Gate`` entry would deadlock)."""
    return _load_fetchpdf(scratch, email, gate_requests=False)


#: The resolver APIs every member's search needs, as host -> path prefix (D413; amends D402 and
#: D164): Unpaywall (the direct route's first step for every non-DataCite DOI, and fetchpdf's
#: resolver asks it for every DOI) and the ID Converter (the ``xml`` step's PMCID check and the
#: priming; fetchpdf's resolver for every member; its old ``www.ncbi`` address 301s there). A 429
#: standing after the gate's retries, or a ``Gate`` cooldown, on one of these halts the lane for
#: the run: every later member would meet it. Any other host, a provider's or not (arxiv.org, S2,
#: OpenAlex, Crossref, DataCite, Zenodo, Europe PMC, CORE, PMC, a publisher), is one rung of one
#: member's search, which fetchpdf walks past: its limit charges that role only (review L4-1,
#: D402 for S2; e2e build 2: an arXiv Gate cooldown set by the arXiv lane halted the OA lane, D413).
LANE_WIDE_APIS: Mapping[str, str] = {
    "api.unpaywall.org": "/",
    "pmc.ncbi.nlm.nih.gov": "/tools/idconv/",
    "www.ncbi.nlm.nih.gov": "/pmc/utils/idconv/",
}


def lane_wide(url: str) -> bool:
    """Whether a rate limit on ``url`` halts the OA lane (:data:`LANE_WIDE_APIS`)."""
    parts = urllib.parse.urlsplit(url)
    prefix = LANE_WIDE_APIS.get((parts.hostname or "").lower())
    return prefix is not None and parts.path.startswith(prefix)


def rate_limited(limited: list[Limited], ids: Identifiers, content_role: str, env: LaneEnv) -> FetchOutcome:
    """The episode of a role that found no artifact while a 429 stood (module docstring):
    ``failed_transient[rate_limit]``, the longest ``Retry-After`` as the hint; ``halt_run`` only
    when one of the limits was on a resolver API every member needs (:func:`lane_wide`)."""
    env.count("oa.rate_limited")
    hosts = sorted({x.host for x in limited})
    halt = any(lane_wide(x.url) for x in limited)
    waits = [x.retry_after_s for x in limited if x.retry_after_s is not None]
    hint = env.now + dt.timedelta(seconds=max(waits)) if waits else None
    error = (f"{ids.raw}: no {content_role} artifact while rate-limited (HTTP 429 after retries: "
             f"{', '.join(hosts)})")[:500]
    return FetchOutcome("failed_transient", "rate_limit", error, halt_run=halt, retry_after_hint=hint)


LANE = OaLane()

__all__ = ["DATACITE_PREFIXES", "HTML_ARTICLE_HOSTS", "LANE", "LANE_WIDE_APIS", "MAX_CANDIDATES", "ROLE_MEDIA", "Identifiers",
           "OaLane", "OaState", "Retrieved", "identifiers_of", "identity_ok", "lane_wide", "pick", "rate_limited", "route_of", "slot_of", "slots",
           "unknown_to", "unpaywall_pdfs", "vor_first", "zenodo_files", "zenodo_open"]
