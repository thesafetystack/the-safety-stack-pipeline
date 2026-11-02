"""Parse Codex adjudication outputs runs/VA/A*.out.md -> verify/adj2/<item>.json (invalid JSON backslash escapes doubled)."""
import json, re, sys, collections
from pathlib import Path
I = Path(sys.argv[1]); key = json.load(open(I / 'verify/key2.json')); n = 0; probs = []
fix = lambda s: re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
for f in sorted((I / 'runs/VA').glob('A*.out.md')):
    for iid, js in re.findall(r'ITEM\s+(g2-\d{4})\s*```json\s*(\{.*?\})\s*```', f.read_text(), re.S):
        d = json.loads(fix(js)); exp = [x['slug'] for x in key[iid]['disputes'] if x['to_codex']]; got = [v['slug'] for v in d['verdicts']]
        if exp != got: probs.append((iid, 'order', exp, got))
        txt = re.sub(r'\s+', ' ', (I / f'texts/{iid}.txt').read_text(errors='replace'))
        for v in d['verdicts']:
            q = re.sub(r'\s+', ' ', v.get('quote') or '').strip()
            if q and q not in txt: probs.append((iid, v['slug'], 'quote not verbatim'))
        d['adjudicator'] = 'codex-sol'; (I / f'verify/adj2/{iid}.json').write_text(json.dumps(d, ensure_ascii=False, indent=1)); n += 1
print(n, 'items; problems:', probs)
c = collections.Counter()
for f in (I / 'verify/adj2').glob('*.json'):
    d = json.load(open(f)); k = {x['slug']: x for x in key[d['item_id']]['disputes']}
    for v in d['verdicts']: c[(k[v['slug']]['to_codex'], str(k[v['slug']]['sonnet']), v['answer'])] += 1
for x, y in sorted(c.items()): print(x, y)
