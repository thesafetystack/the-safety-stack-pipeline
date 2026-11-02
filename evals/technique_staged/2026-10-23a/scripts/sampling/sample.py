"""Gold v3 sample: 100 documents shared by technique_staged 2026-10-23a and model_roles 2026-10-23a.

Read only (the store directory is a mirror; nothing is written there). Three steps, each writing into OUT:

  sample.py frame  <STORE_DIR> <CORPUS_REL> <OUT>   -> OUT/frame.parquet   (per-doc frame, decoded text length)
  sample.py scan   <STORE_DIR> <OUT> [--procs 2]   -> OUT/mentions.jsonl  (LLM-free model matcher, whole current text)
  sample.py select <OUT> <EVALS_DIR>               -> OUT/sample.jsonl, OUT/sampling.json

Frame: the corpus build's `screen_relevant` rows (screen status ok and verdict relevant, as the internal release
query), all licence classes, degraded texts included. Eligible = frame minus documents without usable current text
(current text status not ok, blob missing, or text_usability verdict `unusable`) minus documents already in an
existing eval set (any `evals/*/*/items*.jsonl` row outside this
sample's own 2026-10-23a sets: same doc_id, a former or same-raw doc_id, the same current text
sha256, or the same normalised title of more than 15 characters).

Design: source kind = doc_class doc_type grouped (paper: preprint, paper; forum: forum_post; blog: lab_blog; card:
system_card, model_card; report: eval_report, framework_policy, critique, other, webpage, regulatory). Kind allocation
proportional to the eligible counts with a minimum of MIN_PER_KIND (largest remainder, iterated so that a kind lifted
to the minimum takes its documents from the others). Within a kind, systematic sampling with a seeded random start
over the kind's documents sorted by (year bucket, model-mention bucket, seeded random key): implicit stratification,
so year and mention buckets are represented proportionally within a kind (within one document), and every document of
a kind has inclusion probability n_k / N_k exactly; design weight = N_k / n_k. Year bucket from metadata.published_at
(<=2019, 2020-2023, 2024, 2025, 2026, none). Mention bucket = distinct strong-matched catalogue nodes (0, 1-3, 4+).
Item ids g3-0001.. are assigned in a seeded random order (they carry no stratum information).
"""
import argparse
import collections
import hashlib
import importlib.util
import json
import math
import random
import re
import sys
from pathlib import Path

SEED = 20261023
SET_ID = '2026-10-23a'
N = 100
MIN_PER_KIND = 3
KIND = {'preprint': 'paper', 'paper': 'paper', 'forum_post': 'forum', 'lab_blog': 'blog', 'system_card': 'card',
        'model_card': 'card', 'eval_report': 'report', 'framework_policy': 'report', 'critique': 'report',
        'other': 'report', 'webpage': 'report', 'regulatory': 'report'}
KINDS = ('paper', 'forum', 'blog', 'card', 'report')
YEARS = ('<=2019', '2020-2023', '2024', '2025', '2026', 'none')
MENTIONS = ('0', '1-3', '4+')
BANDS = ('<20k', '20-60k', '60-100k', '>100k')
REPO = Path(__file__).resolve().parents[5]


def year_bucket(y):
    if y is None:
        return 'none'
    return '<=2019' if y <= 2019 else '2020-2023' if y <= 2023 else str(y) if y <= 2026 else 'none'


def mention_bucket(n):
    return '0' if n == 0 else '1-3' if n <= 3 else '4+'


def band(c):
    return BANDS[0] if c < 20000 else BANDS[1] if c < 60000 else BANDS[2] if c < 100000 else BANDS[3]


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def cmd_frame(a):
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
    store, out = Path(a.store), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    c.execute("SET memory_limit='3GB'; SET threads=2")
    f = str(store / a.corpus)
    t = c.sql(f"""
    select doc_id, (annotations.screen.status = 'ok' and annotations.screen.payload.verdict = 'relevant') as screen_relevant,
      annotations.text_usability.status as tu_status, annotations.text_usability.payload.verdict as usability,
      annotations.doc_class.payload.doc_type as doc_type, year(metadata.published_at) as year, metadata.title as title,
      content.current_text_sha256 as text_sha256, content.current_text_status as text_status,
      content.current_role as basis_role, content.current_source_id as current_source_id,
      coalesce(list_filter(identity.members, lambda m: m.source_id = content.current_source_id)[1].license_class,
               list_filter(identity.members, lambda m: m.source_id = identity.anchor_source_id)[1].license_class) as license_class,
      list_filter(identity.members, lambda m: m.source_id = identity.anchor_source_id)[1].channel as channel,
      identity.former_doc_ids as former_doc_ids, content.same_raw_doc_ids as same_raw_doc_ids
    from read_parquet('{f}')
    where annotations.screen.status = 'ok' and annotations.screen.payload.verdict = 'relevant'
    order by doc_id""").to_arrow_table()
    present, chars = [], []
    for d in t.column('text_sha256').to_pylist():
        p = store / 'blobs' / d if d else None
        ok = bool(p and p.exists())
        present.append(ok)
        chars.append(len(p.read_bytes().decode('utf-8', errors='replace')) if ok else None)
    t = t.append_column('blob_present', pa.array(present)).append_column('chars', pa.array(chars, pa.int64()))
    pq.write_table(t, out / 'frame.parquet')
    meta = {'corpus': a.corpus, 'corpus_sha256': sha256_file(f), 'screen_relevant_rows': t.num_rows}
    (out / 'frame.json').write_text(json.dumps(meta, indent=1) + '\n')
    print(json.dumps(meta))


_SC = None


def _scan_one(arg):
    global _SC
    store, doc_id, dig = arg
    if _SC is None:
        spec = importlib.util.spec_from_file_location('mmatch', REPO / 'workflows/annotate/model_roles/match.py')
        m = importlib.util.module_from_spec(spec)
        sys.modules['mmatch'] = m
        spec.loader.exec_module(m)
        _SC = m.scanner_for(REPO / 'configs')
    text = (Path(store) / 'blobs' / dig).read_bytes().decode('utf-8', errors='replace')
    s = _SC.scanner.scan(text)
    return {'doc_id': doc_id, 'strong': sorted(s['mentions']), 'weak': sorted(s['weak'])}


def cmd_scan(a):
    import duckdb
    from multiprocessing import Pool
    out = Path(a.out)
    rows = duckdb.sql(f"select doc_id, text_sha256 from '{out / 'frame.parquet'}' where blob_present order by doc_id").fetchall()
    if a.only:
        keep = {json.loads(line)['doc_id'] for line in open(a.only)}
        rows = [r for r in rows if r[0] in keep]
    res = []
    with Pool(a.procs) as p:
        for r in p.imap_unordered(_scan_one, [(a.store, d, g) for d, g in rows], chunksize=16):
            res.append(r)
    res.sort(key=lambda r: r['doc_id'])
    dst = Path(a.dst) if a.dst else out / 'mentions.jsonl'
    with open(dst, 'w') as fh:
        for r in res:
            fh.write(json.dumps(r, sort_keys=True) + '\n')
    print('scanned', len(res), '->', dst.name, 'models.yaml', sha256_file(REPO / 'configs/models.yaml')[:12])


def _norm_title(s):
    return re.sub(r'\W+', ' ', (s or '').lower()).strip()


def existing_eval_keys(evals_dir):
    ids, digs, titles, files = set(), set(), set(), []
    for f in sorted(Path(evals_dir).glob('*/*/items*.jsonl')):
        if f.parent.name == SET_ID:  # this sample's own frozen sets
            continue
        files.append(str(f.relative_to(Path(evals_dir).parent)))
        for line in open(f):
            r = json.loads(line)
            if r.get('doc_id'):
                ids.add(r['doc_id'])
            si = r.get('source_item') or ''
            if si.startswith('mirror:'):
                ids.add(si.split(':', 1)[1])
            inp = r.get('input') or {}
            digs |= {inp.get('text_digest'), inp.get('full_text_digest')} - {None}
            t = _norm_title(inp.get('title'))
            if len(t) > 15:
                titles.add(t)
    return ids, digs, titles, files


def allocate(counts, n, minimum):
    """Largest-remainder allocation proportional to counts, each kind at least min(minimum, count)."""
    fixed = {}
    while True:
        rest = {k: c for k, c in counts.items() if k not in fixed}
        n_rest = n - sum(fixed.values())
        tot = sum(rest.values())
        quota = {k: n_rest * c / tot for k, c in rest.items()}
        low = {k for k, q in quota.items() if q < minimum}
        if not low:
            break
        for k in sorted(low):
            fixed[k] = min(minimum, counts[k])
    alloc = {k: math.floor(q) for k, q in quota.items()}
    left = n_rest - sum(alloc.values())
    for k in sorted(quota, key=lambda k: (-(quota[k] - alloc[k]), k))[:left]:
        alloc[k] += 1
    alloc.update(fixed)
    return {k: alloc[k] for k in counts}, quota  # in the kinds' order (JSON output stable across hash seeds)


def cmd_select(a):
    import duckdb
    out = Path(a.out)
    frame = duckdb.sql(f"select * from '{out / 'frame.parquet'}' order by doc_id").fetchall()
    cols = [d[0] for d in duckdb.sql(f"select * from '{out / 'frame.parquet'}' limit 0").description]
    frame = [dict(zip(cols, r)) for r in frame]
    ment = {}
    for line in open(out / 'mentions.jsonl'):
        r = json.loads(line)
        ment[r['doc_id']] = len(r['strong'])
    ids, digs, titles, files = existing_eval_keys(a.evals)
    drop = collections.Counter()
    elig = []
    for r in frame:
        if not (r['text_status'] == 'ok' and r['blob_present'] and r['tu_status'] == 'ok' and r['usability'] != 'unusable'):
            drop['no_usable_text'] += 1
            continue
        keys = {r['doc_id'], *(r['former_doc_ids'] or []), *(r['same_raw_doc_ids'] or [])}
        if keys & ids or r['text_sha256'] in digs or _norm_title(r['title']) in titles and len(_norm_title(r['title'])) > 15:
            drop['in_existing_eval'] += 1
            continue
        if r['doc_type'] not in KIND:
            drop[f"unmapped_doc_type:{r['doc_type']}"] += 1
            continue
        r = dict(r, kind=KIND[r['doc_type']], year_bucket=year_bucket(r['year']),
                 mention_n=ment[r['doc_id']], mention_bucket=mention_bucket(ment[r['doc_id']]))
        elig.append(r)
    rng = random.Random(SEED)
    keys = list(range(len(elig)))
    rng.shuffle(keys)
    for r, k in zip(elig, keys):
        r['_key'] = k
    by_kind = collections.defaultdict(list)
    for r in elig:
        by_kind[r['kind']].append(r)
    counts = {k: len(by_kind[k]) for k in KINDS}
    alloc, quota = allocate(counts, a.n, MIN_PER_KIND)
    picked = []
    starts = {}
    for k in KINDS:
        units = sorted(by_kind[k], key=lambda r: (YEARS.index(r['year_bucket']), MENTIONS.index(r['mention_bucket']), r['_key']))
        Nk, nk = len(units), alloc[k]
        step = Nk / nk
        u = rng.random() * step
        starts[k] = u
        for j in range(nk):
            r = units[math.floor(u + j * step)]
            picked.append(dict(r, weight=Nk / nk, inclusion_prob=nk / Nk))
    order = list(range(len(picked)))
    rng.shuffle(order)
    sample = []
    for i, idx in enumerate(order, 1):
        r = picked[idx]
        sample.append({'item_id': f'g3-{i:04d}', 'doc_id': r['doc_id'], 'stratum': r['kind'], 'weight': round(r['weight'], 6),
                       'inclusion_prob': round(r['inclusion_prob'], 8), 'source_kind': r['kind'], 'doc_type': r['doc_type'],
                       'year': r['year'], 'year_bucket': r['year_bucket'], 'chars': r['chars'], 'band': band(r['chars']),
                       'mention_n': r['mention_n'], 'mention_bucket': r['mention_bucket'], 'text_sha256': r['text_sha256'],
                       'license_class': r['license_class'], 'usability': r['usability'], 'channel': r['channel'],
                       'basis_role': r['basis_role']})
    sample.sort(key=lambda r: r['item_id'])
    with open(out / 'sample.jsonl', 'w') as fh:
        for r in sample:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n')

    def tab(rows, a_, b_, av, bv):
        c = collections.Counter((r[a_], r[b_]) for r in rows)
        return {x: {y: c[(x, y)] for y in bv if c[(x, y)]} for x in av}
    fmeta = json.loads((out / 'frame.json').read_text())
    info = {
        'seed': SEED, 'n': a.n, 'min_per_kind': MIN_PER_KIND, 'frame': fmeta,
        'mentions_sha256': sha256_file(out / 'mentions.jsonl'), 'models_yaml_sha256': sha256_file(REPO / 'configs/models.yaml'),
        'excluded': dict(drop), 'eligible': len(elig), 'existing_eval_files': files,
        'kind_counts': counts, 'kind_quota_unrounded': {k: round(v, 3) for k, v in quota.items()}, 'allocation': alloc,
        'systematic_start': {k: round(v, 6) for k, v in starts.items()},
        'weights': {k: round(counts[k] / alloc[k], 4) for k in KINDS},
        'frame_kind_x_year': tab(elig, 'kind', 'year_bucket', KINDS, YEARS),
        'sample_kind_x_year': tab(sample, 'source_kind', 'year_bucket', KINDS, YEARS),
        'frame_kind_x_mentions': tab(elig, 'kind', 'mention_bucket', KINDS, MENTIONS),
        'sample_kind_x_mentions': tab(sample, 'source_kind', 'mention_bucket', KINDS, MENTIONS),
        'sample_kind_x_band': tab(sample, 'source_kind', 'band', KINDS, BANDS),
        'sample_licence': dict(collections.Counter(r['license_class'] for r in sample)),
        'sample_usability': dict(collections.Counter(r['usability'] for r in sample)),
        'sample_sha256': sha256_file(out / 'sample.jsonl'),
    }
    (out / 'sampling.json').write_text(json.dumps(info, indent=1) + '\n')
    print(json.dumps({k: info[k] for k in ('eligible', 'excluded', 'kind_counts', 'allocation', 'weights')}))


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    s = p.add_subparsers(dest='cmd', required=True)
    f = s.add_parser('frame')
    f.add_argument('store')
    f.add_argument('corpus')
    f.add_argument('out')
    sc = s.add_parser('scan')
    sc.add_argument('store')
    sc.add_argument('out')
    sc.add_argument('--procs', type=int, default=2)
    sc.add_argument('--only', help='a jsonl with doc_id rows: scan only these (a re-check)')
    sc.add_argument('--dst', help='output file (default OUT/mentions.jsonl)')
    se = s.add_parser('select')
    se.add_argument('out')
    se.add_argument('evals')
    se.add_argument('--n', type=int, default=N)
    a = p.parse_args()
    {'frame': cmd_frame, 'scan': cmd_scan, 'select': cmd_select}[a.cmd](a)


if __name__ == '__main__':
    main()
