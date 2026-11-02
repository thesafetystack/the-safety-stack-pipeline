"""Score the judge eval sets 2026-10-25j: production against each judge, judge against judge, production against the
judges' consensus (DESIGN.md "Scoring"). No LLM call.

    score.py [--sets ...] [--out DIR] [--judges opus,sol,sonnet | opus,sol]
    score.py --write-model-roles-normalized    # model_roles/judge-normalized.jsonl, for the release's re-scoring

Reads ``<set>/items.jsonl`` and ``<set>/production.jsonl`` (here) and the judge records
``EVAL104_PRIVATE/runs/<judge>/<set>/*.json`` (``ok`` records only; a missing or failed record drops that item from
every comparison that needs it, and the counts say so). Writes ``<out>/<set>.md`` and ``<out>/<set>.json`` (default
``EVAL104_PRIVATE/scores``). The samples are self-weighting (proportional allocation), so no weights are applied.

Per judgement:

* categorical (triage verdict; screen S, R and overall verdicts, R category, usability verdict, doc_type,
  self_published, primary_lang): n, raw agreement, Cohen's kappa, the confusion table, per-class precision and
  recall of production with the judge (or the consensus) as reference; triage and screen also the binary
  keep / fetch view (relevant or borderline vs not). Consensus = the items where both judges give the same label.
  Screen splits production-vs-consensus agreement by luna's window (whole text seen or not). doc_type is scored only
  where it is a model judgement (no channel doc_type), with production's metadata paper/preprint rule applied to the
  judges' answers too.
* sets of units (affiliation (author, surface) and (author, org slug); usability issues; model_roles (node, role)
  cells at >= 2 / >= 1 and node presence; technique_staged slug presence and (slug, role) cells at >= 2 / >= 1, group
  nodes out: the gold-v3 units): micro precision / recall / F1 of production against each judge, Dice and Jaccard
  between judges, production's recall on the consensus (units both judges have) and precision against the union.
  model_roles judge answers go through production's own normalisation (``judge.normalize`` with the matcher menu,
  refinement and quote checks) so both sides are post-processed alike.
* disagreements: every item where production and the judges do not all agree, for a later adjudication pass.

Three judges (default since 2026-10-26: Opus, Sol, Sonnet): every pair of sources is compared
(``<a>_vs_<b>``); ``production_vs_consensus`` keeps its 2026-10-25 meaning (Opus and Sol agree; Sonnet ignored, the
item set unchanged) and ``production_vs_majority`` is added: categorical = the label at least 2 of the 3 judges give
(a judge without a label for the item, missing record or not scored, casts no vote; 1-1-1 or a single label = no
majority, counted in ``majority_no_label_n``); sets = the units at least 2 of the 3 judges have, over the items where
all three judges and production have a record, plus production's precision against the 3-judge union. Per-class
F1 is reported beside precision and recall. The screen's keep view (relevant or borderline vs irrelevant) also
carries ``per_class_ci95`` on production's comparisons with each reference (document bootstrap within the doc_type
strata, added 2026-10-27; no other number changed). ``--judges opus,sol`` reproduces the two-judge outputs of 2026-10-25
(same numbers; the only addition is ``f1`` in ``per_class``).

The metrics and the per-set unit and label readers live in ``stack.evals.release_quality`` (moved there
2026-10-27, unchanged, so that a dataset release re-scores its own build with the same code, D728); this script keeps
loading, the model_roles normalisation and the affiliation window split (both read the texts), the disagreement
lists and the reports. ``model_roles/judge-normalized.jsonl`` holds the judges' model_roles answers after production's
``judge.normalize`` (the release cannot read the texts): ``--write-model-roles-normalized`` writes it from the
released labels; every scoring run checks that its units equal the ones computed from the validated labels.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

from stack.evals import release_quality as M  # noqa: E402

PRODUCTION = "production"
Units = dict[str, set]
#: The judges scored (``--judges``): ("opus", "sol") = the 2026-10-25 two-judge outputs; all three = majority added.
JUDGES: tuple[str, ...] = C.JUDGES
PAIR = ("opus", "sol")  # the two-judge consensus (production_vs_consensus) in either mode


def three() -> bool:
    return len(JUDGES) == 3


def comparisons() -> list[tuple[str, str]]:
    """Every pair of sources, production first (two-judge mode: the 2026-10-25 three pairs, same order)."""
    import itertools

    return list(itertools.combinations((PRODUCTION, *JUDGES), 2))


# ── loading ───────────────────────────────────────────────────────────────────────────────────


def load(set_name: str) -> tuple[list[dict], dict[tuple[str, str], dict], dict[str, dict[tuple[str, str], dict]], dict]:
    """Items, production, the judges' validated labels (``validate.py``: status valid / repaired / over_length are
    used; invalid / missing are not) and the per-judge status counts."""
    items = C.read_jsonl(C.SET_ROOT / set_name / "items.jsonl")
    prod = {(r["item_id"], r["call"]): r for r in C.read_jsonl(C.SET_ROOT / set_name / "production.jsonl")}
    judges: dict[str, dict[tuple[str, str], dict]] = {}
    status: dict[str, collections.Counter] = {}
    for j in JUDGES:
        judges[j], status[j] = {}, collections.Counter()
        p = C.private_root() / "validated" / j / f"{set_name}.jsonl"
        if not p.exists():
            raise SystemExit(f"{p.name}: run validate.py first")
        for r in C.read_jsonl(p):
            status[j][f"{r['call']}:{r['status']}"] += 1
            if r["status"] in ("valid", "repaired", "over_length"):
                judges[j][(r["item_id"], r["call"])] = r
    return items, prod, judges, {j: dict(sorted(c.items())) for j, c in status.items()}


# ── metrics (stack.evals.release_quality, shared with the release's re-scoring) ──────────────────────


def compare_cat(items, get, call, split=None, ci_strata=None) -> dict[str, Any]:
    return M.compare_cat(items, get, call, split, ci_strata, judges=JUDGES)


def disagreements_cat(items: list[dict], res: dict[str, Any], name: str) -> list[dict]:
    lab = res["_labels"]
    out = []
    for it in items:
        i = it["item_id"]
        vals = {s: lab[s].get(i) for s in lab}
        present = [v for v in vals.values() if v is not None]
        if len(present) >= 2 and len({str(v) for v in present}) > 1:
            out.append({"item_id": i, "judgement": name, **{s: vals[s] for s in vals},
                        **({"majority": res["_majority"].get(i)} if "_majority" in res else {}),
                        "title": (it["metadata"].get("title") or "")[:90]})
    return out


def compare_units(units, ids, strata=None) -> dict[str, Any]:
    return M.compare_units(units, ids, strata, judges=JUDGES)


majority_units = M.majority_units
keep = M.keep
strata_of = M.strata_of
norm_surface = M.norm_surface


def disagreements_units(units: dict[str, dict[str, set]], ids: list[str], name: str, cap: int = 12) -> list[dict]:
    out = []
    for i in ids:
        if any(units[s].get(i) is None for s in (PRODUCTION, *JUDGES)):
            continue
        p = units[PRODUCTION][i]
        js = [units[j][i] for j in JUDGES]
        if all(p == x for x in js):
            continue
        o, s = units[PAIR[0]][i], units[PAIR[1]][i]
        cons = o & s
        if not three():
            out.append({"item_id": i, "unit": name, "consensus_missing_in_production": sorted(map(str, cons - p))[:cap],
                        "production_not_in_either_judge": sorted(map(str, p - (o | s)))[:cap],
                        "judges_disagree": sorted(map(str, o ^ s))[:cap]})
            continue
        maj, union, inter = majority_units(js), set().union(*js), set.intersection(*js)
        out.append({"item_id": i, "unit": name, "majority_missing_in_production": sorted(map(str, maj - p))[:cap],
                    "production_not_in_majority": sorted(map(str, p - maj))[:cap],
                    "production_not_in_any_judge": sorted(map(str, p - union))[:cap],
                    "consensus_missing_in_production": sorted(map(str, cons - p))[:cap],
                    "judges_disagree": sorted(map(str, union - inter))[:cap]})
    return out


# ── per-set label readers ─────────────────────────────────────────────────────────────────────


def lab_of(prod: dict, judges: dict, src: str, item: str, call: str) -> dict | None:
    if src == PRODUCTION:
        r = prod.get((item, call))
        return r["label"] if r else None
    r = judges[src].get((item, call))
    return r["label"] if r else None


def getter(prod: dict, judges: dict) -> Callable[[str, str, str], Any]:
    return lambda s, i, call: lab_of(prod, judges, s, i, call)


def score_triage(items, prod, judges) -> tuple[dict, list]:
    res = M.score_triage(items, getter(prod, judges), JUDGES)
    dis = disagreements_cat(items, res["verdict"], "verdict")
    return res, dis


def score_screen(items, prod, judges) -> tuple[dict, list]:
    rules = {it["item_id"]: it["doc_class_rule"] for it in items}
    res = M.score_screen(items, getter(prod, judges), rules, JUDGES)
    dis = []
    for k in ("overall verdict", "S (stack) verdict", "R (shallow review) verdict", "usability verdict",
              "doc_type (model judgements only)"):
        dis += disagreements_cat(items, res[k], k)
    return res, dis


def score_affiliation(items, prod, judges) -> tuple[dict, list]:
    from stack.stage_runner import load_path_module

    ex = load_path_module("_j104_aff_extract_s", C.REPO / "workflows/annotate/affiliation/extract.py")
    units_res, units = M.score_affiliation(items, getter(prod, judges), JUDGES)
    surf, orgs = units["surf"], units["orgs"]
    # window split: are the consensus surfaces production missed inside luna's header window or only outside it?
    blobs = C.Blobs()

    def window_split(js: tuple[str, ...], name: str) -> dict[str, Any]:
        split: collections.Counter = collections.Counter()
        for it in items:
            i = it["item_id"]
            if any(surf[s].get(i) is None for s in (PRODUCTION, *js)):
                continue
            window = re.sub(r"\s+", " ", unicodedata.normalize(
                "NFKC", ex.header_window(blobs.get(it["input"]["text_sha256"]).decode("utf-8")))).casefold()
            for key, sf in majority_units([surf[j][i] for j in js]):
                where = "inside_window" if sf in window else "outside_window"
                split[f"{where}:ref"] += 1
                split[f"{where}:found_by_production"] += (key, sf) in surf[PRODUCTION][i]
        return {w: {f"{name}_units": split[f"{w}:ref"], "production_recall":
                    round(split[f"{w}:found_by_production"] / split[f"{w}:ref"], 4) if split[f"{w}:ref"] else None}
                for w in ("inside_window", "outside_window")}
    res = {"window split (consensus surfaces vs luna's header window, 8k head + 2k tail)":
           window_split(PAIR, "consensus"),
           **({"window split (majority surfaces vs luna's header window, 8k head + 2k tail)":
               window_split(JUDGES, "majority")} if three() else {}),
           **units_res}
    dis = disagreements_units(orgs, [it["item_id"] for it in items], "(author, org slug)")
    return res, dis


def normalized_model_roles(items, judges, labels_of: Callable[[str, str], dict | None]
                           ) -> tuple[dict[str, dict[str, dict | None]], collections.Counter]:
    """judge -> item -> the judge's model_roles answer through production's ``judge.normalize`` (menu over the whole
    text), and the repair counts; a failure is counted and the raw answer used (loud, as before)."""
    import guides as G

    main = G.mr_main()
    c = main.match.scanner_for(C.REPO / "configs")
    blobs = C.Blobs()
    out: dict[str, dict[str, dict | None]] = {j: {} for j in judges}
    repairs: collections.Counter = collections.Counter()
    failed: dict[tuple[str, str], str] = {}
    repaired: dict[tuple[str, str], bool] = {}
    for it in items:
        i = it["item_id"]
        text = blobs.get(it["input"]["text_sha256"]).decode("utf-8")  # the original (text.txt has wrapped lines)
        for j in judges:
            lab = labels_of(j, i)
            if lab is None:
                out[j][i] = None
                continue
            try:
                lab, rep = M.normalize_model_roles(lab, text, main, c)
                repairs[f"{j}:repaired_docs"] += rep
                repaired[(j, i)] = rep
            except Exception as exc:  # loud: counted and reported, the raw rows are used
                repairs[f"{j}:normalize_failed:{type(exc).__name__}"] += 1
                failed[(j, i)] = type(exc).__name__
            out[j][i] = lab
    out["_failed"] = failed  # type: ignore[assignment]
    out["_repaired"] = repaired  # type: ignore[assignment]
    return out, repairs


def score_model_roles(items, prod, judges) -> tuple[dict, list]:
    norm, repairs = normalized_model_roles(items, JUDGES, lambda j, i: lab_of(prod, judges, j, i, "model_roles"))
    check_normalized_file(items, norm)

    def get(s: str, i: str, call: str) -> Any:
        return lab_of(prod, judges, s, i, call) if s == PRODUCTION else norm[s].get(i)
    res, cells2 = M.score_model_roles(items, get, JUDGES, repairs)
    return res, disagreements_units(cells2, [it["item_id"] for it in items], "(node, role) >= 2")


def check_normalized_file(items, norm: dict) -> None:
    """The release's re-scoring reads ``model_roles/judge-normalized.jsonl``: its units must be the ones this run
    computes from the validated labels (refuses otherwise; ``--write-model-roles-normalized`` rewrites it)."""
    p = C.SET_ROOT / "model_roles" / M.NORMALIZED_MR
    if not p.exists():
        print(f"model_roles: {p.name} missing (score.py --write-model-roles-normalized writes it)")
        return
    stored = {(r["judge"], r["item_id"]): r for r in C.read_jsonl(p)}
    bad = []
    for j in JUDGES:
        for it in items:
            i = it["item_id"]
            lab = norm[j].get(i)
            r = stored.get((j, i))
            if (lab is None) != (r is None) or (lab is not None and M.model_roles_units(lab.get("models") or [])
                                                 != M.model_roles_units(r["models"])):
                bad.append(f"{j}:{i}")
    if bad:
        raise SystemExit(f"model_roles: {p.name} disagrees with the validated labels on {len(bad)} answer(s): "
                         f"{bad[:8]}")
    print(f"model_roles: {p.name} matches the validated labels' units")


def write_normalized() -> int:
    """``--write-model-roles-normalized``: the released judge labels (``labels-<judge>.jsonl``, usable statuses)
    normalised with the mirror's texts into ``model_roles/judge-normalized.jsonl``: per (judge, item) the models
    with a role (node id and its non-zero strengths), whether normalize repaired the answer, a failure's exception
    name (the raw answer's models then, as score.py uses it)."""
    items = C.read_jsonl(C.SET_ROOT / "model_roles" / "items.jsonl")
    rel = {j: {r["item_id"]: r["label"] for r in C.read_jsonl(C.SET_ROOT / "model_roles" / f"labels-{j}.jsonl")
               if r["status"] in M.USABLE} for j in C.JUDGES}
    norm, _rep = normalized_model_roles(items, C.JUDGES, lambda j, i: rel[j].get(i))
    rows = []
    for j in C.JUDGES:
        for it in items:
            i = it["item_id"]
            lab = norm[j].get(i)
            if lab is None:
                continue
            models = [{"node_id": m["node_id"], "roles": {k: v for k, v in (m.get("roles") or {}).items() if v}}
                      for m in lab.get("models") or [] if any((m.get("roles") or {}).values())]
            rows.append({"item_id": i, "judge": j, "repaired": norm["_repaired"].get((j, i)),
                         "normalize_failed": norm["_failed"].get((j, i)), "models": models})
    sha = C.write_jsonl(C.SET_ROOT / "model_roles" / M.NORMALIZED_MR, rows)
    print(f"model_roles/{M.NORMALIZED_MR}: {len(rows)} rows, sha256 {sha}")
    return 0


def score_technique(items, prod, judges) -> tuple[dict, list]:
    res, slugs = M.score_technique(items, getter(prod, judges), JUDGES)
    return res, disagreements_units(slugs, [it["item_id"] for it in items], "slug presence")


SCORERS = {"triage": score_triage, "screen": score_screen, "affiliation": score_affiliation,
           "model_roles": score_model_roles, "technique_staged": score_technique}


# ── output ────────────────────────────────────────────────────────────────────────────────────


strip_labels = M.strip_labels


def fmt(x: Any) -> str:
    return "n/a" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def matrix(name: str, x: dict[str, Any]) -> list[str]:
    """A confusion matrix (rows: the first source, columns: the second) when there are at most 6 classes."""
    conf = x.get("confusion_pred->ref") or {}
    cats = sorted({c for k in conf for c in k.split("->")})
    if not cats or len(cats) > 6:
        return []
    a, _, b = name.partition("_vs_")
    out = [f"{name} ({a} rows, {b} columns):", "", "| | " + " | ".join(cats) + " |", "|---" * (len(cats) + 1) + "|"]
    for p in cats:
        out.append(f"| **{p}** | " + " | ".join(str(conf.get(f"{p}->{q}", 0)) for q in cats) + " |")
    return out + [""]


def per_class_table(x: dict[str, Any]) -> list[str]:
    ci = x.get("per_class_ci95") or {}

    def cell(c: str, v: dict[str, Any], m: str) -> str:
        iv = (ci.get(c) or {}).get(m)
        return fmt(v.get(m)) + (f" [{fmt(iv[0])}, {fmt(iv[1])}]" if iv else "")
    return ["| class | precision | recall | F1 | n_pred | n_ref |", "|---|---|---|---|---|---|"] + [
        f"| {c} | {cell(c, v, 'precision')} | {cell(c, v, 'recall')} | {cell(c, v, 'f1')} | {v['n_pred']} | "
        f"{v['n_ref']} |" for c, v in x["per_class"].items()] + [""]


REF_DOC = {"consensus": "Opus and Sol agree (the 2026-10-25 two-judge rule; Sonnet ignored)",
           "majority": "at least 2 of the 3 judges (Opus, Sol, Sonnet) agree"}


def headline(res: dict) -> list[str]:
    """Production against the majority and against the Opus-and-Sol consensus, one row per judgement."""
    cat, uni = [], []
    for name, r in res.items():
        if not isinstance(r, dict) or "production_vs_majority" not in r:
            continue
        m, c = r["production_vs_majority"], r["production_vs_consensus"]
        name = name.replace("|", "\\|")  # inside a markdown table
        if "kappa" in m:
            cat.append(f"| {name} | {m['n']} | {fmt(m['agreement'])} | {fmt(m['kappa'])} | {c['n']} | "
                       f"{fmt(c['agreement'])} | {fmt(c['kappa'])} | {r.get('majority_no_label_n', 0)} |")
        else:
            uni.append(f"| {name} | {m['n_items']} | {fmt(m['precision'])} / {fmt(m['recall'])} / {fmt(m['f1'])} | "
                       f"{m['majority_units']} | {fmt(c['precision'])} / {fmt(c['recall'])} / {fmt(c['f1'])} | "
                       f"{c['consensus_units']} |")
    L = ["## Headline: production vs majority (2 of 3) and vs consensus (Opus and Sol)", ""]
    if cat:
        L += ["| judgement | n maj | agreement maj | kappa maj | n cons | agreement cons | kappa cons | no majority |",
              "|---|---|---|---|---|---|---|---|", *cat, ""]
    if uni:
        L += ["| units | items | P / R / F1 vs majority | majority units | P / R / F1 vs consensus | consensus units |",
              "|---|---|---|---|---|---|", *uni, ""]
    return L + ["Per-class P / R / F1 for each categorical judgement are in its section below.", ""]


def md_report(set_name: str, res: dict, dis: list, status: dict, n_items: int) -> str:
    refs = ["majority", "consensus"] if three() else ["consensus"]
    comps = [f"{a}_vs_{b}" for a, b in comparisons()]
    L = [f"# Judge eval scores: {set_name} ({C.SET_ID})", "",
         f"{n_items} items. Judge records: " + "; ".join(f"{j}: {json.dumps(s)}" for j, s in status.items()), "",
         "Production is the prediction, the judge (or the reference label set) the reference. "
         + " ".join(f"{r.capitalize()} = items (categorical) or units (sets) on which {REF_DOC[r]}." for r in refs), ""]
    if three():
        L += headline(res)
    for name, r in res.items():
        if not isinstance(r, dict):
            L += [f"_{name}_: {r}", ""]
            continue
        L.append(f"## {name}")
        L.append("")
        if "production_vs_opus" in r and "kappa" in r["production_vs_opus"]:
            L += ["| comparison | n | agreement | kappa |", "|---|---|---|---|"]
            for k in comps + [f"production_vs_{x}" for x in reversed(refs)]:
                x = r[k]
                L.append(f"| {k} | {x['n']} | {fmt(x['agreement'])} | {fmt(x['kappa'])} |")
            L.append("")
            if "majority_no_label_n" in r:
                L += [f"Items with at least two judge labels but no majority (1-1-1): {r['majority_no_label_n']}.", ""]
            for k in [f"production_vs_{x}" for x in refs] + comps:
                L += matrix(k, r[k])
            for ref in refs:
                pc = r[f"production_vs_{ref}"]
                if pc["n"]:
                    L += [f"Production vs {ref}, per class:", ""] + per_class_table(pc)
                    L.append(f"Confusion (production -> {ref}): " + ", ".join(f"{k}: {v}" for k, v in
                                                                             pc["confusion_pred->ref"].items()))
                    L.append("")
                for k, v in r.items():
                    if k.startswith(f"production_vs_{ref}_by_"):
                        L.append(f"By {k.removeprefix(f'production_vs_{ref}_by_')} ({ref}): " + "; ".join(
                            f"{g}: n {x['n']}, agreement {fmt(x['agreement'])}, kappa {fmt(x['kappa'])}"
                            for g, x in v.items()))
                        L.append("")
            if "scored_items" in r:
                L += [f"Scored items (doc_type a model judgement): {r['scored_items']}", ""]
        elif "production_vs_opus" in r:
            L += ["| comparison | items | P [95% CI] | R [95% CI] | F1 [95% CI] | Jaccard | tp / fp / fn |",
                  "|---|---|---|---|---|---|---|"]
            for k in comps + [f"production_vs_{x}" for x in reversed(refs)]:
                x = r[k]
                ci = x.get("ci95") or {}
                cell = lambda m: fmt(x[m]) + (f" [{fmt(ci[m][0])}, {fmt(ci[m][1])}]" if m in ci else "")  # noqa: E731
                L.append(f"| {k} | {x['n_items']} | {cell('precision')} | {cell('recall')} | {cell('f1')} | "
                         f"{fmt(x['jaccard'])} | {x['tp']} / {x['fp']} / {x['fn']} |")
            L.append("")
            for ref in reversed(refs):
                c = r[f"production_vs_{ref}"]
                who, union = (("Opus and Sol both have", "Opus | Sol") if ref == "consensus" else
                              ("at least 2 of the 3 judges have", "Opus | Sol | Sonnet"))
                L += [f"{ref.capitalize()} units ({who}): {c[f'{ref}_units']}; production's precision against the "
                      f"union ({union}): {fmt(c['precision_vs_union'])} ({c['production_units']} production units).",
                      ""]
        else:
            L += ["```", json.dumps(r, indent=1, ensure_ascii=False)[:3000], "```", ""]
    L += [f"## Disagreements ({len(dis)})", ""]
    for d in dis:
        L.append("- " + json.dumps(d, ensure_ascii=False))
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sets", default=",".join(C.SETS))
    ap.add_argument("--out", default="")
    ap.add_argument("--judges", default=",".join(C.JUDGES),
                    help="opus,sol,sonnet (default: majority added) or opus,sol (the 2026-10-25 two-judge outputs)")
    ap.add_argument("--write-model-roles-normalized", action="store_true",
                    help=f"write model_roles/{M.NORMALIZED_MR} from the released labels and exit")
    a = ap.parse_args()
    if a.write_model_roles_normalized:
        return write_normalized()
    global JUDGES
    JUDGES = tuple(a.judges.split(","))
    if JUDGES not in (C.JUDGES, PAIR):
        raise SystemExit(f"--judges must be {','.join(C.JUDGES)} or {','.join(PAIR)}")
    out = Path(a.out) if a.out else C.private_root() / "scores"
    out.mkdir(parents=True, exist_ok=True)
    for set_name in a.sets.split(","):
        items, prod, judges, status = load(set_name)
        res, dis = SCORERS[set_name](items, prod, judges)
        res = strip_labels(res)
        (out / f"{set_name}.json").write_text(C.dumps({"set": set_name, "status": status, "scores": res,
                                                       "disagreements": dis}), "utf-8")
        (out / f"{set_name}.md").write_text(md_report(set_name, res, dis, status, len(items)), "utf-8")
        print(set_name, json.dumps(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
