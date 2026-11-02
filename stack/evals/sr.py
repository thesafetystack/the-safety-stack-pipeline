"""Shallow Review 2025 eval data (track E2): the SR 2025 recall set and the SR-heavy items appended
to the round-1 triage and screen sets.

``python -m stack.evals.sr recall --store v4/store-dev --in data/documents/<series>/ --out
evals/triage/sr2025-recall --cache <dir>``; ``... candidates --store v4/store-dev --in data/documents/<series>/
--queries <q.json> --out <cands.jsonl> --cache <dir>``; ``... append --task triage|screen --set 2026-10-12a
--picks <picks.json> --store v4/store-dev --in data/documents/<series>/ --packets <dir> --cache <dir>``
(``--in``: the documents version read, a series directory or an exact file; required, D382). Run with ``STACK_ENV_FILE=../repo/.env`` so the polite-pool
contact (``STACK_MAILTO``, read by name, never printed) reaches OpenAlex and arXiv.

**Network** (:class:`Net`): public read-only APIs through the collectors' own transport,
:class:`stack.provider_transport.JournalRequest`: per-host pacing by its file-lock :class:`Gate`
(arXiv 3.1 s, OpenAlex 0.15 s, any other host 1 s) and a raw-response journal under ``--cache``,
so a rerun replays instead of refetching. A non-200 that is not a 404 fails loudly after four
retries of a transient status (0, 429, 5xx; a 429 waits out the transport's 60 s host cooldown).
OpenAlex's free daily budget is per IP and shared with the collectors: once it answers 429, the
remaining ``other`` links resolve through the arXiv API by exact title instead (an ``arxiv``
channel record; the item carries ``resolver: arxiv_title``; a paper not on arXiv is dropped), and
the manifest says so. (Semantic Scholar's unauthenticated pool was tried as the fallback on
2026-10-12 and answered 429 through four retries over four minutes.)

**Recall set** (:func:`build_recall`). SR's public ``papers.csv`` (one row per agenda x link) is
reduced to unique links (:func:`classify_link`: ``arxiv``, ``lesswrong``, ``alignmentforum``,
``openreview``, ``journal``, ``lab``, ``org_site``, ``other``, ``none``), each keyed by its typed
id (arXiv id, ForumMagnum post id, OpenReview forum id, DOI, else the canonical URL) and carrying
every agenda it is listed under. Candidates are ordered by ``sha256(seed + kind + ':' + key)``.
Three groups with quotas (:data:`RECALL_QUOTAS`): ``arxiv``; ``forum`` (lesswrong +
alignmentforum); ``other`` (the rest). Inside a group, first :data:`RECALL_MIN_PER_KIND` of each
kind, then the group's pooled hash order. Each candidate is resolved to the catalogue record
production's triage question sees (``document_text(row, excerpt)``):

* in ``v4/store-dev`` (a key of the documents head matches: arXiv id, DOI, ``fm_post``, URL, or a
  key of the OpenAlex work it resolved to): the store row's own frozen item body
  (:func:`stack.evals.freeze.triage_cands`), ``origin: "store"``; a store row without a basis
  (production does not ask it) is dropped;
* arXiv: the Atom API (batched ``id_list``) parsed by ``collect.arxiv_sweep``'s own
  ``parse_entries``; excerpt = the abstract minted as the collector mints it
  (:func:`stack.collect.inband.normalize`, :data:`~stack.collect.inband.MIN_CHARS`);
* LessWrong / Alignment Forum: GraphQL ``post`` by id for the title; then the forum search
  (``/api/search``, the endpoint ``collect.forums`` reads) for that title, and the hit with the
  post's id gives title, authors (``collect.forums._hit_authors``) and the body, minted as the
  collector mints it: that is production's LessWrong excerpt exactly. No hit: GraphQL
  ``contents.markdown`` normalised (``excerpt_origin: graphql_markdown``, an approximation);
* everything else: OpenAlex by DOI, else by exact (case- and punctuation-folded) title; abstract =
  ``collect.openalex.deinvert_abstract``, authors = ``work_authors``, excerpt = the abstract as
  minted. Lab and org pages are not fetched (production's url channels never ask a question).

A record with neither abstract nor excerpt is a title-only record production does not ask: it is
dropped and the next candidate of the same kind stratum is drawn. Licence class = the store row's
(``basis_of(r, "fields")``) or, for an external record, the ``channels.yaml`` licence class of the
channel whose record it replicates (``arxiv``, ``lesswrong``, ``openalex``), which is how
``collect.base`` sets a member's licence class.

**SR-heavy amendment** (:func:`append_heavy`): ``picks.json`` lists hand-picked items
(``source`` ``store`` / ``lesswrong`` / ``arxiv``, the ``query`` that found each, ``topic``,
``intent``); the tool freezes them exactly as the freezer would (triage: store rows through
``triage_cands``, external rows through ``document_text``; screen: store rows through
``screen_cands``, LessWrong posts through ``freeze.screen_message`` over a minimal row) and appends
them to ``items.jsonl``: existing bytes untouched (checked), ids continuing the set, split 50/50
inside the new items per ``(channel_group, intent)``, ``sr_heavy: true`` and ``origin`` on each,
``manifest.json`` gaining an ``amendments`` entry (every existing key kept, including the original
``items_sha256``). A pick whose document is already in the set, or an item id already taken, is
refused. Screen whole texts go to ``texts/<item_id>.txt.gz`` (new files only).
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import inspect
import io
import json
import os
import re
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from stack.evals import freeze as F

Row = dict[str, Any]

SR_CSV_URL = ("https://raw.githubusercontent.com/arb-consulting/shallow-review-2025/main/main-pipeline/data/"
              "2025-12-16-draft-post-review/papers.csv")
RECALL_SEED = "track-e2-sr2025"
HEAVY_SEED = "track-e2-heavy"
#: Items per group (module docstring).
RECALL_QUOTAS: dict[str, int] = {"arxiv": 95, "forum": 30, "other": 25}
#: Kinds of each group, and the first items each kind gets before the group's pooled hash order.
GROUP_KINDS: dict[str, tuple[str, ...]] = {
    "arxiv": ("arxiv",), "forum": ("lesswrong", "alignmentforum"),
    "other": ("openreview", "journal", "lab", "org_site", "other")}
RECALL_MIN_PER_KIND: dict[str, int] = {"alignmentforum": 3, "openreview": 3, "journal": 3, "lab": 3,
                                       "org_site": 3}
#: The production channel whose record an external item replicates (its licence class and group).
RECORD_CHANNEL = {"arxiv": "arxiv", "lesswrong": "lesswrong", "alignmentforum": "lesswrong",
                  "openalex": "openalex"}

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX_API = "https://api.openalex.org/works"
LW_BASE = "https://www.lesswrong.com"
OPENALEX_SELECT = "id,doi,title,display_name,authorships,abstract_inverted_index,ids,publication_date,type"

LAB_HOSTS = ("anthropic.com", "openai.com", "deepmind.google", "deepmind.com", "transformer-circuits.pub",
             "ai.meta.com", "research.google", "blog.google", "x.ai", "mistral.ai")
JOURNAL_HOSTS = ("nature.com", "pnas.org", "science.org", "springer.com", "wiley.com", "acm.org", "mlr.press",
                 "aaai.org", "ssrn.com", "theoj.org", "doi.org", "sciencedirect.com", "mdpi.com",
                 "tandfonline.com", "sagepub.com", "cambridge.org", "oup.com", "ieee.org", "plos.org",
                 "frontiersin.org", "philpapers.org", "aclanthology.org", "neurips.cc", "ijcai.org")
#: Hosts that are not documents with a scholarly record (code, drives, short links, video, chat).
OTHER_HOSTS = ("github.com", "gist.github.com", "docs.google.com", "drive.google.com", "google.com", "t.co",
               "x.com", "twitter.com", "youtube.com", "youtu.be", "huggingface.co", "fbcdn.net",
               "simplecast.com", "manifund.org", "neuronpedia.org", "example-blog.com")
_DOI_IN = re.compile(r"\b(10\.\d{4,9}/[^\s?#]+)")
_LW_SEQ_POST = re.compile(r"/s/[A-Za-z0-9]+/p/([A-Za-z0-9]+)")


# ---------------------------------------------------------------- network
class NetError(RuntimeError):
    """A public API answered with an unexpected status (never masked as an empty result)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


#: ``send(method, url, kw) -> (status, body)``; tests pass a fake, the CLI the journal transport.
Send = Callable[[str, str, Mapping[str, Any]], tuple[int, bytes]]
TRANSIENT = (0, 429, 500, 502, 503, 504)
RETRIES = 5


def _load_mailto() -> None:
    """``STACK_MAILTO`` from the environment or, by name only, ``$STACK_ENV_FILE`` (as the LLM client
    reads its key); put into this process's environment for the transport's User-Agent. Never
    printed."""
    if os.environ.get("STACK_MAILTO") or os.environ.get("STACK_METADATA_MAILTO"):
        return
    from stack.llm import provider_key

    val = provider_key("STACK_MAILTO")
    if val:
        os.environ["STACK_MAILTO"] = val


def journal_send(cache: Path) -> Send:
    """The collectors' journaling, paced transport with its journal under ``cache``."""
    from stack.provider_transport import JournalRequest

    cache.mkdir(parents=True, exist_ok=True)
    os.environ["STACK_METADATA_SPOOL"] = str(cache)
    _load_mailto()
    jr = JournalRequest("sr-e2")

    def send(method: str, url: str, kw: Mapping[str, Any]) -> tuple[int, bytes]:
        r = jr(method, url, **kw)
        return r.status_code, r.content

    return send


@dataclass
class Net:
    send: Send
    sleep: Callable[[float], None] = time.sleep
    calls: collections.Counter[str] = field(default_factory=collections.Counter)

    def _call(self, method: str, url: str, kw: Mapping[str, Any],
              missing: tuple[int, ...] = (404,)) -> bytes | None:
        host = urllib.parse.urlsplit(url).hostname or url
        for attempt in range(RETRIES):
            status, body = self.send(method, url, kw)
            self.calls[host] += 1
            if status == 200:
                return body
            if status in missing:
                self.calls[f"{host}:{status}"] += 1
                return None
            if status not in TRANSIENT or attempt == RETRIES - 1:
                break
            self.calls[f"{host}:retry"] += 1
            # a 429 sets the transport's per-host cooldown (60 s without Retry-After): wait it out
            self.sleep(65.0 if status == 429 else 10.0 * (attempt + 1))
        raise NetError(f"{method} {host}{urllib.parse.urlsplit(url).path}: HTTP {status}", status)

    def get(self, url: str, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None, missing: tuple[int, ...] = (404,)) -> bytes | None:
        """The body of a 200; None for a status in ``missing`` (counted); else :class:`NetError`."""
        kw: dict[str, Any] = {}
        if params:
            kw["params"] = dict(params)
        if headers:
            kw["headers"] = dict(headers)
        return self._call("GET", url, kw, missing)

    def post_json(self, url: str, body: Any, headers: Mapping[str, str] | None = None) -> Any:
        kw: dict[str, Any] = {"json_body": body}
        if headers:
            kw["headers"] = dict(headers)
        raw = self._call("POST", url, kw)
        return None if raw is None else json.loads(raw)


# ---------------------------------------------------------------- JSONL
#: Characters ``str.splitlines`` splits on that ``json.dumps(ensure_ascii=False)`` leaves raw; the
#: harness reads sets with ``splitlines``, so a raw U+2028 inside a text splits an item in two.
_LINE_BREAKERS = {"\u2028": "\\u2028", "\u2029": "\\u2029", "\x85": "\\u0085"}


def jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    """``freeze.jsonl`` with the line-breaking characters escaped (same JSON values, one row per
    ``splitlines`` line)."""
    text = F.jsonl(rows).decode("utf-8")
    for raw, esc in _LINE_BREAKERS.items():
        text = text.replace(raw, esc)
    return text.encode("utf-8")


def read_jsonl(path: Path) -> list[Row]:
    """Rows of a JSONL file split on ``\\n`` only (robust to a raw U+2028 inside a value)."""
    return [json.loads(x) for x in path.read_text("utf-8").split("\n") if x.strip()]


# ---------------------------------------------------------------- papers.csv and links
@dataclass
class Link:
    """One unique SR link with every agenda row that lists it."""

    kind: str
    key: str
    url: str
    title: str
    rows: list[Row]

    @property
    def order_id(self) -> str:
        return f"{self.kind}:{self.key}"


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")


def _host_in(host: str, hosts: Iterable[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def classify_link(url: str) -> tuple[str, str]:
    """``(kind, key)`` of an SR link (module docstring); key '' when nothing keys it."""
    from stack.scholarly_records import normalize_arxiv, normalize_doi, normalize_fm_post, normalize_url

    u = (url or "").strip()
    if not u:
        return "none", ""
    host = _host(u)
    if _host_in(host, ("arxiv.org",)):
        return "arxiv", normalize_arxiv(u)
    if _host_in(host, ("lesswrong.com", "alignmentforum.org", "greaterwrong.com")):
        kind = "alignmentforum" if "alignmentforum" in host else "lesswrong"
        m = _LW_SEQ_POST.search(u)
        return kind, normalize_fm_post(u) or (m.group(1) if m else "")
    if _host_in(host, ("openreview.net",)):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        return "openreview", (q.get("id") or [""])[0]
    m = _DOI_IN.search(urllib.parse.unquote(u))
    doi = normalize_doi(m.group(1).rstrip(".")) if m else ""
    if doi and doi.startswith("10.48550/arxiv."):
        return "arxiv", normalize_arxiv(doi)
    if _host_in(host, JOURNAL_HOSTS) or doi:
        return "journal", doi or normalize_url(u)
    if _host_in(host, LAB_HOSTS):
        return "lab", normalize_url(u)
    if _host_in(host, OTHER_HOSTS):
        return "other", normalize_url(u)
    return "org_site", normalize_url(u)


def parse_papers_csv(data: bytes) -> tuple[list[Row], list[str]]:
    """The csv's rows and columns."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    return list(reader), list(reader.fieldnames or [])


def unique_links(rows: Sequence[Row]) -> list[Link]:
    """One :class:`Link` per ``(kind, key)`` (rows without a key are kept per URL, kind as
    classified), in csv order of first appearance."""
    out: dict[tuple[str, str], Link] = {}
    for r in rows:
        url = (r.get("link_url") or "").strip()
        kind, key = classify_link(url)
        k = (kind, key or url)
        if k not in out:
            out[k] = Link(kind, key, url, (r.get("title") or r.get("link_text") or "").strip(), [])
        out[k].rows.append(r)
    return list(out.values())


def link_counts(rows: Sequence[Row]) -> dict[str, int]:
    return dict(collections.Counter(classify_link(r.get("link_url") or "")[0] for r in rows).most_common())


# ---------------------------------------------------------------- record building
def norm_title(t: str) -> str:
    """Case-, accent- and punctuation-folded title for exact matching."""
    nfd = unicodedata.normalize("NFKD", t or "")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", "".join(c for c in nfd if not unicodedata.combining(c)).lower()).split())


#: SR ``link_text`` / ``title`` values that are scraping artefacts, not titles.
_NON_TITLE = re.compile(r"^\s*(unable to access|\d{3}\s*:|error\b|too many requests|access denied|just a moment)",
                        re.IGNORECASE)
#: Folded titles shorter than this are not compared (``"2"``, ``"Rosas"``, ``"the void"``).
MIN_TITLE_CHARS = 12
TITLE_RATIO = 0.9


def sr_titles(link: Link) -> list[str]:
    """The title SR's editors gave each row of a link: its ``link_text``, or its ``title`` column when
    the ``link_text`` is not informative (a scraping artefact or a folded title under
    :data:`MIN_TITLE_CHARS`); deduplicated. The ``title`` column is otherwise not used: SR scraped it
    from the URL, so when the URL is wrong it names the wrong paper too (sr-0041, sr-0107 of the
    first freeze)."""
    def ok(t: str) -> bool:
        return bool(t) and not _NON_TITLE.match(t) and len(norm_title(t)) >= MIN_TITLE_CHARS

    out: list[str] = []
    for r in link.rows:
        lt, ti = (" ".join((r.get(k) or "").split()) for k in ("link_text", "title"))
        t = lt if ok(lt) else (ti if ok(ti) else "")
        if t and norm_title(t) not in map(norm_title, out):
            out.append(t)
    return out


_STOP = frozenset("a an and are as at be by for from in is it of on or the to via vs with".split())
TOKEN_OVERLAP = 0.6


def titles_match(a: str, b: str) -> bool:
    """Folded equality; containment of the shorter in the longer (a subtitle or a prefix); a
    :data:`TITLE_RATIO` sequence ratio; or, for an editor's paraphrase (``Opus 4.5's Soul Document``
    for ``Claude 4.5 Opus' Soul Document``), content words shared at :data:`TOKEN_OVERLAP` of the
    shorter title's (at least two of them)."""
    import difflib

    x, y = norm_title(a), norm_title(b)
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    if x == y or short in long_ or difflib.SequenceMatcher(None, x, y).ratio() >= TITLE_RATIO:
        return True
    ta, tb = (set(t.split()) - _STOP for t in (x, y))
    k = min(len(ta), len(tb))
    return k >= 2 and len(ta & tb) / k >= TOKEN_OVERLAP


def minted(text: str | None) -> str | None:
    """An in-band text as ``collect.inband.mint_entry`` stores it: normalised, None under the noise
    floor."""
    from stack.collect.inband import MIN_CHARS, normalize

    if not text:
        return None
    t = normalize(text)
    return t if len(t) >= MIN_CHARS else None


def record_input(title: str | None, authors: Sequence[str], abstract: str | None,
                 excerpt: str | None) -> Row:
    """The triage item ``input`` for an external record: ``record = document_text(row, excerpt)``
    over the row the gate view would hold."""
    from workflows.triage.ask import document_text

    row = {"metadata": {"title": title, "abstract": abstract, "authors": [{"name": n} for n in authors]}}
    record = document_text(row, excerpt)
    return {"title": title, "authors": list(authors), "abstract": abstract, "excerpt": excerpt, "record": record}


def has_basis(inp: Mapping[str, Any]) -> bool:
    """Production's ``has_basis`` on an external record: an abstract or an excerpt."""
    return bool(inp.get("abstract")) or inp.get("excerpt") is not None


def license_of(channel: str) -> str:
    from stack.annotate.stage import stack_config

    return str(stack_config().channels.channel(channel).license_class)


def arxiv_records(net: Net, ids: Sequence[str]) -> dict[str, Row]:
    """arXiv id -> ``{title, authors, abstract}`` from the Atom API, batched (``collect.arxiv_sweep``'s
    ``parse_entries``)."""
    from stack.arxiv import ATOM_BATCH, batched
    from workflows.collect.arxiv_sweep.main import parse_entries

    out: dict[str, Row] = {}
    for chunk in batched(sorted(set(ids)), ATOM_BATCH):
        body = net.get(ARXIV_API, {"id_list": ",".join(chunk), "max_results": len(chunk)})
        entries = parse_entries((body or b"").decode("utf-8"))
        if entries is None:
            raise NetError(f"arXiv: a batch of {len(chunk)} ids returned no Atom feed")
        for e in entries:
            out[e["arxiv_id"]] = e
    return out


def arxiv_input(e: Mapping[str, Any]) -> Row:
    return record_input(e["title"] or None, e["authors"], e["abstract"] or None, minted(e["abstract"]))


def openalex_work(net: Net, *, doi: str = "", title: str = "") -> Row | None:
    """The OpenAlex work for a DOI, else the first exact-title match (preferring one with an
    abstract); None when there is none."""
    if doi:
        # OpenAlex answers 400 to a DOI it cannot parse: a miss, like a 404
        body = net.get(f"{OPENALEX_API}/doi:{doi}", {"select": OPENALEX_SELECT}, missing=(400, 404))
        if body is not None:
            work: Row = json.loads(body)
            return work
    if not title:
        return None
    clean = " ".join(re.sub(r"[^\w\s-]", " ", title).split())  # filter syntax characters out
    if not clean:
        return None
    body = net.get(OPENALEX_API, {"filter": f"title.search:{clean}", "select": OPENALEX_SELECT, "per_page": 25})
    want = norm_title(title)
    hits = [w for w in json.loads(body or b"{}").get("results") or []
            if norm_title(str(w.get("title") or w.get("display_name") or "")) == want]
    hits.sort(key=lambda w: not w.get("abstract_inverted_index"))
    return hits[0] if hits else None


def arxiv_title_match(net: Net, title: str) -> Row | None:
    """The arXiv entry whose title equals ``title`` after folding (Atom search ``ti:``), or None."""
    words = " ".join(re.sub(r"[^\w\s-]", " ", title or "").split())
    if not words:
        return None
    want = norm_title(title)
    hits = [e for e in arxiv_search(net, f'ti:"{words}"', 10) if norm_title(e["title"]) == want]
    return hits[0] if hits else None


def openalex_input(w: Mapping[str, Any]) -> Row:
    from workflows.collect.openalex.main import deinvert_abstract, work_authors

    abstract = deinvert_abstract(w.get("abstract_inverted_index"))
    title = str(w.get("title") or "").strip() or None
    return record_input(title, work_authors(dict(w)), abstract, minted(abstract))


def openalex_keys(w: Mapping[str, Any]) -> list[tuple[str, str]]:
    from workflows.collect.openalex.main import work_keys

    return work_keys(dict(w))


def _browser_headers() -> dict[str, str]:
    from workflows.collect.forums.main import BROWSER_UA

    return {"User-Agent": BROWSER_UA}


GQL_POST = ("query($id: String) { post(input: {selector: {_id: $id}}) { result { _id title slug postedAt "
            "baseScore af user { displayName username } coauthors { displayName username } "
            "contents { markdown } } } }")


def forum_post(net: Net, post_id: str) -> Row | None:
    """The GraphQL post (LessWrong host; AF posts live in the same database)."""
    data = net.post_json(f"{LW_BASE}/graphql", {"query": GQL_POST, "variables": {"id": post_id}},
                         headers=_browser_headers())
    if not isinstance(data, dict):
        return None
    if data.get("errors") and not (data.get("data") or {}).get("post"):
        raise NetError(f"LessWrong GraphQL: post {post_id}: {str(data['errors'])[:200]}")
    res = ((data.get("data") or {}).get("post") or {}).get("result")
    return res if isinstance(res, dict) else None


def forum_search(net: Net, query: str, n: int = 20) -> list[Row]:
    """Hits of the forum search ``collect.forums`` reads (``/api/search``, posts index)."""
    data = net.post_json(f"{LW_BASE}/api/search",
                         [{"indexName": "posts", "params": {"query": query, "hitsPerPage": n, "page": 0}}],
                         headers=_browser_headers())
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise NetError(f"LessWrong search: unexpected response for {query!r}")
    return [h for h in data[0].get("hits") or [] if isinstance(h, dict)]


def _hit_id(h: Mapping[str, Any]) -> str:
    return str(h.get("_id") or h.get("objectID") or "")


def forum_input(net: Net, post_id: str) -> tuple[Row | None, Row]:
    """``(input, info)`` of a forum post as the ``lesswrong`` channel's record: the search hit's title,
    authors and minted body when the search finds the post, else GraphQL's title, authors and
    normalised markdown (``info.excerpt_origin``). ``(None, info)`` when the post does not resolve."""
    from stack.collect.forummagnum import post_authors
    from workflows.collect.forums.main import _hit_authors

    post = forum_post(net, post_id)
    if post is None:
        return None, {"excerpt_origin": None, "reason": "graphql_no_post"}
    hit = next((h for h in forum_search(net, str(post.get("title") or "")) if _hit_id(h) == post_id), None)
    info = {"af": bool(post.get("af")), "karma": post.get("baseScore"), "posted_at": post.get("postedAt")}
    if hit is not None:
        body = hit.get("body")
        title = str(hit.get("title") or "").strip() or None
        return (record_input(title, _hit_authors(hit), None, minted(body if isinstance(body, str) else None)),
                {**info, "excerpt_origin": "lw_search_body", "full_text": minted(body if isinstance(body, str)
                                                                                  else None)})
    md = ((post.get("contents") or {}).get("markdown"))
    title = str(post.get("title") or "").strip() or None
    return (record_input(title, post_authors(post), None, minted(md)),
            {**info, "excerpt_origin": "graphql_markdown", "full_text": minted(md)})


# ---------------------------------------------------------------- the store side
def store_index(src: F.Source) -> dict[tuple[str, str], str]:
    """``(key kind, value)`` -> doc_id over the documents head's identity keys (URLs normalised)."""
    from stack.scholarly_records import normalize_url

    out: dict[tuple[str, str], str] = {}
    for r in src.rows:
        for k in r["identity"]["keys"] or []:
            v = normalize_url(k["value"]) if k["kind"] == "url" else k["value"]
            out.setdefault((k["kind"], v), r["doc_id"])
    return out


def link_store_keys(link: Link) -> list[tuple[str, str]]:
    from stack.scholarly_records import normalize_doi, normalize_url

    keys: list[tuple[str, str]] = []
    if link.kind == "arxiv" and link.key:
        keys += [("arxiv", link.key), ("doi", f"10.48550/arxiv.{link.key}")]
    if link.kind in ("lesswrong", "alignmentforum") and link.key:
        keys.append(("fm_post", link.key))
    if link.kind == "journal" and normalize_doi(link.key):
        keys.append(("doi", link.key))
    if normalize_url(link.url):
        keys.append(("url", normalize_url(link.url)))
    return keys


# ---------------------------------------------------------------- recall set
@dataclass
class Resolved:
    link: Link
    origin: str
    doc_id: str
    channel_group: str
    channel: str
    license_class: str
    input: Row
    info: Row


@dataclass
class Resolver:
    """Resolves links to production records (module docstring), store first."""

    net: Net
    index: Mapping[tuple[str, str], str]
    store: Mapping[str, F.Cand]
    arxiv: dict[str, Row] = field(default_factory=dict)
    checks: list[Row] = field(default_factory=list)  # external vs store record, for overlap items
    openalex_down: bool = False  # set on OpenAlex's budget 429; later "other" links go to arXiv by title
    mismatches: list[Row] = field(default_factory=list)  # SR title vs resolved title (resolve)

    def prefetch_arxiv(self, links: Iterable[Link]) -> None:
        ids = [lk.key for lk in links if lk.kind == "arxiv" and lk.key and lk.key not in self.arxiv]
        if ids:
            self.arxiv.update(arxiv_records(self.net, ids))

    def _from_store(self, link: Link, doc_id: str, ext: Row | None) -> tuple[Resolved | None, str]:
        c = self.store[doc_id]
        if ext is not None:
            self.checks.append({"link": link.order_id, "doc_id": doc_id,
                                "record_equal": ext["record"] == c.item["input"]["record"]})
        if not c.item["production_asks"]:
            return None, "store_title_only"
        return Resolved(link, "store", doc_id, c.group, c.channel, c.item["license_class"],
                        c.item["input"], {}), ""

    def _store_doc(self, keys: Iterable[tuple[str, str]]) -> str | None:
        return next((self.index[k] for k in keys if k in self.index), None)

    def resolve(self, link: Link) -> tuple[Resolved | None, str]:
        """:meth:`_resolve` plus the title check: a record whose title matches none of SR's informative
        titles for the link (:func:`sr_titles`, :func:`titles_match`) is SR pointing at the wrong
        document; it is re-resolved through the arXiv title search by SR's titles (``resolver:
        arxiv_link_text``, the original URL kept as ``sr_link``) or dropped (``title_mismatch``).
        Every mismatch is listed in :attr:`mismatches`."""
        res, why = self._resolve(link)
        want = sr_titles(link)
        if res is None or not want or any(titles_match(t, res.input.get("title") or "") for t in want):
            return res, why
        entry: Row = {"link": link.order_id, "url": link.url, "sr_titles": want,
                      "resolved_title": res.input.get("title"), "resolved_doc": res.doc_id}
        self.mismatches.append(entry)
        for t in want:
            e = arxiv_title_match(self.net, t)
            if e is None:
                continue
            ext = arxiv_input(e)
            info = {"resolver": "arxiv_link_text", "arxiv_id": e["arxiv_id"]}
            doc = self._store_doc([("arxiv", e["arxiv_id"]), ("doi", f"10.48550/arxiv.{e['arxiv_id']}")])
            if doc is not None:
                new, why2 = self._from_store(link, doc, ext)
                if new is not None:
                    new.info = info
            else:
                new, why2 = self._external(link, "arxiv", ext, info, doc_id=f"ext:arxiv:{e['arxiv_id']}")
            entry.update(outcome="relinked" if new is not None else f"dropped:{why2}",
                         arxiv_id=e["arxiv_id"], new_title=e["title"])
            return new, why2
        entry["outcome"] = "dropped:title_mismatch"
        return None, "title_mismatch"

    def _resolve(self, link: Link) -> tuple[Resolved | None, str]:
        """The resolved record, or ``(None, reason)`` for a drop (no title check)."""
        if not link.key and link.kind in ("arxiv", "lesswrong", "alignmentforum"):
            return None, "no_key"
        doc = self._store_doc(link_store_keys(link))
        if link.kind == "arxiv":
            e = self.arxiv.get(link.key)
            if e is None:
                self.prefetch_arxiv([link])
                e = self.arxiv.get(link.key)
            ext = arxiv_input(e) if e is not None else None
            if doc is not None:
                return self._from_store(link, doc, ext)
            if ext is None:
                return None, "arxiv_not_found"
            return self._external(link, "arxiv", ext, {})
        if link.kind in ("lesswrong", "alignmentforum"):
            ext, info = forum_input(self.net, link.key)
            if doc is not None:
                return self._from_store(link, doc, ext)
            if ext is None:
                return None, str(info.get("reason"))
            info.pop("full_text", None)
            return self._external(link, "lesswrong", ext, info)
        from stack.scholarly_records import normalize_doi

        if doc is not None:
            return self._from_store(link, doc, None)
        doi = normalize_doi(link.key)
        w: Row | None = None
        if not self.openalex_down:
            try:
                w = openalex_work(self.net, doi=doi, title=link.title)
            except NetError as exc:
                if exc.status != 429:
                    raise
                self.openalex_down = True  # the shared daily budget is spent: arXiv by title from here
        if self.openalex_down:
            e = arxiv_title_match(self.net, link.title)
            if e is None:
                return None, "arxiv_title_not_found"
            doc, ext = self._store_doc([("arxiv", e["arxiv_id"])]), arxiv_input(e)
            if doc is not None:
                return self._from_store(link, doc, ext)
            return self._external(link, "arxiv", ext, {"resolver": "arxiv_title", "arxiv_id": e["arxiv_id"]})
        if w is None:
            return None, "openalex_not_found"
        doc = self._store_doc(openalex_keys(w))
        ext = openalex_input(w)
        if doc is not None:
            return self._from_store(link, doc, ext)
        return self._external(link, "openalex", ext, {"resolver": "openalex", "openalex_id": w.get("id"),
                                                      "doi": w.get("doi")})

    def _external(self, link: Link, channel: str, inp: Row, info: Row,
                  doc_id: str | None = None) -> tuple[Resolved | None, str]:
        if not has_basis(inp):
            return None, "title_only"
        group = "forum" if channel == "lesswrong" else "paper"
        return Resolved(link, "external", doc_id or f"ext:{link.kind}:{link.key}", group, channel,
                        license_of(channel), inp, info), ""


def link_order(seed: str, link: Link) -> str:
    return F.order_key(seed, link.order_id)


def sample_recall(links: Sequence[Link], resolver: Resolver, seed: str = RECALL_SEED,
                  quotas: Mapping[str, int] = RECALL_QUOTAS,
                  min_per_kind: Mapping[str, int] = RECALL_MIN_PER_KIND) -> tuple[list[Resolved], Row]:
    """Draw per group (module docstring), resolving each draw; a drop draws the next candidate of
    the same kind (minimum phase) or of the group (pooled phase). Returns the resolved items and
    the per-kind report."""
    report: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    chosen: list[Resolved] = []
    seen_docs: set[str] = set()
    for group, quota in quotas.items():
        kinds = GROUP_KINDS[group]
        pool = sorted((lk for lk in links if lk.kind in kinds), key=lambda lk: link_order(seed, lk))
        if group == "arxiv":
            resolver.prefetch_arxiv(pool[: quota * 2])
        tried: set[str] = set()
        got: collections.Counter[str] = collections.Counter()

        def attempt(lk: Link) -> None:
            tried.add(lk.order_id)
            report[lk.kind]["attempted"] += 1
            res, why = resolver.resolve(lk)
            if res is not None and res.doc_id in seen_docs:
                res, why = None, "duplicate_doc"
            if res is None:
                report[lk.kind][f"dropped:{why}"] += 1
                return
            seen_docs.add(res.doc_id)
            chosen.append(res)
            got[lk.kind] += 1
            report[lk.kind]["resolved"] += 1
            report[lk.kind][f"origin:{res.origin}"] += 1
            if res.info.get("resolver") == "arxiv_link_text":
                report[lk.kind]["relinked:arxiv_link_text"] += 1

        for kind in kinds:
            for lk in (x for x in pool if x.kind == kind):
                if got[kind] >= min_per_kind.get(kind, 0) or sum(got.values()) >= quota:
                    break
                attempt(lk)
        for lk in pool:
            if sum(got.values()) >= quota:
                break
            if lk.order_id not in tried:
                attempt(lk)
        if sum(got.values()) < quota:
            report[f"group:{group}"]["short"] = quota - sum(got.values())
    return chosen, {k: dict(sorted(v.items())) for k, v in sorted(report.items())}


def recall_items(chosen: Sequence[Resolved], seed: str = RECALL_SEED) -> tuple[list[Row], list[Row]]:
    """``items.jsonl`` and ``gold.jsonl`` rows (ids ``sr-0001`` … in hash order, split 50/50 per
    kind stratum)."""
    cands = [F.Cand(doc_id=r.doc_id, order=link_order(seed, r.link), group=r.link.kind, channel=r.channel,
                    proxy="-", hard=None, eligible=True) for r in chosen]
    split = F.assign_split(cands)
    by_doc = {r.doc_id: r for r in chosen}
    items, gold = [], []
    for i, c in enumerate(sorted(cands, key=lambda c: c.order), 1):
        r = by_doc[c.doc_id]
        first = r.link.rows[0]
        iid = f"sr-{i:04d}"
        items.append({
            "item_id": iid, "doc_id": r.doc_id, "split": split[r.doc_id],
            "stratum": {"channel_group": r.channel_group, "channel": r.channel, "kind": r.link.kind,
                        "sr_section": first.get("agenda_section") or None,
                        "sr_agenda": first.get("agenda_name") or None},
            "hard": None, "license_class": r.license_class, "production_asks": True,
            "input": r.input, "input_digest": F.sha256_hex(r.input["record"]), "origin": r.origin,
            "sr_link": r.link.url, "sr_agendas": sorted({x.get("agenda_full_name") or "" for x in r.link.rows}),
            **({"excerpt_origin": r.info["excerpt_origin"]} if r.info.get("excerpt_origin") else {}),
            **({"resolver": r.info["resolver"]} if r.info.get("resolver") else {})})
        label = {"verdict": "relevant", "fetch": True, "scope": None}
        why = f"Included in Shallow Review 2025 under {first.get('agenda_full_name')}"
        gold.append({"adjudication_note": None, "agreement": None, "confidence": None, "contested": False,
                     "doc_id": r.doc_id, "item_id": iid, "label": label,
                     "labelers": [{"who": "sr2025-inclusion", "label": label, "confidence": None,
                                   "quote": None, "rationale": why, "source": SR_CSV_URL}],
                     "quote": None, "rationale": why})
    return items, gold


def fetch_csv(net: Net, out: Path) -> tuple[bytes, Row]:
    """papers.csv, saved as ``<out>/source/papers.csv`` with ``fetch.json`` (url, first fetch time,
    sha256); a rerun reads the saved copy and its fetch record."""
    src_dir = out / "source"
    path, meta = src_dir / "papers.csv", src_dir / "fetch.json"
    if not path.is_file() and meta.is_file():  # archived (D796): the eval-text cache's copy, if fetched
        task, set_id = F.set_key(out)
        cached = F.texts_cache_root() / task / set_id / "source" / "papers.csv"
        path = cached if cached.is_file() else path
    if path.is_file() and meta.is_file():
        data = path.read_bytes()
        rec = json.loads(meta.read_text("utf-8"))
        if hashlib.sha256(data).hexdigest() != rec["sha256"]:
            raise RuntimeError(f"{path} does not match the sha256 in {meta}")
        return data, rec
    got = net.get(SR_CSV_URL)
    if got is None:
        raise NetError(f"{SR_CSV_URL}: 404 (list the repository tree to find the file)")
    data = got
    if meta.is_file() and hashlib.sha256(data).hexdigest() != json.loads(meta.read_text("utf-8"))["sha256"]:
        raise RuntimeError(f"{SR_CSV_URL} no longer matches the sha256 in {meta} (the copy is archived, D796)")
    src_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    rec = {"url": SR_CSV_URL, "fetched_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
           "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    meta.write_text(json.dumps(rec, indent=2) + "\n", "utf-8")
    return data, rec


def code_provenance() -> Row:
    p = F.provenance()
    p["sr_builder"] = {"module": "stack.evals.sr", "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return p


def build_recall(net: Net, src: F.Source, out: Path, *, seed: str = RECALL_SEED, force: bool = False) -> Row:
    """Fetch, sample, resolve and write the recall set (module docstring); returns the manifest."""
    data, fetch = fetch_csv(net, out)
    rows, columns = parse_papers_csv(data)
    links = unique_links(rows)
    store = {c.doc_id: c for c in F.triage_cands(src, seed)}
    resolver = Resolver(net, store_index(src), store)
    chosen, report = sample_recall(links, resolver, seed)
    items, gold = recall_items(chosen, seed)
    target = out / "items.jsonl"
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists: a frozen set is not replaced without --force")
    lc = collections.Counter(lk.kind for lk in links)
    manifest = {
        "set_id": out.name, "task": "triage", "kind": "recall-only (every item is an SR 2025 inclusion)",
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": {**fetch, "rows": len(rows), "columns": columns, "path": "source/papers.csv"},
        "link_kinds": {"rows": link_counts(rows), "unique_links": dict(lc.most_common()), "unique_total": len(links)},
        "source_store": str(src.store), "documents_in": src.documents, "documents_head": src.head,
        **code_provenance(), "seed": seed, "order": "sha256(seed + kind + ':' + key) hex, ascending",
        "sampling": {"quotas": RECALL_QUOTAS, "group_kinds": GROUP_KINDS, "min_per_kind": RECALL_MIN_PER_KIND,
                     "replacement": "a drop draws the next candidate of the same kind (minimum phase) or group"},
        "resolution": report,
        "record_channels": {k: {"channel": v, "license_class": license_of(v)} for k, v in RECORD_CHANNEL.items()},
        "license_rule": ("store rows: basis_of(row, 'fields').license_class (the anchor member's, which "
                         "collect.base sets to its channel's channels.yaml license_class); external rows: the "
                         "license_class of the channel whose record they replicate"),
        "openalex_budget_exhausted": resolver.openalex_down,
        "title_check": {"rule": (f"each resolved record's title against SR's link_text (its title column only "
                                 f"when the link_text is a scraping artefact or under {MIN_TITLE_CHARS} folded "
                                 f"chars; the title column was scraped from the possibly wrong URL): folded "
                                 f"equality, containment, difflib ratio >= {TITLE_RATIO}, or content-word overlap "
                                 f">= {TOKEN_OVERLAP} of the shorter title; a mismatch is "
                                 "re-resolved by SR's title through the arXiv title search (resolver "
                                 "arxiv_link_text) or dropped (title_mismatch) and replaced from its stratum"),
                        "unchecked_in_sample": sum(not sr_titles(r.link) for r in chosen),
                        "mismatches": resolver.mismatches},
        "overlap": {"store_items": sum(it["origin"] == "store" for it in items),
                    "external_vs_store_record": resolver.checks},
        "net_calls": dict(net.calls),
        "items_sha256": hashlib.sha256(jsonl(items)).hexdigest(),
        "counts": {"total": len(items),
                   "split": dict(collections.Counter(it["split"] for it in items)),
                   "kind": dict(collections.Counter(it["stratum"]["kind"] for it in items)),
                   "kind_split": dict(sorted(collections.Counter(f"{it['stratum']['kind']}|{it['split']}"
                                                                 for it in items).items())),
                   "origin": dict(collections.Counter(it["origin"] for it in items)),
                   "license_class": dict(collections.Counter(it["license_class"] for it in items)),
                   "sr_section": dict(collections.Counter(it["stratum"]["sr_section"] for it in items).most_common()),
                   "excerpt_origin": dict(collections.Counter(it.get("excerpt_origin") for it in items
                                                              if it["stratum"]["channel_group"] == "forum"))}}
    out.mkdir(parents=True, exist_ok=True)
    target.write_bytes(jsonl(items))
    (out / "gold.jsonl").write_bytes(jsonl(gold))
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return manifest


# ---------------------------------------------------------------- SR-heavy candidates
def arxiv_search(net: Net, query: str, n: int = 15) -> list[Row]:
    """arXiv API keyword search (``search_query``), parsed by ``collect.arxiv_sweep``."""
    from workflows.collect.arxiv_sweep.main import parse_entries

    body = net.get(ARXIV_API, {"search_query": query, "max_results": n, "sortBy": "relevance"})
    entries = parse_entries((body or b"").decode("utf-8"))
    if entries is None:
        raise NetError(f"arXiv search {query!r}: no Atom feed")
    return entries


def candidates(net: Net, src: F.Source, queries: Mapping[str, Any], exclude_docs: set[str]) -> list[Row]:
    """Candidate titles for hand-picking (``queries``: ``{"store": [{pattern, topic}], "lesswrong":
    [{q, topic}], "arxiv": [{q, topic}]}``); store rows already in ``exclude_docs`` are left out."""
    out: list[Row] = []
    from workflows.triage import ask as triage_ask

    reader = type("R", (), {"blobs": src.blobs})()
    for spec in queries.get("store") or []:
        pat = re.compile(spec["pattern"], re.IGNORECASE)
        for r in src.rows:
            if r["doc_id"] in exclude_docs:
                continue
            md = r["metadata"]
            excerpt = triage_ask._excerpt(reader, r.get("inband_text_sha256"))
            text = "\n".join(x for x in (md.get("title"), md.get("abstract"), (excerpt or "")[:3000]) if x)
            if pat.search(text):
                out.append({"source": "store", "id": r["doc_id"], "title": md.get("title"),
                            "channel": r["channel_group"], "has_basis": triage_ask.has_basis(r),
                            "has_text": bool((r.get("content") or {}).get("current_text_sha256")),
                            "query": spec["pattern"], "topic": spec.get("topic")})
    for spec in queries.get("lesswrong") or []:
        for h in forum_search(net, spec["q"], spec.get("n", 15)):
            out.append({"source": "lesswrong", "id": _hit_id(h), "title": h.get("title"), "karma": h.get("baseScore"),
                        "af": h.get("af"), "query": spec["q"], "topic": spec.get("topic")})
    for spec in queries.get("arxiv") or []:
        for e in arxiv_search(net, spec["q"], spec.get("n", 15)):
            out.append({"source": "arxiv", "id": e["arxiv_id"], "title": e["title"], "query": spec["q"],
                        "topic": spec.get("topic")})
    return out


# ---------------------------------------------------------------- SR-heavy amendment
class AmendError(RuntimeError):
    """An amendment that would rewrite or collide with the frozen set."""


def _ext_key(doc_id: str) -> str:
    return doc_id.rsplit(":", 1)[-1] if doc_id.startswith("ext:") else doc_id


def _picked_doc(index: Mapping[tuple[str, str], str], pick: Mapping[str, Any]) -> str | None:
    """The store doc a pick is (a store pick, or an external id the store already holds)."""
    if pick["source"] == "store":
        return str(pick["id"])
    keys = {"lesswrong": [("fm_post", pick["id"])],
            "arxiv": [("arxiv", pick["id"]), ("doi", f"10.48550/arxiv.{pick['id']}")]}[pick["source"]]
    return next((index[k] for k in keys if k in index), None)


@dataclass
class HeavyItem:
    pick: Row
    origin: str
    doc_id: str
    group: str
    channel: str
    item: Row  # stratum-less body: hard, license_class, production_asks, input, input_digest
    proxy: str
    full_text: str | None = None
    text_origin: str | None = None


def heavy_triage(net: Net, src: F.Source, picks: Sequence[Row], seed: str) -> list[HeavyItem]:
    store = {c.doc_id: c for c in F.triage_cands(src, seed)}
    index = store_index(src)
    arx = arxiv_records(net, [p["id"] for p in picks if p["source"] == "arxiv"])
    out = []
    for p in picks:
        doc = _picked_doc(index, p)
        if doc is not None:
            if doc not in store:
                raise AmendError(f"pick {p['id']}: store doc {doc} is not a live row")
            c = store[doc]
            if not c.item["production_asks"]:
                raise AmendError(f"pick {p['id']}: store doc {doc} has no basis (production does not ask it)")
            out.append(HeavyItem(p, "store", doc, c.group, c.channel, {"hard": c.hard, **c.item}, c.proxy))
            continue
        if p["source"] == "lesswrong":
            inp, info = forum_input(net, p["id"])
            channel, origin_note = "lesswrong", info.get("excerpt_origin")
        else:
            e = arx.get(p["id"])
            inp, channel, origin_note = (arxiv_input(e) if e else None), "arxiv", None
        if inp is None or not has_basis(inp):
            raise AmendError(f"pick {p['source']}:{p['id']}: no record with a basis")
        group = "forum" if channel == "lesswrong" else "paper"
        body = "\n".join(x for x in (inp["abstract"], inp["excerpt"]) if x)
        out.append(HeavyItem(p, "external", f"ext:{p['source']}:{p['id']}", group, channel,
                             {"hard": F.hard_tag(group, inp["title"] or "", inp["abstract"], inp["excerpt"]),
                              "license_class": license_of(channel), "production_asks": True, "input": inp,
                              "input_digest": F.sha256_hex(inp["record"])},
                             F.proxy(inp["title"] or "", body), text_origin=origin_note))
    return out


def screen_row(doc_id: str, title: str | None, text: str, channel: str, role: str) -> Row:
    """The minimal documents row ``freeze.screen_message`` needs for an external text."""
    from stack.core.hashes import text_digest

    sid = f"{doc_id}#src"
    return {"doc_id": doc_id, "metadata": {"title": title},
            "identity": {"anchor_source_id": sid,
                         "members": [{"source_id": sid, "channel": channel, "license_class": license_of(channel)}]},
            "content": {"current_text_sha256": text_digest(text), "current_source_id": sid, "current_role": role}}


def screen_input(row: Row, full: str) -> tuple[Row, str]:
    """``(input, input_digest)`` as ``freeze.screen_cands`` builds them for ``row`` and its whole text."""
    import workflows.annotate.screen.main as screen_wf

    user = F.screen_message(row, full)
    sent = screen_wf._truncate(full, screen_wf._MAX_DOC_CHARS)
    if not user.endswith(sent):
        raise RuntimeError(f"{row['doc_id']}: the captured screen message does not end with the truncated text")
    return ({"title": row["metadata"]["title"], "text": sent, "text_digest": row["content"]["current_text_sha256"],
             "basis_role": row["content"]["current_role"], "full_text_digest": F.sha256_hex(full),
             "full_text_chars": len(full)}, F.sha256_hex(user))


def heavy_screen(net: Net, src: F.Source, picks: Sequence[Row], seed: str) -> list[HeavyItem]:
    store = {c.doc_id: c for c in F.screen_cands(src, seed)}
    index = store_index(src)
    out = []
    for p in picks:
        doc = _picked_doc(index, p)
        if doc is not None:
            if doc not in store:
                raise AmendError(f"pick {p['id']}: store doc {doc} has no current text")
            c = store[doc]
            out.append(HeavyItem(p, "store", doc, c.group, c.channel, {"hard": None, **c.item}, c.proxy,
                                 full_text=c.full_text, text_origin="store"))
            continue
        if p["source"] != "lesswrong":
            raise AmendError(f"pick {p['source']}:{p['id']}: external screen items are LessWrong/AF posts only")
        inp, info = forum_input(net, p["id"])
        full = info.get("full_text")
        if inp is None or not full:
            raise AmendError(f"pick lesswrong:{p['id']}: no post text")
        doc_id = f"ext:lesswrong:{p['id']}"
        sinp, digest = screen_input(screen_row(doc_id, inp["title"], full, "lesswrong", "post"), full)
        out.append(HeavyItem(p, "external", doc_id, "forum", "lesswrong",
                             {"hard": None, "license_class": license_of("lesswrong"), "production_asks": True,
                              "input": sinp, "input_digest": digest},
                             F.proxy(inp["title"] or "", sinp["text"]), full_text=full,
                             text_origin=str(info.get("excerpt_origin"))))
    return out


def _next_id(prefix: str, items: Sequence[Row]) -> int:
    nums = [int(i["item_id"].split("-")[1]) for i in items if i["item_id"].startswith(prefix + "-")]
    return max(nums, default=0) + 1


def heavy_items(new: Sequence[HeavyItem], existing: Sequence[Row], prefix: str, seed: str,
                first: int | None = None) -> list[Row]:
    """The appended rows: ids continue the set (or from ``first``), hash order; split 50/50 per
    ``(channel_group, intent)`` inside the new items. Refuses a document already in the set and an
    id already taken."""
    have_docs = {i["doc_id"] for i in existing}
    have_ids = {i["item_id"] for i in existing}
    dup = [h.doc_id for h in new if h.doc_id in have_docs]
    if dup or len({h.doc_id for h in new}) != len(new):
        raise AmendError(f"documents already in the set or picked twice: {dup or 'duplicate picks'}")
    cands = [F.Cand(doc_id=h.doc_id, order=F.order_key(seed, h.doc_id), group=h.group,
                    channel=h.channel, proxy=str(h.pick.get("intent")), hard=None, eligible=True) for h in new]
    split = F.assign_split(cands)
    by = {h.doc_id: h for h in new}
    start = first if first is not None else _next_id(prefix, existing)
    rows = []
    for n, c in enumerate(sorted(cands, key=lambda c: c.order)):
        h = by[c.doc_id]
        iid = f"{prefix}-{start + n:04d}"
        if iid in have_ids:
            raise AmendError(f"item id {iid} is already in the set")
        rows.append({"item_id": iid, "doc_id": h.doc_id, "split": split[h.doc_id],
                     "stratum": {"channel_group": h.group, "channel": h.channel, "proxy": h.proxy},
                     "hard": h.item["hard"], "license_class": h.item["license_class"],
                     "production_asks": h.item["production_asks"], "input": h.item["input"],
                     "input_digest": h.item["input_digest"], "sr_heavy": True, "origin": h.origin,
                     **({"text_origin": h.text_origin} if h.text_origin else {})})
    return rows


def append_items(set_dir: Path, rows: Sequence[Row], amendment: Row) -> Row:
    """Append ``rows`` to ``items.jsonl`` (existing bytes kept, checked after the write) and the
    amendment to ``manifest.json`` (every key kept). Returns the amendment as written."""
    path, mpath = set_dir / "items.jsonl", set_dir / "manifest.json"
    old = path.read_bytes()
    if old and not old.endswith(b"\n"):
        raise AmendError(f"{path} does not end with a newline")
    manifest = json.loads(mpath.read_text("utf-8"))
    new = old + jsonl(rows)
    amend = {**amendment, "added": [r["item_id"] for r in rows],
             "items_sha256_before": hashlib.sha256(old).hexdigest(),
             "items_sha256_after": hashlib.sha256(new).hexdigest()}
    path.write_bytes(new)
    if path.read_bytes()[: len(old)] != old:
        raise AmendError(f"{path}: the existing bytes changed")
    manifest.setdefault("amendments", []).append(amend)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return amend


def append_heavy(net: Net, src: F.Source, task: str, set_dir: Path, picks: Sequence[Row], packets: Path, *,
                 exclude: Iterable[str] = (), seed: str = HEAVY_SEED, first: int | None = None) -> Row:
    """Build, check and append the SR-heavy items of ``task`` (module docstring)."""
    excluded = {_ext_key(d) for d in exclude}
    clash = [p["id"] for p in picks if _ext_key(str(p["id"])) in excluded]
    if clash:
        raise AmendError(f"picks in the SR recall sample: {clash}")
    existing = read_jsonl(set_dir / "items.jsonl")
    if task == "triage":
        new, prefix = heavy_triage(net, src, picks, seed), "tri"
    elif task == "screen":
        new, prefix = heavy_screen(net, src, picks, seed), "scr"
    else:
        raise ValueError(f"task {task!r}")
    clash = [h.doc_id for h in new if _ext_key(h.doc_id) in excluded]
    if clash:
        raise AmendError(f"picks resolve to documents in the SR recall sample: {clash}")
    rows = heavy_items(new, existing, prefix, seed, first)
    if task == "screen":
        full = {h.doc_id: h.full_text or "" for h in new}
        for r in rows:
            p = F.text_path(set_dir, r["item_id"])
            if p.exists():
                raise AmendError(f"{p} exists")
        for r in rows:
            text = full[r["doc_id"]]
            if F.sha256_hex(text) != r["input"]["full_text_digest"]:
                raise RuntimeError(f"{r['item_id']}: whole text does not match its digest")
            F.text_path(set_dir, r["item_id"]).parent.mkdir(parents=True, exist_ok=True)
            F.text_path(set_dir, r["item_id"]).write_bytes(F.gzip_text(text))
    by_doc = {h.doc_id: h for h in new}
    amendment = {
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "by": "track E2",
        "what": "SR-heavy items (docs/scope/shallow-review.md (c)-(e)); chosen by query and title only, unlabelled",
        "seed": seed, "order": "sha256(seed + doc_id) hex, ascending; ids continue the set",
        "split": "50/50 inside the new items per (channel_group, intent), hash order",
        "source_store": str(src.store), "documents_in": src.documents, "documents_head": src.head,
        **code_provenance(),
        "sources": [{"item_id": r["item_id"], "doc_id": r["doc_id"], "origin": r["origin"],
                     "source": by_doc[r["doc_id"]].pick["source"], "query": by_doc[r["doc_id"]].pick.get("query"),
                     "topic": by_doc[r["doc_id"]].pick.get("topic"),
                     "intent": by_doc[r["doc_id"]].pick.get("intent"),
                     **({"text_origin": r["text_origin"]} if r.get("text_origin") else {})} for r in rows],
        "net_calls": dict(net.calls)}
    amend = append_items(set_dir, rows, amendment)
    packets.mkdir(parents=True, exist_ok=True)
    if task == "triage":
        (packets / "triage-batch-01.jsonl").write_bytes(
            jsonl({"item_id": r["item_id"], "record": r["input"]["record"]} for r in rows))
    else:
        (packets / "screen-batch-01.jsonl").write_bytes(
            jsonl({"item_id": r["item_id"], "title": r["input"]["title"], "text": r["input"]["text"]} for r in rows))
    return amend


# ---------------------------------------------------------------- CLI
app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Shallow Review 2025 eval data: the recall set and the SR-heavy amendments.")

StoreArg = Annotated[Path, typer.Option("--store", help="Store root (read-only).")]
InArg = Annotated[str, typer.Option("--in", help="The documents version to read: data/documents/<series>/ "
                                    "(its last file) or an exact file; required, no default series.")]
CacheArg = Annotated[Path, typer.Option("--cache", help="Raw-response journal directory (reruns replay).")]


@app.command()
def recall(store: StoreArg, in_: InArg, cache: CacheArg,
           out: Annotated[Path, typer.Option("--out", help="evals/triage/<set-id>")] = F.REPO / "evals" / "triage" / "sr2025-recall",
           seed: Annotated[str, typer.Option("--seed")] = RECALL_SEED,
           force: Annotated[bool, typer.Option("--force", help="Replace an existing items.jsonl.")] = False) -> None:
    """Build the SR 2025 recall set (module docstring)."""
    net = Net(journal_send(cache))
    m = build_recall(net, F.load_source(store.absolute(), in_), out, seed=seed, force=force)
    typer.echo(f"papers.csv: {m['source']['rows']} rows, sha256 {m['source']['sha256'][:16]}, "
               f"columns {m['source']['columns']}")
    typer.echo(f"link kinds (rows): {json.dumps(m['link_kinds']['rows'])}")
    typer.echo(f"link kinds (unique {m['link_kinds']['unique_total']}): {json.dumps(m['link_kinds']['unique_links'])}")
    for k, v in m["resolution"].items():
        typer.echo(f"  {k}: {json.dumps(v)}")
    typer.echo(f"counts: {json.dumps(m['counts'])}")
    checks = m["overlap"]["external_vs_store_record"]
    typer.echo(f"store overlap: {m['overlap']['store_items']} items; external record == store record "
               f"{sum(c['record_equal'] for c in checks)}/{len(checks)}")
    typer.echo(f"net calls: {json.dumps(m['net_calls'])}  items_sha256: {m['items_sha256'][:16]}")


@app.command("candidates")
def candidates_cmd(store: StoreArg, in_: InArg, cache: CacheArg,
                   queries: Annotated[Path, typer.Option("--queries", help="JSON: {store, lesswrong, arxiv} query lists.")],
                   out: Annotated[Path, typer.Option("--out", help="Candidates JSONL.")],
                   exclude_set: Annotated[list[Path], typer.Option("--exclude-set", help="items.jsonl whose docs to skip.")] = []) -> None:  # noqa: B006
    """List candidate titles for SR-heavy picks (titles and the query that found them)."""
    excl = {r["doc_id"] for p in exclude_set for r in read_jsonl(p)}
    net = Net(journal_send(cache))
    rows = candidates(net, F.load_source(store.absolute(), in_), json.loads(queries.read_text("utf-8")), excl)
    out.write_bytes(jsonl(rows))
    typer.echo(f"{len(rows)} candidates -> {out}; net calls {json.dumps(dict(net.calls))}")


@app.command()
def append(store: StoreArg, in_: InArg, cache: CacheArg,
           task: Annotated[str, typer.Option("--task", help="triage | screen")],
           set_id: Annotated[str, typer.Option("--set", help="Set id, e.g. 2026-10-12a.")],
           picks: Annotated[Path, typer.Option("--picks", help="JSON list of picks.")],
           packets: Annotated[Path, typer.Option("--packets", help="Labeler packet directory.")],
           recall_items: Annotated[Path | None, typer.Option("--exclude-recall", help="Recall items.jsonl.")] = None,
           evals_root: Annotated[Path, typer.Option("--evals")] = F.REPO / "evals",
           first: Annotated[int | None, typer.Option("--first-id", help="First new item number.")] = None) -> None:
    """Append SR-heavy items to a frozen triage or screen set (module docstring)."""
    excl = [r["doc_id"] for r in read_jsonl(recall_items)] if recall_items else []
    net = Net(journal_send(cache))
    a = append_heavy(net, F.load_source(store.absolute(), in_), task, evals_root / task / set_id,
                     json.loads(picks.read_text("utf-8")), packets, exclude=excl, first=first)
    typer.echo(f"added {len(a['added'])}: {a['added'][0]} .. {a['added'][-1]}")
    typer.echo(f"items_sha256 {a['items_sha256_before'][:16]} -> {a['items_sha256_after'][:16]}")
    typer.echo("origin: " + json.dumps(collections.Counter(s["origin"] for s in a["sources"])))
    typer.echo("intent: " + json.dumps(collections.Counter(s["intent"] for s in a["sources"])))
    typer.echo("topic: " + json.dumps(collections.Counter(s["topic"] for s in a["sources"])))


if __name__ == "__main__":
    app()
