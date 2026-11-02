"""The scholarly lookups: shared code of ``collect.openalex-lookup`` and ``collect.s2-lookup``
(D92; their stage modules under ``workflows/collect/<provider>-lookup/`` are thin).

**A lookup** (§3.1 collect row, §5.6, D22, D13; :func:`run_lookup`) is a collector that reads keys
instead of query windows. It reads the ``documents`` head (``--in data/documents/<series>/``; a run
without it is refused) and, per document, its ``identity.keys`` of the stage line's ``keys:`` kinds,
taken in the provider's order (:data:`PROVIDERS`: OpenAlex ``doi, pmid, arxiv, openalex``, S2
``arxiv, doi, pmid, s2``, v3's batch preferences). A document is skipped when it is retired or
masked, when a member already comes from the provider's own channel (v3's self-skip, §5.2), or
when one of its keys already has a hit in the stage's own ``sources.<suffix>`` chain. Otherwise its
first key that is **not done** is due; a key is done when its row in the own chain is ``observed``
or has ``miss_count >= miss_cap`` (the stage line's; v3's done-test SQL ``:921-939`` and its
quarantine, now over the own chain). **Passes** (D226): a key whose last lookup was a miss less
than ``relist_after_h`` hours ago (stage option, default 12) is not asked again, so the passes of
one build do not spend ``miss_cap`` on one day (counted ``not_due``; done documents count
``already_covered``); a key of a failed batch has no row and is due in the next pass; a provider
``retry_after`` from a halt holds the whole run (``held_retry_after``, ``scope_complete = false``).
**Scope** (D520): ``collect.s2-lookup`` asks only for documents whose ``metadata.abstract`` is not
usable (:func:`is_usable_abstract`; counted ``docs_with_abstract``); its stage option
``only_missing_abstract: false`` restores the full scope. OpenAlex looks every document up. Due keys are de-duplicated across documents and batched per
provider axis (OpenAlex filter ``doi|pmid|openalex``, an arXiv id as its DataCite DOI; S2 batch
``ARXIV:/DOI:/PMID:`` forms), at most ``max_records`` keys per run (stage option, or
``STACK_METADATA_LIMIT``).

**Rows.** One ``sources`` row per looked-up key on the provider's channel (``openalex`` /
``semantic_scholar``), ``external_id = '<kind>:<value>'`` so a key keeps its ``source_id`` from
miss to hit (C32). A hit: ``record_format record`` over the raw provider record, keys typed by
:mod:`stack.scholarly_records` from the record plus the looked-up key, ``metadata.origin
provider``, ``lookup_of_source_ids`` = the document members carrying the looked-up key, a usable
abstract (:func:`is_usable_abstract`) as ``metadata.abstract`` and the in-band ``abstract`` role. A
miss, or a hit whose DOI/arXiv id contradicts the looked-up key (v3's S2 rule): the harness's V1
miss row with the looked-up key. One ``coverage`` row per batch (``query`` = the batch's keys,
window start = end = the run's clock; ``gate_digest`` NULL in M2, X16). Lookups never write
``documents``.

**Transport.** The harness's :class:`stack.collect.base.Transport` at v3's pacing
(:data:`OPENALEX_DELAY_S`; S2 at the ``semantic_scholar`` channel's rate, the Gate's 2.5 s, D550) and
v3 ``http_request``'s ladder (2 s, 4 s). A 429 whose retry-after exceeds 60 s halts the run (the
OpenAlex budget, §5.6; the keyless S2 pool). S2 (D521, D550; :data:`S2_PACING` ``wait_429``): a
shorter 429 is waited out (its retry-after; when the answer names none, the Gate's backoff: 5 s,
doubling to 60 s, reset by a 200) and re-sent, at most :data:`~stack.collect.base.BACKOFF_WAITS`
(6) times, instead of taking the ladder; the ladder stays for 5xx and transport failures. OpenAlex
keeps the ladder on 429. A
batch that still fails after the ladder or the waits ends the run there (v3 blocked the provider for the run):
its window is ``failed_*``, its keys get no row and stay due, ``scope_complete = false``.
``STACK_MAILTO`` is read for OpenAlex's polite pool. **Keys** (:mod:`stack.collect.provider_keys`,
both read through ``STACK_ENV_FILE`` or the environment): ``SEMANTIC_SCHOLAR_API_KEY`` goes to S2
as ``x-api-key``; ``OPENALEX_API_KEY`` goes to OpenAlex only through the run's
:class:`~stack.collect.provider_keys.OpenAlexMeter` (ledger reservation per request; stage options
``max_requests``/``max_usd``, default $0.50 a keyed run). A request a spend cap refuses is not
sent: the batch gets no row (its keys stay due) and the run ends ``scope_complete = false``
(``openalex_budget_stop``). Keyless, both run as before.

v3's database-bound ``run`` (due selection, checkpoints, fetch_log quarantine) is retired; its
pure provider helpers (abstract waterfall, venue and withdrawal parsing, Zenodo siblings, the
Crossref / Europe PMC / arXiv Atom / OAI / Zenodo fetchers over v3's ``http_request`` seam) were
reachable only from tests and were deleted in the postrev dead-code pass. The pure normalisers live in :mod:`stack.scholarly_records`.
"""

from __future__ import annotations

import json as _json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

# NOTE: written when the runner loaded this module via ``importlib.util.module_from_spec`` +
# ``exec_module`` WITHOUT registering it in ``sys.modules``. ``@dataclass`` on a module loaded
# that way crashes (its KW_ONLY scan over stringized ``from __future__`` annotations does a
# ``sys.modules[__name__]`` lookup that returns None), so the small value types below are PLAIN
# classes. The lookup stages now import it as ``workflows.fetch.metadata.main`` (registered).

# ─────────────────────────────────────────────────────────────────────────────
# Env-var interface (documented alongside the contract decisions above)
# ─────────────────────────────────────────────────────────────────────────────
_ENV_LIMIT = "STACK_METADATA_LIMIT"      # int; read as the `max_records` override (0 = no cap)

#: v3's ``fetch.scholarly`` variables, refused so a carried-over v3 ``.env`` fails loudly: the v4
#: variable read instead, or ``None`` when v4 has none (v3's refresh and provider-only switches).
_LEGACY_ENV: dict[str, str | None] = {
    "STACK_SCHOLARLY_LIMIT": _ENV_LIMIT,
    "STACK_SCHOLARLY_MAILTO": "STACK_MAILTO",
    "STACK_SCHOLARLY_REFRESH": None,
    "STACK_SCHOLARLY_ONLY": None,
}


def _reject_legacy_env() -> None:
    """Fail loudly instead of silently ignoring v3's operational controls."""
    found = [(old, new) for old, new in _LEGACY_ENV.items() if old in os.environ]
    if found:
        fixes = ", ".join(f"{old} -> {new}" if new else f"{old}: no v4 equivalent, unset it"
                          for old, new in found)
        raise ValueError(f"v3 fetch.scholarly environment variables are not read by the v4 lookups: {fixes}")


# Batch sizes (ported): OpenAlex filter batches of 50, S2 batch 100.
OPENALEX_BATCH = 50
S2_BATCH = 100

# API endpoints.
OPENALEX_URL = "https://api.openalex.org/works"
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"


# ─────────────────────────────────────────────────────────────────────────────
# The shared abstract-usability predicate (ported from stages/lib/corpus.py). THE threshold: the
# same predicate that DEFINES the availability gap and GATES recording it usable (the v1
# convergence lesson — a laxer write gate re-opens the gap forever).
# ─────────────────────────────────────────────────────────────────────────────
MIN_USABLE_ABSTRACT_CHARS = 100


def is_usable_abstract(text: str) -> bool:
    """True if an abstract is long enough to be worth recording as available (≥100 chars).

    The one definition, shared by the gap detector and the write gate (v1's ``is_usable_abstract``).
    Takes the abstract body (fetch.metadata has no title+body file — just the abstract string)."""
    t = (text or "").strip()
    return len(t) >= MIN_USABLE_ABSTRACT_CHARS


def _safe_json(raw: bytes) -> Any:
    try:
        return _json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _safe_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


# ─────────────────────────────────────────────────────────────────────────────
# The lookups (collect.openalex-lookup, collect.s2-lookup): module docstring. Plain classes, not
# dataclasses (the NOTE at the top: this module is also loaded by path).
# ─────────────────────────────────────────────────────────────────────────────
from stack.collect.base import (
    BACKOFF_WAITS,
    RELIST_OPTIONS,
    CollectError,
    Collector,
    Pacing,
    Sender,
    Transport,
    utc,
)
from stack.collect.provider_keys import (
    METER_OPTIONS,
    SELECT_OPTION,
    OpenAlexBudgetStop,
    OpenAlexMeter,
    openalex_select,
    s2_headers,
)
from stack.http import HttpResponse
from stack.provider_transport import gate_interval
from stack.scholarly_records import (
    Key,
    keys_for,
    normalize_doi,
    normalize_openalex,
    normalize_openalex_id,
    normalize_pmid,
    normalize_s2,
)
from stack.stage_runner import StageContext, StageResult

#: v3's per-provider politeness delays (v3 ``metadata_enrichment.DELAYS``): OpenAlex ~6.7 req/s.
#: S2: the ``semantic_scholar`` channel's rate (D550; ``run_lookup`` takes it from
#: ``gate_interval("s2")``), 2.5 s as shipped.
OPENALEX_DELAY_S = 0.15
S2_DELAY_S = 2.5

#: v3 ``http_request``'s ladder: 3 attempts, base 2 s doubling, on 429/5xx/transport failure; a 429
#: asking for more than 60 s was not retried in v3 and halts the run here (§5.6).
RETRY_DELAYS_S = (2.0, 4.0)
HALT_429_AFTER_S = 60.0
OPENALEX_PACING = Pacing(rps=1 / OPENALEX_DELAY_S, retry_delays_s=RETRY_DELAYS_S,
                         halt_429_after_s=HALT_429_AFTER_S)
#: S2 (D521, D550): a 429 within the halt threshold is waited out (the Gate's backoff when S2 names
#: no retry-after; the 2 s / 4 s ladder failed the batch and ended the run).
S2_PACING = Pacing(rps=1 / S2_DELAY_S, retry_delays_s=RETRY_DELAYS_S,
                   halt_429_after_s=HALT_429_AFTER_S, wait_429=BACKOFF_WAITS)
#: The OpenAlex fields the lookup read before D525. No ``select`` is sent by default (full works, the
#: rows' ``provider_record``); the stage option ``select:`` restores a field list, which must include these.
OPENALEX_SELECT = ("id,doi,ids,title,publication_year,publication_date,type,cited_by_count,"
                   "open_access,best_oa_location,primary_location,authorships,biblio,"
                   "abstract_inverted_index")
S2_FIELDS = ("paperId,externalIds,title,year,publicationDate,abstract,venue,publicationVenue,"
             "publicationTypes,authors.name,openAccessPdf")
#: ``metadata.provider_version`` of a lookup row: bump when the record shape read here changes.
PROVIDER_VERSION = 1


class LookupProvider:
    """One lookup provider: ``channel`` (``channels.yaml``) of its rows, key kinds in its order of
    preference, batch width, pacing."""

    def __init__(self, name: str, channel: str, kinds: tuple[str, ...], width: int,
                 pacing: Pacing) -> None:
        self.name, self.channel, self.kinds, self.width, self.pacing = (
            name, channel, kinds, width, pacing)


PROVIDERS: dict[str, LookupProvider] = {
    "openalex": LookupProvider("openalex", "openalex", ("doi", "pmid", "arxiv", "openalex"),
                               OPENALEX_BATCH, OPENALEX_PACING),
    "s2": LookupProvider("s2", "semantic_scholar", ("arxiv", "doi", "pmid", "s2"), S2_BATCH,
                         S2_PACING),
}
#: The ``options`` keys :func:`run_lookup` reads per provider (D434): ``max_records``,
#: ``relist_after_h``, and for openalex its meter's caps.
LOOKUP_OPTIONS: dict[str, frozenset[str]] = {
    "openalex": frozenset({"max_records", SELECT_OPTION}) | RELIST_OPTIONS | METER_OPTIONS,
    "s2": frozenset({"max_records", "only_missing_abstract"}) | RELIST_OPTIONS,
}
#: Per provider, the default of the ``only_missing_abstract`` option (D520): S2 is asked only for
#: documents without a usable abstract (its yield is abstracts and ids); OpenAlex has no option.
ONLY_MISSING_ABSTRACT: dict[str, bool] = {"openalex": False, "s2": True}

# Transport seams: tests install a ``tests/fakes/http.FakeHttp`` as ``SEND`` and a no-op ``_sleep``;
# ``http_fetch`` is the one request call (the run's Transport, with the provider's ladder).
SEND: Sender | None = None
_T: Transport | None = None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def http_fetch(method: str, url: str, *, params: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None, body: bytes | None = None) -> HttpResponse:
    """One provider request through the run's transport (paced, retried, halting on a long 429)."""
    if _T is None:
        raise CollectError("http_fetch called outside a lookup run")
    return _T.fetch(method, url, params=params, headers=headers, body=body)


class Batch:
    """One provider answer: ``hits`` ``{request key: raw record}``; ``error`` ``None`` for a
    valid answer, else ``(error_class, permanent)`` for the coverage row."""

    def __init__(self, hits: dict[str, dict[str, Any]] | None = None,
                 error: tuple[str, bool] | None = None) -> None:
        self.hits = hits or {}
        self.error = error


def _failure(resp: HttpResponse) -> tuple[str, bool]:
    """A failed response → the coverage ``(error_class, permanent)``."""
    if resp.status is None:
        return ("timeout" if "timeout" in (resp.error or "").lower() else "network"), False
    if resp.status == 429:
        return "rate_limit", False
    if resp.status >= 500:
        return "http_5xx", False
    if resp.status in (401, 403):
        return "provider_auth", False
    return "query_invalid", True


def _halves(values: list[str], one: Callable[[list[str]], Batch]) -> Batch:
    """v3's 413/414 rule: a request too large for the provider is split in two."""
    mid = len(values) // 2
    left, right = one(values[:mid]), one(values[mid:])
    return Batch({**left.hits, **right.hits}, left.error or right.error)


def _json_body(resp: HttpResponse) -> Any:
    return _safe_json(resp.body) if resp.body else None


def openalex_chunk(values: list[str], *, by: str, mailto: str | None, select: str | None = None) -> Batch:
    """One OpenAlex filter request (``by`` ∈ ``doi``/``pmid``/``openalex``, ≤ ``OPENALEX_BATCH``
    values in their key normal form); hits keyed by the same normal form. A body that is not a
    ``results`` list of works is ``parse_error`` (v3: the provider stopped answering usefully).
    ``select``: the ``select`` parameter (D525; ``None`` = full works)."""
    params: dict[str, Any] = {"filter": f"{by}:" + "|".join(values)}
    if select is not None:
        params["select"] = select
    params["per_page"] = OPENALEX_BATCH
    if mailto:
        params["mailto"] = mailto
    resp = http_fetch("GET", OPENALEX_URL, params=params)
    if resp.status in (413, 414) and len(values) > 1:
        return _halves(values, lambda v: openalex_chunk(v, by=by, mailto=mailto, select=select))
    if resp.status != 200:
        return Batch(error=_failure(resp))
    data = _json_body(resp)
    if (not isinstance(data, dict) or not isinstance(data.get("results"), list)
            or any(not isinstance(w, dict) for w in data["results"])):
        return Batch(error=("parse_error", True))
    field: Callable[[dict[str, Any]], Any] = {
        "doi": lambda w: w.get("doi"), "pmid": lambda w: (w.get("ids") or {}).get("pmid"),
        "openalex": lambda w: w.get("id")}[by]
    norm = {"doi": normalize_doi, "pmid": normalize_pmid, "openalex": normalize_openalex_id}[by]
    hits: dict[str, dict[str, Any]] = {}
    for w in data["results"]:
        k = norm(str(field(w) or ""))
        if k in values and k not in hits:
            hits[k] = w
    return Batch(hits)


def s2_chunk(forms: list[str], *, auth: dict[str, str] | None = None) -> Batch:
    """One S2 batch request (``ARXIV:``/``DOI:``/``PMID:``/paperId forms, ≤ ``S2_BATCH``); the
    answer is a list aligned with the request (``null`` = not found); hits keyed by form. A
    misaligned or malformed list is ``parse_error``. ``auth`` = the ``x-api-key`` header when
    keyed (:func:`stack.collect.provider_keys.s2_headers`)."""
    resp = http_fetch("POST", S2_BATCH_URL, params={"fields": S2_FIELDS},
                      headers={"Content-Type": "application/json", **(auth or {})},
                      body=_json.dumps({"ids": forms}).encode())
    again = lambda v: s2_chunk(v, auth=auth)
    if resp.status in (413, 414) and len(forms) > 1:
        return _halves(forms, again)
    if resp.status == 400 and len(forms) > 1 and any(
            w in _safe_text(resp.body).lower() for w in ("too large", "10 mb", "maximum")):
        return _halves(forms, again)
    if resp.status != 200:
        return Batch(error=_failure(resp))
    data = _json_body(resp)
    if (not isinstance(data, list) or len(data) != len(forms)
            or any(p is not None and not isinstance(p, dict) for p in data)):
        return Batch(error=("parse_error", True))
    return Batch({f: p for f, p in zip(forms, data, strict=True) if p})


class Due:
    """One key to look up: ``(kind, value)`` and the document members carrying it."""

    def __init__(self, kind: str, value: str) -> None:
        self.kind, self.value = kind, value
        self.lookup_of: set[str] = set()

    @property
    def external_id(self) -> str:
        return f"{self.kind}:{self.value}"


def due_keys(docs: list[dict[str, Any]], c: Collector, p: LookupProvider, kinds: list[str],
             miss_cap: int, *, miss_wait: timedelta | None = None,
             only_missing_abstract: bool = False) -> tuple[list[Due], dict[str, int]]:
    """The keys to look up this run (module docstring), in document order, and skip counts.
    ``miss_wait``: a document whose due key missed less than this ago is not due (``not_due``).
    ``only_missing_abstract`` (D520): a document whose ``metadata.abstract`` is usable
    (:func:`is_usable_abstract`) is not looked up (``docs_with_abstract``)."""
    counts = {"docs_seen": 0, "docs_skipped_self": 0, "docs_done": 0, "docs_exhausted": 0,
              "not_due": 0, "docs_with_abstract": 0}
    due: dict[tuple[str, str], Due] = {}
    rank = {k: i for i, k in enumerate(kinds)}
    for doc in sorted(docs, key=lambda d: d["doc_id"]):
        ident = doc["identity"]
        if doc["state"] == "retired" or ident["is_masked"]:
            continue
        counts["docs_seen"] += 1
        if any(m["channel"] == p.channel for m in ident["members"]):
            counts["docs_skipped_self"] += 1
            continue
        if only_missing_abstract and is_usable_abstract((doc.get("metadata") or {}).get("abstract") or ""):
            counts["docs_with_abstract"] += 1
            continue
        carriers: dict[tuple[str, str], set[str]] = {}
        for k in ident["keys"]:
            if k["kind"] in rank:
                carriers.setdefault((k["kind"], k["value"]), set()).add(k["source_id"])
        cands = sorted(carriers, key=lambda kv: (rank[kv[0]], kv[1]))
        prior = {kv: c.prior(p.channel, f"{kv[0]}:{kv[1]}") for kv in cands}
        if any(r is not None and r["state"] == "observed" for r in prior.values()):
            counts["docs_done"] += 1
            continue
        pick = next((kv for kv in cands if _misses(prior[kv]) < miss_cap), None)
        if pick is None:
            counts["docs_exhausted"] += int(bool(cands))
            continue
        last = prior[pick]
        if miss_wait is not None and last is not None and c.now - last["observed_at"] < miss_wait:
            counts["not_due"] += 1
            continue
        due.setdefault(pick, Due(*pick)).lookup_of |= carriers[pick]
    return list(due.values()), counts


def _misses(row: dict[str, Any] | None) -> int:
    """Consecutive misses of a key in the own chain (0 when never looked up)."""
    return 0 if row is None else int(row["metadata"]["miss_count"] or 0)


def _request_key(p: LookupProvider, d: Due) -> tuple[str, str]:
    """``(axis, request key)``: OpenAlex filters by doi/pmid/openalex (an arXiv id by its DataCite
    DOI); S2 takes one id form per key."""
    if p.name == "openalex":
        if d.kind == "arxiv":
            return "doi", f"10.48550/arxiv.{d.value}"
        return d.kind, d.value
    prefix = {"arxiv": "ARXIV:", "doi": "DOI:", "pmid": "PMID:", "s2": ""}[d.kind]
    return "ids", prefix + d.value


def _conflicts(looked: list[Key], found: list[Key]) -> bool:
    """v3's S2 rule, both providers: the record names a DOI (or arXiv id) and none of the
    looked-up key's own DOIs (arXiv ids) — a different work answered."""
    for kind in ("doi", "arxiv"):
        mine = {k["value"] for k in looked if k["kind"] == kind}
        theirs = {k["value"] for k in found if k["kind"] == kind}
        if mine and theirs and not mine & theirs:
            return True
    return False


def _published_at(date: Any) -> Any:
    try:
        return utc(date) if date else None
    except ValueError:
        return None


def _openalex_view(work: dict[str, Any], looked: list[Key]) -> tuple[
        list[Key], dict[str, Any], str | None]:
    ids = work.get("ids") or {}
    found = keys_for([("doi", work.get("doi")), ("pmid", ids.get("pmid")),
                      ("openalex", work.get("id"))])
    frag = normalize_openalex(work, is_arxiv=any(k["kind"] == "arxiv" for k in looked + found))
    authors = [{"name": ((a.get("author") or {}).get("display_name") or "").strip(),
                "orcid": (a.get("author") or {}).get("orcid"),
                "affiliation_raw": "; ".join(x.get("display_name") or "" for x in
                                             a.get("institutions") or []) or None}
               for a in work.get("authorships") or []]
    md = {"title": frag.get("title"), "authors": [a for a in authors if a["name"]] or None,
          "published_at": _published_at(frag.get("pub_date")), "doc_type": frag.get("venue_type"),
          "abstract": frag.get("abstract")}
    return found, md, str(work.get("id") or "") or None


def _s2_view(paper: dict[str, Any], looked: list[Key]) -> tuple[
        list[Key], dict[str, Any], str | None]:
    ext = paper.get("externalIds") or {}
    found = keys_for([("doi", ext.get("DOI")), ("arxiv", ext.get("ArXiv")),
                      ("pmid", ext.get("PubMed")), ("s2", paper.get("paperId"))])
    frag = normalize_s2(paper)
    types = set(paper.get("publicationTypes") or [])
    is_arxiv = any(k["kind"] == "arxiv" for k in looked + found)
    doc_type = ("journal" if "JournalArticle" in types else "conference" if "Conference" in types
                else "preprint" if is_arxiv else None)
    md = {"title": frag.get("title"), "authors": frag.get("authors"),
          "published_at": _published_at(frag.get("pub_date")), "doc_type": doc_type,
          "abstract": frag.get("abstract")}
    pid = str(paper.get("paperId") or "")
    return found, md, f"https://www.semanticscholar.org/paper/{pid}" if pid else None


def _write(c: Collector, p: LookupProvider, d: Due, record: dict[str, Any] | None,
           query: dict[str, Any], request: str | None = None) -> bool:
    """The row for one looked-up key; ``True`` for a hit. ``matched_by`` (D228): the batch query,
    ``query_id`` ``lookup:<provider>:<axis>``, ``label`` the key as requested."""
    looked = keys_for([(d.kind, d.value)])
    match = {"query": query, "query_id": f"lookup:{p.name}:{query['by']}", "label": request}
    if record is not None:
        view = _openalex_view if p.name == "openalex" else _s2_view
        found, md, url = view(record, looked)
        if _conflicts(looked, found):
            c.counts["conflicts"] = c.counts.get("conflicts", 0) + 1
        else:
            keys = keys_for([(k["kind"], k["value"]) for k in looked + found])
            abstract = md.pop("abstract") or ""
            usable = is_usable_abstract(abstract)
            row = c.observe(p.channel, d.external_id, record,
                            keys=[(k["kind"], k["value"]) for k in keys], canonical_url=url,
                            metadata={**md, "abstract": abstract if usable else None,
                                      "lookup_of_source_ids": sorted(d.lookup_of),
                                      "provider_version": PROVIDER_VERSION},
                            provider_record=record, **match)  # D522: the record as returned
            if row is not None and usable:
                c.add_inband(row["source_id"], "abstract", text=abstract, media_type="text/plain")
            return True
    c.miss(p.channel, d.external_id, keys=[(k["kind"], k["value"]) for k in looked], **match)
    return False


def _flag(value: Any) -> bool:
    """A boolean stage option: YAML ``true``/``false`` as is; anything else is refused (a typo
    must not silently widen or narrow the scope)."""
    if isinstance(value, bool):
        return value
    raise CollectError(f"expected true or false, got {value!r}")


def _documents_key(ctx: StageContext) -> str:
    keys = [k for k in ctx.inputs if k == "documents" or k.startswith("documents/")]
    if len(keys) != 1:
        raise CollectError(f"{ctx.stage} reads the documents head's keys: pass exactly one "
                           f"`--in data/documents/<series>/` (got {sorted(ctx.inputs)})")
    return keys[0]


def run_lookup(ctx: StageContext, provider: str) -> StageResult:
    """One lookup run of ``provider`` (:data:`PROVIDERS`; module docstring)."""
    global _T
    _reject_legacy_env()
    p = PROVIDERS[provider]
    c = Collector(ctx)
    line = ctx.plan_stage
    kinds = list(p.kinds)
    if line.keys is not None:
        bad = sorted(set(line.keys) - set(p.kinds))
        if bad:
            raise CollectError(f"{ctx.stage}: keys {bad} not looked up by {p.name} ({p.kinds})")
        kinds = [k for k in p.kinds if k in line.keys]
    if line.miss_cap is None:
        raise CollectError(f"{ctx.stage}: the stage line sets no miss_cap (D22)")
    cap = c.option("max_records", 0, env=_ENV_LIMIT, cast=int)
    only_missing = ONLY_MISSING_ABSTRACT[p.name]
    if "only_missing_abstract" in LOOKUP_OPTIONS[p.name]:
        only_missing = c.option("only_missing_abstract", only_missing, cast=_flag)
    docs = ctx.view(_documents_key(ctx)).select(["doc_id", "state", "identity", "metadata"]).to_pylist()
    due, counts = due_keys(docs, c, p, kinds, line.miss_cap, miss_wait=c.relist_after(),
                           only_missing_abstract=only_missing)
    c.counts.update(counts, keys_due=len(due),
                    already_covered=counts["docs_done"] + counts["docs_exhausted"])
    complete = True
    if cap and len(due) > cap:
        due, complete = due[:cap], False
    if due and c.hold(p.channel, len(due)):
        due = []  # a halt's retry_after is still in the future (D226)
    by_axis: dict[str, list[Due]] = {}
    for d in due:
        by_axis.setdefault(_request_key(p, d)[0], []).append(d)
    mailto = os.environ.get("STACK_MAILTO") or None
    select: str | None = None
    if p.name == "openalex":
        try:
            select = openalex_select(c.option(SELECT_OPTION, None), OPENALEX_SELECT)
        except ValueError as exc:
            raise CollectError(f"{ctx.stage}: {exc}") from None
    send: Sender | None = SEND
    s2_auth: dict[str, str] = {}
    if p.name == "openalex":
        send = OpenAlexMeter.for_run(c).wrap(SEND)
    else:
        s2_auth = s2_headers()
        c.counts["s2_keyed"] = int(bool(s2_auth))
    pacing = p.pacing
    if p.name == "s2":  # the channel's rate (D550); raises on an unreadable channels.yaml
        pacing = replace(pacing, rps=1.0 / (gate_interval("s2") or S2_DELAY_S))
    _T = c.transport(pacing, send=send, sleep=lambda s: _sleep(s))
    try:
        with c.halts():
            for axis, group in by_axis.items():
                for i in range(0, len(group), p.width):
                    chunk = group[i:i + p.width]
                    reqs = [_request_key(p, d)[1] for d in chunk]
                    query = {"lookup": p.name, "by": axis, "keys": reqs}
                    with c.window(p.channel, query, c.now, c.now) as w:
                        w.seen = len(chunk)
                        batch = (openalex_chunk(reqs, by=axis, mailto=mailto, select=select) if p.name == "openalex"
                                 else s2_chunk(reqs, auth=s2_auth))
                        if batch.error is not None:
                            w.fail(batch.error[0], permanent=batch.error[1])
                        else:
                            for d, r in zip(chunk, reqs, strict=True):
                                w.kept += _write(c, p, d, batch.hits.get(r), query, r)
                            w.complete()
                    if batch.error is not None:  # v3: a failing provider is blocked for the run
                        c.counts["batches_failed"] = c.counts.get("batches_failed", 0) + 1
                        return c.finish(scope_complete=False)
    except OpenAlexBudgetStop:
        # A spend cap refused the next request: the in-flight batch wrote no row (its keys and
        # every later batch's stay due), the rows already observed are emitted.
        return c.finish(scope_complete=False)
    finally:
        _T = None
    return c.finish(scope_complete=complete)
