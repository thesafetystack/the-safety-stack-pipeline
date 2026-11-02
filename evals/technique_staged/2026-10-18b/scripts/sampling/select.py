"""Stratified sample for gold v2 from pool.jsonl. Usage: python select.py <pool.jsonl> <out.jsonl>
Seed 20261018. Strata = doc-type group x length band; quotas below; within a cell, random.Random(seed).sample over the
pool sorted by doc_id; licence classes open / public_web only (restricted texts are not sent to an external labeller).
If a cell has fewer docs than its quota, all are taken and the shortfall is reported (not redistributed)."""
import json, random, sys, collections
SEED = 20261018
GROUP = {'preprint': 'paper', 'paper': 'paper', 'forum_post': 'forum', 'lab_blog': 'blog', 'system_card': 'card',
         'model_card': 'card', 'eval_report': 'report', 'framework_policy': 'report', 'critique': 'report', 'other': 'report'}
BANDS = ('<20k', '20-60k', '60-100k', '>100k')
QUOTA = {  # group: quotas per band
    'paper': (8, 15, 14, 15), 'forum': (14, 10, 5, 3), 'blog': (14, 3, 0, 1), 'card': (3, 2, 0, 3), 'report': (2, 3, 2, 3)}
def band(c): return BANDS[0] if c < 20000 else BANDS[1] if c < 60000 else BANDS[2] if c < 100000 else BANDS[3]
pool = sorted((json.loads(l) for l in open(sys.argv[1])), key=lambda r: r['doc_id'])
pool = [p for p in pool if p['license_class'] in ('open', 'public_web') and p['doc_type'] in GROUP]
cells = collections.defaultdict(list)
for p in pool: cells[(GROUP[p['doc_type']], band(p['chars']))].append(p)
rng = random.Random(SEED); out = []; short = []
for g, qs in QUOTA.items():
    for b, q in zip(BANDS, qs):
        c = cells[(g, b)]
        take = rng.sample(c, q) if len(c) >= q else list(c)
        if len(c) < q: short.append((g, b, q, len(c)))
        for p in sorted(take, key=lambda r: r['doc_id']): out.append({**p, 'group': g, 'band': b})
with open(sys.argv[2], 'w') as fh:
    for i, p in enumerate(out, 1): fh.write(json.dumps({'item_id': f'g2-{i:04d}', **p}, ensure_ascii=False, sort_keys=True) + '\n')
print('selected', len(out), 'short', short)
print(collections.Counter(p['group'] for p in out), collections.Counter(p['band'] for p in out))
