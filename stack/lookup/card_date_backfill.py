"""The card-date audit (v3 ``fetch.card_dates``, D18) and the ``collect.card-dates-lookup`` stage.

**Where it runs (proposed decision T2.6).** v3 ran it as its own workflow after fetch
(v3 ``configs/workflows.yaml`` ``fetch.card_dates``), never inside ``collect.cards``: it reads each
card's acquired content and may fetch the official page, up to two linked card documents and the
listing, which ``collect.cards`` (listing only, no item fetch) must not. In v4 it is its own
lookup stage, ``collect.card-dates-lookup`` (``workflows/collect/card-dates-lookup/main.py``, thin),
as DESIGN §11a's mapping has it.

**The stage** (:func:`run_lookup`). It reads the ``documents`` head (``--in
data/documents/<series>/``, as the other lookups) and audits each live document that has a member on a
``*_cards`` channel or an undated blog channel (:data:`CHANNELS`: :data:`CARD_CHANNELS` +
:data:`UNDATED_BLOGS`) and no ``metadata.published_at``. The audit
(:func:`audit_doc`, v3's, unchanged) reads the card's stored snapshot (``content.snapshots``: ``raw``,
``text``) first, then the listing title's leading dateline, and only when that
does not settle a date fetches the official page (+ ≤ 2 card-labelled ``.pdf``/``.md`` links on
it; hosts limited to :data:`HOSTS`). v3's second fallback, re-reading the listing page, needs
``discovery.note.listing_url``, which v4 does not store: it is not done.

Rows go on the own chain ``sources.card-dates-lookup`` under the ``card_dates`` channel,
``external_id`` = the card's ``url`` key, the one key ``url`` (so ``gather`` unions the row into the
card's document, §6 url group), ``lookup_of_source_ids`` = the card member(s):

* a date found → an observation, ``record`` = the audit result (candidates with their evidence),
  ``metadata.published_at`` = the date;
* missing / partial / conflict → the harness's miss row, ``query`` = ``{fingerprint,
  parser_version}`` (its ``query_digest`` is v3's skip key: an unchanged card is not audited
  again until the parser version or the card's content, URL or title changes; ``refresh`` forces);
* deferred (a read failed and nothing settled) → no row, the card stays due (v3 wrote it
  ``deferred`` and retried it).

The fetched pages are kept in ``blobs/`` (v3 did the same); the evidence cites their digests.
v3's 24-hour disk cache (``.cache/card-dates``) is dropped: the own chain is the memory.

Fetching: v3's :class:`stack.http.PoliteFetcher` (pacing per host at the card channel's
``politeness.rate_rps``, robots when the channel sets ``respect_robots``, v3's retry ladder, 20 s
timeout, 20 MB cap) over the module seam :data:`GET` (tests replace it). Options (plan line,
D72; the v3 env var wins): ``max_records`` / ``STACK_CARD_DATES_LIMIT``, ``channels`` /
``STACK_CARD_DATES_CHANNELS``, ``refresh`` / ``STACK_CARD_DATES_REFRESH``.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from stack.collect.base import query_digest
from stack.http import (
    DEFAULT_HEADERS,
    HostPacer,
    PoliteFetcher,
    RobotsCache,
    urllib_get,
)
from stack.lookup.card_dates import (
    VERSION,
    Tree,
    document_dates,
    evidence,
    listing_dates,
    resolve,
)
from stack.stage_runner import StageContext, StageResult
from stack.urls import url_key

CARD_CHANNELS = ('openai_cards','anthropic_cards','deepmind_cards','meta_cards','xai_cards','deepseek_cards','qwen_cards')
#: Blog channels whose listing never carries a publication date, audited like a card (D662):
#: ``deepmind_blog``'s one surface is a sitemap whose ``lastmod`` is a modification date (2026-07 for
#: 266 of its 351 posts on 2026-10-21: a site migration), while every post page carries
#: ``article:published_time`` and JSON-LD ``datePublished`` (6 of 6 pages checked, 2017-2024, matched
#: the posts' known dates). The stored page (acquire's ``page`` role) is read first, so a post
#: acquire has fetched costs no request.
UNDATED_BLOGS = ('deepmind_blog',)
CHANNELS = CARD_CHANNELS + UNDATED_BLOGS
HOSTS = ('openai.com','anthropic.com','deepmind.google','storage.googleapis.com','ai.meta.com',
         'llama.com','raw.githubusercontent.com','github.com','x.ai','deepseek.com','qwen.ai','qwenlm.github.io','huggingface.co')


def allowed(url):
    host = urlsplit(url).hostname or ''
    return urlsplit(url).scheme == 'https' and any(host == h or host.endswith('.'+h) for h in HOSTS)


#: The lookup's own channel (``configs/channels.yaml``).
LOOKUP_CHANNEL = 'card_dates'
#: The ``options`` keys :func:`run_lookup` reads (D434).
OPTIONS: frozenset[str] = frozenset({'channels', 'max_records', 'refresh'})
#: The HTTP getter seam (:class:`stack.http.HttpGetter`); tests replace it.
GET = urllib_get


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class Fetch:
    """``fetch(url, channel) -> (raw, final_url, media_type)`` for :func:`audit_doc`: v3's polite
    fetch at the card channel's politeness (module docstring); raises on any failure."""

    def __init__(self, channels: dict[str, Any], counts: dict[str, int]):
        self.channels = channels
        self.counts = counts
        self.http = PoliteFetcher(self._get,
                                  pacer=HostPacer(monotonic=time.monotonic, sleep=lambda s: _sleep(s)),
                                  robots=RobotsCache(self._get, DEFAULT_HEADERS['User-Agent'], 20),
                                  timeout=20, max_bytes=20_000_000, sleep=lambda s: _sleep(s))

    def _get(self, *a, **kw):
        """One request through the :data:`GET` seam, counted ``http_requests`` (robots reads and
        retries included, as the harness transport counts)."""
        self.counts['http_requests'] = self.counts.get('http_requests', 0) + 1
        return GET(*a, **kw)

    def __call__(self, url, channel):
        if not allowed(url): raise ValueError('Not an allowed official card URL')
        policy = self.channels[channel].politeness or {}
        self.counts['http_fetches'] = self.counts.get('http_fetches', 0) + 1
        outcome, response, _ = self.http.fetch(url, rate_rps=policy.get('rate_rps', 1),
                                               respect_robots=bool(policy.get('respect_robots', True)))
        if outcome or response is None or response.status != 200:
            raise ValueError(f'HTTP fetch failed: {outcome or (response.status if response else "no response")}')
        if not allowed(response.final_url): raise ValueError('Redirect left allowed official hosts')
        return response.body, response.final_url, response.headers.get('content-type', '')


def decode(raw,media):
    if raw.startswith(b'%PDF') or 'pdf' in (media or ''):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'card.pdf';p.write_bytes(raw)
            r=subprocess.run(['pdftotext','-f','1','-l','2','-layout',str(p),'-'],capture_output=True,timeout=30,check=True)
            return r.stdout.decode('utf-8'),False
    text=raw.decode('utf-8')
    return text, 'html' in (media or '') or '<html' in text[:2000].lower() or '<!doctype html' in text[:2000].lower()


def audit_doc(doc, blobs, fetch):
    candidates=[]; errors=[]; artifacts={}
    def parse(raw,url,media,ref=None):
        digest=hashlib.sha256(raw).hexdigest()
        if ref and digest!=ref: raise ValueError('Stored blob digest mismatch')
        text,html=decode(raw,media)
        found=document_dates(text,url,html)
        for c in found: c.update(url=url,blob_digest=digest,blob_bucket='raw')
        candidates.extend(found)
        artifacts[digest]=raw
        return text,html
    for field,bucket,media in [('text_sha256','text','text/plain'),('raw_sha256','raw',doc.get('media_type'))]:
        if doc.get(field):
            try: parse(blobs.get(doc[field]),doc['url'],media,doc[field])
            except Exception as exc: errors.append(f'{bucket}: {exc}')  # noqa: BLE001
    # Listing title is already scoped to this source. Only a leading dateline qualifies.
    if (c:=evidence(doc['title'],'listing_title')) and re.match(r'^(?:\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})',doc['title']):
        c.update(url=doc['discovery'].get('note',{}).get('listing_url') or doc['url'])
        candidates.append(c)
    result=resolve(candidates)
    if not result['pub_date'] and result['state']!='conflict':
        try:
            raw,url,media=fetch(doc['url'],doc['channel'])
            text,html=parse(raw,url,media)
            if html:
                # At most two direct, explicitly card-labelled document links; no crawl.
                links=[]
                for n in Tree(text).root.walk():
                    if n.tag=='a' and n.attrs.get('href'):
                        target=urljoin(url,n.attrs['href'])
                        if target!=url and allowed(target) and ('system card' in n.text().lower() or 'model card' in n.text().lower()) and target.lower().split('?')[0].endswith(('.pdf','.md')) and target not in links:
                            links.append(target)
                for target in links[:2]:
                    try:
                        data,actual,mime=fetch(target,doc['channel']);parse(data,actual,mime)
                    except Exception as exc: errors.append(f'linked card: {exc}')  # noqa: BLE001
        except Exception as exc: errors.append(f'official page: {exc}')  # noqa: BLE001
        listing=doc['discovery'].get('note',{}).get('listing_url')
        if listing and listing!=doc['url'] and allowed(listing):
            try:
                raw,url,media=fetch(listing,doc['channel']);content=raw.decode('utf-8')
                digest=hashlib.sha256(raw).hexdigest();artifacts[digest]=raw
                for c in listing_dates(content,url).get(doc['url'],[]):
                    c.update(url=url,blob_digest=digest,blob_bucket='raw');candidates.append(c)
            except Exception as exc: errors.append(f'listing: {exc}')  # noqa: BLE001
    result=resolve(candidates)
    if errors and not result['pub_date'] and result['state']!='conflict':result['state']='deferred'
    result.update(fingerprint=doc['fingerprint'],errors=errors)
    return result,artifacts


def _flag(value: Any) -> bool:
    return value is not None and str(value).strip().lower() in ('1', 'true', 'yes')


def _fingerprint(url: str, raw: str | None, text: str | None, title: str) -> str:
    """v3's skip fingerprint: what the audit reads (URL, stored content digests, listing title)."""
    return hashlib.sha256(json.dumps([url, raw, text, title], sort_keys=True).encode()).hexdigest()


def due_docs(docs: list[dict[str, Any]], c: Any, channels: set[str], refresh: bool
             ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The cards to audit this run (module docstring), as :func:`audit_doc`'s ``doc`` dicts plus
    ``key`` (the url key) and ``members`` (the card source ids), and skip counts."""
    counts = {'docs_cards': 0, 'docs_dated': 0, 'docs_done': 0, 'docs_unchanged': 0}
    out: dict[str, dict[str, Any]] = {}
    for d in sorted(docs, key=lambda d: d['doc_id']):
        ident = d['identity']
        if d['state'] == 'retired' or ident.get('is_masked'):
            continue
        cards = [m for m in ident['members'] if m['channel'] in channels and m['is_live']]
        if not cards:
            continue
        counts['docs_cards'] += 1
        if (d.get('metadata') or {}).get('published_at') is not None:
            counts['docs_dated'] += 1
            continue
        m = min(cards, key=lambda m: m['source_id'])
        key = m['key_value'] if m['key_kind'] == 'url' else (m['canonical_url'] or '')
        if not key or key in out:
            continue
        snaps = [s for s in ((d.get('content') or {}).get('snapshots') or [])
                 if s['source_id'] in {x['source_id'] for x in cards}]
        snap = max(snaps, key=lambda s: s['captured_at']) if snaps else None
        url = m['canonical_url'] or key
        doc = {'source_id': m['source_id'], 'channel': m['channel'], 'url': url, 'title': m['title'] or '',
                   'discovery': {}, 'raw_sha256': snap['raw']['sha256'] if snap else None,
                   'text_sha256': snap['text']['sha256'] if snap and snap['text'] else None,
                   'media_type': snap['raw']['media_type'] if snap else None}
        doc['fingerprint'] = _fingerprint(url, doc['raw_sha256'], doc['text_sha256'], doc['title'])
        prior = c.prior(LOOKUP_CHANNEL, key)
        if prior is not None and prior['state'] == 'observed':
            counts['docs_done'] += 1
            continue
        if not refresh and prior is not None and \
                prior['metadata']['query_digest'] == query_digest(skip_query(doc['fingerprint'])):
            counts['docs_unchanged'] += 1
            continue
        out[key] = {**doc, 'key': key, 'members': sorted(x['source_id'] for x in cards)}
    return list(out.values()), counts


def skip_query(fingerprint: str) -> dict[str, Any]:
    """The miss row's ``query``: its digest is the skip key (module docstring)."""
    return {'fingerprint': fingerprint, 'parser_version': VERSION}


def run_lookup(ctx: StageContext) -> StageResult:
    """One ``collect.card-dates-lookup`` run (module docstring)."""
    from stack.collect.base import CollectError, Collector

    c = Collector(ctx)
    keys = [k for k in ctx.inputs if k == 'documents' or k.startswith('documents/')]
    if len(keys) != 1:
        raise CollectError(f'{ctx.stage} reads the documents head: pass exactly one '
                           f'`--in data/documents/<series>/` (got {sorted(ctx.inputs)})')
    limit = c.option('max_records', 0, env='STACK_CARD_DATES_LIMIT', cast=int) or 0
    if limit < 0:
        raise CollectError('max_records must be >= 0')
    chosen = c.option('channels', None, env='STACK_CARD_DATES_CHANNELS')
    channels = set(chosen.split(',') if isinstance(chosen, str) else (chosen or CHANNELS))
    if channels - set(CHANNELS):
        raise CollectError(f'only card channels and undated blog channels are supported: '
                           f'{sorted(channels - set(CHANNELS))}')
    refresh = _flag(c.option('refresh', False, env='STACK_CARD_DATES_REFRESH'))
    c.channel(LOOKUP_CHANNEL)
    docs = ctx.view(keys[0]).select(['doc_id', 'state', 'identity', 'metadata', 'content']).to_pylist()
    due, counts = due_docs(docs, c, channels, refresh)
    c.counts.update(counts, cards_due=len(due),
                    already_covered=counts['docs_done'] + counts['docs_unchanged'])
    complete = True
    if limit and len(due) > limit:
        due, complete = due[:limit], False
    fetch = Fetch({ch: c.channel(ch) for ch in channels}, c.counts)
    states = {'found': 0, 'missing': 0, 'partial': 0, 'conflict': 0, 'deferred': 0}
    for i, doc in enumerate(due):
        c.progress(done=i, total=len(due))
        result, artifacts = audit_doc(doc, ctx.blobs, fetch)
        states[result['state']] += 1
        if result['state'] == 'deferred':
            continue  # a read failed and nothing settled: no row, due again next run
        for raw in artifacts.values():
            ctx.blobs.put(raw)
        keys_ = [('url', url_key(doc['key']) or doc['key'])]  # the key form even from a canonical_url
        # matched_by (D228): the skip query (the miss rows' query_digest is its skip key)
        match = {'query': skip_query(doc['fingerprint']), 'query_id': f'lookup:{LOOKUP_CHANNEL}',
                 'label': doc['channel']}
        if result['pub_date']:
            c.observe(LOOKUP_CHANNEL, doc['key'], result, keys=keys_, canonical_url=doc['url'],
                      metadata={'published_at': result['pub_date'],
                                'lookup_of_source_ids': doc['members'],
                                'provider_version': VERSION},
                      **match)
        else:
            c.miss(LOOKUP_CHANNEL, doc['key'], keys=keys_, **match)
    c.progress(done=len(due), total=len(due))
    c.counts.update({f'cards_{k}': v for k, v in states.items()})
    return c.finish(scope_complete=complete)
