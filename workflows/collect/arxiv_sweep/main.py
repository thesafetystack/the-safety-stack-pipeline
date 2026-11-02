"""``collect.arxiv_sweep`` — the arXiv author sweep (§5.6 arXiv row; v1 ``search_arxiv.py``)
plus the keyword and scoped-category families (SR scope note, docs/scope/shallow-review.md (e)).

Sweeps the frontier-lab researcher roster against the arXiv Atom API: one date-windowed
``au:`` query per researcher, over a category allowlist (:data:`ARXIV_CATEGORIES`), verifying
each hit's author list against the target name (so distinct namesakes don't collide).

Two more query families run in the same stage (option ``families``, default all three:
``author``, ``kw``, ``cat``), each one date-windowed, ascending, paged query per item:

* **kw** — one query per concept group in the option ``keyword_groups`` (default
  :data:`DEFAULT_KEYWORD_GROUPS`, SR 2025's own arXiv sweep: "AI safety", "AI alignment",
  "steerability"): ``(ti:"t1" OR abs:"t1" OR ti:"t2" OR …)``, ANDed with the AI clause
  (:func:`ai_clause`) for a ``needs_ai_context`` group; no category allowlist (the phrase does
  the scoping). Coverage query ``kw:group:<id>``.
* **cat** — one query per category in the option ``scoped_categories`` (default
  :data:`DEFAULT_SCOPED_CATEGORIES`): ``cat:<c> AND (<AI clause>)``. Coverage query ``cat:<c>``.
  A hit is kept only when its title or abstract carries a safety stem (:data:`stack.rules.
  SAFETY_STEMS`) or a phrase of :data:`CATEGORY_KEEP_GROUPS` (umbrella and the SR-scope groups):
  the client-side filter of collection-yield.md C6 (Tomáš 2026-10-13; 25 of 64 kept on the
  2026-10-12 30-day window). Counted ``cat_kept`` / ``cat_dropped``; a dropped hit is not
  observed and not in the window's ``seen``.

No author check on these two families. Rows from all three families are the
same kind of row (channel ``arxiv``, same keys); a paper two queries return in one run is one
source (the harness keeps the first observation and counts ``sources_repeat``). Each row's
``metadata.matched_by`` lists every query of the run that returned it (D228: ``query_id``
``au:<Last_First>`` with the roster name as ``label``, ``kw:group:<id>`` with the group id,
``cat:<c>`` with the category). Every genuine hit is one
``sources`` row on the chain ``sources.arxiv_sweep`` (channel ``arxiv``, external id the bare
versionless arXiv id; keys typed by :mod:`stack.scholarly_records`: ``arxiv`` plus the DataCite
DOI ``10.48550/arxiv.<id>`` from the bridge), carrying what the Atom entry already held:
``metadata`` title / authors / abstract / ``published_at`` / ``doc_type = preprint``, the
``<summary>`` abstract as an in-band ``abstract`` entry (§5.4, v3 D-INBAND1; minted through the
harness into ``blobs/``), and the Atom ``<entry>`` element as received (its bytes sliced from the
feed, ``application/atom+xml``) as the row's ``provider_record`` in-band entry (D522; it carries the
arXiv ``categories``, which the channel's ``record_projection`` leaves out of the record digest). Every executed query writes ONE ``coverage`` row on
``coverage.arxiv_sweep`` (seen = name-matched hits, kept = hits new to the chain, the window
actually covered, and an honest outcome). No content fetch, no LLM. I/O is the collector harness
(:mod:`stack.collect.base`); this module keeps v3's provider logic.

A ``from`` moved earlier than a query's first window (2025-10-01 → 2024-11-30) leaves a hole
below its coverage (:meth:`stack.collect.base.Collector.holes`); each hole is asked first as a
bounded window ``submittedDate:[hole start TO hole end]`` (written ``[start, end]``, or its banked
prefix), then the usual tail from the watermark: only the gap is swept again (round 5 (RUN) B7a).

**High-volume names (C4; collection-yield.md §3.2, §9.3; round 5 (RUN) B7b).** A roster name on
the roster's ``disambiguate:`` list brings mostly namesakes or out-of-scope work (nine names, 46 %
of a 21-day sample). An author-family hit matched only by such names is kept only when its title or
abstract carries a safety stem (:func:`stack.rules.has_safety_stem`) or another author matches
another roster name (over the whole roster, not the run's sample); counted
``disambiguate_kept`` / ``disambiguate_dropped``, and a dropped hit is not observed and not in the
window's ``seen``. With the option ``disambiguate_by_id`` such a name that has ``openalex_ids`` in
the roster is not queried here at all (``author_queries_by_id``): ``collect.openalex``'s author-id
family (its option ``author_ids``) sweeps it by id instead. Without that option the filter applies
to every listed name, resolved or not.

Incremental windows (the v1 lesson): each researcher's query start date is the newest ``ok``
coverage row's ``window_end_at`` for that (channel, query) (:meth:`Collector.watermark`; only
``ok`` rows advance it). Pagination runs **ascending** by submitted date (D-SWEEP1), so a pass
stopped by the per-query cap still covered a contiguous prefix ``[from, newest_fetched]`` and
banks exactly that as an ``ok`` window ending at the frontier — the sweep walks forward run by
run and always claims only ground it exhausted. A pass that cannot advance (one day holding more
than the cap) is ``partial``; a failed page is ``failed_transient``; neither moves the watermark
(both are written over the zero-length window ``[from, from]``, so their ``attempts`` accumulate
on one key across runs). A window the cap stopped short of its end (today, or a hole's end), banked
prefix or stalled day, is counted ``queries_capped`` and the run ends ``scope_complete = false``
(build reason ``limit``, D471): the next run or pass resumes at the frontier.

**Announcement lag (D405).** The API lists a paper only once it is announced (the evening after
arXiv's 14:00 ET cutoff; a Friday-afternoon submission on Monday evening), while the window is in
``submittedDate``: a paper submitted before a run and announced after it lies below the next
run's watermark. The tail is therefore asked from ``max(floor, watermark − announce_lag_days)``
(option, default 4: a weekend), and its window is still written from the watermark, so coverage
stays contiguous and the D-SWEEP1 frontier must still pass the watermark. A hit submitted before
the watermark and unchanged writes no row (``skip_unchanged``, counted ``sources_unchanged``).
When the cap fills inside the look-back (the frontier does not pass the watermark), those hits are
kept and the query is asked again from the watermark (counted ``lookback_capped``), so the
look-back never stalls a query. A paper announced more than ``announce_lag_days`` after its
submission is still missed; a lower ``from`` (a hole) does not recover it either.

Pacing (D26): requests go through the harness transport (``HostPacer`` at ``1 / ARXIV_DELAY_S``,
plus ``provider_transport``'s cross-process arXiv ``Gate``); v3's explicit 3 s sleeps between
pages and queries are that pacer now. A failed page (non-200 or a garbled feed) gets ONE retry
after ``RETRY_DELAY_S`` (v3). A 429 does not halt the run (v3 never did; the ``Gate`` cooldown
fails later requests fast and each query records ``failed_transient``).

Options (plan stage line ``options:``, D72; the v3 env var wins when set):
  * ``roster``      / ``STACK_ARXIV_ROSTER``        — roster YAML (default ``configs/rosters/researchers.yaml``)
  * ``max_records`` / ``STACK_COLLECT_LIMIT``       — per-query result cap (default: the channel's
                                                      ``breadth`` preset ``max_results_per_query``)
  * ``max_queries`` / ``STACK_COLLECT_MAX_QUERIES`` — cap on researchers swept (0 = all)
  * ``fraction``    / ``STACK_COLLECT_FRACTION``    — stable hash-selected roster fraction (0, 1]
  * ``from``        / ``STACK_ARXIV_FROM``          — floor from-date for a query with no watermark
  * ``window_days``                                 — floor = today − N days when ``from`` is unset
    (else the channel's ``from_date``)
  * ``families``                                    — which query families run (default
                                                      ``[author, kw, cat]``)
  * ``keyword_groups``                              — concept-group ids of the kw family (default
                                                      ``[umbrella, steerability]``)
  * ``scoped_categories``                           — categories of the cat family (default
                                                      ``[cs.GT, cs.LO, math.LO, econ.TH, q-bio.NC]``)
  * ``disambiguate_by_id``                          — C4 (above): skip the ``au:`` query of a
                                                      ``disambiguate`` name with ``openalex_ids``
                                                      (default false)
  * ``announce_lag_days``                           — days below the watermark the tail re-asks
                                                      (default 4; 0 = none; D405, above)
``roster``, ``max_queries``, ``fraction`` and ``disambiguate_by_id`` apply to the author family only; ``max_records``,
``from``, ``window_days`` and ``announce_lag_days`` to every query.

Cardinal rule: imports only ``stack.*`` and the stdlib; nothing from other workflows.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stack.collect import roster as roster_mod
from stack.collect.base import RELIST_OPTIONS, Collector, Pacing, Transport
from stack.collect.inband import ATOM_MEDIA_TYPE
from stack.collect.roster import roster_name
from stack.collect.sampling import (
    ENV_FRACTION,
    ENV_MAX_QUERIES,
    SAMPLING_OPTIONS,
    select,
)
from stack.config import StackConfig
from stack.rules import has_safety_stem
from stack.scholarly_records import keys_for, normalize_arxiv
from stack.stage_runner import StageContext, StageResult

#: The ``options`` keys this stage's code reads; any other is refused (D434)
OPTIONS: frozenset[str] = frozenset({"announce_lag_days", "disambiguate_by_id", "families", "from", "keyword_groups", "max_records",
                                     "roster", "scoped_categories", "window_days"}) | SAMPLING_OPTIONS | RELIST_OPTIONS

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL = "arxiv"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ARXIV_PAGE_SIZE = 100
ARXIV_DATE_UPPER = "99991231235959"
ARXIV_DELAY_S = 3.0  # arXiv ToS etiquette: ~1 request / 3s (the transport's pacing)
RETRY_DELAY_S = 10.0  # longer cool-off before the one retry of a throttled/garbled page
DEFAULT_FROM = "2020-01-01"
# D405: arXiv lists a paper only once announced (the evening after the 14:00 ET cutoff; Friday
# afternoon to Monday evening over a weekend), so the tail re-asks this many days below the mark.
DEFAULT_ANNOUNCE_LAG_DAYS = 4
ENDPOINT = "https://export.arxiv.org/api/query"

# The scoped categories: game theory, logic, theoretical economics and neuroscience, where rostered
# authors' decision-theory, logic and game-theory papers sit (SR scope note (e)). The cat family
# queries each ANDed with the AI clause; the author sweep's allowlist includes them.
DEFAULT_SCOPED_CATEGORIES = ("cs.GT", "cs.LO", "math.LO", "econ.TH", "q-bio.NC")
# Author-sweep category allowlist — AI-safety work lives here, and this dodges the huge
# initial-listed physics/astro collaborations that otherwise collide on a common surname+initial
# ("E. Perez"); physics stays excluded. A code-level allowlist (v1 hardcoded it too); channels.yaml
# has no per-channel params slot.
ARXIV_CATEGORIES = ("cs.AI", "cs.LG", "cs.CL", "cs.CR", "cs.CY", "cs.MA", "stat.ML",
                    *DEFAULT_SCOPED_CATEGORIES)
_CAT_CLAUSE = "(" + " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES) + ")"
# The kw family's default groups: SR 2025's arXiv sweep ("AI alignment", "AI safety" sit in
# `umbrella`; "steerability" has a group of its own).
DEFAULT_KEYWORD_GROUPS = ("umbrella", "steerability")
#: C6 (collection-yield.md §9.3, Tomáš 2026-10-13): the concept groups whose phrases (``terms``)
#: keep a cat-family hit besides a safety stem: ``umbrella`` and the 20 SR-scope groups
#: (channels.yaml's 2026-10-12 SR extension block).
CATEGORY_KEEP_GROUPS = (
    "umbrella", "decision-theory-af", "decision-theory", "acausal", "anthropics", "infra-bayes",
    "natural-abstractions", "tiling", "program-equilibrium", "game-theory-ai",
    "gradual-disempowerment", "alignment-targets", "social-choice-ai", "model-psychology",
    "introspection", "agi-metrics", "heuristic-arguments", "brain-like-agi", "scientist-ai",
    "steerability", "shard-theory")
FAMILIES = ("author", "kw", "cat")
# Abstract phrases the AI clause adds to channels.yaml `ai_context` (which names models, not
# agents): decision-theory, game-theory and anthropics papers about AI speak of agents and AGI.
AI_CLAUSE_EXTRA = ("artificial agents", "AI agents", "AI alignment", "superintelligence", "AGI")
_CATEGORY_RE = re.compile(r"^[a-z-]+(\.[A-Za-z-]+)?$")

# A browser-like UA (the sandbox proxy + arXiv are friendlier to one than to a bare urllib UA).
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36 stack-collect/1.0 (+research)"
)

#: v3's constants as the harness transport's pacing: one request per ARXIV_DELAY_S per host; no
#: transport-level retry ladder (fetch_author's one retry after RETRY_DELAY_S is the ladder);
#: a 429 never halts the run.
PACING = Pacing(rps=1.0 / ARXIV_DELAY_S, retry_delays_s=(), headers={"User-Agent": BROWSER_UA})

_ENV_ROSTER = "STACK_ARXIV_ROSTER"
_ENV_LIMIT = "STACK_COLLECT_LIMIT"
_ENV_FROM = "STACK_ARXIV_FROM"
_DEFAULT_LIMIT = 100


# ─────────────────────────────────────────────────────────────────────────────
# Author-name matching (ported behaviour from v1 search_arxiv.py; do-not-copy-code note)
# ─────────────────────────────────────────────────────────────────────────────
def _norm_name(s: str) -> str:
    """Fold diacritics, lowercase, collapse whitespace (stdlib re-implementation of v1's
    ``corpus.norm_name``)."""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def _first_alpha(s: str) -> str:
    return re.sub(r"[^a-z]", "", _norm_name(s))[:1]


def _is_initial(s: str) -> bool:
    """True if a first-name token is just an initial (e.g. 'E', 'E.')."""
    return len(re.sub(r"[^a-z]", "", _norm_name(s))) <= 1


def _name_parts(name: str) -> dict[str, str]:
    name = roster_name(name)
    parts = name.split()
    if not parts:
        return {}
    return {"first": parts[0], "last": parts[-1], "normalized": _norm_name(name)}


def names_match(target_name: str, paper_author: str) -> bool:
    """Robust author match (v1 tightening): last names must match, then match on first initial
    when either side gives only an initial, else require the full first names to match. Keeps
    recall for initial-listed authors ("E. Perez") while dropping surname+initial collisions
    ("Estefania Perez" vs "Ethan Perez"). Either side may be ``Last, First`` (:func:`roster_name`)."""
    target = _name_parts(target_name)
    author = _name_parts(paper_author)
    if not target or not author:
        return False
    if target["normalized"] == author["normalized"]:
        return True
    if _norm_name(target["last"]) != _norm_name(author["last"]):
        return False
    tf, af = target["first"], author["first"]
    if not tf or not af:
        return True
    if _is_initial(tf) or _is_initial(af):
        return _first_alpha(tf) == _first_alpha(af)
    return _norm_name(tf) == _norm_name(af)


def author_in_list(paper_authors: list[str], target: str) -> bool:
    return any(names_match(target, a) for a in paper_authors)


def arxiv_author_query(name: str) -> str:
    """The arXiv ``au:`` token for a researcher: ``Lastname_Firstname`` (full first name — the
    ``Lastname_Firstinitial`` form matches almost nothing on arXiv's author index). A single-token
    name passes through unchanged; ``Last, First`` is read as :func:`roster_name` does."""
    parts = roster_name(name).replace(",", " ").split()
    if len(parts) >= 2:
        return f"{parts[-1]}_{parts[0]}"
    return name.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Roster
# ─────────────────────────────────────────────────────────────────────────────
def _roster_path(config: StackConfig) -> Path:
    return roster_mod.default_path(config.config_dir)


def load_roster_full(config: StackConfig, path: Path | None = None) -> roster_mod.Roster:
    """The roster (``path``, else ``STACK_ARXIV_ROSTER``, else ``configs/rosters/researchers.yaml``)
    as :func:`stack.collect.roster.load_roster_file` reads it: entries with their OpenAlex ids, and
    the ``disambiguate`` names (C4)."""
    return roster_mod.load_roster_file(path or _roster_path(config))


def load_roster(config: StackConfig, path: Path | None = None) -> list[str]:
    """Load + normalise the researcher roster (``path``, else ``STACK_ARXIV_ROSTER``, else
    ``configs/rosters/researchers.yaml``): a deterministic, de-duplicated, sorted name list, each
    name in ``First Last`` order (:func:`roster_name`, so a ``Last, First`` entry and its
    ``First Last`` twin are one name). Entries are bare strings or ``{name, openalex_ids?,
    orcid?}`` mappings (:mod:`stack.collect.roster`)."""
    return load_roster_full(config, path).names


# ─────────────────────────────────────────────────────────────────────────────
# C4: high-volume names (collection-yield.md §3.2, §9.3; round 5 (RUN) B7b)
# ─────────────────────────────────────────────────────────────────────────────
def roster_index(names: list[str]) -> dict[str, list[str]]:
    """Roster names by normalised last name (the key :func:`names_match` requires to agree)."""
    index: dict[str, list[str]] = {}
    for n in names:
        parts = _name_parts(n)
        if parts:
            index.setdefault(_norm_name(parts["last"]), []).append(n)
    return index


def other_roster_author(authors: list[str], own: list[str], index: dict[str, list[str]]) -> bool:
    """A paper author that is not one of ``own`` (the query's roster names) matches another roster
    name: "a second roster author"."""
    for a in authors:
        if any(names_match(n, a) for n in own):
            continue
        parts = _name_parts(a)
        if parts and any(names_match(r, a) for r in index.get(_norm_name(parts["last"]), ())
                         if r not in own):
            return True
    return False


def keep_disambiguated_hit(entry: dict[str, Any], own: list[str],
                           index: dict[str, list[str]]) -> bool:
    """C4: a hit of a ``disambiguate`` name is kept only with a safety stem in its title or
    abstract (:func:`stack.rules.has_safety_stem`) or a second roster author."""
    text = f"{entry.get('title') or ''}\n{entry.get('abstract') or ''}"
    return has_safety_stem(text) or other_roster_author(list(entry.get("authors") or []), own, index)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP (module-level seams: tests monkeypatch ``http_get`` / ``_sleep`` offline)
# ─────────────────────────────────────────────────────────────────────────────
#: The run's paced transport (set by :func:`run`); ``http_get`` goes through it.
_T: Transport | None = None


def http_get(url: str, params: dict[str, Any], *, timeout: float = 60.0) -> tuple[int, str]:
    """One paced GET through the run's transport; returns ``(status, body_text)``. A transport
    failure returns ``(0, "")`` so the caller records an honest failed window instead of crashing
    the sweep. ``timeout`` is kept for the seam's signature; the transport's
    ``PACING.timeout_s`` applies."""
    if _T is None:
        raise RuntimeError("arxiv_sweep.http_get: no transport outside run(ctx)")
    resp = _T.get(url, params)
    if resp.status is None:
        return 0, ""
    return resp.status, resp.body.decode("utf-8", "replace")


def _sleep(seconds: float) -> None:
    """Politeness and retry delays (module-level so offline tests monkeypatch it to a no-op);
    the transport's pacer sleeps through it too."""
    time.sleep(seconds)


# ─────────────────────────────────────────────────────────────────────────────
# Atom parsing
# ─────────────────────────────────────────────────────────────────────────────
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def parse_entries(xml_text: str) -> list[dict[str, Any]] | None:
    """Parse an arXiv Atom feed → ``[{arxiv_id, title, authors, abstract, pub_date, categories}]``
    (``arxiv_id`` in the ``arxiv`` key's normal form, :func:`stack.scholarly_records.normalize_arxiv`:
    bare, versionless, lower case). Returns ``None`` on an unparseable body (arXiv throttling
    sends empty/garbled responses) so the caller can tell 'feed broken' (error) from a genuine
    end-of-results (empty list).

    That distinction is the whole point of the return type, and XML well-formedness alone does not
    establish it: an interstitial served in place of the feed — a proxy's ``<html>403
    Forbidden</html>``, a CDN error page — parses perfectly happily and simply contains no
    ``atom:entry`` elements. Read as an empty list it is indistinguishable from "this author has
    no more papers", and :func:`fetch_author` banks ``complete`` for a page it never received,
    permanently advancing the author's watermark past work it never saw. So the ROOT ELEMENT is
    checked too: anything that is not an Atom ``feed`` is a broken body, not an empty one."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != f"{{{ARXIV_NS['atom']}}}feed":
        return None
    out: list[dict[str, Any]] = []
    elements = root.findall("atom:entry", ARXIV_NS)
    raws = entry_bytes(xml_text, len(elements))
    for entry, raw in zip(elements, raws or [None] * len(elements), strict=True):
        id_elem = entry.find("atom:id", ARXIV_NS)
        arxiv_id = normalize_arxiv((id_elem.text or "") if id_elem is not None else "")
        if not arxiv_id:
            continue
        title_elem = entry.find("atom:title", ARXIV_NS)
        summary_elem = entry.find("atom:summary", ARXIV_NS)
        pub_elem = entry.find("atom:published", ARXIV_NS)
        primary = entry.find("arxiv:primary_category", ARXIV_NS)
        cats = [str(c.get("term")) for c in entry.findall("atom:category", ARXIV_NS) if c.get("term")]
        if primary is not None and primary.get("term"):
            cats = [str(primary.get("term")), *cats]
        authors = [
            a.find("atom:name", ARXIV_NS).text  # type: ignore[union-attr]
            for a in entry.findall("atom:author", ARXIV_NS)
            if a.find("atom:name", ARXIV_NS) is not None
        ]
        title = " ".join((title_elem.text or "").split()) if title_elem is not None else ""
        abstract = " ".join((summary_elem.text or "").split()) if summary_elem is not None else ""
        pub_date = None
        if pub_elem is not None and pub_elem.text:
            m = _ISO_DATE.match(pub_elem.text.strip())
            if m:
                pub_date = m.group(1)
        out.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": [a for a in authors if a],
                "abstract": abstract,
                "pub_date": pub_date,
                "categories": list(dict.fromkeys(cats)),
                "atom": raw if raw is not None else ET.tostring(entry, encoding="utf-8"),
            }
        )
    return out


_ENTRY = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?entry\b.*?</(?:[A-Za-z_][\w.-]*:)?entry\s*>", re.S)


def entry_bytes(xml_text: str, n: int) -> list[bytes] | None:
    """The feed's ``n`` ``<entry>`` elements as received (UTF-8 bytes sliced from the body, in order), or
    ``None`` when the slices do not line up with the parsed entries (then each entry is re-serialised).
    The ``provider_record`` of an arXiv row (D522)."""
    found = [m.group(0).encode("utf-8") for m in _ENTRY.finditer(xml_text)]
    return found if len(found) == n else None


def _fetch_page(endpoint: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str]:
    """One page fetch+parse → ``(entries, error_class)``. ``entries`` is the parsed list (possibly
    empty = genuine end-of-results) on success, or ``None`` on a failed page — a non-200 status
    or an unparseable/garbled feed — with the transient ``error_class`` it maps to (``network``
    for a transport failure or a garbled body, ``rate_limit`` for 429/403, ``http_5xx``)."""
    status, body = http_get(endpoint, params)
    if status != 200:
        if status in (403, 429):
            return None, "rate_limit"
        return None, "http_5xx" if status >= 500 else "network"
    entries = parse_entries(body)
    return entries, "" if entries is not None else "network"


def fetch_author(
    query_name: str,
    from_compact: str,
    limit: int,
    endpoint: str = ENDPOINT,
    *,
    stats: dict[str, Any] | None = None,
    on_page: Callable[[int], None] | None = None,
    to_compact: str = ARXIV_DATE_UPPER,
) -> tuple[list[dict[str, Any]], str]:
    """One author query (``au:<query_name>`` over :data:`ARXIV_CATEGORIES`) through
    :func:`fetch_query`."""
    return fetch_query(f"au:{query_name} AND {_CAT_CLAUSE}", from_compact, limit, endpoint,
                       stats=stats, on_page=on_page, to_compact=to_compact)


def fetch_query(
    base_query: str,
    from_compact: str,
    limit: int,
    endpoint: str = ENDPOINT,
    *,
    stats: dict[str, Any] | None = None,
    on_page: Callable[[int], None] | None = None,
    to_compact: str = ARXIV_DATE_UPPER,
) -> tuple[list[dict[str, Any]], str]:
    """Page through arXiv for one query (``base_query AND submittedDate:[from TO to]``, ``to``
    open unless a hole below the coverage is swept) up to ``limit`` entries. Returns
    ``(entries, exhausted)`` with ``exhausted`` ∈ {complete, truncated, error} (v3 receipts,
    :data:`stack.collect.base.EXHAUSTED`): ``complete`` = the API returned every result before
    the cap; ``truncated`` = the per-run cap stopped pagination while more may exist;
    ``error`` = the feed failed/garbled mid-pull (``stats['error_class']`` names why).

    A failed page (non-200 or unparseable feed) gets ONE retry after a longer ``RETRY_DELAY_S``
    cool-off — under arXiv throttling ~32% of author pages came back garbled, and a single retry
    recovers most, keeping the author's watermark advancing instead of stalling the whole run.
    Retries are tallied into ``stats['retries']`` when a ``stats`` dict is passed. Pacing between
    requests is the transport's (``PACING``).

    Pagination is **ASCENDING** by submitted date (D-SWEEP1, 2026-07-22). Descending order made
    a capped pass unrecoverable: it returned the NEWEST ``limit`` results, so the covered window
    had a hole in the middle, the watermark could never advance, and every later run re-fetched
    the same newest page while the older tail stayed permanently invisible. Ascending order makes
    a capped pass return a contiguous prefix ``[from, last_entry_date]`` — a genuinely exhausted
    sub-window the caller can bank (see :func:`covered_window`).

    ``on_page`` (when given) is called after each fetched page with the running entry total."""
    entries: list[dict[str, Any]] = []
    start = 0
    while True:
        remaining = limit - len(entries)
        if remaining <= 0:
            return entries, "truncated"  # cap reached; cannot claim completeness
        page = min(ARXIV_PAGE_SIZE, remaining)
        params = {
            "search_query": (
                f"{base_query} AND submittedDate:[{from_compact} TO {to_compact}]"
            ),
            "start": start,
            "max_results": page,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }
        batch, error_class = _fetch_page(endpoint, params)
        if batch is None:
            # One retry after a longer cool-off.
            _sleep(RETRY_DELAY_S)
            if stats is not None:
                stats["retries"] = stats.get("retries", 0) + 1
            batch, error_class = _fetch_page(endpoint, params)
            if batch is None:
                if stats is not None:
                    stats["error_class"] = error_class
                return entries, "error"
        if not batch:
            return entries, "complete"
        entries.extend(batch)
        start += len(batch)
        if on_page is not None:
            on_page(len(entries))
        if len(batch) < page:
            return entries, "complete"


def plan_author_queries(roster: list[str],
                        disambiguate: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The author family's plan: one query per ``au:`` token, in roster order, ``{family, query
    (the token, the coverage query), query_id au:<token>, names, label, disambiguate (the
    query's names on the roster's C4 list)}``. Roster names that share a token (``Adam Jermyn`` /
    ``Adam S. Jermyn``) are one query matched against each name: two windows on one key in one run
    is a harness error (D229)."""
    by_token: dict[str, list[str]] = {}
    for n in roster:
        token = arxiv_author_query(n)
        if token:
            by_token.setdefault(token, []).append(n)
    return [{"family": "author", "query": t, "query_id": f"au:{t}", "names": names,
             "label": " | ".join(names), "disambiguate": [n for n in names if n in disambiguate]}
            for t, names in by_token.items()]


# ─────────────────────────────────────────────────────────────────────────────
# Keyword and scoped-category queries
# ─────────────────────────────────────────────────────────────────────────────
def _phrases(terms: list[str] | tuple[str, ...]) -> list[str]:
    """Usable phrases, order kept, duplicates (case-insensitive) dropped; a term with a double
    quote would break out of the quoting and change the query, so it is dropped."""
    out: list[str] = []
    for t in terms:
        t = " ".join(str(t).split())
        if t and '"' not in t and t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out


def ai_clause(ai_context: list[str]) -> str:
    """The AI clause: ``abs:"p1" OR abs:"p2" OR …`` over channels.yaml ``ai_context`` plus
    :data:`AI_CLAUSE_EXTRA` (no parentheses; callers group it)."""
    return " OR ".join(f'abs:"{p}"' for p in _phrases([*ai_context, *AI_CLAUSE_EXTRA]))


def keyword_query(terms: list[str], ai: str | None) -> str:
    """The kw family's query for one concept group: each phrase in title or abstract, OR'd;
    ANDed with ``(ai)`` when given (a ``needs_ai_context`` group). '' when no term is usable."""
    phrases = _phrases(terms)
    if not phrases:
        return ""
    q = "(" + " OR ".join(f'ti:"{t}" OR abs:"{t}"' for t in phrases) + ")"
    return f"{q} AND ({ai})" if ai else q


def category_query(category: str, ai: str) -> str:
    """The cat family's query for one category: ``cat:<c> AND (<AI clause>)``."""
    return f"cat:{category} AND ({ai})"


def _str_list(value: Any) -> list[str]:
    items = [value] if isinstance(value, str) else list(value)
    return [str(v).strip() for v in items if str(v).strip()]


def plan_keyword_queries(config: StackConfig, group_ids: list[str]) -> list[dict[str, Any]]:
    """The kw family's plan, in ``group_ids`` order: ``{family, query (the coverage query
    kw:group:<id>), search}``. An id that is not a concept group is a ``ValueError``; a group
    with no usable term is left out."""
    groups = {g.id: g for g in config.channels.concept_groups}
    unknown = sorted(set(group_ids) - set(groups))
    if unknown:
        raise ValueError(f"option keyword_groups: {unknown} are not concept groups")
    ai = ai_clause(list(config.channels.ai_context))
    plan: list[dict[str, Any]] = []
    for gid in dict.fromkeys(group_ids):
        g = groups[gid]
        search = keyword_query(g.terms, ai if g.needs_ai_context else None)
        if search:
            plan.append({"family": "kw", "query": f"kw:group:{gid}", "search": search,
                         "label": gid})
    return plan


def plan_category_queries(config: StackConfig, categories: list[str]) -> list[dict[str, Any]]:
    """The cat family's plan: ``{family, query (cat:<c>), search}`` per category. A string that
    is not shaped like an arXiv category (``cs.GT``, ``econ.TH``, ``math.LO``) is a ``ValueError``."""
    bad = sorted(c for c in categories if not _CATEGORY_RE.match(c))
    if bad:
        raise ValueError(f"option scoped_categories: {bad} are not arXiv category names")
    ai = ai_clause(list(config.channels.ai_context))
    return [{"family": "cat", "query": f"cat:{c}", "search": category_query(c, ai), "label": c}
            for c in dict.fromkeys(categories)]


def category_keep_regex(config: StackConfig,
                        group_ids: tuple[str, ...] = CATEGORY_KEEP_GROUPS) -> re.Pattern[str]:
    """C6: one case-insensitive regex over the ``terms`` of ``group_ids`` (each a whole phrase,
    whitespace-flexible); an id that is not a concept group is a ``ValueError``."""
    groups = {g.id: g for g in config.channels.concept_groups}
    unknown = sorted(set(group_ids) - set(groups))
    if unknown:
        raise ValueError(f"CATEGORY_KEEP_GROUPS: {unknown} are not concept groups")
    phrases = _phrases([t for gid in group_ids for t in groups[gid].terms])
    alts = "|".join(r"\s+".join(map(re.escape, p.split())) for p in phrases)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def keep_category_hit(entry: dict[str, Any], phrase_re: re.Pattern[str]) -> bool:
    """C6: a cat-family hit is kept when its title or abstract has a safety stem
    (:func:`stack.rules.has_safety_stem`) or a :func:`category_keep_regex` phrase."""
    text = f"{entry.get('title') or ''}\n{entry.get('abstract') or ''}"
    return has_safety_stem(text) or phrase_re.search(text) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Coverage window helpers
# ─────────────────────────────────────────────────────────────────────────────
def _as_of_date() -> str:
    """Today's date (ISO) — the upper bound a COMPLETE sweep covers. Module-level so tests can
    monkeypatch a fixed 'now'; :func:`run` passes the harness clock's date instead."""
    return dt.datetime.now(dt.UTC).date().isoformat()


def _to_compact(iso_date: str) -> str:
    return iso_date.replace("-", "") + "0000"  # YYYYMMDDHHMM


def covered_window(
    entries: list[dict[str, Any]], exhausted: str, eff_from: str, *, today: str | None = None
) -> tuple[str, str]:
    """The window this pass ACTUALLY covered → ``(window_to, exhausted)`` (D-SWEEP1).

    * ``complete`` — the API ran out of results: covered through today.
    * ``truncated`` — the cap stopped an ASCENDING pull, so ``[eff_from, newest_fetched]`` is
      contiguous and genuinely exhausted. Bank it as ``complete`` with a partial ``window_to``
      (``to < today`` is what marks the sweep as not yet caught up) so the next run resumes from
      there instead of re-fetching the same page forever. Two guards keep this honest: the
      frontier must be a real date that actually ADVANCES past ``eff_from`` (a single day holding
      more than ``limit`` submissions cannot be split, so it stays ``truncated`` and stalls
      loudly), and it is clamped to today (never trust a future date from the feed).
    * ``error`` — nothing is claimed; the window does not move.
    """
    today = today or _as_of_date()
    if exhausted == "complete":
        return today, "complete"
    if exhausted != "truncated":
        return eff_from, exhausted
    dates = [str(e["pub_date"]) for e in entries if e.get("pub_date")]
    if not dates:
        return eff_from, "truncated"
    frontier = min(max(dates), today)
    if frontier <= eff_from:
        return eff_from, "truncated"  # cannot split a single day — stall honestly
    return frontier, "complete"


# ─────────────────────────────────────────────────────────────────────────────
# The stage
# ─────────────────────────────────────────────────────────────────────────────
def _floor_from(c: Collector, chan_from: str | None, today: dt.date) -> str:
    """The from-date of a query with no watermark: ``from`` (env ``STACK_ARXIV_FROM`` wins),
    else today − ``window_days``, else the channel's ``from_date``, else ``DEFAULT_FROM``."""
    explicit = c.option("from", None, env=_ENV_FROM, cast=str)
    if explicit:
        return dt.date.fromisoformat(explicit.strip()[:10]).isoformat()
    days = c.option("window_days", None, cast=int)
    if days:
        return (today - dt.timedelta(days=days)).isoformat()
    return (chan_from or DEFAULT_FROM).strip()


def _families(c: Collector) -> list[str]:
    families = _str_list(c.option("families", list(FAMILIES)))
    unknown = sorted(set(families) - set(FAMILIES))
    if unknown:
        raise ValueError(f"option families: {unknown} are not query families {list(FAMILIES)}")
    return families


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def _disambiguate(matched: list[dict[str, Any]], item: dict[str, Any],
                  index: dict[str, list[str]], tally: dict[str, int]) -> list[dict[str, Any]]:
    """C4: drop a hit whose matching roster names are all on the ``disambiguate`` list and which
    has neither a safety stem nor a second roster author (:func:`keep_disambiguated_hit`);
    counted ``disambiguate_kept`` / ``disambiguate_dropped``. A dropped hit is not observed and
    not in the window's ``seen`` (as the cat family's C6 filter)."""
    keep: list[dict[str, Any]] = []
    for e in matched:
        by = [n for n in item["names"] if author_in_list(e["authors"], n)]
        if all(n in item["disambiguate"] for n in by) and not keep_disambiguated_hit(
                e, item["names"], index):
            tally["disambiguate_dropped"] += 1
            continue
        tally["disambiguate_kept"] += 1
        keep.append(e)
    return keep


def run(ctx: StageContext) -> StageResult:
    global _T
    from stack.config import load

    c = Collector(ctx)
    config = load()
    chan = c.channel(CHANNEL)
    # Prefer HTTPS so an egress proxy routes it; arXiv's Atom API serves the same over https.
    endpoint = (chan.location or ENDPOINT).replace("http://", "https://", 1)
    today_d = c.now.date()
    today = today_d.isoformat()
    floor_from = _floor_from(c, chan.from_date, today_d)
    configured = config.channels.max_results_per_query(CHANNEL, _DEFAULT_LIMIT)
    limit = max(1, c.option("max_records", configured, env=_ENV_LIMIT, cast=int))
    families = _families(c)
    lag = c.option("announce_lag_days", DEFAULT_ANNOUNCE_LAG_DAYS, cast=int)
    if lag < 0:
        raise ValueError(f"option announce_lag_days: {lag} < 0")

    # The query plan, family by family: {family, query (the coverage query), search | name}.
    queries: list[dict[str, Any]] = []
    planned = selected = 0
    full_roster = None
    by_id = 0
    if "author" in families:
        roster_opt = c.option("roster", None, env=_ENV_ROSTER, cast=str)
        full_roster = load_roster_full(config, Path(roster_opt) if roster_opt else None)
        roster = full_roster.names
        planned = len(roster)
        if _truthy(c.option("disambiguate_by_id", False, cast=str)):
            # C4 (option): a disambiguate name with resolved OpenAlex ids is left to
            # collect.openalex's author-id family; its au: query is not asked.
            handed = {n for n in full_roster.disambiguate if full_roster.ids_of(n)}
            by_id = len(handed)
            roster = [n for n in roster if n not in handed]
        # Scale control: plan options feed sampling.select's env knobs (a set env var wins).
        env = dict(os.environ)
        for name, var in (("fraction", ENV_FRACTION), ("max_queries", ENV_MAX_QUERIES)):
            value = c.option(name, None, env=var, cast=str)
            if value is not None:
                env[var] = value
        roster, sampling = select(roster, lambda n: f"{CHANNEL}:{n}", env=env)
        selected = int(sampling["selected"])
        queries += plan_author_queries(roster, full_roster.disambiguate)
    # C4: "a second roster author" is any other name of the WHOLE roster (not the sample).
    index = roster_index(full_roster.names) if full_roster is not None else {}
    if "kw" in families:
        groups = _str_list(c.option("keyword_groups", list(DEFAULT_KEYWORD_GROUPS)))
        queries += plan_keyword_queries(config, groups)
    keep_re = None
    if "cat" in families:
        cats = _str_list(c.option("scoped_categories", list(DEFAULT_SCOPED_CATEGORIES)))
        queries += plan_category_queries(config, cats)
        keep_re = category_keep_regex(config)

    _T = c.transport(PACING, sleep=lambda s: _sleep(s))
    api_stats: dict[str, Any] = {"retries": 0}
    tally = {"queries_complete": 0, "queries_truncated": 0, "queries_error": 0, "caught_up": 0,
             "hits_fetched": 0, "hits_matched": 0, "hits_new": 0, "cat_kept": 0, "cat_dropped": 0,
             "disambiguate_kept": 0, "disambiguate_dropped": 0, "lookback_capped": 0,
             "queries_capped": 0}
    relist = c.relist_after()
    for f in FAMILIES:
        tally[f"queries_{f}"] = tally[f"hits_{f}"] = 0
    observed: set[str] = set()
    tally["holes"] = 0

    def pull(item: dict[str, Any], label: str, ask_from: str, upper: str | None
             ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        """Fetch the query from ``ask_from`` (to ``upper``, else open) and filter its hits →
        ``(entries, exhausted, matched)``."""
        api_stats.pop("error_class", None)
        to_c = _to_compact(upper) if upper is not None else ARXIV_DATE_UPPER
        if item["family"] == "author":
            entries, exhausted = fetch_author(label, _to_compact(ask_from), limit, endpoint,
                                              stats=api_stats, to_compact=to_c)
            matched = [e for e in entries
                       if any(author_in_list(e["authors"], n) for n in item["names"])]
            if item.get("disambiguate"):
                matched = _disambiguate(matched, item, index, tally)  # C4
        else:
            entries, exhausted = fetch_query(item["search"], _to_compact(ask_from), limit,
                                             endpoint, stats=api_stats, to_compact=to_c)
            matched = entries  # the phrase / category + AI clause did the scoping
            if item["family"] == "cat":
                assert keep_re is not None
                matched = [e for e in entries if keep_category_hit(e, keep_re)]  # C6
                tally["cat_kept"] += len(matched)
                tally["cat_dropped"] += len(entries) - len(matched)
        tally["hits_fetched"] += len(entries)
        return entries, exhausted, matched

    def record(item: dict[str, Any], label: str, matched: list[dict[str, Any]],
               eff_from: str) -> int:
        """Observe ``matched``; returns how many are new to the chain. A hit submitted before
        ``eff_from`` (the announce-lag overlap, D405) and unchanged writes no row."""
        new = 0
        for e in matched:
            aid = str(e["arxiv_id"])
            if c.prior(CHANNEL, aid) is None and aid not in observed:
                new += 1
            observed.add(aid)
            row = c.observe(
                CHANNEL, aid, e,
                keys=[(k["kind"], k["value"]) for k in keys_for([("arxiv", aid)])],
                canonical_url=f"https://arxiv.org/abs/{aid}",
                metadata={"title": e["title"] or None, "abstract": e["abstract"] or None,
                          "authors": list(e["authors"]) or None, "published_at": e["pub_date"],
                          "doc_type": "preprint"},
                provider_record=e["atom"], record_media_type=ATOM_MEDIA_TYPE,  # D522
                skip_unchanged=str(e["pub_date"] or "") < eff_from,
                query=label, query_id=item.get("query_id"), label=item.get("label"),
            )
            if row is None:
                continue  # re-seen in the look-back, unchanged: no row, no in-band offer
            if e["abstract"]:
                c.add_inband(c.source_id(CHANNEL, aid), "abstract", text=e["abstract"],
                             media_type="text/plain")
        return new

    def sweep(item: dict[str, Any], label: str, eff_from: str, upper: str | None,
              ask_from: str | None = None) -> None:
        """One window of the query from ``eff_from`` (to ``upper``, a hole's end, else open):
        fetch (from ``ask_from`` when given: the tail's look-back, D405), filter, observe, write
        the window it covered, which starts at ``eff_from`` either way."""
        ask_from = ask_from or eff_from
        entries, exhausted, matched = pull(item, label, ask_from, upper)
        new = 0
        if ask_from < eff_from and covered_window(
                entries, exhausted, eff_from, today=upper or today)[1] == "truncated":
            # The cap filled inside the look-back: keep those hits, then ask from the watermark
            # (else the query would re-ask the same capped page every run).
            tally["lookback_capped"] += 1
            new += record(item, label, matched, eff_from)
            entries, exhausted, matched = pull(item, label, eff_from, upper)
        new += record(item, label, matched, eff_from)
        # Window actually covered (D-SWEEP1): complete → today (a hole: its end); cap-truncated
        # ascending pull → the contiguous prefix it really covered; error / unsplittable day →
        # [from, from].
        # D471: the cap stopped this window short of its end (a banked prefix or a stalled day)
        tally["queries_capped"] += 1 if exhausted == "truncated" else 0
        to_date, exhausted = covered_window(entries, exhausted, eff_from, today=upper or today)
        with c.window(CHANNEL, label, eff_from, to_date) as w:
            w.seen, w.kept = len(matched), new
            w.close(exhausted, error_class=api_stats.get("error_class") or "network")
        tally[f"queries_{exhausted}"] += 1
        tally["caught_up"] += 1 if to_date >= today else 0
        tally["hits_matched"] += len(matched)
        tally["hits_new"] += new
        tally[f"queries_{item['family']}"] += 1
        tally[f"hits_{item['family']}"] += len(matched)

    for i, item in enumerate(queries):
        c.progress(i, len(queries))
        label = str(item["query"])
        # A complete pass already covered this query through today: asking again would return
        # nothing and still pay arXiv's 3 s pacing (v1's `completed_today` rule); an unsplittable
        # day's `partial` truncates the same way when asked again within relist_after_h (D226).
        # A hole below the coverage (the floor moved back) keeps the query due.
        if not c.due(CHANNEL, label, until=today_d, relist_after=relist, partial_due=False,
                     floor=floor_from):
            continue
        for start, end in c.holes(CHANNEL, label, floor_from):
            tally["holes"] += 1
            sweep(item, label, start.date().isoformat(), end.date().isoformat())
        mark = c.watermark(CHANNEL, label)
        if mark is not None and mark.date() >= today_d:
            continue  # only a hole was due; the tail is covered through today
        if mark is None:
            sweep(item, label, floor_from, None)
        else:  # announce lag (D405): re-ask `lag` days below the watermark
            rescan = max(floor_from, (mark.date() - dt.timedelta(days=lag)).isoformat())
            sweep(item, label, mark.date().isoformat(), None, ask_from=rescan)
    c.progress(len(queries), len(queries))

    c.counts.update(tally, roster_planned=planned, roster_selected=selected,
                    author_queries_by_id=by_id,
                    query_limit=limit, page_retries=int(api_stats["retries"]),
                    announce_lag_days=lag)
    # D471 (S25-58): a query the cap stopped is not caught up, so the run is not scope complete
    return c.finish(scope_complete=tally["queries_capped"] == 0)
