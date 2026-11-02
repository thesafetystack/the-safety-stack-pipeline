"""X (Twitter) API v2: the client and the ``collect.x-lookup`` stage (round 4, track X; proposal
``docs/design/link-crawling.md`` §4.9; DESIGN §5.6 "X posts"). The stage module
``workflows/collect/x-lookup/main.py`` is thin.

**Keys looked up.** ``x_post`` keys of the ``documents`` head (``--in data/documents/<series>/``,
optional here: builds pass it to every ``*-lookup`` line) plus the stage option ``ids`` (post ids
or status URLs; operator runs before ``collect.links`` exists). A key is **due** when its source
``(x, <post id>)`` has no row in the own chain, or its latest row is a miss with ``miss_count <
miss_cap``; an observed or retracted post is done and never asked again. **Passes** (D226 (c)):
a key whose last lookup was a miss less than ``relist_after_h`` hours ago (stage option, default 12)
is not asked again (``not_due``), so the passes of one build do not spend ``miss_cap`` on one day;
the keys of a failed or budget-cut batch have no row and are due in the next pass. Retired and masked
documents are skipped. ``max_records`` (option, or ``STACK_X_LIMIT``) caps the keys per run.

**Requests** (verified live 2026-10-12 on the project's pay-per-use app, RUNLOG (X)):
``GET /2/tweets?ids=…`` (≤ 100 ids; :data:`TWEET_FIELDS`; expansions ``referenced_tweets.id``, plus
``author_id`` user objects only with ``options.users``), ``GET /2/tweets/search/recent`` (7 days)
and ``GET /2/tweets/search/all`` (full archive) for threads, ``GET /2/usage/tweets`` (the
project's post-read counter, no posts) before and after the run when ``options.usage_check``.
Bearer token from ``X_API_BEARER_TOKEN`` through :func:`stack.llm.provider_key` (the environment,
else the file ``$STACK_ENV_FILE`` names); it is sent as a request header only, never written to a
record, count, coverage row or message. Without a token the stage makes no request, writes
nothing for its keys (they stay due) and ends ``scope_complete = false`` (``x_no_credentials``).

**Threads** (``options.threads``, default on; one document per thread: a thread is rendered once;
D243-D247). A looked-up post is a thread candidate when
it starts its conversation (``conversation_id == id``) or replies to its own author; reposts never
are. After all lookups of the run, the candidates are grouped by ``(conversation_id, author)`` and
each group gets one search ``conversation_id:<c> from:<author id> (to:<handle> OR -is:reply)`` with
``expansions=referenced_tweets.id`` (the quoted posts): the author's posts in the conversation that
start it or answer the author (verified live; without the ``to:`` clause the author's replies to
commenters come back too, 27 of 88 posts billed in the first live run). ``to:`` takes the handle:
full-archive search ignores ``to:<numeric id>`` (live 2026-10-13: 1 of 14 posts; recent search
honours it). With no handle known (status URLs, mentions, the user expansion), the query is
``conversation_id:<c> from:<author id>`` and the chain rule drops the replies to others
(``x_thread_no_handle``). Recent search when the conversation root (its
snowflake time) is at most :data:`RECENT_DAYS` old, else full-archive search, paged up to
``max_thread_posts`` (default 25). Outcomes:

* **The author started the conversation** (the root, by the author, is among the posts): one
  **self-thread row**, ``external_id = thread:<conversation_id>:<author id>``
  (:func:`thread_external_id`), whose record (:func:`build_thread_record`) holds the chain
  (:func:`self_thread`: the root, the looked-up posts, and every post of the author answering a post
  already in the chain, in id order; other authors' posts never) and the posts it quotes. Its keys
  are the ``x_post`` of **every** member, so gather puts every document linking any member into
  the thread's one document, and its text renders the thread once, in order. A member post gets no
  row of its own and is never looked up again (:func:`thread_members`, ``keys_in_thread``).
* **Growth.** A newly linked post of a known thread is not a member yet, so it is due; its lookup
  and search rewrite the same thread row (same ``source_id``, same document) with the earlier
  members kept (seeds) and the new ones added. A thread is not polled otherwise.
* **Someone else's conversation** (``foreign``: a self-reply whose conversation's root is not the
  author's, or was deleted): a post row as for any reply, ``thread_status: foreign``, with its
  replied-to post as context; a reply to another author is never a candidate. The author's posts in
  another author's conversation can be several disjoint chains, so ``(conversation_id, author)``
  is no unit there.
* **Search failed or cut** (``pending``): each post looked up this run is written as a post row
  with ``thread_status: pending``; a later run retries the search (:func:`pending_threads`) and
  writes the thread row with those posts as seeds; the pending post rows stay (their documents
  merge into the thread's; the pointer takes the thread's newer text). Each search is a coverage
  window ``{x_thread, from}``; a failed search counts against ``miss_cap`` like a miss and waits
  ``relist_after_h`` like one (``not_due``); a budget-cut (``partial``) one is due in the next pass.
* Rows written before round 5 (one post row per looked-up post, the thread folded into its record)
  are not migrated: they stay done, and a later thread row merges with them on ``x_post``.
* Counts: ``x_thread_rows``, ``x_thread_posts`` (members written), ``x_threads_extended``,
  ``x_threads_foreign``, ``x_threads_ok``/``_pending``, ``x_thread_no_handle``, ``keys_in_thread``,
  ``x_quoted_in_thread``.

**Rows** (channel ``x``, group ``social``, ``license_class: api_tos``, ``miss_means: withdrawn``). A
**post row**: ``external_id`` = the post id; keys ``x_post`` plus the non-merging
``conversation_id``, ``reply_to_id``, ``quote_of``, ``repost_of``. A repost also carries its
original's ``x_post`` key, so gather collapses it onto the original (DESIGN §5.6). A **self-thread
row** ("Threads" above): ``x_post`` of every member, ``conversation_id``, ``quote_of``; title
``@handle (thread, n posts): …``, URL and date of the root. The record (:func:`build_record`:
``post``, ``thread`` (empty), ``quoted``, ``repost_of``, ``replied_to``, ``handles``,
``thread_status``; :func:`build_thread_record`: ``kind: thread``, ``conversation_id``,
``author_id``, ``post`` (the root), ``thread``, ``quotes``, ``handles``) is the row's ``provider_record``
in-band entry (D522, D524; canonical JSON in ``blobs/``) and the in-band ``post`` entry carries it as raw
JSON together with a Markdown rendering (:func:`render`) as its ``text`` (``text/markdown``; C33, D690), so acquire needs
no JSON converter.
``metadata``: title ``@handle: <first 100 characters>``, authors ``[@handle]``, ``published_at =
created_at``, ``doc_type = social_post``, ``lookup_of_source_ids`` = the document members carrying
the key (a thread row: of any member, accumulated). A quoted post returned in a lookup's
``includes`` is written as its own ``x`` row (it is a post Zvi's reader sees in full), unless it is
a member of a self-thread row. Errors per id (deleted, protected, suspended) and ids the answer
omits are miss rows. Handles come from the user expansion when on, else from status URLs (the
document member's ``canonical_url``, the ``ids`` option, ``entities.urls`` and ``mentions`` of the
returned posts); a post whose handle is unknown is shown as ``x.com/i/status/<id>``.

**Cost** (pay-per-use: $0.005 per post read, $0.010 per user, per the public price list; the project
counter ``/2/usage/tweets`` counted **only the posts in ``data``**, not those in ``includes``, and a
post re-read within 24 h once, RUNLOG (X)). Every request is reserved in the run's **X ledger**
before it is sent and settled after (:class:`Spend`; the LLM ledger's class, in memory, one per run,
:func:`stack.llm.ledger.run_ledger` with provider ``x``; the run record's ``spend.usd_micro.x``):
reservation = the most
the request can return (ids × 2 posts, plus users; a search page's ``max_results`` × 2, ``includes``
too); settlement = the distinct post and user ids this run had not counted yet, ``includes`` counted
too (conservative). Hard stops, per run: :data:`CAP_USD` ($10, Tomáš; ``STACK_X_CAP_USD`` may only
lower it) and the stage line's ``budget.max_usd`` (required: a paid API never runs without a budget). A request that would cross either is not sent; its keys stay due and the run ends
``scope_complete = false`` (``x_budget_stop``). Counts: ``x_cost_micro_usd`` (settled, this run),
``x_posts_counted``, ``x_users_counted``, ``x_project_usage_before``/``_after``.

**Transport.** :class:`stack.collect.base.Transport` at :data:`PACING` (1 request/s: full-archive
search allows one per second); 5xx and transport failures retried (2 s, 4 s); a 429 halts the run
(nothing further is sent, the window ``failed_transient[rate_limit]`` uncharged); 401, 402, 403
end the run (``provider_auth``). The lowest ``x-rate-limit-remaining`` seen per endpoint and its
``x-rate-limit-limit`` go to the counts.

**Relayed URLs** (proposal §4.9) are not written here: :func:`relayed_urls` lists the expanded
URLs a record carries, for ``collect.links``' shared link code (track L) to turn into link rows
relayed by the post.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from stack.collect.base import RELIST_OPTIONS, CollectError, Collector, Pacing, RunHalted, Sender, Transport
from stack.collect.inband import json_record, record_entry
from stack.http import HttpResponse
from stack.llm.errors import BudgetExceeded
from stack.llm.ledger import Ledger, Reservation, run_ledger
from stack.provider_transport import retry_seconds
from stack.scholarly_records import keys_for, normalize_x_post
from stack.stage_runner import StageContext, StageResult

Row = dict[str, Any]

CHANNEL = "x"
API = "https://api.x.com/2"
LOOKUP_URL = f"{API}/tweets"
SEARCH_RECENT_URL = f"{API}/tweets/search/recent"
SEARCH_ALL_URL = f"{API}/tweets/search/all"
USAGE_URL = f"{API}/usage/tweets"
TOKEN_ENV = "X_API_BEARER_TOKEN"
LIMIT_ENV = "STACK_X_LIMIT"
CAP_ENV = "STACK_X_CAP_USD"
#: The X spend cap per run ($10; D213). A run's ledger refuses any request past it.
CAP_USD = 10.00
PRICE_POST_USD = 0.005
PRICE_USER_USD = 0.010
LOOKUP_WIDTH = 100
SEARCH_MIN, SEARCH_MAX = 10, 100
#: Recent search covers seven days; a root older than this goes to full-archive search.
RECENT_DAYS = 6.5
PROVIDER_VERSION = 1
DOC_TYPE = "social_post"
CONTENT_ROLE = "post"
#: The ``options`` keys :func:`run` reads (D434).
OPTIONS: frozenset[str] = frozenset({"ids", "max_records", "max_thread_posts", "threads", "usage_check",
                                     "users"}) | RELIST_OPTIONS
#: ``external_id`` prefix of a self-thread row (:func:`thread_external_id`).
THREAD_PREFIX = "thread:"
TWEET_FIELDS = ("created_at,author_id,conversation_id,lang,entities,note_tweet,referenced_tweets,"
                "in_reply_to_user_id,public_metrics,edit_history_tweet_ids")
USER_FIELDS = "username,name"
#: X's epoch for snowflake ids (ms).
_SNOWFLAKE_EPOCH_MS = 1288834974657
PACING = Pacing(rps=1.0, retry_delays_s=(2.0, 4.0),
                retry_statuses=frozenset({0, 500, 502, 503, 504}))

_STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)"
    r"(/(?:photo|video)/\d+)?", re.IGNORECASE)
_TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+")

# Transport seams: tests install a ``tests/fakes/http.FakeHttp`` as ``SEND`` and a no-op ``_sleep``.
SEND: Sender | None = None
_T: Transport | None = None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ─────────────────────────────────────────────────────────────── pure helpers
def post_time(post_id: str) -> dt.datetime:
    """The creation time encoded in a snowflake post id (posts since late 2010)."""
    ms = (int(post_id) >> 22) + _SNOWFLAKE_EPOCH_MS
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC)


def full_text(post: Mapping[str, Any]) -> str:
    """The post's whole text: ``note_tweet.text`` (posts over 280 characters) else ``text``."""
    note = post.get("note_tweet") or {}
    return str(note.get("text") or post.get("text") or "")


def _urls(post: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``entities.urls`` of the post and of its note (the note's own entities when present)."""
    out = list(((post.get("note_tweet") or {}).get("entities") or {}).get("urls") or [])
    out += list((post.get("entities") or {}).get("urls") or [])
    return [u for u in out if isinstance(u, Mapping)]


def _media(url: str) -> bool:
    m = _STATUS_URL_RE.match(url or "")
    return bool(m and m.group(3))


def expanded_text(post: Mapping[str, Any]) -> str:
    """:func:`full_text` with each ``t.co`` link replaced by its expanded URL; media links
    (``…/photo/1``, ``…/video/1``) become ``[media]``."""
    text = full_text(post)
    repl = {}
    for u in _urls(post):
        short, exp = u.get("url"), u.get("expanded_url")
        if short and exp:
            repl[str(short)] = "[media]" if _media(str(exp)) else str(exp)
    return _TCO_RE.sub(lambda m: repl.get(m.group(0), m.group(0)), text).strip()


def referenced(post: Mapping[str, Any], kind: str) -> str | None:
    """The id of the post's ``referenced_tweets`` entry of ``kind`` (``quoted``, ``replied_to``,
    ``retweeted``), or None."""
    for r in post.get("referenced_tweets") or []:
        if isinstance(r, Mapping) and r.get("type") == kind and r.get("id"):
            return str(r["id"])
    return None


def harvest_handles(posts: Iterable[Mapping[str, Any]], by_post: dict[str, str],
                    by_user: dict[str, str]) -> None:
    """Fill ``post id → handle`` from status URLs in ``entities.urls`` and ``user id → handle``
    from ``mentions`` and from posts whose handle is known."""
    posts = [p for p in posts if isinstance(p, Mapping)]
    for p in posts:
        for u in _urls(p):
            m = _STATUS_URL_RE.match(str(u.get("expanded_url") or ""))
            if m and m.group(1).lower() != "i":
                by_post.setdefault(m.group(2), m.group(1))
        for mention in (p.get("entities") or {}).get("mentions") or []:
            if isinstance(mention, Mapping) and mention.get("id") and mention.get("username"):
                by_user.setdefault(str(mention["id"]), str(mention["username"]))
    for p in posts:
        h = by_post.get(str(p.get("id")))
        if h and p.get("author_id"):
            by_user.setdefault(str(p["author_id"]), h)


def handle_from_url(url: str | None) -> tuple[str, str] | None:
    """``(post id, handle)`` from an x.com/twitter.com status URL, else None."""
    m = _STATUS_URL_RE.match(url or "")
    if m and m.group(1).lower() != "i":
        return m.group(2), m.group(1)
    return None


def thread_external_id(cid: str, author_id: str) -> str:
    """The ``external_id`` of a self-thread row (module docstring): ``thread:<conversation id>:<author
    id>``. Post rows keep the bare post id."""
    return f"{THREAD_PREFIX}{cid}:{author_id}"


def self_thread(posts: Iterable[Mapping[str, Any]], author_id: str, root_id: str,
                seeds: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The author's own reply chain in a conversation, in id order: the root (when it is the
    author's), the ``seeds`` (the looked-up members and an earlier version's members, kept when
    the search no longer returns them or their parent is the author's reply to someone else) and
    every post of the author that replies to a post already in the chain. Other authors' posts
    never enter; a post id appears once."""
    mine: dict[str, dict[str, Any]] = {}
    for p in posts:
        if str(p.get("author_id")) == author_id and p.get("id"):
            mine.setdefault(str(p["id"]), dict(p))
    chain: dict[str, dict[str, Any]] = {}
    keep = {root_id, *(str(s) for s in seeds)}
    for pid in sorted(mine, key=int):
        p = mine[pid]
        if pid in keep or referenced(p, "replied_to") in chain:
            chain[pid] = p
    return list(chain.values())


def is_thread_candidate(post: Mapping[str, Any]) -> bool:
    """Starts its conversation, or replies to its own author (module docstring)."""
    if not post.get("conversation_id") or not post.get("author_id"):
        return False
    return (post["conversation_id"] == post.get("id")
            or str(post.get("in_reply_to_user_id") or "") == str(post["author_id"]))


def is_own_conversation(posts: Iterable[Mapping[str, Any]], cid: str, author_id: str) -> bool:
    """Whether ``posts`` hold the conversation's root (id = ``cid``) by ``author_id``: the author
    started the conversation, so ``(cid, author)`` is a self-thread (module docstring)."""
    return any(str(p.get("id")) == cid and str(p.get("author_id")) == author_id for p in posts)


def build_record(post: Mapping[str, Any], *, includes: Mapping[str, Mapping[str, Any]] | None = None,
                 handles: Mapping[str, str] | None = None,
                 thread_status: str | None = None) -> dict[str, Any]:
    """The source record of one post row (module docstring). ``includes`` = ``post id → post``
    of the answer's ``includes.tweets``; ``handles`` = ``user id → handle``, kept for the
    record's authors; ``thread_status`` = ``pending`` (a thread candidate whose search failed or
    was cut: retried in a later run), ``foreign`` (a self-reply in a conversation the author did
    not start) or None (not a candidate, or threads off). ``thread`` stays empty: a self-thread is
    its own row (:func:`build_thread_record`)."""
    inc = includes or {}
    q, r, o = referenced(post, "quoted"), referenced(post, "replied_to"), referenced(post, "retweeted")
    rec = {"post": dict(post), "thread": [],
           "quoted": dict(inc[q]) if q and q in inc else None,
           "repost_of": dict(inc[o]) if o and o in inc else None,
           "replied_to": dict(inc[r]) if r and r in inc else None,
           "thread_status": thread_status}
    rec["handles"] = record_handles(rec, handles or {})
    return rec


def build_thread_record(chain: list[dict[str, Any]], *, cid: str, author_id: str,
                        quotes: Mapping[str, Mapping[str, Any]] | None = None,
                        handles: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The source record of a self-thread row: ``kind: thread``, the conversation and author,
    ``post`` = the first post (the root), ``thread`` = the chain in id order, ``quotes`` = ``post
    id → post`` for the posts the chain quotes that an answer returned (search or lookup
    ``includes``); ``quoted``, ``repost_of``, ``replied_to`` stay None (the channel's
    ``record_projection`` reads ``post``, ``thread``, ``quoted``, ``repost_of``)."""
    if not chain:
        raise CollectError(f"thread {cid}/{author_id}: an empty chain has no row")
    inc = quotes or {}
    qids = sorted({q for p in chain if (q := referenced(p, "quoted"))})
    rec = {"kind": "thread", "conversation_id": cid, "author_id": author_id,
           "post": dict(chain[0]), "thread": [dict(p) for p in chain],
           "quotes": {q: dict(inc[q]) for q in qids if q in inc},
           "quoted": None, "repost_of": None, "replied_to": None, "thread_status": "ok"}
    rec["handles"] = record_handles(rec, handles or {})
    return rec


def _record_posts(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [p for p in [record.get("post"), *(record.get("thread") or []), record.get("quoted"),
                        record.get("repost_of"), record.get("replied_to"),
                        *(record.get("quotes") or {}).values()] if p]


def record_handles(record: Mapping[str, Any], known: Mapping[str, str]) -> dict[str, str]:
    """``user id → handle`` for the authors of the record's posts that ``known`` names."""
    authors = {str(p.get("author_id")) for p in _record_posts(record)}
    return {a: known[a] for a in sorted(authors) if a in known}


def _who(post: Mapping[str, Any], handles: Mapping[str, str]) -> str:
    h = handles.get(str(post.get("author_id")))
    return f"@{h}" if h else f"X user {post.get('author_id')}"


def status_url(post_id: str, handle: str | None) -> str:
    return f"https://x.com/{handle or 'i'}/status/{post_id}"


def _when(post: Mapping[str, Any]) -> str:
    ts = str(post.get("created_at") or "")
    return ts[:16].replace("T", " ") + " UTC" if ts else "date unknown"


def _quote_block(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def is_thread_record(record: Mapping[str, Any]) -> bool:
    return record.get("kind") == "thread"


def render(record: Mapping[str, Any]) -> str:
    """The Markdown text of a record (the in-band ``post`` entry's text side). A post: author,
    date, URL, the full text with expanded links, then the quoted, reposted and replied-to posts.
    A self-thread: author, date and URL of the root, then every post in order (``[i/n]``), each
    followed by the post it quotes; a one-post thread reads as a post."""
    handles = record.get("handles") or {}
    post = record["post"]
    if is_thread_record(record):
        thread, quotes = record["thread"], record.get("quotes") or {}
        if len(thread) == 1:
            return render({"post": post, "quoted": quotes.get(referenced(post, "quoted") or ""),
                           "handles": handles})
        lines = [f"Thread by {_who(post, handles)} on X ({len(thread)} posts), {_when(post)}",
                 status_url(str(post["id"]), handles.get(str(post.get("author_id")))), ""]
        for i, p in enumerate(thread, 1):
            lines += [f"[{i}/{len(thread)}] {expanded_text(p)}", ""]
            q = quotes.get(referenced(p, "quoted") or "")
            if q:
                lines += [f"Quoting {_who(q, handles)} ({_when(q)}):", _quote_block(expanded_text(q)), ""]
        return "\n".join(lines).strip() + "\n"
    shown = record.get("repost_of") or post
    lines = [f"{_who(post, handles)} on X, {_when(post)}",
             status_url(str(post["id"]), handles.get(str(post.get("author_id")))), ""]
    if record.get("repost_of"):
        lines += [f"Repost of {_who(shown, handles)} ({_when(shown)}):", ""]
    lines += [expanded_text(shown), ""]
    for label, key in (("Quoting", "quoted"), ("In reply to", "replied_to")):
        other = record.get(key)
        if other:
            lines += [f"{label} {_who(other, handles)} ({_when(other)}):",
                      _quote_block(expanded_text(other)), ""]
    return "\n".join(lines).strip() + "\n"


def title_of(record: Mapping[str, Any]) -> str:
    handles = record.get("handles") or {}
    post = record["post"]
    shown = record.get("repost_of") or post
    text = " ".join(expanded_text(shown).split())
    n = len(record.get("thread") or []) if is_thread_record(record) else 1
    who = _who(post, handles) + (f" (thread, {n} posts)" if n > 1 else "")
    return f"{who}: {text[:100]}" + ("…" if len(text) > 100 else "")


def relayed_urls(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """The expanded URLs the record's posts carry (the post, its thread, quoted and reposted
    posts), media links and links to the record's own posts left out, first occurrence each:
    ``{url, post_id}`` for the link code (proposal §4.9, track L)."""
    posts = [p for p in _record_posts(record) if p is not record.get("replied_to")]
    own = {str(p.get("id")) for p in posts}
    out: dict[str, dict[str, str]] = {}
    for p in posts:
        for u in _urls(p):
            exp = str(u.get("expanded_url") or "")
            if not exp or _media(exp):
                continue
            hit = handle_from_url(exp)
            if hit and hit[0] in own:
                continue
            out.setdefault(exp, {"url": exp, "post_id": str(p.get("id"))})
    return list(out.values())


def row_keys(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    """A post: ``x_post`` (and a repost's original's ``x_post``) plus the non-merging keys. A
    self-thread: ``x_post`` of **every** member (so a link to any member merges into the thread's
    document), ``conversation_id`` and ``quote_of`` of the quoted posts."""
    post = record["post"]
    if is_thread_record(record):
        pairs = [("x_post", str(p["id"])) for p in record["thread"]]
        pairs.append(("conversation_id", str(record["conversation_id"])))
        pairs += [("quote_of", q) for p in record["thread"] if (q := referenced(p, "quoted"))]
        return [(k["kind"], k["value"]) for k in keys_for(pairs)]
    pairs = [("x_post", str(post["id"]))]
    if post.get("conversation_id"):
        pairs.append(("conversation_id", str(post["conversation_id"])))
    for kind, ref in (("reply_to_id", "replied_to"), ("quote_of", "quoted"), ("repost_of", "retweeted")):
        rid = referenced(post, ref)
        if rid:
            pairs.append((kind, rid))
            if kind == "repost_of":
                pairs.append(("x_post", rid))  # a repost collapses onto its original (§5.6)
    return [(k["kind"], k["value"]) for k in keys_for(pairs)]


# ─────────────────────────────────────────────────────────────── spend
def x_ledger(run_id: str) -> Ledger:
    """The run's X ledger (:func:`stack.llm.ledger.run_ledger`, provider ``x``); cap
    :data:`CAP_USD`, lowered (never raised) by ``$STACK_X_CAP_USD``."""
    cap = CAP_USD
    raw = os.environ.get(CAP_ENV, "").strip()
    if raw:
        try:
            cap = float(raw)
        except ValueError:
            raise CollectError(f"${CAP_ENV}={raw!r} is not a number") from None
        if not 0 < cap <= CAP_USD:
            raise CollectError(f"${CAP_ENV}={raw}: must be in (0, {CAP_USD}] (it may only lower the cap)")
    return run_ledger(run_id, "x", cap_usd=cap)


class Spend:
    """Reserve → send → settle, per request, in the X ledger (module docstring)."""

    def __init__(self, c: Collector, ledger: Ledger, budget_usd: float) -> None:
        self.c, self.ledger, self.budget = c, ledger, budget_usd
        self.posts: set[str] = set()
        self.users: set[str] = set()

    def headroom(self) -> float:
        """USD this run may still reserve (the lower of the run budget and the ledger cap)."""
        cap = self.ledger.cap_usd if self.ledger.cap_usd is not None else self.budget
        return min(self.budget, cap) - self.ledger.run_total(self.c.ctx.run_id)

    def reserve(self, model: str, usd: float) -> Reservation:
        return self.ledger.reserve(run_id=self.c.ctx.run_id, stage=self.c.ctx.stage, model=model,
                                   attempt="first", amount_usd=usd, tokens_in=0, tokens_out=0,
                                   run_budget_usd=self.budget)

    def settle(self, res: Reservation, resp: HttpResponse | None, body: Any) -> None:
        """Reconcile to the posts and users this answer adds (0 for a non-200); release when no
        answer arrived."""
        if resp is None or resp.status is None:
            self.ledger.release(res)
            return
        new_posts: set[str] = set()
        new_users: set[str] = set()
        if resp.status == 200 and isinstance(body, Mapping):
            data = body.get("data")
            items = data if isinstance(data, list) else [data] if isinstance(data, Mapping) else []
            inc = body.get("includes") or {}
            ids = {str(p.get("id")) for p in [*items, *(inc.get("tweets") or [])]
                   if isinstance(p, Mapping) and p.get("id")}
            uids = {str(u.get("id")) for u in inc.get("users") or [] if isinstance(u, Mapping) and u.get("id")}
            new_posts, new_users = ids - self.posts, uids - self.users
            self.posts |= new_posts
            self.users |= new_users
            self.c.counts["x_posts_returned"] = self.c.counts.get("x_posts_returned", 0) + len(items)
        actual = len(new_posts) * PRICE_POST_USD + len(new_users) * PRICE_USER_USD
        self.ledger.reconcile(res, actual_usd=actual, tokens_in=len(new_posts), tokens_out=len(new_users))
        for k, n in (("x_posts_counted", len(new_posts)), ("x_users_counted", len(new_users)),
                     ("x_cost_micro_usd", round(actual * 1e6))):
            self.c.counts[k] = self.c.counts.get(k, 0) + n


# ─────────────────────────────────────────────────────────────── requests
class Stop(Exception):
    """End the run's requests: ``reason`` ∈ ``budget`` | ``auth``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _body(resp: HttpResponse) -> Any:
    try:
        return json.loads(resp.body) if resp.body else None
    except (ValueError, UnicodeDecodeError):
        return None


def _rate(c: Collector, endpoint: str, resp: HttpResponse) -> None:
    """Keep the lowest ``x-rate-limit-remaining`` (and the limit) seen per endpoint."""
    h = resp.headers or {}
    for name, key in (("x-rate-limit-remaining", f"x_ratelimit_remaining_{endpoint}"),
                      ("x-rate-limit-limit", f"x_ratelimit_limit_{endpoint}")):
        try:
            v = int(h[name])
        except (KeyError, ValueError, TypeError):
            continue
        c.counts[key] = min(c.counts.get(key, v), v) if "remaining" in key else v


def request(c: Collector, spend: Spend | None, endpoint: str, url: str, params: Mapping[str, Any],
            token: str, reserve_usd: float) -> tuple[HttpResponse, Any]:
    """One paid (``spend``) or free request. Raises :class:`Stop` when the ledger refuses the
    reservation (``budget``) or the provider refuses the credentials (``auth``), and
    :class:`RunHalted` on a 429."""
    if _T is None:
        raise CollectError("xapi.request called outside a run")
    res = None
    if spend is not None:
        try:
            res = spend.reserve(f"x-api/{endpoint}", reserve_usd)
        except BudgetExceeded:
            c.counts["x_budget_stop"] = 1
            raise Stop("budget") from None
    resp: HttpResponse | None = None
    body: Any = None
    try:
        resp = _T.fetch("GET", url, params=params, headers={"Authorization": f"Bearer {token}"})
        body = _body(resp)
    finally:
        if res is not None:
            assert spend is not None
            spend.settle(res, resp, body)
    _rate(c, endpoint, resp)
    if resp.status == 429:
        reset = (resp.headers or {}).get("x-rate-limit-reset")
        try:
            wait = max(float(reset) - time.time(), 0.0) if reset else retry_seconds(dict(resp.headers), resp.body)
        except ValueError:
            wait = 900.0
        raise c.halt(wait, f"api.x.com {endpoint}: 429, reset in {wait:.0f}s")
    if resp.status in (401, 402, 403):
        c.counts["x_auth_refused"] = int(resp.status)
    return resp, body


def _failure(resp: HttpResponse) -> tuple[str, bool]:
    if resp.status is None:
        return ("timeout" if "timeout" in (resp.error or "").lower() else "network"), False
    if resp.status in (401, 402, 403):
        return "provider_auth", False
    if resp.status >= 500:
        return "http_5xx", False
    return "query_invalid", True


def usage(c: Collector, token: str) -> int | None:
    """The project's post-read counter (``/2/usage/tweets``; no posts, not ledgered)."""
    resp, body = request(c, None, "usage", USAGE_URL, {}, token, 0.0)
    try:
        return int(body["data"]["project_usage"]) if resp.status == 200 else None
    except (KeyError, TypeError, ValueError):
        return None


def lookup_params(ids: list[str], users: bool) -> dict[str, Any]:
    exp = "referenced_tweets.id" + (",author_id,referenced_tweets.id.author_id" if users else "")
    params: dict[str, Any] = {"ids": ",".join(ids), "tweet.fields": TWEET_FIELDS, "expansions": exp}
    if users:
        params["user.fields"] = USER_FIELDS
    return params


def search_params(cid: str, author_id: str, page: int, token: str | None, *,
                  handle: str | None = None) -> dict[str, Any]:
    """The thread search (module docstring): with the author's ``handle`` known, the author's posts
    that start the conversation or answer the author (``to:`` takes the handle: full-archive
    search ignores a numeric ``to:``, live 2026-10-13); without it, every post of the author in
    the conversation (:func:`self_thread` drops the replies to others)."""
    to = f" (to:{handle} OR -is:reply)" if handle else ""
    q = f"conversation_id:{cid} from:{author_id}{to}"
    params: dict[str, Any] = {"query": q, "max_results": page, "tweet.fields": TWEET_FIELDS,
                              "expansions": "referenced_tweets.id"}
    if token:
        params["next_token"] = token
    return params


# ─────────────────────────────────────────────────────────────── the stage
class Due:
    """One post id to look up and the document members carrying its key."""

    def __init__(self, post_id: str) -> None:
        self.post_id = post_id
        self.lookup_of: set[str] = set()


def due_ids(c: Collector, docs: list[Row], extra: Iterable[str], miss_cap: int,
            handles: dict[str, str], *, miss_wait: dt.timedelta | None = None,
            members: Mapping[str, str] | None = None) -> tuple[list[Due], dict[str, int]]:
    """The post ids to look up this run (module docstring), in document then option order; fills
    ``handles`` (post id → handle) from member URLs and the option's URLs. ``miss_wait``: a key
    whose last lookup missed less than this ago is not due (``not_due``). ``members``: post ids
    already in a self-thread row (:func:`thread_members`), done (``keys_in_thread``)."""
    counts = {"docs_seen": 0, "keys_done": 0, "keys_in_thread": 0, "keys_exhausted": 0, "ids_invalid": 0,
              "not_due": 0}
    due: dict[str, Due] = {}
    in_thread = members or {}

    def consider(pid: str) -> Due | None:
        if pid in due:
            return due[pid]
        if pid in in_thread:
            counts["keys_in_thread"] += 1
            return None
        prior = c.prior(CHANNEL, pid)
        if prior is not None and prior["state"] in ("observed", "retracted"):
            counts["keys_done"] += 1
            return None
        if prior is not None and int(prior["metadata"]["miss_count"] or 0) >= miss_cap:
            counts["keys_exhausted"] += 1
            return None
        if miss_wait is not None and prior is not None and c.now - prior["observed_at"] < miss_wait:
            counts["not_due"] += 1
            return None
        due[pid] = Due(pid)
        return due[pid]

    for doc in sorted(docs, key=lambda d: d["doc_id"]):
        ident = doc["identity"]
        if doc["state"] == "retired" or ident["is_masked"]:
            continue
        counts["docs_seen"] += 1
        for m in ident["members"]:
            hit = handle_from_url(m.get("canonical_url"))
            if hit:
                handles.setdefault(*hit)
        for k in ident["keys"]:
            if k["kind"] == "x_post":
                d = consider(k["value"])
                if d is not None:
                    d.lookup_of.add(k["source_id"])
    for raw in extra:
        pid = normalize_x_post(str(raw))
        if not pid:
            counts["ids_invalid"] += 1
            continue
        hit = handle_from_url(str(raw))
        if hit:
            handles.setdefault(*hit)
        consider(pid)
    return list(due.values()), counts


def _documents(ctx: StageContext) -> list[Row]:
    keys = [k for k in ctx.inputs if k == "documents" or k.startswith("documents/")]
    if len(keys) > 1:
        raise CollectError(f"{ctx.stage}: at most one documents input (got {sorted(keys)})")
    if not keys:
        return []
    return ctx.view(keys[0]).select(["doc_id", "state", "identity"]).to_pylist()


def _own(ctx: StageContext, fam: str) -> list[Row]:
    pinned = ctx.prior.get(fam) or ctx.inputs.get(fam)
    return ctx.view(fam).to_pylist() if pinned is not None else []


def thread_query(cid: str, author_id: str) -> dict[str, str]:
    return {"x_thread": cid, "from": author_id}


def thread_members(ctx: StageContext) -> dict[str, str]:
    """``post id → external_id`` of the own chain's observed self-thread rows, over their ``x_post``
    keys: a member post is never looked up again (module docstring, "Growth")."""
    out: dict[str, str] = {}
    for r in _own(ctx, "sources"):
        ident = r["identity"]
        if r["state"] != "observed" or ident["channel"] != CHANNEL \
                or not str(ident["external_id"]).startswith(THREAD_PREFIX):
            continue
        for k in ident["keys"]:
            if k["kind"] == "x_post":
                out.setdefault(k["value"], str(ident["external_id"]))
    return out


def pending_threads(ctx: StageContext, miss_cap: int, c: Collector | None = None,
                    miss_wait: dt.timedelta | None = None) -> dict[tuple[str, str], list[Row]]:
    """Thread searches to retry: ``(conversation_id, author)`` whose coverage windows in the own
    chain have no ``ok`` and fewer than ``miss_cap`` failures, with the observed rows of the own
    chain whose record waits for that thread (``thread_status: pending``; their records are read
    from the blob store). With ``c`` and ``miss_wait``, a search whose newest attempt failed less
    than ``miss_wait`` ago is not retried yet (counted ``not_due`` on ``c``)."""
    by_query: dict[str, list[Row]] = {}
    for r in _own(ctx, "coverage"):
        if r["channel"] != CHANNEL:
            continue
        try:
            q = json.loads(r["query"])
        except ValueError:
            continue
        if isinstance(q, dict) and "x_thread" in q:
            by_query.setdefault(r["query_digest"], []).append(r)
    open_: dict[str, tuple[str, str]] = {}
    for qd, rows in by_query.items():
        if any(r["status"] == "ok" for r in rows):
            continue
        failed = sum(r["status"].startswith("failed") for r in rows)
        if failed >= miss_cap:
            continue
        tried = [r for r in rows if r["last_attempt_at"] is not None]
        last = max(tried, key=lambda r: r["last_attempt_at"]) if tried else None
        if (c is not None and miss_wait is not None and last is not None and last["status"].startswith("failed")
                and c.now - last["last_attempt_at"] < miss_wait):
            c.counts["not_due"] = c.counts.get("not_due", 0) + 1
            continue
        q = json.loads(rows[0]["query"])
        open_[qd] = (str(q["x_thread"]), str(q["from"]))
    out: dict[tuple[str, str], list[Row]] = {}
    if not open_:
        return out
    wanted = {v: qd for qd, v in open_.items()}
    for r in _own(ctx, "sources"):
        if r["state"] != "observed" or r["identity"]["channel"] != CHANNEL or record_entry(r) is None:
            continue
        cids = [k["value"] for k in r["identity"]["keys"] if k["kind"] == "conversation_id"]
        if not cids:
            continue
        record = json_record(ctx.blobs, r)
        if not isinstance(record, dict):
            raise CollectError(f"{r['source_id']}: its provider_record does not read as a JSON object")
        key = (cids[0], str(record["post"].get("author_id")))
        if key in wanted and record.get("thread_status") == "pending":
            out.setdefault(key, []).append({"row": r, "record": record})
    return out


class Group:
    """One self-thread candidate ``(conversation_id, author)`` of this run: the posts looked up
    this run that belong to it (each with the document members carrying its key and the batch
    query that asked for it), the ``pending`` rows of earlier runs waiting for its search
    (:func:`pending_threads`), and the posts the lookup answers returned for them in
    ``includes`` (quoted and replied-to posts)."""

    def __init__(self, cid: str, author_id: str) -> None:
        self.cid, self.author_id = cid, author_id
        self.looked_up: dict[str, tuple[dict[str, Any], set[str], dict[str, Any]]] = {}
        self.pending: list[Row] = []
        self.includes: dict[str, dict[str, Any]] = {}

    def seed_posts(self) -> list[dict[str, Any]]:
        return [p for p, _, _ in self.looked_up.values()] + [dict(it["record"]["post"]) for it in self.pending]


class Run:
    """One ``collect.x-lookup`` run (module docstring)."""

    def __init__(self, ctx: StageContext) -> None:
        self.ctx = ctx
        self.c = Collector(ctx)
        line = ctx.plan_stage
        if line.miss_cap is None:
            raise CollectError(f"{ctx.stage}: the stage line sets no miss_cap (D22)")
        if line.keys is not None and set(line.keys) != {"x_post"}:
            raise CollectError(f"{ctx.stage}: keys {line.keys}: only x_post is looked up")
        if line.budget is None or line.budget.max_usd is None:
            raise CollectError(f"{ctx.stage}: the stage line sets no budget.max_usd (a paid API "
                               f"runs only under a budget; per-run cap ${CAP_USD:.2f})")
        self.miss_cap = line.miss_cap
        self.budget = float(line.budget.max_usd)
        c = self.c
        self.limit = c.option("max_records", 0, env=LIMIT_ENV, cast=int)
        self.extra = [str(x) for x in c.option("ids", [], cast=list)]
        self.threads = bool(c.option("threads", True))
        self.max_thread = int(c.option("max_thread_posts", 25, cast=int))
        if not 1 <= self.max_thread <= 500:
            raise CollectError(f"{ctx.stage}: options.max_thread_posts {self.max_thread} not in [1, 500]")
        self.users = bool(c.option("users", False))
        self.usage_check = bool(c.option("usage_check", True))
        self.handles_by_post: dict[str, str] = {}
        self.handles_by_user: dict[str, str] = {}
        self.thread_done: dict[tuple[str, str], tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]] = {}
        self.groups: dict[tuple[str, str], Group] = {}
        self.quoted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.members: dict[str, str] = {}          # post id → thread external_id, own chain
        self.written_members: dict[str, str] = {}  # the same for thread rows written this run
        self.token = ""
        self.spend: Spend | None = None

    # ---------------------------------------------------------- threads
    def search_thread(self, g: Group) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
        """Search ``g``'s conversation for the author's own posts (one search per key per run):
        ``(posts, includes, 'ok')``, or ``([], {}, 'pending')`` when the search failed or was cut.
        Raises :class:`Stop`/:class:`RunHalted` after writing the window."""
        key = (g.cid, g.author_id)
        if key in self.thread_done:
            return self.thread_done[key]
        cid, author_id = key
        c, spend = self.c, self.spend
        assert spend is not None
        recent = (c.now - post_time(cid)).total_seconds() <= RECENT_DAYS * 86400
        url, endpoint = (SEARCH_RECENT_URL, "search_recent") if recent else (SEARCH_ALL_URL, "search_all")
        page = max(SEARCH_MIN, min(SEARCH_MAX, self.max_thread))
        handle = self.handle_of(g)
        if handle is None:
            c.counts["x_thread_no_handle"] = c.counts.get("x_thread_no_handle", 0) + 1
        found: list[dict[str, Any]] = []
        inc: dict[str, dict[str, Any]] = {}
        result: tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str] = ([], {}, "pending")
        stop: Stop | None = None
        with c.window(CHANNEL, thread_query(cid, author_id), c.now, c.now) as w:
            nxt: str | None = None
            try:
                while True:
                    # a page returns up to `page` posts in `data` and as many again in `includes`
                    resp, body = request(c, spend, endpoint, url,
                                         search_params(cid, author_id, page, nxt, handle=handle),
                                         self.token, 2 * page * PRICE_POST_USD)
                    c.counts["x_thread_requests"] = c.counts.get("x_thread_requests", 0) + 1
                    if resp.status != 200 or not isinstance(body, Mapping):
                        err = _failure(resp) if resp.status != 200 else ("parse_error", True)
                        w.fail(err[0], permanent=err[1])
                        if err[0] == "provider_auth":
                            stop = Stop("auth")
                        break
                    found += [dict(p) for p in body.get("data") or [] if isinstance(p, Mapping) and p.get("id")]
                    for p in (body.get("includes") or {}).get("tweets") or []:
                        if isinstance(p, Mapping) and p.get("id"):
                            inc[str(p["id"])] = dict(p)
                    nxt = (body.get("meta") or {}).get("next_token")
                    if not nxt or len(found) >= self.max_thread or not body.get("data"):
                        harvest_handles([*found, *inc.values()], self.handles_by_post, self.handles_by_user)
                        posts = [*found, *g.seed_posts()]
                        own = is_own_conversation(posts, cid, author_id)
                        w.seen = len(found)
                        w.kept = len(self_thread(posts, author_id, cid, g.looked_up)) if own else 0
                        w.complete()
                        result = (found, inc, "ok")
                        c.counts["x_threads_ok"] = c.counts.get("x_threads_ok", 0) + 1
                        break
            except Stop as s:
                w.seen = len(found)
                w.truncated(cap=False)  # a budget stop, counted x_budget_stop (D492)
                stop = s
            finally:
                self.thread_done[key] = result
                if result[2] != "ok":
                    c.counts["x_threads_pending"] = c.counts.get("x_threads_pending", 0) + 1
        if stop is not None:
            raise stop
        return result

    def handle_of(self, g: Group) -> str | None:
        """The author's handle: this run's (user expansion, status URLs, mentions), else the one a
        pending row's or the earlier thread row's record kept."""
        a = g.author_id
        known = self.handles_by_user.get(a) or next(
            (self.handles_by_post[m] for m in g.looked_up if m in self.handles_by_post), None)
        if known:
            return known
        for it in g.pending:
            if (it["record"].get("handles") or {}).get(a):
                return str(it["record"]["handles"][a])
        _, prior = self.prior_thread(g.cid, a)
        return str((prior or {}).get("handles", {}).get(a) or "") or None

    def defer_thread(self, cid: str, author_id: str) -> None:
        """Record a thread search this run does not make (the run is stopping): a ``partial``
        window, so a later run retries it (:func:`pending_threads`)."""
        key = (cid, author_id)
        if key not in self.thread_done:
            with self.c.window(CHANNEL, thread_query(cid, author_id), self.c.now, self.c.now) as w:
                w.truncated(cap=False)  # deferred by a stopping run, not a cap (D492)
            self.thread_done[key] = ([], {}, "pending")
            self.c.counts["x_threads_pending"] = self.c.counts.get("x_threads_pending", 0) + 1

    def prior_thread(self, cid: str, author_id: str) -> tuple[Row | None, dict[str, Any] | None]:
        """The own chain's observed row of the self-thread ``(cid, author)`` and its record."""
        row = self.c.prior(CHANNEL, thread_external_id(cid, author_id))
        if row is None or row["state"] != "observed" or record_entry(row) is None:
            return None, None
        record = json_record(self.ctx.blobs, row)
        if not isinstance(record, dict):
            raise CollectError(f"{row['source_id']}: its provider_record does not read as a JSON object")
        return row, record

    def write_group(self, g: Group, found: list[dict[str, Any]], inc: Mapping[str, dict[str, Any]],
                    status: str) -> None:
        """The rows of one candidate group after its search (module docstring, "Threads"): one
        self-thread row when the author started the conversation; else (``foreign``) or when the
        search failed or was cut (``pending``) a post row per post looked up this run (earlier
        pending rows stay as they are)."""
        c = self.c
        if status == "ok":
            prior_row, prior = self.prior_thread(g.cid, g.author_id)
            prior_posts = [dict(p) for p in (prior or {}).get("thread") or []]
            posts = [*found, *g.seed_posts(), *prior_posts]
            if is_own_conversation(posts, g.cid, g.author_id):
                self.write_thread(g, posts, inc, prior_row, prior)
                return
            status = "foreign"
            c.counts["x_threads_foreign"] = c.counts.get("x_threads_foreign", 0) + 1
        for post, lookup_of, match in g.looked_up.values():
            self.write(build_record(post, includes=g.includes, thread_status=status), lookup_of, **match)

    def write_thread(self, g: Group, posts: list[dict[str, Any]], inc: Mapping[str, dict[str, Any]],
                     prior_row: Row | None, prior: Mapping[str, Any] | None) -> None:
        seeds = {*g.looked_up, *(str(it["record"]["post"]["id"]) for it in g.pending),
                 *(str(p["id"]) for p in (prior or {}).get("thread") or [])}
        chain = self_thread(posts, g.author_id, g.cid, seeds)
        if len(chain) > self.max_thread:  # the cap never drops a looked-up or earlier member
            chain = [p for i, p in enumerate(chain) if i < self.max_thread or str(p["id"]) in seeds]
        quotes = {**((prior or {}).get("quotes") or {}), **g.includes, **inc}
        record = build_thread_record(chain, cid=g.cid, author_id=g.author_id, quotes=quotes,
                                     handles={**((prior or {}).get("handles") or {}), **self.handles_by_user})
        lookup_of = set((prior_row or {}).get("metadata", {}).get("lookup_of_source_ids") or ())
        lookup_of |= {s for _, lo, _ in g.looked_up.values() for s in lo}
        lookup_of |= {s for it in g.pending for s in it["row"]["metadata"]["lookup_of_source_ids"] or ()}
        ext = thread_external_id(g.cid, g.author_id)
        self.write(record, lookup_of, external_id=ext, query=thread_query(g.cid, g.author_id),
                   query_id=f"thread:{g.cid}", label=f"{g.cid}:{g.author_id}")
        for _, _, match in g.looked_up.values():
            self.c.attribute(CHANNEL, ext, **match)
        self.written_members.update({str(p["id"]): ext for p in chain})
        n = self.c.counts
        n["x_thread_rows"] = n.get("x_thread_rows", 0) + 1
        n["x_thread_posts"] = n.get("x_thread_posts", 0) + len(chain)
        if prior_row is not None:
            n["x_threads_extended"] = n.get("x_threads_extended", 0) + int(len(chain) > len(prior["thread"]))

    def threads_phase(self, searching: bool) -> None:
        """Search every group (or, once the run is stopping, defer it) and write its rows."""
        stop: Stop | RunHalted | None = None
        for g in self.groups.values():
            found: list[dict[str, Any]] = []
            inc: dict[str, dict[str, Any]] = {}
            status = "pending"
            if searching and stop is None:
                try:
                    found, inc, status = self.search_thread(g)
                except (Stop, RunHalted) as s:
                    stop = s
            else:
                self.defer_thread(g.cid, g.author_id)
            self.write_group(g, found, inc, status)
        if stop is not None:
            raise stop

    # ---------------------------------------------------------- rows
    def write(self, record: dict[str, Any], lookup_of: Iterable[str], *, external_id: str | None = None,
              **match: Any) -> Row | None:
        """One observed row (a post, or with ``external_id`` a self-thread) with its ``provider_record``
        and in-band ``post`` entries; ``match`` = :meth:`Collector.observe`'s ``query``/``query_id``/
        ``label`` (``matched_by``, D228)."""
        c, post = self.c, record["post"]
        pid = str(post["id"])
        record["handles"] = record_handles(record, {**(record.get("handles") or {}), **self.handles_by_user})
        handle = record["handles"].get(str(post.get("author_id")))
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        row = c.observe(CHANNEL, external_id or pid, record, keys=row_keys(record),
                        canonical_url=status_url(pid, handle),
                        metadata={"title": title_of(record),
                                  "authors": [f"@{handle}"] if handle else [f"X user {post.get('author_id')}"],
                                  "published_at": post.get("created_at"), "doc_type": DOC_TYPE,
                                  "lookup_of_source_ids": sorted(lookup_of),
                                  "provider_version": PROVIDER_VERSION},
                        provider_record=record, **match)
        if row is not None:
            c.add_inband(row["source_id"], CONTENT_ROLE, text=render(record), raw=payload,
                         media_type="application/json", text_media_type="text/markdown")
        return row

    def flush_quoted(self) -> None:
        """The quoted posts of this run's lookups, each its own row unless the own chain already
        has it or it is a member of a self-thread row (earlier, or written this run)."""
        for quoted, match in self.quoted:
            qid = str(quoted["id"])
            if qid in self.members or qid in self.written_members:
                self.c.counts["x_quoted_in_thread"] = self.c.counts.get("x_quoted_in_thread", 0) + 1
                continue
            prior = self.c.prior(CHANNEL, qid)
            if prior is not None and prior["state"] == "observed":
                continue
            self.c.counts["x_quoted_rows"] = self.c.counts.get("x_quoted_rows", 0) + 1
            self.write(build_record(quoted), (), **match)

    # ---------------------------------------------------------- batches
    def lookup_batch(self, batch: list[Due]) -> None:
        """One ``GET /2/tweets``: misses and posts that are not thread candidates are written now;
        candidates join their :class:`Group` (written after the searches); quoted posts are
        queued (:meth:`flush_quoted`)."""
        c, spend = self.c, self.spend
        assert spend is not None
        ids = [d.post_id for d in batch]
        per_id = 2 * PRICE_POST_USD + (2 * PRICE_USER_USD if self.users else 0.0)
        query = {"lookup": "x", "by": "ids", "keys": ids}
        with c.window(CHANNEL, query, c.now, c.now) as w:
            w.seen = len(ids)
            try:
                resp, body = request(c, spend, "lookup", LOOKUP_URL, lookup_params(ids, self.users),
                                     self.token, per_id * len(ids))
            except Stop:
                w.truncated(cap=False)  # a budget stop, counted x_budget_stop (D492)
                raise
            if resp.status != 200 or not isinstance(body, Mapping):
                err = _failure(resp) if resp.status != 200 else ("parse_error", True)
                w.fail(err[0], permanent=err[1])
                c.counts["batches_failed"] = c.counts.get("batches_failed", 0) + 1
                raise Stop("auth" if err[0] == "provider_auth" else "failed")
            data = [p for p in body.get("data") or [] if isinstance(p, Mapping) and p.get("id")]
            inc_list = [p for p in (body.get("includes") or {}).get("tweets") or []
                        if isinstance(p, Mapping) and p.get("id")]
            for u in (body.get("includes") or {}).get("users") or []:
                if isinstance(u, Mapping) and u.get("id") and u.get("username"):
                    self.handles_by_user[str(u["id"])] = str(u["username"])
            harvest_handles([*data, *inc_list], self.handles_by_post, self.handles_by_user)
            includes = {str(p["id"]): dict(p) for p in inc_list}
            hits = {str(p["id"]): dict(p) for p in data}
            errors = {str(e.get("resource_id") or e.get("value")): e for e in body.get("errors") or []
                      if isinstance(e, Mapping)}
            for d in batch:
                post = hits.get(d.post_id)
                if post is None:
                    title = str((errors.get(d.post_id) or {}).get("title") or "absent")
                    name = "x_miss_" + re.sub(r"[^a-z]+", "_", title.lower()).strip("_")
                    c.counts[name] = c.counts.get(name, 0) + 1
                    c.miss(CHANNEL, d.post_id, query=query, keys=[("x_post", d.post_id)],
                           query_id="lookup:x:ids", label=d.post_id)
                    continue
                match = {"query": query, "query_id": "lookup:x:ids", "label": d.post_id}
                q = referenced(post, "quoted")
                if q and q in includes:
                    self.quoted.append((includes[q], match))
                if self.threads and is_thread_candidate(post) and not referenced(post, "retweeted"):
                    key = (str(post["conversation_id"]), str(post["author_id"]))
                    g = self.groups.setdefault(key, Group(*key))
                    g.looked_up[d.post_id] = (post, d.lookup_of, match)
                    g.includes.update({r: includes[r] for kind in ("quoted", "replied_to")
                                       if (r := referenced(post, kind)) and r in includes})
                    w.kept += 1
                    continue
                if self.write(build_record(post, includes=includes), d.lookup_of, **match) is not None:
                    w.kept += 1
            w.complete()

    def run(self) -> StageResult:
        global _T
        from stack.llm import provider_key

        c = self.c
        wait = c.relist_after()
        self.members = thread_members(self.ctx)
        due, counts = due_ids(c, _documents(self.ctx), self.extra, self.miss_cap, self.handles_by_post,
                              miss_wait=wait, members=self.members)
        c.counts.update(counts, keys_due=len(due))
        pending = pending_threads(self.ctx, self.miss_cap, c, wait) if self.threads else {}
        c.counts["x_threads_to_retry"] = len(pending)
        complete = True
        if self.limit and len(due) > self.limit:
            due, complete = due[: self.limit], False
        if not due and not pending:
            return c.finish(scope_complete=complete)
        token = provider_key(TOKEN_ENV)
        if not token:
            c.counts["x_no_credentials"] = 1
            return c.finish(scope_complete=False)
        self.token = token
        self.spend = Spend(c, x_ledger(self.ctx.run_id), self.budget)
        for key, items in pending.items():
            self.groups.setdefault(key, Group(*key)).pending.extend(items)
        _T = c.transport(PACING, send=SEND, sleep=lambda s: _sleep(s))
        try:
            with c.halts():
                try:
                    if self.usage_check:
                        before = usage(c, token)
                        if before is not None:
                            c.counts["x_project_usage_before"] = before
                    stop: Stop | RunHalted | None = None
                    try:
                        per_id = 2 * PRICE_POST_USD + (2 * PRICE_USER_USD if self.users else 0.0)
                        i = 0
                        while i < len(due):
                            afford = math.floor(self.spend.headroom() / per_id + 1e-9)
                            n = min(LOOKUP_WIDTH, afford, len(due) - i)
                            if n <= 0:
                                c.counts["x_budget_stop"] = 1
                                raise Stop("budget")
                            self.lookup_batch(due[i:i + n])
                            i += n
                            c.progress(i, len(due))
                    except (Stop, RunHalted) as s:
                        stop = s
                    # the looked-up candidates are written whatever happened (pending when unsearched)
                    try:
                        self.threads_phase(searching=stop is None)
                    except (Stop, RunHalted) as s:
                        stop = stop or s
                    self.flush_quoted()
                    if stop is not None:
                        raise stop
                except Stop as s:
                    c.counts[f"x_stopped_{s.reason}"] = 1
                    complete = False
                finally:
                    if self.usage_check and c.halted is None:
                        try:
                            after = usage(c, token)
                        except (Stop, RunHalted):
                            after = None
                        if after is not None:
                            c.counts["x_project_usage_after"] = after
        finally:
            _T = None
        if c.counts.get("x_threads_pending"):
            complete = False
        return c.finish(scope_complete=complete)


def run(ctx: StageContext) -> StageResult:
    """``collect.x-lookup`` (module docstring)."""
    return Run(ctx).run()


__all__ = ["CAP_USD", "CHANNEL", "OPTIONS", "PRICE_POST_USD", "build_record", "build_thread_record", "expanded_text",
           "post_time", "relayed_urls", "render", "row_keys", "run", "self_thread", "thread_external_id",
           "x_ledger"]
