"""A release's quality numbers from its own build (D728): the judge eval set's stored labels scored against the
build's production labels (:func:`rescore`), and triage's agreement with the screen (:func:`triage_vs_screen`).
Deterministic and offline: no LLM call, nothing read but the build's store tables and the repository's eval files.

    python -m stack.evals.release_quality rescore --store ROOT --build ID [--eval evals/judge-2026-10-25j]
        [--out FILE.json] [--compare]          # --compare: diff against the committed <set>/scores.json
    python -m stack.evals.release_quality triage-vs-screen --store ROOT --build ID [--out FILE.json]

**Metrics** (moved here from ``evals/judge-2026-10-25j/scripts/score.py`` and ``score_triage_w.py``, which import
them; their docstrings are the method): categorical agreement, Cohen's kappa and per-class P / R / F1 with
document-bootstrap intervals (:func:`compare_cat`), unit sets' micro P / R / F1 (:func:`compare_units`), the
majority (2 of 3 judges) and consensus (Opus = Sol) references, and the weighted triage estimator
(:func:`triage_w_scores`). ``judges`` is the three judges, or ``("opus", "sol")`` for the 2026-10-25 two-judge
outputs. The per-set scorers (:func:`score_triage`, :func:`score_screen`, :func:`score_affiliation`,
:func:`score_model_roles`, :func:`score_technique`) read labels through ``get(source, item_id, call)``, so the eval
scripts (production = the frozen ``production.jsonl``) and the re-scoring (production = the build) share them.

**The build's tables** (:func:`load_build`): the build record's corpus (``fold.build``), the ``documents`` file its
``fold.corpus`` run read (the build's ``corpus_input`` run's ``inputs.documents``), the ``triage_evidence`` head of
that series (cumulative, D129: it holds every evidence row the documents name), and the triage question's current
config digest (``stack.gate.questions`` of the build's plan file in this checkout).

**Production labels** (:func:`production_labels`), per item of a set, from the build: triage = the
``safety_relevance`` question of the document's ``documents`` row and its evidence row (verdict, confidence,
basis); screen, affiliation, model_roles, technique_staged = the corpus row's annotation payloads, verbatim strings
capped at 300 characters as the frozen ``production.jsonl`` and the released judge labels are
(:func:`cap_verbatim`). An item is **dropped** (counted by reason, never scored) when its document is not in the
build (``missing``), is outside the set's population there (the frame rules of ``scripts/sample.py``: e.g. triage
``state_retired``, ``not_llm_decision`` for a signal (D724) or a rule decision; screen ``screen_origin_human``), or
its input changed since the judges labelled it (``input_changed``: corpus sets, the annotation's ``basis_digest`` is
not the item's text sha256; triage, the evidence row's ``basis`` or ``basis_digest`` differs from the evidence the
item was frozen with), or (triage) the documents row's verdict is not its evidence row's (``verdict_mismatch``). The
judges labelled the frozen input; a production label on another input is not compared.

**Re-scoring** (:func:`rescore`): the judges' released labels (``<set>/labels-<judge>.jsonl``, status ``valid`` /
``repaired`` / ``over_length``; model_roles from ``model_roles/judge-normalized.jsonl``, the judges' answers through
production's ``judge.normalize``, which needs the texts: written by ``score.py --write-model-roles-normalized``) against
the build's production labels, by the per-set scorers above. The proportional sets are self-weighting and keep their
sampling strata (bootstrap strata). triage_w is weighted: its strata (production verdict; ``not_relevant`` by
confidence at 0.94 / 0.98) and the proportional triage set's (basis x verdict) are recomputed on the build's
population with the frame and exclusions of ``sample.py`` / ``triage_w.py`` (the eval files named in
``triage_w/sampling.json`` ``existing_eval_files``, and for triage_w the five sets' own documents); each item is
re-assigned to the stratum of its production label in this build; N_h, n_h and the weights N_h / n_h are this
build's. Not recomputed (they need the texts or describe build ws1qpn8myzxah's inputs): the screen window splits,
affiliation's window split, the disagreement lists. Re-scoring the eval's own build reproduces the committed
``scores.json`` numbers (``--compare``; ``tests/test_release_quality.py`` checks the committed set).

**Triage vs screen** (:func:`triage_vs_screen`): every corpus document with a current-digest triage verdict
(``stack.gate.verdict_of``: a decision or, D724, a signal) and a model screen verdict (``ok``, origin ``ai_check``):
triage's fetch (relevant | borderline) against the screen's keep (relevant | borderline), as P / R / F1 with the
confusion table, overall and by evidence basis, triage confidence and kind (decision / signal). The screen is a
model judgement too: this is the agreement of two pipeline stages, not accuracy. Decisions are the documents triage
chose to fetch (mostly ``fetch``), so their recall says little; signals (documents fetched for other reasons) are the
unbiased part.
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import random
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCTION = "production"
#: The judges of the 2026-10-25j eval sets (Opus and Sol 2026-10-25, Sonnet added 2026-10-26).
JUDGES: tuple[str, ...] = ("opus", "sol", "sonnet")
#: The two-judge consensus (Opus and Sol give the same label).
PAIR = ("opus", "sol")
BOOT_N, BOOT_SEED = 2000, 20261026
#: Judge record statuses that are scored (``validate.py``).
USABLE = ("valid", "repaired", "over_length")
TRIAGE_CLASSES = ("relevant", "borderline", "not_relevant")
KEEP = frozenset({"relevant", "borderline"})
QUESTION = "safety_relevance"
#: The corpus sets and their annotation (whose ``basis_digest`` is the item's input check).
CORPUS_SETS = {"screen": "screen", "affiliation": "affiliation", "model_roles": "model_roles",
               "technique_staged": "technique_staged"}
SETS = ("triage", "triage_w", "screen", "affiliation", "model_roles", "technique_staged")
NORMALIZED_MR = "judge-normalized.jsonl"
Get = Callable[[str, str, str], Any]


class QualityError(ValueError):
    """A re-scoring input is missing or inconsistent (the export refuses)."""


# ---------------------------------------------------------------- categorical metrics
def comparisons(judges: Sequence[str] = JUDGES) -> list[tuple[str, str]]:
    """Every pair of sources, production first (two judges: the 2026-10-25 three pairs, same order)."""
    return list(itertools.combinations((PRODUCTION, *judges), 2))


def kappa(pairs: list[tuple[Any, Any]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    cats = sorted({str(x) for p in pairs for x in p})
    po = sum(str(a) == str(b) for a, b in pairs) / n
    ca = collections.Counter(str(a) for a, _ in pairs)
    cb = collections.Counter(str(b) for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in cats) / (n * n)
    return None if pe >= 1 else round((po - pe) / (1 - pe), 4)


def categorical(pairs: list[tuple[str, Any, Any]]) -> dict[str, Any]:
    """``pairs`` = (item_id, prediction, reference)."""
    pr = [(p, r) for _, p, r in pairs]
    n = len(pr)
    cats = sorted({str(x) for x in (v for p in pr for v in p)})
    conf = {f"{a}->{b}": c for (a, b), c in sorted(collections.Counter((str(p), str(r)) for p, r in pr).items())}
    per = {}
    for c in cats:
        tp = sum(str(p) == c and str(r) == c for p, r in pr)
        np_, nr = sum(str(p) == c for p, _ in pr), sum(str(r) == c for _, r in pr)
        pc_, rc = (tp / np_ if np_ else None), (tp / nr if nr else None)
        f1 = (2 * pc_ * rc / (pc_ + rc) if pc_ + rc else 0.0) if pc_ is not None and rc is not None else None
        per[c] = {"precision": round(pc_, 4) if pc_ is not None else None, "recall": round(rc, 4) if rc is not None
                  else None, "f1": round(f1, 4) if f1 is not None else None, "n_pred": np_, "n_ref": nr}
    return {"n": n, "agreement": round(sum(str(p) == str(r) for p, r in pr) / n, 4) if n else None,
            "kappa": kappa(pr), "confusion_pred->ref": conf, "per_class": per}


def compare_cat(items: list[dict[str, Any]], get: Callable[[str, str], Any], call: str,
                split: Callable[[dict[str, Any]], str] | None = None, ci_strata: dict[str, str] | None = None,
                judges: Sequence[str] = JUDGES) -> dict[str, Any]:
    """``get(source, item_id)`` -> label or None (missing / not scored). With ``ci_strata`` (item -> stratum), each
    ``production_vs_<reference>`` also carries ``per_class_ci95``: document-bootstrap 95 % intervals of every class's
    precision / recall / F1 (:func:`boot_cat`)."""
    del call  # kept for the callers' readability (the call the labels come from)
    out: dict[str, Any] = {}
    src = [PRODUCTION, *judges]
    lab = {s: {it["item_id"]: get(s, it["item_id"]) for it in items} for s in src}
    for a, b in comparisons(judges):
        out[f"{a}_vs_{b}"] = categorical([(i, lab[a][i], lab[b][i]) for i in lab[a]
                                          if lab[a][i] is not None and lab[b][i] is not None])
    o, s_ = PAIR
    cons = {i: lab[o][i] for i in lab[o] if lab[o][i] is not None and str(lab[o][i]) == str(lab[s_].get(i))}
    refs = {"consensus": cons}
    if len(judges) == 3:
        maj, no_label = {}, 0
        for i in lab[PRODUCTION]:
            votes = [lab[j][i] for j in judges if lab[j][i] is not None]
            top = collections.Counter(str(v) for v in votes).most_common(1)
            if top and top[0][1] >= 2:
                maj[i] = next(v for v in votes if str(v) == top[0][0])
            elif len(votes) >= 2:
                no_label += 1
        refs["majority"] = maj
        out["majority_no_label_n"] = no_label
    for name, ref in refs.items():
        triples = [(i, lab[PRODUCTION][i], v) for i, v in ref.items() if lab[PRODUCTION][i] is not None]
        out[f"production_vs_{name}"] = categorical(triples)
        if ci_strata is not None:
            out[f"production_vs_{name}"]["per_class_ci95"] = boot_cat(triples, ci_strata)
        out[f"{name}_n"] = len(ref)
        if split:
            by = collections.defaultdict(list)
            for it in items:
                i = it["item_id"]
                if i in ref and lab[PRODUCTION][i] is not None:
                    by[split(it)].append((i, lab[PRODUCTION][i], ref[i]))
            out[f"production_vs_{name}_by_" + split.__name__] = {k: categorical(v) for k, v in sorted(by.items())}
    out["_labels"] = lab
    if len(judges) == 3:
        out["_majority"] = refs["majority"]
    return out


# ---------------------------------------------------------------- unit metrics
def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = (2 * p * r / (p + r) if p + r else 0.0) if p is not None and r is not None else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": None if p is None else round(p, 4),
            "recall": None if r is None else round(r, 4), "f1": None if f is None else round(f, 4)}


def micro(rows: list[tuple[int, int, int]]) -> dict[str, Any]:
    tp, fp, fn = (sum(r[k] for r in rows) for k in range(3))
    return prf(tp, fp, fn)


def boot_units(per_item: dict[str, tuple[int, int, int]], strata: dict[str, str]) -> dict[str, list[float]]:
    """95 % percentile intervals of micro P / R / F1, documents resampled with replacement within stratum."""
    rng = random.Random(BOOT_SEED)
    by: dict[str, list[str]] = collections.defaultdict(list)
    for i in sorted(per_item):
        by[strata.get(i, "")].append(i)
    if len(per_item) < 10:
        return {}
    acc: dict[str, list[float]] = {"precision": [], "recall": [], "f1": []}
    for _ in range(BOOT_N):
        rows = [per_item[rng.choice(ids)] for ids in by.values() for _ in ids]
        m = micro(rows)
        for k in acc:
            if m[k] is not None:
                acc[k].append(m[k])
    out = {}
    for k, v in acc.items():
        if len(v) >= BOOT_N // 2:
            v.sort()
            out[k] = [round(v[int(0.025 * len(v))], 4), round(v[int(0.975 * len(v)) - 1], 4)]
    return out


def boot_cat(triples: list[tuple[str, Any, Any]], strata: dict[str, str]) -> dict[str, dict[str, list[float]]]:
    """95 % percentile intervals of each class's precision / recall / F1 (production vs a reference), items
    resampled with replacement within stratum (as :func:`boot_units`: :data:`BOOT_N` resamples, seed
    :data:`BOOT_SEED`); a metric undefined in more than half the resamples gets no interval; {} under 10 items."""
    if len(triples) < 10:
        return {}
    rng = random.Random(BOOT_SEED)
    by: dict[str, list[tuple[str, Any, Any]]] = collections.defaultdict(list)
    for t in sorted(triples, key=lambda t: t[0]):
        by[strata.get(t[0], "")].append(t)
    cats = sorted({str(x) for _, p, r in triples for x in (p, r)})
    acc: dict[str, dict[str, list[float]]] = {c: {"precision": [], "recall": [], "f1": []} for c in cats}
    for _ in range(BOOT_N):
        pcl = categorical([rng.choice(ts) for ts in by.values() for _ in ts])["per_class"]
        for c in cats:
            for k in ("precision", "recall", "f1"):
                v = (pcl.get(c) or {}).get(k)
                if v is not None:
                    acc[c][k].append(v)
    out: dict[str, dict[str, list[float]]] = {}
    for c, ms in acc.items():
        for k, v in ms.items():
            if len(v) >= BOOT_N // 2:
                v.sort()
                out.setdefault(c, {})[k] = [round(v[int(0.025 * len(v))], 4), round(v[int(0.975 * len(v)) - 1], 4)]
    return out


def majority_units(sets: list[set[Any]]) -> set[Any]:
    """Units at least 2 of the judges have (for two judges: both, i.e. the intersection)."""
    cnt = collections.Counter(u for s in sets for u in s)
    return {u for u, n in cnt.items() if n >= 2}


def compare_units(units: dict[str, dict[str, set[Any] | None]], ids: list[str], strata: dict[str, str] | None = None,
                  judges: Sequence[str] = JUDGES) -> dict[str, Any]:
    """``units[source][item_id]`` = set of unit keys (None = missing). Micro over the items present in both, with
    document-bootstrap 95 % intervals (``ci95``) when ``strata`` is given and n >= 10. Consensus = the units both
    judges have (intersection): production's P / R / F1 against it, and production's precision against the union."""
    def per(a: str, b: str) -> dict[str, tuple[int, int, int]]:
        out = {}
        for i in ids:
            x, y = units[a].get(i), units[b].get(i)
            if x is None or y is None:
                continue
            out[i] = (len(x & y), len(x - y), len(y - x))
        return out

    def report(pi: dict[str, tuple[int, int, int]]) -> dict[str, Any]:
        r = micro(list(pi.values()))
        r["n_items"] = len(pi)
        r["jaccard"] = round(r["tp"] / (r["tp"] + r["fp"] + r["fn"]), 4) if r["tp"] + r["fp"] + r["fn"] else None
        if strata is not None:
            r["ci95"] = boot_units(pi, strata)
        return r
    out = {f"{a}_vs_{b}": report(per(a, b)) for a, b in comparisons(judges)}
    refs: dict[str, Sequence[str]] = {"consensus": PAIR}
    if len(judges) == 3:
        refs["majority"] = judges
    for name, js in refs.items():
        ref_pi, union_pi = {}, {}
        for i in ids:
            if any(units[s].get(i) is None for s in (PRODUCTION, *js)):
                continue
            js_units = [u for u in (units[j][i] for j in js) if u is not None]
            ref = majority_units(js_units)
            union: set[Any] = set().union(*js_units)
            p = units[PRODUCTION][i]
            assert p is not None
            ref_pi[i] = (len(p & ref), len(p - ref), len(ref - p))
            union_pi[i] = (len(p & union), len(p - union), 0)
        k = f"production_vs_{name}"
        out[k] = report(ref_pi)
        u = micro(list(union_pi.values()))
        out[k]["precision_vs_union"] = u["precision"]
        out[k][f"recall_on_{name}"] = out[k]["recall"]
        out[k][f"{name}_units"] = out[k]["tp"] + out[k]["fn"]
        out[k]["production_units"] = u["tp"] + u["fp"]
    return out


def keep(v: str | None) -> str | None:
    """The binary keep / fetch view of a verdict (relevant or borderline vs not)."""
    if v is None:
        return None
    return "keep" if v in KEEP else "drop"


def strata_of(items: list[dict[str, Any]]) -> dict[str, str]:
    return {it["item_id"]: it["stratum"] for it in items}


#: Keys whose string values (or lists of strings, or ``text`` of quote objects) are verbatim document text.
VERBATIM_KEYS = frozenset({"quote", "quotes", "surface", "author_surface", "name_as_written", "names_as_written",
                           "snippet", "text"})


def cap_verbatim(obj: Any, cap: int = 300, _in: bool = False) -> Any:
    """Every verbatim string (under :data:`VERBATIM_KEYS`) cut with ``stack.fold.dataset.cut_string`` (at most
    ``cap`` code points, at a word boundary, "…")."""
    from stack.fold.dataset import cut_string

    if isinstance(obj, dict):
        return {k: cap_verbatim(v, cap, k in VERBATIM_KEYS) for k, v in obj.items()}
    if isinstance(obj, list):
        return [cap_verbatim(x, cap, _in) for x in obj]
    if isinstance(obj, str) and _in:
        return cut_string(obj, cap)
    return obj


# ---------------------------------------------------------------- per-set scorers (labels through ``get``)
def score_triage(items: list[dict[str, Any]], get: Get, judges: Sequence[str] = JUDGES) -> dict[str, Any]:
    """The proportional triage set: the verdict (split by evidence basis) and the fetch view."""
    def g(s: str, i: str) -> Any:
        return (get(s, i, "triage") or {}).get("verdict")

    def basis(it: dict[str, Any]) -> str:
        return str(it["basis"])
    return {"verdict": compare_cat(items, g, "triage", basis, judges=judges),
            "fetch (relevant|borderline vs not_relevant)": compare_cat(items, lambda s, i: keep(g(s, i)), "triage",
                                                                       judges=judges)}


SCREEN_KEEP = "overall keep (relevant|borderline vs irrelevant)"


def score_screen(items: list[dict[str, Any]], get: Get, rules: Mapping[str, Mapping[str, Any]],
                 judges: Sequence[str] = JUDGES, windows: bool = True) -> dict[str, Any]:
    """The screen set; ``rules`` = item -> its doc_class rule (``channel_doc_type``, ``metadata_paper_type``,
    ``model_judgement``); ``windows``: split agreement by luna's window (``items[*].luna_window``)."""
    def rel(fieldname: str) -> Callable[[str, str], Any]:
        def g(s: str, i: str) -> Any:
            lab = get(s, i, "relevance")
            if lab is None:
                return None
            if fieldname == "verdict":
                return lab.get("verdict")
            sub = lab.get(fieldname) or {}
            return sub.get("verdict")
        return g

    def window(it: dict[str, Any]) -> str:
        return "whole_text_seen" if it["luna_window"]["final_whole"] else "excerpt_seen"

    def tier1_window(it: dict[str, Any]) -> str:
        return "whole_text_seen" if it["luna_window"]["tier1"]["whole"] else "excerpt_seen"

    def cat(s: str, i: str) -> Any:
        lab = get(s, i, "relevance")
        sr = (lab or {}).get("sr") or {}
        return sr.get("category") if sr.get("verdict") in ("relevant", "borderline") else None

    def usab(s: str, i: str) -> Any:
        lab = get(s, i, "quality")
        return ((lab or {}).get("usability") or {}).get("verdict")

    def doc_type(s: str, i: str) -> Any:
        r = rules[i]
        if not r["model_judgement"]:
            return None
        lab = get(s, i, "quality")
        d = ((lab or {}).get("doc_class") or {}).get("doc_type")
        if s != PRODUCTION and d in ("paper", "preprint") and r["metadata_paper_type"]:
            d = r["metadata_paper_type"]  # production's own rule, applied to the judge's answer
        return d

    def dc(fieldname: str) -> Callable[[str, str], Any]:
        def g(s: str, i: str) -> Any:
            lab = get(s, i, "quality")
            v = ((lab or {}).get("doc_class") or {}).get(fieldname)
            return None if lab is None else str(v).lower() if v is not None else "null"
        return g

    w, w1 = (window, tier1_window) if windows else (None, None)
    res = {
        "overall verdict": compare_cat(items, rel("verdict"), "relevance", w, judges=judges),
        "S (stack) verdict": compare_cat(items, rel("stack"), "relevance", w, judges=judges),
        "R (shallow review) verdict": compare_cat(items, rel("sr"), "relevance", w, judges=judges),
        SCREEN_KEEP: compare_cat(items, lambda s, i: keep(rel("verdict")(s, i)), "relevance", w, strata_of(items),
                                 judges=judges),
        "R category (where R is relevant or borderline)": compare_cat(items, cat, "relevance", judges=judges),
        "usability verdict": compare_cat(items, usab, "quality", w1, judges=judges),
        "doc_type (model judgements only)": compare_cat(items, doc_type, "quality", judges=judges),
        "self_published": compare_cat(items, dc("self_published"), "quality", judges=judges),
        "primary_lang": compare_cat(items, dc("primary_lang"), "quality", judges=judges),
    }
    res["doc_type (model judgements only)"]["scored_items"] = sum(bool(rules[it["item_id"]]["model_judgement"])
                                                                  for it in items)
    ids = [it["item_id"] for it in items]
    units: dict[str, dict[str, set[Any] | None]] = {s: {} for s in (PRODUCTION, *judges)}
    for s in units:
        for i in ids:
            lab = get(s, i, "quality")
            units[s][i] = None if lab is None else set((lab.get("usability") or {}).get("issues") or [])
    res["usability issues (codes)"] = compare_units(units, ids, strata_of(items), judges)
    return res


def norm_surface(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"\s+", " ", s).strip(" ,.;:*†‡§¶0123456789^{}()[]")
    return s


AFF_SURFACE = "(author, verbatim surface) [normalised: case, spacing, edge markers]"


def _workflow(name: str, rel: str) -> Any:
    from stack.stage_runner import load_path_module

    return load_path_module(name, REPO_ROOT / "workflows" / rel)


def score_affiliation(items: list[dict[str, Any]], get: Get, judges: Sequence[str] = JUDGES
                      ) -> tuple[dict[str, Any], dict[str, dict[str, set[Any] | None]]]:
    """The three unit comparisons and the note; also the (author, surface) units (the eval script's window split
    reads them with the texts)."""
    from stack.annotate.stage import stack_config
    from stack.canon import Canon

    ex = _workflow("_rq_aff_extract", "annotate/affiliation/extract.py")
    canon = Canon.from_config(stack_config())
    ids = [it["item_id"] for it in items]

    def authors(s: str, i: str) -> list[tuple[str, list[str]]] | None:
        lab = get(s, i, "affiliation")
        if lab is None:
            return None
        if s == PRODUCTION:
            return [(x["author_surface"], [a["surface"] for a in x.get("affiliations") or []]
                     if x.get("resolution") == "text" else []) for x in sorted(lab.get("items") or [],
                                                                               key=lambda x: x["byline_index"])]
        return [(x.get("author_surface") or "", [a.get("surface") or "" for a in x.get("affiliations") or []])
                for x in lab.get("authors") or []]

    surf: dict[str, dict[str, set[Any] | None]] = {s: {} for s in (PRODUCTION, *judges)}
    orgs: dict[str, dict[str, set[Any] | None]] = {s: {} for s in (PRODUCTION, *judges)}
    has: dict[str, dict[str, set[Any] | None]] = {s: {} for s in (PRODUCTION, *judges)}
    for s in surf:
        for i in ids:
            al = authors(s, i)
            if al is None:
                surf[s][i] = orgs[s][i] = has[s][i] = None
                continue
            su, og, hs = set(), set(), set()
            for name, affs in al:
                key = ex.norm_author_name(name)
                for a in affs:
                    if norm_surface(a):
                        su.add((key, norm_surface(a)))
                        og |= {(key, r.slug) for r in canon.orgs_in(a) if r.slug}
                if any(norm_surface(a) for a in affs):
                    hs.add(key)
            surf[s][i], orgs[s][i], has[s][i] = su, og, hs
    st = strata_of(items)
    res = {AFF_SURFACE: compare_units(surf, ids, st, judges),
           "(author, org slug) [registry match on the surfaces]": compare_units(orgs, ids, st, judges),
           "authors with at least one affiliation": compare_units(has, ids, st, judges),
           "note": "production surfaces: items with resolution 'text' (openalex / email-domain fallbacks are not text "
                   "surfaces); authors keyed by production's name key (norm_author_name)"}
    return res, {"surf": surf, "orgs": orgs}  # type: ignore[dict-item]


def model_roles_units(models: Iterable[Mapping[str, Any]]) -> tuple[set[Any], set[Any], set[Any]]:
    """(node, role) >= 2 cells, (node, role) >= 1 cells and nodes with any role, of a payload's ``models``."""
    c2, c1, nodes = set(), set(), set()
    for m in models:
        rs = {k: v for k, v in (m.get("roles") or {}).items() if v}
        if rs:
            nodes.add(m["node_id"])
        for r, v in rs.items():
            c1.add((m["node_id"], r))
            if v >= 2:
                c2.add((m["node_id"], r))
    return c2, c1, nodes


def score_model_roles(items: list[dict[str, Any]], get: Get, judges: Sequence[str] = JUDGES,
                      normalisation: Mapping[str, int] | None = None
                      ) -> tuple[dict[str, Any], dict[str, dict[str, set[Any] | None]]]:
    """model_roles units and the (node, role) >= 2 units; the judges' labels must already be normalised
    (production's ``judge.normalize``; ``normalisation`` = its repair counts, reported as they are)."""
    ids = [it["item_id"] for it in items]
    U: dict[str, dict[str, dict[str, set[Any] | None]]] = {u: {s: {} for s in (PRODUCTION, *judges)}
                                                           for u in ("cells2", "cells1", "nodes")}
    for i in ids:
        for s in (PRODUCTION, *judges):
            lab = get(s, i, "model_roles")
            if lab is None:
                for u in U:
                    U[u][s][i] = None
                continue
            U["cells2"][s][i], U["cells1"][s][i], U["nodes"][s][i] = model_roles_units(lab.get("models") or [])
    st = strata_of(items)
    return {"(node, role) >= 2": compare_units(U["cells2"], ids, st, judges),
            "(node, role) >= 1": compare_units(U["cells1"], ids, st, judges),
            "node presence (any role >= 1)": compare_units(U["nodes"], ids, st, judges),
            "judge_normalisation": dict(normalisation or {}),
            "note": "models rows only (gold-v3 units); judge answers normalised by production's judge.normalize"
            }, U["cells2"]


def normalize_model_roles(label: Mapping[str, Any], text: str, main: Any, c: Any
                          ) -> tuple[dict[str, Any], bool]:
    """A judge's model_roles answer through production's ``judge.normalize`` (the menu built over the whole text,
    as the judges saw it): ``(payload, repaired)``. Raises what normalize raises."""
    scan = c.scanner.scan(text)
    menu, _beyond, _f = main.fills(text, scan, c.vocab, max(len(text), 1_000))
    norm = main.judge.normalize(label, menu_nodes=[e.node_id for e in menu], menu_names={e.node_id: e.name for e in menu},
                                menu_kinds={e.node_id: e.denotation for e in menu}, text=text, beyond_window=[],
                                refine=main.refiner(c))
    return main.judge.to_payload(norm), bool(norm["repairs"])


TS_TAXONOMY = "taxonomy/techniques-1.0.0.yaml"


def score_technique(items: list[dict[str, Any]], get: Get, judges: Sequence[str] = JUDGES
                    ) -> tuple[dict[str, Any], dict[str, dict[str, set[Any] | None]]]:
    """technique_staged: slug presence and (slug, role) cells, group nodes and invalid slugs out (taxonomy
    :data:`TS_TAXONOMY`, the judges' catalogue); also the slug-presence units."""
    cat = _workflow("_rq_ts_main", "annotate/technique_staged/main.py").catalogue(REPO_ROOT / TS_TAXONOMY)
    groups = {g.slug for g in cat.tax.group_nodes}
    valid = set(cat.slugs)
    ids = [it["item_id"] for it in items]
    U: dict[str, dict[str, dict[str, set[Any] | None]]] = {u: {s: {} for s in (PRODUCTION, *judges)}
                                                           for u in ("slugs", "cells2", "cells1")}
    invalid: collections.Counter[str] = collections.Counter()
    for i in ids:
        for s in (PRODUCTION, *judges):
            lab = get(s, i, "technique_staged")
            if lab is None:
                for u in U:
                    U[u][s][i] = None
                continue
            sl, c2, c1 = set(), set(), set()
            for t in lab.get("techniques") or []:
                slug = t.get("slug")
                if slug not in valid:
                    invalid[s] += 1
                    continue
                if slug in groups:
                    continue
                rs = {k: v for k, v in (t.get("roles") or {}).items() if isinstance(v, int) and v > 0}
                if not rs:
                    continue
                sl.add(slug)
                for r, v in rs.items():
                    c1.add((slug, r))
                    if v >= 2:
                        c2.add((slug, r))
            U["slugs"][s][i], U["cells2"][s][i], U["cells1"][s][i] = sl, c2, c1
    st = strata_of(items)
    return {"slug presence": compare_units(U["slugs"], ids, st, judges),
            "(slug, role) >= 2": compare_units(U["cells2"], ids, st, judges),
            "(slug, role) >= 1": compare_units(U["cells1"], ids, st, judges), "invalid_slugs_dropped": dict(invalid),
            "note": f"taxonomy {cat.version}; group nodes left out (gold-v3 strict units)"}, U["slugs"]


def strip_labels(obj: Any) -> Any:
    """A scores dict without the per-item label maps the scorers keep for the disagreement lists."""
    if isinstance(obj, dict):
        return {k: strip_labels(v) for k, v in obj.items() if k not in ("_labels", "_majority")}
    return obj


# ---------------------------------------------------------------- the weighted triage estimator (triage_w)
#: triage_w's planned n_h per stratum and its not_relevant confidence cut points (``scripts/triage_w.py``).
TW_ALLOC = {"relevant": 30, "borderline": 30, "not_relevant|c<=0.94": 70, "not_relevant|c0.95-0.98": 40,
            "not_relevant|c0.99": 30}
TW_CUTS = (0.94, 0.98)
TW_STRATA = tuple(TW_ALLOC)
TW_NOT_RELEVANT = TW_STRATA[2:]


def stratum_of(verdict: str, confidence: float | None) -> str:
    """triage_w's stratum of a production verdict and confidence."""
    if verdict != "not_relevant":
        return verdict
    if confidence is None:
        raise ValueError("not_relevant row without a confidence")
    c = round(confidence, 4)
    return ("not_relevant|c<=0.94" if c <= TW_CUTS[0] else "not_relevant|c0.95-0.98" if c <= TW_CUTS[1]
            else "not_relevant|c0.99")


Cell = tuple[float, list[tuple[str | None, str | None]]]  # (N_h, [(pred, ref)] for every item of the cell)


def totals(cells: list[Cell]) -> tuple[dict[tuple[str, str], float], int, list[str]]:
    t: dict[tuple[str, str], float] = collections.defaultdict(float)
    scored, empty = 0, []
    for k, (n_pop, pairs) in enumerate(cells):
        s = [(p, r) for p, r in pairs if r is not None and p is not None]
        scored += len(s)
        if not s:
            empty.append(str(k))
            continue
        w = n_pop / len(s)
        for p, r in s:
            t[(str(p), str(r))] += w
    return t, scored, empty


def _div(a: float, b: float) -> float | None:
    return a / b if b > 0 else None


def _f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None:
        return None
    return 2 * p * r / (p + r) if p + r > 0 else 0.0


def tw_metrics(t: dict[tuple[str, str], float]) -> dict[str, float | None]:
    def T(pf: Callable[[str], bool], rf: Callable[[str], bool]) -> float:
        return sum(v for (p, r), v in t.items() if pf(p) and rf(r))

    def anyv(_x: str) -> bool:
        return True

    def k(x: str) -> bool:
        return x in KEEP
    tot = T(anyv, anyv)
    out: dict[str, float | None] = {}
    tp = T(k, k)
    out["fetch_precision"] = _div(tp, T(k, anyv))
    out["fetch_recall"] = _div(tp, T(anyv, k))
    out["fetch_f1"] = _f1(out["fetch_precision"], out["fetch_recall"])
    for c in TRIAGE_CLASSES:
        def e(x: str, c: str = c) -> bool:
            return x == c
        out[f"{c}_precision"] = _div(T(e, e), T(e, anyv))
        out[f"{c}_recall"] = _div(T(e, e), T(anyv, e))
        out[f"{c}_f1"] = _f1(out[f"{c}_precision"], out[f"{c}_recall"])
    out["agreement"] = _div(sum(v for (p, r), v in t.items() if p == r), tot)

    def nr(x: str) -> bool:
        return x == "not_relevant"
    out["missed_relevant"] = T(nr, lambda r: r == "relevant")
    out["missed_keep"] = T(nr, k)
    out["ref_relevant_total"] = T(anyv, lambda r: r == "relevant")
    out["ref_keep_total"] = T(anyv, k)
    out["total"] = tot
    return out


def estimate(cells: list[Cell]) -> dict[str, Any]:
    t, scored, empty = totals(cells)
    point = tw_metrics(t)
    rng = random.Random(BOOT_SEED)
    reps: dict[str, list[float]] = collections.defaultdict(list)
    for _ in range(BOOT_N):
        bc = [(n_pop, [pairs[rng.randrange(len(pairs))] for _ in pairs]) for n_pop, pairs in cells if pairs]
        for key, x in tw_metrics(totals(bc)[0]).items():
            if x is not None:
                reps[key].append(x)
    ci: dict[str, list[Any]] = {}
    for key, v in reps.items():
        if len(v) >= BOOT_N // 2:
            v.sort()
            ci[key] = [v[int(0.025 * len(v))], v[int(0.975 * len(v)) - 1]]
            if len(v) < BOOT_N:
                ci[key].append(f"{BOOT_N - len(v)} resamples undefined")
    n_all = sum(len(p) for _, p in cells)
    return {"n_items": n_all, "n_scored": scored, "n_dropped": n_all - scored, "empty_cells": empty,
            "point": point, "ci95": ci,
            "confusion_weighted": {f"{p}->{r}": round(v, 1) for (p, r), v in sorted(t.items())}}


def cells_for(groups: Mapping[str, tuple[float, list[dict[str, Any]]]], lab: Mapping[str, Mapping[str, Any]],
              ref: str, pred: str = PRODUCTION) -> list[Cell]:
    return [(n_pop, [(lab[pred][it["item_id"]], lab[ref][it["item_id"]]) for it in its])
            for _, (n_pop, its) in sorted(groups.items())]


def pooled_estimate(groups: Mapping[str, tuple[float, list[dict[str, Any]]]], lab: Mapping[str, Mapping[str, Any]],
                    ref: str, n_pop: Mapping[str, int]) -> dict[str, Any]:
    """The pooled estimate: per stratum h one cell of N_h with the scored items of both samples; the bootstrap
    resamples the triage_w part and the jtri part of each stratum separately."""
    strata = list(n_pop)
    parts = {k: [(lab[PRODUCTION][it["item_id"]], lab[ref][it["item_id"]]) for it in its]
             for k, (_, its) in groups.items()}

    def cells(pp: dict[str, list[tuple[Any, Any]]]) -> list[Cell]:
        return [(float(n_pop[h]), pp[f"w:{h}"] + pp[f"t:{h}"]) for h in strata]
    t, scored, empty = totals(cells(parts))
    point = tw_metrics(t)
    rng = random.Random(BOOT_SEED)
    reps: dict[str, list[float]] = collections.defaultdict(list)
    for _ in range(BOOT_N):
        bp = {k: [v[rng.randrange(len(v))] for _ in v] for k, v in parts.items()}
        for key, x in tw_metrics(totals(cells(bp))[0]).items():
            if x is not None:
                reps[key].append(x)
    ci = {}
    for key, v in reps.items():
        if len(v) >= BOOT_N // 2:
            v.sort()
            ci[key] = [v[int(0.025 * len(v))], v[int(0.975 * len(v)) - 1]]
    n_all = sum(len(v) for v in parts.values())
    return {"n_items": n_all, "n_scored": scored, "n_dropped": n_all - scored, "empty_cells": empty, "point": point,
            "ci95": ci, "confusion_weighted": {f"{p}->{r}": round(v, 1) for (p, r), v in sorted(t.items())}}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    c = z * z / n
    mid, half = (p + c / 2) / (1 + c), z * ((p * (1 - p) / n + c / (4 * n)) ** 0.5) / (1 + c)
    return max(0.0, mid - half), min(1.0, mid + half)


def verdict_labels(items: Iterable[dict[str, Any]], production: Mapping[str, str | None],
                   judge_verdicts: Mapping[str, Mapping[str, str | None]], judges: Sequence[str] = JUDGES
                   ) -> dict[str, dict[str, str | None]]:
    """labels[source][item] for production, each judge, the consensus and (three judges) the majority."""
    ids = [it["item_id"] for it in items]
    lab: dict[str, dict[str, str | None]] = {PRODUCTION: {i: production.get(i) for i in ids}}
    for j in judges:
        lab[j] = {i: judge_verdicts[j].get(i) for i in ids}
    a, b = PAIR
    lab["consensus"] = {i: lab[a][i] if lab[a][i] is not None and lab[a][i] == lab[b][i] else None for i in lab[a]}
    if len(judges) == 3:
        maj = {}
        for i in lab[a]:
            votes = [lab[j][i] for j in judges if lab[j][i] is not None]
            top = collections.Counter(votes).most_common(1)
            maj[i] = top[0][0] if top and top[0][1] >= 2 else None
        lab["majority"] = maj
    return lab


def tw_refs(judges: Sequence[str]) -> list[str]:
    return [*judges, "consensus", *(["majority"] if len(judges) == 3 else [])]


def triage_w_scores(iw: list[dict[str, Any]], it_: list[dict[str, Any]], lab: Mapping[str, Mapping[str, Any]],
                    design_w: Mapping[str, Any], design_t: Mapping[str, Any], judges: Sequence[str] = JUDGES
                    ) -> dict[str, Any]:
    """triage_w's scores (``score_triage_w.py`` docstring): ``iw`` = triage_w items with ``stratum`` (a
    :data:`TW_STRATA` name), ``pred``, ``conf``, ``weight``, ``metadata``; ``it_`` = triage items with ``stratum``
    (basis|verdict), ``w_stratum`` (their triage_w stratum), ``pred``, ``conf``. ``design_w``: ``eligible_by_stratum``,
    ``pooled_frame_by_stratum``, ``weights``, ``n_h``, ``cut_points``; ``design_t``: ``eligible_by_stratum`` of the
    triage set's strata that are scored."""
    strata = list(TW_STRATA)
    designs: dict[str, dict[str, tuple[float, list[dict[str, Any]]]]] = {
        "triage_w": {h: (design_w["eligible_by_stratum"][h], [it for it in iw if it["stratum"] == h]) for h in strata},
        "pooled": {**{f"w:{h}": (0.0, [it for it in iw if it["stratum"] == h]) for h in strata},
                   **{f"t:{h}": (0.0, [it for it in it_ if it["w_stratum"] == h]) for h in strata}},
        "triage": {h: (n, [it for it in it_ if it["stratum"] == h]) for h, n in design_t["eligible_by_stratum"].items()},
    }
    res: dict[str, Any] = {"judges": list(judges), "estimates": {}}
    for dname, groups in designs.items():
        res["estimates"][dname] = {}
        for ref in tw_refs(judges):
            if dname == "pooled":
                est = pooled_estimate(designs["pooled"], lab, ref, design_w["pooled_frame_by_stratum"])
            else:
                est = estimate(cells_for(groups, lab, ref))
            res["estimates"][dname][ref] = est
    res["judge_pairs_triage_w"] = {}
    for x, y in itertools.combinations(judges, 2):
        e = estimate(cells_for(designs["triage_w"], lab, y, pred=x))
        both = [it["item_id"] for it in iw if lab[x][it["item_id"]] and lab[y][it["item_id"]]]
        res["judge_pairs_triage_w"][f"{x}_vs_{y}"] = {
            "n": len(both), "raw_agreement": _div(sum(lab[x][i] == lab[y][i] for i in both), len(both)),
            "weighted_agreement": e["point"]["agreement"], "weighted_agreement_ci95": e["ci95"].get("agreement")}
    raw = {}
    for h in strata:
        its = [it for it in iw if it["stratum"] == h]
        raw[h] = {"N_h": design_w["eligible_by_stratum"][h], "n_h": len(its), "production": h.split("|")[0],
                  "jtri_items": sum(it["w_stratum"] == h for it in it_),
                  **{ref: dict(collections.Counter(str(lab[ref][it["item_id"]]) for it in its)) for ref in tw_refs(judges)},
                  "jtri": {ref: dict(collections.Counter(str(lab[ref][it["item_id"]]) for it in it_
                                                         if it["w_stratum"] == h)) for ref in tw_refs(judges)}}
    res["per_stratum_raw"] = raw
    main_ref = "majority" if len(judges) == 3 else "consensus"
    res["missed_by_stratum"] = {}
    for ref in ("consensus", main_ref):
        for h in TW_NOT_RELEVANT:
            for src, its, n_pop in (("triage_w", [it for it in iw if it["stratum"] == h],
                                     design_w["eligible_by_stratum"][h]),
                                    ("pooled", [it for it in [*iw, *it_] if (it.get("w_stratum") or it["stratum"]) == h],
                                     design_w["pooled_frame_by_stratum"][h])):
                sc = [lab[ref][it["item_id"]] for it in its if lab[ref][it["item_id"]] is not None]
                k = sum(v in KEEP for v in sc)
                kr = sum(v == "relevant" for v in sc)
                lo, hi = wilson(k, len(sc))
                lo_r, hi_r = wilson(kr, len(sc))
                res["missed_by_stratum"][f"{ref}|{src}|{h}"] = {
                    "scored": len(sc), "keep": k, "relevant": kr, "N_h": n_pop,
                    "missed_keep": n_pop * k / len(sc) if sc else None,
                    "missed_keep_wilson95": [n_pop * lo, n_pop * hi],
                    "missed_relevant": n_pop * kr / len(sc) if sc else None,
                    "missed_relevant_wilson95": [n_pop * lo_r, n_pop * hi_r]}
    fns, dis = [], []
    for it in [*iw, *it_]:
        i = it["item_id"]
        vals = {s: lab[s][i] for s in (PRODUCTION, *judges)}
        row = {"item_id": i, "judgement": "verdict", **vals, "consensus": lab["consensus"][i],
               **({"majority": lab["majority"][i]} if "majority" in lab else {}),
               "stratum": it.get("w_stratum") or it["stratum"], "production_confidence": it["conf"],
               "weight": it["weight"], "title": (it["metadata"].get("title") or "")[:90]}
        present = [v for v in vals.values() if v is not None]
        if i.startswith("jtrw") and len(set(present)) > 1:
            dis.append(row)
        if it["pred"] == "not_relevant" and lab[main_ref][i] in KEEP:
            fns.append(row)
    res["false_negative_items"] = fns
    res["disagreements"] = dis
    res["design"] = {"strata": {h: {"N_h": design_w["eligible_by_stratum"][h], "n_h": design_w["n_h"][h],
                                    "weight": design_w["weights"][h],
                                    "pooled_N_h": design_w["pooled_frame_by_stratum"][h]} for h in strata},
                     "cut_points": design_w["cut_points"], "boot": {"n": BOOT_N, "seed": BOOT_SEED}}
    return res


# ---------------------------------------------------------------- eval sets on disk
def read_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in Path(p).read_text("utf-8").splitlines() if x.strip()]


def norm_title(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def eval_keys(files: Iterable[Path]) -> tuple[set[str], set[str], set[str]]:
    """Doc ids, text digests and normalised titles (> 15 chars) of the items of ``files`` (``items*.jsonl`` of
    earlier eval sets; ``common.existing_eval_keys``' key set)."""
    ids: set[str] = set()
    digs: set[str] = set()
    titles: set[str] = set()
    for f in files:
        for line in Path(f).read_text("utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("doc_id"):
                ids.add(r["doc_id"])
            si = r.get("source_item") or ""
            if si.startswith("mirror:"):
                ids.add(si.split(":", 1)[1])
            inp = r.get("input") or {}
            digs |= {inp.get("text_digest"), inp.get("full_text_digest"), r.get("text_digest")} - {None}
            for t in (inp.get("title"), r.get("title")):
                nt = norm_title(t)
                if len(nt) > 15:
                    titles.add(nt)
    return ids, digs, titles


#: The keys of the earlier eval sets that triage_w's frame excludes (``triage_w/sampling.json``
#: ``existing_eval_files``), as a table in the judge eval family (D795): several of those sets' items carry long
#: third-party excerpts and are archived outside the repository, and a release must not need them.
EXISTING_KEYS = "triage_w/existing-eval-keys.json"


def existing_keys_table(repo_root: Path, files: Iterable[str]) -> dict[str, Any]:
    """:data:`EXISTING_KEYS`'s content from the files themselves: each file's path and sha256 (its pin) and the
    sorted doc ids, text digests and normalised titles :func:`eval_keys` reads from them."""
    import hashlib

    from stack.evals.freeze import find_eval_file

    paths = []
    pins = []
    for f in files:
        p = find_eval_file(repo_root / "evals", f.removeprefix("evals/"))
        if p is None:
            raise QualityError(f"judge eval: {f} is in neither the repository nor the eval-text cache")
        paths.append(p)
        pins.append({"path": f, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    ids, digs, titles = eval_keys(paths)
    return {"files": pins, "doc_ids": sorted(ids), "digests": sorted(digs), "titles": sorted(titles)}


FAMILY_SETS = ("triage", "screen", "affiliation", "model_roles", "technique_staged")


def family_keys(set_root: Path) -> tuple[set[str], set[str], set[str]]:
    """Doc ids, text sha256s and normalised titles (> 15 chars) of every item of the five proportional sets
    (``triage_w.py``: excluded from triage_w's frame)."""
    ids, digs, titles = set(), set(), set()
    for s in FAMILY_SETS:
        for r in read_jsonl(set_root / s / "sample.jsonl"):
            ids.add(r["doc_id"])
            if r.get("text_sha256"):
                digs.add(r["text_sha256"])
        for r in read_jsonl(set_root / s / "items.jsonl"):
            ids.add(r["doc_id"])
            t = norm_title((r.get("metadata") or {}).get("title"))
            if len(t) > 15:
                titles.add(t)
    return ids, digs, titles


def excluded_by(r: Mapping[str, Any], keys: tuple[set[str], set[str], set[str]]) -> str | None:
    """Why frame row ``r`` matches an eval key set (doc id incl. former / same-raw ids, text sha256, title)."""
    ids, digs, titles = keys
    k = {r["doc_id"], *(r["former_doc_ids"] or []), *(r["same_raw_doc_ids"] or [])}
    if k & ids:
        return "doc_id"
    if r["text_sha256"] and r["text_sha256"] in digs:
        return "text_sha256"
    t = norm_title(r["title"])
    if len(t) > 15 and t in titles:
        return "title"
    return None


@dataclass
class EvalSet:
    """One judge eval set family on disk (``evals/judge-2026-10-25j``): items, the judges' released labels, and the
    files read (path, sha256) for the release record."""

    root: Path
    rel: str
    judges: tuple[str, ...] = JUDGES
    files: list[dict[str, str]] = field(default_factory=list)

    def _read(self, rel: str) -> bytes:
        p = self.root / rel
        if not p.is_file():
            raise QualityError(f"judge eval: {self.rel}/{rel} is missing")
        b = p.read_bytes()
        import hashlib

        e = {"path": f"{self.rel}/{rel}", "sha256": hashlib.sha256(b).hexdigest()}
        if e not in self.files:
            self.files.append(e)
        return b

    def jsonl(self, rel: str) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self._read(rel).decode("utf-8").splitlines() if x.strip()]

    def json(self, rel: str) -> Any:
        return json.loads(self._read(rel))

    def items(self, s: str) -> list[dict[str, Any]]:
        return self.jsonl(f"{s}/items.jsonl")

    def judge_labels(self, s: str) -> dict[str, dict[tuple[str, str], Any]]:
        """judge -> (item, call) -> label, the usable records of ``<set>/labels-<judge>.jsonl``; model_roles labels
        are the normalised answers of :data:`NORMALIZED_MR` (``{"models": [...]}``)."""
        out: dict[str, dict[tuple[str, str], Any]] = {}
        for j in self.judges:
            out[j] = {(r["item_id"], r["call"]): r["label"] for r in self.jsonl(f"{s}/labels-{j}.jsonl")
                      if r["status"] in USABLE}
        if s == "model_roles":
            norm = {(r["judge"], r["item_id"]): r for r in self.jsonl(f"{s}/{NORMALIZED_MR}")}
            for j in self.judges:
                for (i, call) in list(out[j]):
                    r = norm.get((j, i))
                    if r is None:
                        raise QualityError(f"judge eval: {self.rel}/{s}/{NORMALIZED_MR} has no row for {j} {i}")
                    out[j][(i, call)] = {"models": r["models"]}
        return out

    def normalisation(self) -> dict[str, int]:
        """score.py's ``judge_normalisation`` counts, from :data:`NORMALIZED_MR`."""
        c: collections.Counter[str] = collections.Counter()
        for r in self.jsonl(f"model_roles/{NORMALIZED_MR}"):
            if r["judge"] not in self.judges:
                continue
            if r.get("normalize_failed"):
                c[f"{r['judge']}:normalize_failed:{r['normalize_failed']}"] += 1
            else:
                c[f"{r['judge']}:repaired_docs"] += bool(r["repaired"])
        return dict(c)


# ---------------------------------------------------------------- the build's tables
DOC_COLUMNS = ["doc_id", "state", "metadata.title", "identity.former_doc_ids", "content.same_raw_doc_ids",
               "content.current_text_sha256", "annotations.triage"]
EV_COLUMNS = ["evidence_id", "doc_id", "status", "verdict", "confidence", "basis", "basis_digest", "prompt_name"]


@dataclass
class BuildTables:
    """What re-scoring and triage-vs-screen read of one build (module docstring, "The build's tables")."""

    build_id: str
    documents: pa.Table
    documents_file: str
    evidence: pa.Table
    evidence_file: str
    question_digest: str
    plan: str
    plan_digest_matches: bool
    corpus: pa.Table  # doc_id, annotations and metadata.doc_type of the build's corpus (fold.build)

    def record(self) -> dict[str, Any]:
        return {"build": self.build_id, "documents": self.documents_file, "triage_evidence": self.evidence_file,
                "triage_question_digest": self.question_digest, "plan": self.plan,
                "plan_digest_matches_build": self.plan_digest_matches}


def load_build(store_root: Path, build_rec: Mapping[str, Any], corpus: pa.Table) -> BuildTables:
    """The build's ``documents`` file (the one its fold.corpus run read), the ``triage_evidence`` head of that
    series, the triage question's current digest, and ``corpus`` (the build's corpus table, as read by the export:
    its ``doc_id``, ``annotations`` and ``metadata.doc_type`` are kept)."""
    from stack import gate
    from stack.plan import load_plan
    from stack.store.chain import read_view, resolve
    from stack.store.index import store_index

    bid = str(build_rec["build_id"])
    files = build_rec.get("files") or {}
    ci = files.get("corpus_input") or {}
    idx = store_index(store_root)
    run = next((e for e in idx.runs if e.run_id == ci.get("run_id")), None)
    if run is None:
        raise QualityError(f"build {bid}: the corpus_input run {ci.get('run_id')!r} has no run record in the store")
    doc_fid = (run.record.inputs or {}).get("documents")
    out = idx.output(doc_fid) if doc_fid else None
    if out is None:
        raise QualityError(f"build {bid}: its fold.corpus run names no readable documents file ({doc_fid!r})")
    dkey = str(out[2]["path"])
    docs = read_view(resolve(store_root, dkey, verify_ids=False), columns=DOC_COLUMNS)
    series = dkey.split("/")[2]
    ev_chain = resolve(store_root, f"data/triage_evidence/{series}/", verify_ids=False)
    ev = read_view(ev_chain, columns=EV_COLUMNS)
    plan_name = str(build_rec.get("plan") or "")
    pf = REPO_ROOT / "configs" / "plans" / f"{plan_name}.yaml"
    if not pf.is_file():
        raise QualityError(f"build {bid}: its plan {plan_name!r} is not in configs/plans/ of this checkout")
    plan = load_plan(pf)
    qd = [q.config_digest for q in gate.questions(plan) if q.name == QUESTION]
    if len(qd) != 1:
        raise QualityError(f"plan {plan_name}: no single {QUESTION} question")
    meta = corpus.column("metadata").combine_chunks()
    slim = pa.table({"doc_id": corpus.column("doc_id"), "annotations": corpus.column("annotations"),
                     "metadata": pa.StructArray.from_arrays([meta.field("doc_type")], ["doc_type"])})
    return BuildTables(bid, docs, str(doc_fid), ev, ev_chain.head, qd[0], plan_name,
                       plan.plan_digest == build_rec.get("plan_digest"), slim)


def _duck(bt: BuildTables) -> Any:
    from stack import duck

    con = duck.connect()
    con.execute("SET threads=2")
    con.register("docs", bt.documents)
    con.register("ev", bt.evidence)
    con.register("corpus", bt.corpus)
    con.execute(f"""create temp table tri as select d.doc_id, d.state, d.metadata.title as title,
        d.identity.former_doc_ids as former_doc_ids, d.content.same_raw_doc_ids as same_raw_doc_ids,
        d.content.current_text_sha256 as text_sha256,
        list_filter(d.annotations.triage.payload.questions, lambda x: x.question = '{QUESTION}')[1] as q
      from docs d""")
    return con


TRI_SQL = """select t.doc_id, t.state, t.title, t.former_doc_ids, t.same_raw_doc_ids, t.text_sha256,
    t.q.status as q_status, t.q.error_class as q_error_class, t.q.verdict as q_verdict, t.q.confidence as q_confidence,
    t.q.evidence_id as evidence_id, t.q.config_digest as q_digest, e.status as ev_status, e.verdict as ev_verdict,
    e.confidence as ev_confidence, e.basis as basis, e.basis_digest as basis_digest
  from tri t left join ev e on e.evidence_id = t.q.evidence_id"""


def _triage_reason(r: Mapping[str, Any]) -> str | None:
    """``sample.in_population('triage', r)`` on a joined row: None in the population, else why not."""
    if r["state"] != "acquired":
        return f"state_{r['state']}"
    if r["q_verdict"] is None or r["q_status"] != "ok" or r["ev_status"] != "ok" or r["basis"] is None:
        return "not_llm_decision"
    return None


@dataclass
class TriageFrame:
    """The triage population of a build by stratum (both designs) and the joined rows of the items' documents."""

    w_eligible: dict[str, int]
    w_pooled: dict[str, int]
    t_eligible: dict[str, int]
    population: int
    excluded: dict[str, int]
    rows: dict[str, dict[str, Any]]  # doc_id -> joined row, for the items


def triage_frame(con: Any, existing: tuple[set[str], set[str], set[str]], family: tuple[set[str], set[str], set[str]],
                 want: set[str]) -> TriageFrame:
    """The frame of ``sample.py`` / ``triage_w.py`` on this build: population rows (:func:`_triage_reason`) minus
    the earlier eval sets' documents (``existing``: triage set and pooled frame) and, for triage_w, the five sets'
    own documents (``family``); and the joined rows of ``want`` (the items' doc ids), in or out of the population."""
    w_el: collections.Counter[str] = collections.Counter()
    w_po: collections.Counter[str] = collections.Counter()
    t_el: collections.Counter[str] = collections.Counter()
    excl: collections.Counter[str] = collections.Counter()
    rows: dict[str, dict[str, Any]] = {}
    pop = 0
    reader = con.execute(TRI_SQL).to_arrow_reader(50_000)
    for batch in reader:
        for r in batch.to_pylist():
            if r["doc_id"] in want:
                rows[r["doc_id"]] = r
            if _triage_reason(r):
                continue
            pop += 1
            ws = stratum_of(r["q_verdict"], r["ev_confidence"])
            x = excluded_by(r, existing)
            if x:
                excl[f"in_existing_eval:{x}"] += 1
                continue
            w_po[ws] += 1
            t_el[f"{r['basis']}|{r['q_verdict']}"] += 1
            x = excluded_by(r, family)
            if x:
                excl[f"in_2026-10-25j_set:{x}"] += 1
                continue
            w_el[ws] += 1
    return TriageFrame(dict(w_el), dict(w_po), dict(t_el), pop, dict(sorted(excl.items())), rows)


def corpus_rows(bt: BuildTables, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The corpus rows (doc_id, annotations) of ``doc_ids`` as Python dicts."""
    want = sorted(set(doc_ids))
    t = bt.corpus.filter(pc.is_in(bt.corpus.column("doc_id"), value_set=pa.array(want, pa.string())))
    return {r["doc_id"]: r for r in t.to_pylist()}


def _plain(obj: Any) -> Any:
    import datetime as dt

    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    return obj


def _ann(row: Mapping[str, Any], name: str) -> dict[str, Any]:
    return ((row.get("annotations") or {}).get(name)) or {}


def corpus_reason(set_name: str, row: Mapping[str, Any] | None) -> str | None:
    """``sample.in_population`` for the corpus sets, on a corpus row (None: in the population). A model_roles /
    technique_staged annotation without an evidence id (an override supplied it: the Claude stand-ins,
    ``sample.py``'s ``claude_standin``) is out as ``<mr|ts>_<status>_no_evidence``."""
    if row is None:
        return "missing"
    if set_name == "screen":
        a = _ann(row, "screen")
        if a.get("status") != "ok":
            return f"screen_{a.get('status')}"
        return None if a.get("origin") == "ai_check" else f"screen_origin_{a.get('origin')}"
    if set_name == "affiliation":
        a = _ann(row, "affiliation")
        if not a.get("status"):
            return "no_affiliation"
        if a["status"] != "ok":
            return f"affiliation_{a['status']}"
        return None if a.get("origin") == "ai_check" else f"affiliation_origin_{a.get('origin')}"
    a = _ann(row, set_name)
    p = "mr" if set_name == "model_roles" else "ts"
    if not a.get("status"):
        return "not_annotated"
    if a["status"] not in ("ok", "empty") or a.get("origin") != "ai_check":
        return f"{p}_{a['status']}_{a.get('origin')}"
    return None if a.get("evidence_id") else f"{p}_{a['status']}_no_evidence"


def corpus_label(set_name: str, row: Mapping[str, Any], call: str) -> dict[str, Any]:
    """The production label of a corpus row as ``freeze.py`` writes it to ``production.jsonl`` (the fields the
    scorers read; verbatim strings capped)."""
    def pl(name: str) -> dict[str, Any]:
        return dict(cap_verbatim(_plain(_ann(row, name).get("payload") or {})))
    if set_name == "screen":
        if call == "relevance":
            p = pl("screen")
            return {"verdict": p.get("verdict"), "stack": pl("stack_relevance"), "sr": pl("sr_relevance")}
        return {"usability": pl("text_usability"), "doc_class": pl("doc_class")}
    p = pl(set_name)
    p.pop("prepass", None)
    return p


@dataclass
class SetLabels:
    """One set's kept items, production labels on them, and the dropped items by reason."""

    items: list[dict[str, Any]]
    production: dict[tuple[str, str], Any]
    dropped: dict[str, int]
    n_items: int


def production_labels(set_name: str, items: list[dict[str, Any]], bt: BuildTables, *,
                      triage_rows: Mapping[str, Mapping[str, Any]] | None = None,
                      corpus: Mapping[str, Mapping[str, Any]] | None = None,
                      original_evidence: Mapping[str, Mapping[str, Any]] | None = None) -> SetLabels:
    """Module docstring, "Production labels": the kept items (triage items re-assigned to their stratum in this
    build), their labels by (item, call), and the drops by reason."""
    kept: list[dict[str, Any]] = []
    prod: dict[tuple[str, str], Any] = {}
    dropped: collections.Counter[str] = collections.Counter()
    for it in items:
        i, d = it["item_id"], it["doc_id"]
        if set_name in ("triage", "triage_w"):
            r = (triage_rows or {}).get(d)
            why = "missing" if r is None else _triage_reason(r)
            if why is None:
                assert r is not None
                ev0 = (original_evidence or {}).get(it["verification"]["evidence_id"])
                if r["evidence_id"] != it["verification"]["evidence_id"] and (
                        ev0 is None or (ev0["basis"], ev0["basis_digest"]) != (r["basis"], r["basis_digest"])):
                    why = "input_changed"
                elif r["ev_verdict"] != r["q_verdict"]:  # the documents row and its evidence disagree: not scored
                    why = "verdict_mismatch"
            if why:
                dropped[why] += 1
                continue
            assert r is not None
            new = dict(it, pred=r["ev_verdict"], conf=r["ev_confidence"], basis=r["basis"])
            if set_name == "triage_w":
                new["stratum"] = stratum_of(r["q_verdict"], r["ev_confidence"])
            else:
                new["stratum"] = f"{r['basis']}|{r['q_verdict']}"
                new["w_stratum"] = stratum_of(r["q_verdict"], r["ev_confidence"])
            kept.append(new)
            prod[(i, "triage")] = {"verdict": r["ev_verdict"], "confidence": r["ev_confidence"]}
            continue
        row = (corpus or {}).get(d)
        why = corpus_reason(set_name, row)
        if why is None:
            assert row is not None
            if _ann(row, CORPUS_SETS[set_name]).get("basis_digest") != it["input"]["text_sha256"]:
                why = "input_changed"
        if why:
            dropped[why] += 1
            continue
        assert row is not None
        new = dict(it)
        if set_name == "screen":
            new["doc_class_rule"] = screen_rule(row)
        kept.append(new)
        for call in it["calls"]:
            prod[(i, call)] = corpus_label(set_name, row, call)
    return SetLabels(kept, prod, dict(sorted(dropped.items())), len(items))


def screen_rule(row: Mapping[str, Any]) -> dict[str, Any]:
    """The doc_class rule of a corpus row (``freeze.freeze_screen``): the channel's doc_type (not scored) and the
    metadata's paper/preprint answer."""
    sm = _workflow("_rq_screen_main", "annotate/screen/main.py")
    ct = sm.channel_doc_type(row)
    return {"channel_doc_type": ct, "metadata_paper_type": sm.metadata_paper_type(row), "model_judgement": ct is None}


# ---------------------------------------------------------------- re-scoring
def _getter(prod: Mapping[tuple[str, str], Any], judges: Mapping[str, Mapping[tuple[str, str], Any]]) -> Get:
    def get(s: str, i: str, call: str) -> Any:
        return prod.get((i, call)) if s == PRODUCTION else judges[s].get((i, call))
    return get


def rescore(repo_root: Path, rel: str, bt: BuildTables) -> dict[str, Any]:
    """Every set of the judge eval family ``repo_root / rel`` scored against build ``bt``
    (module docstring, "Re-scoring"): ``{"sets": {set: scores as in its scores.json}, "coverage": {set: {items,
    scored, dropped}}, "judge_build", "build", "files"}``."""
    eval_root = repo_root / rel
    if not eval_root.is_dir():
        raise QualityError(f"judge eval: {rel} is not a directory of the repo")
    ev = EvalSet(eval_root, rel)
    builds = {ev.json(f"{s}/manifest.json")["build"] for s in SETS}
    if len(builds) != 1:
        raise QualityError(f"judge eval: {rel}: the sets name different builds {sorted(builds)}")
    items = {s: ev.items(s) for s in SETS}
    smp_w = ev.json("triage_w/sampling.json")
    keys = ev.json(EXISTING_KEYS)
    if sorted(e["path"] for e in keys["files"]) != sorted(smp_w["existing_eval_files"]):
        raise QualityError(f"judge eval: {EXISTING_KEYS} does not cover triage_w/sampling.json's existing_eval_files")
    existing = (set(keys["doc_ids"]), set(keys["digests"]), set(keys["titles"]))
    family = family_keys(eval_root)
    con = _duck(bt)
    want = {it["doc_id"] for s in ("triage", "triage_w") for it in items[s]}
    fr = triage_frame(con, existing, family, want)
    ev0_ids = sorted({it["verification"]["evidence_id"] for s in ("triage", "triage_w") for it in items[s]})
    ev0 = {r["evidence_id"]: r for r in bt.evidence.filter(
        pc.is_in(bt.evidence.column("evidence_id"), value_set=pa.array(ev0_ids, pa.string()))).to_pylist()}
    corpus = corpus_rows(bt, {it["doc_id"] for s in CORPUS_SETS for it in items[s]})
    out_sets: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    labs: dict[str, SetLabels] = {}
    for s in SETS:
        labs[s] = production_labels(s, items[s], bt, triage_rows=fr.rows, corpus=corpus, original_evidence=ev0)
        coverage[s] = {"items": labs[s].n_items, "scored": len(labs[s].items), "dropped": labs[s].dropped}
    for s in ("triage", "screen", "affiliation", "model_roles", "technique_staged"):
        sl = labs[s]
        get = _getter(sl.production, ev.judge_labels(s))
        if s == "triage":
            res = score_triage(sl.items, get)
        elif s == "screen":
            res = score_screen(sl.items, get, {it["item_id"]: it["doc_class_rule"] for it in sl.items}, windows=False)
        elif s == "affiliation":
            res = score_affiliation(sl.items, get)[0]
        elif s == "model_roles":
            res = score_model_roles(sl.items, get, normalisation=ev.normalisation())[0]
        else:
            res = score_technique(sl.items, get)[0]
        out_sets[s] = {"set": s, "scores": strip_labels(res)}
    out_sets["triage_w"] = _rescore_triage_w(ev, labs["triage_w"], labs["triage"], fr, smp_w)
    return {"judge_eval": rel, "judge_build": builds.pop(), "build": bt.record(), "sets": out_sets,
            "coverage": coverage,
            "triage_frame": {"population": fr.population, "excluded": fr.excluded}, "files": ev.files}


def _rescore_triage_w(ev: EvalSet, w: SetLabels, t: SetLabels, fr: TriageFrame, smp_w: Mapping[str, Any]
                      ) -> dict[str, Any]:
    jw, jt = ev.judge_labels("triage_w"), ev.judge_labels("triage")
    prod = {i: lab["verdict"] for (i, _c), lab in {**w.production, **t.production}.items()}
    judge_v = {j: {i: (lab or {}).get("verdict") for (i, _c), lab in {**jw[j], **jt[j]}.items()} for j in JUDGES}
    lab = verdict_labels([*w.items, *t.items], prod, judge_v)
    n_h = {h: sum(it["stratum"] == h for it in w.items) for h in TW_STRATA}
    eligible = {h: fr.w_eligible.get(h, 0) for h in TW_STRATA}
    design_w = {"eligible_by_stratum": eligible, "pooled_frame_by_stratum": {h: fr.w_pooled.get(h, 0) for h in TW_STRATA},
                "n_h": n_h, "weights": {h: round(eligible[h] / n_h[h], 4) if n_h[h] else None for h in TW_STRATA},
                "cut_points": smp_w["confidence_cut_points"]}
    t_strata = sorted({it["stratum"] for it in t.items})
    design_t = {"eligible_by_stratum": {h: fr.t_eligible.get(h, 0) for h in t_strata}}
    res = triage_w_scores(w.items, t.items, lab, design_w, design_t)
    res.pop("disagreements")
    res.pop("false_negative_items")
    return res


# ---------------------------------------------------------------- triage vs screen
#: Triage confidence bins of the report (inclusive upper bounds, rounded to 0.01).
CONF_BINS = ((0.84, "<=0.84"), (0.94, "0.85-0.94"), (0.98, "0.95-0.98"), (1.0, ">=0.99"))
TVS_NOTE = ("Agreement between two pipeline stages, not accuracy: the screen is itself a model judgement. "
            "Decisions are the documents triage chose to fetch (mostly fetch), so their recall says little; "
            "signals (D724: documents fetched for other reasons, asked anyway) are the unbiased part.")


def conf_bin(c: float | None) -> str:
    if c is None:
        return "none"
    x = round(c, 2)
    return next(name for hi, name in CONF_BINS if x <= hi)


def _cell(c: Mapping[str, int]) -> dict[str, Any]:
    tp, fp, fn, tn = (c.get(k, 0) for k in ("tp", "fp", "fn", "tn"))
    p = prf(tp, fp, fn)
    return {"n": tp + fp + fn + tn, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": p["precision"],
            "recall": p["recall"], "f1": p["f1"]}


def triage_vs_screen(bt: BuildTables) -> dict[str, Any]:
    """Module docstring, "Triage vs screen"."""
    con = _duck(bt)
    rows = con.execute("""
      select t.q.status as q_status, t.q.error_class as q_error_class, t.q.verdict as verdict,
        t.q.confidence as confidence, t.q.config_digest = ? as current, e.basis as basis,
        c.annotations.screen.payload.verdict as screen
      from corpus c left join tri t using (doc_id) left join ev e on e.evidence_id = t.q.evidence_id
      where c.annotations.screen.status = 'ok' and c.annotations.screen.origin = 'ai_check'
        and c.annotations.screen.payload.verdict is not null""", [bt.question_digest]).fetchall()
    from stack import gate

    cov: collections.Counter[str] = collections.Counter()
    groups: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for q_status, q_err, verdict, conf, current, basis, screen in rows:
        cov["screen_verdicts"] += 1
        if q_status is None:
            cov["no_triage_question"] += 1
            continue
        a = {"status": q_status, "error_class": q_err, "verdict": verdict}
        v = gate.verdict_of(a)
        if v is None:
            cov["no_triage_verdict"] += 1
            continue
        if not current:
            cov["older_question_digest"] += 1
            continue
        kind = "decision" if q_status == "ok" else "signal" if gate.is_signal(a) else "other"
        cov[f"compared:{kind}"] += 1
        t_ = v in KEEP
        s_ = screen in KEEP
        cell = "tp" if t_ and s_ else "fp" if t_ else "fn" if s_ else "tn"
        for g in ("all", f"basis:{basis or 'none'}", f"confidence:{conf_bin(conf)}", f"kind:{kind}"):
            groups[g][cell] += 1
        groups[f"verdicts:{v}->{screen}"]["n"] += 1
    order = {**{f"kind:{k}": n for n, k in enumerate(("decision", "signal", "other"))},
             **{f"confidence:{name}": n for n, (_hi, name) in enumerate((*CONF_BINS, (None, "none")))}}
    by = {g: _cell(groups[g]) for g in sorted((g for g in groups if not g.startswith("verdicts:")),
                                              key=lambda g: (g.split(":")[0], order.get(g, 0), g))}
    return {"note": TVS_NOTE, **bt.record(),
            "definition": "fetch = triage verdict relevant|borderline; keep = screen verdict relevant|borderline; "
                          "documents with a current-digest triage verdict (decision or signal) and an ai_check "
                          "screen verdict",
            "coverage": dict(sorted(cov.items())), "overall": by.get("all", _cell({})),
            "by_basis": {g.split(":", 1)[1]: c for g, c in by.items() if g.startswith("basis:")},
            "by_confidence": {g.split(":", 1)[1]: c for g, c in by.items() if g.startswith("confidence:")},
            "by_kind": {g.split(":", 1)[1]: c for g, c in by.items() if g.startswith("kind:")},
            "verdict_table": {g.split(":", 1)[1]: c["n"] for g, c in sorted(groups.items())
                              if g.startswith("verdicts:")}}


def triage_vs_screen_md(r: Mapping[str, Any]) -> str:
    """A markdown report of :func:`triage_vs_screen`."""
    def f(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.3f}"

    def line(name: str, c: Mapping[str, Any]) -> str:
        return (f"| {name} | {c['n']:,} | {c['tp']:,} | {c['fp']:,} | {c['fn']:,} | {c['tn']:,} | {f(c['precision'])} "
                f"| {f(c['recall'])} | {f(c['f1'])} |")
    head = "| group | n | tp | fp | fn | tn | precision | recall | F1 |\n|---|---|---|---|---|---|---|---|---|"
    L = [f"# Triage vs screen, build `{r['build']}`", "", r["note"], "", r["definition"] + ".", "",
         "Coverage: " + ", ".join(f"{k} {v:,}" for k, v in r["coverage"].items()), "", head,
         line("all", r["overall"])]
    for key in ("by_kind", "by_basis", "by_confidence"):
        L += [line(f"{key.removeprefix('by_')}: {g}", c) for g, c in r[key].items()]
    L += ["", "Triage verdict -> screen verdict: " + ", ".join(f"{k} {v:,}" for k, v in r["verdict_table"].items()),
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------- reproduction check
def diff_scores(a: Any, b: Any, path: str = "") -> list[str]:
    """Paths where two scores dicts differ (``only in rescored`` / ``only in committed`` / values)."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path} > {k}" if path else str(k)
            if k not in b:
                out.append(f"only in rescored: {p}")
            elif k not in a:
                out.append(f"only in committed: {p}")
            else:
                out += diff_scores(a[k], b[k], p)
        return out
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b):
        for n, (x, y) in enumerate(zip(a, b)):
            out += diff_scores(x, y, f"{path}[{n}]")
        return out
    if a != b and not (isinstance(a, float) and isinstance(b, float) and abs(a - b) < 1e-9):
        out.append(f"differs: {path}: rescored {a!r}, committed {b!r}")
    return out


def compare_committed(eval_root: Path, r: Mapping[str, Any]) -> dict[str, list[str]]:
    """Per set, :func:`diff_scores` of the re-scored dict against the committed ``<set>/scores.json`` (minus the
    keys re-scoring does not write: ``status``, and triage_w's ``status`` / ``false_negative_items``)."""
    out = {}
    for s, mine in r["sets"].items():
        com = json.loads((eval_root / s / "scores.json").read_text("utf-8"))
        com.pop("status", None)
        if s == "triage_w":
            com.pop("false_negative_items", None)
        out[s] = diff_scores(json.loads(json.dumps(mine)), com)
    return out


def _main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("rescore", "triage-vs-screen"):
        p = sub.add_parser(name)
        p.add_argument("--store", required=True)
        p.add_argument("--build", required=True)
        p.add_argument("--out", default="")
        if name == "rescore":
            p.add_argument("--eval", default="evals/judge-2026-10-25j")
            p.add_argument("--compare", action="store_true")
    a = ap.parse_args(argv)
    from stack.fold import corpus_input
    from stack.store.index import store_index

    root = Path(a.store)
    b = store_index(root).build(a.build)
    if b is None:
        raise SystemExit(f"build {a.build}: no build record in {root}")
    rec = b.record
    tbl, _h, _p = corpus_input(root, rec["files"]["corpus"]["path"], fold_config="build")
    bt = load_build(root, rec, tbl)
    del tbl
    if a.cmd == "triage-vs-screen":
        res = triage_vs_screen(bt)
        print(triage_vs_screen_md(res))
    else:
        res = rescore(REPO_ROOT, a.eval, bt)
        print(json.dumps(res["coverage"], indent=1))
        if a.compare:
            d = compare_committed(REPO_ROOT / a.eval, res)
            res["compare"] = d
            for s, lines in d.items():
                print(f"{s}: {len(lines)} difference(s)")
                for x in lines[:40]:
                    print("  " + x)
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
