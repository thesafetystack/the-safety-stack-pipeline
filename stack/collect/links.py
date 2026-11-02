"""``collect.links``: outbound links of linking documents as candidate sources, chosen by a
context-aware LLM selector (round 4, track L2; ``docs/design/link-crawling.md`` revision 2, as
amended by the L2 brief). The stage module ``workflows/collect/links/main.py`` is thin; its README
says where everything is stored.

**Linking documents.** The ``documents`` head (``--in data/documents/<series>/``; builds pass it,
``stack.build.DOCUMENT_COLLECTORS``) filtered by the ``links`` source rules of
``stack/rules.py`` (:data:`stack.rules.LINK_RULES_ON` unless ``options.rules`` names others:
the newsletter feeds ``zvi_substack``, ``import_ai``, ``paradigm3_news`` and any document with
the key ``flag:collect_links``; the labs' and safety orgs' blogs are listed, off), evaluated as
the compiled SQL the triage gate uses. Retired and masked documents and documents whose live
members are all link channels (depth 1, §4.12) are never linking documents. The **linker** is the
member whose body is read: the newest in-band ``post`` entry with a raw ``text/html`` body (feed
``content:encoded``, decoded as UTF-8 explicitly), else the pointer's current raw ``text/html``
snapshot (acquired pages; a ``wayback`` snapshot is skipped). A linking document without a body
yet (a paradigm3 newsletter before acquire) is not processed; its earlier decisions are carried.
``since`` (date) leaves older linking documents unprocessed (carried, never dropped); ``only``
(doc ids, source ids or URLs) and ``max_linking`` limit the run to a few linking documents.

**Extraction** (no network; :func:`extract`): the content region (a per-host XPath in
:data:`stack.rules.LINK_REGIONS`, else the largest ``article``/``main``/``[role=main]``, else
``body``; an in-band fragment is all content), anchors under ``nav``, ``header``, ``footer``,
``aside``, ``form``, ``button``, ``script``, ``style``, ``noscript`` skipped (``chrome``). Each
``href`` is typed by :func:`stack.urls.link_keys` against the body's URL. Dropped as noise, with
the reason kept in the decision record: ``share`` intents, ``asset`` (CDN and image links),
``app`` (app stores, ``/app``), ``button`` (an anchor styled as a button: subscribe widgets),
``subscribe``/``comment`` paths, and ``self``-links of the publication
(:data:`stack.rules.LINK_SELF_HOSTS`, the body's own host, the linking document's own URL), except
that a **flagged** linking document (key ``flag:collect_links``) keeps a link to its own hosts when
:func:`stack.urls.link_keys` types the target ``pdf`` (a paper hosted on the post's site: MIRI's
announcement posts link their reports as ``intelligence.org/files/<x>.pdf``; D665); in-page ``#`` anchors and non-http schemes are only counted. Per kept anchor an **occurrence**:
the raw ``href``, the fetch URL, the anchor text (≤ 200 characters; an image's ``alt``), the
sentence holding it with its neighbours (≤ ~400 characters, else the sentence around the
anchor), the nearest preceding ``h1``-``h4`` and the anchor's position. Occurrences aggregate
per target (its first typed key) within the linking document.

**Decisions**, per (linking document, target), every one recorded with its reason:

1. **Rule** (no LLM): a target whose rule kind is in :data:`stack.rules.LINK_EXCLUDED_KINDS`
   (``options.exclude_kinds`` replaces the list) is ``rejected``, reason ``rule:<kind>``. The rule
   kind is the URL's kind hint, except that a url-group target at a site root is ``homepage`` and
   one on a listing or site path (:data:`stack.rules.LINK_NAVIGATION_PATHS`) is ``navigation``.
2. **Cache**: an earlier LLM decision with the same prompt digest, model, target key and
   occurrence digest (the item as the selector saw it plus the linking document's title, source and
   date) is reused: no call.
3. **Selector** (:data:`SCHEMA`): the rest, per linking document in batches of ``batch_size``
   (default 60), one call each through :func:`stack.llm.open_client` (ledger, response cache,
   egress gate by the linking channel's licence class): the cached system prompt is the plan pin
   ``prompts.collect.links`` (``name`` → ``workflows/collect/links/<name>.md``, ``model``,
   ``role`` default :data:`ROLE`, ``max_tokens`` default :data:`MAX_TOKENS`, ``template_digest``);
   the user message is the linking document's title, source, date and URL and one JSON line per
   link (host + path, kind hint, anchor, context, section, further anchors). The answer per item:
   ``select``/``reject``, a kind, a reason (cut at 120 characters). The stage line's
   ``budget.max_usd`` is required; a budget stop, an item the answer omits or a failed call
   leaves items **undecided**: that linking document's record is written ``partial``, its earlier
   complete decisions stay in force, the run ends ``scope_complete = false``, and the next run
   asks only the undecided items.

**Where it is stored.** (a) Every decision, selected or rejected, and every dropped link: one
**decision record** per linking document (JSON in ``blobs/``) whenever its decisions change,
referenced by a ``coverage.links`` row on the linking member's channel with ``query =
{"links_of": <linker source_id>, "decisions": <blob digest>}``, ``seen`` = candidates, ``kept`` =
selected, ``ok`` (complete) or ``partial`` (undecided items left); :func:`decision_rows` flattens
the newest records. (b) Each **selected** target: one ``sources`` row on ``sources.links``, channel
``links_<group>`` (:data:`CHANNEL_OF_GROUP`), ``external_id`` = ``<kind>:<value>`` of its first
typed key, keys = the typed keys plus one ``relay`` key per linking document (its URL; the
:data:`MAX_RELAYS` newest), ``canonical_url`` = the earliest occurrence's fetch URL,
``metadata.title`` = the first anchor that reads as a title (≥ 4 words, ≤ 200 characters, not a
URL); both are **first-minted**: a target whose previous row is not retracted keeps that row's
title (when it had one) and ``canonical_url`` (passed through :func:`stack.urls.fetch_url` again,
so a fetch-form rule change still applies), so a new linking document adds a relay key and
occurrences without changing them, and a target whose first occurrence disappears keeps both
until it is retracted (round 4 L4), ``doc_type`` ``forum_post``/``tweet`` for the forum/social groups, ``origin = provider``,
the ``provider_record`` (D522) = ``{target, linked_from}``: every linking document with its occurrences and its
decision, reason, model and prompt. A tweet target is what ``collect.x-lookup`` hydrates (its
``x_post`` key reaches the ``documents`` head through gather).

**Recomputation** (§4.3). Every run recomputes the active targets from the complete decision
records of the linking documents (this run's, else the newest complete one carried) and writes
the difference: a new or changed target row is observed (``skip_unchanged``), a target no linking
document selects any more is retracted (a miss row; the stage line must set ``miss_cap: 1``;
``miss_means: withdrawn``). Linking documents no rule selects any more (or retired, masked) drop
out. More than ``max_changed_targets`` (default 5,000) changed rows fails the run before anything
is written. A re-run over unchanged documents makes no LLM call and writes no row.

**Not done here** (deferred, link-crawling.md §4.7/§4.10/§4.11): the in-band ``link_context``
entry (needs the excerpt-rank change in shared core; the context is in the record), the metadata
probe, ``collect.lw-lookup``, the per-channel recheck and the acquire carry rule.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import lxml.html
from lxml import etree

from stack import rules
from stack.query import one_statement, sql_connection
from stack.checks.digests import H
from stack.collect.base import CollectError, Collector, utc
from stack.conversion.ladder import decode_text
from stack.core.hashes import template_hash
from stack.llm import BudgetScope, FatalLlmError, LicenseClass, LlmClient, LlmError, open_client
from stack.scholarly_records import keys_for
from stack.stage_runner import StageContext, StageResult
from stack.store.blobs import BlobNotFound
from stack.urls import KINDS, LinkTarget, fetch_url, link_keys, url_key

Row = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "workflows" / "collect" / "links"
PROMPT_KEY = "collect.links"
#: The ``options`` keys :func:`run` reads (D434).
OPTIONS: frozenset[str] = frozenset({"batch_size", "exclude_kinds", "max_changed_targets", "max_linking", "only",
                                     "rules", "since"})
#: The ``llm.yaml`` role a pin uses unless it names another (gpt-6-luna; its 512 output tokens are
#: raised per call to :data:`MAX_TOKENS`, a batch answers up to 60 items).
ROLE = "classify"
MAX_TOKENS = 12_000
PIN_KEYS = frozenset({"name", "model", "role", "max_tokens", "template_digest"})
BATCH_SIZE = 60
MAX_BATCH_SIZE = 100
MAX_CHANGED_TARGETS = 5_000
MAX_RELAYS = 50
#: Bumped when extraction or the decision record changes shape (part of the rules digest).
#: 2: a flagged linking document keeps same-host ``pdf`` targets (D665).
EXTRACTOR_VERSION = 2
#: The key that marks a manual source whose links are harvested (:data:`stack.scholarly_records.FLAGS`;
#: the ``flagged_links`` rule of :mod:`stack.rules`).
FLAG_KEY = ("flag", "collect_links")
RECORD_VERSION = 1
CHANNEL_OF_GROUP: dict[str, str] = {"paper": "links_paper", "forum": "links_forum", "url": "links_url",
                                    "social": "links_social"}
LINK_CHANNELS = frozenset(CHANNEL_OF_GROUP.values())
DOC_TYPE_OF_GROUP: dict[str, str] = {"forum": "forum_post", "social": "tweet"}
ANCHOR_CHARS = 200
CONTEXT_CHARS = 400
#: A context block this short that ends in ``:`` or precedes a blockquote is a lead-in: the next
#: block joins it (:func:`context_of`).
SHORT_BLOCK_CHARS = 160
HEADING_CHARS = 200
REASON_CHARS = 120
MAX_MORE_ANCHORS = 2
#: Rule kinds beyond :data:`stack.urls.KINDS` (computed here for url-group targets).
EXTRA_RULE_KINDS = ("homepage", "navigation")
RULE_KINDS = frozenset(KINDS) | frozenset(EXTRA_RULE_KINDS)
#: The selector's kinds (what it says the target is).
SELECT_KINDS = ("paper", "research_post", "forum_post", "tweet", "repo", "tool_or_dataset", "docs", "policy",
                "news", "product", "hub", "media", "other")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "decision": {"type": "string", "enum": ["select", "reject"]},
                    "kind": {"type": "string", "enum": list(SELECT_KINDS)},
                    "reason": {"type": "string"},
                },
                "required": ["i", "decision", "kind", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}
SELECTED, REJECTED = "selected", "rejected"
CHROME_TAGS = frozenset({"nav", "header", "footer", "aside", "form", "button", "script", "style", "noscript",
                         "select", "template"})
BLOCK_TAGS = frozenset({"p", "li", "blockquote", "td", "th", "dd", "dt", "figcaption", "caption", "pre",
                        "h1", "h2", "h3", "h4", "h5", "h6"})
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})
_WS = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"”’)\]]?\s+")
_XML_DECL = re.compile(rb"^\s*<\?xml[^>]*\?>")
_URLISH = re.compile(r"^(?:https?://|www\.)|^[\w.-]+\.(?:com|org|net|ai|io|pub|edu|gov)(?:/|$)", re.IGNORECASE)


def _ws(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _host(url: str) -> str:
    h = (urllib.parse.urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _covered(host: str, hosts: Iterable[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _iso(value: Any) -> str | None:
    d = utc(value) if value is not None else None
    return d.isoformat() if d is not None else None


# ─────────────────────────────────────────────────────────────── extraction
@dataclass(frozen=True)
class Occurrence:
    """One anchor of a linking body."""

    href: str
    url: str
    anchor: str
    context: str
    heading: str
    position: int

    def as_json(self) -> Row:
        return {"href": self.href, "url": self.url, "anchor": self.anchor, "context": self.context,
                "heading": self.heading, "position": self.position}


@dataclass
class Candidate:
    """A target of one linking body and its occurrences (document order)."""

    key: str
    target: LinkTarget
    occurrences: list[Occurrence] = field(default_factory=list)


@dataclass
class Extracted:
    candidates: dict[str, Candidate]
    dropped: list[Row]
    counts: Counter[str]


def parse_body(raw: bytes, *, fragment: bool) -> etree._Element:
    """The parsed body: an in-band fragment decoded as UTF-8 explicitly (feed and GraphQL bodies
    carry no charset, §3.3) under a ``div``; a page decoded as the converter decodes it
    (:func:`stack.conversion.ladder.decode_text`: its ``<meta charset>`` with GB2312/GBK read as GB18030,
    detection, else UTF-8)."""
    if fragment:
        return lxml.html.fragment_fromstring(raw.decode("utf-8", errors="replace"), create_parent="div")
    text, _codec = decode_text(raw)
    text = _XML_DECL.sub(b"", text.encode("utf-8")).decode("utf-8")
    return lxml.html.document_fromstring(text)


def region_of(root: etree._Element, host: str, *, fragment: bool) -> tuple[etree._Element, str]:
    """The content region and how it was found (``fragment``, ``site``, ``largest``, ``body``)."""
    if fragment:
        return root, "fragment"
    for h, xp in rules.LINK_REGIONS.items():
        if host == h or host.endswith("." + h):
            hits = root.xpath(xp)
            if hits:
                return hits[0], "site"
    cands = root.xpath("//article | //main | //*[@role='main']")
    if cands:
        return max(cands, key=lambda e: len(e.text_content())), "largest"
    body = root.find(".//body")
    return (body if body is not None else root), "body"


def _in_chrome(el: etree._Element, region: etree._Element) -> bool:
    p = el
    while p is not None and p is not region:
        if isinstance(p.tag, str) and p.tag.lower() in CHROME_TAGS:
            return True
        p = p.getparent()
    return False


def _block_of(el: etree._Element, region: etree._Element) -> etree._Element:
    p = el.getparent()
    while p is not None and p is not region:
        if isinstance(p.tag, str) and p.tag.lower() in BLOCK_TAGS:
            return p
        p = p.getparent()
    return el.getparent() if el.getparent() is not None else el


class _Found(Exception):
    pass


def _text_before(block: etree._Element, anchor: etree._Element) -> str:
    """The text of ``block`` that precedes ``anchor`` (document order)."""
    parts: list[str] = []

    def walk(e: etree._Element) -> None:
        if e is anchor:
            raise _Found
        if isinstance(e.tag, str) and e.text:
            parts.append(e.text)
        for ch in e:
            walk(ch)
            if ch.tail:
                parts.append(ch.tail)

    try:
        walk(block)
    except _Found:
        pass
    return "".join(parts)


def context_of(anchor: etree._Element, region: etree._Element) -> str:
    """The sentence holding ``anchor`` with its neighbours (≤ :data:`CONTEXT_CHARS`), else the
    sentence alone, else a window of that size around the anchor."""
    block = _block_of(anchor, region)
    text = _ws(block.text_content())
    if len(text) <= SHORT_BLOCK_CHARS:
        # A lead-in ("Nathan Calvin:", "All three had expressed alarm on Twitter:") whose substance is
        # the next block (Zvi quotes the linked tweet in a blockquote after it): the neighbour joins.
        nxt = block.getnext()
        while nxt is not None and not isinstance(nxt.tag, str):
            nxt = nxt.getnext()
        if nxt is not None and block is not region and (text.endswith(":") or nxt.tag.lower() == "blockquote"):
            more = _ws(nxt.text_content())
            if more:
                return _cut(f"{text} {more}", CONTEXT_CHARS)
    if len(text) <= CONTEXT_CHARS:
        return text
    offset = len(_ws(_text_before(block, anchor)))
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        spans.append((start, m.end()))
        start = m.end()
    spans.append((start, len(text)))
    idx = next((i for i, (s, e) in enumerate(spans) if s <= offset < e), len(spans) - 1)
    lo, hi = spans[max(idx - 1, 0)][0], spans[min(idx + 1, len(spans) - 1)][1]
    if hi - lo <= CONTEXT_CHARS:
        return text[lo:hi].strip()
    s, e = spans[idx]
    if e - s <= CONTEXT_CHARS:
        return text[s:e].strip()
    half = CONTEXT_CHARS // 2
    lo = max(0, min(offset - half, len(text) - CONTEXT_CHARS))
    return ("…" if lo > 0 else "") + text[lo:lo + CONTEXT_CHARS].strip() + ("…" if lo + CONTEXT_CHARS < len(text) else "")


def anchor_text(a: etree._Element) -> str:
    t = _ws(a.text_content())
    if not t:
        alts = [_ws(img.get("alt")) for img in a.iter("img")]
        t = next((x for x in alts if x), "")
    return _cut(t, ANCHOR_CHARS)


def target_key(target: LinkTarget) -> str:
    """The target's identity in this stage: ``<kind>:<value>`` of its first typed key (the
    link row's ``external_id``)."""
    kind, value = target.keys[0]
    return f"{kind}:{value}"


def noise_reason(target: LinkTarget, a: etree._Element, self_hosts: Sequence[str],
                 self_keys: frozenset[str], *, flagged: bool = False) -> str | None:
    """Why an anchor is extraction noise (module docstring), or ``None``. ``flagged``: the linking
    document carries :data:`FLAG_KEY`, so a same-host ``pdf`` target is kept (D665)."""
    if target.kind == "share":
        return "share"
    host = _host(target.fetch_url)
    path = urllib.parse.urlsplit(target.fetch_url).path or "/"
    if _covered(host, rules.LINK_ASSET_HOSTS) or path.lower().endswith(rules.LINK_ASSET_EXTENSIONS):
        return "asset"
    if _covered(host, rules.LINK_APP_HOSTS):
        return "app"
    if "button" in (a.get("class") or "").lower().split():
        return "button"
    if target.group == "url":
        for reason, pat in rules.LINK_NOISE_PATHS:
            if re.search(pat, path, re.IGNORECASE):
                return reason
    if any(f"{k}:{v}" in self_keys for k, v in target.keys):
        return "self"
    if _covered(host, self_hosts) and not (flagged and target.kind == "pdf"):
        return "self"
    return None


def extract(raw: bytes, *, base_url: str, fragment: bool, self_hosts: Sequence[str] = (),
            self_keys: Iterable[str] = (), flagged: bool = False) -> Extracted:
    """The candidates of one linking body (module docstring). ``self_hosts``: the publication's
    hosts (the body's own host is added); ``self_keys``: target keys of the linking document;
    ``flagged``: the linking document carries :data:`FLAG_KEY` (same-host PDFs kept, D665)."""
    root = parse_body(raw, fragment=fragment)
    for el in root.iter():  # text_content() joins blocks and <br> lines without a space: add one
        if isinstance(el.tag, str) and (el.tag.lower() == "br" or el.tag.lower() in BLOCK_TAGS
                                        or el.tag.lower() == "div"):
            el.tail = " " + (el.tail or "")
    host = _host(base_url)
    region, how = region_of(root, host, fragment=fragment)
    hosts = tuple(dict.fromkeys([*self_hosts, *([host] if host else [])]))
    skeys = frozenset(self_keys)
    counts: Counter[str] = Counter({f"region_{how}": 1})
    cands: dict[str, Candidate] = {}
    dropped: list[Row] = []
    heading = ""
    position = 0
    for el in region.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag in HEADING_TAGS:
            heading = _cut(_ws(el.text_content()), HEADING_CHARS)
        if tag != "a" or el.get("href") is None:
            continue
        counts["anchors"] += 1
        pos, position = position, position + 1
        if _in_chrome(el, region):
            counts["drop_chrome"] += 1
            continue
        href = (el.get("href") or "").strip()
        if not href or href.startswith("#"):
            counts["drop_fragment"] += 1
            continue
        target = link_keys(href, base_url)
        if target is None or not target.keys or not target.keys[0][1]:
            counts["drop_nonhttp"] += 1
            continue
        anchor = anchor_text(el)
        reason = noise_reason(target, el, hosts, skeys, flagged=flagged)
        if reason is not None:
            counts[f"drop_{reason}"] += 1
            dropped.append({"url": target.fetch_url, "anchor": anchor, "reason": reason})
            continue
        key = target_key(target)
        cand = cands.get(key)
        if cand is None:
            cand = cands[key] = Candidate(key, target)
        cand.occurrences.append(Occurrence(href[:1000], target.fetch_url, anchor, context_of(el, region),
                                           heading, pos))
    counts["candidates"] = len(cands)
    return Extracted(cands, dropped, counts)


def rule_kind(target: LinkTarget) -> str:
    """The kind the rule filter reads (module docstring, Decisions 1)."""
    if target.group != "url":
        return target.kind
    path = urllib.parse.urlsplit(target.fetch_url).path or "/"
    if path in ("", "/") and "?" not in url_key(target.fetch_url):
        return "homepage"
    if any(re.match(p, path, re.IGNORECASE) for p in rules.LINK_NAVIGATION_PATHS):
        return "navigation"
    return target.kind


def rules_digest(exclude_kinds: Sequence[str]) -> str:
    """Digest of everything extraction and the rule filter read (changes re-decide linkers)."""
    return H({"extractor": EXTRACTOR_VERSION, "self": {k: list(v) for k, v in rules.LINK_SELF_HOSTS.items()}, "regions": rules.LINK_REGIONS,
              "asset_hosts": list(rules.LINK_ASSET_HOSTS), "asset_ext": list(rules.LINK_ASSET_EXTENSIONS),
              "app_hosts": list(rules.LINK_APP_HOSTS), "noise": [list(p) for p in rules.LINK_NOISE_PATHS],
              "navigation": list(rules.LINK_NAVIGATION_PATHS), "exclude_kinds": sorted(exclude_kinds)})


# ─────────────────────────────────────────────────────────────── linking documents
@dataclass
class Linker:
    """One linking document as this run reads it."""

    doc_id: str
    source_id: str
    channel: str
    title: str
    url: str
    published_at: str | None
    body_digest: str
    body_from: str  # inband | snapshot
    media_type: str
    member_ids: frozenset[str]
    self_keys: frozenset[str]
    flagged: bool = False  # carries FLAG_KEY (D665)

    def header(self) -> Row:
        return {"doc_id": self.doc_id, "source_id": self.source_id, "channel": self.channel, "title": self.title,
                "url": self.url, "published_at": self.published_at,
                "body": {"digest": self.body_digest, "from": self.body_from, "media_type": self.media_type}}


def _html(media: str | None) -> bool:
    m = (media or "").lower()
    return m.startswith("text/html") or m.startswith("application/xhtml")


def body_of(doc: Row) -> tuple[str, str, str, str, str] | str:
    """``(source_id, raw sha256, media_type, base_url, from)`` of the document's body (module
    docstring), or the reason there is none (``no_body``, ``wayback``)."""
    ident = doc["identity"]
    live = {m["source_id"]: m for m in ident["members"] if m["is_live"]}
    entries = [e for e in (doc.get("inband") or {}).get("entries") or []
               if e["content_role"] == "post" and e.get("raw") and _html(e["raw"]["media_type"])
               and e["source_id"] in live]
    if entries:
        e = max(entries, key=lambda e: (e["captured_at"], e["source_id"]))
        m = live[e["source_id"]]
        return e["source_id"], e["raw"]["sha256"], e["raw"]["media_type"], m.get("canonical_url") or "", "inband"
    content = doc.get("content") or {}
    sid = content.get("current_snapshot_id")
    snap = next((s for s in content.get("snapshots") or [] if s["snapshot_id"] == sid), None)
    if snap is None or not _html(snap["raw"]["media_type"]):
        return "no_body"
    if snap.get("via") == "wayback":
        return "wayback"
    return snap["source_id"], snap["raw"]["sha256"], snap["raw"]["media_type"], \
        snap.get("final_url") or snap.get("url") or "", "snapshot"


def linking_doc_ids(docs_table: Any, rule_names: Sequence[str]) -> set[str]:
    """``doc_id`` of the documents some named ``links`` rule matches (the rules' compiled SQL)."""
    if docs_table.num_rows == 0 or not rule_names:
        return set()
    sql = " OR ".join(f"({rules.rule(n, 'links').sql()})" for n in rule_names)
    con = sql_connection()  # config-supplied SQL: no file access, one statement
    try:
        con.register("docs", docs_table.select(["doc_id", "identity", "metadata"]))
        return {d for (d,) in con.execute(one_statement(f"SELECT doc_id FROM docs WHERE {sql}")).fetchall()}
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────── the selector
@dataclass(frozen=True)
class Prompt:
    name: str
    template: str
    role: str
    model: str
    max_tokens: int
    digest: str  # the cache's prompt digest: template + schema (the model is keyed separately)


def resolve_prompt(ctx: StageContext, llm: Any) -> Prompt:
    """The plan pin ``prompts.collect.links`` (module docstring, Decisions 3); refused when absent
    or malformed, before any call."""
    pin = dict(ctx.plan.prompts.get(PROMPT_KEY) or {})
    where = f"{ctx.stage}: plan.prompts[{PROMPT_KEY!r}]"
    if not pin:
        raise CollectError(f"{where} is required (the selector's prompt pin, e.g. {{name: link-select-v1}})")
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise CollectError(f"{where} keys {extra} not in {sorted(PIN_KEYS)}")
    name = pin.get("name")
    if not isinstance(name, str) or not name:
        raise CollectError(f"{where} needs a `name` (the template workflows/collect/links/<name>.md)")
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise CollectError(f"{where}: no template {path}")
    role = str(pin.get("role") or ROLE)
    if role not in llm.roles:
        raise CollectError(f"{where}: role {role!r} is not in llm.yaml roles")
    model = str(pin.get("model") or llm.roles[role].model)
    max_tokens = pin.get("max_tokens", MAX_TOKENS)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 256:
        raise CollectError(f"{where}: max_tokens must be an integer >= 256, got {max_tokens!r}")
    template = path.read_text("utf-8")
    want = pin.get("template_digest")
    have = template_hash(template, SCHEMA, {"model": model, "max_tokens": max_tokens})
    if want is not None and want != have:
        raise CollectError(f"{where} pins template_digest {want} but {path.name} with {model} has {have}")
    return Prompt(name, template, role, model, max_tokens, H({"template": template, "schema": SCHEMA}))


def display(t: LinkTarget) -> str:
    """Host and path the selector sees: the key form without the scheme (identity parameters
    kept, tracking dropped), except a post's status URL, whose key form drops the handle."""
    if t.group == "social":
        p = urllib.parse.urlsplit(t.fetch_url)
        return _host(t.fetch_url) + p.path
    return url_key(t.fetch_url).removeprefix("https://") or t.fetch_url


def item_of(cand: Candidate) -> Row:
    """One link as the selector sees it (without its batch index)."""
    t = cand.target
    first = cand.occurrences[0]
    more = list(dict.fromkeys(o.anchor for o in cand.occurrences[1:] if o.anchor and o.anchor != first.anchor))
    item: Row = {"target": display(t), "hint": rule_kind(t),
                 "anchor": first.anchor, "context": first.context, "section": first.heading}
    if more:
        item["more_anchors"] = more[:MAX_MORE_ANCHORS]
    return item


def occurrence_digest(linker: Linker, item: Mapping[str, Any]) -> str:
    return H({"item": dict(item), "linking": [linker.title, linker.channel, linker.published_at]})


def render_user(linker: Linker, items: Sequence[Mapping[str, Any]]) -> str:
    lines = [f"Linking document: \"{linker.title}\"",
             f"Source: {linker.channel} · {(linker.published_at or 'undated')[:10]} · {linker.url}",
             "", f"Links ({len(items)}, one JSON object per line):"]
    lines += [json.dumps({"i": i, **it}, ensure_ascii=False) for i, it in enumerate(items)]
    return "\n".join(lines)


def cache_key(prompt: Prompt, key: str, occ_digest: str) -> tuple[str, str, str, str]:
    return (prompt.digest, prompt.model, key, occ_digest)


# ─────────────────────────────────────────────────────────────── stored state
def _own(ctx: StageContext, fam: str) -> list[Row]:
    pinned = ctx.prior.get(fam) or ctx.inputs.get(fam)
    return ctx.view(fam).to_pylist() if pinned is not None else []


def _documents(ctx: StageContext) -> Any | None:
    keys = [k for k in ctx.inputs if k == "documents" or k.startswith("documents/")]
    if len(keys) > 1:
        raise CollectError(f"{ctx.stage}: at most one documents input (got {sorted(keys)})")
    return ctx.view(keys[0]) if keys else None


def _blob_json(ctx: StageContext, digest: str, what: str) -> Row:
    try:
        return cast(Row, json.loads(ctx.blobs.get(digest)))
    except BlobNotFound as exc:
        raise CollectError(f"{ctx.stage}: {what} blob {digest} is missing from the store") from exc


@dataclass
class State:
    """A linker's stored decision records: the newest (any status) and the newest complete."""

    latest: Row | None = None
    latest_digest: str | None = None
    complete: Row | None = None


def prior_states(ctx: StageContext) -> dict[str, State]:
    """Per linker ``source_id`` its newest and newest complete decision record (``coverage.links``)."""
    rows: list[tuple[dt.datetime, str, str, str]] = []
    for r in _own(ctx, "coverage"):
        try:
            q = json.loads(r["query"])
        except (TypeError, ValueError):
            continue
        if not isinstance(q, dict) or "links_of" not in q or "decisions" not in q:
            continue
        rows.append((r["last_attempt_at"] or r["window_end_at"], q["links_of"], q["decisions"], r["status"]))
    states: dict[str, State] = {}
    for _at, sid, digest, status in sorted(rows):
        st = states.setdefault(sid, State())
        st.latest_digest = digest
        st.latest = None  # read lazily below
        if status == "ok":
            st.complete = {"_digest": digest}
    for sid, st in states.items():
        assert st.latest_digest is not None
        st.latest = _blob_json(ctx, st.latest_digest, f"decision record of {sid}")
        if st.complete is not None:
            d = st.complete["_digest"]
            st.complete = st.latest if d == st.latest_digest else _blob_json(ctx, d, f"decision record of {sid}")
    return states


def decision_cache(states: Mapping[str, State]) -> dict[tuple[str, str, str, str], Row]:
    """Every stored LLM decision by (prompt digest, model, target key, occurrence digest)."""
    out: dict[tuple[str, str, str, str], Row] = {}
    for st in states.values():
        for rec in (st.complete, st.latest):
            if rec is None:
                continue
            for c in rec.get("candidates") or []:
                if c.get("by") == "llm" and c.get("decision") in (SELECTED, REJECTED):
                    out[(c["prompt_digest"], c["model"], c["key"], c["occurrence_digest"])] = c
    return out


def decision_rows(ctx_or_root: StageContext | str | Path, coverage: str | None = None) -> list[Row]:
    """The newest decision record of every linker, flattened: one row per (linker, target) with
    ``decision``, ``by``, ``reason`` and the first occurrence; plus one row per dropped link
    (``decision = 'dropped'``). For audits and the README's recipe."""
    if isinstance(ctx_or_root, StageContext):
        states = prior_states(ctx_or_root)
    else:
        from stack.store.blobs import open_blobs
        from stack.store.chain import read_view, resolve

        if coverage is None:
            raise ValueError("decision_rows(<store>, coverage): name the coverage.links series directory "
                             "or file (data/coverage.links/<series>/)")
        root = Path(ctx_or_root)
        blobs = open_blobs(root)
        latest: dict[str, tuple[Any, str]] = {}
        for r in read_view(resolve(root, coverage)).to_pylist():
            q = json.loads(r["query"])
            if "links_of" in q:
                at = r["last_attempt_at"] or r["window_end_at"]
                if q["links_of"] not in latest or latest[q["links_of"]][0] <= at:
                    latest[q["links_of"]] = (at, q["decisions"])
        states = {sid: State(latest=json.loads(blobs.get(d)), latest_digest=d)
                  for sid, (_at, d) in latest.items()}
    out: list[Row] = []
    for sid, st in sorted(states.items()):
        rec = st.latest or {}
        lk = rec.get("linker") or {}
        base = {"linker_source_id": sid, "linker_doc_id": lk.get("doc_id"), "linker_title": lk.get("title"),
                "linker_channel": lk.get("channel"), "complete": rec.get("complete")}
        for c in rec.get("candidates") or []:
            o = (c.get("occurrences") or [{}])[0]
            out.append({**base, "key": c["key"], "group": c["group"], "rule_kind": c["rule_kind"],
                        "decision": c["decision"] or "undecided", "by": c.get("by"), "reason": c.get("reason"),
                        "selector_kind": c.get("selector_kind"), "url": o.get("url"), "anchor": o.get("anchor"),
                        "context": o.get("context"), "occurrences": len(c.get("occurrences") or [])})
        for d in rec.get("dropped") or []:
            out.append({**base, "key": None, "group": None, "rule_kind": None, "decision": "dropped", "by": "extract",
                        "reason": d["reason"], "selector_kind": None, "url": d["url"], "anchor": d["anchor"],
                        "context": None, "occurrences": 1})
    return out


# ─────────────────────────────────────────────────────────────── the stage
def _strings(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return list(value)
    raise CollectError(f"collect.links: options.{what} must be a list of strings, got {value!r}")


def _positive_int(value: Any, what: str, default: int | None, *, cap: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or (cap is not None and value > cap):
        raise CollectError(f"collect.links: options.{what} must be an integer in [1, {cap or '∞'}], got {value!r}")
    return value


class Run:
    """One ``collect.links`` run (module docstring)."""

    def __init__(self, ctx: StageContext) -> None:
        self.ctx = ctx
        self.c = Collector(ctx)
        c = self.c
        line = ctx.plan_stage
        if line.miss_cap != 1:
            raise CollectError(f"{ctx.stage}: the stage line must set miss_cap: 1 (a target no linking document "
                               f"selects is retracted at once), got {line.miss_cap!r}")
        if line.budget is None or line.budget.max_usd is None:
            raise CollectError(f"{ctx.stage}: the stage line needs budget.max_usd (the selector is a paid LLM)")
        self.budget = float(line.budget.max_usd)
        since = c.option("since", None)
        self.since = utc(str(since)) if since not in (None, "") else None
        self.rule_names = _strings(c.option("rules", None), "rules") or list(rules.LINK_RULES_ON)
        for n in self.rule_names:
            rules.rule(n, "links")  # an unknown name or a rule of another stage refuses here
        self.only = set(_strings(c.option("only", None), "only"))
        self.max_linking = _positive_int(c.option("max_linking", None), "max_linking", None)
        excl = c.option("exclude_kinds", None)
        self.exclude = tuple(_strings(excl, "exclude_kinds")) if excl is not None else rules.LINK_EXCLUDED_KINDS
        bad = sorted(set(self.exclude) - RULE_KINDS)
        if bad:
            raise CollectError(f"{ctx.stage}: options.exclude_kinds {bad} not in {sorted(RULE_KINDS)}")
        self.batch = cast(int, _positive_int(c.option("batch_size", None), "batch_size", BATCH_SIZE,
                                             cap=MAX_BATCH_SIZE))
        self.max_changed = cast(int, _positive_int(c.option("max_changed_targets", None), "max_changed_targets",
                                                   MAX_CHANGED_TARGETS))
        from stack.config import load

        self.config = load()
        self.prompt = resolve_prompt(ctx, self.config.llm)
        self.rules_digest = rules_digest(self.exclude)
        self.counts: Counter[str] = Counter()
        self.client: LlmClient | None = None
        self.scope = BudgetScope()
        self.halted = False

    # ---------------------------------------------------------------- linkers
    def linkers(self, docs: Any) -> tuple[list[tuple[Linker, Extracted]], set[str], set[str]]:
        """(processed linkers with their extraction, live member ids of every eligible document,
        source ids of the eligible documents' members) — module docstring."""
        cnt = self.counts
        hits = linking_doc_ids(docs, self.rule_names)
        rows = docs.filter(_mask(docs, hits)).to_pylist() if hits else []
        eligible_members: set[str] = set()
        todo: list[tuple[Row, tuple[str, str, str, str, str]]] = []
        for d in sorted(rows, key=lambda d: d["doc_id"]):
            ident = d["identity"]
            if d["state"] == "retired" or ident["is_masked"]:
                cnt["linking_retired_or_masked"] += 1
                continue
            live = [m for m in ident["members"] if m["is_live"]]
            if live and all(m["channel"] in LINK_CHANNELS for m in live):
                cnt["linking_link_only"] += 1  # depth 1 (§4.12)
                continue
            cnt["linking_eligible"] += 1
            eligible_members |= {m["source_id"] for m in live}
            body = body_of(d)
            if isinstance(body, str):
                cnt[f"linking_{body}"] += 1
                continue
            pub = utc(d["metadata"]["published_at"]) if d["metadata"]["published_at"] is not None else None
            if self.since is not None and pub is not None and pub < self.since:
                cnt["linking_before_since"] += 1
                continue
            if self.only and not self._named(d):
                cnt["linking_not_named"] += 1
                continue
            todo.append((d, body))
        todo.sort(key=lambda t: (-(utc(t[0]["metadata"]["published_at"]) or dt.datetime.min.replace(
            tzinfo=dt.UTC)).timestamp(), t[0]["doc_id"]))
        if self.max_linking is not None and len(todo) > self.max_linking:
            cnt["linking_over_max"] += len(todo) - self.max_linking
            todo = todo[: self.max_linking]
        out: list[tuple[Linker, Extracted]] = []
        for d, (sid, digest, media, base, frm) in todo:
            lk = self._linker(d, sid, digest, media, base, frm)
            try:
                raw = self.ctx.blobs.get(digest)
            except BlobNotFound as exc:
                raise CollectError(f"{self.ctx.stage}: body blob {digest} of {d['doc_id']} is missing") from exc
            ex = extract(raw, base_url=base or lk.url, fragment=frm == "inband",
                         self_hosts=rules.LINK_SELF_HOSTS.get(lk.channel, ()), self_keys=lk.self_keys,
                         flagged=lk.flagged)
            cnt.update({k: v for k, v in ex.counts.items()})
            out.append((lk, ex))
        cnt["linking_processed"] = len(out)
        return out, eligible_members, {lk.source_id for lk, _ in out}

    def _named(self, d: Row) -> bool:
        ident = d["identity"]
        names = {d["doc_id"], *(m["source_id"] for m in ident["members"])}
        names |= {k["value"] for k in ident["keys"] if k["kind"] == "url"}
        names |= {m["canonical_url"] for m in ident["members"] if m.get("canonical_url")}
        return bool(names & self.only) or any(url_key(o) in names for o in self.only if o.startswith("http"))

    def _linker(self, d: Row, sid: str, digest: str, media: str, base: str, frm: str) -> Linker:
        ident = d["identity"]
        member = next(m for m in ident["members"] if m["source_id"] == sid)
        url = member.get("canonical_url") or base or next(
            (k["value"] for k in ident["keys"] if k["kind"] == "url"), "")
        self_keys = frozenset(f"{k['kind']}:{k['value']}" for k in ident["keys"]
                              if k["kind"] not in ("relay", "related", "flag"))
        return Linker(d["doc_id"], sid, member["channel"], d["metadata"]["title"] or member.get("title") or "",
                      url, _iso(d["metadata"]["published_at"]), digest, frm, media,
                      frozenset(m["source_id"] for m in ident["members"]), self_keys,
                      any((k["kind"], k["value"]) == FLAG_KEY for k in ident["keys"]))

    # ---------------------------------------------------------------- decisions
    def decide(self, linker: Linker, ex: Extracted, cache: Mapping[tuple[str, str, str, str], Row]) -> Row:
        """The linker's decision record (module docstring); asks the selector for what neither the
        rules nor the cache decide."""
        cnt = self.counts
        cands: list[Row] = []
        ask: list[tuple[Row, Row]] = []
        for cand in ex.candidates.values():
            t = cand.target
            rk = rule_kind(t)
            item = item_of(cand)
            od = occurrence_digest(linker, item)
            rec: Row = {"key": cand.key, "group": t.group, "keys": [list(k) for k in t.keys], "kind": t.kind,
                        "rule_kind": rk, "occurrences": [o.as_json() for o in cand.occurrences],
                        "occurrence_digest": od, "decision": None, "by": None, "reason": None,
                        "selector_kind": None, "model": None, "prompt": None, "prompt_digest": None}
            cands.append(rec)
            if rk in self.exclude:
                rec.update(decision=REJECTED, by="rule", reason=f"rule:{rk}")
                cnt[f"rule_{rk}"] += 1
                continue
            hit = cache.get(cache_key(self.prompt, cand.key, od))
            if hit is not None:
                rec.update({k: hit[k] for k in ("decision", "by", "reason", "selector_kind", "model", "prompt",
                                                 "prompt_digest")})
                cnt["selector_cached"] += 1
                continue
            ask.append((rec, item))
        for i in range(0, len(ask), self.batch):
            if self.halted or self.scope.tripped:
                break
            self._ask(linker, ask[i:i + self.batch])
        undecided = sum(1 for r in cands if r["decision"] is None)
        sel = sum(1 for r in cands if r["decision"] == SELECTED)
        cnt["candidates_selected"] += sel
        cnt["candidates_rejected"] += sum(1 for r in cands if r["decision"] == REJECTED)
        cnt["candidates_undecided"] += undecided
        return {"version": RECORD_VERSION, "stage": self.ctx.stage, "linker": linker.header(),
                "extractor": EXTRACTOR_VERSION, "rules_digest": self.rules_digest,
                "exclude_kinds": sorted(self.exclude),
                "prompt": {"name": self.prompt.name, "digest": self.prompt.digest, "model": self.prompt.model},
                "complete": undecided == 0,
                "counts": {"anchors": ex.counts["anchors"], "candidates": len(cands), "selected": sel,
                           "rejected": len(cands) - sel - undecided, "undecided": undecided,
                           "dropped": {k.removeprefix("drop_"): v for k, v in sorted(ex.counts.items())
                                       if k.startswith("drop_")}},
                "candidates": sorted(cands, key=lambda r: (r["occurrences"][0]["position"], r["key"])),
                "dropped": ex.dropped}

    def _ask(self, linker: Linker, batch: list[tuple[Row, Row]]) -> None:
        cnt = self.counts
        user = render_user(linker, [it for _rec, it in batch])
        if self.client is None:
            self.client = open_client(self.config, run_id=self.ctx.run_id, stage=self.ctx.stage,
                                      store_root=self.ctx.store_root, budget_usd=self.budget)
        client = self.client
        p = self.prompt
        lc = cast(LicenseClass, self.c.channel(linker.channel).license_class)
        try:
            res = self.scope.call(lambda: client.complete(
                role=p.role, model=p.model, template=p.template, refs={}, user=user, schema=SCHEMA,
                license_class=lc, max_tokens=p.max_tokens, prompt_name=p.name))
        except FatalLlmError as exc:
            self.halted = True
            cnt["selector_halted"] += 1
            cnt[f"selector_failed_{exc.error_class}"] += 1
            return
        except LlmError as exc:
            cnt[f"selector_failed_{exc.error_class}"] += 1
            cnt["selector_cost_micro_usd"] += round(float(getattr(exc, "cost_usd", 0.0) or 0.0) * 1e6)
            return
        if res is None:
            cnt["selector_budget_stop"] += 1
            return
        cnt["selector_calls"] += 1
        cnt["selector_cache_hits"] += int(res.is_cache_hit)
        cnt["selector_items_asked"] += len(batch)
        cnt["selector_cost_micro_usd"] += round(res.cost_usd * 1e6)
        answered: dict[int, Row] = {}
        for a in res.parsed.get("items") or []:
            i = a.get("i")
            if isinstance(i, int) and 0 <= i < len(batch) and i not in answered:
                answered[i] = a
            else:
                cnt["selector_bad_index"] += 1
        for i, (rec, _item) in enumerate(batch):
            a = answered.get(i)
            if a is None:
                cnt["selector_missing_items"] += 1
                continue
            rec.update(decision=SELECTED if a["decision"] == "select" else REJECTED, by="llm",
                       reason=_cut(_ws(str(a.get("reason") or "")), REASON_CHARS), selector_kind=a.get("kind"),
                       model=p.model, prompt=p.name, prompt_digest=p.digest)

    # ---------------------------------------------------------------- rows
    def target_rows(self, records: Iterable[Row]) -> dict[tuple[str, str], Row]:
        """``(channel, external_id)`` → the row a selected target should have (module docstring)."""
        by_key: dict[str, Row] = {}
        for rec in records:
            lk = rec["linker"]
            for cand in rec["candidates"]:
                t = by_key.setdefault(cand["key"], {"group": cand["group"], "keys": cand["keys"], "kind": cand["kind"],
                                                    "linked_from": [], "selected": False})
                t["selected"] = t["selected"] or cand["decision"] == SELECTED
                t["linked_from"].append({
                    "source_id": lk["source_id"], "channel": lk["channel"], "title": lk["title"], "url": lk["url"],
                    "published_at": lk["published_at"], "decision": cand["decision"], "by": cand["by"],
                    "reason": cand["reason"], "selector_kind": cand["selector_kind"], "model": cand["model"],
                    "prompt": cand["prompt"], "occurrences": cand["occurrences"]})
        out: dict[tuple[str, str], Row] = {}
        for key, t in by_key.items():
            if not t["selected"]:
                continue
            linked = sorted(t["linked_from"], key=lambda lf: (lf["published_at"] or "", lf["source_id"]))
            out[(CHANNEL_OF_GROUP[t["group"]], key)] = {
                "target": {"group": t["group"], "keys": t["keys"], "kind": t["kind"]}, "linked_from": linked}
        return out

    def observe(self, channel: str, ext: str, record: Row) -> Row | None:
        linked = record["linked_from"]
        occs = [o for lf in linked for o in lf["occurrences"]]
        newest = sorted(linked, key=lambda lf: (lf["published_at"] or "", lf["source_id"]), reverse=True)
        relays = [("relay", lf["url"]) for lf in newest[:MAX_RELAYS] if lf["url"]]
        keys = [(k["kind"], k["value"]) for k in keys_for([tuple(k) for k in record["target"]["keys"]] + relays)]
        title = next((o["anchor"] for o in occs if _titleish(o["anchor"])), None)
        url = occs[0]["url"] if occs else None
        prior = self.c.prior(channel, ext)
        if prior is not None and prior["state"] != "retracted":  # first-minted values (module docstring)
            title = prior["metadata"].get("title") or title
            url = fetch_url(prior["identity"].get("canonical_url")) or url
        md: Row = {"title": title, "origin": "provider", "doc_type": DOC_TYPE_OF_GROUP.get(record["target"]["group"])}
        return self.c.observe(channel, ext, record, keys=keys, canonical_url=url,
                              metadata=md, provider_record=record, skip_unchanged=True)

    # ---------------------------------------------------------------- run
    def run(self) -> StageResult:
        ctx, c, cnt = self.ctx, self.c, self.counts
        docs = _documents(ctx)
        if docs is None:
            cnt["no_documents_input"] = 1
            c.counts.update(cnt)
            return c.finish()
        states = prior_states(ctx)
        cache = decision_cache(states)
        processed, eligible_members, processed_ids = self.linkers(docs)
        records: dict[str, Row] = {}  # linker source_id → the complete record the rows use
        partial = False
        for n, (lk, ex) in enumerate(processed):
            c.progress(done=n, total=len(processed))
            rec = self.decide(lk, ex, cache)
            digest = self.ctx.blobs.put(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            st = states.get(lk.source_id)
            if st is not None and st.latest_digest == digest:
                cnt["linking_unchanged"] += 1
            else:
                with c.window(lk.channel, {"links_of": lk.source_id, "decisions": digest}, c.now, c.now) as w:
                    w.seen = rec["counts"]["candidates"]
                    w.kept = rec["counts"]["selected"]
                    if rec["complete"]:
                        w.complete()
                    else:
                        w.truncated(cap=False)  # undecided candidates (halt/budget), not a cap (D492)
                cnt["linking_recorded"] += 1
            if rec["complete"]:
                records[lk.source_id] = rec
            else:
                partial = True
                cnt["linking_partial"] += 1
                if st is not None and st.complete is not None:
                    records[lk.source_id] = st.complete
        for sid, st in states.items():
            if sid in processed_ids:
                continue
            if sid in eligible_members and st.complete is not None:
                records[sid] = st.complete
                cnt["linking_carried"] += 1
            else:
                cnt["linking_dropped"] += 1
        c.progress(done=len(processed), total=len(processed))
        want = self.target_rows(records.values())
        cnt["targets_active"] = len(want)
        changed = 0
        for (channel, ext), record in sorted(want.items()):
            if self.observe(channel, ext, record) is not None:
                changed += 1
        prior_rows = {(r["identity"]["channel"], r["identity"]["external_id"]): r for r in _own(ctx, "sources")}
        retracted = 0
        for (channel, ext), r in sorted(prior_rows.items()):
            if (channel, ext) in want or r["state"] == "retracted":
                continue
            keys = [(k["kind"], k["value"]) for k in r["identity"]["keys"] if k["kind"] != "relay"]
            c.miss(channel, ext, query={"links": "no active selection"}, keys=keys)
            retracted += 1
        cnt["rows_retracted"] = retracted
        cnt["rows_observed"] = changed
        if changed + retracted > self.max_changed:
            raise CollectError(f"{ctx.stage}: {changed + retracted} changed target rows exceed max_changed_targets "
                               f"{self.max_changed} (a selector or normaliser bug? raise the option for a backfill)")
        c.counts.update({k: int(v) for k, v in cnt.items()})
        return c.finish(scope_complete=not partial and not self.halted and not self.scope.tripped)


def _mask(table: Any, ids: set[str]) -> Any:
    """A boolean mask of ``table`` rows whose ``doc_id`` is in ``ids``."""
    import pyarrow as pa
    import pyarrow.compute as pc

    return pc.is_in(table.column("doc_id"), value_set=pa.array(sorted(ids), pa.string()))


def _titleish(anchor: str | None) -> bool:
    a = (anchor or "").strip()
    return len(a.split()) >= 4 and len(a) <= ANCHOR_CHARS and not _URLISH.search(a)


def run(ctx: StageContext) -> StageResult:
    return Run(ctx).run()


__all__ = ["BATCH_SIZE", "CHANNEL_OF_GROUP", "MAX_CHANGED_TARGETS", "OPTIONS", "PROMPT_KEY", "ROLE", "SCHEMA",
           "Candidate",
           "Extracted", "Linker", "Occurrence", "Run", "body_of", "context_of", "decision_rows", "extract",
           "item_of", "linking_doc_ids", "prior_states", "render_user", "rule_kind", "run", "target_key"]
