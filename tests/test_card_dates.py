"""Publication dates are evidence-backed, scoped, and distinct from other dates."""
import datetime as dt
import hashlib
import json

import pytest

from stack.checks.verify import run_checks
from stack.collect.listing import Surface, candidate_of
from stack.lookup import card_date_backfill
from stack.lookup.card_dates import (
    document_dates,
    evidence,
    listing_dates,
    resolve,
    text_dates,
)
from stack.plan import Plan
from stack.store import chain as store_chain
from stack.store.blobs import open_blobs
from tests.fakes.http import FakeHttp
from tests.synth.records import run_line, sdir
from tests.synth.rows import blob, document_row, sid, with_digests
from tests.test_collect_lookup import publish_documents


def test_calendar_dates_and_precision():
    assert evidence('Published September 14, 2026','test')['value']=='2026-09-14'
    assert evidence('February 30, 2026','test') is None
    assert resolve([evidence('September 2026','test')])['pub_date'] is None
    assert resolve([evidence('2026','test')])['state']=='partial'


def test_listing_dates_do_not_leak_between_cards():
    html='<div><article><a href="/a">Card A</a><span>Published</span><time datetime="2026-01-02">Jan 2, 2026</time></article><article><a href="/b">Card B</a></article></div>'
    d=listing_dates(html,'https://openai.com/')
    assert resolve(d['https://openai.com/a'])['pub_date']=='2026-01-02'
    assert resolve(d['https://openai.com/b'])['pub_date'] is None


def test_openai_concatenated_listing_title():
    d=listing_dates('<a href="/a">Jul 09, 2026GPT-5.6 System Card</a>','https://openai.com/')
    assert resolve(d['https://openai.com/a'])['pub_date']=='2026-07-09'


def test_modified_and_model_release_are_not_publication():
    values=text_dates('# Model Card\nModel Release Date: March 3, 2026\nLast updated: April 4, 2026')
    assert resolve(values)['pub_date'] is None
    assert {v['kind'] for v in values}=={'modification','model_release'}
    c=candidate_of(Surface(key='test',method='sitemap'), 'https://openai.com/example-model-card','', '2026-02-01')
    assert c.date_hint=='' and c.modified_hint=='2026-02-01'


def test_jsonld_and_meta_publication_conflict():
    html='<meta property="article:published_time" content="2026-01-01T12:00:00Z"><meta property="article:modified_time" content="2026-02-01"><script type="application/ld+json">'+json.dumps({'@type':'Article','url':'https://openai.com/a','datePublished':'2026-01-02'})+'</script>'
    assert resolve(document_dates(html,'https://openai.com/a',True))['state']=='conflict'


def test_related_jsonld_does_not_supply_date():
    html='<script type="application/ld+json">'+json.dumps({'@type':'Article','url':'https://openai.com/other','datePublished':'2026-01-02'})+'</script>'
    assert resolve(document_dates(html,'https://openai.com/a',True))['pub_date'] is None


def test_pdf_text_and_markdown_datelines():
    for text in ['Model Card\n\nSeptember 14, 2026\nIntroduction', '# Model Card\n\n**Published:** September 14, 2026']:
        assert resolve(text_dates(text))['pub_date']=='2026-09-14'


def test_partial_conflict_with_exact_date():
    assert resolve([evidence('2026-01-01','x'),evidence('February 2026','y')])['state']=='conflict'


def test_read_failure_remains_retryable():
    from stack.lookup.card_date_backfill import audit_doc
    class Blobs:
        def get(self,*a,**kw):raise ValueError('missing')
    def fetch(*a):raise TimeoutError('timeout')
    result,_=audit_doc({'url':'https://openai.com/a','channel':'openai_cards','title':'Card','discovery':{},'text_sha256':'x','fingerprint':'f'},Blobs(),fetch)
    assert result['state']=='deferred' and not result['pub_date']


def test_partial_conflicts_and_reference_dates():
    assert resolve([evidence('January 2026','a'), evidence('February 2026','b')])['state']=='conflict'
    assert not text_dates('# Model Card\nIntroduction\n# References\nPublished January 1, 2025')


def test_stored_dates_need_no_network_and_verify_digest():
    import hashlib

    from stack.lookup.card_date_backfill import audit_doc
    raw=b'# Model Card\nPublished January 2, 2025'
    class Blobs:
        def get(self,*a,**kw):return raw
    doc={'url': 'https://openai.com/a','channel': 'openai_cards','title': 'Card','discovery': {},'fingerprint': 'f','text_sha256': hashlib.sha256(raw).hexdigest()}
    result,_=audit_doc(doc,Blobs(),lambda *a: pytest.fail('unnecessary network'))
    assert result['pub_date']=='2025-01-02'
    doc['text_sha256']='wrong'
    def failed(*a):raise TimeoutError('retry later')
    result,_=audit_doc(doc,Blobs(),failed)
    assert result['state']=='deferred' and 'digest mismatch' in result['errors'][0]


def test_listing_unlabelled_entry_date():
    html='<article><a href="/a">Model Card</a><span>June 3, 2026</span></article><article><a href="/b">Other Model Card</a></article>'
    dates=listing_dates(html,'https://openai.com/')
    assert resolve(dates['https://openai.com/a'])['pub_date']=='2026-06-03'
    assert not resolve(dates['https://openai.com/b'])['pub_date']


# ─────────────────────────────────────────────────────────────────────────────
# collect.card-dates-lookup on a temp store: documents head in, sources.card-dates-lookup out
# ─────────────────────────────────────────────────────────────────────────────
PAGE_B = 'https://www.anthropic.com/claude-x-system-card'
PAGE_C = 'https://openai.com/index/undated-system-card'
PAGE_D = 'https://openai.com/index/flaky-system-card'


def _card(i, channel, url, *, state='identified', published=None, **content):
    row = document_row(i, state=state)
    m = row['identity']['members'][0]
    m.update(channel=channel, key_kind='url', key_value=url, canonical_url=url,
             title=f'Card {i}', doc_type='system_card', published_at=None)
    row['identity'].update(channels=[channel], keys=[{'kind': 'url', 'value': url, 'source_id': sid(i)}])
    row['metadata']['published_at'] = published
    if content:  # an acquired card: its snapshot and current text are the stored blobs
        row['content']['snapshots'][0].update(content)
        row['content']['current_text_sha256'] = (content['text'] or {}).get('sha256')
        row['inband']['entries'] = []
    return with_digests(row)


@pytest.mark.slow  # real store + runner
def test_card_dates_lookup_rows_skips_and_deferral(tmp_path, monkeypatch):
    for var in ('STACK_CARD_DATES_LIMIT', 'STACK_CARD_DATES_CHANNELS', 'STACK_CARD_DATES_REFRESH'):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / 'store'
    blobs = open_blobs(root)
    text = b'# Model Card\n\n**Published:** January 2, 2025\n'
    a_raw, a_text = blobs.put(text), blobs.put(text)
    docs = [
        # A: the stored, acquired text settles the date; no network
        _card(1, 'openai_cards', 'https://openai.com/index/a-system-card', state='acquired',
              raw=blob(a_raw, len(text), 'text/markdown'), text=blob(a_text, len(text), 'text/markdown')),
        _card(2, 'anthropic_cards', PAGE_B),        # B: the official page's meta tag
        _card(3, 'openai_cards', PAGE_C),           # C: a page without a date: a miss
        _card(4, 'openai_cards', PAGE_D),           # D: the page fails: deferred, no row
        _card(5, 'openai_cards', 'https://openai.com/index/dated-system-card',
              published=dt.datetime(2024, 5, 1, tzinfo=dt.UTC)),   # already dated: not due
        with_digests(document_row(6)),              # not a card
    ]
    publish_documents(root, docs)
    fake = FakeHttp()
    fake.add('https://www.anthropic.com/robots.txt', status=404)
    fake.add('https://openai.com/robots.txt', status=404)
    fake.add(PAGE_B, body='<html><head><meta property="article:published_time" '
                          'content="2026-03-04T10:00:00Z"></head><body>Claude X</body></html>',
             headers={'Content-Type': 'text/html'})
    fake.add(PAGE_C, body='<html><body><h1>Undated system card</h1></body></html>',
             headers={'Content-Type': 'text/html'})
    fake.add(PAGE_D, status=503)
    monkeypatch.setattr(card_date_backfill, 'GET', fake.get)
    monkeypatch.setattr(card_date_backfill, '_sleep', lambda s: None)
    plan = Plan.model_validate({'plan': 'test', 'schema_version': 2, 'series': 'main', 'stages': [
        {'stage': 'collect.card-dates-lookup', 'prior': ['sources', 'coverage']}]})

    def go():
        rec = run_line(root, plan, 'collect.card-dates-lookup')
        assert rec.status == 'ok', rec.error
        return rec

    rec1 = go()
    assert (rec1.counts['docs_cards'], rec1.counts['docs_dated'], rec1.counts['cards_due']) == (5, 1, 4)
    assert (rec1.counts['cards_found'], rec1.counts['cards_missing'], rec1.counts['cards_deferred']) == (2, 1, 1)
    rows = {r['identity']['external_id']: r
            for r in store_chain.read_view(store_chain.resolve(root, sdir('sources.card-dates-lookup'))).to_pylist()}
    assert sorted(rows) == sorted(['https://openai.com/index/a-system-card', PAGE_B, PAGE_C])
    a = rows['https://openai.com/index/a-system-card']
    assert (a['state'], a['identity']['channel'], a['metadata']['published_at']) == (
        'observed', 'card_dates', dt.datetime(2025, 1, 2, tzinfo=dt.UTC))
    assert a['metadata']['lookup_of_source_ids'] == [sid(1)]
    assert a['identity']['keys'] == [{'kind': 'url', 'value': 'https://openai.com/index/a-system-card'},  # D772 alternates
                                     {'kind': 'url', 'value': 'https://openai.com/blog/a-system-card'},
                                     {'kind': 'url', 'value': 'https://openai.com/research/a-system-card'}]
    assert rows[PAGE_B]['metadata']['published_at'] == dt.datetime(2026, 3, 4, tzinfo=dt.UTC)
    assert (rows[PAGE_C]['state'], rows[PAGE_C]['metadata']['miss_count']) == ('missing', 1)
    assert not [u for u in fake.urls() if 'a-system-card' in u]  # A needed no network
    page_b = fake.routes[('GET', PAGE_B)].body
    assert blobs.get(hashlib.sha256(page_b).hexdigest()) == page_b  # kept, cited

    # run 2: C is unchanged (skipped without a fetch), D is due again, A/B are done
    n = len(fake.calls)
    rec2 = go()
    assert (rec2.counts['docs_done'], rec2.counts['docs_unchanged'], rec2.counts['cards_due']) == (2, 1, 1)
    assert rec2.counts['minted_ids'] == 0
    assert {u for u in fake.urls()[n:]} == {PAGE_D, 'https://openai.com/robots.txt'}

    results = {r.check: r for r in run_checks(root)}
    assert {k: r.items for k, r in results.items() if r.status in ('failed', 'warn')} == {}


# ─────────────────────────────────────────────────────────────────────────────
# deepmind_blog (D662): its sitemap listing carries no publication date
# ─────────────────────────────────────────────────────────────────────────────
DM_POST = 'https://deepmind.google/blog/specifying-ai-safety-problems-in-simple-environments/'
# The shape of the post page (2026-10-21): article:published_time and JSON-LD datePublished, and a
# modification date that is the site migration's (the sitemap's lastmod), not the post's.
DM_PAGE = ('<html><head><meta content=2017-11-28T00:00:00+00:00 property=article:published_time>'
           '<meta content=2026-07-06T12:51:10.775590+00:00 property=article:modified_time>'
           '<script type="application/ld+json">{"@type": "BlogPosting", "datePublished": '
           '"2017-11-28T00:00:00+00:00", "dateModified": "2026-07-06T12:51:10.775590+00:00"}</script>'
           '</head><body><h1>Specifying AI safety problems in simple environments</h1>'
           '<p>We present a suite of environments, first studied in April 2016.</p></body></html>')


def test_an_undated_deepmind_blog_post_is_dated_from_its_stored_page(tmp_path, monkeypatch):
    """A ``deepmind_blog`` post has no date in its listing (a sitemap; ``lastmod`` is a modification
    date). The lookup reads the acquired page's ``article:published_time``/JSON-LD
    ``datePublished`` (2017-11-28, not the 2026-07-06 modification, not a date in the prose) with
    no request; a post with no stored page is read from its page. The default channel set
    includes the channel."""
    for var in ('STACK_CARD_DATES_LIMIT', 'STACK_CARD_DATES_CHANNELS', 'STACK_CARD_DATES_REFRESH'):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / 'store'
    blobs = open_blobs(root)
    raw = blobs.put(DM_PAGE.encode())
    other = 'https://deepmind.google/blog/an-unfetched-post/'
    docs = [_card(1, 'deepmind_blog', DM_POST, state='acquired', raw=blob(raw, len(DM_PAGE.encode()), 'text/html'),
                  text=None),
            _card(2, 'deepmind_blog', other)]
    for d in docs:
        d['identity']['members'][0]['doc_type'] = 'lab_blog'
    docs[0]['content']['current_text_sha256'] = None
    publish_documents(root, [with_digests(d) for d in docs])
    fake = FakeHttp()
    fake.add('https://deepmind.google/robots.txt', status=404)
    fake.add(other, body=DM_PAGE.replace('2017-11-28', '2024-01-04'), headers={'Content-Type': 'text/html'})
    monkeypatch.setattr(card_date_backfill, 'GET', fake.get)
    monkeypatch.setattr(card_date_backfill, '_sleep', lambda s: None)
    plan = Plan.model_validate({'plan': 'test', 'schema_version': 2, 'series': 'main', 'stages': [
        {'stage': 'collect.card-dates-lookup', 'prior': ['sources', 'coverage']}]})
    rec = run_line(root, plan, 'collect.card-dates-lookup')
    assert rec.status == 'ok', rec.error
    assert (rec.counts['docs_cards'], rec.counts['cards_found']) == (2, 2)
    rows = {r['identity']['external_id']: r
            for r in store_chain.read_view(store_chain.resolve(root, sdir('sources.card-dates-lookup'))).to_pylist()}
    assert rows[DM_POST]['metadata']['published_at'] == dt.datetime(2017, 11, 28, tzinfo=dt.UTC)
    assert rows[other]['metadata']['published_at'] == dt.datetime(2024, 1, 4, tzinfo=dt.UTC)
    assert not [u for u in fake.urls() if 'specifying' in u]  # the stored page needed no network
