"""Verification estimates + final gold. Usage: verify_apply.py <impl55_dir> <taxonomy.yaml> [--write]
Truth on the 30-doc sample: slugs both G0 (pre-verification gold) and Opus gave (incl. level-alternative matches) are
true; a disputed slug is true if the adjudicator said yes, false if no, excluded (dont_care) if borderline; a
better_slug named for a disputed slug is added as true. Estimates: G0 and Opus precision / recall against that
truth, group nodes excluded, by confidence and top strength; 95% doc-bootstrap CIs (10,000, seed 20261018).
--write: labels/final/<item>.json for all 120 items = G0, with the sample's verdicts applied (yes on an Opus-only
row -> row added from Opus, sources ["opus"], confidence med; no on a G0 row -> removed (listed in dropped);
borderline -> dont_care true (G0 row kept flagged, Opus-only row added flagged); better_slug -> row added if absent)."""
import json, random, sys, yaml
from pathlib import Path
I, taxf = Path(sys.argv[1]), Path(sys.argv[2]); write = '--write' in sys.argv
tax = yaml.safe_load(taxf.read_text()); GN = {g['slug'] for g in tax['group_nodes']}
key = json.load(open(I / 'verify/key.json'))
items = [r['item_id'] for r in json.load(open(I / 'verify/sample.json'))]
def top(r): return max([int(v) for v in (r.get('roles') or {}).values() if v] or [0])
per = {}
for i in items:
    g = json.load(open(I / 'labels/G0' / f'{i}.json')); o = json.load(open(I / 'verify/opus' / f'{i}.json'))
    gr = {r['slug']: r for r in g['techniques']}; orr = {r['slug']: r for r in o['techniques']}
    verd = {}
    if key[i]['disputes']:
        a = json.load(open(I / 'verify/adj' / f'{i}.json'))
        assert [v['slug'] for v in a['verdicts']] == [x['slug'] for x in key[i]['disputes']], i
        verd = {v['slug']: v for v in a['verdicts']}
    true, dc = set(), set()
    for s in set(gr) | set(orr):
        if s in verd:
            ans = verd[s]['answer']; (true if ans == 'yes' else dc if ans == 'borderline' else set()).add(s)
            b = verd[s].get('better_slug')
            if b and b != s: true.add(b)
        else: true.add(s)  # agreed (or level-alternative match)
    dc -= true
    per[i] = dict(g=gr, o=orr, true=true, dc=dc, verd=verd)
def counts(i, side, filt=lambda r: True):
    p = per[i]; rows = p[side]
    pred = {s for s, r in rows.items() if s not in GN and s not in p['dc'] and filt(r)}
    T = {s for s in p['true'] if s not in GN}
    return len(pred & T), len(pred - T), len(T - {s for s in rows if s not in GN})
def prf(ids, side, filt=lambda r: True):
    tp = fp = fn = 0
    for i in ids:
        a, b, c = counts(i, side, filt); tp += a; fp += b; fn += c
    return tp / max(1, tp + fp), tp / max(1, tp + fn), tp, fp, fn
def ci(side):
    rng = random.Random(20261018); P, R = [], []
    for _ in range(10000):
        ids = [rng.choice(items) for _ in items]; p, r, *_ = prf(ids, side); P.append(p); R.append(r)
    P.sort(); R.sort(); return (P[250], P[9750]), (R[250], R[9750])
out = {}
for side in ('g', 'o'):
    p, r, tp, fp, fn = prf(items, side); (pl, ph), (rl, rh) = ci(side)
    out[side] = {'P': round(p, 3), 'P_ci': [round(pl, 3), round(ph, 3)], 'R': round(r, 3), 'R_ci': [round(rl, 3), round(rh, 3)], 'tp': tp, 'fp': fp, 'fn': fn}
    for c in ('high', 'med', 'low'):
        pp, _, a, b, _ = prf(items, side, lambda r, c=c: r.get('confidence') == c); out[side][f'P_{c}'] = (round(pp, 3), a + b)
    for s in (1, 2, 3, 4):
        pp, _, a, b, _ = prf(items, side, lambda r, s=s: top(r) == s); out[side][f'P_str{s}'] = (round(pp, 3), a + b)
# recall of true slugs by the strength / confidence the other side gave them (misses of G0 by Opus confidence)
miss = {}
for i in items:
    p = per[i]
    for s in p['true'] - GN - set(p['g']):
        r = p['o'].get(s); k = (r or {}).get('confidence', 'adjudicator'); miss[k] = miss.get(k, 0) + 1
out['g0_misses_by_opus_confidence'] = miss
long_ids = [r['item_id'] for r in json.load(open(I / 'verify/sample.json')) if r['chars'] > 60000]
out['g_long'] = dict(zip(('P', 'R', 'tp', 'fp', 'fn'), [round(x, 3) if isinstance(x, float) else x for x in prf(long_ids, 'g')]))
out['g_short'] = dict(zip(('P', 'R', 'tp', 'fp', 'fn'), [round(x, 3) if isinstance(x, float) else x for x in prf([i for i in items if i not in long_ids], 'g')]))
out['dont_care_slugs'] = sum(len(p['dc'] - GN) for p in per.values())
print(json.dumps(out, indent=1))
if write:
    fin = I / 'labels/final'; fin.mkdir(parents=True, exist_ok=True)
    for f in (I / 'labels/G0').glob('*.json'):
        d = json.load(open(f)); i = d['item_id']
        if i in per:
            p = per[i]; rows = []; dropped = list(d.get('dropped') or []); acts = []
            for r in d['techniques']:
                v = p['verd'].get(r['slug'])
                if v and v['answer'] == 'no':
                    dropped.append({'slug': r['slug'], 'by': ['verification'], 'reason': v['reason']}); acts.append(f"removed {r['slug']}"); continue
                if v and v['answer'] == 'borderline':
                    r = {**r, 'dont_care': True, 'verification': v['reason']}; acts.append(f"dont_care {r['slug']}")
                elif v: r = {**r, 'verification': 'confirmed: ' + v['reason']}
                rows.append(r)
            have = {r['slug'] for r in rows} | {x['slug'] for x in dropped if x.get('by') == ['verification']}
            for s, v in p['verd'].items():
                if s not in p['g'] and v['answer'] in ('yes', 'borderline'):
                    o = p['o'][s]
                    rows.append({'slug': s, 'roles': o.get('roles') or {}, 'quote': v.get('quote') or o.get('quote', ''), 'confidence': 'med',
                                 'sources': ['opus'], 'alt_slugs': o.get('alt_slugs') or [], 'level_alternatives': v.get('level_alternatives') or o.get('level_alternatives') or [],
                                 'implemented_with': o.get('implemented_with') or [], 'decision': 'added by verification: ' + v['reason'],
                                 **({'dont_care': True} if v['answer'] == 'borderline' else {})}); acts.append(f"added {s}" + (' (dont_care)' if v['answer'] == 'borderline' else ''))
                b = v.get('better_slug')
                if b and b not in {r['slug'] for r in rows}:
                    src = p['g'].get(s) or p['o'].get(s)
                    rows.append({'slug': b, 'roles': src.get('roles') or {}, 'quote': v.get('quote') or src.get('quote', ''), 'confidence': 'med', 'sources': ['verification'],
                                 'alt_slugs': [], 'level_alternatives': v.get('level_alternatives') or [], 'implemented_with': [], 'decision': f'better slug for disputed {s}: ' + v['reason']})
                    acts.append(f"added {b} (better slug for {s})")
            d = {**d, 'techniques': rows, 'dropped': dropped, 'verification': {'sample': True, 'verifier': 'opus', 'adjudicator': 'sonnet', 'actions': acts}}
        (fin / f'{i}.json').write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True))
    print('wrote final/')
