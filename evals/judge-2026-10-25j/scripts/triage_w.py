"""triage_w: the positive-weighted triage supplement of the judge eval sets 2026-10-25j (sample, freeze, run,
validate, release). Scoring is ``score_triage_w.py``.

    triage_w.py sample                     # writes triage_w/sample.jsonl, triage_w/sampling.json (+ private copy)
    triage_w.py freeze [--check-tasks]     # task dirs EVAL104_PRIVATE/tasks/triage_w/, items / production / manifest
    triage_w.py run [run_judges.py args]   # run_judges.main() on set triage_w (same jail, commands, prompt, guides)
    triage_w.py validate [--judges ...]    # validate.validate_one per item -> EVAL104_PRIVATE/validated/<j>/triage_w.jsonl
    triage_w.py release [--judges ...]     # release.release_set("triage_w"): labels-<judge>.jsonl, scores, disagreements

Why: the proportional triage set (``triage/``, 100 items) has ~9 production positives, too few for precision / recall
on the positive classes, and triage's costly error is a false negative (a relevant document never fetched). This set
oversamples production's positives and its low-confidence ``not_relevant`` rows, with design weights w = N_h / n_h.

**Frame** = the triage frame of ``sample.py`` (``documents`` rows in state acquired with an LLM triage verdict) minus
(a) every existing eval set's documents (``common.existing_eval_keys``, gold-v3 included: the triage set's exclusion)
and (b) every document of this set family (``<set>/items.jsonl`` / ``sample.jsonl`` of all five sets, the 100 jtri
items among them): same doc id, former / same-raw id, current text sha256, normalised title > 15 characters.

**Strata** (production verdict; ``not_relevant`` split by production confidence, cut points chosen on the population's
confidence distribution, which is discrete in steps of 0.01): ``relevant`` 30, ``borderline`` 30,
``not_relevant|c<=0.94`` 70 (~13 % of not_relevant), ``not_relevant|c0.95-0.98`` 40 (~37 %), ``not_relevant|c0.99``
30 (~50 %). Within a stratum: systematic sampling with a seeded start over the units sorted by (evidence basis
fields / title, anchor channel, year bucket, unit key), so basis is stratified implicitly. Seed scheme = ``sample.py``'s
(unit key sha256("20261025:triage_w:<doc_id>"), start from sha256("20261025:triage_w:start:<stratum>")). Item ids
``jtrw-NNNN`` in unit-key order.

The other modules are imported, never edited: this file registers ``triage_w`` in ``common.CALLS`` / ``PREFIX`` at
import time (process-local) and redirects ``freeze.freeze_triage``'s task-dir writes to the ``triage_w`` set name.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import sample as S

from stack.evals import release_quality as M

SET = "triage_w"
C.CALLS[SET] = ("triage",)
C.PREFIX[SET] = "jtrw"
#: n_h per stratum (the brief: relevant 30, borderline 30, not_relevant 140 as ~70 / 40 / 30 low / mid / high) and
#: the not_relevant confidence cut points (inclusive upper bounds of the lower two sub-strata); the strata, the frame
#: exclusions and the scoring live in ``stack.evals.release_quality`` (shared with the release's re-scoring, D728).
ALLOC = M.TW_ALLOC
CUTS = M.TW_CUTS
stratum_of = M.stratum_of


def family_keys() -> tuple[set[str], set[str], set[str]]:
    """Doc ids, text sha256s and normalised titles (> 15 chars) of every item of the five 2026-10-25j sets."""
    return M.family_keys(C.SET_ROOT)


def excluded_by(r: dict[str, Any], ids: set[str], digs: set[str], titles: set[str]) -> str | None:
    return M.excluded_by(r, (ids, digs, titles))


def cmd_sample(_: argparse.Namespace) -> int:
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'; SET threads=2")
    rows = S.frame_triage(con)
    conf = dict(con.execute(f"select evidence_id, confidence from read_parquet('{C.data_file('triage_evidence')}')")
                .fetchall())
    ev_ids, ev_dig, ev_tit, ev_files = C.existing_eval_keys(C.REPO / "evals")
    f_ids, f_dig, f_tit = family_keys()
    pop_out: collections.Counter[str] = collections.Counter()
    drop: collections.Counter[str] = collections.Counter()
    pop, elig = [], []
    pooled_frame: collections.Counter[str] = collections.Counter()  # the triage set's eligible frame, by these strata
    for r in rows:
        why = S.in_population("triage", r)
        if why:
            pop_out[why] += 1
            continue
        r["confidence"] = conf[r["evidence_id"]]
        r["_stratum"] = stratum_of(r["verdict"], r["confidence"])
        pop.append(r)
        x = excluded_by(r, ev_ids, ev_dig, ev_tit)
        if x:
            drop[f"in_existing_eval:{x}"] += 1
            continue
        pooled_frame[r["_stratum"]] += 1
        x = excluded_by(r, f_ids, f_dig, f_tit)
        if x:
            drop[f"in_2026-10-25j_set:{x}"] += 1
            continue
        elig.append(r)
    by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in elig:
        by[r["_stratum"]].append(r)
    if set(by) != set(ALLOC):
        raise SystemExit(f"strata {sorted(by)} != {sorted(ALLOC)}")
    picked, starts = [], {}
    for s in ALLOC:
        units = sorted(by[s], key=lambda r: (r["basis"], *S.sort_key(SET, r)))
        nk, nn = ALLOC[s], len(units)
        step = nn / nk
        u = S.unit_float(SET, f"start:{s}") * step
        starts[s] = round(u, 6)
        for j in range(nk):
            picked.append(dict(units[math.floor(u + j * step)], _weight=nn / nk, _p=nk / nn))
    if len({r["doc_id"] for r in picked}) != len(picked):
        raise SystemExit("a document was picked twice")
    picked.sort(key=lambda r: S.ukey(SET, r["doc_id"]))
    sample = []
    for i, r in enumerate(picked, 1):
        sample.append({"item_id": f"{C.PREFIX[SET]}-{i:04d}", "doc_id": r["doc_id"], "stratum": r["_stratum"],
                       "weight": round(r["_weight"], 6), "inclusion_prob": round(r["_p"], 8), "year": r["year"],
                       "year_bucket": C.year_bucket(r["year"]), "channel": r["channel"],
                       "license_class": r["license_class"], "text_sha256": r["text_sha256"], "basis": r["basis"],
                       "verdict": r["verdict"], "confidence": r["confidence"], "evidence_id": r["evidence_id"],
                       "prompt_name": r["prompt_name"]})
    pc = collections.Counter(r["_stratum"] for r in pop)
    nr_conf = collections.Counter(round(r["confidence"], 2) for r in pop if r["verdict"] == "not_relevant")
    cum, tot, dist = 0, sum(nr_conf.values()), {}
    for c in sorted(nr_conf):
        cum += nr_conf[c]
        dist[f"{c:.2f}"] = {"n": nr_conf[c], "cum_share": round(cum / tot, 4)}
    eb = {s: len(by[s]) for s in ALLOC}
    info = {"set": SET, "seed": S.SEED, "n": len(sample), "frame_rows": len(rows), "population": len(pop),
            "population_by_verdict": dict(sorted(collections.Counter(r["verdict"] for r in pop).items())),
            "not_in_population": dict(sorted(pop_out.items())), "excluded": dict(sorted(drop.items())),
            "eligible": len(elig), "population_by_stratum": {s: pc[s] for s in ALLOC}, "eligible_by_stratum": eb,
            "pooled_frame_by_stratum": {s: pooled_frame[s] for s in ALLOC},
            "allocation": ALLOC, "weights": {s: round(eb[s] / ALLOC[s], 4) for s in ALLOC},
            "systematic_start": starts, "confidence_cut_points": {"low_max": CUTS[0], "mid_max": CUTS[1]},
            "not_relevant_confidence_distribution": dist,
            "sample_by_stratum_and_basis": {f"{s}|{b}": n for (s, b), n in sorted(collections.Counter(
                (r["stratum"], r["basis"]) for r in sample).items())},
            "existing_eval_files": ev_files,
            "family_sets_excluded": [f"evals/{C.SET_ROOT.name}/{s}" for s in C.SETS],
            "design": ("disproportionate stratified sampling: strata = production verdict, not_relevant split by "
                       "production confidence (<= 0.94 / 0.95-0.98 / 0.99); fixed allocation; systematic sampling "
                       "within stratum over units sorted by (basis, anchor channel, year bucket, unit key); design "
                       "weight N_h / n_h over the eligible frame"),
            "inputs": {k: {"file": C.DATA_FILES[k], "sha256": C.sha256_file(C.data_file(k))}
                       for k in ("documents", "triage_evidence")}}
    for out in (C.SET_ROOT / SET, C.private_root() / "sampling" / SET):
        info["sample_sha256"] = C.write_jsonl(out / "sample.jsonl", sample)
        (out / "sampling.json").write_text(C.dumps(info), "utf-8")
    print(json.dumps({k: info[k] for k in ("population", "population_by_verdict", "excluded", "eligible",
                                           "population_by_stratum", "eligible_by_stratum", "weights",
                                           "sample_by_stratum_and_basis")}, indent=1))
    return 0


def cmd_freeze(a: argparse.Namespace) -> int:
    import duckdb
    import freeze as F
    import guides as G

    F.CHECK_ONLY = a.check_tasks
    orig = F.write_task
    F.write_task = lambda _set, item, call, files: orig(SET, item, call, files)  # tasks/triage_w/<item>/
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'; SET threads=2")
    sample = C.read_jsonl(C.SET_ROOT / SET / "sample.jsonl")
    items, prod, stats = F.freeze_triage(con, sample, C.Blobs())
    # same guide, schema and wrapper prompt bytes as the triage set (the released guides/ files)
    rel = C.SET_ROOT / "guides"
    want = {"fields": C.sha256_file(rel / "triage-fields.md"), "title": C.sha256_file(rel / "triage-title.md")}
    schema_sha = C.sha256_file(rel / "schema-triage.json")
    if (rel / "prompt-triage.txt").read_text("utf-8") != C.wrapper_prompt("triage") + "\n":
        raise SystemExit("wrapper prompt differs from guides/prompt-triage.txt")
    for it in items:
        tf = it["task_files"]["triage"]
        if tf["guide.md"] != want[it["basis"]] or tf["schema.json"] != schema_sha:
            raise SystemExit(f"{it['item_id']}: guide or schema differs from guides/ (the triage set's)")
    stats["guide_schema_prompt_identical_to_triage"] = len(items)
    by_id = {s["item_id"]: s for s in sample}
    for p in prod:
        s = by_id[p["item_id"]]
        if (p["label"]["verdict"], p["label"]["confidence"]) != (s["verdict"], s["confidence"]):
            raise SystemExit(f"{p['item_id']}: production label differs from the sampled verdict / confidence")
    prod = [F.cap_quotes(p) for p in prod]
    isha = C.write_jsonl(C.SET_ROOT / SET / "items.jsonl", items)
    psha = C.write_jsonl(C.SET_ROOT / SET / "production.jsonl", prod)
    gs = {n: C.sha256_text(t) for n, t in G.all_guides().items() if n.startswith("triage-")}
    manifest = {"set_id": C.SET_ID, "set": SET, "calls": ["triage"], "n_items": len(items),
                "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "build": "ws1qpn8myzxah",
                "data_files": {k: C.DATA_FILES[k] for k in ("documents", "triage_evidence")},
                "items_sha256": isha, "production_sha256": psha,
                "sample_sha256": C.sha256_file(C.SET_ROOT / SET / "sample.jsonl"), "verification": stats,
                "guides_sha256": gs | {"schema-triage.json": schema_sha,
                                       "prompt-triage.txt": C.sha256_file(rel / "prompt-triage.txt")},
                "prompts": {"triage": C.wrapper_prompt("triage")},
                "tasks": "EVAL104_PRIVATE/tasks/triage_w/<item_id>/ (private: third-party texts)"}
    if not a.check_tasks or (C.SET_ROOT / SET / "manifest.json").exists():
        (C.SET_ROOT / SET / "manifest.json").write_text(C.dumps(manifest), "utf-8")
    print(SET, len(items), stats)
    return 0


def cmd_run(rest: list[str]) -> int:
    import run_judges as R

    if any(x == "--sets" or x.startswith("--sets=") for x in rest):
        raise SystemExit("triage_w.py run sets --sets itself")
    sys.argv = ["run_judges.py", "--sets", SET, *rest]
    return R.main()


def cmd_validate(a: argparse.Namespace) -> int:
    import validate as V

    out = C.private_root() / "validated"
    items = [r["item_id"] for r in C.read_jsonl(C.SET_ROOT / SET / "items.jsonl")]
    summary = {}
    for judge in a.judges.split(","):
        rows = [V.validate_one(judge, SET, i, "triage") for i in items]
        C.write_jsonl(out / judge / f"{SET}.jsonl", rows)
        cnt = collections.Counter(r["status"] for r in rows)
        summary[f"{judge}/{SET}/triage"] = {**dict(cnt), "listed": [
            {"item_id": r["item_id"], "status": r["status"], "flags": r["flags"], "problems": r["problems"]}
            for r in rows if r["status"] != "valid" or r["problems"]]}
    (out / f"summary-{SET}.json").write_text(C.dumps(summary), "utf-8")
    for k, v in summary.items():
        print(k, {s: n for s, n in v.items() if s != "listed"}, *(json.dumps(x) for x in v["listed"]))
    return 0


def cmd_release(a: argparse.Namespace) -> int:
    import release as RL

    C.JUDGES = tuple(a.judges.split(","))  # release_set iterates common.JUDGES
    print(SET, json.dumps(RL.release_set(SET)))
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        return cmd_run(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sample")
    f = sub.add_parser("freeze")
    f.add_argument("--check-tasks", action="store_true")
    for name in ("validate", "release"):
        p = sub.add_parser(name)
        p.add_argument("--judges", default=",".join(C.JUDGES))
    sub.add_parser("run", help="run_judges.py arguments follow (--judges, --parallel-*, --limit, --dry-run ...)")
    a = ap.parse_args()
    return {"sample": cmd_sample, "freeze": cmd_freeze, "validate": cmd_validate, "release": cmd_release}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
