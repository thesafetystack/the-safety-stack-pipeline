"""Verification sample: 30 of the 120 gold-v2 docs, seed 20261019. Quotas per (group, long?) where long = >60k chars;
docs over 300k chars excluded from the draw except one card (cost). Usage: verify_sample.py <selected.jsonl> <out.json>"""
import json, random, sys
SEED = 20261019
Q = {('paper', 0): 7, ('paper', 1): 5, ('forum', 0): 5, ('forum', 1): 2, ('blog', 0): 4, ('card', 0): 2, ('card', 1): 1,
     ('report', 0): 2, ('report', 1): 2}
rows = sorted((json.loads(l) for l in open(sys.argv[1])), key=lambda r: r['item_id'])
rng = random.Random(SEED); out = []
for (g, lg), q in Q.items():
    c = [r for r in rows if r['group'] == g and (r['chars'] > 60000) == bool(lg) and (r['chars'] < 300000 or g == 'card')]
    out += rng.sample(c, q)
out = sorted(out, key=lambda r: r['item_id'])
json.dump([{'item_id': r['item_id'], 'group': r['group'], 'band': r['band'], 'chars': r['chars']} for r in out], open(sys.argv[2], 'w'), indent=1)
print(len(out), sum(r['chars'] > 60000 for r in out), sum(r['chars'] for r in out))
for r in out: print(r['item_id'], r['group'], r['band'], r['chars'], r['title'][:60])
