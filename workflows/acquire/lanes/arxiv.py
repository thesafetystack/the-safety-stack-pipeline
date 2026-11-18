"""Lane ``arxiv`` (§3.1, §5.6 "arXiv"; D11, D25, D26, D35): the ``full_text`` PDF of the **current
revision** of an arXiv paper, with ``options.structured.arxiv_html`` its LaTeXML HTML (role ``html``),
and the arXiv part of the ``history`` substage.

ADAPTed in place from v3's ``fetch.arxiv`` (``workflows/fetch/arxiv/main.py``; it never ran in
production, D35, so the M3 real run is its verification). Kept: the batched Atom version sweep
(100 ids per request, one retry of a garbled page), the version gate and the versioned PDF URL (so
the bytes and the recorded revision cannot disagree), the ``%PDF`` check, validators replayed only
against the URL they came from. Dropped with the database: due selection and the ownership
partition (the skeleton's scope and :func:`~workflows.acquire.lanes.lane_for`),
``fetch_log``/``fetch_state`` (``ROLE`` accounting is the skeleton's), the crash-window check, the
download budget (``options.max_rows``), the 404 withdrawal callback (step 5 below reads arXiv's
withdrawal page in the episode instead).
v3's HTML-first rung (arXiv's LaTeXML page) is back as an opt-in **role of its own** (round 5,
T-RUN B8): see LaTeXML HTML below; the PDF is fetched beside it and stays the fallback, unless the
acquire line's ``options.arxiv_pdf`` (``on_miss``, ``never``; D480) has ``carry`` skip the ``full_text``
role (``skipped[not_requested]``): then this lane is never called for it.

**History** (:meth:`ArxivLane.history`, called by :func:`workflows.acquire.history.run_history`).
Over the working rows' arXiv roles (``lane = 'arxiv'``, ``full_text``, a live member, status not
``skipped``/``quarantined``/``failed_permanent``):

0. a role checked recently is skipped (D151) (:func:`history_fresh`, v3's rule of
   ``stack/metadata_history.py:105-110``): ``ok``, its next check (``retry_after``) still in the
   future, and the newest listed revision held, so a no-op re-run within the recheck period makes
   no history request;
1. one Atom sweep (:func:`sweep_versions`, ``id_list``, :data:`~stack.arxiv.ATOM_BATCH` ids per
   request, in the run's order: the newest first under ``order: newest_first``; with ``max_rows`` over
   the rows the fetch can reach only, D410) learns each paper's current (default) version ``vN``;
   there is **no per-paper request** (the OAI-PMH ``GetRecord`` listing went on 2026-10-13: one
   request per new paper through the 3.1 s gate). An episode whose id no sweep asked (``--only
   fetch``, a row admitted after a halt freed its slot) asks for it together with the next unknown
   due ids of the run, one batch (:meth:`ArxivLane.prepare`, ``_lookahead``). **A 429/503 from Atom**
   (returned by the session at once, D588) is waited out and the batch asked again (D584, **Limits** below);
   only when that gives up (the fourth limited answer in a row, a stated wait over 300 s, or a wait
   past the fetch budget) do the sweeps stop for the rest of the run, without halting the lane
   (D411): the papers left unanswered keep their state in the history, their ``full_text`` episodes
   answer ``failed_transient[rate_limit]`` without a request (backed off, not counted toward the cap,
   D484; ``Retry-After`` as the hint; counted ``arxiv.limited_roles``), so they are due again next
   run, their ``html`` episodes ask the unversioned HTML URL (D585, LaTeXML HTML below), and the
   papers whose version is known are fetched;
2. a paper with an Atom answer gets it in ``content.revisions``
   (:func:`workflows.acquire.history.atom_revisions`: ``vN`` dated by the entry's ``<updated>``,
   ``v1`` by its ``<published>`` when N > 1, UTC midnights, no title), merged only where it adds
   something (a revision not listed, or listed without a date), so an earlier, finer listing is
   kept and an unchanged paper's row is not rewritten; versions between v1 and ``vN`` are not
   listed. A paper the sweep did not answer keeps its ``revisions`` and its role as they were;
3. an ``ok`` role whose row holds no raw snapshot of ``vN`` is made due (``retry_after := now``):
   the fetch substage then calls this lane, which mints ``vN`` (a new raw snapshot,
   ``revision = 'vN'``, captured now, so the pointer moves to it; ``as_of`` is left to the
   skeleton's capture time on purpose: an older revision fetched or imported earlier must never
   outrank the current one by date);
4. an ``ok`` role whose ``vN`` is held and listed is stamped checked: ``retry_after := now +
   recheck`` (the plan's ``acquire.recheck.default``, weekly; v3's 7 days), not due until then;
   the skeleton stamps the same on an ``ok`` episode of this lane (:attr:`ArxivLane.recheck_ok`);
5. **withdrawal is detected from arXiv's answer to the version itself** (2026-10-13), not in the
   history. The OAI-PMH flag (``source_type = 'I'``) is no longer served (T3.6), and the Atom
   entry of a withdrawal version carries no reliable marker: only the author's free-text
   ``<arxiv:comment>`` ("Withdrawn by the authors …", but also "3 pages, withdrawn", and "Theorem
   3 withdrawn" on a live paper), with the old title, abstract and PDF link (live probe,
   2026-10-13). So the history treats a withdrawal version like any new ``vN`` (step 3 makes the
   role due), and the episode asks for it: arXiv answers both the versioned PDF and the versioned
   HTML URL of a withdrawal version with HTTP 404 and its own page "This version of <id>vN has been
   withdrawn and is unavailable" (:func:`withdrawal_notice`; the phrase and the exact ``<id>vN``
   must match, any other 404 stays ``dead``/``empty``). That answer puts the id in
   ``ArxivState.withdrawn`` (``arxiv.withdrawn``) and the role ends ``skipped[withdrawn]``: nothing
   is minted, the pointer stays on the last held text, and a later role of the same id in the run
   (the ``html`` role after the PDF) answers ``skipped[withdrawn]`` without a request. The
   detection costs no extra request (the episode asks that URL anyway). Still lost: history and
   the skeleton leave ``skipped`` roles alone, so a later reinstating version (``v(N+1)`` after the
   withdrawal) is never picked up for that role; a new paper whose ``vN`` is a withdrawal gets no
   text at all. Pre-2007 papers "withdrawn" by a stub replacement (a one-line PDF, 200) mint the
   stub as ``vN``, as before.

What the sweep learned is kept in ``env.lane_state['arxiv']`` (:class:`ArxivState`), so the lane
asks nothing twice; under ``--only fetch`` (no history) the lane sweeps its own id.

**LaTeXML HTML** (role ``html``; only with the acquire line's ``options.structured.arxiv_html``,
else ``skipped[not_requested]`` as before; the plan's ``acquire.wants`` lists ``html`` beside
``full_text``). The episode resolves the current version ``vN`` like the PDF's (the history's
sweep, else its own Atom request; a listing is never taken for an answer, D807) and asks
``arxiv.org/html/<id>vN`` once, as a bare probe (no Wayback: an archived "No HTML for …" page must
never become the paper) under the same politeness, group and 3 s rule as the PDF.
**Without Atom** (D585: Atom gave up for the run, or its page for the id was unusable) the episode asks the unversioned ``arxiv.org/html/<id>`` (arXiv serves the
current version there) and reads the version from the answer (:func:`html_version`: a redirect's
final URL, else the one version the page names as ``/html/<id>vN`` or ``arXiv:<id>vN``). Found:
as below, with ``revision = 'vN'``, the snapshot's ``url`` the unversioned URL asked, and
``validator_url`` the versioned URL (so the "no HTML" memory and the next run's version check are
the usual ones); a ``vN`` already held is ``ok`` with nothing minted. Not found: nothing minted,
``failed_transient[rate_limit]`` (``arxiv.html.unversioned_unresolved``), and the unversioned URL
is not asked again in the run (one wasted request per run at most). A 404 is ``empty`` with the
unversioned URL as ``validator_url`` (re-asked once Atom names the version), or ``skipped[withdrawn]``
when it is arXiv's withdrawal page of ``<id>vN``. The ``full_text`` role gets no such path: a PDF
names its version nowhere reliable. The answer goes
through :func:`stack.arxiv.markup_is_acceptable` (status 200, at least
:data:`~stack.arxiv.ARXIV_HTML_MIN_BYTES`, no "No HTML" marker, sniffed HTML):

========================================  ==================================================
case                                      outcome
========================================  ==================================================
``vN`` html already held                  ``ok``, nothing minted
acceptable                                ``ok``, one raw snapshot (``text/html``, ``revision
                                          = 'vN'``, ``url`` = the versioned URL)
404/410 with arXiv's withdrawal page      ``skipped[withdrawn]`` (step 5)
404/410 (arXiv's soft 404 is a real 404)  ``empty``, ``last_outcome = 'no_html:http_404'``
200 that is not acceptable                ``empty``, ``last_outcome = 'no_html:<why>'``
429/503, 403, other 4xx/5xx, transport    as for the PDF (waited out, D584; ``forbidden``, …)
========================================  ==================================================

Every answer records ``validator_url`` = the versioned URL asked (no validators are replayed for
``html``). **The "no HTML" memory** (v3's 180-day ``absent_since``,
:data:`stack.arxiv.ARXIV_HTML_RECHECK_DAYS`) is that role state: an ``empty`` ``html`` role whose
``validator_url`` is the current ``vN``'s URL is not asked again until ``last_attempt_at + 180 d``
(the history step sets ``retry_after`` to that); a new version (another URL) is asked at once. arXiv
renders HTML from late 2023 on, keyed on the latest version's submission, so an old paper can gain
HTML when revised, and a paper without HTML is asked about twice a year. The history step over
``html`` roles (:meth:`ArxivLane._html_history`; a due role joins the Atom sweep, whose answer is listed in
``content.revisions`` as for the PDF, step 2): ``vN`` (or a newer one, D807) held and the role ``ok`` and due → checked
(``retry_after = now + recheck``), only on this run's Atom answer (a due role without one keeps its state); remembered absent → ``retry_after = last_attempt_at + 180 d``; a
withdrawn paper or an ``ok``/``empty`` role without ``vN`` → due now. Counts ``arxiv.html.requests``,
``arxiv.html.downloads``, ``arxiv.html.held``, ``arxiv.html.missing``,
``arxiv.html.unacceptable.<why>``, ``arxiv.html.remembered``, ``arxiv.html.requeued``,
``arxiv.html.checked``, ``arxiv.html.unversioned`` (probes of the unversioned URL, D585),
``arxiv.html.unversioned_unresolved``.

**Episode** (:meth:`ArxivLane.fetch`):

========================================  ==================================================
case                                      :class:`~workflows.acquire.lanes.FetchOutcome`
========================================  ==================================================
role other than ``full_text``             ``skipped[not_requested]``
no arXiv id on the member                 ``failed_permanent[no_fetchable_url]``
withdrawn (seen earlier in the run)       ``skipped[withdrawn]``, no request
Atom page unusable after its retry        ``failed_transient[network]`` (``atom_unavailable``; an
                                          empty feed for a batch of several ids counts as unusable;
                                          an ``html`` role asks the unversioned URL, D585)
id absent from the Atom feed              ``failed_transient[network]`` (``atom_unknown``),
                                          ``failed_permanent[dead]`` at the cap (R4-2)
``vN`` already held                       ``ok``, nothing minted (``last_outcome = 'unchanged'``)
200 with a PDF                            ``ok``, one raw snapshot, ``revision = 'vN'``
304 (validators of this versioned URL)    ``ok``, nothing minted, ``last_outcome = 'not_modified'``
200 that is not a PDF                     ``failed_transient[forbidden]`` (an interstitial or block
                                          page), ``failed_permanent[dead]`` at the cap (R4-2)
404/410 with arXiv's withdrawal page      ``skipped[withdrawn]`` (step 5), nothing minted
404/410, 4xx                              ``failed_permanent[dead]``
403                                       ``failed_transient[forbidden]``, ``failed_permanent
                                          [blocked]`` at the cap (:func:`~.http.forbidden`)
451 / 401/402                             ``failed_permanent[blocked]`` / ``[paywalled]``
body over ``max_bytes``                   ``failed_permanent[too_large]``
**429 or 503** (a PDF or HTML URL,       waited out and asked again (D584, **Limits**); when
after the retries)                        that gives up, ``halt_run``: no row charged, halts the
                                          lane for the run (other lanes go on), ``scope_complete
                                          = false`` (D107, T3.6-f2)
id left unanswered once Atom gave up      ``failed_transient[rate_limit]`` (not counted toward
(history or episode, D411, D584)          the cap, D484; the ``Retry-After`` as the hint), no
                                          request, no halt (``html``: the unversioned URL, D585)
other 5xx / timeout / connection          ``failed_transient[http_5xx|timeout|network]``
========================================  ==================================================

**Transport** (D26): every request (Atom, PDF, HTML) goes through the run's shared
:class:`~workflows.acquire.lanes.http.HttpSession` (one :class:`~stack.http.HostPacer` for all
lanes; ``env.transport`` when injected, else :func:`stack.http.urllib_get` →
``provider_transport.throttled_open``, whose ``Gate`` holds arXiv to one request per 3.1 s across
processes) under the ``arxiv`` channel's politeness (``rate_rps: 0.33``, ~3 s per host). A 429 on
it halves the session's pacer no lower than the Gate's interval, and the Gate's cooldown answer
(nothing sent) does not halve it (D584, as D401 in collect).

**Limits** (D584; :func:`_ask`). A limited answer is a 429 (a Gate cooldown answer included), or a
503 that states a wait or is still standing after the session's 5xx retries. The session returns a
429 or a Gate cooldown answer at once (``wait_limits=False``, D588): on the real transport a real
429 sets the Gate's cooldown (60 s without ``Retry-After``), so the session's 2/4/8 s retries would
only have met that cooldown and sent nothing. The ``k``-th limited answer in
a row waits ``max(its stated wait, 60 s * 2**(k-1))`` (:func:`limit_wait`: 60, 120, 240 s; a Gate
cooldown answer states what is left of the cooldown) with ``env.sleep`` and asks again; the
:data:`LIMIT_GIVE_UP`-th (4th) in a row, a stated wait over :data:`LIMIT_WAIT_MAX_S` (300 s), or a
wait longer than the fetch budget has left (``LaneEnv.deadline``) gives up (:func:`give_up_reason`;
the reason is in the error). Atom (``export.arxiv.org``) and the PDF/HTML host (``arxiv.org``) keep
separate runs of limited answers, reset by any answer that is not limited. Waiting holds the arXiv
fetch group (one arXiv call at a time anyway); the other lanes go on.

**Counts**: ``arxiv.atom_requests`` (Atom pages the sweeps asked; a page waited out and asked again
counts once), ``arxiv.atom_retries``, ``arxiv.atom_unresolved`` (ids whose batch yielded no feed;
not those Atom's limit left unasked), ``arxiv.atom_limited`` (1: Atom's limit gave up, no more
sweeps this run, D411, D584), ``arxiv.limited_waits.atom`` / ``arxiv.limited_waits.web`` (limited
answers waited out), ``arxiv.limited_wait_s`` (seconds waited), ``arxiv.limited_roles`` (roles
answered ``failed_transient[rate_limit]`` without a request because Atom gave up: called, not
requested),
``arxiv.requeued``, ``arxiv.withdrawn`` (step 5), ``arxiv.requests``, ``arxiv.downloads``,
``arxiv.halted`` (the PDF/HTML limit gave up: the lane halts), ``arxiv.history_fresh`` (roles skipped by step 0), ``arxiv.history_checked``
(step 4). (``arxiv.history.requests``, ``arxiv.history_listed`` and ``history_failed`` went with
the OAI-PMH step; :data:`stack.build.CALL_COUNTS` still sums the first for older run records.)
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stack.arxiv import (
    ARXIV_HTML_RECHECK_DAYS,
    ATOM_BATCH,
    AtomVersion,
    arxiv_html_url,
    arxiv_pdf_url,
    atom_id_list_url,
    batched,
    markup_is_acceptable,
    normalize_arxiv_id,
    parse_atom_versions,
)
from stack.http import HttpResponse, looks_like_pdf
from stack.mediatype import media_type_for
from stack.plan import parse_duration
from stack.provider_transport import retry_seconds, stated_retry_seconds
from stack.scholarly_records import normalize_arxiv
from workflows.acquire.history import atom_revisions, merge_revisions, revision_number
from workflows.acquire.lanes import (
    ARXIV_GROUP,
    KEY_ARXIV_ID,
    FetchOutcome,
    FetchRequest,
    LaneEnv,
    arxiv_key,
    channel_entry,
    keys_arxiv_ids,
)
from workflows.acquire.lanes.http import (
    HttpSession,
    forbidden,
    transient_class,
    until_cap,
)
from workflows.acquire.point import structured_of
from workflows.acquire.scope import is_due as _due

Row = dict[str, Any]
Fetch = Callable[[str], tuple[str | None, HttpResponse | None, list[dict[str, Any]]]]

CHANNEL = "arxiv"
DEFAULT_ENDPOINT = "https://export.arxiv.org/api/query"
#: A garbled Atom page (arXiv throttles by serving interstitials) gets ONE retry after this.
ATOM_RETRY_DELAY_S = 10.0
#: arXiv's "slow down" answers (the session returns them at once, D588; a Gate cooldown answer is a 429 too):
#: waited out and asked again (D584, :func:`limit_wait`); the :data:`LIMIT_GIVE_UP`-th in a row gives
#: up, which for a PDF or HTML request halts the lane for the run (no row charged; other lanes go on)
#: and for an Atom request stops the sweeps for the run (D411, module docstring).
HALT_STATUSES = frozenset({429, 503})
#: D584: the k-th limited answer in a row waits ``max(its stated wait, LIMIT_WAIT_S * 2**(k-1))``
#: (60, 120, 240 s; the Gate's cooldown without ``Retry-After`` is 60 s), then the request is asked
#: again; the :data:`LIMIT_GIVE_UP`-th in a row, a stated wait over :data:`LIMIT_WAIT_MAX_S`, or a
#: wait past the fetch's budget (``LaneEnv.deadline``) gives up instead. Separate runs of limited
#: answers for Atom (``export.arxiv.org``) and for the PDF/HTML host (``arxiv.org``).
LIMIT_WAIT_S = 60.0
LIMIT_WAIT_MAX_S = 300.0
LIMIT_GIVE_UP = 4
ATOM = "atom"
WEB = "web"
#: Role statuses the history sweep looks at (terminal ones are left alone).
_HISTORY_STATUSES = frozenset({"ok", "empty", "not_attempted", "failed_transient"})
_PERMANENT = {"dead": "dead", "blocked": "blocked", "paywalled": "paywalled"}
#: The re-check period when the run has no plan ``acquire`` block (v3's 7 days).
DEFAULT_RECHECK = dt.timedelta(days=7)
#: How long an ``empty`` ``html`` role's "no HTML for this version" is trusted (module docstring).
HTML_MEMORY = dt.timedelta(days=ARXIV_HTML_RECHECK_DAYS)
HTML_ROLE = "html"
#: arXiv's own 404 page for a withdrawn version (versioned PDF and HTML URLs alike; live probe
#: 2026-10-13, ``tests/fixtures/arxiv_withdrawn/``): ``<h1>This version of <id>vN has been withdrawn
#: and is unavailable</h1>``.
_WITHDRAWN_PAGE = re.compile(rb"This version of (\S+) has been withdrawn and is unavailable")


# ─────────────────────────────────────────────────────────────────────────────
# Run state and pure helpers
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ArxivState:
    """What this run learned about arXiv (``env.lane_state['arxiv']``)."""

    versions: dict[str, AtomVersion] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)   # ids whose Atom batch never yielded a feed
    withdrawn: set[str] = field(default_factory=set)    # arXiv's withdrawal page seen this run (step 5)
    halted: str | None = None                            # why arXiv told us to stop, once it has
    atom_limited: str | None = None                      # why Atom said slow down: no more sweeps this run (D411)
    atom_hint: dt.datetime | None = None                 # its Retry-After, as the limited roles' hint
    ahead: dict[str, int] = field(default_factory=dict)  # the run's due ids in fetch order (prepare, D410)
    streak: dict[str, int] = field(default_factory=dict)  # ATOM/WEB -> limited answers in a row (D584)
    unversioned_off: str | None = None                   # why the bare HTML URL is not asked again (D585)

    @classmethod
    def of(cls, env: LaneEnv) -> ArxivState:
        st: ArxivState = env.state(CHANNEL, cls)
        return st


@dataclass(frozen=True)
class AtomSweep:
    """What one sweep learned from the Atom API (v3 shape, plus the halt)."""

    versions: dict[str, AtomVersion]
    failed: set[str]
    requests: int = 0
    retries: int = 0
    halted: str | None = None
    limited: frozenset[str] = frozenset()   # the ids left unasked or unanswered by the halt (in ``failed``)
    retry_after: float | None = None        # the halting answer's Retry-After (seconds), if it gave one


def arxiv_id_of(member: Mapping[str, Any]) -> str:
    """The bare arXiv id of a member (D580): its typed key (:func:`~workflows.acquire.lanes.arxiv_key`:
    an ``arxiv`` key or arXiv's own DOI ``10.48550/arxiv.<id>``), else the id from ``identity.keys``
    (``KEY_ARXIV_ID``: acquire's request builder passes it for ``html`` roles, R6 item 2b; the history
    step for its own targets), else an arXiv URL or arXiv DOI in ``canonical_url``
    (:func:`stack.scholarly_records.normalize_arxiv`, anchored). Never a ``YYMM.NNNNN`` substring of
    another value: a publisher DOI such as ``10.1145/3847353.3847499`` names no arXiv id."""
    aid = arxiv_key(member.get("key_kind"), member.get("key_value"))
    if aid:
        return aid
    aid = normalize_arxiv_id(str(member.get(KEY_ARXIV_ID) or ""))
    if aid:
        return aid
    return normalize_arxiv(str(member.get("canonical_url") or ""))


def atom_endpoint() -> str:
    """The channel's ``location`` (``channels.yaml`` records the historical ``http://``; the API
    redirects to https, so ask for the secure one directly)."""
    loc = str(channel_entry(CHANNEL).get("location") or DEFAULT_ENDPOINT)
    return loc.replace("http://", "https://", 1)


def halt_reason(resp: HttpResponse | None) -> str | None:
    """``'HTTP 429'``/``'HTTP 503'`` when arXiv answered "slow down", else ``None``."""
    if resp is not None and resp.status in HALT_STATUSES:
        return f"arXiv answered HTTP {resp.status}"
    return None


def limit_wait(streak: int, stated: float | None) -> float:
    """The wait after the ``streak``-th limited answer in a row (D584): the answer's stated wait
    (``Retry-After``; the Gate's cooldown answer states what is left of the cooldown) or
    ``LIMIT_WAIT_S * 2**(streak-1)``, whichever is longer."""
    return max(stated or 0.0, LIMIT_WAIT_S * float(2 ** (streak - 1)))


def give_up_reason(streak: int, wait: float, left: float | None) -> str | None:
    """Why the ``streak``-th limited answer in a row is not waited out (D584), else ``None``:
    the :data:`LIMIT_GIVE_UP`-th in a row, a wait over :data:`LIMIT_WAIT_MAX_S`, or one longer than
    the ``left`` seconds of the fetch's budget."""
    if streak >= LIMIT_GIVE_UP:
        return f"{streak} in a row"
    if wait > LIMIT_WAIT_MAX_S:
        return f"asks {wait:.0f} s, over {LIMIT_WAIT_MAX_S:.0f} s"
    if left is not None and wait > left:
        return f"asks {wait:.0f} s, {max(left, 0.0):.0f} s of the fetch budget left"
    return None


def html_version(resp: HttpResponse, aid: str) -> int | None:
    """The version an answer to the bare ``arxiv.org/html/<aid>`` is of (D585): the final URL's
    ``/html/<aid>vN`` (a redirect), else the one version the page names as ``/html/<aid>vN`` (its
    ``<base href>``, canonical or asset links) or ``arXiv:<aid>vN`` (the watermark) in its first
    256 KB; ``None`` when nothing names a version, or the page names several."""
    esc = re.escape(aid)
    m = re.search(rf"/html/{esc}v(\d+)", resp.final_url or "")
    if m:
        return int(m.group(1))
    named = {int(v) for v in re.findall(rb"(?:/html/|arXiv:)" + esc.encode() + rb"v(\d+)", resp.body[:262144])}
    return named.pop() if len(named) == 1 else None


def sweep_versions(fetch: Fetch, endpoint: str, ids: Sequence[str], *, sleep: Callable[[float], None],
                   batch: int = ATOM_BATCH) -> AtomSweep:
    """Batched ``id_list`` queries (v3). A page that is not a feed gets one retry after
    :data:`ATOM_RETRY_DELAY_S`; a batch that still yields nothing lands every one of its ids in
    ``failed``. A well-formed feed with no entry at all for a batch of several ids is treated
    like a page that is not a feed (R4-2: one empty answer must not read as "all of these are
    unknown"); for a single id it is that id's ``atom_unknown``. A 429/503 that ``fetch`` returns (the lane's ``fetch`` waits limited answers out
    and returns one only when it gives up, D584) halts the sweep: that batch and every later one
    are ``failed`` (and ``limited``), ``halted`` says why, ``retry_after`` carries the answer's
    ``Retry-After``."""
    versions: dict[str, AtomVersion] = {}
    failed: set[str] = set()
    requests = retries = 0
    halted: str | None = None
    wait: float | None = None
    chunks = list(batched(list(ids), max(1, int(batch))))
    for n, chunk in enumerate(chunks):
        page: dict[str, AtomVersion] | None = None
        for attempt in range(2):
            if attempt:
                sleep(ATOM_RETRY_DELAY_S)
                retries += 1
            requests += 1
            outcome, resp, _ = fetch(atom_id_list_url(endpoint, chunk))
            halted = halt_reason(resp) if outcome is not None else None
            if halted is not None:
                assert resp is not None
                wait = retry_seconds(resp.headers, resp.body) if "retry-after" in resp.headers else None
                break
            if outcome is None and resp is not None and resp.status == 200:
                page = parse_atom_versions(resp.body.decode("utf-8", errors="replace"))
                if page == {} and len(chunk) > 1:
                    page = None
            if page is not None:
                break
        if halted is not None:
            limited = {a for rest in chunks[n:] for a in rest}
            failed.update(limited)
            return AtomSweep(versions=versions, failed=failed, requests=requests, retries=retries, halted=halted,
                             limited=frozenset(limited), retry_after=wait)
        if page is None:
            failed.update(chunk)
            continue
        versions.update(page)
    return AtomSweep(versions=versions, failed=failed, requests=requests, retries=retries, halted=halted)


def version_gate(atom: AtomVersion | None, held: set[str], *, withdrawn: bool, atom_failed: bool
                 ) -> tuple[str, int | None]:
    """``(gate, version)`` (v3's gate over the row's held revisions instead of ``fetch_state``):
    ``withdrawn`` / ``atom_unavailable`` / ``atom_unknown`` need no download; ``version_held``
    settles without one; ``never_fetched`` and ``version_changed`` download ``vN``."""
    if withdrawn:
        return "withdrawn", None
    if atom_failed:
        return "atom_unavailable", None
    if atom is None:
        return "atom_unknown", None
    if f"v{atom.version}" in held:
        return "version_held", atom.version
    return ("version_changed" if held else "never_fetched"), atom.version


def held_revisions(snapshots: Sequence[Mapping[str, Any]], source_id: str, content_role: str = "full_text") -> set[str]:
    """Revisions of the raw ``content_role`` snapshots held for ``source_id``."""
    return {str(s["revision"]) for s in snapshots
            if s.get("kind") == "raw" and s.get("source_id") == source_id
            and s.get("content_role") == content_role and s.get("revision")}


def html_remembered_until(role: Mapping[str, Any], url: str) -> dt.datetime | None:
    """When the "no HTML" memory of ``role`` for the versioned HTML ``url`` ends (module docstring),
    or ``None`` when the role remembers no absence of that version."""
    last = role.get("last_attempt_at")
    if role.get("status") != "empty" or role.get("validator_url") != url or last is None:
        return None
    return last + HTML_MEMORY


def withdrawal_notice(resp: HttpResponse | None, aid: str, version: int) -> bool:
    """``True`` when ``resp`` is arXiv's withdrawal page for exactly ``<aid>v<version>``: a 404 or
    410 whose body names that version in "This version of … has been withdrawn and is
    unavailable" (module docstring, step 5). Any other 404 is not."""
    if resp is None or resp.status not in (404, 410):
        return False
    m = _WITHDRAWN_PAGE.search(resp.body[:65536])
    return m is not None and m.group(1) == f"{aid}v{version}".encode()


def _withdrawn(env: LaneEnv, st: ArxivState, aid: str, url: str, patch: dict[str, Any]) -> FetchOutcome:
    st.withdrawn.add(aid)
    env.count("arxiv.withdrawn")
    return FetchOutcome("skipped", "withdrawn", f"{url}: arXiv: this version has been withdrawn", role_patch=patch)


def newest_listed(content: Mapping[str, Any]) -> int | None:
    """The newest version number in ``content.revisions`` (``None`` when nothing is listed)."""
    nums = [revision_number(str(r["revision"])) for r in content.get("revisions") or []]
    nums = [n for n in nums if n]
    return max(nums) if nums else None


def recheck_of(env: LaneEnv) -> dt.timedelta:
    """The re-check period of an ``ok`` arXiv role: the plan's ``acquire.recheck.default``."""
    acq = env.acquire
    return parse_duration(acq.recheck.default) if acq is not None else DEFAULT_RECHECK


def history_fresh(role: Mapping[str, Any], content: Mapping[str, Any], now: dt.datetime) -> bool:
    """v3's skip rule (``stack/metadata_history.py:105-110``: reuse when the history is complete,
    checked within 7 days, and the observed version equals the current capture's) on a v4 row: the
    role is ``ok``, its next check ``retry_after`` lies after ``now`` (stamped by the last check or
    the last ``ok`` episode, one recheck period later), and the newest revision in
    ``content.revisions`` (the observed version; absent until a listing completed) is held as a
    raw snapshot of the role's source (the current capture)."""
    if role["status"] != "ok":
        return False
    due = role.get("retry_after")
    if due is None or due <= now:
        return False
    listed = [str(r["revision"]) for r in content.get("revisions") or []]
    if not listed:
        return False
    observed = max(listed, key=lambda r: (revision_number(r), r))
    return observed in held_revisions(content["snapshots"], role["source_id"])


def conditional_headers(role: Mapping[str, Any] | None, url: str, *, backed: bool) -> dict[str, str]:
    """Validators are replayed only against the URL they were minted from (the versioned URL) and
    only when a raw snapshot of it is held (``backed``): a validator ``http`` stored for the bare
    URL, or one from an earlier version, is never replayed, so a 304 honestly means "this
    version, unchanged"."""
    if role is None or not backed or str(role.get("validator_url") or "") != url:
        return {}
    h: dict[str, str] = {}
    if role.get("etag"):
        h["If-None-Match"] = str(role["etag"])
    if role.get("last_modified"):
        h["If-Modified-Since"] = str(role["last_modified"])
    return h


# ─────────────────────────────────────────────────────────────────────────────
# The lane
# ─────────────────────────────────────────────────────────────────────────────
def _ask(env: LaneEnv, st: ArxivState, url: str, api: str, *, conditional: Mapping[str, str] | None = None
         ) -> tuple[str | None, HttpResponse | None, list[dict[str, Any]], str | None]:
    """One arXiv request through the run's session under D584's limit rule: a limited answer
    (:func:`halt_reason`) is waited out (:func:`limit_wait`, ``env.sleep``; counted
    ``arxiv.limited_waits.<api>`` and ``arxiv.limited_wait_s``) and the request asked again, until an
    answer that is not limited (which resets ``api``'s run of limited answers) or until
    :func:`give_up_reason` says stop. Returns the session's ``(outcome, response, history)`` over
    every try, and why it gave up (``None`` when it did not). Pacing stays the session's and the
    Gate's: a wait only ever adds time."""
    session = HttpSession.of(env)
    history: list[dict[str, Any]] = []
    while True:
        outcome, resp, hist = session.fetch_url(url, CHANNEL, conditional=conditional, wait_limits=False)
        history.extend(hist)
        why = halt_reason(resp)
        if why is None:
            if resp is not None and resp.status is not None:
                st.streak[api] = 0
            return outcome, resp, history, None
        assert resp is not None
        streak = st.streak[api] = st.streak.get(api, 0) + 1
        wait = limit_wait(streak, stated_retry_seconds(resp.headers, resp.body))
        left = None if env.deadline is None else env.deadline - env.monotonic()
        stop = give_up_reason(streak, wait, left)
        if stop is not None:
            return outcome, resp, history, f"{why} ({stop})"
        env.count(f"arxiv.limited_waits.{api}")
        env.count("arxiv.limited_wait_s", round(wait))
        env.sleep(wait)


def _fetch_for(env: LaneEnv, st: ArxivState, gave_up: list[str]) -> Fetch:
    """The Atom sweep's fetch: :func:`_ask` (D584); its give-up answer is passed on as the limited
    response (:func:`sweep_versions` stops on it) and the reason appended to ``gave_up``."""
    def fetch(url: str) -> tuple[str | None, HttpResponse | None, list[dict[str, Any]]]:
        outcome, resp, hist, why = _ask(env, st, url, ATOM)
        if why is not None:
            gave_up.append(why)
        return outcome, resp, hist
    return fetch


def _sweep(env: LaneEnv, st: ArxivState, ids: Sequence[str]) -> None:
    """Ask Atom for ``ids`` not known yet, in the given order (the run's order, D410). A limited
    answer is waited out and asked again (D584, :func:`_ask`); once that gives up, the sweeps stop
    for the rest of the run without halting the lane (D411): the ids left unanswered stay unknown
    (not ``unresolved``), and the episode answers them ``failed_transient[rate_limit]``, or probes
    the bare HTML URL for an ``html`` role (D585; :meth:`ArxivLane.fetch`)."""
    ask = list(dict.fromkeys(a for a in ids if a not in st.versions and a not in st.unresolved))
    if not ask or st.halted is not None or st.atom_limited is not None:
        return
    gave_up: list[str] = []
    sweep = sweep_versions(_fetch_for(env, st, gave_up), atom_endpoint(), ask, sleep=env.sleep)
    st.versions.update(sweep.versions)
    st.unresolved.update(sweep.failed - sweep.limited)
    env.count("arxiv.atom_requests", sweep.requests)
    if sweep.retries:
        env.count("arxiv.atom_retries", sweep.retries)
    if sweep.failed - sweep.limited:
        env.count("arxiv.atom_unresolved", len(sweep.failed - sweep.limited))
    if sweep.halted is not None:
        st.atom_limited = f"Atom API: {gave_up[-1] if gave_up else sweep.halted}"
        st.atom_hint = None if sweep.retry_after is None else env.now + dt.timedelta(seconds=sweep.retry_after)
        env.count("arxiv.atom_limited")


def _list(content: Row, atom: AtomVersion) -> None:
    """An Atom answer into ``content.revisions``, only where it adds something (module docstring, History 2)."""
    dated = {r["revision"] for r in content["revisions"] or [] if r.get("published_at") is not None}
    adds = [r for r in atom_revisions(atom) if r["revision"] not in dated]
    if adds:
        content["revisions"] = merge_revisions(content["revisions"], adds)


def _lookahead(st: ArxivState, aid: str) -> list[str]:
    """``aid`` and the next due ids of the run in fetch order not known yet (one Atom batch), so an
    episode that must sweep its own id asks for its successors too (D410)."""
    pos = st.ahead.get(aid)
    if pos is None:
        return [aid]
    nxt = [a for a in list(st.ahead)[pos + 1:] if a not in st.versions and a not in st.unresolved]
    return [aid, *nxt[:ATOM_BATCH - 1]]


def _halt(env: LaneEnv, st: ArxivState, why: str) -> None:
    if st.halted is None:
        st.halted = why
        env.count("arxiv.halted")


class ArxivLane:
    """``LANE``: the arXiv ``full_text`` episode and the arXiv history (module docstring)."""

    name = "arxiv"
    #: The skeleton re-checks an ``ok`` role of this lane like a ``url`` page (D22): ``retry_after
    #: = now + recheck``, which :func:`history_fresh` reads as "checked".
    recheck_ok = True

    @staticmethod
    def host_key(request: FetchRequest, env: LaneEnv) -> str:
        """The fetch group (``lanes.fetch_group``): every call of this lane is one arXiv group
        (Atom, PDFs and HTML share arXiv's one-request-per-3-s gate), so its state is touched
        by one thread at a time."""
        return ARXIV_GROUP

    # ── history ──────────────────────────────────────────────────────────────
    def history(self, rows: list[Row], env: LaneEnv) -> list[Row]:
        st = ArxivState.of(env)
        html = self._html_targets(rows, env) if structured_of(env.options).arxiv_html else {}
        targets: dict[str, list[tuple[Row, int]]] = {}
        for row in rows:
            if row["identity"]["is_masked"]:
                continue
            live = {m["source_id"]: m for m in row["identity"]["members"] if m["is_live"]}
            for i, role in enumerate(row["content"]["roles"]):
                member = live.get(role.get("source_id"))
                if (member is None or role.get("lane") != self.name or role["content_role"] != "full_text"
                        or role["status"] not in _HISTORY_STATUSES):
                    continue
                if history_fresh(role, row["content"], env.now):
                    env.count("arxiv.history_fresh")
                    continue
                aid = arxiv_id_of(member)
                if aid:
                    targets.setdefault(aid, []).append((row, i))
        # An html role that is due joins the batched sweep (its answer is listed as the PDF's is).
        # targets is in row order, i.e. the run's order (D410: the sweep asks the newest first under newest_first).
        due_html = [aid for aid, items in html.items() if aid not in targets and any(
            _due(row["content"]["roles"][i], env.now) for row, i in items)]
        if not targets and not due_html:
            self._html_history(html, env, st)
            return rows
        _sweep(env, st, [*targets, *due_html])
        recheck = recheck_of(env)
        for aid in sorted(targets):
            atom = st.versions.get(aid)
            if atom is None:
                continue
            current = f"v{atom.version}"
            for row, i in targets[aid]:
                c = row["content"]
                _list(c, atom)
                role = c["roles"][i]
                if role["status"] in ("ok", "empty") and (
                        aid in st.withdrawn or current not in held_revisions(c["snapshots"], role["source_id"])):
                    c["roles"][i] = {**role, "retry_after": env.now}  # due: the lane mints vN, or skips
                    env.count("arxiv.requeued")
                elif role["status"] == "ok" and current in {r["revision"] for r in c["revisions"] or []}:
                    c["roles"][i] = {**role, "retry_after": env.now + recheck}  # checked: vN held and listed
                    env.count("arxiv.history_checked")
        self._html_history(html, env, st)
        return rows

    def _html_targets(self, rows: list[Row], env: LaneEnv) -> dict[str, list[tuple[Row, int]]]:
        """The working rows' ``html`` roles of this lane (live member, a status history looks at),
        by arXiv id."""
        out: dict[str, list[tuple[Row, int]]] = {}
        for row in rows:
            if row["identity"]["is_masked"]:
                continue
            live = {m["source_id"]: m for m in row["identity"]["members"] if m["is_live"]}
            key_ids: dict[str, str] | None = None
            for i, role in enumerate(row["content"]["roles"]):
                member = live.get(role.get("source_id"))
                if (member is None or role.get("lane") != self.name or role["content_role"] != HTML_ROLE
                        or role["status"] not in _HISTORY_STATUSES):
                    continue
                key_ids = keys_arxiv_ids(row["identity"]) if key_ids is None else key_ids
                aid = arxiv_id_of({**member, KEY_ARXIV_ID: key_ids.get(role["source_id"], "")})
                if aid:
                    out.setdefault(aid, []).append((row, i))
        return out

    def _html_history(self, html: Mapping[str, list[tuple[Row, int]]], env: LaneEnv, st: ArxivState) -> None:
        """The history step over ``html`` roles (module docstring, LaTeXML HTML), after the sweep."""
        recheck = recheck_of(env)
        for aid in sorted(html):
            for row, i in html[aid]:
                c = row["content"]
                atom = st.versions.get(aid)
                if atom is not None:
                    _list(c, atom)
                role = c["roles"][i]
                version = atom.version if atom is not None else newest_listed(c)
                if version is None or (atom is None and _due(role, env.now)):
                    continue  # no fresh answer: the role keeps its state; if due, the lane asks (or probes, D585)
                url = arxiv_html_url(aid, version)
                until = html_remembered_until(role, url)
                if aid in st.withdrawn:
                    if role["status"] in ("ok", "empty") and not _due(role, env.now):
                        c["roles"][i] = {**role, "retry_after": env.now}  # the lane answers skipped[withdrawn]
                elif version <= max(map(revision_number, held_revisions(c["snapshots"], role["source_id"],
                                                                         HTML_ROLE)), default=0):  # or newer, D807
                    if role["status"] == "ok" and _due(role, env.now):
                        c["roles"][i] = {**role, "retry_after": env.now + recheck}
                        env.count("arxiv.html.checked")
                elif until is not None and until > env.now:
                    if role.get("retry_after") != until:
                        c["roles"][i] = {**role, "retry_after": until}
                    env.count("arxiv.html.remembered")
                elif role["status"] in ("ok", "empty") and not _due(role, env.now):
                    c["roles"][i] = {**role, "retry_after": env.now}  # vN not held, not remembered absent
                    env.count("arxiv.html.requeued")

    def prepare(self, requests_: list[FetchRequest], env: LaneEnv) -> None:
        """Remember the run's due arXiv ids in fetch order, for :func:`_lookahead` (D410)."""
        st = ArxivState.of(env)
        for req in requests_:
            aid = arxiv_id_of(req.member)
            if aid:
                st.ahead.setdefault(aid, len(st.ahead))

    # ── one role ─────────────────────────────────────────────────────────────
    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        html = request.content_role == HTML_ROLE and structured_of(env.options).arxiv_html
        if request.content_role != "full_text" and not html:
            return FetchOutcome("skipped", "not_requested",
                                f"the arXiv lane fetches full_text (and html with options.structured.arxiv_html), "
                                f"not {request.content_role!r}")
        aid = arxiv_id_of(request.member)
        if not aid:
            return FetchOutcome("failed_permanent", "no_fetchable_url",
                                f"member {request.source_id} carries no arXiv id")
        st = ArxivState.of(env)
        if aid not in st.versions and aid not in st.withdrawn:
            _sweep(env, st, _lookahead(st, aid))
        if st.halted is not None:
            return FetchOutcome("failed_transient", "rate_limit", st.halted, halt_run=True)
        if (st.atom_limited is not None and aid not in st.versions and aid not in st.unresolved
                and aid not in st.withdrawn):
            if html and st.unversioned_off is None:
                return self._html(request, env, st, aid, None)  # D585: the bare URL names its version
            env.count("arxiv.limited_roles")
            return FetchOutcome("failed_transient", "rate_limit", f"arXiv:{aid}: {st.atom_limited}",
                                retry_after_hint=st.atom_hint)  # D411: not counted (D484), no halt
        held = held_revisions(request.snapshots, request.source_id, request.content_role)
        atom = st.versions.get(aid)
        gate, version = version_gate(atom, held, withdrawn=aid in st.withdrawn,
                                     atom_failed=atom is None and aid in st.unresolved)
        if gate == "withdrawn":
            return FetchOutcome("skipped", "withdrawn", f"arXiv:{aid}: the current version is a withdrawal")
        if gate == "atom_unavailable" and html and st.unversioned_off is None:
            return self._html(request, env, st, aid, None)  # D585, as under the Atom limit
        if gate == "atom_unavailable":
            return FetchOutcome("failed_transient", "network", f"arXiv:{aid}: atom_unavailable")
        if gate == "atom_unknown":
            return until_cap(request.role, "network", "dead", f"arXiv:{aid}: atom_unknown (not in the feed)", {},
                             what="atom_unknown")
        assert version is not None
        if gate == "version_held":
            if html:
                env.count("arxiv.html.held")
            return FetchOutcome("ok")
        if html:
            return self._html(request, env, st, aid, version)
        return self._download(request, env, st, aid, version)

    @staticmethod
    def _html(request: FetchRequest, env: LaneEnv, st: ArxivState, aid: str, version: int | None) -> FetchOutcome:
        """The LaTeXML HTML of ``vN``: one bare probe (module docstring, LaTeXML HTML). With
        ``version`` ``None`` (Atom gave no answer for ``aid``, D585) the unversioned URL is asked
        and the answer's version read by :func:`html_version`."""
        session = HttpSession.of(env)
        url = arxiv_html_url(aid, version)
        outcome, resp, hist, gave_up = _ask(env, st, url, WEB)
        env.count("arxiv.html.requests", len(hist))
        if version is None:
            env.count("arxiv.html.unversioned")
        patch: dict[str, Any] = {"tried_urls": [url], "validator_url": url}
        status = resp.status if resp is not None else None
        if gave_up is not None:
            _halt(env, st, f"HTML: {gave_up}")
            return FetchOutcome("failed_transient", "rate_limit", f"{url}: {gave_up}", halt_run=True)
        if outcome is None and resp is not None:
            miss = markup_is_acceptable(resp.body, "html", status)
            if miss is None and version is None:
                version = html_version(resp, aid)
                if version is None:  # D585: no version, nothing minted; the bare URL is not asked again this run
                    st.unversioned_off = f"{url}: the page names no single version"
                    env.count("arxiv.html.unversioned_unresolved")
                    env.count("arxiv.limited_roles")
                    return FetchOutcome("failed_transient", "rate_limit",
                                        f"arXiv:{aid}: {st.atom_limited or 'atom_unavailable'}; {st.unversioned_off}",
                                        retry_after_hint=st.atom_hint)
                patch["validator_url"] = arxiv_html_url(aid, version)
                if f"v{version}" in held_revisions(request.snapshots, request.source_id, HTML_ROLE):
                    env.count("arxiv.html.held")
                    return FetchOutcome("ok", role_patch=patch)
            if miss is None:
                assert version is not None
                if len(resp.body) > session.max_bytes:
                    return FetchOutcome("failed_permanent", "too_large",
                                        f"{url}: {len(resp.body)} bytes over the {session.max_bytes}-byte cap",
                                        role_patch=patch)
                digest = env.blobs.put(resp.body)
                env.count("arxiv.html.downloads")
                snap = {"digest": digest, "bytes": len(resp.body),
                        "media_type": media_type_for(resp.body, resp.headers.get("content-type")), "url": url,
                        "final_url": resp.final_url, "http_status": status, "via": "live",
                        "etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified"),
                        "revision": f"v{version}"}
                return FetchOutcome("ok", snapshots=[snap], role_patch=patch)
            env.count(f"arxiv.html.unacceptable.{miss}")
            return FetchOutcome("empty", role_patch={**patch, "last_outcome": f"no_html:{miss}"})
        if version is None:
            m = _WITHDRAWN_PAGE.search(resp.body[:65536]) if resp is not None else None
            named = re.fullmatch(rf"{re.escape(aid)}v(\d+)", m.group(1).decode(errors="replace")) if m else None
            version = int(named.group(1)) if named else None
        if version is not None and withdrawal_notice(resp, aid, version):
            return _withdrawn(env, st, aid, url, patch)
        if status in (404, 410):
            env.count("arxiv.html.missing")
            return FetchOutcome("empty", role_patch={**patch, "last_outcome": f"no_html:http_{status}"})
        error = (f"{url}: HTTP {status}" if status is not None
                 else f"{url}: transport {resp.error}" if resp is not None else f"{url}: {outcome}")
        if outcome == "blocked" and status == 403:
            return forbidden(request.role, error, patch)
        if outcome in _PERMANENT:
            return FetchOutcome("failed_permanent", _PERMANENT[str(outcome)], error, role_patch=patch)
        return FetchOutcome("failed_transient", transient_class(resp), error, role_patch=patch)

    @staticmethod
    def _download(request: FetchRequest, env: LaneEnv, st: ArxivState, aid: str, version: int) -> FetchOutcome:
        session = HttpSession.of(env)
        url = arxiv_pdf_url(aid, version)
        backed = any(s.get("kind") == "raw" and s.get("url") == url for s in request.snapshots)
        outcome, resp, hist, gave_up = _ask(env, st, url, WEB,
                                            conditional=conditional_headers(request.role, url, backed=backed))
        env.count("arxiv.requests", len(hist))
        patch: dict[str, Any] = {"tried_urls": [url]}
        if outcome is None and resp is not None:
            headers = resp.headers
            validators = {"validator_url": url, "etag": headers.get("etag"),
                          "last_modified": headers.get("last-modified")}
            if resp.status == 304:
                return FetchOutcome("ok", role_patch={**patch, **validators, "last_outcome": "not_modified"})
            body = resp.body
            if not looks_like_pdf(body):
                # A 200 that is not a PDF: arXiv's "not available" page, a block page (v1's magic check);
                # transient until the cap (R4-2), as an interstitial is lifted later.
                return until_cap(request.role, "forbidden", "dead",
                                 f"{url}: HTTP 200 but not a PDF ({len(body)} bytes)", patch, what="200 non-PDF")
            if len(body) > session.max_bytes:
                return FetchOutcome("failed_permanent", "too_large",
                                    f"{url}: {len(body)} bytes over the {session.max_bytes}-byte cap", role_patch=patch)
            digest = env.blobs.put(body)
            env.count("arxiv.downloads")
            snap = {"digest": digest, "bytes": len(body), "media_type": media_type_for(body, headers.get("content-type")),
                    "url": url, "final_url": resp.final_url, "http_status": resp.status, "via": "live",
                    "etag": headers.get("etag"), "last_modified": headers.get("last-modified"),
                    "revision": f"v{version}"}
            return FetchOutcome("ok", snapshots=[snap], role_patch={**patch, **validators})
        what = (f"HTTP {resp.status}" if resp is not None and resp.status is not None
                else f"transport {resp.error}" if resp is not None else str(outcome))
        error = f"{url}: {what} ({len(hist)} request(s))"
        if withdrawal_notice(resp, aid, version):
            return _withdrawn(env, st, aid, url, patch)
        if gave_up is not None:
            _halt(env, st, f"PDF: {gave_up}")
            return FetchOutcome("failed_transient", "rate_limit", f"{error}: {gave_up}", halt_run=True)
        if outcome == "blocked" and resp is not None and resp.status == 403:
            return forbidden(request.role, error, patch)
        if outcome in _PERMANENT:
            return FetchOutcome("failed_permanent", _PERMANENT[str(outcome)], error, role_patch=patch)
        return FetchOutcome("failed_transient", transient_class(resp), error, role_patch=patch)


LANE = ArxivLane()

__all__ = [
    "ATOM_RETRY_DELAY_S", "DEFAULT_RECHECK", "HALT_STATUSES", "HTML_MEMORY", "HTML_ROLE", "LANE", "LIMIT_GIVE_UP",
    "LIMIT_WAIT_MAX_S", "LIMIT_WAIT_S", "ArxivLane", "ArxivState", "AtomSweep", "arxiv_id_of", "atom_endpoint",
    "conditional_headers", "give_up_reason", "halt_reason", "held_revisions", "history_fresh", "html_remembered_until",
    "html_version", "limit_wait", "newest_listed", "recheck_of", "sweep_versions", "version_gate",
]
