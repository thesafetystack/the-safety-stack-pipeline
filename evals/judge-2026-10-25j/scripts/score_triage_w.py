"""Weighted scoring of the triage_w supplement (and its pooling with the proportional triage set). No LLM call.

    score_triage_w.py [--judges opus,sol,sonnet] [--out DIR]

Reads ``triage_w/`` and ``triage/`` (items, production, sampling.json) and the validated judge labels
``EVAL104_PRIVATE/validated/<judge>/{triage_w,triage}.jsonl`` (``triage_w.py validate`` / ``validate.py``; status
valid / repaired / over_length used). Writes ``<out>/triage_w.md`` and ``<out>/triage_w.json`` (default
``EVAL104_PRIVATE/scores``; the json carries ``disagreements`` for ``release.release_set``).

**Estimator.** Production is the prediction; references are each judge, the consensus (Opus and Sol give the same
verdict) and, with three judges, the majority (a verdict at least 2 of the 3 give; a judge without a label casts no
vote; 1-1-1 = no label). Per cell (sample, stratum h) the scored items S_h are those with a reference label; the
estimated population confusion is T[a][b] = sum_h N_h / |S_h| * #{i in S_h: pred a, ref b} (a reference that drops
items, the consensus above all, is re-weighted within the stratum: this assumes the dropped items look like the kept
ones, which for disagreement items is optimistic; the dropped count is reported). From T: fetch (relevant|borderline
vs not_relevant) and per-class precision / recall / F1, weighted agreement, and the estimated missed documents
T[not_relevant][relevant] and T[not_relevant][relevant|borderline]. 95 % intervals: percentile bootstrap, 2,000
resamples of documents with replacement within each cell (all n_h items, so the reference's dropout varies too),
seed 20261026 (``score.BOOT_SEED``).

**Designs.** ``triage_w``: the 5 strata of ``triage_w.py`` with N_h = the eligible frame. ``pooled``: triage_w plus
the 100 jtri items, post-stratified into the same 5 strata by their production verdict and confidence; N_h = the
triage set's eligible frame by those strata (``pooled_frame_by_stratum``); per stratum the estimate is N_h x the
pooled mean, i.e. the sample-size-weighted composite of the two independent estimates (each is unbiased for the
stratum, the jtri one by post-stratification; the triage_w frame lacks the 151 family documents, a 0.1 % difference
in frames). ``triage``: the proportional set alone, its own 6 design strata (basis x verdict), for comparison.

The estimator and the scoring body live in ``stack.evals.release_quality`` (``triage_w_scores``; moved there
2026-10-27, unchanged) so that a dataset release re-scores its own build with the same code (D728: strata, N_h and
weights recomputed on that build's population); this script loads the validated labels and writes the reports.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C
import triage_w as TW

from stack.evals import release_quality as M

BOOT_N, BOOT_SEED = M.BOOT_N, M.BOOT_SEED
CLASSES = M.TRIAGE_CLASSES
KEEP = M.KEEP
PAIR = M.PAIR
JUDGES: tuple[str, ...] = M.JUDGES


def load(set_name: str) -> tuple[list[dict], dict[str, dict[str, str | None]], dict[str, dict[str, int]]]:
    """Items (with ``pred`` and ``conf``), labels[source][item] (production, judges, consensus, majority), status."""
    items = C.read_jsonl(C.SET_ROOT / set_name / "items.jsonl")
    prod = {r["item_id"]: r["label"] for r in C.read_jsonl(C.SET_ROOT / set_name / "production.jsonl")}
    status, verdicts = {}, {}
    for j in JUDGES:
        p = C.private_root() / "validated" / j / f"{set_name}.jsonl"
        if not p.exists():
            raise SystemExit(f"validated/{j}/{set_name}.jsonl missing: validate first")
        rows = {r["item_id"]: r for r in C.read_jsonl(p) if r["call"] == "triage"}
        status[j] = dict(sorted(collections.Counter(r["status"] for r in rows.values()).items()))
        verdicts[j] = {it["item_id"]: (rows[it["item_id"]]["label"] or {}).get("verdict")
                       if it["item_id"] in rows and rows[it["item_id"]]["status"] in M.USABLE else None for it in items}
    lab = M.verdict_labels(items, {i: prod[i]["verdict"] for i in prod}, verdicts, JUDGES)
    lab["production"] = {i: prod[i]["verdict"] for i in prod}
    for it in items:
        it["pred"] = prod[it["item_id"]]["verdict"]
        it["conf"] = prod[it["item_id"]]["confidence"]
    return items, lab, status


def refs() -> list[str]:
    return [*JUDGES, "consensus", *(["majority"] if len(JUDGES) == 3 else [])]


# ── estimator (stack.evals.release_quality, shared with the release's re-scoring) ─────────────────

Cell = M.Cell
totals, metrics, estimate, cells_for, wilson = M.totals, M.tw_metrics, M.estimate, M.cells_for, M.wilson
pooled_estimate = M.pooled_estimate


# ── report ────────────────────────────────────────────────────────────────────────────────────


def f(x: Any, d: int = 3) -> str:
    return "-" if x is None else f"{x:.{d}f}" if isinstance(x, float) else str(x)


def fci(e: dict, key: str, d: int = 3) -> str:
    p, ci = e["point"].get(key), e["ci95"].get(key)
    if p is None:
        return "-"
    s = f"{p:,.0f}" if d == 0 else f"{p:.{d}f}"
    if ci:
        s += f" [{ci[0]:,.0f}, {ci[1]:,.0f}]" if d == 0 else f" [{ci[0]:.{d}f}, {ci[1]:.{d}f}]"
    return s


def main() -> int:
    global JUDGES
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--judges", default="opus,sol,sonnet")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    JUDGES = tuple(a.judges.split(","))
    out = Path(a.out) if a.out else C.private_root() / "scores"
    out.mkdir(parents=True, exist_ok=True)
    smp_w = json.loads((C.SET_ROOT / "triage_w" / "sampling.json").read_text("utf-8"))
    smp_t = json.loads((C.SET_ROOT / "triage" / "sampling.json").read_text("utf-8"))
    iw, lw, st_w = load("triage_w")
    it_, lt, st_t = load("triage")
    for it in it_:
        it["w_stratum"] = TW.stratum_of(it["pred"], it["conf"])
    for it in iw:
        if TW.stratum_of(it["pred"], it["conf"]) != it["stratum"]:
            raise SystemExit(f"{it['item_id']}: stratum does not match production verdict / confidence")
    lab = {s: {**lw[s], **lt[s]} for s in lw}  # item ids are disjoint (jtrw / jtri)
    t_strata = [h for h in smp_t["eligible_by_stratum"] if smp_t["allocation"].get(h)]
    res = M.triage_w_scores(
        iw, it_, lab,
        {"eligible_by_stratum": smp_w["eligible_by_stratum"], "pooled_frame_by_stratum": smp_w["pooled_frame_by_stratum"],
         "weights": smp_w["weights"], "n_h": TW.ALLOC, "cut_points": smp_w["confidence_cut_points"]},
        {"eligible_by_stratum": {h: smp_t["eligible_by_stratum"][h] for h in t_strata}}, JUDGES)
    res = {"judges": res.pop("judges"), "status": {"triage_w": st_w, "triage": st_t}, **res}
    main_ref = "majority" if "majority" in refs() else "consensus"
    (out / "triage_w.json").write_text(C.dumps(res), "utf-8")
    (out / "triage_w.md").write_text(report(res, main_ref), "utf-8")
    print((out / "triage_w.md").read_text("utf-8"))
    return 0


def report(res: dict, main_ref: str) -> str:
    L = ["# triage_w: weighted triage supplement, scores", "",
         "Production (luna) = prediction; references = each judge, consensus (Opus = Sol), majority (>= 2 of 3). "
         "Weighted population estimates (design weights N_h / n_h, re-weighted within stratum over the items the "
         "reference labels); 95 % stratified document-bootstrap intervals (2,000 resamples, seed 20261026). Fetch = "
         "relevant|borderline vs not_relevant. Missed = estimated documents in production's not_relevant population "
         "that the reference labels relevant (or relevant|borderline). Method: `scripts/score_triage_w.py` docstring.",
         "", "## Design", "", "| stratum | N_h (eligible) | n_h | weight | jtri items in it | pooled N_h |",
         "|---|---|---|---|---|---|"]
    for h, d in res["design"]["strata"].items():
        L.append(f"| {h} | {d['N_h']:,} | {d['n_h']} | {d['weight']:.1f} | {res['per_stratum_raw'][h]['jtri_items']} "
                 f"| {d['pooled_N_h']:,} |")
    L += ["", f"Cut points (production confidence of not_relevant): <= {res['design']['cut_points']['low_max']}, "
          f"<= {res['design']['cut_points']['mid_max']}, rest (0.99).", "",
          "Validation status: " + "; ".join(f"{s} {j}: {c}" for s, v in res["status"].items() for j, c in v.items()),
          ""]
    for dname, title in (("triage_w", "triage_w alone (200 items)"),
                         ("pooled", "pooled: triage_w + the 100 proportional jtri items, post-stratified"),
                         ("triage", "proportional triage set alone (100 items, its own strata), for comparison")):
        L += [f"## {title}", "", "| reference | scored / items | fetch P | fetch R | fetch F1 | missed relevant | "
              "missed rel.+border. | agreement |", "|---|---|---|---|---|---|---|---|"]
        for ref, e in res["estimates"][dname].items():
            L.append(f"| {ref} | {e['n_scored']} / {e['n_items']} | {fci(e, 'fetch_precision')} | "
                     f"{fci(e, 'fetch_recall')} | {fci(e, 'fetch_f1')} | {fci(e, 'missed_relevant', 0)} | "
                     f"{fci(e, 'missed_keep', 0)} | {fci(e, 'agreement')} |")
        L += ["", "Per class (P / R / F1 with intervals):", "", "| reference | class | precision | recall | F1 |",
              "|---|---|---|---|---|"]
        for ref, e in res["estimates"][dname].items():
            if ref not in ("consensus", "majority"):
                continue
            for c in CLASSES:
                L.append(f"| {ref} | {c} | {fci(e, c + '_precision')} | {fci(e, c + '_recall')} | {fci(e, c + '_f1')} |")
        e = res["estimates"][dname][main_ref]
        L += ["", f"Weighted confusion vs {main_ref} (production->reference, estimated documents): "
              + ", ".join(f"{k} {v:,.0f}" for k, v in e["confusion_weighted"].items()), ""]
    L += ["## Raw per-stratum counts (triage_w; reference label -> items)", "",
          "| stratum | production | reference | counts |", "|---|---|---|---|"]
    for h, r in res["per_stratum_raw"].items():
        for ref in [k for k in r if k not in ("N_h", "n_h", "production", "jtri_items", "jtri")]:
            L.append(f"| {h} | {r['production']} | {ref} | "
                     + ", ".join(f"{k} {v}" for k, v in sorted(r[ref].items())) + " |")
    L += ["", "jtri items by the same strata (reference = " + main_ref + "): "
          + "; ".join(f"{h}: " + ", ".join(f"{k} {v}" for k, v in sorted(r["jtri"][main_ref].items()))
                      for h, r in res["per_stratum_raw"].items() if r["jtri"][main_ref]), "",
          "## Missed relevant|borderline per not_relevant stratum (Wilson 95 % per stratum, scaled to N_h)", "",
          "| reference | sample | stratum | keep / scored | est. missed [Wilson 95 %] |", "|---|---|---|---|---|",
          *[f"| {k.split('|')[0]} | {k.split('|')[1]} | {'|'.join(k.split('|')[2:])} | {v['keep']} / {v['scored']} | "
            f"{f(v['missed_keep'], 0) if v['missed_keep'] is not None else '-'} "
            f"[{v['missed_keep_wilson95'][0]:,.0f}, {v['missed_keep_wilson95'][1]:,.0f}] |"
            for k, v in res["missed_by_stratum"].items()], "",
          "## Judge agreement on triage_w", "", "| pair | n | raw agreement | weighted agreement |", "|---|---|---|---|"]
    for k, v in res["judge_pairs_triage_w"].items():
        ci = v["weighted_agreement_ci95"]
        L.append(f"| {k} | {v['n']} | {f(v['raw_agreement'])} | {f(v['weighted_agreement'])}"
                 + (f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "") + " |")
    L += ["", f"## Production not_relevant, {main_ref} relevant|borderline ({len(res['false_negative_items'])} items)",
          "", "| item | stratum | conf | opus / sol / sonnet | title |", "|---|---|---|---|---|"]
    for r in res["false_negative_items"]:
        L.append(f"| {r['item_id']} | {r['stratum']} | {r['production_confidence']} | "
                 + " / ".join(str(r.get(j)) for j in res["judges"]) + f" | {r['title']} |")
    L += ["", f"Disagreement rows (triage_w, production and judges not all equal): {len(res['disagreements'])} "
          "(disagreements.jsonl).", ""]
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
