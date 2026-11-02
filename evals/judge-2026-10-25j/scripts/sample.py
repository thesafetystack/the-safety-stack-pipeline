"""Sampling of the judge eval sets 2026-10-25j: one independent seeded sample of N documents per set (DESIGN.md).

    sample.py [--sets triage,screen,...] [--n 100]

Read only (EVAL104_DATA, EVAL104_BLOBS_GLOB; see common.py). Writes per set ``<set>/sample.jsonl`` and
``<set>/sampling.json`` in this directory, and the same two files under ``EVAL104_PRIVATE/sampling/<set>/``.

**Frames** (build ws1qpn8myzxah):

* triage: ``documents`` head rows in state ``acquired`` whose triage ``safety_relevance`` question has an LLM
  verdict (status ok, ``triage_evidence`` row present); rule decisions, ``skipped`` / no-basis rows and
  ``retired`` (merged) rows are out. Strata: evidence ``basis`` (fields / title) x production verdict.
* screen: corpus rows whose ``screen`` annotation is an ``ai_check`` ``ok`` (27 human overrides and 3
  quarantined rows out). Strata: ``doc_class.doc_type``.
* affiliation: corpus rows whose ``affiliation`` annotation is an ``ai_check`` ``ok`` (the XML tier's
  ``computed`` rows, quarantined and unannotated rows out). Strata: ``doc_type``.
* model_roles / technique_staged: corpus rows with the luna annotation (``ok`` or ``empty``, origin ``ai_check``,
  an evidence id), minus the earlier Claude stand-ins (override rows of that target). Strata: ``doc_type``.

Every corpus frame needs a current text (status ok). Screen: no blob condition (2026-10-25, coordinator: the 2,129
screen documents whose blob is not in the local mirror were a bias); a sampled item's missing blob is fetched read-only
by ``fetch_blobs.py``. The other corpus sets still exclude a document whose blob is not in the mirror (counted).

**Exclusion**: every document of an existing eval set (``evals/*/*/items*.jsonl`` outside this set family; gold-v3's
100 included): same doc_id, a former or same-raw doc_id, the same current text sha256, or the same normalised title
of more than 15 characters (:func:`common.existing_eval_keys`).

**Design**: proportional allocation over the strata (largest remainder, ties by stratum name; a small stratum can
get 0: the sample is self-weighting, weight N/n for everyone); within a stratum systematic sampling with a seeded
start over the units sorted by (implicit stratification, unit key): triage by (anchor channel, year bucket),
corpus sets by (year bucket, anchor channel). Unit key = sha256("<seed>:<set>:<doc_id>"), start from
sha256("<seed>:<set>:<stratum>"): no use of Python's hash or global random state, so the result is stable across
hash seeds and processes. Item ids follow the unit key order of the picked documents (they carry no stratum).
"""
from __future__ import annotations

import argparse
import collections
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

SEED = 20261025
N = 100
YEARS = ("<=2019", "2020-2023", "2024", "2025", "2026", "none")


def ukey(set_name: str, x: str) -> str:
    return C.sha256_text(f"{SEED}:{set_name}:{x}")


def unit_float(set_name: str, x: str) -> float:
    return int(ukey(set_name, x), 16) / float(1 << 256)


def allocate(counts: dict[str, int], n: int) -> tuple[dict[str, int], dict[str, float]]:
    """Largest-remainder proportional allocation (ties: stratum name)."""
    tot = sum(counts.values())
    quota = {k: n * c / tot for k, c in counts.items()}
    alloc = {k: math.floor(q) for k, q in quota.items()}
    left = n - sum(alloc.values())
    for k in sorted(quota, key=lambda k: (-(quota[k] - alloc[k]), k))[:left]:
        alloc[k] += 1
    return alloc, quota


ANCHOR = ("list_filter(identity.members, lambda m: m.source_id = identity.anchor_source_id)[1]")


def _rows(con: Any, q: str) -> list[dict[str, Any]]:
    cur = con.execute(q)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def frame_triage(con: Any) -> list[dict[str, Any]]:
    d, e = C.data_file("documents"), C.data_file("triage_evidence")
    # materialised first: the same query as one CTE join ran > 5 minutes (duckdb plan), as two tables < 1 s
    con.execute(f"""create or replace temp table tri_docs as
      select doc_id, state, metadata.title as title, year(metadata.published_at) as year,
        identity.former_doc_ids as former_doc_ids, content.same_raw_doc_ids as same_raw_doc_ids,
        content.current_text_sha256 as text_sha256, {ANCHOR}.channel as channel,
        {ANCHOR}.license_class as license_class,
        list_filter(annotations.triage.payload.questions, lambda x: x.question = 'safety_relevance')[1] as q
      from read_parquet('{d}')""")
    con.execute(f"""create or replace temp table tri_ev as
      select evidence_id, basis, prompt_name, status from read_parquet('{e}')""")
    return _rows(con, """
    select t.doc_id, t.state, t.title, t.year, t.former_doc_ids, t.same_raw_doc_ids, t.text_sha256, t.channel,
      t.license_class, t.q.verdict as verdict, t.q.status as q_status, t.q.evidence_id as evidence_id,
      e.basis as basis, e.prompt_name as prompt_name, e.status as ev_status
    from tri_docs t left join tri_ev e on e.evidence_id = t.q.evidence_id
    where t.q.verdict is not null
    order by t.doc_id""")


def frame_corpus(con: Any) -> list[dict[str, Any]]:
    c, o = C.data_file("corpus"), C.data_file("overrides")
    q = f"""
    select doc_id, metadata.title as title, year(metadata.published_at) as year, metadata.doc_type as meta_doc_type,
      identity.former_doc_ids as former_doc_ids, content.same_raw_doc_ids as same_raw_doc_ids,
      content.current_text_sha256 as text_sha256, content.current_text_status as text_status,
      content.current_role as basis_role, {ANCHOR}.channel as channel,
      coalesce(list_filter(identity.members, lambda m: m.source_id = content.current_source_id)[1].license_class,
               {ANCHOR}.license_class) as license_class,
      annotations.doc_class.payload.doc_type as doc_type,
      annotations.screen.status as screen_status, annotations.screen.origin as screen_origin,
      annotations.screen.payload.verdict as screen_verdict,
      annotations.affiliation.status as aff_status, annotations.affiliation.origin as aff_origin,
      annotations.model_roles.status as mr_status, annotations.model_roles.origin as mr_origin,
      annotations.model_roles.evidence_id as mr_evidence,
      annotations.technique_staged.status as ts_status, annotations.technique_staged.origin as ts_origin,
      annotations.technique_staged.evidence_id as ts_evidence,
      annotations.text_usability.payload.verdict as usability
    from read_parquet('{c}') order by doc_id"""
    rows = _rows(con, q)
    standins = collections.defaultdict(set)
    for target, key in con.execute(f"select target, key from read_parquet('{o}') where target in "
                                   f"('model_roles','technique_staged') and kind = 'row'").fetchall():
        standins[target].add(key)
    for r in rows:
        r["mr_standin"] = r["doc_id"] in standins["model_roles"]
        r["ts_standin"] = r["doc_id"] in standins["technique_staged"]
    return rows


def in_population(set_name: str, r: dict[str, Any]) -> str | None:
    """None when ``r`` is in the set's population, else the reason it is not (counted)."""
    if set_name == "triage":
        if r["state"] != "acquired":
            return f"state_{r['state']}"
        if r["q_status"] != "ok" or r["ev_status"] != "ok" or r["basis"] is None:
            return "no_llm_evidence"
        return None
    if set_name == "screen":
        if r["screen_status"] != "ok":
            return f"screen_{r['screen_status']}"
        return None if r["screen_origin"] == "ai_check" else f"screen_origin_{r['screen_origin']}"
    if set_name == "affiliation":
        if r["aff_status"] is None:
            return "no_affiliation"
        if r["aff_status"] != "ok":
            return f"affiliation_{r['aff_status']}"
        return None if r["aff_origin"] == "ai_check" else f"affiliation_origin_{r['aff_origin']}"
    p = "mr" if set_name == "model_roles" else "ts"
    if r[f"{p}_status"] is None:
        return "not_annotated"
    if r[f"{p}_standin"]:
        return "claude_standin"
    if r[f"{p}_status"] not in ("ok", "empty") or r[f"{p}_origin"] != "ai_check" or not r[f"{p}_evidence"]:
        return f"{p}_{r[f'{p}_status']}_{r[f'{p}_origin']}"
    return None


def stratum(set_name: str, r: dict[str, Any]) -> str:
    if set_name == "triage":
        return f"{r['basis']}|{r['verdict']}"
    return r["doc_type"] or "none"


def sort_key(set_name: str, r: dict[str, Any]) -> tuple[Any, ...]:
    yb = YEARS.index(C.year_bucket(r["year"]))
    if set_name == "triage":
        return (r["channel"] or "", yb, ukey(set_name, r["doc_id"]))
    return (yb, r["channel"] or "", ukey(set_name, r["doc_id"]))


def select(set_name: str, rows: list[dict[str, Any]], keys: tuple[set, set, set, list], blobs: C.Blobs | None,
           n: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ids, digs, titles, files = keys
    pop_out: collections.Counter[str] = collections.Counter()
    drop: collections.Counter[str] = collections.Counter()
    pop, elig = [], []
    for r in rows:
        why = in_population(set_name, r)
        if why:
            pop_out[why] += 1
            continue
        pop.append(r)
        if set_name == "screen" and not (r["text_status"] == "ok" and r["text_sha256"]):
            drop["no_current_text"] += 1  # screen: a missing local blob is fetched for the sampled items (fetch_blobs.py)
            continue
        if set_name not in ("triage", "screen") and not (r["text_status"] == "ok" and blobs is not None
                                                        and blobs.exists(r["text_sha256"])):
            drop["no_text_blob"] += 1
            continue
        k = {r["doc_id"], *(r["former_doc_ids"] or []), *(r["same_raw_doc_ids"] or [])}
        t = C.norm_title(r["title"])
        if k & ids:
            drop["in_existing_eval:doc_id"] += 1
            continue
        if r["text_sha256"] and r["text_sha256"] in digs:
            drop["in_existing_eval:text_sha256"] += 1
            continue
        if len(t) > 15 and t in titles:
            drop["in_existing_eval:title"] += 1
            continue
        elig.append(r)
    by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in elig:
        by[stratum(set_name, r)].append(r)
    counts = {s: len(by[s]) for s in sorted(by)}
    pop_counts = dict(sorted(collections.Counter(stratum(set_name, r) for r in pop).items()))
    alloc, quota = allocate(counts, n)
    picked, starts = [], {}
    for s in sorted(by):
        units = sorted(by[s], key=lambda r: sort_key(set_name, r))
        nk, nn = alloc[s], len(units)
        if nk == 0:
            continue
        step = nn / nk
        u = unit_float(set_name, f"start:{s}") * step
        starts[s] = round(u, 6)
        for j in range(nk):
            r = units[math.floor(u + j * step)]
            picked.append(dict(r, _stratum=s, _weight=nn / nk, _p=nk / nn))
    picked.sort(key=lambda r: ukey(set_name, r["doc_id"]))
    sample = []
    for i, r in enumerate(picked, 1):
        row = {"item_id": f"{C.PREFIX[set_name]}-{i:04d}", "doc_id": r["doc_id"], "stratum": r["_stratum"],
               "weight": round(r["_weight"], 6), "inclusion_prob": round(r["_p"], 8),
               "year": r["year"], "year_bucket": C.year_bucket(r["year"]), "channel": r["channel"],
               "license_class": r["license_class"], "text_sha256": r["text_sha256"]}
        if set_name == "triage":
            row.update(basis=r["basis"], verdict=r["verdict"], evidence_id=r["evidence_id"],
                       prompt_name=r["prompt_name"])
        else:
            row.update(doc_type=r["doc_type"], meta_doc_type=r["meta_doc_type"], basis_role=r["basis_role"],
                       usability=r["usability"])
        sample.append(row)
    info = {"set": set_name, "seed": SEED, "n": n, "frame_rows": len(rows), "population": len(pop),
            "not_in_population": dict(sorted(pop_out.items())), "excluded": dict(sorted(drop.items())),
            "eligible": len(elig), "population_by_stratum": pop_counts, "eligible_by_stratum": counts,
            "quota_unrounded": {k: round(v, 4) for k, v in quota.items()}, "allocation": alloc,
            "weights": {s: round(counts[s] / alloc[s], 4) for s in counts if alloc[s]},
            "systematic_start": starts, "existing_eval_files": files,
            "design": ("proportional allocation (largest remainder) over strata; systematic sampling within stratum "
                       "over units sorted by " + ("(anchor channel, year bucket, unit key)" if set_name == "triage"
                                                  else "(year bucket, anchor channel, unit key)"))}
    return sample, info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sets", default=",".join(C.SETS))
    ap.add_argument("--n", type=int, default=N)
    a = ap.parse_args()
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'; SET threads=2")
    keys = C.existing_eval_keys(C.REPO / "evals")
    blobs = C.Blobs()
    corpus = None
    inputs = {}
    for set_name in a.sets.split(","):
        if set_name == "triage":
            rows = frame_triage(con)
            inputs = {k: C.sha256_file(C.data_file(k)) for k in ("documents", "triage_evidence")}
        else:
            corpus = corpus if corpus is not None else frame_corpus(con)
            rows = corpus
            inputs = {k: C.sha256_file(C.data_file(k)) for k in ("corpus", "overrides")}
        sample, info = select(set_name, rows, keys, blobs, a.n)
        info["inputs"] = {k: {"file": C.DATA_FILES[k], "sha256": v} for k, v in inputs.items()}
        for out in (C.SET_ROOT / set_name, C.private_root() / "sampling" / set_name):
            info["sample_sha256"] = C.write_jsonl(out / "sample.jsonl", sample)
            (out / "sampling.json").write_text(C.dumps(info), "utf-8")
        print(set_name, {k: info[k] for k in ("population", "excluded", "eligible", "allocation")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
