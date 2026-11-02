"""Second verification round (Codex adjudicator). Usage: disputes2.py <impl55_dir>
Gold G1 = labels/R (Astra reconciliation, all 120). Disputes G1 vs Opus on the 30-doc sample as in disputes.py.
A disputed slug goes to the Codex adjudicator when (a) it has no Sonnet verdict (newly disputed after reconciliation),
(b) Sonnet said borderline, or (c) the two Sonnet runs of batch S1 disagreed on it. P/Q blinding with seed 20261021.
Writes verify/V2/ (packet: common files, texts, DISPUTES.md, PROMPT.md), verify/key2.json, verify/V2.sha256."""
import hashlib, json, random, re, shutil, sys
from pathlib import Path
I = Path(sys.argv[1]); V = I / 'verify'
items = [r['item_id'] for r in json.load(open(V / 'sample.json'))]
son = {}
for f in (V / 'adj').glob('g2-*.json'):
    d = json.load(open(f)); son[d['item_id']] = {v['slug']: v for v in d['verdicts']}
first = {}
for f in (V / 'adj-s1-first').glob('g2-*.json'):
    d = json.load(open(f)); first[d['item_id']] = {v['slug']: v['answer'] for v in d['verdicts']}
rng = random.Random(20261021); key = {}; md = ['# Disputed rows\n']
d = V / 'V2'
if d.exists(): shutil.rmtree(d)
(d / 'texts').mkdir(parents=True)
for f in (I / 'packet-common').iterdir(): shutil.copy(f, d / f.name)
for i in items:
    g = json.load(open(I / 'labels/R' / f'{i}.json')); o = json.load(open(V / 'opus' / f'{i}.json'))
    gr = {r['slug']: r for r in g['techniques']}; orr = {r['slug']: r for r in o['techniques']}
    galt = {a for r in g['techniques'] for a in r.get('level_alternatives') or []}
    oalt = {a for r in o['techniques'] for a in r.get('level_alternatives') or []}
    disp = []
    for s, r in gr.items():
        if s not in orr and s not in oalt and not (set(r.get('level_alternatives') or []) & set(orr)): disp.append(('gold', s, r))
    for s, r in orr.items():
        if s not in gr and s not in galt and not (set(r.get('level_alternatives') or []) & set(gr)): disp.append(('opus', s, r))
    disp.sort(key=lambda x: x[1])
    swap = rng.random() < 0.5; name = {'gold': 'Q' if swap else 'P', 'opus': 'P' if swap else 'Q'}
    rows = []
    for who, s, r in disp:
        sv = son.get(i, {}).get(s); why = None
        if sv is None: why = 'new'
        elif sv['answer'] == 'borderline': why = 'sonnet_borderline'
        elif i in first and s in first[i] and first[i][s] != sv['answer']: why = 'sonnet_unstable'
        rows.append({'slug': s, 'by': who, 'sonnet': sv['answer'] if sv else None, 'to_codex': why})
    key[i] = {'swap': swap, 'disputes': rows}
    send = [(who, s, r) for (who, s, r), x in zip(disp, rows) if x['to_codex']]
    if not send: continue
    shutil.copy(I / 'texts' / f'{i}.txt', d / 'texts' / f'{i}.txt')
    md.append(f'\n## {i}\n\nRows both labellers agree on (not disputed): {", ".join(sorted(set(gr) & set(orr))) or "none"}\n')
    for who, s, r in send:
        other = sorted(orr if who == 'gold' else gr)
        md.append(f"\n- `{s}` given only by labeller {name[who]} (roles {json.dumps(r.get('roles'))}, confidence {r.get('confidence')}); quote: \"{r.get('quote','')}\"; reason: {r.get('reason') or r.get('decision') or r.get('note') or ''}\n  (the other labeller's slugs on this document: {', '.join(other)})\n")
(d / 'DISPUTES.md').write_text(''.join(md))
todo = [i for i in items if any(x['to_codex'] for x in key[i]['disputes'])]
json.dump(key, open(V / 'key2.json', 'w'), indent=1)
# [line removed at staging: it names workspace paths or a person; packet.json has the original sha256]
for f in d.rglob('*'):
    if f.is_file() and f.parent.name != 'texts' and LEAK.search(f.read_text(errors='replace')): raise SystemExit(f'leak {f}')
c = {}
for v in key.values():
    for x in v['disputes']: c[x['to_codex'] or 'kept_sonnet'] = c.get(x['to_codex'] or 'kept_sonnet', 0) + 1
print(len(todo), 'items to Codex;', c)
json.dump(todo, open(V / 'V2-items.json', 'w'))
