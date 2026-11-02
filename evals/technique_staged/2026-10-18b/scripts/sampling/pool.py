"""Candidate pool for gold v2 (read-only over the store mirror's batch-import heads; no writes to the store).
Usage: python pool.py <STORE_DIR> <EXCLUDE_ITEMS_JSONL> <OUT_JSONL>
Head = newest *.full.parquet per table under data/<table>/batch-import (the finished snapshot)."""
import duckdb, glob, json, sys
from pathlib import Path
store, excl, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
h = lambda t: sorted(glob.glob(str(store / 'data' / t / 'batch-import' / '*.full.parquet')))[-1]
ex = {json.loads(l)['doc_id'] for l in excl.open()}
con = duckdb.connect(); con.execute("SET memory_limit='2GB'; SET threads=2")
heads = {t: Path(h(t)).name for t in ('corpus', 'stack_relevance', 'text_usability', 'doc_class')}
rows = con.execute(f"""
with c as (select doc_id, metadata.title as title, identity.anchor_source_id as anchor,
   [{{'source_id': m.source_id, 'channel': m.channel, 'license_class': m.license_class}} for m in identity.members] as members,
   content.current_text_digest as dig, content.current_role as role, content.current_source_id as cur_src,
   content.current_text_status as tst, content.current_text_quality as tq, content.same_raw_doc_ids as same
   from read_parquet('{h('corpus')}')),
 r as (select doc_id, verdict, confidence from read_parquet('{h('stack_relevance')}') where status='ok'),
 u as (select doc_id, verdict as usab from read_parquet('{h('text_usability')}') where status='ok'),
 d as (select doc_id, doc_type from read_parquet('{h('doc_class')}') where status='ok')
select c.*, r.verdict, r.confidence, u.usab, d.doc_type from c join r using(doc_id) join u using(doc_id) join d using(doc_id)
where r.verdict='relevant' and u.usab='usable' and c.tst='ok' and c.dig is not null
order by doc_id""").fetchall()
cols = [x[0] for x in con.description]
n = miss = 0
with out.open('w') as fh:
    for row in rows:
        r = dict(zip(cols, row))
        if r['doc_id'] in ex or any(s in ex for s in (r['same'] or [])):
            continue
        p = store / 'blobs' / r['dig']
        if not p.exists():
            miss += 1; continue
        chars = len(p.read_bytes().decode('utf-8', errors='replace'))
        mem = r['members'] or []
        sup = next((m for m in mem if m['source_id'] == r['cur_src']), None) or next((m for m in mem if m['source_id'] == r['anchor']), {})
        anc = next((m for m in mem if m['source_id'] == r['anchor']), {})
        fh.write(json.dumps({'doc_id': r['doc_id'], 'title': r['title'], 'doc_type': r['doc_type'], 'chars': chars,
            'channel': anc.get('channel'), 'license_class': sup.get('license_class'), 'basis_role': r['role'],
            'text_quality': r['tq'], 'text_digest': r['dig'], 'relevance_conf': r['confidence']}, ensure_ascii=False, sort_keys=True) + '\n')
        n += 1
print(json.dumps({'heads': heads, 'pool': n, 'blob_missing': miss, 'excluded_gold': len(ex)}))
