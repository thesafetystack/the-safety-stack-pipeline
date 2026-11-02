"""Verification disputes: gold (G_dir) vs Opus (O_dir) on the verify sample; slug-level, group nodes included.
A slug is disputed if only one side has it, unless it is a level alternative of a row on the other side.
Labellers are shown as P/Q with a seeded per-item swap (seed 20261020) so the adjudicator cannot tell gold from Opus.
Usage: disputes.py <G_dir> <O_dir> <sample.json> <texts> <common> <out_dir>   -> packet V/ + key.json"""
import json, random, shutil, sys, hashlib, re
from pathlib import Path
G, O, sample, texts, common, out = map(Path, sys.argv[1:7])
items = [r['item_id'] for r in json.load(open(sample))]
rng = random.Random(20261020); key = {}; md = ['# Disputed rows\n']
d = out / 'V'; 
if d.exists(): shutil.rmtree(d)
(d / 'texts').mkdir(parents=True)
for f in common.iterdir(): shutil.copy(f, d / f.name)
for i in items:
    g = json.load(open(G / f'{i}.json')); o = json.load(open(O / f'{i}.json'))
    gr = {r['slug']: r for r in g['techniques']}; orr = {r['slug']: r for r in o['techniques']}
    galt = {a for r in g['techniques'] for a in r.get('level_alternatives') or []}
    oalt = {a for r in o['techniques'] for a in r.get('level_alternatives') or []}
    swap = rng.random() < 0.5; name = {'gold': 'Q' if swap else 'P', 'opus': 'P' if swap else 'Q'}
    disp = []
    for s, r in gr.items():
        if s not in orr and s not in oalt and not (set(r.get('level_alternatives') or []) & set(orr)): disp.append(('gold', s, r))
    for s, r in orr.items():
        if s not in gr and s not in galt and not (set(r.get('level_alternatives') or []) & set(gr)): disp.append(('opus', s, r))
    disp.sort(key=lambda x: x[1])
    key[i] = {'swap': swap, 'disputes': [{'slug': s, 'by': who} for who, s, _ in disp]}
    if not disp: continue
    shutil.copy(texts / f'{i}.txt', d / 'texts' / f'{i}.txt')
    md.append(f'\n## {i}\n\nRows both labellers agree on (not disputed): {", ".join(sorted(set(gr) & set(orr))) or "none"}\n')
    for who, s, r in disp:
        other = sorted(orr if who == 'gold' else gr)
        md.append(f"\n- `{s}` given only by labeller {name[who]} (roles {json.dumps(r.get('roles'))}, confidence {r.get('confidence')}); quote: \"{r.get('quote','')}\"; reason: {r.get('reason') or r.get('decision') or r.get('note') or ''}\n  (the other labeller's slugs on this document: {', '.join(other)})\n")
(d / 'DISPUTES.md').write_text(''.join(md))
todo = [i for i in items if key[i]['disputes']]
(d / 'PROMPT.md').write_text((out.parent / 'prompts' / 'VERIFY-ADJ.md').read_text().replace('{{ITEMS}}', ', '.join(todo)))
json.dump(key, open(out / 'key.json', 'w'), indent=1)
# [line removed at staging: it names workspace paths or a person; packet.json has the original sha256]
sums = []
for f in sorted(p for p in d.rglob('*') if p.is_file()):
    b = f.read_bytes()
    if f.parent.name != 'texts' and LEAK.search(b.decode('utf-8', 'replace')): raise SystemExit(f'leak {f}')
    sums.append(f"{hashlib.sha256(b).hexdigest()}  {f.relative_to(d)}\n")
(out / 'V.sha256').write_text(''.join(sums))
print(len(todo), 'items with disputes;', sum(len(v['disputes']) for v in key.values()), 'disputed slugs')
