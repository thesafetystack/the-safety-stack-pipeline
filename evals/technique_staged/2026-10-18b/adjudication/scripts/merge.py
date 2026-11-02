"""Deterministic L1+L2 merge (used where the Codex reconciler could not run). Usage: merge.py <L1> <L2> <out_dir> [items.json]
Rows: union over slugs. sources ["X","Y"] when both gave the slug, else the one. roles: per role the max. confidence:
both -> the higher of the two; one -> min(its confidence, med). quote/reason/implemented_with from the row with the
higher confidence (L1 on ties). level_alternatives / alt_slugs: union (minus the row's own slug and minus slugs that
are rows of the merged item, for level_alternatives only when that slug has its own row). doc_kind from L1.
extra_techniques / menu_gaps: L1's plus L2's entries not already present by name/surface."""
import json, sys
from pathlib import Path
A, B, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]); out.mkdir(parents=True, exist_ok=True)
only = set(json.load(open(sys.argv[4]))) if len(sys.argv) > 4 else None
RANK = {'low': 0, 'med': 1, 'high': 2}
for f in sorted(A.glob('*.json')):
    i = f.stem
    if only is not None and i not in only: continue
    a, b = json.load(open(f)), json.load(open(B / f.name))
    ra = {r['slug']: r for r in a['techniques']}; rb = {r['slug']: r for r in b['techniques']}
    rows = []
    for s in sorted(set(ra) | set(rb)):
        x, y = ra.get(s), rb.get(s)
        src = [n for n, r in (('X', x), ('Y', y)) if r]
        cands = [r for r in (x, y) if r]
        best = max(cands, key=lambda r: RANK.get(r.get('confidence'), 1))  # stable: x first on ties
        roles = {}
        for r in cands:
            for k, v in (r.get('roles') or {}).items():
                if v: roles[k] = max(roles.get(k, 0), int(v))
        conf = best.get('confidence', 'med')
        if len(src) == 1 and RANK.get(conf, 1) > 1: conf = 'med'
        la = sorted({z for r in cands for z in r.get('level_alternatives') or []} - {s})
        alt = sorted({z for r in cands for z in r.get('alt_slugs') or []} - {s})
        rows.append({'slug': s, 'roles': roles, 'quote': best.get('quote', ''), 'confidence': conf, 'sources': src,
                     'alt_slugs': alt, 'level_alternatives': la, 'implemented_with': best.get('implemented_with') or [],
                     'decision': f"merge rule (no reconciler): {'both labellers' if len(src) == 2 else 'one labeller, kept at <= med'}; reason: {best.get('reason', '')}"})
    names = {e.get('name_as_written') for e in a.get('extra_techniques') or []}
    surf = {e.get('surface') for e in a.get('menu_gaps') or []}
    d = {'item_id': i, 'doc_kind': a.get('doc_kind'), 'techniques': rows, 'dropped': [],
         'extra_techniques': (a.get('extra_techniques') or []) + [e for e in b.get('extra_techniques') or [] if e.get('name_as_written') not in names],
         'menu_gaps': (a.get('menu_gaps') or []) + [e for e in b.get('menu_gaps') or [] if e.get('surface') not in surf],
         'doc_note': 'merge rule (Codex reconciler unavailable: usage limit). X: ' + (a.get('doc_note') or '') + ' | Y: ' + (b.get('doc_note') or ''),
         'disagreement_note': '', 'adjudicator': 'merge-rule'}
    (out / f'{i}.json').write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True))
print('merged')
